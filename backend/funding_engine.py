"""
Funding Rate + Open Interest engine.

Polls Binance USD-M Futures REST endpoints (no websocket exists for these):
  GET /fapi/v1/premiumIndex      -> live funding rate, mark price, next settle
  GET /fapi/v1/openInterest      -> current OI in BTC
  GET /fapi/v1/fundingRate       -> historical funding settlements (every 8h)
  GET /futures/data/openInterestHist -> historical OI (5m periods)

Detects three states from the combo:
  NEUTRAL  - nothing actionable
  SQUEEZE  - OI rising + funding deeply negative (shorts trapped, bullish)
  FLUSH    - OI rising + funding extremely positive (longs trapped, bearish)
"""
import asyncio
import time
from collections import deque
from typing import Callable

import httpx

from storage import Store


FAPI = "https://fapi.binance.com"

POLL_INTERVAL = 30  # seconds between OI/funding polls

# Signal thresholds (tunable)
OI_TREND_LOOKBACK_MS = 60 * 60 * 1000        # 1 hour lookback for OI trend
OI_RISING_THRESHOLD = 0.005                  # +0.5% OI increase = "rising"
FUNDING_DEEP_NEG = -0.0002                   # -0.02% per 8h = deeply negative
FUNDING_EXTREME_POS = 0.0002                 # +0.02% per 8h = extremely positive


class FundingOIEngine:
    def __init__(self, symbol: str, store: Store):
        self.symbol = symbol.upper()
        self.store = store
        self.funding_rate: float = 0.0
        self.next_funding_time: int = 0
        self.mark_price: float = 0.0
        self.oi: float = 0.0          # in BTC
        self.oi_value: float = 0.0    # in USD
        self.signal: str = "neutral"
        self._oi_history: deque[tuple[int, float]] = deque(maxlen=500)  # (ts, oi_value)
        self._listeners: list[Callable] = []
        self._running = False

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
                print(f"[funding listener error] {e}")

    async def seed_history(self):
        async with httpx.AsyncClient(timeout=15) as c:
            # Funding rate history (settlements every 8h)
            try:
                r = await c.get(f"{FAPI}/fapi/v1/fundingRate",
                                params={"symbol": self.symbol, "limit": 500})
                r.raise_for_status()
                for f in r.json():
                    self.store.upsert_funding(self.symbol, int(f["fundingTime"]), float(f["fundingRate"]))
            except Exception as e:
                print(f"[funding] history seed failed: {e}")

            # OI history (5m granularity, last ~41 hours)
            try:
                r = await c.get(f"{FAPI}/futures/data/openInterestHist",
                                params={"symbol": self.symbol, "period": "5m", "limit": 500})
                r.raise_for_status()
                for o in r.json():
                    ts = int(o["timestamp"])
                    oi = float(o["sumOpenInterest"])
                    oi_value = float(o["sumOpenInterestValue"])
                    self.store.upsert_oi(self.symbol, ts, oi, oi_value)
                    self._oi_history.append((ts, oi_value))
            except Exception as e:
                print(f"[funding] OI history seed failed: {e}")

    async def run(self):
        self._running = True
        backoff = 1
        while self._running:
            try:
                await self._poll()
                backoff = 1
                await asyncio.sleep(POLL_INTERVAL)
            except Exception as e:
                print(f"[funding] poll error, retry in {backoff}s: {e}")
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60)

    async def _poll(self):
        async with httpx.AsyncClient(timeout=10) as c:
            r1, r2 = await asyncio.gather(
                c.get(f"{FAPI}/fapi/v1/premiumIndex", params={"symbol": self.symbol}),
                c.get(f"{FAPI}/fapi/v1/openInterest", params={"symbol": self.symbol}),
            )
            r1.raise_for_status()
            r2.raise_for_status()
            d1 = r1.json()
            d2 = r2.json()

        self.funding_rate = float(d1["lastFundingRate"])
        self.next_funding_time = int(d1["nextFundingTime"])
        self.mark_price = float(d1["markPrice"])

        oi = float(d2["openInterest"])
        ts = int(d2["time"])
        oi_value = oi * self.mark_price

        # Persist a new funding settlement if it appeared
        if "interestRate" in d1:  # also implies premiumIndex came back
            # premiumIndex returns lastFundingRate; settlement times move every 8h.
            # We just upsert; if same ts (8h boundary), it's a no-op.
            settle_ts = self.next_funding_time - 8 * 3600 * 1000
            self.store.upsert_funding(self.symbol, settle_ts, self.funding_rate)

        self.oi = oi
        self.oi_value = oi_value
        self._oi_history.append((ts, oi_value))
        self.store.upsert_oi(self.symbol, ts, oi, oi_value)

        self.signal = self._detect_signal()

        await self._emit("tick", self.snapshot_current())

    def _detect_signal(self) -> str:
        if len(self._oi_history) < 2:
            return "neutral"

        # Find the OI value from ~OI_TREND_LOOKBACK_MS ago
        latest_ts, latest_oi = self._oi_history[-1]
        target_ts = latest_ts - OI_TREND_LOOKBACK_MS
        baseline_oi = None
        for ts, oi in self._oi_history:
            if ts >= target_ts:
                baseline_oi = oi
                break
        if baseline_oi is None or baseline_oi == 0:
            baseline_oi = self._oi_history[0][1]

        oi_change = (latest_oi - baseline_oi) / baseline_oi if baseline_oi else 0.0

        if oi_change >= OI_RISING_THRESHOLD:
            if self.funding_rate <= FUNDING_DEEP_NEG:
                return "squeeze"   # bullish: shorts trapped
            if self.funding_rate >= FUNDING_EXTREME_POS:
                return "flush"     # bearish: longs trapped
        return "neutral"

    def snapshot_current(self) -> dict:
        return {
            "symbol": self.symbol,
            "funding_rate": self.funding_rate,
            "next_funding_time": self.next_funding_time,
            "mark_price": self.mark_price,
            "oi": self.oi,
            "oi_value": self.oi_value,
            "signal": self.signal,
            "oi_change_1h": self._oi_change_pct(OI_TREND_LOOKBACK_MS),
        }

    def _oi_change_pct(self, lookback_ms: int) -> float:
        if len(self._oi_history) < 2:
            return 0.0
        latest_ts, latest_oi = self._oi_history[-1]
        target_ts = latest_ts - lookback_ms
        baseline = None
        for ts, oi in self._oi_history:
            if ts >= target_ts:
                baseline = oi
                break
        if baseline is None or baseline == 0:
            return 0.0
        return (latest_oi - baseline) / baseline

    def snapshot_history(self) -> dict:
        funding = self.store.load_funding(self.symbol, 500)
        oi = self.store.load_oi(self.symbol, 500)
        return {
            "funding": funding,
            "oi": oi,
            "current": self.snapshot_current(),
        }
