"""
iX — Bitunix ICT scalping platform.
FastAPI server: market data, ICT analysis, signals, paper trading, AI.

Engines per symbol (lazy-spawned on first connection):
  MarketEngine   — Bitunix WS klines + trades for all timeframes
  FundingEngine  — Bitunix funding rate polling

ICT analysis runs synchronously on every candle close event, then the
result is broadcast to all connected clients for that (symbol, timeframe).
"""
import asyncio
import json
import os
import secrets
from contextlib import asynccontextmanager
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException, Header, Query, Depends
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

import ict_engine
import signal_engine
from ai_engine import AIAnalyzer, gather_context
from bitunix_funding import FundingEngine
from bitunix_registry import BitunixRegistry
from market_engine import MarketEngine, TIMEFRAME_MS
from paper_engine import PaperEngine, valid_uid
from storage import Store


load_dotenv(Path(__file__).resolve().parent / ".env")

ACCESS_TOKEN = os.environ.get("IX_ACCESS_TOKEN", "").strip()
if not ACCESS_TOKEN or len(ACCESS_TOKEN) < 24:
    raise RuntimeError(
        "IX_ACCESS_TOKEN must be set in backend/.env and be >= 24 chars. "
        "Generate: python -c \"import secrets; print(secrets.token_urlsafe(18))\""
    )

OPENCODE_API_KEY  = os.environ.get("OPENCODE_API_KEY", "").strip()
OPENCODE_BASE_URL = os.environ.get("OPENCODE_BASE_URL", "https://openrouter.ai/api/v1").strip()
OPENCODE_MODEL    = os.environ.get("OPENCODE_MODEL", "moonshotai/kimi-k2").strip()
OPENCODE_PROXY    = os.environ.get("OPENCODE_PROXY", "").strip()

FRONTEND_DIR   = Path(__file__).resolve().parent.parent / "frontend"
DEFAULT_SYMBOL = "BTCUSDT"
WARM_SYMBOLS   = ["BTCUSDT", "ETHUSDT"]
TIMEFRAMES     = list(TIMEFRAME_MS.keys())
WS_KEEPALIVE   = 25.0
_WS_PING       = json.dumps({"event": "ping"})


# ---------------------------------------------------------------------------
# Auth helpers
# ---------------------------------------------------------------------------

def _check_token(candidate: str | None) -> bool:
    if not candidate:
        return False
    return secrets.compare_digest(candidate, ACCESS_TOKEN)


async def require_auth(
    x_auth_token: str | None = Header(default=None, alias="X-Auth-Token"),
    token:        str | None = Query(default=None),
):
    if not _check_token(x_auth_token or token):
        raise HTTPException(status_code=401, detail="unauthorized")


# ---------------------------------------------------------------------------
# Engine lifecycle
# ---------------------------------------------------------------------------

async def _spawn_symbol(app: FastAPI, sym: str, store: Store):
    """
    Idempotent: create + seed + start MarketEngine + FundingEngine for sym.
    """
    sym = sym.upper()
    registry: BitunixRegistry = app.state.registry

    if not registry.has_perp(sym):
        return False

    tasks: dict = app.state.tasks

    if sym not in app.state.market:
        eng = MarketEngine(sym, store)
        app.state.market[sym] = eng
        await eng.seed_history()

    if sym not in app.state.funding:
        fnd = FundingEngine(sym, store)
        app.state.funding[sym] = fnd
        await fnd.seed_history()

    if ("market", sym) not in tasks:
        tasks[("market", sym)] = asyncio.create_task(app.state.market[sym].run())
    if ("funding", sym) not in tasks:
        tasks[("funding", sym)] = asyncio.create_task(app.state.funding[sym].run())

    return True


async def ensure_engines(sym: str) -> bool:
    sym = sym.upper()
    registry: BitunixRegistry = app.state.registry
    if not registry.has_perp(sym):
        return False
    if sym in app.state.market:
        return True
    locks: dict = app.state.spawn_locks
    if sym not in locks:
        locks[sym] = asyncio.Lock()
    async with locks[sym]:
        if sym in app.state.market:
            return True
        print(f"[lazy] spawning engines for {sym}")
        return await _spawn_symbol(app, sym, app.state.store)


# ---------------------------------------------------------------------------
# ICT analysis helper
# ---------------------------------------------------------------------------

def _run_ict(app: FastAPI, sym: str, tf: str) -> dict:
    """Run ICT analysis on the current candle snapshot for (sym, tf)."""
    eng: MarketEngine | None = app.state.market.get(sym.upper())
    if eng is None:
        return ict_engine._empty()
    candles = eng.snapshot(tf)
    return ict_engine.analyze(candles, tf)


