"""
ICT Confluence Signal Engine.

Takes ICT analysis output (from ict_engine.analyze) plus funding/OI data
and generates actionable, scored signals with plain-English explanations.

Signal types:
  long_setup     — high-confidence long entry (score >= 65)
  short_setup    — high-confidence short entry (score >= 65)
  long_watch     — developing long opportunity (score 45-64)
  short_watch    — developing short opportunity (score 45-64)
  sweep_warning  — liquidity sweep detected, reversal likely

Confluence scoring (max 100):
  +20  Structure bias aligned with signal direction
  +20  Price in discount zone (for long) / premium zone (for short)
  +25  Price entering an active Order Block
       +5  extra if OB has strength >= 80
  +15  OB overlaps with unfilled FVG (breaker zone)
  +20  Recent liquidity sweep in signal direction
       +5  extra if sweep was within last 3 candles (fresh)
  +10  Funding rate contrarian (negative for long, positive for short)
  +5   Equal high/low liquidity cleared (EQH swept → long, EQL swept → short)
  +5   Displacement candle in signal direction within last 5 candles
"""
from __future__ import annotations

import time
from typing import Any

_LONG  = "long"
_SHORT = "short"

# Score thresholds
SETUP_THRESHOLD = 65
WATCH_THRESHOLD = 45


def _f(v: Any) -> float:
    try:
        return float(v or 0)
    except (TypeError, ValueError):
        return 0.0


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def generate_signals(
    ict:      dict,
    funding:  dict | None = None,
    htf_ict:  dict | None = None,
) -> list[dict]:
    """
    Generate ICT-based signals from the analysis snapshot.

    Args:
        ict:      output of ict_engine.analyze() for current timeframe
        funding:  current funding snapshot {funding_rate, oi, oi_change_1h}
        htf_ict:  output of ict_engine.analyze() for the 1h/4h context TF

    Returns:
        List of signal dicts, sorted by confidence descending.
    """
    signals: list[dict] = []
    current = _f(ict.get("current_price"))
    if current <= 0:
        return signals

    structure    = ict.get("structure", {})
    bias         = structure.get("bias", "neutral")
    events       = structure.get("events", [])
    obs          = ict.get("order_blocks", [])
    fvgs         = ict.get("fvgs", [])
    eq_levels    = ict.get("equal_levels", [])
    liquidity    = ict.get("liquidity", {})
    pd           = ict.get("premium_discount", {})
    displacements= ict.get("displacement", [])
    sessions     = ict.get("sessions", {})
    now_ts       = int(ict.get("ts", 0))

    htf_bias = (htf_ict.get("structure", {}).get("bias", "neutral")
                if htf_ict else "neutral")

    funding_rate = _f((funding or {}).get("funding_rate"))
    pd_zone      = pd.get("current_zone", "equilibrium")
    pd_pos       = _f(pd.get("position_pct", 50))

    sweeps       = liquidity.get("sweeps", [])
    recent_sweep_dir: str | None = None
    recent_sweep_age = 9999
    if sweeps:
        last_sweep = sweeps[-1]
        recent_sweep_dir = last_sweep.get("direction")  # "bullish" | "bearish"
        # Age in candles (approximate — we use timestamp delta)
        sweep_ts = int(last_sweep.get("ts", 0))
        if now_ts > 0 and sweep_ts > 0:
            recent_sweep_age = (now_ts - sweep_ts) // 60_000  # minutes

    # Evaluate both long and short
    for direction in (_LONG, _SHORT):
        score, factors = _score(
            direction=direction,
            current=current,
            bias=bias,
            htf_bias=htf_bias,
            events=events,
            obs=obs,
            fvgs=fvgs,
            eq_levels=eq_levels,
            pd_zone=pd_zone,
            pd_pos=pd_pos,
            recent_sweep_dir=recent_sweep_dir,
            recent_sweep_age=recent_sweep_age,
            funding_rate=funding_rate,
            displacements=displacements,
            now_ts=now_ts,
        )

        if score < WATCH_THRESHOLD:
            continue

        sig_type = _classify(direction, score)
        entry_zone, target, stop = _calc_levels(
            direction, current, obs, fvgs, pd, eq_levels
        )
        rr = _calc_rr(direction, current, target, stop)
        reason = _build_reason(direction, bias, htf_bias, factors,
                               pd_zone, recent_sweep_dir)

        # Check for recent sweep warning
        sweep_warning = (
            recent_sweep_dir is not None and recent_sweep_age <= 5
        )

        signals.append({
            "id":           f"sig_{now_ts}_{direction}",
            "type":         sig_type,
            "direction":    direction,
            "confidence":   score,
            "entry_zone":   entry_zone,
            "target":       target,
            "stop":         stop,
            "rr":           rr,
            "factors":      factors,
            "reason":       reason,
            "sweep_warning": sweep_warning,
            "ts":           now_ts,
        })

    # Add raw sweep warning if fresh sweep with no qualified signal
    if (recent_sweep_dir is not None
            and recent_sweep_age <= 3
            and not any(s["direction"] == recent_sweep_dir for s in signals)):
        signals.append({
            "id":           f"sweep_{now_ts}",
            "type":         "sweep_warning",
            "direction":    recent_sweep_dir,
            "confidence":   50,
            "entry_zone":   None,
            "target":       None,
            "stop":         None,
            "rr":           None,
            "factors":      [f"Fresh {recent_sweep_dir} liquidity sweep detected"],
            "reason":       (
                f"A {'buy-side' if recent_sweep_dir == 'bearish' else 'sell-side'} "
                f"liquidity sweep occurred within the last 3 candles. Watch for "
                f"displacement and reversal confirmation."
            ),
            "sweep_warning": True,
            "ts":           now_ts,
        })

    signals.sort(key=lambda x: -x["confidence"])
    return signals[:4]  # return up to 4 signals


