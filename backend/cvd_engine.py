"""
CVD engine: connects to Binance Spot aggTrade websocket and aggregates
true buy/sell volume per candle.

aggTrade.m field:
  m=true  -> buyer is maker, taker hit the bid -> SELL aggression
  m=false -> seller is maker, taker hit the ask -> BUY aggression

This is true tick-level aggressor classification straight from the exchange.
No approximation. State is persisted to SQLite so 24/7 monitoring resumes
exactly where it left off after a restart.
"""
import asyncio
import json
import time
from collections import deque
from typing import Callable

import httpx
import websockets

from storage import Store

BINANCE_WS = "wss://fstream.binance.com/ws"
BINANCE_REST = "https://fapi.binance.com"

TIMEFRAME_MS = {
    "1m": 60_000,
    "5m": 300_000,
    "15m": 900_000,
    "1h": 3_600_000,
}

LIVE_PERSIST_INTERVAL_S = 2.0  # how often to flush the in-progress candle to disk


class Candle:
    __slots__ = ("ts", "open", "high", "low", "close", "volume",
                 "buy_vol", "sell_vol", "delta", "cvd", "observed")

    def __init__(self, ts: int, price: float, cvd: float, observed: bool = False):
        self.ts = ts
        self.open = price
        self.high = price
        self.low = price
        self.close = price
        self.volume = 0.0
        self.buy_vol = 0.0
        self.sell_vol = 0.0
        self.delta = 0.0
        self.cvd = cvd
        self.observed = observed

    def update(self, price: float, qty: float, is_sell: bool):
        self.close = price
        if price > self.high:
            self.high = price
        if price < self.low:
            self.low = price
        self.volume += qty
        if is_sell:
            self.sell_vol += qty
            self.delta -= qty
        else:
            self.buy_vol += qty
            self.delta += qty
        self.observed = True

    def to_dict(self):
        return {
            "ts": self.ts,
            "open": self.open,
            "high": self.high,
            "low": self.low,
            "close": self.close,
            "volume": self.volume,
            "buy_vol": self.buy_vol,
            "sell_vol": self.sell_vol,
            "delta": self.delta,
            "cvd": self.cvd,
            "observed": self.observed,
        }


