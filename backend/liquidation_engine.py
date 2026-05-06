"""
Liquidation heatmap engine.

Pulls liquidation-history and ohlcv-history from Coinalyze for BTCUSDT_PERP on
Binance Futures, then bins each interval's long/short liquidation USD across
the [low, high] price range it occurred in. The result is a true price-level
liquidation heatmap built from real historical events (no leverage modeling).

Refresh cadence: once per hour. The first poll cold-fills `LOOKBACK_DAYS` of
data; later polls only fetch intervals newer than the latest stored timestamp.
"""
import asyncio
import time
from typing import Callable

import httpx

from storage import Store


COINALYZE = "https://api.coinalyze.net/v1"

# Tuning
LOOKBACK_DAYS = 7              # how much history to keep
INTERVAL = "5min"              # Coinalyze interval string
INTERVAL_SEC = 5 * 60
POLL_INTERVAL_SEC = 300        # 5 minutes — matches Coinalyze's source cadence
NUM_BUCKETS = 80               # price-level buckets in the rendered heatmap


def _round_bucket_size(raw: float) -> float:
    """Round bucket size up to a 'nice' number ($25, $50, $100, $250, ...)."""
    if raw <= 0:
        return 100.0
    nice = [10, 25, 50, 100, 250, 500, 1000, 2500, 5000, 10000]
    for n in nice:
        if raw <= n:
            return float(n)
    return float(nice[-1])


