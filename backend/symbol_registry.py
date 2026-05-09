"""
Symbol registry — caches Binance Futures + Spot exchange info, proxies
CoinGecko search for autocomplete, and serves token metadata (logo, price,
24h volume) on demand.

Used by the search-bar UI to support any coin Binance lists as a USDT-M perp
without us having to hardcode it.
"""
import asyncio
import re
import time
from typing import Any

import httpx


FAPI_EXCHANGE_INFO = "https://fapi.binance.com/fapi/v1/exchangeInfo"
SPOT_EXCHANGE_INFO = "https://api.binance.com/api/v3/exchangeInfo"
COINGECKO_SEARCH = "https://api.coingecko.com/api/v3/search"
FAPI_TICKER_24H = "https://fapi.binance.com/fapi/v1/ticker/24hr"

SEARCH_TTL = 3600           # 1h — search results stable enough
LOGO_TTL = 7 * 86400        # 7d — logos basically never change
TICKER_TTL = 8              # 8s — refresh price + 24h vol


class SymbolRegistry:
    def __init__(self):
        self.perp_symbols: set[str] = set()
        self.spot_symbols: set[str] = set()
        self._search_cache: dict[str, tuple[float, list[dict]]] = {}
        self._logo_cache: dict[str, tuple[float, str]] = {}
        self._ticker_cache: dict[str, tuple[float, dict]] = {}
        self._lock = asyncio.Lock()

    async def init(self):
        """Populate perp + spot symbol sets at boot."""
        async with httpx.AsyncClient(timeout=15) as c:
            try:
                r = await c.get(FAPI_EXCHANGE_INFO)
                r.raise_for_status()
                for s in r.json().get("symbols", []):
                    if (s.get("contractType") == "PERPETUAL"
                            and s.get("quoteAsset") == "USDT"
                            and s.get("status") == "TRADING"):
                        self.perp_symbols.add(s["symbol"])
                print(f"[registry] {len(self.perp_symbols)} Binance USDT-M perps")
            except Exception as e:
                print(f"[registry] perp list fetch failed: {e}")

            try:
                r = await c.get(SPOT_EXCHANGE_INFO)
                r.raise_for_status()
                for s in r.json().get("symbols", []):
                    if s.get("status") == "TRADING" and s.get("quoteAsset") == "USDT":
                        self.spot_symbols.add(s["symbol"])
                print(f"[registry] {len(self.spot_symbols)} Binance USDT spot pairs")
            except Exception as e:
                print(f"[registry] spot list fetch failed: {e}")

    def has_perp(self, symbol: str) -> bool:
        return symbol.upper() in self.perp_symbols

    def has_spot(self, symbol: str) -> bool:
        return symbol.upper() in self.spot_symbols

    @staticmethod
    def base_of(symbol: str) -> str:
        return re.sub(r"USDT$", "", symbol.upper())

    async def search(self, q: str, limit: int = 12) -> list[dict]:
        q = q.strip().lower()
        if len(q) < 1:
            return []
        now = time.time()
        cached = self._search_cache.get(q)
        if cached and cached[0] > now:
            return cached[1][:limit]

        try:
            async with httpx.AsyncClient(timeout=8) as c:
                r = await c.get(COINGECKO_SEARCH, params={"query": q})
                r.raise_for_status()
                data = r.json()
        except Exception as e:
            print(f"[registry] search '{q}' error: {e}")
            return []

        results: list[dict] = []
        seen_perps: set[str] = set()
        for coin in data.get("coins", []):
            base = (coin.get("symbol") or "").upper()
            if not base:
                continue
            perp_symbol = f"{base}USDT"
            if perp_symbol not in self.perp_symbols:
                continue
            if perp_symbol in seen_perps:
                continue   # CoinGecko can have multiple coins with the same ticker
            seen_perps.add(perp_symbol)
            logo = coin.get("large") or coin.get("thumb") or ""
            results.append({
                "symbol": perp_symbol,
                "base": base,
                "name": coin.get("name", base),
                "logo": logo,
                "rank": coin.get("market_cap_rank"),
            })
            if logo:
                self._logo_cache[perp_symbol] = (now + LOGO_TTL, logo)
            if len(results) >= limit:
                break

        # CoinGecko orders by relevance already, but also boost by market cap
        results.sort(key=lambda x: (x.get("rank") or 999_999))
        self._search_cache[q] = (now + SEARCH_TTL, results)
        return results[:limit]

    async def _fetch_logo(self, symbol: str) -> str:
        """One-shot logo lookup via CoinGecko search by base ticker."""
        sym = symbol.upper()
        now = time.time()
        cached = self._logo_cache.get(sym)
        if cached and cached[0] > now:
            return cached[1]
        base = self.base_of(sym)
        try:
            async with httpx.AsyncClient(timeout=6) as c:
                r = await c.get(COINGECKO_SEARCH, params={"query": base})
                r.raise_for_status()
                for coin in r.json().get("coins", []):
                    if (coin.get("symbol") or "").upper() == base:
                        logo = coin.get("large") or coin.get("thumb") or ""
                        if logo:
                            self._logo_cache[sym] = (now + LOGO_TTL, logo)
                            return logo
        except Exception as e:
            print(f"[registry] logo {sym} error: {e}")
        return ""

    async def get_meta(self, symbol: str) -> dict:
        sym = symbol.upper()
        now = time.time()

        # 24h ticker
        ticker: dict = {}
        cached = self._ticker_cache.get(sym)
        if cached and cached[0] > now:
            ticker = cached[1]
        else:
            try:
                async with httpx.AsyncClient(timeout=5) as c:
                    r = await c.get(FAPI_TICKER_24H, params={"symbol": sym})
                    r.raise_for_status()
                    ticker = r.json()
                    self._ticker_cache[sym] = (now + TICKER_TTL, ticker)
            except Exception as e:
                print(f"[registry] ticker {sym} error: {e}")

        # Logo (cached separately, looked up if missing)
        logo = ""
        cached_logo = self._logo_cache.get(sym)
        if cached_logo and cached_logo[0] > now:
            logo = cached_logo[1]
        else:
            logo = await self._fetch_logo(sym)

        def _f(key: str) -> float:
            try:
                return float(ticker.get(key, 0) or 0)
            except (TypeError, ValueError):
                return 0.0

        return {
            "symbol": sym,
            "base": self.base_of(sym),
            "price": _f("lastPrice"),
            "change_24h_pct": _f("priceChangePercent"),
            "volume_24h_usd": _f("quoteVolume"),
            "high_24h": _f("highPrice"),
            "low_24h": _f("lowPrice"),
            "logo": logo,
        }
