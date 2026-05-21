"""
Bitunix market data engine.

One engine per symbol. Maintains OHLCV candle history for all active
timeframes. Streams from the Bitunix public WebSocket (kline + trade
channels) and seeds historical data from the Bitunix REST API.

Tracks true buy/sell volume per candle from the trade channel —
essential for ICT order block validity and displacement detection.
No CVD. This is pure price + volume truth.

WebSocket endpoint: wss://fapi.bitunix.com/public/
REST endpoint:      https://fapi.bitunix.com/api/v1/futures/market/kline

Kline WS push interval: 500ms (in-progress candle state).
Candle close detection: when the derived bucket timestamp changes.
"""
import asyncio
import json
import time
from collections import deque
from typing import Callable

import httpx
import websockets

from storage import Store

BITUNIX_WS   = "wss://fapi.bitunix.com/public/"
BITUNIX_REST = "https://fapi.bitunix.com"

# Timeframes exposed to the frontend
TIMEFRAME_MS: dict[str, int] = {
    "1m":  60_000,
    "5m":  300_000,
    "15m": 900_000,
    "1h":  3_600_000,
    "4h":  14_400_000,
}

# REST interval param → WS channel suffix
_WS_SUFFIX: dict[str, str] = {
    "1m":  "1min",
    "5m":  "5min",
    "15m": "15min",
    "1h":  "60min",
    "4h":  "4h",
}

LIVE_PERSIST_S = 2.0   # flush live candle to SQLite at most every N seconds
WS_PING_S      = 20.0  # application-level ping interval


class Candle:
    __slots__ = ("ts", "open", "high", "low", "close",
                 "volume", "buy_vol", "sell_vol", "observed")

    def __init__(self, ts: int, open_: float, observed: bool = False):
        self.ts       = ts
        self.open     = open_
        self.high     = open_
        self.low      = open_
        self.close    = open_
        self.volume   = 0.0
        self.buy_vol  = 0.0
        self.sell_vol = 0.0
        self.observed = observed

    def apply_kline(self, o: float, h: float, l: float, c: float, vol: float):
        self.open    = o
        self.high    = h
        self.low     = l
        self.close   = c
        self.volume  = vol
        self.observed = True

    def apply_trade(self, price: float, qty: float, is_buy: bool):
        self.close = price
        if price > self.high:
            self.high = price
        if price < self.low:
            self.low = price
        self.volume += qty
        if is_buy:
            self.buy_vol += qty
        else:
            self.sell_vol += qty
        self.observed = True

    def to_dict(self) -> dict:
        return {
            "ts":       self.ts,
            "open":     self.open,
            "high":     self.high,
            "low":      self.low,
            "close":    self.close,
            "volume":   self.volume,
            "buy_vol":  self.buy_vol,
            "sell_vol": self.sell_vol,
            "observed": self.observed,
        }

    def _store_dict(self) -> dict:
        """Dict compatible with storage.Store.upsert (includes legacy fields)."""
        d = self.to_dict()
        d["delta"] = self.buy_vol - self.sell_vol
        d["cvd"]   = 0.0
        return d


