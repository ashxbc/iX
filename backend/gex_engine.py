"""
Options Gamma Exposure (GEX) engine — Deribit BTC options.

Pulls the full BTC options book every 5 minutes, computes Black-Scholes gamma
per contract, aggregates GEX per strike, and derives a single "regime" state:

  * pinning   — net positive GEX dominant + spot above flip zone.
                Dealers are long gamma here: they buy dips, sell rips.
                Vol gets suppressed, price gets pinned.
  * explosive — net negative GEX dominant or spot below flip zone.
                Dealers are short gamma here: they buy rallies, sell dumps.
                Vol expands, moves accelerate.
  * neutral   — borderline, insufficient data, or no flip detected.

Why this matters: TradFi desks watch this religiously because dealer hedging
flow is *deterministic* — when price crosses the flip zone, vol changes
regime within hours, not days. Crypto retail almost never looks at it.

Flip zone: cumulative net GEX from low strikes to high crosses zero. That's
the price level where dealer hedging behaviour reverses.

Filters (intentionally tight to cut noise):
  * Expiries ≤ 14 days — gamma is concentrated here, far-dated barely matters
  * Strikes within ±15% of spot — wings have negligible gamma
  * mark_iv > 0 and OI > 0 — drop dead instruments

Black-Scholes gamma:
  d1    = (ln(S/K) + 0.5σ²T) / (σ√T)
  gamma = N'(d1) / (S σ √T)

Per-contract dealer GEX (relative magnitudes — the 0.01 for "$ per 1% move"
is dropped because we only compare strikes against each other):
  gex_call = gamma × call_OI × S² × contract_size
  gex_put  = gamma × put_OI  × S² × contract_size
  net_gex  = gex_call − gex_put     (dealer convention: net short calls,
                                     net long puts)
"""
import asyncio
import math
import re
import time
from collections import defaultdict
from datetime import datetime, timezone
from typing import Callable

import httpx

from storage import Store


DERIBIT_URL = "https://www.deribit.com/api/v2/public/get_book_summary_by_currency"
POLL_INTERVAL_SEC = 300        # 5 minutes
MAX_DAYS_TO_EXPIRY = 14
STRIKE_RANGE_PCT = 0.15
CONTRACT_SIZE = 1.0            # Deribit BTC/ETH option = 1 underlying

# Deribit supports BTC, ETH, SOL options. Pattern: "<CCY>-<DDMMMYY>-<strike>-<C|P>"
MONTHS = {
    "JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MAY": 5, "JUN": 6,
    "JUL": 7, "AUG": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12,
}


def _parse_instrument(name: str, currency: str):
    pattern = re.compile(rf"^{currency}-(\d{{1,2}})([A-Z]{{3}})(\d{{2}})-(\d+)-([CP])$")
    m = pattern.match(name)
    if not m:
        return None
    day, mon, yr, strike, side = m.groups()
    try:
        # Deribit settles at 08:00 UTC on expiry day.
        d = datetime(2000 + int(yr), MONTHS[mon], int(day), 8, 0, 0, tzinfo=timezone.utc)
    except (KeyError, ValueError):
        return None
    return {"expiry": d, "strike": float(strike), "side": side}


def _norm_pdf(x: float) -> float:
    return math.exp(-0.5 * x * x) / math.sqrt(2 * math.pi)


def _bs_gamma(S: float, K: float, T_years: float, sigma: float) -> float:
    if T_years <= 0 or sigma <= 0 or S <= 0 or K <= 0:
        return 0.0
    d1 = (math.log(S / K) + 0.5 * sigma * sigma * T_years) / (sigma * math.sqrt(T_years))
    return _norm_pdf(d1) / (S * sigma * math.sqrt(T_years))