# ---------------------------------------------------------------------------
# Internal scoring
# ---------------------------------------------------------------------------

def _score(
    direction:         str,
    current:           float,
    bias:              str,
    htf_bias:          str,
    events:            list,
    obs:               list,
    fvgs:              list,
    eq_levels:         list,
    pd_zone:           str,
    pd_pos:            float,
    recent_sweep_dir:  str | None,
    recent_sweep_age:  int,
    funding_rate:      float,
    displacements:     list,
    now_ts:            int,
) -> tuple[int, list[str]]:
    score   = 0
    factors = []

    # 1. Structure bias alignment
    if bias == direction and htf_bias in (direction, "neutral"):
        score += 20
        factors.append(
            f"{'Bullish' if direction == _LONG else 'Bearish'} structure "
            f"confirmed on current TF"
            + (f" + HTF" if htf_bias == direction else "")
        )
    elif bias == direction:
        score += 12
        factors.append(
            f"{'Bullish' if direction == _LONG else 'Bearish'} structure on current TF "
            f"(HTF neutral)"
        )

    # 2. Premium / Discount positioning
    if direction == _LONG and (pd_zone == "discount" or pd_pos < 35):
        score += 20
        factors.append(f"Price in discount zone ({pd_pos:.0f}% of range — below 35%)")
    elif direction == _SHORT and (pd_zone == "premium" or pd_pos > 65):
        score += 20
        factors.append(f"Price in premium zone ({pd_pos:.0f}% of range — above 65%)")
    elif direction == _LONG and pd_zone == "equilibrium" and pd_pos < 50:
        score += 8
    elif direction == _SHORT and pd_zone == "equilibrium" and pd_pos > 50:
        score += 8

    # 3. Order Block entry
    ob_match = None
    for ob in obs:
        if ob["type"] != ("bullish" if direction == _LONG else "bearish"):
            continue
        top    = _f(ob["top"])
        bottom = _f(ob["bottom"])
        # Price touching or inside OB
        if bottom * 0.999 <= current <= top * 1.001:
            ob_match = ob
            break
        # Price approaching OB (within 0.3%)
        if direction == _LONG and bottom > 0 and 0 < (current - top) / bottom < 0.003:
            ob_match = ob
            break
        if direction == _SHORT and top > 0 and 0 < (bottom - current) / top < 0.003:
            ob_match = ob
            break

    if ob_match:
        base_pts = 25
        if ob_match.get("strength", 0) >= 80:
            base_pts += 5
            factors.append(
                f"High-strength {'bullish' if direction == _LONG else 'bearish'} "
                f"OB at {_fmt(ob_match['bottom'])}-{_fmt(ob_match['top'])} "
                f"(strength {ob_match['strength']})"
            )
        else:
            factors.append(
                f"Price entering {'bullish' if direction == _LONG else 'bearish'} "
                f"OB at {_fmt(ob_match['bottom'])}-{_fmt(ob_match['top'])}"
            )
        score += base_pts

        # FVG overlap (breaker zone)
        for fvg in fvgs:
            if fvg["type"] != ("bullish" if direction == _LONG else "bearish"):
                continue
            ft = _f(fvg["top"]); fb = _f(fvg["bottom"])
            ot = _f(ob_match["top"]); ob_ = _f(ob_match["bottom"])
            overlap = min(ft, ot) - max(fb, ob_)
            if overlap > 0:
                score += 15
                factors.append(
                    f"OB overlaps unfilled FVG "
                    f"({_fmt(fb)}-{_fmt(ft)}) — breaker confluence"
                )
                break

    # 4. Liquidity sweep
    sweep_dir_match = (
        (direction == _LONG  and recent_sweep_dir == "bullish") or
        (direction == _SHORT and recent_sweep_dir == "bearish")
    )
    if sweep_dir_match:
        pts = 20
        age_desc = f"{recent_sweep_age}m ago"
        if recent_sweep_age <= 3:
            pts += 5
            age_desc = "fresh (<= 3 candles)"
        score += pts
        factors.append(
            f"Recent {'sell-side' if direction == _LONG else 'buy-side'} "
            f"liquidity sweep ({age_desc}) — fuel for reversal"
        )

    # 5. Funding rate contrarian signal
    if direction == _LONG and funding_rate < -0.0002:
        score += 10
        factors.append(
            f"Funding rate negative ({funding_rate*100:.4f}%) — "
            f"shorts overloaded, squeeze risk"
        )
    elif direction == _SHORT and funding_rate > 0.0002:
        score += 10
        factors.append(
            f"Funding rate positive ({funding_rate*100:.4f}%) — "
            f"longs overloaded, flush risk"
        )
    elif direction == _LONG and funding_rate < 0:
        score += 4
    elif direction == _SHORT and funding_rate > 0:
        score += 4

    # 6. EQH/EQL liquidity swept
    for eq in eq_levels:
        if direction == _LONG and eq["type"] == "EQL":
            # EQL below current price → sell-side has been tapped
            if _f(eq["price"]) < current * 1.005:
                score += 5
                factors.append(
                    f"Equal lows at {_fmt(eq['price'])} ({eq['count']}x) "
                    f"— sell-side liquidity cleared"
                )
                break
        if direction == _SHORT and eq["type"] == "EQH":
            if _f(eq["price"]) > current * 0.995:
                score += 5
                factors.append(
                    f"Equal highs at {_fmt(eq['price'])} ({eq['count']}x) "
                    f"— buy-side liquidity cleared"
                )
                break

    # 7. Displacement candle
    dir_displace = [d for d in displacements[-5:]
                    if d.get("direction") == ("bullish" if direction == _LONG else "bearish")]
    if dir_displace:
        score += 5
        d = dir_displace[-1]
        factors.append(
            f"Displacement candle detected "
            f"({d['body_pct']:.1f}x avg body, {d['vol_pct']:.1f}x avg vol)"
        )

    return min(score, 100), factors


