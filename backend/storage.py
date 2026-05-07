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

CREATE TABLE IF NOT EXISTS funding (
    symbol TEXT NOT NULL,
    ts INTEGER NOT NULL,
    rate REAL NOT NULL,
    PRIMARY KEY (symbol, ts)
);
CREATE INDEX IF NOT EXISTS idx_funding_s ON funding(symbol, ts DESC);

CREATE TABLE IF NOT EXISTS open_interest (
    symbol TEXT NOT NULL,
    ts INTEGER NOT NULL,
    oi REAL NOT NULL,
    oi_value REAL NOT NULL,
    PRIMARY KEY (symbol, ts)
);
CREATE INDEX IF NOT EXISTS idx_oi_s ON open_interest(symbol, ts DESC);

CREATE TABLE IF NOT EXISTS liquidations (
    symbol TEXT NOT NULL,
    ts INTEGER NOT NULL,
    long_usd REAL NOT NULL,
    short_usd REAL NOT NULL,
    low REAL NOT NULL,
    high REAL NOT NULL,
    PRIMARY KEY (symbol, ts)
);
CREATE INDEX IF NOT EXISTS idx_liq_s ON liquidations(symbol, ts DESC);

CREATE TABLE IF NOT EXISTS basis (
    symbol TEXT NOT NULL,
    ts INTEGER NOT NULL,
    spot REAL NOT NULL,
    perp REAL NOT NULL,
    basis REAL NOT NULL,
    basis_pct REAL NOT NULL,
    PRIMARY KEY (symbol, ts)
);
CREATE INDEX IF NOT EXISTS idx_basis_s ON basis(symbol, ts DESC);

CREATE TABLE IF NOT EXISTS paper_accounts (
    user_id TEXT PRIMARY KEY,
    balance REAL NOT NULL,
    created_ts INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS paper_trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT NOT NULL,
    side TEXT NOT NULL,
    entry_price REAL NOT NULL,
    size_usd REAL NOT NULL,
    leverage REAL NOT NULL,
    margin REAL NOT NULL,
    liq_price REAL NOT NULL,
    status TEXT NOT NULL,
    entry_ts INTEGER NOT NULL,
    close_price REAL,
    close_ts INTEGER,
    pnl_usd REAL
);
CREATE INDEX IF NOT EXISTS idx_paper_uid_status ON paper_trades(user_id, status, entry_ts DESC);
CREATE INDEX IF NOT EXISTS idx_paper_status ON paper_trades(status);

CREATE TABLE IF NOT EXISTS gex_snapshots (
    symbol TEXT NOT NULL,
    ts INTEGER NOT NULL,
    spot REAL NOT NULL,
    flip_zone REAL NOT NULL,
    total_call_gex REAL NOT NULL,
    total_put_gex REAL NOT NULL,
    net_gex REAL NOT NULL,
    state TEXT NOT NULL,
    PRIMARY KEY (symbol, ts)
);
CREATE INDEX IF NOT EXISTS idx_gex_s ON gex_snapshots(symbol, ts DESC);

