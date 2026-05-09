"""
FastAPI server. All (symbol, timeframe) engines start at boot and run 24/7,
persisting every candle to SQLite. Frontend clients subscribe to already-
running engines.
"""
import asyncio
import json
import os
import secrets
from pathlib import Path
from contextlib import asynccontextmanager

from dotenv import load_dotenv
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException, Header, Query, Depends
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from ai_engine import AIAnalyzer, gather_context
from basis_engine import BasisEngine
from cvd_engine import CVDEngine, TIMEFRAME_MS
from divergence import detect_divergences
from funding_engine import FundingOIEngine
from gex_engine import GexEngine
from iceberg_engine import IcebergEngine
from liquidation_engine import LiquidationEngine
from live_liq_engine import LiveLiquidationEngine
from paper_engine import PaperEngine, valid_uid
from symbol_registry import SymbolRegistry
from taker_engine import TakerEngine
from storage import Store


load_dotenv(Path(__file__).resolve().parent / ".env")
ACCESS_TOKEN = os.environ.get("IX_ACCESS_TOKEN", "").strip()
if not ACCESS_TOKEN or len(ACCESS_TOKEN) < 24:
    raise RuntimeError(
        "IX_ACCESS_TOKEN must be set in backend/.env and be at least 24 chars long. "
        "Generate one with: python -c \"import secrets; print(secrets.token_urlsafe(18))\""
    )

COINALYZE_API_KEY = os.environ.get("COINALYZE_API_KEY", "").strip()
OPENCODE_API_KEY = os.environ.get("OPENCODE_API_KEY", "").strip()
OPENCODE_BASE_URL = os.environ.get("OPENCODE_BASE_URL", "https://openrouter.ai/api/v1").strip()
OPENCODE_MODEL = os.environ.get("OPENCODE_MODEL", "moonshotai/kimi-k2").strip()
OPENCODE_PROXY = os.environ.get("OPENCODE_PROXY", "").strip()


def _check_token(candidate: str | None) -> bool:
    if not candidate:
        return False
    return secrets.compare_digest(candidate, ACCESS_TOKEN)


async def require_auth(
    x_auth_token: str | None = Header(default=None, alias="X-Auth-Token"),
    token: str | None = Query(default=None),
):
    if not _check_token(x_auth_token or token):
        raise HTTPException(status_code=401, detail="unauthorized")


FRONTEND_DIR = Path(__file__).resolve().parent.parent / "frontend"

# Warm symbols: spawned at boot, kept running 24/7. Used to keep BTC/ETH
# always-instant for default loads + paper trading mark + GEX (Deribit
# only lists BTC/ETH options anyway, so GEX must live here).
WARM_SYMBOLS = ["BTCUSDT", "ETHUSDT"]
# Override spot source for coins with no Binance USDT spot pair. Lazy-spawned
# coins not in this map use Binance spot if available, else basis is skipped.
ALT_SPOT_SOURCES: dict[str, dict] = {
    "HYPEUSDT": {"exchange": "bybit",  "spot_symbol": "HYPEUSDT"},
    "XMRUSDT":  {"exchange": "kucoin", "spot_symbol": "XMR-USDT"},
}
# GEX is only meaningful where a deep options market exists.
GEX_CURRENCIES = {"BTCUSDT": "BTC", "ETHUSDT": "ETH"}
DEFAULT_SYMBOL = "BTCUSDT"
TIMEFRAMES = list(TIMEFRAME_MS.keys())