def _run_signals(app: FastAPI, sym: str, tf: str) -> list[dict]:
    ict_data    = _run_ict(app, sym, tf)
    htf_tf      = "1h" if tf in ("1m", "5m", "15m") else "4h"
    htf_data    = _run_ict(app, sym, htf_tf) if htf_tf != tf else None
    fnd         = app.state.funding.get(sym.upper())
    funding_snap = fnd.snapshot_current() if fnd else None
    return signal_engine.generate_signals(ict_data, funding_snap, htf_data)


# ---------------------------------------------------------------------------
# Lifespan
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    store = Store()
    app.state.store   = store
    app.state.market:   dict[str, MarketEngine]  = {}
    app.state.funding:  dict[str, FundingEngine] = {}
    app.state.tasks:    dict = {}
    app.state.spawn_locks: dict[str, asyncio.Lock] = {}

    registry = BitunixRegistry()
    await registry.init()
    app.state.registry = registry

    print(f"[boot] pre-warming {WARM_SYMBOLS}")
    for sym in WARM_SYMBOLS:
        await _spawn_symbol(app, sym, store)

    # Mark price provider for paper trading
    def _mark(symbol: str) -> float:
        eng = app.state.market.get(symbol.upper())
        return eng.last_price() if eng else 0.0

    paper = PaperEngine(store, _mark)
    app.state.paper = paper
    app.state.tasks[("paper",)] = asyncio.create_task(paper.run())

    app.state.ai = AIAnalyzer(
        OPENCODE_API_KEY, OPENCODE_BASE_URL, OPENCODE_MODEL, OPENCODE_PROXY
    )
    if OPENCODE_API_KEY:
        print(f"[boot] AI analyzer enabled (model={OPENCODE_MODEL})")
    else:
        print("[boot] AI disabled (OPENCODE_API_KEY not set)")

    print(f"[boot] {len(app.state.tasks)} tasks running")

    try:
        yield
    finally:
        for t in app.state.tasks.values():
            t.cancel()
        await asyncio.gather(*app.state.tasks.values(), return_exceptions=True)


app = FastAPI(lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["X-Auth-Token", "Content-Type"],
)


# ---------------------------------------------------------------------------
# WS keepalive pump
# ---------------------------------------------------------------------------

async def _ws_pump(ws: WebSocket, queue: asyncio.Queue):
    while True:
        try:
            msg = await asyncio.wait_for(queue.get(), timeout=WS_KEEPALIVE)
        except asyncio.TimeoutError:
            await ws.send_text(_WS_PING)
            continue
        await ws.send_text(json.dumps(msg))


# ---------------------------------------------------------------------------
# REST — auth / symbols
# ---------------------------------------------------------------------------

@app.get("/api/auth/check")
async def auth_check(_: None = Depends(require_auth)):
    return {"ok": True}


@app.get("/api/symbols", dependencies=[Depends(require_auth)])
async def symbols():
    return {
        "symbols":    WARM_SYMBOLS,
        "default":    DEFAULT_SYMBOL,
        "timeframes": TIMEFRAMES,
    }


@app.get("/api/symbols/search", dependencies=[Depends(require_auth)])
async def symbols_search(q: str = "", limit: int = 12):
    if not q.strip():
        return {"results": []}
    registry: BitunixRegistry = app.state.registry
    results = await registry.search(q, limit=limit)
    return {"results": results}


@app.get("/api/symbols/meta", dependencies=[Depends(require_auth)])
async def symbols_meta(symbol: str):
    sym = symbol.upper()
    registry: BitunixRegistry = app.state.registry
    if not registry.has_perp(sym):
        raise HTTPException(status_code=404, detail=f"{sym} not on Bitunix")
    meta = await registry.get_meta(sym)
    return meta


# ---------------------------------------------------------------------------
# REST — ICT snapshot
# ---------------------------------------------------------------------------

@app.get("/api/ict", dependencies=[Depends(require_auth)])
async def ict_snapshot(symbol: str = DEFAULT_SYMBOL, tf: str = "5m"):
    sym = symbol.upper()
    if not await ensure_engines(sym):
        raise HTTPException(status_code=404, detail=f"no engine for {sym}")
    if tf not in TIMEFRAME_MS:
        raise HTTPException(status_code=400, detail=f"invalid timeframe {tf}")
    return _run_ict(app, sym, tf)


