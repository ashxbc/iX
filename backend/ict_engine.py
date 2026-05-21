"""
ICT (Inner Circle Trading) / Smart Money Concepts analysis engine.

Pure functional — no async, no side effects. Takes a list of closed
candle dicts and returns a structured ICT analysis snapshot.

Concepts implemented:
  - Swing High / Swing Low detection (configurable lookback)
  - Market Structure: BOS (Break of Structure) / CHoCH (Change of Character)
  - Order Blocks (bullish/bearish) with strength scoring
  - Fair Value Gaps (FVG) — imbalance zones
  - Equal Highs / Equal Lows (EQH / EQL) — liquidity clusters
  - Buy-Side / Sell-Side Liquidity levels
  - Liquidity Sweeps (wick-through + close-back)
  - Premium / Discount zones (relative to the current swing range)
  - Displacement candles (institutional impulse moves)
  - Session-based levels (Asia, London, NY, Previous Day)

All functions operate on a list of dicts with keys:
  ts, open, high, low, close, volume, buy_vol, sell_vol
"""
from __future__ import annotations

import math
import time
from typing import Any

# ---------------------------------------------------------------------------
# Constants / tuning
# ---------------------------------------------------------------------------

SWING_LOOKBACK   = 5      # candles each side for swing confirmation
EQ_TOLERANCE_PCT = 0.0012 # 0.12% — price levels considered "equal"
MIN_FVG_PCT      = 0.0004 # 0.04% minimum gap to qualify as an FVG
MAX_OBS          = 6      # max active OBs per side returned to frontend
MAX_FVGS         = 6      # max active FVGs per side
MAX_SWINGS       = 20     # max swing points returned
MAX_EVENTS       = 30     # max structure events returned
MAX_SWEEPS       = 10     # max sweep events returned

# Session boundaries in UTC hour (0-23)
SESSIONS = {
    "asia":   (0,  8),
    "london": (7,  13),
    "ny":     (12, 21),
}


# ---------------------------------------------------------------------------
# Helper: safe float
# ---------------------------------------------------------------------------

def _f(v: Any) -> float:
    try:
        return float(v or 0)
    except (TypeError, ValueError):
        return 0.0


# ---------------------------------------------------------------------------
# Step 1: Swing detection
# ---------------------------------------------------------------------------

def find_swings(candles: list[dict], lookback: int = SWING_LOOKBACK) -> list[dict]:
    """
    Return confirmed swing highs and lows. A swing is confirmed only when
    `lookback` candles after it have closed — so the last `lookback` candles
    are never labelled swings. This avoids phantom swings on live data.
    """
    n      = len(candles)
    swings = []
    limit  = n - lookback

    for i in range(lookback, limit):
        c    = candles[i]
        high = _f(c["high"])
        low  = _f(c["low"])

        is_sh = all(
            high >= _f(candles[j]["high"])
            for j in range(max(0, i - lookback), min(n, i + lookback + 1))
            if j != i
        )
        is_sl = all(
            low <= _f(candles[j]["low"])
            for j in range(max(0, i - lookback), min(n, i + lookback + 1))
            if j != i
        )

        if is_sh:
            swings.append({
                "type":  "high",
                "price": high,
                "ts":    int(c["ts"]),
                "idx":   i,
                "broken": False,
            })
        if is_sl:
            swings.append({
                "type":  "low",
                "price": low,
                "ts":    int(c["ts"]),
                "idx":   i,
                "broken": False,
            })

    swings.sort(key=lambda x: x["idx"])
    return swings


# ---------------------------------------------------------------------------
# Step 2: Market structure (BOS / CHoCH)
# ---------------------------------------------------------------------------