async def _spawn_engines_for(app: FastAPI, sym: str, store: Store, with_gex: bool = False):
    """
    Construct + seed + start every engine relevant for `sym`. Idempotent: if
    a given engine already exists for the symbol, skip it. Caller must ensure
    the symbol is a valid Binance USDT-M perp (use the registry).
    """
    sym = sym.upper()
    registry: SymbolRegistry = app.state.registry

    seed_coros = []

    # CVD per timeframe
    for tf in TIMEFRAMES:
        key = (sym.lower(), tf)
        if key not in app.state.engines:
            eng = CVDEngine(sym, tf, store)
            app.state.engines[key] = eng
            seed_coros.append(eng.seed_history())

    # Funding/OI
    if sym not in app.state.funding:
        fund_eng = FundingOIEngine(sym, store)
        app.state.funding[sym] = fund_eng
        seed_coros.append(fund_eng.seed_history())

    # Basis — only if a spot pair exists (Binance, Bybit alt, or KuCoin alt)
    if sym not in app.state.basis:
        alt = ALT_SPOT_SOURCES.get(sym)
        if alt:
            basis_eng = BasisEngine(
                sym, store,
                spot_exchange=alt["exchange"],
                spot_symbol=alt["spot_symbol"],
            )
            app.state.basis[sym] = basis_eng
            seed_coros.append(basis_eng.seed_history())
        elif registry.has_spot(sym):
            basis_eng = BasisEngine(sym, store)
            app.state.basis[sym] = basis_eng
            seed_coros.append(basis_eng.seed_history())
        # else: no spot anywhere — basis pane will show "no spot pair"

    # Live liquidation feed
    if sym not in app.state.live_liq:
        app.state.live_liq[sym] = LiveLiquidationEngine(sym)

    # Iceberg / whale absorption
    if sym not in app.state.iceberg:
        app.state.iceberg[sym] = IcebergEngine(sym)

    # Taker ratio
    if sym not in app.state.taker:
        taker_eng = TakerEngine(sym, store)
        app.state.taker[sym] = taker_eng
        seed_coros.append(taker_eng.seed_history())

    # GEX — only when explicitly requested AND Deribit lists this asset
    if with_gex and sym in GEX_CURRENCIES and sym not in app.state.gex:
        gex_eng = GexEngine(sym, store, currency=GEX_CURRENCIES[sym])
        app.state.gex[sym] = gex_eng
        seed_coros.append(gex_eng.seed_history())

    # Coinalyze liquidation heatmap
    if COINALYZE_API_KEY and sym not in app.state.liquidation:
        liq_eng = LiquidationEngine(sym, f"{sym}_PERP.A", COINALYZE_API_KEY, store)
        app.state.liquidation[sym] = liq_eng
        seed_coros.append(liq_eng.seed_history())

    if seed_coros:
        await asyncio.gather(*seed_coros, return_exceptions=True)

    # Start any tasks that aren't running yet
    tasks = app.state.tasks
    for tf in TIMEFRAMES:
        k = ("cvd", sym, tf)
        if k not in tasks:
            tasks[k] = asyncio.create_task(app.state.engines[(sym.lower(), tf)].run())
    if ("funding", sym) not in tasks:
        tasks[("funding", sym)] = asyncio.create_task(app.state.funding[sym].run())
    if sym in app.state.basis and ("basis", sym) not in tasks:
        tasks[("basis", sym)] = asyncio.create_task(app.state.basis[sym].run())
    if ("live_liq", sym) not in tasks:
        tasks[("live_liq", sym)] = asyncio.create_task(app.state.live_liq[sym].run())
    if ("iceberg", sym) not in tasks:
        tasks[("iceberg", sym)] = asyncio.create_task(app.state.iceberg[sym].run())
    if ("taker", sym) not in tasks:
        tasks[("taker", sym)] = asyncio.create_task(app.state.taker[sym].run())
    if sym in app.state.gex and ("gex", sym) not in tasks:
        tasks[("gex", sym)] = asyncio.create_task(app.state.gex[sym].run())
    if sym in app.state.liquidation and ("liq", sym) not in tasks:
        tasks[("liq", sym)] = asyncio.create_task(app.state.liquidation[sym].run())