@app.get("/api/signals", dependencies=[Depends(require_auth)])
async def signals_snapshot(symbol: str = DEFAULT_SYMBOL, tf: str = "5m"):
    sym = symbol.upper()
    if not await ensure_engines(sym):
        raise HTTPException(status_code=404, detail=f"no engine for {sym}")
    return {"signals": _run_signals(app, sym, tf)}


@app.get("/api/funding", dependencies=[Depends(require_auth)])
async def funding_status(symbol: str = DEFAULT_SYMBOL):
    sym = symbol.upper()
    fnd = app.state.funding.get(sym)
    if fnd is None:
        raise HTTPException(status_code=404, detail=f"no funding for {sym}")
    return fnd.snapshot_history()


# ---------------------------------------------------------------------------
# Paper trading REST
# ---------------------------------------------------------------------------

def _paper_uid_or_400(uid: str | None) -> str:
    if not valid_uid(uid):
        raise HTTPException(status_code=400, detail="invalid uid")
    return uid  # type: ignore[return-value]


@app.get("/api/paper/account", dependencies=[Depends(require_auth)])
async def paper_account(uid: str = Query(...)):
    return app.state.paper.account_snapshot(_paper_uid_or_400(uid))


@app.get("/api/paper/trades", dependencies=[Depends(require_auth)])
async def paper_trades(uid: str = Query(...), limit: int = 100):
    return {"trades": app.state.paper.history(_paper_uid_or_400(uid), limit)}


@app.post("/api/paper/open", dependencies=[Depends(require_auth)])
async def paper_open(payload: dict):
    u   = _paper_uid_or_400(payload.get("uid"))
    sym = (payload.get("symbol") or DEFAULT_SYMBOL).upper()
    if not app.state.registry.has_perp(sym):
        raise HTTPException(status_code=400, detail=f"unsupported symbol {sym}")
    await ensure_engines(sym)
    try:
        trade = app.state.paper.open_trade(
            u, symbol=sym,
            side=payload.get("side", ""),
            size_usd=payload.get("size_usd", 0),
            leverage=payload.get("leverage", 1),
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"trade": trade, "account": app.state.paper.account_snapshot(u)}


@app.post("/api/paper/close", dependencies=[Depends(require_auth)])
async def paper_close(payload: dict):
    u = _paper_uid_or_400(payload.get("uid"))
    try:
        tid = int(payload.get("trade_id"))
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="invalid trade_id")
    try:
        trade = app.state.paper.close_trade(u, tid)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"trade": trade, "account": app.state.paper.account_snapshot(u)}


@app.post("/api/paper/reset", dependencies=[Depends(require_auth)])
async def paper_reset(payload: dict):
    u = _paper_uid_or_400(payload.get("uid"))
    app.state.paper.reset_account(u)
    return {"account": app.state.paper.account_snapshot(u)}


# ---------------------------------------------------------------------------
# WebSocket — main candle + ICT feed
# ---------------------------------------------------------------------------

@app.websocket("/ws/{symbol}/{timeframe}")
async def candle_feed(
    ws:        WebSocket,
    symbol:    str,
    timeframe: str,
    token:     str | None = Query(default=None),
):
    if not _check_token(token):
        await ws.close(code=4401); return
    if timeframe not in TIMEFRAME_MS:
        await ws.close(code=4400); return

    sym = symbol.upper()
    await ws.accept()

    if not await ensure_engines(sym):
        await ws.close(code=4404); return

    eng: MarketEngine = app.state.market[sym]
    queue: asyncio.Queue = asyncio.Queue(maxsize=300)

    ict_lock = asyncio.Lock()   # serialise ICT re-runs

    async def on_market_event(event: str, payload: dict):
        try:
            queue.put_nowait({"event": event, "data": payload})
        except asyncio.QueueFull:
            pass
        # After a confirmed candle close, re-run ICT + signals and broadcast
        if event == "candle":
            async with ict_lock:
                ict_data = _run_ict(app, sym, timeframe)
                sigs     = signal_engine.generate_signals(
                    ict_data,
                    app.state.funding.get(sym, {}) and
                    app.state.funding[sym].snapshot_current()
                    if sym in app.state.funding else None,
                )
                try:
                    queue.put_nowait({"event": "ict",     "data": ict_data})
                    queue.put_nowait({"event": "signals",  "data": sigs})
                except asyncio.QueueFull:
                    pass

    eng.on_update(timeframe, on_market_event)

    try:
        # Full snapshot on connect
        snap     = eng.snapshot(timeframe)
        ict_data = ict_engine.analyze(snap, timeframe)
        fnd_snap = (app.state.funding[sym].snapshot_current()
                    if sym in app.state.funding else None)
        sigs     = signal_engine.generate_signals(ict_data, fnd_snap)

        await ws.send_text(json.dumps({
            "event": "snapshot",
            "data":  {
                "candles":   snap,
                "ict":       ict_data,
                "signals":   sigs,
                "timeframes": TIMEFRAMES,
            },
        }))

        # Pump loop with keepalive
        while True:
            try:
                msg = await asyncio.wait_for(queue.get(), timeout=WS_KEEPALIVE)
            except asyncio.TimeoutError:
                await ws.send_text(_WS_PING)
                continue
            await ws.send_text(json.dumps(msg))

    except WebSocketDisconnect:
        pass
    finally:
        eng.off_update(timeframe, on_market_event)


