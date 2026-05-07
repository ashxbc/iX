"""
Taker Buy/Sell Ratio engine — multi-timeframe aggression flow.

Binance Futures classifies every fill at the matching engine level: a "taker
buy" is an aggressive market buyer crossing the spread; a "taker sell" is an
aggressive market seller. The ratio

    ratio = taker_buy_qv / total_qv     # quote-volume weighted

measures who is more urgent. >0.5 = buyers more aggressive, <0.5 = sellers.

We track 3 timeframes simultaneously (5m / 15m / 1h) so we can detect
*alignment* (all aggressive in the same direction = strong flow) vs
*divergence* (timeframes disagree = exhaustion / distribution / absorption).

Signal logic (server-side):
  * Per TF: EMA-smoothed ratio to denoise microstructure noise
  * Composite "regime":
        bull_strong  — all 3 EMAs > 0.52
        bull         — all 3 EMAs > 0.50
        bear_strong  — all 3 EMAs < 0.48
        bear         — all 3 EMAs < 0.50
        diverging    — TFs disagree (most actionable; needs price context)
  * "alignment" score in [-1, +1] = mean( (ema - 0.5) * 2 ) clipped

Divergence detection vs price (the predictive part):
  * If 1h ratio falling while price making higher highs over the lookback
    window  → BEARISH DIV (distribution: rallies sold into).
  * If 1h ratio rising while price making lower lows
    → BULLISH DIV (absorption: dips bought).
"""
import asyncio
import json
import time
from collections import deque
from typing import Callable

import httpx
import websockets

from storage import Store


FAPI_KLINES = "https://fapi.binance.com/fapi/v1/klines"
WS_COMBINED = "wss://fstream.binance.com/stream?streams={streams}"

TIMEFRAMES = ["5m", "15m", "1h"]
TF_LIMIT = {"5m": 288, "15m": 192, "1h": 168}   # ~24h / 48h / 7d
EMA_ALPHA = {"5m": 0.30, "15m": 0.20, "1h": 0.15}

BULL_STRONG = 0.52
BEAR_STRONG = 0.48

DIV_LOOKBACK = {"5m": 12, "15m": 8, "1h": 6}    # bars to compare price vs ratio trend
DIV_PRICE_EPS = 0.001    # 0.1% — minimum price move to count as a HH/LL
DIV_RATIO_EPS = 0.005    # ratio EMA delta floor