class CVDEngine:
    def __init__(self, symbol: str, timeframe: str, store: Store, max_candles: int = 1000):
        self.symbol = symbol.lower()
        self.timeframe = timeframe
        self.tf_ms = TIMEFRAME_MS[timeframe]
        self.max_candles = max_candles
        self.candles: deque[Candle] = deque(maxlen=max_candles)
        self.cvd: float = 0.0
        self.store = store
        self._listeners: list[Callable] = []
        self._running = False
        self._last_live_persist = 0.0

    def on_update(self, fn: Callable):
        self._listeners.append(fn)

    def off_update(self, fn: Callable):
        if fn in self._listeners:
            self._listeners.remove(fn)

    async def _emit(self, event: str, payload: dict):
        for fn in list(self._listeners):
            try:
                await fn(event, payload)
            except Exception as e:
                print(f"[listener error] {e}")

    def _bucket(self, ts_ms: int) -> int:
        return (ts_ms // self.tf_ms) * self.tf_ms

    async def _fetch_klines(self, start_time: int | None = None,
                            end_time: int | None = None, limit: int = 1000) -> list:
        url = f"{BINANCE_REST}/fapi/v1/klines"
        params = {"symbol": self.symbol.upper(), "interval": self.timeframe,
                  "limit": min(max(limit, 1), 1000)}
        if start_time is not None:
            params["startTime"] = start_time
        if end_time is not None:
            params["endTime"] = end_time
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                r = await client.get(url, params=params)
                r.raise_for_status()
                return r.json()
        except Exception as e:
            print(f"[{self.symbol} {self.timeframe}] kline fetch failed: {e}")
            return []

    @staticmethod
    def _kline_to_candle(k) -> "Candle":
        ts = int(k[0])
        c = Candle(ts, float(k[1]), 0.0, observed=False)
        c.high = float(k[2]); c.low = float(k[3]); c.close = float(k[4])
        c.volume = float(k[5])
        return c

    async def _backfill_gap(self, start_time: int, end_time: int):
        """Fill missing buckets in (start_time..end_time) with real klines and
        emit them so the frontend extends the chart in real time."""
        if start_time > end_time:
            return
        existing = {c.ts for c in self.candles}
        klines = await self._fetch_klines(start_time=start_time, end_time=end_time)
        for k in klines:
            ts = int(k[0])
            if ts in existing:
                continue
            c = self._kline_to_candle(k)
            self.candles.append(c)
            await self._emit("candle", c.to_dict())

    async def seed_history(self):
        """Restore observed candles from SQLite, then fetch real klines for any
        gaps (after-last-observed and before-oldest-observed). Klines are *not*
        persisted — they're display-only price data with no real CVD/delta.
        We never synthesize flat filler candles."""
        now_ms = int(time.time() * 1000)
        current_bucket = self._bucket(now_ms)

        observed = self.store.load_recent(self.symbol.upper(), self.timeframe, self.max_candles)
        if observed:
            self.cvd = observed[-1]["cvd"]
            prev_ts: int | None = None
            for o in observed:
                # If there's an internal gap between observed bars (server was
                # off then on), pad it with real klines so the price chart is
                # contiguous.
                if prev_ts is not None and o["ts"] - prev_ts > self.tf_ms:
                    klines = await self._fetch_klines(
                        start_time=prev_ts + self.tf_ms,
                        end_time=o["ts"] - 1,
                    )
                    seen = set()
                    for k in klines:
                        ts = int(k[0])
                        if ts <= prev_ts or ts >= o["ts"] or ts in seen:
                            continue
                        seen.add(ts)
                        self.candles.append(self._kline_to_candle(k))
                c = Candle(o["ts"], o["open"], o["cvd"], observed=True)
                c.high = o["high"]; c.low = o["low"]; c.close = o["close"]
                c.volume = o["volume"]; c.buy_vol = o["buy_vol"]; c.sell_vol = o["sell_vol"]
                c.delta = o["delta"]; c.cvd = o["cvd"]
                self.candles.append(c)
                prev_ts = o["ts"]

        # Forward gap: from (last observed + tf) up to (current bucket - 1)
        if self.candles and self.candles[-1].ts < current_bucket - self.tf_ms:
            gap = await self._fetch_klines(
                start_time=self.candles[-1].ts + self.tf_ms,
                end_time=current_bucket - 1,
            )
            existing = {c.ts for c in self.candles}
            for k in gap:
                ts = int(k[0])
                if ts in existing or ts >= current_bucket:
                    continue
                self.candles.append(self._kline_to_candle(k))

        # Backward pad: older klines until we reach max_candles
        need = self.max_candles - len(self.candles)
        if need > 0:
            end_time = (self.candles[0].ts - 1) if self.candles else (current_bucket - 1)
            old = await self._fetch_klines(end_time=end_time, limit=need)
            existing = {c.ts for c in self.candles}
            pad = []
            for k in old:
                ts = int(k[0])
                if ts in existing or ts >= current_bucket:
                    continue
                pad.append(self._kline_to_candle(k))
            for c in reversed(pad):
                self.candles.appendleft(c)

    async def run(self):
        self._running = True
        stream = f"{self.symbol}@aggTrade"
        url = f"{BINANCE_WS}/{stream}"
        backoff = 1
        while self._running:
            try:
                async with websockets.connect(url, ping_interval=20, ping_timeout=20) as ws:
                    backoff = 1
                    print(f"[cvd] connected: {self.symbol} {self.timeframe} (cvd={self.cvd:.4f})")
                    async for msg in ws:
                        await self._handle_trade(json.loads(msg))
            except Exception as e:
                print(f"[cvd] {self.symbol} {self.timeframe} reconnecting after error: {e}")
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30)

    async def _handle_trade(self, t: dict):
        price = float(t["p"])
        qty = float(t["q"])
        ts = int(t["T"])
        is_sell = bool(t["m"])

        bucket = self._bucket(ts)

        if self.candles and self.candles[-1].ts > bucket:
            # Out-of-order trade for a closed bucket — drop it.
            return

        if not self.candles or self.candles[-1].ts < bucket:
            # Persist previous candle if it was observed.
            if self.candles and self.candles[-1].observed:
                self.store.upsert(self.symbol.upper(), self.timeframe, self.candles[-1].to_dict())

            # Backfill any missing buckets between the last candle and the new
            # live bucket using real klines (no synthetic flat candles).
            if self.candles and self.candles[-1].ts < bucket - self.tf_ms:
                await self._backfill_gap(self.candles[-1].ts + self.tf_ms, bucket - 1)

            self.candles.append(Candle(bucket, price, self.cvd))

        c = self.candles[-1]
        # First real trade on a kline-padded candle: zero out the kline's volume
        # so we count only what we actually observe.
        if not c.observed:
            c.volume = 0.0
            c.buy_vol = 0.0
            c.sell_vol = 0.0
            c.delta = 0.0

        c.update(price, qty, is_sell)
        self.cvd += (-qty if is_sell else qty)
        c.cvd = self.cvd

        await self._emit("tick", c.to_dict())

        # Throttled flush of the live candle so a crash loses at most ~2s.
        now = time.monotonic()
        if now - self._last_live_persist >= LIVE_PERSIST_INTERVAL_S:
            self._last_live_persist = now
            self.store.upsert(self.symbol.upper(), self.timeframe, c.to_dict())

    def snapshot(self) -> list[dict]:
        return [c.to_dict() for c in self.candles]