async def ensure_engines(sym: str) -> bool:
    """
    Spawn engines for `sym` on first request. Returns True if the symbol is
    a tradeable Binance perp (or already spawned), False otherwise.
    Concurrency-safe via a per-symbol lock so duplicate WS connects don't
    spawn duplicate engines.
    """
    sym = sym.upper()
    registry: SymbolRegistry = app.state.registry
    if not registry.has_perp(sym):
        return False
    if sym in app.state.taker:
        return True   # fast-path: already spawned

    locks = app.state.spawn_locks
    lock = locks.get(sym)
    if lock is None:
        lock = asyncio.Lock()
        locks[sym] = lock
    async with lock:
        if sym in app.state.taker:
            return True
        print(f"[lazy] spawning engines for {sym}")
        await _spawn_engines_for(app, sym, app.state.store, with_gex=False)
    return True


@asynccontextmanager
async def lifespan(app: FastAPI):
    store = Store()
    app.state.store = store

    # All engines indexed by perp symbol. CVD is also keyed by timeframe.
    app.state.engines: dict[tuple[str, str], CVDEngine] = {}
    app.state.funding: dict[str, FundingOIEngine] = {}
    app.state.basis: dict[str, BasisEngine] = {}
    app.state.live_liq: dict[str, LiveLiquidationEngine] = {}
    app.state.taker: dict[str, TakerEngine] = {}
    app.state.gex: dict[str, GexEngine] = {}
    app.state.iceberg: dict[str, IcebergEngine] = {}
    app.state.liquidation: dict[str, LiquidationEngine] = {}
    app.state.tasks: dict = {}
    app.state.spawn_locks: dict[str, asyncio.Lock] = {}

    # Boot the symbol registry first — drives validation + search + meta.
    registry = SymbolRegistry()
    await registry.init()
    app.state.registry = registry

    # Spawn engines for the warm set (BTC + ETH) so they're instant on first
    # load and so GEX can run (Deribit only lists those two anyway).
    print(f"[boot] spawning warm engines for {WARM_SYMBOLS}")
    for sym in WARM_SYMBOLS:
        await _spawn_engines_for(app, sym, store, with_gex=(sym in GEX_CURRENCIES))

    # Paper trading mark provider: any of the 14 coins. Basis engine has the
    # most accurate live mid (2s polling); CVD candles are the fallback when
    # basis is unavailable for a coin (HYPE/MON/XMR or initial seeding).
    def _mark_provider(symbol: str) -> float:
        sym = (symbol or "").upper()
        b = app.state.basis.get(sym)
        if b is not None and getattr(b, "perp", 0) > 0:
            return float(b.perp)
        for tf in ("1m", "5m", "15m", "1h"):
            eng = app.state.engines.get((sym.lower(), tf))
            if eng and eng.candles and eng.candles[-1].close > 0:
                return float(eng.candles[-1].close)
        return 0.0

    paper_eng = PaperEngine(store, _mark_provider)
    app.state.paper = paper_eng
    app.state.tasks[("paper",)] = asyncio.create_task(paper_eng.run())

    # AI analyzer (manual on-demand, no background loop).
    app.state.ai = AIAnalyzer(OPENCODE_API_KEY, OPENCODE_BASE_URL, OPENCODE_MODEL, OPENCODE_PROXY)
    if OPENCODE_API_KEY:
        print(f"[boot] AI analyzer enabled (model={OPENCODE_MODEL})")
    else:
        print("[boot] OPENCODE_API_KEY not set — AI analyzer disabled (icon still appears, click shows config message)")

    print(f"[boot] all engines running ({len(app.state.tasks)} tasks)")

    try:
        yield
    finally:
        for t in app.state.tasks.values():
            t.cancel()
        await asyncio.gather(*app.state.tasks.values(), return_exceptions=True)
        for eng in app.state.engines.values():
            if eng.candles and eng.candles[-1].observed:
                store.upsert(eng.symbol.upper(), eng.timeframe, eng.candles[-1].to_dict())


app = FastAPI(lifespan=lifespan)