CREATE TABLE IF NOT EXISTS taker_ratios (
    symbol TEXT NOT NULL,
    timeframe TEXT NOT NULL,
    ts INTEGER NOT NULL,
    qv REAL NOT NULL,
    taker_buy_qv REAL NOT NULL,
    ratio REAL NOT NULL,
    PRIMARY KEY (symbol, timeframe, ts)
);
CREATE INDEX IF NOT EXISTS idx_taker_st ON taker_ratios(symbol, timeframe, ts DESC);
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

    def upsert_funding(self, symbol: str, ts: int, rate: float):
        with self._lock:
            self._conn.execute(
                """INSERT INTO funding (symbol, ts, rate) VALUES (?,?,?)
                ON CONFLICT(symbol, ts) DO UPDATE SET rate=excluded.rate""",
                (symbol, ts, rate),
            )
            self._conn.commit()

    def upsert_oi(self, symbol: str, ts: int, oi: float, oi_value: float):
        with self._lock:
            self._conn.execute(
                """INSERT INTO open_interest (symbol, ts, oi, oi_value) VALUES (?,?,?,?)
                ON CONFLICT(symbol, ts) DO UPDATE SET oi=excluded.oi, oi_value=excluded.oi_value""",
                (symbol, ts, oi, oi_value),
            )
            self._conn.commit()

    def load_funding(self, symbol: str, limit: int = 500) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT ts, rate FROM funding WHERE symbol=? ORDER BY ts DESC LIMIT ?",
                (symbol, limit),
            ).fetchall()
        rows.reverse()
        return [{"ts": r[0], "rate": r[1]} for r in rows]

    def load_oi(self, symbol: str, limit: int = 500) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT ts, oi, oi_value FROM open_interest WHERE symbol=? ORDER BY ts DESC LIMIT ?",
                (symbol, limit),
            ).fetchall()
        rows.reverse()
        return [{"ts": r[0], "oi": r[1], "oi_value": r[2]} for r in rows]

    def upsert_liquidation(self, symbol: str, ts: int, long_usd: float, short_usd: float, low: float, high: float):
        with self._lock:
            self._conn.execute(
                """INSERT INTO liquidations (symbol, ts, long_usd, short_usd, low, high) VALUES (?,?,?,?,?,?)
                ON CONFLICT(symbol, ts) DO UPDATE SET
                    long_usd=excluded.long_usd, short_usd=excluded.short_usd,
                    low=excluded.low, high=excluded.high""",
                (symbol, ts, long_usd, short_usd, low, high),
            )
            self._conn.commit()

    def load_liquidations(self, symbol: str, since_ts: int | None = None, limit: int = 5000) -> list[dict]:
        with self._lock:
            if since_ts is None:
                rows = self._conn.execute(
                    """SELECT ts, long_usd, short_usd, low, high FROM liquidations
                    WHERE symbol=? ORDER BY ts DESC LIMIT ?""",
                    (symbol, limit),
                ).fetchall()
            else:
                rows = self._conn.execute(
                    """SELECT ts, long_usd, short_usd, low, high FROM liquidations
                    WHERE symbol=? AND ts >= ? ORDER BY ts DESC LIMIT ?""",
                    (symbol, since_ts, limit),
                ).fetchall()
        rows.reverse()
        return [
            {"ts": r[0], "long_usd": r[1], "short_usd": r[2], "low": r[3], "high": r[4]}
            for r in rows
        ]

    def upsert_basis(self, symbol: str, ts: int, spot: float, perp: float, basis: float, basis_pct: float):
        with self._lock:
            self._conn.execute(
                """INSERT INTO basis (symbol, ts, spot, perp, basis, basis_pct) VALUES (?,?,?,?,?,?)
                ON CONFLICT(symbol, ts) DO UPDATE SET
                    spot=excluded.spot, perp=excluded.perp,
                    basis=excluded.basis, basis_pct=excluded.basis_pct""",
                (symbol, ts, spot, perp, basis, basis_pct),
            )
            self._conn.commit()

    def load_basis(self, symbol: str, since_ts: int | None = None, limit: int = 5000) -> list[dict]:
        with self._lock:
            if since_ts is None:
                rows = self._conn.execute(
                    """SELECT ts, spot, perp, basis, basis_pct FROM basis
                    WHERE symbol=? ORDER BY ts DESC LIMIT ?""",
                    (symbol, limit),
                ).fetchall()
            else:
                rows = self._conn.execute(
                    """SELECT ts, spot, perp, basis, basis_pct FROM basis
                    WHERE symbol=? AND ts >= ? ORDER BY ts DESC LIMIT ?""",
                    (symbol, since_ts, limit),
                ).fetchall()
        rows.reverse()
        return [
            {"ts": r[0], "spot": r[1], "perp": r[2], "basis": r[3], "basis_pct": r[4]}
            for r in rows
        ]

    # ---------- paper trading ----------

    def get_or_create_paper_account(self, user_id: str, default_balance: float) -> dict:
        with self._lock:
            row = self._conn.execute(
                "SELECT user_id, balance, created_ts FROM paper_accounts WHERE user_id=?",
                (user_id,),
            ).fetchone()
            if row:
                return {"user_id": row[0], "balance": row[1], "created_ts": row[2]}
            import time as _t
            ts = int(_t.time())
            self._conn.execute(
                "INSERT INTO paper_accounts (user_id, balance, created_ts) VALUES (?,?,?)",
                (user_id, default_balance, ts),
            )
            self._conn.commit()
            return {"user_id": user_id, "balance": default_balance, "created_ts": ts}

    def update_paper_balance(self, user_id: str, new_balance: float):
        with self._lock:
            self._conn.execute(
                "UPDATE paper_accounts SET balance=? WHERE user_id=?",
                (new_balance, user_id),
            )
            self._conn.commit()

    def insert_paper_trade(self, user_id: str, side: str, entry_price: float, size_usd: float,
                            leverage: float, margin: float, liq_price: float, ts_ms: int) -> dict:
        with self._lock:
            cur = self._conn.execute(
                """INSERT INTO paper_trades
                (user_id, side, entry_price, size_usd, leverage, margin, liq_price, status, entry_ts)
                VALUES (?,?,?,?,?,?,?,?,?)""",
                (user_id, side, entry_price, size_usd, leverage, margin, liq_price, "open", ts_ms),
            )
            self._conn.commit()
            tid = cur.lastrowid
        return self.load_paper_trade(tid)

    def close_paper_trade(self, trade_id: int, close_price: float, close_ts_ms: int, pnl: float, status: str):
        with self._lock:
            self._conn.execute(
                """UPDATE paper_trades SET status=?, close_price=?, close_ts=?, pnl_usd=?
                   WHERE id=?""",
                (status, close_price, close_ts_ms, pnl, trade_id),
            )
            self._conn.commit()

    def load_paper_trade(self, trade_id: int) -> dict | None:
        with self._lock:
            row = self._conn.execute(
                """SELECT id, user_id, side, entry_price, size_usd, leverage, margin, liq_price,
                          status, entry_ts, close_price, close_ts, pnl_usd
                   FROM paper_trades WHERE id=?""",
                (trade_id,),
            ).fetchone()
        if not row:
            return None
        return {
            "id": row[0], "user_id": row[1], "side": row[2], "entry_price": row[3],
            "size_usd": row[4], "leverage": row[5], "margin": row[6], "liq_price": row[7],
            "status": row[8], "entry_ts": row[9], "close_price": row[10],
            "close_ts": row[11], "pnl_usd": row[12],
        }

    def load_open_paper_trades(self, user_id: str) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                """SELECT id, user_id, side, entry_price, size_usd, leverage, margin, liq_price,
                          status, entry_ts, close_price, close_ts, pnl_usd
                   FROM paper_trades WHERE user_id=? AND status='open'
                   ORDER BY entry_ts DESC""",
                (user_id,),
            ).fetchall()
        return [
            {"id": r[0], "user_id": r[1], "side": r[2], "entry_price": r[3],
             "size_usd": r[4], "leverage": r[5], "margin": r[6], "liq_price": r[7],
             "status": r[8], "entry_ts": r[9], "close_price": r[10],
             "close_ts": r[11], "pnl_usd": r[12]} for r in rows
        ]

    def load_paper_trades(self, user_id: str, limit: int = 100) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                """SELECT id, user_id, side, entry_price, size_usd, leverage, margin, liq_price,
                          status, entry_ts, close_price, close_ts, pnl_usd
                   FROM paper_trades WHERE user_id=?
                   ORDER BY entry_ts DESC LIMIT ?""",
                (user_id, limit),
            ).fetchall()
        return [
            {"id": r[0], "user_id": r[1], "side": r[2], "entry_price": r[3],
             "size_usd": r[4], "leverage": r[5], "margin": r[6], "liq_price": r[7],
             "status": r[8], "entry_ts": r[9], "close_price": r[10],
             "close_ts": r[11], "pnl_usd": r[12]} for r in rows
        ]

    def users_with_open_paper_trades(self) -> list[str]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT DISTINCT user_id FROM paper_trades WHERE status='open'"
            ).fetchall()
        return [r[0] for r in rows]

    def upsert_gex(self, symbol: str, ts: int, spot: float, flip_zone: float,
                   total_call_gex: float, total_put_gex: float, net_gex: float, state: str):
        with self._lock:
            self._conn.execute(
                """INSERT INTO gex_snapshots
                (symbol, ts, spot, flip_zone, total_call_gex, total_put_gex, net_gex, state)
                VALUES (?,?,?,?,?,?,?,?)
                ON CONFLICT(symbol, ts) DO UPDATE SET
                    spot=excluded.spot, flip_zone=excluded.flip_zone,
                    total_call_gex=excluded.total_call_gex,
                    total_put_gex=excluded.total_put_gex,
                    net_gex=excluded.net_gex, state=excluded.state""",
                (symbol, ts, spot, flip_zone, total_call_gex, total_put_gex, net_gex, state),
            )
            self._conn.commit()

    def upsert_taker(self, symbol: str, timeframe: str, ts: int, qv: float, taker_buy_qv: float, ratio: float):
        with self._lock:
            self._conn.execute(
                """INSERT INTO taker_ratios (symbol, timeframe, ts, qv, taker_buy_qv, ratio) VALUES (?,?,?,?,?,?)
                ON CONFLICT(symbol, timeframe, ts) DO UPDATE SET
                    qv=excluded.qv, taker_buy_qv=excluded.taker_buy_qv, ratio=excluded.ratio""",
                (symbol, timeframe, ts, qv, taker_buy_qv, ratio),
            )
            self._conn.commit()

    def load_taker(self, symbol: str, timeframe: str, limit: int = 500) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                """SELECT ts, qv, taker_buy_qv, ratio FROM taker_ratios
                WHERE symbol=? AND timeframe=? ORDER BY ts DESC LIMIT ?""",
                (symbol, timeframe, limit),
            ).fetchall()
        rows.reverse()
        return [{"ts": r[0], "qv": r[1], "taker_buy_qv": r[2], "ratio": r[3]} for r in rows]

    def last_liquidation_ts(self, symbol: str) -> int | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT MAX(ts) FROM liquidations WHERE symbol=?", (symbol,)
            ).fetchone()
        return row[0] if row and row[0] is not None else None

    def last_ts(self, symbol: str, timeframe: str) -> int | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT MAX(ts) FROM candles WHERE symbol=? AND timeframe=?",
                (symbol, timeframe),
            ).fetchone()
        return row[0] if row and row[0] is not None else None