# ---------------------------------------------------------------------------
# Classification helpers
# ---------------------------------------------------------------------------

def _classify(direction: str, score: int) -> str:
    if score >= SETUP_THRESHOLD:
        return f"{direction}_setup"
    return f"{direction}_watch"


def _calc_levels(
    direction: str,
    current:   float,
    obs:       list,
    fvgs:      list,
    pd:        dict,
    eq_levels: list,
) -> tuple[dict | None, float | None, float | None]:
    """Estimate entry zone, target, and stop for a signal."""
    entry_zone = None
    target     = None
    stop       = None

    # Find the nearest OB for entry zone
    candidate_obs = [ob for ob in obs
                     if ob["type"] == ("bullish" if direction == _LONG else "bearish")]
    if candidate_obs:
        ob = min(candidate_obs, key=lambda x: abs(_f(x["mid"]) - current))
        entry_zone = {"top": _f(ob["top"]), "bottom": _f(ob["bottom"])}
        stop = _f(ob["bottom"]) * 0.998 if direction == _LONG else _f(ob["top"]) * 1.002

    # Target: next liquidity level in signal direction
    if direction == _LONG:
        # Target = nearest buy-side liquidity above current
        eq_highs = sorted(
            [e for e in eq_levels if e["type"] == "EQH" and _f(e["price"]) > current],
            key=lambda x: _f(x["price"])
        )
        range_high = _f(pd.get("range_high", 0))
        candidates = [e["price"] for e in eq_highs] + ([range_high] if range_high > current else [])
        if candidates:
            target = min(candidates, key=lambda x: abs(x - current * 1.01))
    else:
        eq_lows = sorted(
            [e for e in eq_levels if e["type"] == "EQL" and _f(e["price"]) < current],
            key=lambda x: -_f(x["price"])
        )
        range_low = _f(pd.get("range_low", 0))
        candidates = [e["price"] for e in eq_lows] + ([range_low] if 0 < range_low < current else [])
        if candidates:
            target = min(candidates, key=lambda x: abs(x - current * 0.99))

    return entry_zone, target, stop