# ---------------------------------------------------------------------------
# WebSocket — funding feed
# ---------------------------------------------------------------------------

@app.websocket("/ws/funding")
async def funding_feed(
    ws:     WebSocket,
    token:  str | None = Query(default=None),
    symbol: str        = Query(default=DEFAULT_SYMBOL),
):
    if not _check_token(token):
        await ws.close(code=4401); return
    sym = symbol.upper()
    await ws.accept()
    if not await ensure_engines(sym):
        await ws.close(code=4404); return
    fnd = app.state.funding.get(sym)
    if fnd is None:
        await ws.close(code=4404); return

    queue: asyncio.Queue = asyncio.Queue(maxsize=100)

    async def listener(event: str, payload: dict):
        try:
            queue.put_nowait({"event": event, "data": payload})
        except asyncio.QueueFull:
            pass

    fnd.on_update(listener)
    try:
        await ws.send_text(json.dumps(
            {"event": "snapshot", "data": fnd.snapshot_history()}
        ))
        await _ws_pump(ws, queue)
    except WebSocketDisconnect:
        pass
    finally:
        fnd.off_update(listener)


# ---------------------------------------------------------------------------
# WebSocket — paper trading feed
# ---------------------------------------------------------------------------

@app.websocket("/ws/paper")
async def paper_feed(
    ws:    WebSocket,
    token: str | None = Query(default=None),
    uid:   str | None = Query(default=None),
):
    if not _check_token(token):
        await ws.close(code=4401); return
    if not valid_uid(uid):
        await ws.close(code=4400); return
    eng: PaperEngine = app.state.paper
    await ws.accept()
    queue: asyncio.Queue = asyncio.Queue(maxsize=100)

    async def listener(event: str, payload: dict):
        try:
            queue.put_nowait({"event": event, "data": payload})
        except asyncio.QueueFull:
            pass

    eng.on_update(uid, listener)       # type: ignore
    try:
        await ws.send_text(json.dumps(
            {"event": "snapshot", "data": eng.account_snapshot(uid)}  # type: ignore
        ))
        await _ws_pump(ws, queue)
    except WebSocketDisconnect:
        pass
    finally:
        eng.off_update(uid, listener)  # type: ignore


# ---------------------------------------------------------------------------
# WebSocket — AI analysis
# ---------------------------------------------------------------------------

@app.websocket("/ws/ai-analysis")
async def ai_analysis_feed(
    ws:     WebSocket,
    token:  str | None = Query(default=None),
    symbol: str        = Query(default=DEFAULT_SYMBOL),
):
    if not _check_token(token):
        await ws.close(code=4401); return
    sym = symbol.upper()
    if not app.state.registry.has_perp(sym):
        await ws.close(code=4400); return
    ai: AIAnalyzer = app.state.ai
    await ws.accept()
    await ensure_engines(sym)
    try:
        first = await ws.receive_text()
        try:
            msg = json.loads(first)
        except json.JSONDecodeError:
            msg = {}
        if msg.get("action") != "start":
            await ws.send_text(json.dumps(
                {"event": "error", "message": "expected {action: 'start'}"}
            ))
            await ws.close(); return
        if not ai.enabled:
            await ws.send_text(json.dumps({
                "event":   "error",
                "message": "AI not configured — set OPENCODE_API_KEY in backend/.env",
            }))
            await ws.close(); return

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


# ---------------------------------------------------------------------------
# Static
# ---------------------------------------------------------------------------

@app.get("/")
async def index():
    return FileResponse(FRONTEND_DIR / "index.html")

app.mount("/static", StaticFiles(directory=FRONTEND_DIR), name="static")
