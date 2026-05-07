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

from basis_engine import BasisEngine
from cvd_engine import CVDEngine, TIMEFRAME_MS
from divergence import detect_divergences
from funding_engine import FundingOIEngine
from gex_engine import GexEngine
from liquidation_engine import LiquidationEngine
from live_liq_engine import LiveLiquidationEngine
from paper_engine import PaperEngine, valid_uid
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

SYMBOLS = [
    "BTCUSDT", "ETHUSDT", "SOLUSDT", "HYPEUSDT", "ZECUSDT", "TONUSDT",
    "NEARUSDT", "ONDOUSDT", "ENAUSDT", "MONUSDT", "MEGAUSDT", "OPUSDT",
    "BNBUSDT", "XMRUSDT",
]
# Coins with no Binance Spot pair — basis (spot vs perp) cannot be computed.
SYMBOLS_NO_SPOT = {"HYPEUSDT", "MONUSDT", "XMRUSDT"}
# GEX is only meaningful where a deep options market exists. Deribit lists
# BTC and ETH (SOL exists but OI is too thin to be reliable). Map perp symbol
# to Deribit currency code.
GEX_CURRENCIES = {"BTCUSDT": "BTC", "ETHUSDT": "ETH"}
DEFAULT_SYMBOL = "BTCUSDT"
TIMEFRAMES = list(TIMEFRAME_MS.keys())


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
    app.state.liquidation: dict[str, LiquidationEngine] = {}
    app.state.tasks: dict = {}

    # Construct everything first so we can seed in parallel (14 coins serially
    # would take 30+ seconds on cold start).
    seed_coros = []
    for sym in SYMBOLS:
        # CVD per (symbol, timeframe)
        for tf in TIMEFRAMES:
            eng = CVDEngine(sym, tf, store)
            app.state.engines[(sym.lower(), tf)] = eng
            seed_coros.append(eng.seed_history())

        # Funding/OI — every USDT-M perp has funding
        fund_eng = FundingOIEngine(sym, store)
        app.state.funding[sym] = fund_eng
        seed_coros.append(fund_eng.seed_history())

        # Basis — only when a Binance Spot pair exists for this base asset
        if sym not in SYMBOLS_NO_SPOT:
            basis_eng = BasisEngine(sym, store)
            app.state.basis[sym] = basis_eng
            seed_coros.append(basis_eng.seed_history())

        # Live liquidation WS (forceOrder) — exists on every USDT-M perp
        app.state.live_liq[sym] = LiveLiquidationEngine(sym)

        # Taker ratio (kline_5m/15m/1h) — every perp
        taker_eng = TakerEngine(sym, store)
        app.state.taker[sym] = taker_eng
        seed_coros.append(taker_eng.seed_history())

        # GEX — only if Deribit lists this currency's options
        if sym in GEX_CURRENCIES:
            gex_eng = GexEngine(sym, store, currency=GEX_CURRENCIES[sym])
            app.state.gex[sym] = gex_eng
            seed_coros.append(gex_eng.seed_history())

        # Coinalyze liquidation heatmap — same ticker pattern across coins;
        # if Coinalyze doesn't list it, the engine logs and returns empty.
        if COINALYZE_API_KEY:
            liq_eng = LiquidationEngine(sym, f"{sym}_PERP.A", COINALYZE_API_KEY, store)
            app.state.liquidation[sym] = liq_eng
            seed_coros.append(liq_eng.seed_history())

    print(f"[boot] seeding {len(seed_coros)} engines for {len(SYMBOLS)} symbols (parallel)")
    await asyncio.gather(*seed_coros, return_exceptions=True)

    # Now spin up the run loops.
    for (sym, tf), eng in app.state.engines.items():
        app.state.tasks[("cvd", sym, tf)] = asyncio.create_task(eng.run())
    for sym, e in app.state.funding.items():
        app.state.tasks[("funding", sym)] = asyncio.create_task(e.run())
    for sym, e in app.state.basis.items():
        app.state.tasks[("basis", sym)] = asyncio.create_task(e.run())
    for sym, e in app.state.live_liq.items():
        app.state.tasks[("live_liq", sym)] = asyncio.create_task(e.run())
    for sym, e in app.state.taker.items():
        app.state.tasks[("taker", sym)] = asyncio.create_task(e.run())
    for sym, e in app.state.gex.items():
        app.state.tasks[("gex", sym)] = asyncio.create_task(e.run())
    for sym, e in app.state.liquidation.items():
        app.state.tasks[("liq", sym)] = asyncio.create_task(e.run())

    # Paper trading uses BTC perp mid as the mark (paper trading is BTC-only
    # by design — keeps the leverage math simple and the demo focused).
    btc_basis = app.state.basis.get("BTCUSDT")
    paper_eng = PaperEngine(store, btc_basis)
    app.state.paper = paper_eng
    app.state.tasks[("paper",)] = asyncio.create_task(paper_eng.run())

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


@app.get("/api/symbols", dependencies=[Depends(require_auth)])
async def symbols():
    # Per-symbol capability map so the frontend knows which panels to show.
    caps = {
        sym: {
            "spot": sym not in SYMBOLS_NO_SPOT,
            "gex": sym in GEX_CURRENCIES,
            "heatmap": bool(COINALYZE_API_KEY) and sym in app.state.liquidation,
        } for sym in SYMBOLS
    }
    return {
        "symbols": SYMBOLS,
        "default": DEFAULT_SYMBOL,
        "timeframes": TIMEFRAMES,
        "capabilities": caps,
    }


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
    eng = app.state.basis.get(sym)
    if eng is None:
        await ws.close(code=4404)
        return
    await ws.accept()
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
    try:
        trade = eng.open_trade(
            u,
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
    eng = app.state.taker.get(sym)
    if eng is None:
        await ws.close(code=4404)
        return
    await ws.accept()
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
    eng = app.state.live_liq.get(sym)
    if eng is None:
        await ws.close(code=4404)
        return
    await ws.accept()
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
    eng = app.state.liquidation.get(sym)
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
    eng = app.state.funding.get(sym)
    if eng is None:
        await ws.close(code=4404)
        return
    await ws.accept()
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
    key = (symbol.lower(), timeframe)
    engine: CVDEngine | None = app.state.engines.get(key)
    if engine is None:
        await ws.close(code=4404)
        return

    await ws.accept()
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
