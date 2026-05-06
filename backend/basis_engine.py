"""
Spot vs Perp Basis engine.

Polls Binance Spot + USD-M Futures `bookTicker` endpoints, computes the
mid-price on each side, and tracks the spread:

    basis     = spot_mid - perp_mid          (USD)
    basis_pct = basis / spot_mid * 100       (%)

A POSITIVE basis (spot > perp) means real spot demand is leading the move —
buyers want the asset itself, not just leverage. Sustainable.
A NEGATIVE basis (perp > spot) means leverage is driving price — fragile,
prone to long-squeeze cascades.

We poll every 2s for a live readout and persist a 1-minute sampled history
to SQLite for the chart overlay. An EMA smoothes microstructure noise so
the directional state ("spot_led" / "perp_led" / "neutral") is stable.
"""
import asyncio
import time
from collections import deque
from typing import Callable

import httpx

from storage import Store


SPOT_API = "https://api.binance.com/api/v3/ticker/bookTicker"
FAPI = "https://fapi.binance.com/fapi/v1/ticker/bookTicker"

POLL_INTERVAL_SEC = 2          # live readout cadence
PERSIST_INTERVAL_SEC = 60      # sample-to-DB cadence
LOOKBACK_HOURS = 24            # how much history to keep in memory / send to client
EMA_ALPHA = 0.15               # EMA on basis_pct for state classification

# State thresholds — % of spot price (basis_pct units)
# 0.02% on a $80k market = ~$16. Real signal sits well above this on average.
SPOT_LED_THRESHOLD = 0.02
PERP_LED_THRESHOLD = -0.02


class BasisEngine:
    def __init__(self, symbol: str, store: Store):
        self.symbol = symbol.upper()
        self.store = store
        self.spot: float = 0.0
        self.perp: float = 0.0
        self.basis: float = 0.0
        self.basis_pct: float = 0.0
        self.basis_pct_ema: float | None = None
        self.state: str = "neutral"   # "spot_led" / "perp_led" / "neutral"
        self.last_update_ms: int = 0
        self._last_persist_ts: int = 0
        self._listeners: list[Callable] = []
        self._running = False
        self._history: deque[dict] = deque(maxlen=LOOKBACK_HOURS * 60 + 60)

    # ---------- listener plumbing ----------

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
                print(f"[basis listener error] {e}")

    # ---------- lifecycle ----------

    async def seed_history(self):
        """Load the last 24h of 1-min basis samples from DB into memory."""
        cutoff = int(time.time()) - LOOKBACK_HOURS * 3600
        rows = self.store.load_basis(self.symbol, since_ts=cutoff, limit=2000)
        for row in rows:
            self._history.append(row)
            self.basis_pct_ema = (
                row["basis_pct"] if self.basis_pct_ema is None
                else EMA_ALPHA * row["basis_pct"] + (1 - EMA_ALPHA) * self.basis_pct_ema
            )
        if rows:
            print(f"[basis] seeded {len(rows)} samples (last basis: {rows[-1]['basis']:+.2f})")

    async def run(self):
        self._running = True
        backoff = 1
        while self._running:
            try:
                await self._poll()
                backoff = 1
                await asyncio.sleep(POLL_INTERVAL_SEC)
            except Exception as e:
                print(f"[basis] poll error, retry in {backoff}s: {e}")
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30)

    async def _poll(self):
        async with httpx.AsyncClient(timeout=5) as c:
            spot_r, perp_r = await asyncio.gather(
                c.get(SPOT_API, params={"symbol": self.symbol}),
                c.get(FAPI, params={"symbol": self.symbol}),
            )
            spot_r.raise_for_status()
            perp_r.raise_for_status()
            sd = spot_r.json()
            pd = perp_r.json()

        spot_mid = (float(sd["bidPrice"]) + float(sd["askPrice"])) / 2
        perp_mid = (float(pd["bidPrice"]) + float(pd["askPrice"])) / 2
        if spot_mid <= 0 or perp_mid <= 0:
            return

        self.spot = spot_mid
        self.perp = perp_mid
        self.basis = spot_mid - perp_mid
        self.basis_pct = (self.basis / spot_mid) * 100

        # EMA for stable directional state
        if self.basis_pct_ema is None:
            self.basis_pct_ema = self.basis_pct
        else:
            self.basis_pct_ema = (
                EMA_ALPHA * self.basis_pct + (1 - EMA_ALPHA) * self.basis_pct_ema
            )

        # State classification on the EMA, not the raw value.
        if self.basis_pct_ema >= SPOT_LED_THRESHOLD:
            self.state = "spot_led"
        elif self.basis_pct_ema <= PERP_LED_THRESHOLD:
            self.state = "perp_led"
        else:
            self.state = "neutral"

        now_ms = int(time.time() * 1000)
        self.last_update_ms = now_ms

        await self._emit("tick", self.snapshot_current())

        # Persist sample at 1-min cadence
        now_s = int(time.time())
        if now_s - self._last_persist_ts >= PERSIST_INTERVAL_SEC:
            ts_min = (now_s // 60) * 60  # snap to minute boundary
            sample = {
                "ts": ts_min,
                "spot": self.spot,
                "perp": self.perp,
                "basis": self.basis,
                "basis_pct": self.basis_pct,
            }
            self.store.upsert_basis(
                self.symbol, ts_min, self.spot, self.perp, self.basis, self.basis_pct,
            )
            # Replace last entry if same minute, else append
            if self._history and self._history[-1]["ts"] == ts_min:
                self._history[-1] = sample
            else:
                self._history.append(sample)
            self._last_persist_ts = now_s
            await self._emit("sample", sample)

    # ---------- snapshots ----------

    def snapshot_current(self) -> dict:
        return {
            "symbol": self.symbol,
            "spot": self.spot,
            "perp": self.perp,
            "basis": self.basis,
            "basis_pct": self.basis_pct,
            "basis_pct_ema": self.basis_pct_ema or 0.0,
            "state": self.state,
            "updated": self.last_update_ms,
        }

    def snapshot_history(self) -> dict:
        return {
            "current": self.snapshot_current(),
            "history": list(self._history),
        }
