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

SYMBOLS = ["BTCUSDT"]
TIMEFRAMES = list(TIMEFRAME_MS.keys())


@asynccontextmanager
async def lifespan(app: FastAPI):
    store = Store()
    app.state.store = store
    app.state.engines: dict[tuple[str, str], CVDEngine] = {}
    app.state.tasks: dict[tuple[str, str], asyncio.Task] = {}

    print(f"[boot] starting {len(SYMBOLS) * len(TIMEFRAMES)} CVD engines (24/7)")
    for sym in SYMBOLS:
        for tf in TIMEFRAMES:
            eng = CVDEngine(sym, tf, store)
            await eng.seed_history()
            key = (sym.lower(), tf)
            app.state.engines[key] = eng
            app.state.tasks[key] = asyncio.create_task(eng.run())

    # Funding/OI engine (BTC only for now)
    print("[boot] starting funding/OI engine (BTCUSDT)")
    fund_eng = FundingOIEngine("BTCUSDT", store)
    await fund_eng.seed_history()
    app.state.funding = fund_eng
    app.state.funding_task = asyncio.create_task(fund_eng.run())

    # Basis engine (Spot vs Perp premium, BTC only)
    print("[boot] starting basis engine (BTCUSDT)")
    basis_eng = BasisEngine("BTCUSDT", store)
    await basis_eng.seed_history()
    app.state.basis = basis_eng
    app.state.basis_task = asyncio.create_task(basis_eng.run())

    # Live liquidation feed (Binance Futures forceOrder WS, BTC only)
    print("[boot] starting live liquidation feed (BTCUSDT)")
    live_liq = LiveLiquidationEngine("BTCUSDT")
    app.state.live_liq = live_liq
    app.state.live_liq_task = asyncio.create_task(live_liq.run())

    # Taker buy/sell ratio engine (multi-TF, BTC only)
    print("[boot] starting taker ratio engine (BTCUSDT, 5m/15m/1h)")
    taker_eng = TakerEngine("BTCUSDT", store)
    await taker_eng.seed_history()
    app.state.taker = taker_eng
    app.state.taker_task = asyncio.create_task(taker_eng.run())

    # Options Gamma Exposure engine (Deribit BTC options, 5min poll)
    print("[boot] starting GEX engine (Deribit BTC options)")
    gex_eng = GexEngine("BTCUSDT", store)
    await gex_eng.seed_history()
    app.state.gex = gex_eng
    app.state.gex_task = asyncio.create_task(gex_eng.run())

    # Liquidation heatmap engine (BTC only)
    app.state.liquidation = None
    app.state.liquidation_task = None
    if COINALYZE_API_KEY:
        print("[boot] starting liquidation engine (BTCUSDT)")
        liq_eng = LiquidationEngine("BTCUSDT", "BTCUSDT_PERP.A", COINALYZE_API_KEY, store)
        await liq_eng.seed_history()
        app.state.liquidation = liq_eng
        app.state.liquidation_task = asyncio.create_task(liq_eng.run())
    else:
        print("[boot] COINALYZE_API_KEY not set — skipping liquidation engine")

    print("[boot] all engines running")

    try:
        yield
    finally:
        extra_tasks = []
        for attr in ("funding_task", "basis_task", "liquidation_task", "live_liq_task", "taker_task", "gex_task"):
            t = getattr(app.state, attr, None)
            if t is not None:
                t.cancel()
                extra_tasks.append(t)
        for t in app.state.tasks.values():
            t.cancel()
        await asyncio.gather(
            *app.state.tasks.values(),
            *extra_tasks,
            return_exceptions=True,
        )
        for eng in app.state.engines.values():
            if eng.candles and eng.candles[-1].observed:
                store.upsert(eng.symbol.upper(), eng.timeframe, eng.candles[-1].to_dict())


app = FastAPI(lifespan=lifespan)

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
    return {"symbols": SYMBOLS, "timeframes": TIMEFRAMES}


@app.get("/api/status", dependencies=[Depends(require_auth)])
async def status():
    out = []
    for (sym, tf), eng in app.state.engines.items():
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
async def funding_status():
    eng: FundingOIEngine = app.state.funding
    return eng.snapshot_history()