class LiquidationEngine:
    def __init__(self, symbol: str, coinalyze_symbol: str, api_key: str, store: Store):
        self.symbol = symbol.upper()
        self.coinalyze_symbol = coinalyze_symbol
        self.api_key = api_key
        self.store = store
        self._listeners: list[Callable] = []
        self._running = False
        self._heatmap: dict | None = None
        self._last_update_ms: int = 0

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
                print(f"[liq listener error] {e}")

    # ---------- API calls ----------

    async def _fetch_window(self, client: httpx.AsyncClient, from_ts: int, to_ts: int):
        """Fetch liquidation + ohlcv for [from_ts, to_ts] (unix seconds)."""
        params_liq = {
            "symbols": self.coinalyze_symbol,
            "interval": INTERVAL,
            "from": from_ts,
            "to": to_ts,
            "convert_to_usd": "true",
        }
        params_ohlcv = {
            "symbols": self.coinalyze_symbol,
            "interval": INTERVAL,
            "from": from_ts,
            "to": to_ts,
        }
        r1, r2 = await asyncio.gather(
            client.get(f"{COINALYZE}/liquidation-history", params=params_liq,
                       headers={"api_key": self.api_key}),
            client.get(f"{COINALYZE}/ohlcv-history", params=params_ohlcv,
                       headers={"api_key": self.api_key}),
        )
        r1.raise_for_status()
        r2.raise_for_status()
        liq = r1.json()
        ohlcv = r2.json()

        liq_rows = liq[0]["history"] if liq and liq[0].get("history") else []
        ohlcv_rows = ohlcv[0]["history"] if ohlcv and ohlcv[0].get("history") else []

        # Index OHLCV by timestamp for join
        ohlcv_by_ts = {row["t"]: row for row in ohlcv_rows}

        merged = []
        for row in liq_rows:
            t = row["t"]
            o = ohlcv_by_ts.get(t)
            if not o:
                continue
            merged.append({
                "ts": t,
                "long_usd": float(row.get("l", 0) or 0),
                "short_usd": float(row.get("s", 0) or 0),
                "low": float(o["l"]),
                "high": float(o["h"]),
            })
        return merged

    # ---------- Bucketing ----------

    def _build_heatmap(self) -> dict:
        cutoff_ts = int(time.time()) - LOOKBACK_DAYS * 86400
        rows = self.store.load_liquidations(self.symbol, since_ts=cutoff_ts, limit=20000)
        if not rows:
            return {
                "symbol": self.symbol,
                "updated": self._last_update_ms,
                "from_ts": cutoff_ts,
                "to_ts": int(time.time()),
                "bucket_size": 0,
                "price_min": 0,
                "price_max": 0,
                "max_usd": 0,
                "buckets": [],
                "total_long_usd": 0,
                "total_short_usd": 0,
                "interval": INTERVAL,
            }

        # Filter empties; keep only intervals where at least one side had liqs
        active = [r for r in rows if (r["long_usd"] + r["short_usd"]) > 0 and r["high"] > r["low"]]
        if not active:
            active = rows  # fall back so the price range is still meaningful

        price_min = min(r["low"] for r in active)
        price_max = max(r["high"] for r in active)
        if price_max <= price_min:
            price_max = price_min * 1.01 + 1

        raw_size = (price_max - price_min) / NUM_BUCKETS
        bucket_size = _round_bucket_size(raw_size)
        # Snap min/max to bucket boundaries
        b_min = (int(price_min // bucket_size)) * bucket_size
        b_max = (int(price_max // bucket_size) + 1) * bucket_size
        n = int(round((b_max - b_min) / bucket_size))
        longs = [0.0] * n
        shorts = [0.0] * n

        for r in active:
            lo, hi = r["low"], r["high"]
            l_usd, s_usd = r["long_usd"], r["short_usd"]
            if l_usd <= 0 and s_usd <= 0:
                continue
            span = hi - lo
            if span <= 0:
                # All-in single bucket
                idx = int((lo - b_min) // bucket_size)
                if 0 <= idx < n:
                    longs[idx] += l_usd
                    shorts[idx] += s_usd
                continue
            # Distribute uniformly over the candle's [lo, hi] range
            lo_idx = max(0, int((lo - b_min) // bucket_size))
            hi_idx = min(n - 1, int((hi - b_min) // bucket_size))
            for i in range(lo_idx, hi_idx + 1):
                bucket_lo = b_min + i * bucket_size
                bucket_hi = bucket_lo + bucket_size
                overlap = max(0.0, min(hi, bucket_hi) - max(lo, bucket_lo))
                if overlap <= 0:
                    continue
                frac = overlap / span
                longs[i] += l_usd * frac
                shorts[i] += s_usd * frac

        buckets = []
        max_usd = 0.0
        for i in range(n):
            lu = longs[i]
            su = shorts[i]
            if lu == 0 and su == 0:
                continue
            buckets.append({
                "price": b_min + (i + 0.5) * bucket_size,
                "long_usd": lu,
                "short_usd": su,
            })
            max_usd = max(max_usd, lu, su)

        return {
            "symbol": self.symbol,
            "updated": self._last_update_ms,
            "from_ts": int(active[0]["ts"]) if active else cutoff_ts,
            "to_ts": int(active[-1]["ts"]) if active else int(time.time()),
            "bucket_size": bucket_size,
            "price_min": b_min,
            "price_max": b_max,
            "max_usd": max_usd,
            "buckets": buckets,
            "total_long_usd": sum(longs),
            "total_short_usd": sum(shorts),
            "interval": INTERVAL,
        }

    # ---------- Lifecycle ----------

    async def seed_history(self):
        """Cold-fill: fetch the full LOOKBACK_DAYS window."""
        now = int(time.time())
        last = self.store.last_liquidation_ts(self.symbol)
        # If we already have recent data (< 2h old), just rebuild and skip fetch.
        if last and (now - last) < 2 * 3600:
            print(f"[liq] resuming from stored data (last ts {last}, age {now - last}s)")
            self._last_update_ms = int(time.time() * 1000)
            self._heatmap = self._build_heatmap()
            return

        from_ts = max(now - LOOKBACK_DAYS * 86400, (last or 0) + INTERVAL_SEC)
        try:
            async with httpx.AsyncClient(timeout=30) as c:
                merged = await self._fetch_window(c, from_ts, now)
            for row in merged:
                self.store.upsert_liquidation(
                    self.symbol, row["ts"], row["long_usd"], row["short_usd"],
                    row["low"], row["high"],
                )
            print(f"[liq] seeded {len(merged)} intervals")
        except Exception as e:
            print(f"[liq] seed failed: {e}")

        self._last_update_ms = int(time.time() * 1000)
        self._heatmap = self._build_heatmap()

    async def run(self):
        self._running = True
        backoff = 60
        while self._running:
            try:
                await asyncio.sleep(POLL_INTERVAL_SEC)
                await self._poll()
                backoff = 60
            except Exception as e:
                print(f"[liq] poll error, retry in {backoff}s: {e}")
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 1800)

    async def _poll(self):
        now = int(time.time())
        last = self.store.last_liquidation_ts(self.symbol) or (now - LOOKBACK_DAYS * 86400)
        from_ts = max(last + INTERVAL_SEC, now - LOOKBACK_DAYS * 86400)
        if from_ts >= now:
            return
        async with httpx.AsyncClient(timeout=30) as c:
            merged = await self._fetch_window(c, from_ts, now)
        for row in merged:
            self.store.upsert_liquidation(
                self.symbol, row["ts"], row["long_usd"], row["short_usd"],
                row["low"], row["high"],
            )
        if merged:
            print(f"[liq] +{len(merged)} new intervals")
        self._last_update_ms = int(time.time() * 1000)
        self._heatmap = self._build_heatmap()
        await self._emit("snapshot", self._heatmap)

    def snapshot(self) -> dict:
        if self._heatmap is None:
            self._heatmap = self._build_heatmap()
        return self._heatmap