# ---------------------------------------------------------------------------
# WS keepalive: nginx (and most reverse proxies) close idle WebSocket
# connections after ~60s. Engines that emit infrequently (GEX = 5min) silently
# die between pushes. This pump sends a small {"event":"ping"} every 25s when
# the queue is quiet so the connection stays warm. Frontend ignores unknown
# events, so ping requires no client changes.
# ---------------------------------------------------------------------------
WS_KEEPALIVE_SEC = 25.0
_WS_PING = json.dumps({"event": "ping"})


async def _ws_pump(ws: WebSocket, queue: asyncio.Queue):
    while True:
        try:
            msg = await asyncio.wait_for(queue.get(), timeout=WS_KEEPALIVE_SEC)
        except asyncio.TimeoutError:
            await ws.send_text(_WS_PING)
            continue
        await ws.send_text(json.dumps(msg))


def _resolve_engine(d: dict, symbol: str | None, what: str):
    sym = (symbol or DEFAULT_SYMBOL).upper()
    eng = d.get(sym)
    if eng is None:
        raise HTTPException(status_code=404, detail=f"no {what} for {sym}")
    return eng, sym

# Allow the frontend served from a different origin (e.g., Vercel) to call us.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["X-Auth-Token", "Content-Type"],
)


@app.get("/api/auth/check")
async def auth_check(_: None = Depends(require_auth)):
    return {"ok": True}


def _caps_for(sym: str) -> dict:
    """Compute per-symbol capability flags. Used both by /api/symbols (for
    warm) and per-symbol meta endpoints. The frontend uses this to show or
    grey-out panels (basis, GEX, heatmap)."""
    sym = sym.upper()
    registry: SymbolRegistry = app.state.registry
    has_spot_anywhere = registry.has_spot(sym) or sym in ALT_SPOT_SOURCES
    return {
        "spot": has_spot_anywhere,
        "gex": sym in GEX_CURRENCIES,
        "heatmap": bool(COINALYZE_API_KEY),
        "perp": registry.has_perp(sym),
    }


@app.get("/api/symbols", dependencies=[Depends(require_auth)])
async def symbols():
    # Just enough for the frontend to bootstrap. Search + per-symbol meta
    # supersede the old hardcoded dropdown, so we only ship the warm set.
    caps = {sym: _caps_for(sym) for sym in WARM_SYMBOLS}
    return {
        "symbols": WARM_SYMBOLS,
        "default": DEFAULT_SYMBOL,
        "timeframes": TIMEFRAMES,
        "capabilities": caps,
    }


@app.get("/api/symbols/search", dependencies=[Depends(require_auth)])
async def symbols_search(q: str = "", limit: int = 12):
    if not q.strip():
        return {"results": []}
    registry: SymbolRegistry = app.state.registry
    results = await registry.search(q, limit=limit)
    return {"results": results}


@app.get("/api/symbols/meta", dependencies=[Depends(require_auth)])
async def symbols_meta(symbol: str):
    sym = symbol.upper()
    registry: SymbolRegistry = app.state.registry
    if not registry.has_perp(sym):
        raise HTTPException(status_code=404, detail=f"{sym} not on Binance USDT-M perps")
    meta = await registry.get_meta(sym)
    meta["capabilities"] = _caps_for(sym)
    return meta


@app.get("/api/status", dependencies=[Depends(require_auth)])
async def status(symbol: str | None = None):
    target = symbol.upper() if symbol else None
    out = []
    for (sym, tf), eng in app.state.engines.items():
        if target and sym.upper() != target:
            continue
        last = eng.candles[-1].to_dict() if eng.candles else None
        observed = sum(1 for c in eng.candles if c.observed)
        out.append({
            "symbol": sym.upper(),
            "timeframe": tf,
            "candles": len(eng.candles),
            "observed": observed,
            "cvd": eng.cvd,
            "last": last,
        })
    return {"engines": out}


@app.get("/api/funding", dependencies=[Depends(require_auth)])
async def funding_status(symbol: str = DEFAULT_SYMBOL):
    eng, _ = _resolve_engine(app.state.funding, symbol, "funding engine")
    return eng.snapshot_history()


