"""
Recover true CVD for offline gaps by replaying aggTrades from Binance.

Detects time gaps between observed candles in the DB, fetches every
trade in those gaps with true buy/sell aggressor classification, rebuilds
the candles, and shifts subsequent CVDs so the cumulative line is correct
end-to-end.

Usage:
    python recover.py                 # all (symbol, timeframe) combos
    python recover.py BTCUSDT 5m      # one combo
"""
import asyncio
import sys

import httpx

from cvd_engine import BINANCE_REST, TIMEFRAME_MS
from storage import Store


SYMBOLS = ["BTCUSDT", "ETHUSDT", "SOLUSDT"]
TIMEFRAMES = list(TIMEFRAME_MS.keys())
WINDOW_MS = 3_500_000  # under 1 hour, the API limit for aggTrades by time


async def fetch_aggtrades(client: httpx.AsyncClient, symbol: str,
                          start_ms: int, end_ms: int) -> list[dict]:
    """Paginated aggTrades fetch covering [start_ms, end_ms]."""
    out: list[dict] = []
    cursor = start_ms
    while cursor <= end_ms:
        win_end = min(cursor + WINDOW_MS, end_ms)
        r = await client.get(f"{BINANCE_REST}/api/v3/aggTrades", params={
            "symbol": symbol, "startTime": cursor, "endTime": win_end, "limit": 1000,
        })
        r.raise_for_status()
        batch = r.json()
        if not batch:
            cursor = win_end + 1
            continue
        out.extend(batch)
        last_id = batch[-1]["a"]
        # If we filled the limit, paginate by fromId until we leave the window
        while len(batch) == 1000:
            r = await client.get(f"{BINANCE_REST}/api/v3/aggTrades", params={
                "symbol": symbol, "fromId": last_id + 1, "limit": 1000,
            })
            r.raise_for_status()
            batch = r.json()
            if not batch:
                break
            keep = [b for b in batch if b["T"] <= win_end]
            out.extend(keep)
            if len(keep) < len(batch):
                break
            last_id = batch[-1]["a"]
        cursor = win_end + 1
    return out


def bucket_trades(trades: list[dict], tf_ms: int) -> dict[int, dict]:
    candles: dict[int, dict] = {}
    for t in sorted(trades, key=lambda x: x["T"]):
        ts = t["T"]
        bucket = (ts // tf_ms) * tf_ms
        price = float(t["p"])
        qty = float(t["q"])
        is_sell = bool(t["m"])
        c = candles.get(bucket)
        if c is None:
            c = {"ts": bucket, "open": price, "high": price, "low": price, "close": price,
                 "volume": 0.0, "buy_vol": 0.0, "sell_vol": 0.0, "delta": 0.0}
            candles[bucket] = c
        if price > c["high"]: c["high"] = price
        if price < c["low"]:  c["low"] = price
        c["close"] = price
        c["volume"] += qty
        if is_sell:
            c["sell_vol"] += qty
            c["delta"] -= qty
        else:
            c["buy_vol"] += qty
            c["delta"] += qty
    return candles


async def recover_one(store: Store, symbol: str, timeframe: str):
    rows = store.load_recent(symbol, timeframe, 100_000)
    if not rows:
        print(f"  {symbol} {timeframe}: no observed candles, skipping")
        return

    tf_ms = TIMEFRAME_MS[timeframe]
    gaps = []
    for i in range(1, len(rows)):
        if rows[i]["ts"] - rows[i - 1]["ts"] > tf_ms:
            gaps.append((rows[i - 1], rows[i]))

    if not gaps:
        print(f"  {symbol} {timeframe}: no gaps")
        return

    print(f"  {symbol} {timeframe}: recovering {len(gaps)} gap(s)")
    cum_shift = 0.0
    async with httpx.AsyncClient(timeout=30) as client:
        for prev, cur in gaps:
            base = prev["cvd"] + cum_shift
            gap_start = prev["ts"] + tf_ms
            gap_end = cur["ts"] - 1
            mins = (gap_end - gap_start + 1) // 60_000
            print(f"    gap {gap_start}..{gap_end} ({mins} min)", end=" ", flush=True)
            try:
                trades = await fetch_aggtrades(client, symbol, gap_start, gap_end)
            except Exception as e:
                print(f"FAILED: {e}")
                continue
            buckets = bucket_trades(trades, tf_ms)
            cvd = base
            for ts in sorted(buckets):
                c = buckets[ts]
                cvd += c["delta"]
                c["cvd"] = cvd
                store.upsert(symbol, timeframe, c)
            net = cvd - base
            store.shift_cvd_after(symbol, timeframe, prev["ts"], net)
            cum_shift += net
            print(f"-> {len(trades)} trades, {len(buckets)} candles, net delta {net:+.4f}")


async def main():
    store = Store()
    args = sys.argv[1:]
    if args:
        if len(args) != 2:
            print("usage: python recover.py [SYMBOL TIMEFRAME]")
            return
        await recover_one(store, args[0].upper(), args[1])
    else:
        for sym in SYMBOLS:
            for tf in TIMEFRAMES:
                await recover_one(store, sym, tf)


if __name__ == "__main__":
    asyncio.run(main())