def analyze_structure(candles: list[dict],
                      swings:  list[dict]) -> dict:
    """
    Walk through candles and detect structure breaks.

    BOS  = continuation in the direction of existing bias.
    CHoCH = break against the existing bias (first sign of reversal).

    Returns:
      bias   : "bullish" | "bearish" | "neutral"
      events : list of structure events (most recent MAX_EVENTS)
      swings : active (unbroken) swing highs and lows
    """
    events: list[dict] = []
    bias:   str | None = None

    # We'll track the "current" SH/SL to watch. Each time it gets broken we
    # advance to the next confirmed swing of that type.
    last_sh: dict | None = None
    last_sl: dict | None = None
    swing_ptr = 0                      # pointer into sorted swings list

    n = len(candles)
    for i in range(n):
        c     = candles[i]
        close = _f(c["close"])

        # Admit newly confirmed swings (idx + lookback <= i)
        while swing_ptr < len(swings):
            s = swings[swing_ptr]
            if s["idx"] + SWING_LOOKBACK <= i:
                if s["type"] == "high":
                    last_sh = s
                else:
                    last_sl = s
                swing_ptr += 1
            else:
                break

        if last_sh is None or last_sl is None:
            continue

        # Check bullish break (close above last SH)
        if not last_sh["broken"] and close > last_sh["price"]:
            etype = "CHoCH" if bias == "bearish" else "BOS"
            events.append({
                "type":      etype,
                "direction": "bullish",
                "ts":        int(c["ts"]),
                "price":     last_sh["price"],
                "candle_idx": i,
            })
            last_sh["broken"] = True
            bias = "bullish"

        # Check bearish break (close below last SL)
        if not last_sl["broken"] and close < last_sl["price"]:
            etype = "CHoCH" if bias == "bullish" else "BOS"
            events.append({
                "type":      etype,
                "direction": "bearish",
                "ts":        int(c["ts"]),
                "price":     last_sl["price"],
                "candle_idx": i,
            })
            last_sl["broken"] = True
            bias = "bearish"

    # Active (unbroken) swings for display
    active_highs = [s for s in swings if s["type"] == "high" and not s["broken"]]
    active_lows  = [s for s in swings if s["type"] == "low"  and not s["broken"]]

    return {
        "bias":   bias or "neutral",
        "events": events[-MAX_EVENTS:],
        "swings": {
            "highs": [{"ts": s["ts"], "price": s["price"]} for s in active_highs[-MAX_SWINGS // 2:]],
            "lows":  [{"ts": s["ts"], "price": s["price"]} for s in active_lows[-MAX_SWINGS // 2:]],
        },
    }


# ---------------------------------------------------------------------------
# Step 3: Order Blocks
# ---------------------------------------------------------------------------

def _avg_vol(candles: list[dict], idx: int, n: int = 10) -> float:
    start = max(0, idx - n)
    vols  = [_f(candles[j]["volume"]) for j in range(start, idx)]
    return sum(vols) / len(vols) if vols else 1.0


def find_order_blocks(candles: list[dict],
                      structure_events: list[dict]) -> list[dict]:
    """
    For each BOS/CHoCH event, trace back to find the origin order block.

    Bullish OB: last bearish candle (close < open) before the bullish impulse
                that caused the structure break.
    Bearish OB: last bullish candle (close > open) before the bearish impulse.

    Strength (0-100):
      +30  departure speed (< 4 candles to reach BOS level)
      +25  departure distance (> 1.5 % move from OB to BOS level)
      +20  volume expansion (OB bar or impulse bar > 1.5x avg)
      +15  FVG created within the impulse
      +10  origin after a sweep (liquidity sweep → displacement)
    """
    obs: list[dict] = []
    seen_ts: set[int] = set()

    for evt in structure_events:
        direction = evt["direction"]
        bos_idx   = evt["candle_idx"]
        bos_price = evt["price"]

        ob_candle = None
        ob_idx    = None

        # Walk backwards from the BOS candle to find the last opposite candle
        for j in range(bos_idx - 1, max(-1, bos_idx - 40), -1):
            c     = candles[j]
            o, cl = _f(c["open"]), _f(c["close"])
            if direction == "bullish" and cl < o:   # last bearish candle
                ob_candle = c
                ob_idx    = j
                break
            if direction == "bearish" and cl > o:   # last bullish candle
                ob_candle = c
                ob_idx    = j
                break

        if ob_candle is None or ob_idx is None:
            continue

        ob_ts = int(ob_candle["ts"])
        if ob_ts in seen_ts:
            continue
        seen_ts.add(ob_ts)

        ob_high = _f(ob_candle["high"])
        ob_low  = _f(ob_candle["low"])
        ob_mid  = (ob_high + ob_low) / 2

        # Strength scoring
        score = 0

        # Departure speed
        span = bos_idx - ob_idx
        if span <= 3:
            score += 30
        elif span <= 6:
            score += 18
        elif span <= 10:
            score += 10

        # Departure distance
        ref = ob_high if direction == "bullish" else ob_low
        if ref > 0:
            dist_pct = abs(bos_price - ref) / ref
            if dist_pct > 0.03:
                score += 25
            elif dist_pct > 0.015:
                score += 18
            elif dist_pct > 0.008:
                score += 10

        # Volume expansion
        avg_v = _avg_vol(candles, ob_idx, 10)
        ob_v  = _f(ob_candle["volume"])
        if ob_v > avg_v * 2.0:
            score += 20
        elif ob_v > avg_v * 1.5:
            score += 12

        # FVG within impulse
        for k in range(ob_idx, min(bos_idx, len(candles) - 1)):
            if k + 2 < len(candles):
                prev_h = _f(candles[k]["high"])
                next_l = _f(candles[k + 2]["low"])
                prev_l = _f(candles[k]["low"])
                next_h = _f(candles[k + 2]["high"])
                if direction == "bullish" and prev_h < next_l:
                    score += 15
                    break
                if direction == "bearish" and prev_l > next_h:
                    score += 15
                    break

        score = min(score, 100)

        obs.append({
            "id":              f"ob_{ob_ts}_{direction[0]}",
            "type":            direction,          # "bullish" | "bearish"
            "ts":              ob_ts,
            "top":             ob_high,
            "bottom":          ob_low,
            "mid":             ob_mid,
            "strength":        score,
            "mitigated":       False,
            "mitigation_count": 0,
            "bos_ts":          evt["ts"],
        })

    # De-duplicate by proximity (same side + within 0.1% of each other)
    obs = _dedup_zones(obs)

    # Check mitigation (price returned to OB zone)
    last_close = _f(candles[-1]["close"]) if candles else 0.0
    for ob in obs:
        if _f(ob["bottom"]) <= last_close <= _f(ob["top"]):
            ob["mitigated"]        = True
            ob["mitigation_count"] = 1

    # Split by type, keep strongest unmitigated, limit
    bull_obs = sorted([o for o in obs if o["type"] == "bullish"],
                      key=lambda x: (-x["strength"], -x["ts"]))[:MAX_OBS]
    bear_obs = sorted([o for o in obs if o["type"] == "bearish"],
                      key=lambda x: (-x["strength"], -x["ts"]))[:MAX_OBS]

    return bull_obs + bear_obs


def _dedup_zones(zones: list[dict]) -> list[dict]:
    """Remove duplicate zones that overlap within EQ_TOLERANCE_PCT."""
    out: list[dict] = []
    for z in zones:
        mid  = z["mid"]
        dup  = False
        for existing in out:
            if existing["type"] != z["type"]:
                continue
            if existing["mid"] > 0 and abs(mid - existing["mid"]) / existing["mid"] < EQ_TOLERANCE_PCT * 2:
                # Keep the stronger one
                if z["strength"] > existing["strength"]:
                    existing.update(z)
                dup = True
                break
        if not dup:
            out.append(z)
    return out


# ---------------------------------------------------------------------------
# Step 4: Fair Value Gaps (FVG)
# ---------------------------------------------------------------------------

def find_fvgs(candles: list[dict]) -> list[dict]:
    """
    Three-candle imbalance pattern.

    Bullish FVG: candle[i-1].high < candle[i+1].low  (gap up, buy-side imbalance)
    Bearish FVG: candle[i-1].low  > candle[i+1].high (gap down, sell-side imbalance)

    Only gaps >= MIN_FVG_PCT qualify.
    Tracks fill percentage based on subsequent price action.
    """
    fvgs: list[dict] = []
    n = len(candles)

    for i in range(1, n - 1):
        prev = candles[i - 1]
        curr = candles[i]
        nxt  = candles[i + 1]

        ph = _f(prev["high"]);  pl = _f(prev["low"])
        nh = _f(nxt["high"]);   nl = _f(nxt["low"])

        # Bullish FVG
        if ph < nl and ph > 0:
            gap_pct = (nl - ph) / ph
            if gap_pct >= MIN_FVG_PCT:
                fvgs.append({
                    "id":          f"fvg_{int(curr['ts'])}_b",
                    "type":        "bullish",
                    "ts":          int(curr["ts"]),
                    "top":         nl,
                    "bottom":      ph,
                    "mid":         (nl + ph) / 2,
                    "gap_pct":     gap_pct,
                    "filled_pct":  0.0,
                    "active":      True,
                })

        # Bearish FVG
        if pl > nh and pl > 0:
            gap_pct = (pl - nh) / pl
            if gap_pct >= MIN_FVG_PCT:
                fvgs.append({
                    "id":          f"fvg_{int(curr['ts'])}_s",
                    "type":        "bearish",
                    "ts":          int(curr["ts"]),
                    "top":         pl,
                    "bottom":      nh,
                    "mid":         (pl + nh) / 2,
                    "gap_pct":     gap_pct,
                    "filled_pct":  0.0,
                    "active":      True,
                })

    # Update fill status: check if subsequent candles have traded into each FVG
    fvg_lookup: dict[int, dict] = {i: fvgs[i] for i in range(len(fvgs))}
    for i in range(n):
        c  = candles[i]
        ch = _f(c["high"]); cl = _f(c["low"])
        for j, fvg in fvg_lookup.items():
            fvg_ts_idx = next(
                (k for k in range(n) if int(candles[k]["ts"]) == fvg["ts"]), None
            )
            if fvg_ts_idx is None or i <= fvg_ts_idx + 1:
                continue
            top    = fvg["top"]
            bottom = fvg["bottom"]
            height = top - bottom
            if height <= 0:
                continue
            if fvg["type"] == "bullish":
                fill = max(0.0, min(1.0, (top - cl) / height))
                fvg["filled_pct"] = max(fvg["filled_pct"], fill)
            else:
                fill = max(0.0, min(1.0, (ch - bottom) / height))
                fvg["filled_pct"] = max(fvg["filled_pct"], fill)
            if fvg["filled_pct"] >= 0.95:
                fvg["active"] = False

    # Keep only active, limit per side
    active = [f for f in fvgs if f["active"]]
    bull   = sorted([f for f in active if f["type"] == "bullish"],
                    key=lambda x: -x["ts"])[:MAX_FVGS]
    bear   = sorted([f for f in active if f["type"] == "bearish"],
                    key=lambda x: -x["ts"])[:MAX_FVGS]
    return bull + bear


# ---------------------------------------------------------------------------
# Step 5: Equal Highs / Equal Lows
# ---------------------------------------------------------------------------

def find_equal_levels(swings: list[dict]) -> list[dict]:
    """
    Identify EQH (Equal Highs) and EQL (Equal Lows).
    Two or more swing points within EQ_TOLERANCE_PCT of each other.
    These are liquidity clusters — smart money targets.
    """
    highs = [s for s in swings if s["type"] == "high"]
    lows  = [s for s in swings if s["type"] == "low"]
    result: list[dict] = []

    def _cluster(pts: list[dict], kind: str):
        used = [False] * len(pts)
        for i in range(len(pts)):
            if used[i]:
                continue
            cluster = [pts[i]]
            for j in range(i + 1, len(pts)):
                if used[j]:
                    continue
                ref = cluster[0]["price"]
                if ref > 0 and abs(pts[j]["price"] - ref) / ref <= EQ_TOLERANCE_PCT:
                    cluster.append(pts[j])
                    used[j] = True
            if len(cluster) >= 2:
                avg_price = sum(p["price"] for p in cluster) / len(cluster)
                result.append({
                    "type":    kind,
                    "price":   avg_price,
                    "count":   len(cluster),
                    "ts_list": [p["ts"] for p in cluster],
                    "ts":      cluster[-1]["ts"],
                })
            used[i] = True

    _cluster(highs, "EQH")
    _cluster(lows,  "EQL")
    return result


# ---------------------------------------------------------------------------
# Step 6: Liquidity levels and sweeps
# ---------------------------------------------------------------------------

def find_liquidity(candles: list[dict],
                   swings:  list[dict],
                   eq_levels: list[dict]) -> dict:
    """
    Build buy-side and sell-side liquidity levels.
    Detect sweeps: wick through level + close on the opposite side.

    Buy-side liquidity (BSL): above swing highs, EQH lines.
    Sell-side liquidity (SSL): below swing lows, EQL lines.

    Sweep: wick penetrates the level but candle closes back inside.
    """
    if not candles:
        return {"buy_side": [], "sell_side": [], "sweeps": []}

    current_price = _f(candles[-1]["close"])

    # Build level lists
    buy_side: list[dict]  = []
    sell_side: list[dict] = []

    for s in swings:
        if not s.get("broken"):
            if s["type"] == "high":
                buy_side.append({
                    "price":    s["price"],
                    "ts":       s["ts"],
                    "strength": 60,
                    "kind":     "swing_high",
                })
            else:
                sell_side.append({
                    "price":    s["price"],
                    "ts":       s["ts"],
                    "strength": 60,
                    "kind":     "swing_low",
                })

    for eq in eq_levels:
        strength = min(40 + eq["count"] * 15, 95)
        entry = {"price": eq["price"], "ts": eq["ts"],
                 "strength": strength, "kind": eq["type"]}
        if eq["type"] == "EQH":
            buy_side.append(entry)
        else:
            sell_side.append(entry)

    # Sort relative to current price
    buy_side  = sorted([l for l in buy_side  if l["price"] >= current_price],
                       key=lambda x: x["price"])[:10]
    sell_side = sorted([l for l in sell_side if l["price"] <= current_price],
                       key=lambda x: -x["price"])[:10]

    # Sweep detection
    sweeps: list[dict] = []
    n = len(candles)
    all_levels = buy_side + sell_side

    for i in range(1, n):
        c  = candles[i]
        ch = _f(c["high"]); cl = _f(c["low"]); cc = _f(c["close"])
        body_h = max(_f(c["open"]), cc)
        body_l = min(_f(c["open"]), cc)

        for lvl in all_levels:
            lp = lvl["price"]
            if lvl in buy_side:
                # BSL sweep: wick above level, close below level
                if ch > lp > cc:
                    wick = ch - lp
                    body = abs(_f(c["close"]) - _f(c["open"]))
                    if body > 0 and wick / body > 0.3:
                        sweeps.append({
                            "type":      "bsl_sweep",
                            "ts":        int(c["ts"]),
                            "price":     lp,
                            "wick_size": wick,
                            "direction": "bearish",
                        })
            else:
                # SSL sweep: wick below level, close above level
                if cl < lp < cc:
                    wick = lp - cl
                    body = abs(_f(c["close"]) - _f(c["open"]))
                    if body > 0 and wick / body > 0.3:
                        sweeps.append({
                            "type":      "ssl_sweep",
                            "ts":        int(c["ts"]),
                            "price":     lp,
                            "wick_size": wick,
                            "direction": "bullish",
                        })

    # Deduplicate sweeps (same ts + same direction → keep first)
    seen_sweep: set[tuple] = set()
    unique_sweeps: list[dict] = []
    for sw in sweeps:
        key = (sw["ts"], sw["direction"])
        if key not in seen_sweep:
            seen_sweep.add(key)
            unique_sweeps.append(sw)

    return {
        "buy_side":  buy_side,
        "sell_side": sell_side,
        "sweeps":    unique_sweeps[-MAX_SWEEPS:],
    }


# ---------------------------------------------------------------------------
# Step 7: Premium / Discount zones
# ---------------------------------------------------------------------------

def premium_discount(candles: list[dict],
                     swings:  list[dict]) -> dict:
    """
    ICT premium/discount based on the most recent completed swing range.

    Range = last confirmed swing high to last confirmed swing low (or vice versa).
    Premium  : price > 62% of range (institutional sell zone)
    Discount : price < 38% of range (institutional buy zone)
    Equilibrium: 38%-62%
    """
    if not candles or not swings:
        return {}

    current = _f(candles[-1]["close"])
    highs   = sorted([s for s in swings if s["type"] == "high"],
                     key=lambda x: x["idx"])
    lows    = sorted([s for s in swings if s["type"] == "low"],
                     key=lambda x: x["idx"])

    if not highs or not lows:
        return {}

    range_high = highs[-1]["price"]
    range_low  = lows[-1]["price"]

    # Ensure high > low
    if range_high <= range_low:
        range_high, range_low = max(range_high, range_low), min(range_high, range_low)
    if range_high == range_low:
        return {}

    span = range_high - range_low
    pos  = (current - range_low) / span  # 0 = at low, 1 = at high

    if pos >= 0.618:
        zone = "premium"
    elif pos <= 0.382:
        zone = "discount"
    else:
        zone = "equilibrium"

    return {
        "range_high":    range_high,
        "range_low":     range_low,
        "equilibrium":   range_low + span * 0.5,
        "fib_618":       range_low + span * 0.618,
        "fib_382":       range_low + span * 0.382,
        "current_zone":  zone,
        "position_pct":  round(pos * 100, 1),
    }


# ---------------------------------------------------------------------------
# Step 8: Displacement candles
# ---------------------------------------------------------------------------

def find_displacement(candles: list[dict]) -> list[dict]:
    """
    Displacement = an unusually large, one-directional candle with volume
    expansion — the institutional 'hand of God' move.

    Criteria:
      - Body size >= 2.5x average body size of prior 10 candles
      - Volume >= 1.5x average volume of prior 10 candles
      - Wick ratio <= 40% of total range (clean move, not choppy)
    """
    result: list[dict] = []
    n = len(candles)
    lookback = 10

    for i in range(lookback, n):
        c = candles[i]
        o = _f(c["open"]); h = _f(c["high"])
        l = _f(c["low"]);  cl = _f(c["close"])

        body  = abs(cl - o)
        total = h - l
        if total <= 0:
            continue

        # Avg body + vol of prior N candles
        prior = candles[max(0, i - lookback):i]
        avg_body = sum(abs(_f(p["close"]) - _f(p["open"])) for p in prior) / len(prior)
        avg_vol  = sum(_f(p["volume"]) for p in prior) / len(prior)

        if avg_body <= 0 or avg_vol <= 0:
            continue

        vol = _f(c["volume"])
        wick_ratio = (total - body) / total

        if (body >= avg_body * 2.5
                and vol >= avg_vol * 1.5
                and wick_ratio <= 0.40):
            direction = "bullish" if cl > o else "bearish"
            result.append({
                "ts":        int(c["ts"]),
                "direction": direction,
                "body_pct":  round(body / avg_body, 2),
                "vol_pct":   round(vol / avg_vol, 2),
                "open":      o,
                "close":     cl,
                "high":      h,
                "low":       l,
            })

    return result[-10:]


# ---------------------------------------------------------------------------
# Step 9: Session levels
# ---------------------------------------------------------------------------

def session_levels(candles: list[dict]) -> dict:
    """
    Compute high/low for Asia, London, NY sessions and the previous day.
    Uses UTC timestamps.
    """
    if not candles:
        return {}

    now_s   = time.time()
    day_s   = 86400
    today   = int(now_s // day_s) * day_s * 1000   # ms
    yesterday = today - day_s * 1000

    def _bounds(session: str) -> tuple[int, int]:
        start_h, end_h = SESSIONS[session]
        return (
            today + start_h * 3600 * 1000,
            today + end_h   * 3600 * 1000,
        )

    result: dict = {}
    for sess in ("asia", "london", "ny"):
        s_start, s_end = _bounds(sess)
        subset = [c for c in candles
                  if s_start <= int(c["ts"]) < s_end]
        if subset:
            result[sess] = {
                "high": max(_f(c["high"])  for c in subset),
                "low":  min(_f(c["low"])   for c in subset),
            }

    # Previous day high/low
    prev_subset = [c for c in candles
                   if yesterday <= int(c["ts"]) < today]
    if prev_subset:
        result["prev_day"] = {
            "high":  max(_f(c["high"])  for c in prev_subset),
            "low":   min(_f(c["low"])   for c in prev_subset),
            "close": _f(prev_subset[-1]["close"]),
        }

    return result


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def analyze(candles: list[dict],
            tf: str = "5m") -> dict:
    """
    Full ICT analysis on a list of closed candle dicts.

    Args:
        candles: list of candle dicts (sorted oldest→newest, all closed)
        tf:      timeframe label (informational only, e.g. "5m")

    Returns:
        Complete ICT snapshot dict suitable for JSON serialisation.
    """
    if not candles:
        return _empty()

    # Work on closed candles only (exclude the last, which may be live)
    closed = candles[:-1] if len(candles) > 1 else candles

    swings    = find_swings(closed, SWING_LOOKBACK)
    structure = analyze_structure(closed, swings)

    # Restore swing dicts to unbroken state for downstream use
    # (analyze_structure mutates broken flags — re-find for cleanliness)
    swings_clean = find_swings(closed, SWING_LOOKBACK)

    obs        = find_order_blocks(closed, structure["events"])
    fvgs       = find_fvgs(closed)
    eq_levels  = find_equal_levels(swings_clean)
    liquidity  = find_liquidity(closed, swings_clean, eq_levels)
    pd_zones   = premium_discount(closed, swings_clean)
    displace   = find_displacement(closed)
    sessions   = session_levels(closed)

    return {
        "tf":               tf,
        "ts":               int(candles[-1]["ts"]),
        "current_price":    _f(candles[-1]["close"]),
        "structure":        structure,
        "order_blocks":     obs,
        "fvgs":             fvgs,
        "equal_levels":     eq_levels,
        "liquidity":        liquidity,
        "premium_discount": pd_zones,
        "displacement":     displace,
        "sessions":         sessions,
    }


def _empty() -> dict:
    return {
        "tf": "", "ts": 0, "current_price": 0,
        "structure":        {"bias": "neutral", "events": [], "swings": {"highs": [], "lows": []}},
        "order_blocks":     [],
        "fvgs":             [],
        "equal_levels":     [],
        "liquidity":        {"buy_side": [], "sell_side": [], "sweeps": []},
        "premium_discount": {},
        "displacement":     [],
        "sessions":         {},
    }