@app.get("/api/basis", dependencies=[Depends(require_auth)])
async def basis_status(symbol: str = DEFAULT_SYMBOL):
    eng, _ = _resolve_engine(app.state.basis, symbol, "basis engine")
    return eng.snapshot_history()


@app.websocket("/ws/basis")
async def basis_feed(
    ws: WebSocket,
    token: str | None = Query(default=None),
    symbol: str = Query(default=DEFAULT_SYMBOL),
):
    if not _check_token(token):
        await ws.close(code=4401)
        return
    sym = (symbol or DEFAULT_SYMBOL).upper()
    # Accept FIRST so the WS handshake completes immediately and the browser
    # holds the connection. Spawning engines for a fresh coin can take 5-15s
    # during seed (REST calls to Binance for klines/funding/etc.) — without an
    # early accept the handshake stalls and the browser drops it.
    await ws.accept()
    if not await ensure_engines(sym):
        await ws.close(code=4404); return
    eng = app.state.basis.get(sym)
    if eng is None:
        await ws.close(code=4404)
        return
    queue: asyncio.Queue = asyncio.Queue(maxsize=400)

    async def listener(event: str, payload: dict):
        try:
            queue.put_nowait({"event": event, "data": payload})
        except asyncio.QueueFull:
            pass

    eng.on_update(listener)
    try:
        await ws.send_text(json.dumps({"event": "snapshot", "data": eng.snapshot_history()}))
        await _ws_pump(ws, queue)
    except WebSocketDisconnect:
        pass
    finally:
        eng.off_update(listener)


def _paper_uid_or_400(uid: str | None) -> str:
    if not valid_uid(uid):
        raise HTTPException(status_code=400, detail="invalid uid")
    return uid  # type: ignore[return-value]


@app.get("/api/paper/account", dependencies=[Depends(require_auth)])
async def paper_account(uid: str = Query(...)):
    u = _paper_uid_or_400(uid)
    eng: PaperEngine = app.state.paper
    return eng.account_snapshot(u)


@app.get("/api/paper/trades", dependencies=[Depends(require_auth)])
async def paper_trades(uid: str = Query(...), limit: int = 100):
    u = _paper_uid_or_400(uid)
    eng: PaperEngine = app.state.paper
    return {"trades": eng.history(u, limit)}


@app.post("/api/paper/open", dependencies=[Depends(require_auth)])
async def paper_open(payload: dict):
    u = _paper_uid_or_400(payload.get("uid"))
    eng: PaperEngine = app.state.paper
    sym = (payload.get("symbol") or DEFAULT_SYMBOL).upper()
    if sym not in SYMBOLS:
        raise HTTPException(status_code=400, detail=f"unsupported symbol {sym}")
    try:
        trade = eng.open_trade(
            u,
            symbol=sym,
            side=payload.get("side", ""),
            size_usd=payload.get("size_usd", 0),
            leverage=payload.get("leverage", 1),
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"trade": trade, "account": eng.account_snapshot(u)}


@app.post("/api/paper/close", dependencies=[Depends(require_auth)])
async def paper_close(payload: dict):
    u = _paper_uid_or_400(payload.get("uid"))
    try:
        tid = int(payload.get("trade_id"))
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="invalid trade_id")
    eng: PaperEngine = app.state.paper
    try:
        trade = eng.close_trade(u, tid)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"trade": trade, "account": eng.account_snapshot(u)}


@app.post("/api/paper/reset", dependencies=[Depends(require_auth)])
async def paper_reset(payload: dict):
    u = _paper_uid_or_400(payload.get("uid"))
    eng: PaperEngine = app.state.paper
    eng.reset_account(u)
    return {"account": eng.account_snapshot(u)}