class MarketEngine:
    """
    Per-symbol market data engine. Manages klines for all timeframes and
    streams from the Bitunix WebSocket.
    """

    def __init__(self, symbol: str, store: Store, max_candles: int = 500):
        self.symbol      = symbol.upper()
        self.store       = store
        self.max_candles = max_candles
        self._running    = False
        self._last_persist = 0.0

        self.candles: dict[str, deque[Candle]] = {
            tf: deque(maxlen=max_candles) for tf in TIMEFRAME_MS
        }
        self._bucket_now: dict[str, int] = {tf: 0 for tf in TIMEFRAME_MS}

        # Listeners per timeframe + a "trade" channel for raw tick subscribers
        self._listeners: dict[str, list[Callable]] = {
            tf: [] for tf in TIMEFRAME_MS
        }
        self._listeners["trade"] = []

    # ------------------------------------------------------------------
    # Listener management
    # ------------------------------------------------------------------

    def on_update(self, tf: str, fn: Callable):
        self._listeners.setdefault(tf, []).append(fn)

    def off_update(self, tf: str, fn: Callable):
        lst = self._listeners.get(tf, [])
        if fn in lst:
            lst.remove(fn)

    async def _emit(self, tf: str, event: str, payload: dict):
        for fn in list(self._listeners.get(tf, [])):
            try:
                await fn(event, payload)
            except Exception as e:
                print(f"[market-engine emit] {e}")

    # ------------------------------------------------------------------
    # Bucket helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _bucket(ts_ms: int, tf: str) -> int:
        ms = TIMEFRAME_MS[tf]
        return (ts_ms // ms) * ms

    # ------------------------------------------------------------------
    # Historical seed
    # ------------------------------------------------------------------

    async def seed_history(self):
        """Load SQLite + fill gaps from Bitunix REST for all timeframes."""
        await asyncio.gather(*[self._seed_tf(tf) for tf in TIMEFRAME_MS],
                             return_exceptions=True)

    async def _seed_tf(self, tf: str):
        now_ms   = int(time.time() * 1000)
        cur_bkt  = self._bucket(now_ms, tf)
        tf_ms    = TIMEFRAME_MS[tf]

        # 1. Restore observed candles from SQLite
        observed = self.store.load_recent(self.symbol, tf, self.max_candles)
        if observed:
            for o in observed:
                c = Candle(o["ts"], o["open"], observed=True)
                c.high     = o["high"];  c.low   = o["low"]
                c.close    = o["close"]; c.volume = o["volume"]
                c.buy_vol  = o["buy_vol"]; c.sell_vol = o["sell_vol"]
                self.candles[tf].append(c)

        # 2. Forward gap: from last stored candle to current bucket
        last_ts = self.candles[tf][-1].ts if self.candles[tf] else 0
        if last_ts < cur_bkt - tf_ms:
            gap = await self._fetch_klines(
                tf,
                start_time=last_ts + tf_ms,
                end_time=cur_bkt - 1,
                limit=200,
            )
            existing = {c.ts for c in self.candles[tf]}
            for k in gap:
                t = int(k["time"])
                if t not in existing and t < cur_bkt:
                    nc = Candle(t, float(k["open"]), observed=False)
                    nc.apply_kline(float(k["open"]), float(k["high"]),
                                   float(k["low"]),  float(k["close"]),
                                   float(k.get("baseVol", 0) or 0))
                    self.candles[tf].append(nc)

        # 3. Backward pad: fill up to max_candles
        need = self.max_candles - len(self.candles[tf])
        if need > 0:
            end_ts = (self.candles[tf][0].ts - 1
                      if self.candles[tf] else cur_bkt - 1)
            old = await self._fetch_klines(tf, end_time=end_ts, limit=need)
            existing = {c.ts for c in self.candles[tf]}
            pad: list[Candle] = []
            for k in old:
                t = int(k["time"])
                if t not in existing:
                    nc = Candle(t, float(k["open"]), observed=False)
                    nc.apply_kline(float(k["open"]), float(k["high"]),
                                   float(k["low"]),  float(k["close"]),
                                   float(k.get("baseVol", 0) or 0))
                    pad.append(nc)
            for c in reversed(pad):
                self.candles[tf].appendleft(c)

        # Set current bucket tracker
        if self.candles[tf]:
            self._bucket_now[tf] = self._bucket(self.candles[tf][-1].ts, tf)

    async def _fetch_klines(self, tf: str,
                            start_time: int | None = None,
                            end_time:   int | None = None,
                            limit: int = 200) -> list[dict]:
        url    = f"{BITUNIX_REST}/api/v1/futures/market/kline"
        params = {"symbol": self.symbol, "interval": tf,
                  "limit": min(max(limit, 1), 200)}
        if start_time is not None:
            params["startTime"] = start_time
        if end_time is not None:
            params["endTime"] = end_time
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                r = await client.get(url, params=params)
                r.raise_for_status()
                data = r.json().get("data", [])
                # Bitunix returns newest-first; sort ascending for correct deque ordering
                data.sort(key=lambda x: int(x.get("time", 0)))
                return data
        except Exception as e:
            print(f"[market-engine] kline fetch {self.symbol}/{tf} failed: {e}")
            return []

    # ------------------------------------------------------------------
    # WebSocket loop
    # ------------------------------------------------------------------

    async def run(self):
        self._running = True
        backoff = 1
        while self._running:
            try:
                await self._run_ws()
                backoff = 1
            except Exception as e:
                print(f"[market-engine] {self.symbol} WS error: {e}. "
                      f"Retry in {backoff}s")
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30)

    async def _run_ws(self):
        async with websockets.connect(
            BITUNIX_WS,
            ping_interval=None,   # use application-level ping
            max_size=2**20,
        ) as ws:
            # Subscribe to all kline timeframes
            for tf in TIMEFRAME_MS:
                await ws.send(json.dumps({
                    "op": "subscribe",
                    "args": [{"symbol": self.symbol,
                              "ch": f"market_kline_{_WS_SUFFIX[tf]}"}],
                }))
            # Subscribe to trade channel for buy/sell volume
            await ws.send(json.dumps({
                "op": "subscribe",
                "args": [{"symbol": self.symbol, "ch": "trade"}],
            }))
            print(f"[market-engine] {self.symbol} WS connected")

            ping_task = asyncio.create_task(self._ping_loop(ws))
            try:
                async for raw in ws:
                    if not self._running:
                        break
                    try:
                        msg = json.loads(raw)
                    except Exception:
                        continue
                    if msg.get("op") == "pong":
                        continue
                    await self._dispatch(msg)
            finally:
                ping_task.cancel()

    async def _ping_loop(self, ws):
        while True:
            await asyncio.sleep(WS_PING_S)
            try:
                await ws.send(json.dumps({"op": "ping",
                                          "ping": int(time.time())}))
            except Exception:
                break

    # ------------------------------------------------------------------
    # Message dispatch
    # ------------------------------------------------------------------

    async def _dispatch(self, msg: dict):
        ch = msg.get("ch", "")
        if "kline" in ch:
            await self._on_kline(ch, msg)
        elif ch == "trade":
            await self._on_trades(msg)

    async def _on_kline(self, ch: str, msg: dict):
        # Identify which timeframe from the channel name
        tf = None
        for _tf, suffix in _WS_SUFFIX.items():
            if ch.endswith(f"_{suffix}"):
                tf = _tf
                break
        if tf is None:
            return

        data   = msg.get("data", {})
        ts_ms  = int(msg.get("ts", 0))
        bucket = self._bucket(ts_ms, tf)

        o   = float(data.get("o", 0))
        h   = float(data.get("h", 0))
        l   = float(data.get("l", 0))
        c   = float(data.get("c", 0))
        vol = float(data.get("b", 0))  # base volume

        prev_bucket = self._bucket_now.get(tf, 0)

        if bucket > prev_bucket and prev_bucket > 0:
            # ---- candle CLOSED ----
            if self.candles[tf]:
                closed = self.candles[tf][-1]
                if closed.observed:
                    self.store.upsert(self.symbol, tf, closed._store_dict())
                await self._emit(tf, "candle", closed.to_dict())

            # Start the new candle
            nc = Candle(bucket, o, observed=True)
            nc.apply_kline(o, h, l, c, vol)
            self.candles[tf].append(nc)
            self._bucket_now[tf] = bucket

        elif bucket >= prev_bucket:
            # ---- in-progress candle update ----
            if self.candles[tf] and self.candles[tf][-1].ts == bucket:
                curr = self.candles[tf][-1]
                curr.open = o
                if h > curr.high:
                    curr.high = h
                if l < curr.low:
                    curr.low = l
                curr.close   = c
                curr.volume  = max(curr.volume, vol)
                curr.observed = True
            else:
                nc = Candle(bucket, o, observed=True)
                nc.apply_kline(o, h, l, c, vol)
                self.candles[tf].append(nc)
                self._bucket_now[tf] = bucket

            if self.candles[tf]:
                await self._emit(tf, "tick", self.candles[tf][-1].to_dict())

                # Throttled SQLite flush of live candle
                now = time.monotonic()
                if now - self._last_persist >= LIVE_PERSIST_S:
                    self._last_persist = now
                    live = self.candles[tf][-1]
                    if live.observed:
                        self.store.upsert(self.symbol, tf, live._store_dict())

    async def _on_trades(self, msg: dict):
        trades = msg.get("data", [])
        ts_ms  = int(msg.get("ts", 0))
        for t in trades:
            price  = float(t.get("p", 0))
            qty    = float(t.get("v", 0))
            is_buy = t.get("s", "sell") == "buy"
            # Accumulate into buy_vol / sell_vol for the active candle
            for tf in TIMEFRAME_MS:
                bucket = self._bucket(ts_ms, tf)
                if self.candles[tf] and self.candles[tf][-1].ts == bucket:
                    if is_buy:
                        self.candles[tf][-1].buy_vol += qty
                    else:
                        self.candles[tf][-1].sell_vol += qty

    # ------------------------------------------------------------------
    # Snapshot helpers
    # ------------------------------------------------------------------

    def snapshot(self, tf: str) -> list[dict]:
        return [c.to_dict() for c in self.candles.get(tf, [])]

    def last_price(self) -> float:
        for tf in ("1m", "5m", "15m", "1h"):
            cs = self.candles.get(tf)
            if cs:
                return cs[-1].close
        return 0.0
