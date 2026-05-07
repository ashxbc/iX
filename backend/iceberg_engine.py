"""
Iceberg / whale-absorption detection.

Whales never place a single $50M bid — exchanges would front-run instantly.
They place ICEBERGS: a small visible chunk that auto-refreshes after each fill.
The signature is absorbed volume >> visible size at a stable price level.

For every supported perp we run two parallel Binance Futures streams:
  * <sym>@aggTrade        — every fill, with price and qty
  * <sym>@depth20@500ms   — top-20 visible book on each side, 500ms refresh

We bucket prices into percentage-width buckets (0.05% of mid) so the
algorithm scales across coins of any price magnitude (BTC $97k vs MEGA $0.5).

Per bucket, in a rolling 10-minute window:
  absorbed_usd  = sum(price × qty) for fills inside the bucket
  visible_min   = smallest non-zero visible USD ever observed at the bucket
  ratio         = absorbed_usd / max(visible_min, $5k floor)

A bucket becomes an "iceberg" if:
  ratio >= ICEBERG_RATIO   (20× by default — strong absorption signature)
  absorbed_usd >= MIN_ABSORBED_USD  ($25k floor to suppress noise)
  last trade in bucket within last 90s (level still active)
  level within ±2% of current price (still relevant)

We emit a "snapshot" of the top-N active icebergs every 5 seconds. The
frontend draws horizontal lines on the price chart at those levels with
intensity scaled by ratio.
"""
import asyncio
import json
import time
from collections import defaultdict, deque
from typing import Callable

import websockets


WS_TRADE = "wss://fstream.binance.com/ws/{sym}@aggTrade"
WS_DEPTH = "wss://fstream.binance.com/ws/{sym}@depth20@500ms"

BUCKET_PCT = 0.0005           # 0.05% — bucket width as % of mid price
WINDOW_SEC = 600              # 10-minute rolling window
ICEBERG_RATIO = 20.0          # absorbed / visible_min threshold
MIN_ABSORBED_USD = 25_000.0   # lower bound on absorption to flag as whale
MIN_VISIBLE_USD = 5_000.0     # floor on visible size to avoid /0 / spurious
PRICE_RELEVANCE_PCT = 0.02    # only show icebergs within ±2% of mid
ALERT_INTERVAL_SEC = 5
TOP_N = 6                     # broadcast top-N strongest icebergs per symbol