@app.websocket("/ws/paper")
async def paper_feed(ws: WebSocket, token: str | None = Query(default=None), uid: str | None = Query(default=None)):
    if not _check_token(token):
        await ws.close(code=4401)
        return
    if not valid_uid(uid):
        await ws.close(code=4400)
        return
    eng: PaperEngine = app.state.paper
    await ws.accept()
    queue: asyncio.Queue = asyncio.Queue(maxsize=200)

    async def listener(event: str, payload: dict):
        try:
            queue.put_nowait({"event": event, "data": payload})
        except asyncio.QueueFull:
            pass

    eng.on_update(uid, listener)  # type: ignore[arg-type]
    try:
        await ws.send_text(json.dumps({"event": "snapshot", "data": eng.account_snapshot(uid)}))  # type: ignore[arg-type]
        await _ws_pump(ws, queue)
    except WebSocketDisconnect:
        pass
    finally:
        eng.off_update(uid, listener)  # type: ignore[arg-type]


@app.websocket("/ws/ai-analysis")
async def ai_analysis_feed(
    ws: WebSocket,
    token: str | None = Query(default=None),
    symbol: str = Query(default=DEFAULT_SYMBOL),
):
    if not _check_token(token):
        await ws.close(code=4401)
        return
    sym = (symbol or DEFAULT_SYMBOL).upper()
    if sym not in SYMBOLS:
        await ws.close(code=4400)
        return
    ai: AIAnalyzer = app.state.ai
    await ws.accept()

    try:
        # Wait for the client's "start" command before doing any work.
        # This pattern lets the client open the socket while the modal is
        # still showing the intro screen and only kick off the analysis
        # when the user clicks Run.
        first = await ws.receive_text()
        try:
            msg = json.loads(first)
        except json.JSONDecodeError:
            msg = {}
        if msg.get("action") != "start":
            await ws.send_text(json.dumps(
                {"event": "error", "message": "expected first message {action: 'start'}"}
            ))
            await ws.close()
            return

        if not ai.enabled:
            await ws.send_text(json.dumps({
                "event": "error",
                "message": "AI is not configured on the server. Set OPENCODE_API_KEY in backend/.env.",
            }))
            await ws.close()
            return

        ctx = gather_context(app.state, sym)
        async for event in ai.analyze_stream(sym, ctx):
            await ws.send_text(json.dumps(event))
        await ws.close()
    except WebSocketDisconnect:
        pass
    except Exception as e:
        try:
            await ws.send_text(json.dumps({"event": "error", "message": str(e)}))
            await ws.close()
        except Exception:
            pass


@app.get("/api/gex", dependencies=[Depends(require_auth)])
async def gex_status(symbol: str = DEFAULT_SYMBOL):
    sym = (symbol or DEFAULT_SYMBOL).upper()
    eng = app.state.gex.get(sym)
    if eng is None:
        return {"available": False, "snapshot": None, "symbol": sym}
    return {"available": True, "snapshot": eng.snapshot(), "symbol": sym}


@app.websocket("/ws/gex")
async def gex_feed(
    ws: WebSocket,
    token: str | None = Query(default=None),
    symbol: str = Query(default=DEFAULT_SYMBOL),
):
    if not _check_token(token):
        await ws.close(code=4401)
        return
    sym = (symbol or DEFAULT_SYMBOL).upper()
    eng = app.state.gex.get(sym)
    if eng is None:
        await ws.close(code=4404)
        return
    await ws.accept()
    queue: asyncio.Queue = asyncio.Queue(maxsize=50)

    async def listener(event: str, payload: dict):
        try:
            queue.put_nowait({"event": event, "data": payload})
        except asyncio.QueueFull:
            pass

    eng.on_update(listener)
    try:
        snap = eng.snapshot()
        if snap is not None:
            await ws.send_text(json.dumps({"event": "snapshot", "data": snap}))
        await _ws_pump(ws, queue)
    except WebSocketDisconnect:
        pass
    finally:
        eng.off_update(listener)


@app.get("/api/taker", dependencies=[Depends(require_auth)])
async def taker_status(symbol: str = DEFAULT_SYMBOL):
    eng, _ = _resolve_engine(app.state.taker, symbol, "taker engine")
    return eng.snapshot_history()


