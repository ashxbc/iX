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

from cvd_engine import CVDEngine, TIMEFRAME_MS
from divergence import detect_divergences
from funding_engine import FundingOIEngine
from storage import Store


load_dotenv(Path(__file__).resolve().parent / ".env")
ACCESS_TOKEN = os.environ.get("IX_ACCESS_TOKEN", "").strip()
if not ACCESS_TOKEN or len(ACCESS_TOKEN) < 24:
    raise RuntimeError(
        "IX_ACCESS_TOKEN must be set in backend/.env and be at least 24 chars long. "
        "Generate one with: python -c \"import secrets; print(secrets.token_urlsafe(18))\""
    )


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

SYMBOLS = ["BTCUSDT", "ETHUSDT", "SOLUSDT"]
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

    print("[boot] all engines running")

    try:
        yield
    finally:
        for t in app.state.tasks.values():
            t.cancel()
        if hasattr(app.state, "funding_task"):
            app.state.funding_task.cancel()
        await asyncio.gather(
            *app.state.tasks.values(),
            getattr(app.state, "funding_task", asyncio.sleep(0)),
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