class IcebergEngine:
    def __init__(self, symbol: str):
        self.symbol = symbol.upper()
        self._listeners: list[Callable] = []
        self._running = False

        # Trades: deque of (ts_s, price, qty, side[buy|sell])
        self._trades: deque[tuple[float, float, float, str]] = deque()
        # Depth top of book — updated every 500ms
        # bucket_price -> {"bid_usd": float, "ask_usd": float, "visible_min_usd": float, "last_seen": float}
        self._depth: dict[float, dict] = defaultdict(
            lambda: {"bid_usd": 0.0, "ask_usd": 0.0, "visible_min_usd": float("inf"), "last_seen": 0.0}
        )
        self._mid_price: float = 0.0
        self._last_alerts: list[dict] = []

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
                print(f"[iceberg listener error] {e}")

    # ---------- helpers ----------

    def _bucket(self, price: float) -> float | None:
        if self._mid_price <= 0 or price <= 0:
            return None
        size = self._mid_price * BUCKET_PCT
        if size <= 0:
            return None
        return round(price / size) * size

    def _purge_old(self, now_s: float):
        cutoff = now_s - WINDOW_SEC
        while self._trades and self._trades[0][0] < cutoff:
            self._trades.popleft()
        # Drop dead buckets that haven't been seen in 2 windows
        dead = [k for k, v in self._depth.items() if v["last_seen"] < now_s - 2 * WINDOW_SEC]
        for k in dead:
            del self._depth[k]

    # ---------- WS loops ----------

    async def run(self):
        self._running = True
        await asyncio.gather(
            self._trade_loop(),
            self._depth_loop(),
            self._alert_loop(),
            return_exceptions=True,
        )

    async def _trade_loop(self):
        backoff = 1
        url = WS_TRADE.format(sym=self.symbol.lower())
        while self._running:
            try:
                async with websockets.connect(url, ping_interval=20, ping_timeout=20) as ws:
                    backoff = 1
                    async for raw in ws:
                        try:
                            m = json.loads(raw)
                        except Exception:
                            continue
                        try:
                            price = float(m["p"])
                            qty = float(m["q"])
                            ts_ms = int(m["T"])
                            # m["m"] = isMarketMakerBuyer? On Binance: "m":true means buyer is maker (taker SOLD)
                            is_buy = not bool(m.get("m"))
                        except (KeyError, TypeError, ValueError):
                            continue
                        if price <= 0 or qty <= 0:
                            continue
                        self._trades.append((ts_ms / 1000.0, price, qty, "buy" if is_buy else "sell"))
                        if not self._mid_price:
                            self._mid_price = price
            except Exception as e:
                print(f"[iceberg {self.symbol}] trade ws error, retry {backoff}s: {e}")
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30)

    async def _depth_loop(self):
        backoff = 1
        url = WS_DEPTH.format(sym=self.symbol.lower())
        while self._running:
            try:
                async with websockets.connect(url, ping_interval=20, ping_timeout=20) as ws:
                    backoff = 1
                    async for raw in ws:
                        try:
                            m = json.loads(raw)
                        except Exception:
                            continue
                        bids = m.get("b") or m.get("bids") or []
                        asks = m.get("a") or m.get("asks") or []
                        if not bids or not asks:
                            continue
                        try:
                            best_bid = float(bids[0][0])
                            best_ask = float(asks[0][0])
                            mid = (best_bid + best_ask) / 2
                        except (IndexError, ValueError, TypeError):
                            continue
                        if mid <= 0:
                            continue
                        self._mid_price = mid
                        now_s = time.time()
                        # Aggregate visible USD per bucket (top 20 each side)
                        agg: dict[float, dict] = defaultdict(lambda: {"bid_usd": 0.0, "ask_usd": 0.0})
                        for side, levels in (("bid", bids[:20]), ("ask", asks[:20])):
                            for lvl in levels:
                                try:
                                    p = float(lvl[0]); q = float(lvl[1])
                                except (ValueError, TypeError):
                                    continue
                                if p <= 0 or q <= 0:
                                    continue
                                b = self._bucket(p)
                                if b is None:
                                    continue
                                agg[b][f"{side}_usd"] += p * q
                        # Update rolling depth state
                        for bucket, sizes in agg.items():
                            visible_usd = sizes["bid_usd"] + sizes["ask_usd"]
                            if visible_usd <= 0:
                                continue
                            d = self._depth[bucket]
                            d["bid_usd"] = sizes["bid_usd"]
                            d["ask_usd"] = sizes["ask_usd"]
                            d["last_seen"] = now_s
                            # Track minimum non-zero visible — icebergs reload to
                            # the same small size, so the *minimum* we ever saw is
                            # the truthful "what's actually sitting there".
                            if visible_usd < d["visible_min_usd"]:
                                d["visible_min_usd"] = visible_usd
            except Exception as e:
                print(f"[iceberg {self.symbol}] depth ws error, retry {backoff}s: {e}")
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30)

    async def _alert_loop(self):
        # Wait for first data then run every ALERT_INTERVAL_SEC
        while self._running:
            await asyncio.sleep(ALERT_INTERVAL_SEC)
            try:
                await self._compute_and_emit()
            except Exception as e:
                print(f"[iceberg {self.symbol}] alert error: {e}")

    async def _compute_and_emit(self):
        now_s = time.time()
        self._purge_old(now_s)
        if self._mid_price <= 0:
            return
        mid = self._mid_price
        # Sum absorbed_usd per bucket in window
        absorbed: dict[float, dict] = defaultdict(lambda: {"usd": 0.0, "buy_usd": 0.0, "sell_usd": 0.0, "last_ts": 0.0, "trades": 0})
        for ts_s, price, qty, side in self._trades:
            b = self._bucket(price)
            if b is None:
                continue
            usd = price * qty
            a = absorbed[b]
            a["usd"] += usd
            a[f"{side}_usd"] += usd
            a["trades"] += 1
            if ts_s > a["last_ts"]:
                a["last_ts"] = ts_s

        # Build candidate list
        out = []
        relevance_band = mid * PRICE_RELEVANCE_PCT
        for bucket, a in absorbed.items():
            if a["usd"] < MIN_ABSORBED_USD:
                continue
            if abs(bucket - mid) > relevance_band:
                continue
            if a["last_ts"] < now_s - 90:        # not active recently
                continue
            d = self._depth.get(bucket)
            if not d:
                continue
            visible_min = max(d["visible_min_usd"], MIN_VISIBLE_USD) if d["visible_min_usd"] != float("inf") else MIN_VISIBLE_USD
            ratio = a["usd"] / visible_min
            if ratio < ICEBERG_RATIO:
                continue
            # Side classification: which side dominated the absorption
            if a["buy_usd"] > a["sell_usd"] * 1.2:
                whale_side = "bid"   # buyers absorbing → whale bid (support)
            elif a["sell_usd"] > a["buy_usd"] * 1.2:
                whale_side = "ask"   # sellers absorbing → whale ask (resistance)
            else:
                whale_side = "neutral"
            out.append({
                "price": bucket,
                "absorbed_usd": a["usd"],
                "visible_min_usd": visible_min,
                "ratio": ratio,
                "trades": a["trades"],
                "last_ts": int(a["last_ts"] * 1000),
                "side": whale_side,
                "buy_usd": a["buy_usd"],
                "sell_usd": a["sell_usd"],
            })

        out.sort(key=lambda x: x["ratio"], reverse=True)
        out = out[:TOP_N]
        snap = {
            "symbol": self.symbol,
            "mid": mid,
            "ts": int(now_s * 1000),
            "icebergs": out,
        }
        self._last_alerts = out
        await self._emit("snapshot", snap)

    def snapshot(self) -> dict:
        return {
            "symbol": self.symbol,
            "mid": self._mid_price,
            "ts": int(time.time() * 1000),
            "icebergs": list(self._last_alerts),
        }