class TakerEngine:
    def __init__(self, symbol: str, store: Store):
        self.symbol = symbol.upper()
        self.store = store
        self._listeners: list[Callable] = []
        self._running = False

        # per-TF history of closed bars (most recent last)
        self.bars: dict[str, deque[dict]] = {tf: deque(maxlen=TF_LIMIT[tf]) for tf in TIMEFRAMES}
        # per-TF current (in-progress) bar
        self.current: dict[str, dict | None] = {tf: None for tf in TIMEFRAMES}
        # per-TF EMA of the ratio
        self.ema: dict[str, float | None] = {tf: None for tf in TIMEFRAMES}
        self.last_update_ms: int = 0

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
                print(f"[taker listener error] {e}")

    # ---------- lifecycle ----------

    async def seed_history(self):
        """Pull recent klines per TF, hydrate bars + EMA."""
        async with httpx.AsyncClient(timeout=10) as c:
            tasks = [
                c.get(FAPI_KLINES, params={
                    "symbol": self.symbol, "interval": tf, "limit": TF_LIMIT[tf],
                })
                for tf in TIMEFRAMES
            ]
            results = await asyncio.gather(*tasks, return_exceptions=True)

        for tf, res in zip(TIMEFRAMES, results):
            if isinstance(res, Exception):
                print(f"[taker] seed {tf} failed: {res}")
                continue
            try:
                rows = res.json()
            except Exception:
                continue
            for r in rows:
                # [openTime, o, h, l, c, baseVol, closeTime, quoteVol, trades,
                #  takerBuyBaseVol, takerBuyQuoteVol, ignore]
                ts_open = int(r[0])
                close = float(r[4])
                qv = float(r[7])
                tbqv = float(r[10])
                if qv <= 0:
                    continue
                ratio = tbqv / qv
                bar = {"ts": ts_open, "close": close, "ratio": ratio, "qv": qv}
                self.bars[tf].append(bar)
                self.ema[tf] = (
                    ratio if self.ema[tf] is None
                    else EMA_ALPHA[tf] * ratio + (1 - EMA_ALPHA[tf]) * self.ema[tf]
                )
                self.store.upsert_taker(self.symbol, tf, ts_open, qv, tbqv, ratio)
            print(f"[taker] seeded {tf}: {len(self.bars[tf])} bars, ema={self.ema[tf]:.4f}")

    async def run(self):
        self._running = True
        backoff = 1
        streams = "/".join(f"{self.symbol.lower()}@kline_{tf}" for tf in TIMEFRAMES)
        url = WS_COMBINED.format(streams=streams)
        while self._running:
            try:
                async with websockets.connect(url, ping_interval=20, ping_timeout=20) as ws:
                    print(f"[taker] connected: {self.symbol} ({','.join(TIMEFRAMES)})")
                    backoff = 1
                    async for raw in ws:
                        try:
                            msg = json.loads(raw)
                        except Exception:
                            continue
                        await self._handle_kline(msg.get("data") or {})
            except Exception as e:
                print(f"[taker] ws error, reconnect in {backoff}s: {e}")
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30)

    async def _handle_kline(self, data: dict):
        if data.get("e") != "kline":
            return
        k = data.get("k") or {}
        tf = k.get("i")
        if tf not in TIMEFRAMES:
            return
        try:
            ts_open = int(k["t"])
            close = float(k["c"])
            qv = float(k["q"])           # total quote vol (for the bar so far)
            tbqv = float(k["Q"])         # taker buy quote vol (for the bar so far)
            is_closed = bool(k.get("x"))
        except (KeyError, TypeError, ValueError):
            return
        if qv <= 0:
            return
        ratio = tbqv / qv
        bar = {"ts": ts_open, "close": close, "ratio": ratio, "qv": qv}
        self.current[tf] = bar

        if is_closed:
            # Promote to closed-bars deque, replacing if same ts (re-broadcast).
            dq = self.bars[tf]
            if dq and dq[-1]["ts"] == ts_open:
                dq[-1] = bar
            else:
                dq.append(bar)
            # Update EMA on the *closed* ratio only — keeps the smoother stable.
            self.ema[tf] = (
                ratio if self.ema[tf] is None
                else EMA_ALPHA[tf] * ratio + (1 - EMA_ALPHA[tf]) * self.ema[tf]
            )
            self.store.upsert_taker(self.symbol, tf, ts_open, qv, tbqv, ratio)
            await self._emit("bar", {
                "tf": tf, "ts": ts_open, "ratio": ratio,
                "ema": self.ema[tf], "qv": qv, "close": close,
            })

        self.last_update_ms = int(time.time() * 1000)
        # Always emit a tick for the live readout.
        await self._emit("tick", self.snapshot_current())

    # ---------- signal logic ----------

    def _regime(self) -> str:
        emas = [self.ema[tf] for tf in TIMEFRAMES]
        if any(e is None for e in emas):
            return "neutral"
        if all(e > BULL_STRONG for e in emas):
            return "bull_strong"
        if all(e > 0.5 for e in emas):
            return "bull"
        if all(e < BEAR_STRONG for e in emas):
            return "bear_strong"
        if all(e < 0.5 for e in emas):
            return "bear"
        return "diverging"

    def _alignment(self) -> float:
        """Scalar in roughly [-1, +1]: positive = bullish flow across TFs."""
        emas = [self.ema[tf] for tf in TIMEFRAMES]
        if any(e is None for e in emas):
            return 0.0
        score = sum((e - 0.5) * 2 for e in emas) / len(emas)
        # Clip to a reasonable display range — raw ratio rarely exceeds ±0.2.
        return max(-1.0, min(1.0, score * 5))   # *5 so 0.10 EMA delta = 1.0

    def _divergence(self) -> dict | None:
        """
        Compare 1h price trend vs 1h ratio EMA trend over DIV_LOOKBACK bars.
        Returns the most recent active divergence, if any.
        """
        tf = "1h"
        bars = list(self.bars[tf])
        n = DIV_LOOKBACK[tf]
        if len(bars) < n + 1:
            return None
        recent = bars[-n:]
        prior_close = bars[-(n + 1)]["close"]
        latest_close = recent[-1]["close"]

        # Price direction
        price_chg = (latest_close - prior_close) / prior_close
        if abs(price_chg) < DIV_PRICE_EPS:
            return None

        # Reconstruct EMA trajectory over those bars (rolling EMA from prior state).
        # We don't have historical EMA stored, so approximate with simple mean of the
        # last n ratios vs. the n before — robust to noise for a directional read.
        if len(bars) < 2 * n:
            return None
        prior_window = bars[-2 * n:-n]
        recent_window = recent
        prior_ratio_mean = sum(b["ratio"] for b in prior_window) / n
        recent_ratio_mean = sum(b["ratio"] for b in recent_window) / n
        ratio_chg = recent_ratio_mean - prior_ratio_mean

        if abs(ratio_chg) < DIV_RATIO_EPS:
            return None

        # Bearish divergence: price up, ratio down (distribution)
        if price_chg > 0 and ratio_chg < 0:
            return {
                "type": "bearish",
                "ts": recent[-1]["ts"],
                "price_chg_pct": price_chg * 100,
                "ratio_chg": ratio_chg,
                "note": "price up, taker flow weakening — distribution",
            }
        # Bullish divergence: price down, ratio up (absorption)
        if price_chg < 0 and ratio_chg > 0:
            return {
                "type": "bullish",
                "ts": recent[-1]["ts"],
                "price_chg_pct": price_chg * 100,
                "ratio_chg": ratio_chg,
                "note": "price down, taker flow strengthening — absorption",
            }
        return None

    # ---------- snapshots ----------

    def _tf_summary(self, tf: str) -> dict:
        cur = self.current[tf]
        last_closed = self.bars[tf][-1] if self.bars[tf] else None
        return {
            "tf": tf,
            "ratio": (cur or last_closed or {}).get("ratio"),
            "ema": self.ema[tf],
            "ts": (cur or last_closed or {}).get("ts"),
            "close": (cur or last_closed or {}).get("close"),
        }

    def snapshot_current(self) -> dict:
        return {
            "symbol": self.symbol,
            "tfs": {tf: self._tf_summary(tf) for tf in TIMEFRAMES},
            "regime": self._regime(),
            "alignment": self._alignment(),
            "divergence": self._divergence(),
            "updated": self.last_update_ms,
        }

    def snapshot_history(self) -> dict:
        return {
            "current": self.snapshot_current(),
            "history": {tf: list(self.bars[tf]) for tf in TIMEFRAMES},
        }
