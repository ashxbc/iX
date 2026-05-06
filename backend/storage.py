"""
SQLite persistence for candles. Stores every observed candle
(open/high/low/close/volume + true buy/sell/delta/cvd).

WAL mode + a single shared connection guarded by a thread lock so the
async event loop can write through it without contention.
"""
import sqlite3
import threading
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent / "data.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS candles (
    symbol TEXT NOT NULL,
    timeframe TEXT NOT NULL,
    ts INTEGER NOT NULL,
    open REAL NOT NULL,
    high REAL NOT NULL,
    low REAL NOT NULL,
    close REAL NOT NULL,
    volume REAL NOT NULL,
    buy_vol REAL NOT NULL,
    sell_vol REAL NOT NULL,
    delta REAL NOT NULL,
    cvd REAL NOT NULL,
    PRIMARY KEY (symbol, timeframe, ts)
);
CREATE INDEX IF NOT EXISTS idx_candles_st ON candles(symbol, timeframe, ts DESC);
"""


class Store:
    def __init__(self, path: Path = DB_PATH):
        self.path = path
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(path), check_same_thread=False, timeout=10)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.executescript(SCHEMA)
        self._conn.commit()

    def upsert(self, symbol: str, timeframe: str, c: dict):
        with self._lock:
            self._conn.execute(
                """INSERT INTO candles
                (symbol, timeframe, ts, open, high, low, close, volume, buy_vol, sell_vol, delta, cvd)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(symbol, timeframe, ts) DO UPDATE SET
                    open=excluded.open, high=excluded.high, low=excluded.low, close=excluded.close,
                    volume=excluded.volume, buy_vol=excluded.buy_vol, sell_vol=excluded.sell_vol,
                    delta=excluded.delta, cvd=excluded.cvd""",
                (symbol, timeframe, c["ts"], c["open"], c["high"], c["low"], c["close"],
                 c["volume"], c["buy_vol"], c["sell_vol"], c["delta"], c["cvd"]),
            )
            self._conn.commit()

    def load_recent(self, symbol: str, timeframe: str, limit: int = 500) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                """SELECT ts, open, high, low, close, volume, buy_vol, sell_vol, delta, cvd
                FROM candles WHERE symbol=? AND timeframe=?
                ORDER BY ts DESC LIMIT ?""",
                (symbol, timeframe, limit),
            ).fetchall()
        rows.reverse()
        return [
            {"ts": r[0], "open": r[1], "high": r[2], "low": r[3], "close": r[4],
             "volume": r[5], "buy_vol": r[6], "sell_vol": r[7], "delta": r[8],
             "cvd": r[9], "observed": True}
            for r in rows
        ]

    def shift_cvd_after(self, symbol: str, timeframe: str, after_ts: int, delta: float):
        """Add `delta` to cvd of every candle with ts > after_ts."""
        with self._lock:
            self._conn.execute(
                "UPDATE candles SET cvd = cvd + ? WHERE symbol=? AND timeframe=? AND ts > ?",
                (delta, symbol, timeframe, after_ts),
            )
            self._conn.commit()

    def last_ts(self, symbol: str, timeframe: str) -> int | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT MAX(ts) FROM candles WHERE symbol=? AND timeframe=?",
                (symbol, timeframe),
            ).fetchone()
        return row[0] if row and row[0] is not None else None