@app.websocket("/ws/taker")
async def taker_feed(
    ws: WebSocket,
    token: str | None = Query(default=None),
    symbol: str = Query(default=DEFAULT_SYMBOL),
):
    if not _check_token(token):
        await ws.close(code=4401)
        return
    sym = (symbol or DEFAULT_SYMBOL).upper()
    await ws.accept()
    if not await ensure_engines(sym):
        await ws.close(code=4404); return
    eng = app.state.taker.get(sym)
    if eng is None:
        await ws.close(code=4404)
        return
    queue: asyncio.Queue = asyncio.Queue(maxsize=400)

    async def listener(event: str, payload: dict):
        try:
            queue.put_nowait({"event": event, "data": payload})
        except asyncio.QueueFull:
            pass

    eng.on_update(listener)
    try:
        await ws.send_text(json.dumps({"event": "snapshot", "data": eng.snapshot_history()}))
        await _ws_pump(ws, queue)
    except WebSocketDisconnect:
        pass
    finally:
        eng.off_update(listener)


@app.get("/api/iceberg", dependencies=[Depends(require_auth)])
async def iceberg_status(symbol: str = DEFAULT_SYMBOL):
    sym = (symbol or DEFAULT_SYMBOL).upper()
    eng = app.state.iceberg.get(sym)
    if eng is None:
        return {"available": False, "snapshot": None, "symbol": sym}
    return {"available": True, "snapshot": eng.snapshot(), "symbol": sym}


@app.websocket("/ws/iceberg")
async def iceberg_feed(
    ws: WebSocket,
    token: str | None = Query(default=None),
    symbol: str = Query(default=DEFAULT_SYMBOL),
):
    if not _check_token(token):
        await ws.close(code=4401)
        return
    sym = (symbol or DEFAULT_SYMBOL).upper()
    await ws.accept()
    if not await ensure_engines(sym):
        await ws.close(code=4404); return
    eng = app.state.iceberg.get(sym)
    if eng is None:
        await ws.close(code=4404)
        return
    queue: asyncio.Queue = asyncio.Queue(maxsize=50)

    async def listener(event: str, payload: dict):
        try:
            queue.put_nowait({"event": event, "data": payload})
        except asyncio.QueueFull:
            pass

    eng.on_update(listener)
    try:
        # Send current snapshot immediately
        snap = eng.snapshot()
        await ws.send_text(json.dumps({"event": "snapshot", "data": snap}))
        await _ws_pump(ws, queue)
    except WebSocketDisconnect:
        pass
    finally:
        eng.off_update(listener)


@app.websocket("/ws/live-liq")
async def live_liq_feed(
    ws: WebSocket,
    token: str | None = Query(default=None),
    symbol: str = Query(default=DEFAULT_SYMBOL),
):
    if not _check_token(token):
        await ws.close(code=4401)
        return
    sym = (symbol or DEFAULT_SYMBOL).upper()
    await ws.accept()
    if not await ensure_engines(sym):
        await ws.close(code=4404); return
    eng = app.state.live_liq.get(sym)
    if eng is None:
        await ws.close(code=4404)
        return
    queue: asyncio.Queue = asyncio.Queue(maxsize=200)

    async def listener(event: str, payload: dict):
        try:
            queue.put_nowait({"event": event, "data": payload})
        except asyncio.QueueFull:
            pass

    eng.on_update(listener)
    try:
        await _ws_pump(ws, queue)
    except WebSocketDisconnect:
        pass
    finally:
        eng.off_update(listener)


@app.get("/api/liquidations", dependencies=[Depends(require_auth)])
async def liquidations_status(symbol: str = DEFAULT_SYMBOL):
    sym = (symbol or DEFAULT_SYMBOL).upper()
    eng = app.state.liquidation.get(sym)
    if eng is None:
        return {"enabled": False, "snapshot": None, "symbol": sym}
    return {"enabled": True, "snapshot": eng.snapshot(), "symbol": sym}


