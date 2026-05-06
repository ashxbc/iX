"""
Pivot-based divergence detection.

A pivot high at index i: candle[i].high is the max of candle[i-L..i+R].
A pivot low  at index i: candle[i].low  is the min of candle[i-L..i+R].

Bearish divergence: price makes a higher high, CVD makes a lower high
                    on two consecutive pivot highs.
Bullish divergence: price makes a lower low, CVD makes a higher low
                    on two consecutive pivot lows.

Pivots are confirmed only after R bars have closed beyond them, which is
the trade-off for accuracy: signal arrives R bars after the actual high/low.
For a leading reversal indicator this is still earlier than most lagging
oscillators because it triggers at the pivot, not on a moving-average cross.
"""
from typing import Optional


def find_pivot_highs(candles: list[dict], left: int, right: int, key: str = "high") -> list[int]:
    pivots = []
    n = len(candles)
    for i in range(left, n - right):
        v = candles[i][key]
        ok = True
        for j in range(i - left, i + right + 1):
            if j == i:
                continue
            if candles[j][key] >= v:
                ok = False
                break
        if ok:
            pivots.append(i)
    return pivots


def find_pivot_lows(candles: list[dict], left: int, right: int, key: str = "low") -> list[int]:
    pivots = []
    n = len(candles)
    for i in range(left, n - right):
        v = candles[i][key]
        ok = True
        for j in range(i - left, i + right + 1):
            if j == i:
                continue
            if candles[j][key] <= v:
                ok = False
                break
        if ok:
            pivots.append(i)
    return pivots


def detect_divergences(candles: list[dict], left: int = 5, right: int = 2) -> list[dict]:
    if len(candles) < left + right + 2:
        return []

    out: list[dict] = []

    price_highs = find_pivot_highs(candles, left, right, "high")
    cvd_highs = find_pivot_highs(candles, left, right, "cvd")
    common_h = sorted(set(price_highs) & set(cvd_highs))
    for a, b in zip(common_h, common_h[1:]):
        if candles[b]["high"] > candles[a]["high"] and candles[b]["cvd"] < candles[a]["cvd"]:
            out.append({
                "type": "bearish",
                "ts": candles[b]["ts"],
                "price": candles[b]["high"],
                "cvd": candles[b]["cvd"],
                "ref_ts": candles[a]["ts"],
            })

    price_lows = find_pivot_lows(candles, left, right, "low")
    cvd_lows = find_pivot_lows(candles, left, right, "cvd")
    common_l = sorted(set(price_lows) & set(cvd_lows))
    for a, b in zip(common_l, common_l[1:]):
        if candles[b]["low"] < candles[a]["low"] and candles[b]["cvd"] > candles[a]["cvd"]:
            out.append({
                "type": "bullish",
                "ts": candles[b]["ts"],
                "price": candles[b]["low"],
                "cvd": candles[b]["cvd"],
                "ref_ts": candles[a]["ts"],
            })

    return out
