"""
Bitunix funding rate + open interest engine.

Polls the Bitunix REST API every 30 seconds:
  GET /api/v1/futures/market/funding_rate  → rate, mark price, next settle time
  GET /api/v1/futures/market/tickers       → volume, last price (proxy for OI)

Emits tick events to connected WebSocket listeners.
"""
import asyncio
import time
from collections import deque
from typing import Callable

import httpx

from storage import Store

BITUNIX_REST  = "https://fapi.bitunix.com"
POLL_INTERVAL = 30   # seconds


class FundingEngine:
    def __init__(self, symbol: str, store: Store):
        self.symbol           = symbol.upper()
        self.store            = store
        self.funding_rate:    float = 0.0
        self.next_funding_time: int = 0
        self.funding_interval: int  = 8     # hours
        self.mark_price:      float = 0.0
        self.last_price:      float = 0.0
        self.volume_24h:      float = 0.0
        self._listeners:      list[Callable] = []
        self._running:        bool = False
        self._rate_history:   deque[tuple[int, float]] = deque(maxlen=200)

    # ------------------------------------------------------------------
    # Listener management
    # ------------------------------------------------------------------

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
                print(f"[funding emit] {e}")

    # ------------------------------------------------------------------
    # Boot seed
    # ------------------------------------------------------------------

    async def seed_history(self):
        """Pre-load stored funding history from SQLite."""
        rows = self.store.load_funding(self.symbol, limit=500)
        for r in rows:
            self._rate_history.append((r["ts"], r["rate"]))
        # Prime with a live poll so snapshot is immediately available
        await self._poll()

    # ------------------------------------------------------------------
    # Main loop
    # ------------------------------------------------------------------

    async def run(self):
        self._running = True
        backoff = 1
        while self._running:
            try:
                await self._poll()
                backoff = 1
                await asyncio.sleep(POLL_INTERVAL)
            except Exception as e:
                print(f"[funding] {self.symbol} poll error: {e}. Retry in {backoff}s")
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60)

    async def _poll(self):
        async with httpx.AsyncClient(timeout=10) as c:
            r1, r2 = await asyncio.gather(
                c.get(f"{BITUNIX_REST}/api/v1/futures/market/funding_rate",
                      params={"symbol": self.symbol}),
                c.get(f"{BITUNIX_REST}/api/v1/futures/market/tickers",
                      params={"symbols": self.symbol}),
                return_exceptions=True,
            )

        # Funding rate
        if not isinstance(r1, Exception):
            try:
                r1.raise_for_status()
                data = r1.json().get("data", [])
                if data:
                    d = data[0]
                    self.funding_rate      = float(d.get("fundingRate",    0) or 0)
                    self.mark_price        = float(d.get("markPrice",      0) or 0)
                    self.next_funding_time = int(d.get("nextFundingTime",  0) or 0)
                    self.funding_interval  = int(d.get("fundingInterval",  8) or 8)
                    ts_now = int(time.time() * 1000)
                    self._rate_history.append((ts_now, self.funding_rate))
                    self.store.upsert_funding(self.symbol, ts_now, self.funding_rate)
            except Exception as e:
                print(f"[funding] rate parse {self.symbol}: {e}")

        # Ticker (volume + last price)
        if not isinstance(r2, Exception):
            try:
                r2.raise_for_status()
                data = r2.json().get("data", [])
                if data:
                    d = data[0]
                    self.last_price  = float(d.get("lastPrice", 0) or 0)
                    self.volume_24h  = float(d.get("quoteVol",  0) or 0)
            except Exception as e:
                print(f"[funding] ticker parse {self.symbol}: {e}")

        await self._emit("tick", self.snapshot_current())

    # ------------------------------------------------------------------
    # Snapshot
    # ------------------------------------------------------------------

    def snapshot_current(self) -> dict:
        return {
            "symbol":            self.symbol,
            "funding_rate":      self.funding_rate,
            "next_funding_time": self.next_funding_time,
            "funding_interval":  self.funding_interval,
            "mark_price":        self.mark_price,
            "last_price":        self.last_price,
            "volume_24h":        self.volume_24h,
        }

    def snapshot_history(self) -> dict:
        funding = self.store.load_funding(self.symbol, 200)
        return {
            "funding":  funding,
            "current":  self.snapshot_current(),
        }
