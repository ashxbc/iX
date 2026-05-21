"""
Bitunix symbol registry — fetches all active USDT perpetual futures pairs
from the Bitunix REST API, caches 24h ticker data, and drives symbol
search and metadata endpoints.

No CoinGecko, no Binance — Bitunix only.
"""
import asyncio
import re
import time

import httpx

BITUNIX_REST = "https://fapi.bitunix.com"
PAIRS_URL    = f"{BITUNIX_REST}/api/v1/futures/market/trading_pairs"
TICKERS_URL  = f"{BITUNIX_REST}/api/v1/futures/market/tickers"

TICKER_TTL = 8       # seconds — price data
PAIRS_TTL  = 3600    # seconds — pairs list changes rarely


class BitunixRegistry:
    def __init__(self):
        self.pairs: list[dict] = []         # full pair metadata
        self.symbol_set: set[str] = set()   # fast membership test
        self._ticker_cache: dict[str, tuple[float, dict]] = {}
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # Boot
    # ------------------------------------------------------------------

    async def init(self):
        """Load all active USDT-M perp pairs from Bitunix at startup."""
        await self._load_pairs()

    async def _load_pairs(self):
        try:
            async with httpx.AsyncClient(timeout=15) as c:
                r = await c.get(PAIRS_URL)
                r.raise_for_status()
                for pair in r.json().get("data", []):
                    if (pair.get("symbolStatus") == "OPEN"
                            and pair.get("quote") == "USDT"):
                        sym = pair["symbol"].upper()
                        self.symbol_set.add(sym)
                        self.pairs.append(pair)
            print(f"[bitunix-registry] {len(self.symbol_set)} active USDT perp pairs loaded")
        except Exception as e:
            print(f"[bitunix-registry] pairs fetch failed: {e}")

    # ------------------------------------------------------------------
    # Queries
    # ------------------------------------------------------------------

    def has_perp(self, symbol: str) -> bool:
        return symbol.upper() in self.symbol_set

    def all_symbols(self) -> list[str]:
        return sorted(self.symbol_set)

    @staticmethod
    def base_of(symbol: str) -> str:
        return re.sub(r"USDT$", "", symbol.upper())

    async def search(self, q: str, limit: int = 12) -> list[dict]:
        q = q.strip().upper()
        if not q:
            return []
        results = []
        for sym in sorted(self.symbol_set):
            base = self.base_of(sym)
            if q in sym or q in base:
                results.append({
                    "symbol": sym,
                    "base": base,
                    "name": base,
                    "logo": "",
                    "rank": None,
                })
            if len(results) >= limit:
                break
        return results

    async def get_meta(self, symbol: str) -> dict:
        sym = symbol.upper()
        now = time.time()

        cached = self._ticker_cache.get(sym)
        ticker: dict = {}
        if cached and cached[0] > now:
            ticker = cached[1]
        else:
            try:
                async with httpx.AsyncClient(timeout=5) as c:
                    r = await c.get(TICKERS_URL, params={"symbols": sym})
                    r.raise_for_status()
                    data = r.json().get("data", [])
                    ticker = data[0] if data else {}
                    self._ticker_cache[sym] = (now + TICKER_TTL, ticker)
            except Exception as e:
                print(f"[bitunix-registry] ticker {sym} error: {e}")

        def _f(key: str) -> float:
            try:
                return float(ticker.get(key) or 0)
            except (TypeError, ValueError):
                return 0.0

        last  = _f("lastPrice")
        open_ = _f("open")
        change_pct = ((last - open_) / open_ * 100) if open_ else 0.0

        return {
            "symbol":          sym,
            "base":            self.base_of(sym),
            "price":           last,
            "change_24h_pct":  change_pct,
            "volume_24h_usd":  _f("quoteVol"),
            "high_24h":        _f("high"),
            "low_24h":         _f("low"),
            "mark_price":      _f("markPrice"),
            "logo":            "",
        }