def _calc_rr(
    direction: str,
    current:   float,
    target:    float | None,
    stop:      float | None,
) -> float | None:
    if target is None or stop is None or current <= 0:
        return None
    if direction == _LONG:
        reward = target - current
        risk   = current - stop
    else:
        reward = current - target
        risk   = stop - current
    if risk <= 0:
        return None
    return round(reward / risk, 2)


def _build_reason(
    direction:        str,
    bias:             str,
    htf_bias:         str,
    factors:          list[str],
    pd_zone:          str,
    recent_sweep_dir: str | None,
) -> str:
    dir_label  = "bullish long" if direction == _LONG else "bearish short"
    bias_label = bias if bias != "neutral" else "no clear"

    intro = (
        f"{'Bullish' if direction == _LONG else 'Bearish'} ICT setup: "
        f"price action shows {bias_label} structure "
        + (f"confirmed on HTF ({htf_bias}). " if htf_bias == direction else ". ")
    )

    pd_desc = (
        f"Price is positioned in the {pd_zone} zone"
        + (", offering a discount entry for longs. " if direction == _LONG and pd_zone == "discount"
           else ", offering a premium entry for shorts. " if direction == _SHORT and pd_zone == "premium"
           else ". ")
    )

    sweep_desc = ""
    if recent_sweep_dir == ("bullish" if direction == _LONG else "bearish"):
        sweep_desc = (
            f"A recent {'sell-side' if direction == _LONG else 'buy-side'} "
            f"liquidity sweep has cleared the opposing liquidity pool, "
            f"setting up a potential reversal. "
        )

    body = " ".join(factors[:3]) + "."
    return intro + pd_desc + sweep_desc + body


def _fmt(price: float) -> str:
    if price >= 10_000:
        return f"{price:,.0f}"
    elif price >= 100:
        return f"{price:,.2f}"
    else:
        return f"{price:,.4f}"