@app.websocket("/ws/liquidations")
async def liquidations_feed(
    ws: WebSocket,
    token: str | None = Query(default=None),
    symbol: str = Query(default=DEFAULT_SYMBOL),
):
    if not _check_token(token):
        await ws.close(code=4401)
        return
    sym = (symbol or DEFAULT_SYMBOL).upper()
    await ws.accept()
    if not await ensure_engines(sym):
        await ws.close(code=4404); return
    eng = app.state.liquidation.get(sym)
    if eng is None:
        await ws.close(code=4404)
        return
    queue: asyncio.Queue = asyncio.Queue(maxsize=50)

    async def listener(event: str, payload: dict):
        try:
            queue.put_nowait({"event": event, "data": payload})
        except asyncio.QueueFull:
            pass

    eng.on_update(listener)
    try:
        await ws.send_text(json.dumps({"event": "snapshot", "data": eng.snapshot()}))
        await _ws_pump(ws, queue)
    except WebSocketDisconnect:
        pass
    finally:
        eng.off_update(listener)


@app.websocket("/ws/funding")
async def funding_feed(
    ws: WebSocket,
    token: str | None = Query(default=None),
    symbol: str = Query(default=DEFAULT_SYMBOL),
):
    if not _check_token(token):
        await ws.close(code=4401)
        return
    sym = (symbol or DEFAULT_SYMBOL).upper()
    await ws.accept()
    if not await ensure_engines(sym):
        await ws.close(code=4404); return
    eng = app.state.funding.get(sym)
    if eng is None:
        await ws.close(code=4404)
        return
    queue: asyncio.Queue = asyncio.Queue(maxsize=200)

    async def listener(event: str, payload: dict):
        try:
            queue.put_nowait({"event": event, "data": payload})
        except asyncio.QueueFull:
            pass

    eng.on_update(listener)
    try:
        await ws.send_text(json.dumps({"event": "snapshot", "data": eng.snapshot_history()}))
        await _ws_pump(ws, queue)
    except WebSocketDisconnect:
        pass
    finally:
        eng.off_update(listener)


@app.websocket("/ws/{symbol}/{timeframe}")
async def feed(ws: WebSocket, symbol: str, timeframe: str, token: str | None = Query(default=None)):
    if not _check_token(token):
        await ws.close(code=4401)
        return
    if timeframe not in TIMEFRAME_MS:
        await ws.close(code=4400)
        return
    # Accept FIRST — engine spawn for a fresh coin can take 5-15s for the
    # historical klines seed, and the browser will timeout the handshake if
    # we wait. Listener is attached after seed completes.
    await ws.accept()
    if not await ensure_engines(symbol):
        await ws.close(code=4404)
        return
    key = (symbol.lower(), timeframe)
    engine: CVDEngine | None = app.state.engines.get(key)
    if engine is None:
        await ws.close(code=4404)
        return
    queue: asyncio.Queue = asyncio.Queue(maxsize=200)

    async def listener(event: str, payload: dict):
        try:
            queue.put_nowait({"event": event, "data": payload})
        except asyncio.QueueFull:
            pass

    engine.on_update(listener)

    try:
        snap = engine.snapshot()
        divs = detect_divergences(snap)
        await ws.send_text(json.dumps({"event": "snapshot", "data": {"candles": snap, "divergences": divs}}))

        # Inline pump (with keepalive) so we can run divergence detection on
        # closed-candle events without forking the helper.
        while True:
            try:
                msg = await asyncio.wait_for(queue.get(), timeout=WS_KEEPALIVE_SEC)
            except asyncio.TimeoutError:
                await ws.send_text(_WS_PING)
                continue
            await ws.send_text(json.dumps(msg))
            if msg["event"] == "candle":
                divs = detect_divergences(engine.snapshot())
                await ws.send_text(json.dumps({"event": "divergences", "data": divs}))
    except WebSocketDisconnect:
        pass
    finally:
        engine.off_update(listener)


@app.get("/")
async def index():
    return FileResponse(FRONTEND_DIR / "index.html")


app.mount("/static", StaticFiles(directory=FRONTEND_DIR), name="static")
