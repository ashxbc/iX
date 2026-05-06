"""
Live liquidation feed.

Connects to Binance USD-M Futures WebSocket `<symbol>@forceOrder` and emits
aggregated liquidation events to listeners. Aggregation rules:

  * 1-second buffer window
  * Group by side: LONG liquidations (broker SELL) vs SHORT liquidations (broker BUY)
  * Weighted-average price by USD within each group
  * Emit only if aggregated total >= $1,000

Binance forceOrder side semantics:
  * S = "SELL" -> a LONG position was force-closed  (bearish print)
  * S = "BUY"  -> a SHORT position was force-closed (bullish print)
"""
import asyncio
import json
import time
from typing import Callable

import websockets


WS_URL_TMPL = "wss://fstream.binance.com/ws/{sym}@forceOrder"

BUFFER_WINDOW_SEC = 1.0
MIN_USD = 1_000


class LiveLiquidationEngine:
    def __init__(self, symbol: str):
        self.symbol = symbol.upper()
        self._listeners: list[Callable] = []
        self._buffer: list[dict] = []
        self._lock = asyncio.Lock()
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
                print(f"[live-liq listener error] {e}")

    async def run(self):
        self._running = True
        flush_task = asyncio.create_task(self._flusher())
        try:
            await self._ws_loop()
        finally:
            flush_task.cancel()
            await asyncio.gather(flush_task, return_exceptions=True)

    async def _ws_loop(self):
        backoff = 1
        url = WS_URL_TMPL.format(sym=self.symbol.lower())
        while self._running:
            try:
                async with websockets.connect(url, ping_interval=20, ping_timeout=20) as ws:
                    print(f"[live-liq] connected: {self.symbol}")
                    backoff = 1
                    async for raw in ws:
                        try:
                            msg = json.loads(raw)
                        except Exception:
                            continue
                        o = msg.get("o") or {}
                        if not o:
                            continue
                        try:
                            side = o["S"]                # "BUY" / "SELL"
                            ap = float(o.get("ap") or 0)
                            p = float(o.get("p") or 0)
                            price = ap if ap > 0 else p  # avg price preferred, fallback to order price
                            qty = float(o["q"])
                            ts = int(o.get("T") or o.get("E") or int(time.time() * 1000))
                        except (KeyError, TypeError, ValueError):
                            continue
                        if price <= 0:
                            continue
                        usd = price * qty
                        if usd <= 0:
                            continue
                        # Map exchange side to our directional label
                        liq_side = "long" if side == "SELL" else "short"
                        async with self._lock:
                            self._buffer.append({
                                "ts": ts, "side": liq_side, "price": price, "usd": usd,
                            })
            except Exception as e:
                print(f"[live-liq] ws error, reconnect in {backoff}s: {e}")
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30)

    async def _flusher(self):
        while True:
            await asyncio.sleep(BUFFER_WINDOW_SEC)
            async with self._lock:
                if not self._buffer:
                    continue
                events = self._buffer
                self._buffer = []
            # Group by side
            by_side: dict[str, list[dict]] = {"long": [], "short": []}
            for e in events:
                by_side[e["side"]].append(e)
            for side, group in by_side.items():
                if not group:
                    continue
                total_usd = sum(e["usd"] for e in group)
                if total_usd < MIN_USD:
                    continue
                # USD-weighted average price
                w_price = sum(e["price"] * e["usd"] for e in group) / total_usd
                latest_ts = max(e["ts"] for e in group)
                await self._emit("liq", {
                    "symbol": self.symbol,
                    "side": side,                # "long" -> long got liquidated
                    "usd": total_usd,
                    "price": w_price,
                    "ts": latest_ts,
                    "count": len(group),
                })