class GexEngine:
    def __init__(self, symbol: str, store: Store, currency: str = "BTC"):
        self.symbol = symbol.upper()
        self.currency = currency.upper()    # Deribit currency: "BTC" / "ETH"
        self.store = store
        self._listeners: list[Callable] = []
        self._running = False
        self._last_snap: dict | None = None

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
                print(f"[gex listener error] {e}")

    # ---------- lifecycle ----------

    async def seed_history(self):
        try:
            await self._poll()
        except Exception as e:
            print(f"[gex] seed failed: {e}")

    async def run(self):
        self._running = True
        backoff = 5
        # If seed_history already fetched, wait one interval before polling again.
        await asyncio.sleep(POLL_INTERVAL_SEC)
        while self._running:
            try:
                await self._poll()
                backoff = 5
                await asyncio.sleep(POLL_INTERVAL_SEC)
            except Exception as e:
                print(f"[gex] poll error, retry in {backoff}s: {e}")
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 120)

    # ---------- core ----------

    async def _poll(self):
        async with httpx.AsyncClient(timeout=20) as c:
            r = await c.get(DERIBIT_URL, params={"currency": self.currency, "kind": "option"})
            r.raise_for_status()
            data = r.json()
        rows = data.get("result", []) or []
        if not rows:
            return

        # Spot from any row that carries underlying_price.
        spot = 0.0
        for row in rows:
            sp = float(row.get("underlying_price") or 0)
            if sp > 0:
                spot = sp
                break
        if spot <= 0:
            return

        now = datetime.now(timezone.utc)
        max_strike = spot * (1 + STRIKE_RANGE_PCT)
        min_strike = spot * (1 - STRIKE_RANGE_PCT)
        max_T = MAX_DAYS_TO_EXPIRY / 365.0

        per_strike: dict[float, dict] = defaultdict(
            lambda: {"call_gex": 0.0, "put_gex": 0.0, "call_oi": 0.0, "put_oi": 0.0}
        )

        for row in rows:
            inst = _parse_instrument(row.get("instrument_name", ""), self.currency)
            if not inst:
                continue
            T = (inst["expiry"] - now).total_seconds() / (365.0 * 86400.0)
            if T <= 0 or T > max_T:
                continue
            strike = inst["strike"]
            if strike < min_strike or strike > max_strike:
                continue
            iv_pct = float(row.get("mark_iv") or 0)
            if iv_pct <= 0:
                continue
            sigma = iv_pct / 100.0
            oi = float(row.get("open_interest") or 0)
            if oi <= 0:
                continue
            gamma = _bs_gamma(spot, strike, T, sigma)
            if gamma <= 0:
                continue
            gex = gamma * oi * spot * spot * CONTRACT_SIZE
            bucket = per_strike[strike]
            if inst["side"] == "C":
                bucket["call_gex"] += gex
                bucket["call_oi"] += oi
            else:
                bucket["put_gex"] += gex
                bucket["put_oi"] += oi

        if not per_strike:
            return

        strikes_sorted = sorted(per_strike.keys())
        out = []
        for k in strikes_sorted:
            b = per_strike[k]
            net = b["call_gex"] - b["put_gex"]
            out.append({
                "strike": k,
                "call_gex": b["call_gex"],
                "put_gex": b["put_gex"],
                "net_gex": net,
                "call_oi": b["call_oi"],
                "put_oi": b["put_oi"],
            })

        total_call = sum(s["call_gex"] for s in out)
        total_put = sum(s["put_gex"] for s in out)
        net_total = total_call - total_put

        # Flip zone: cumulative net GEX from low → high; first zero crossing.
        flip_zone: float | None = None
        cum = 0.0
        prev_cum = 0.0
        prev_strike: float | None = None
        for s in out:
            cum += s["net_gex"]
            if prev_strike is not None and (
                (prev_cum < 0 and cum >= 0) or (prev_cum > 0 and cum <= 0)
            ):
                if cum != prev_cum:
                    frac = -prev_cum / (cum - prev_cum)
                    flip_zone = prev_strike + frac * (s["strike"] - prev_strike)
                else:
                    flip_zone = s["strike"]
                break
            prev_cum = cum
            prev_strike = s["strike"]

        # Regime classification.
        if flip_zone is None or net_total == 0:
            state = "neutral"
        elif spot >= flip_zone and net_total > 0:
            state = "pinning"
        elif spot < flip_zone or net_total < 0:
            state = "explosive"
        else:
            state = "neutral"

        ts_ms = int(time.time() * 1000)
        ts_s = ts_ms // 1000
        snap = {
            "symbol": self.symbol,
            "ts": ts_ms,
            "spot": spot,
            "flip_zone": flip_zone,
            "total_call_gex": total_call,
            "total_put_gex": total_put,
            "net_gex": net_total,
            "state": state,
            "strikes": out,
        }
        self._last_snap = snap
        self.store.upsert_gex(
            self.symbol, ts_s, spot, flip_zone or 0.0,
            total_call, total_put, net_total, state,
        )
        await self._emit("tick", snap)
        flip_str = f"{flip_zone:.0f}" if flip_zone else "—"
        print(f"[gex {self.currency}] spot={spot:.2f} flip={flip_str} state={state} "
              f"net={net_total:.2e} strikes={len(out)}")

    # ---------- snapshot ----------

    def snapshot(self) -> dict | None:
        return self._last_snap