@app.get("/api/basis", dependencies=[Depends(require_auth)])
async def basis_status():
    eng: BasisEngine = app.state.basis
    return eng.snapshot_history()


@app.websocket("/ws/basis")
async def basis_feed(ws: WebSocket, token: str | None = Query(default=None)):
    if not _check_token(token):
        await ws.close(code=4401)
        return
    eng: BasisEngine = app.state.basis
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
        while True:
            msg = await queue.get()
            await ws.send_text(json.dumps(msg))
    except WebSocketDisconnect:
        pass
    finally:
        eng.off_update(listener)


@app.get("/api/gex", dependencies=[Depends(require_auth)])
async def gex_status():
    eng: GexEngine = app.state.gex
    snap = eng.snapshot()
    return {"snapshot": snap}


@app.websocket("/ws/gex")
async def gex_feed(ws: WebSocket, token: str | None = Query(default=None)):
    if not _check_token(token):
        await ws.close(code=4401)
        return
    eng: GexEngine = app.state.gex
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
        while True:
            msg = await queue.get()
            await ws.send_text(json.dumps(msg))
    except WebSocketDisconnect:
        pass
    finally:
        eng.off_update(listener)


@app.get("/api/taker", dependencies=[Depends(require_auth)])
async def taker_status():
    eng: TakerEngine = app.state.taker
    return eng.snapshot_history()


@app.websocket("/ws/taker")
async def taker_feed(ws: WebSocket, token: str | None = Query(default=None)):
    if not _check_token(token):
        await ws.close(code=4401)
        return
    eng: TakerEngine = app.state.taker
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
        while True:
            msg = await queue.get()
            await ws.send_text(json.dumps(msg))
    except WebSocketDisconnect:
        pass
    finally:
        eng.off_update(listener)


@app.websocket("/ws/live-liq")
async def live_liq_feed(ws: WebSocket, token: str | None = Query(default=None)):
    if not _check_token(token):
        await ws.close(code=4401)
        return
    eng: LiveLiquidationEngine = app.state.live_liq
    await ws.accept()
    queue: asyncio.Queue = asyncio.Queue(maxsize=200)

    async def listener(event: str, payload: dict):
        try:
            queue.put_nowait({"event": event, "data": payload})
        except asyncio.QueueFull:
            pass

    eng.on_update(listener)
    try:
        while True:
            msg = await queue.get()
            await ws.send_text(json.dumps(msg))
    except WebSocketDisconnect:
        pass
    finally:
        eng.off_update(listener)


@app.get("/api/liquidations", dependencies=[Depends(require_auth)])
async def liquidations_status():
    eng: LiquidationEngine | None = app.state.liquidation
    if eng is None:
        return {"enabled": False, "snapshot": None}
    return {"enabled": True, "snapshot": eng.snapshot()}


@app.websocket("/ws/liquidations")
async def liquidations_feed(ws: WebSocket, token: str | None = Query(default=None)):
    if not _check_token(token):
        await ws.close(code=4401)
        return
    eng: LiquidationEngine | None = app.state.liquidation
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
        while True:
            msg = await queue.get()
            await ws.send_text(json.dumps(msg))
    except WebSocketDisconnect:
        pass
    finally:
        eng.off_update(listener)


@app.websocket("/ws/funding")
async def funding_feed(ws: WebSocket, token: str | None = Query(default=None)):
    if not _check_token(token):
        await ws.close(code=4401)
        return
    await ws.accept()
    eng: FundingOIEngine = app.state.funding
    queue: asyncio.Queue = asyncio.Queue(maxsize=200)

    async def listener(event: str, payload: dict):
        try:
            queue.put_nowait({"event": event, "data": payload})
        except asyncio.QueueFull:
            pass

    eng.on_update(listener)
    try:
        await ws.send_text(json.dumps({"event": "snapshot", "data": eng.snapshot_history()}))
        while True:
            msg = await queue.get()
            await ws.send_text(json.dumps(msg))
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

        while True:
            msg = await queue.get()
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
