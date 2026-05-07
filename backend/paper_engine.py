"""
Paper trading engine — fake-money longs/shorts on BTC perp.

Each browser identifies itself with a UUID stored in localStorage. First time
we see a UID, we create an account with $1000. Trades support 1x–100x
leverage with standard perp mechanics:

  margin     = size_usd / leverage          (locked from balance on open)
  pnl        = size_usd × (mark - entry) / entry × side_sign
  liq_price  = entry × (1 ± 1/lev ∓ mm)     (mm = 0.5% maintenance margin)

On liquidation the position closes at liq_price (full margin loss). Manual
close uses the current mark and returns margin + pnl to the balance.

The engine ticks every second: fetches the current mark from the basis
engine's perp mid, checks every open position for a liquidation breach,
auto-closes breached positions, and broadcasts an account snapshot to any
WS subscribers for that UID.
"""
import asyncio
import re
import time
from collections import defaultdict
from typing import Callable

from storage import Store


INITIAL_BALANCE = 1000.0
MAINTENANCE_MARGIN = 0.005     # 0.5%
MAX_LEVERAGE = 100
MIN_LEVERAGE = 1
MIN_SIZE_USD = 1.0
TICK_INTERVAL_SEC = 1.0

UID_RE = re.compile(r"^[A-Za-z0-9_-]{8,64}$")


def valid_uid(uid: str | None) -> bool:
    return bool(uid) and bool(UID_RE.match(uid or ""))


class PaperEngine:
    def __init__(self, store: Store, mark_provider: Callable[[str], float]):
        """
        mark_provider(symbol) -> float : returns the current perp mark price
        for that symbol (0.0 if unavailable). Lets us trade any of the 14 coins
        regardless of which one provides spot data.
        """
        self.store = store
        self._get_mark_for = mark_provider
        self._listeners: dict[str, list[Callable]] = defaultdict(list)
        self._running = False

    # ---------- listeners (per-UID) ----------

    def on_update(self, uid: str, fn: Callable):
        self._listeners[uid].append(fn)

    def off_update(self, uid: str, fn: Callable):
        if uid in self._listeners and fn in self._listeners[uid]:
            self._listeners[uid].remove(fn)

    async def _emit(self, uid: str, event: str, payload: dict):
        for fn in list(self._listeners.get(uid, [])):
            try:
                await fn(event, payload)
            except Exception as e:
                print(f"[paper listener error] {e}")

    # ---------- helpers ----------

    def get_mark(self, symbol: str) -> float:
        try:
            return float(self._get_mark_for(symbol) or 0)
        except Exception:
            return 0.0

    def get_account(self, uid: str) -> dict:
        return self.store.get_or_create_paper_account(uid, INITIAL_BALANCE)

    def _compute_pnl(self, trade: dict, mark: float) -> float:
        change = (mark - trade["entry_price"]) / trade["entry_price"]
        sign = 1 if trade["side"] == "long" else -1
        return trade["size_usd"] * change * sign

    def _liq_price(self, side: str, entry: float, leverage: float) -> float:
        # Hits when loss = margin × (1 − mm/100%-ish-of-lev). Standard formula.
        if side == "long":
            return entry * (1 - 1 / leverage + MAINTENANCE_MARGIN)
        return entry * (1 + 1 / leverage - MAINTENANCE_MARGIN)

    # ---------- public actions ----------

    def open_trade(self, uid: str, symbol: str, side: str, size_usd: float, leverage: float) -> dict:
        side = (side or "").lower()
        if side not in ("long", "short"):
            raise ValueError("side must be long or short")
        symbol = (symbol or "").upper()
        if not symbol:
            raise ValueError("symbol is required")
        try:
            leverage = float(leverage)
            size_usd = float(size_usd)
        except (TypeError, ValueError):
            raise ValueError("size and leverage must be numbers")
        if leverage < MIN_LEVERAGE or leverage > MAX_LEVERAGE:
            raise ValueError(f"leverage must be {MIN_LEVERAGE}–{MAX_LEVERAGE}")
        if size_usd < MIN_SIZE_USD:
            raise ValueError(f"min size is ${MIN_SIZE_USD:.0f}")

        mark = self.get_mark(symbol)
        if mark <= 0:
            raise ValueError(f"{symbol} mark price unavailable, try again in a moment")

        margin = size_usd / leverage
        acct = self.get_account(uid)
        if margin > acct["balance"] + 1e-9:
            raise ValueError(
                f"insufficient balance — need ${margin:.2f}, have ${acct['balance']:.2f}"
            )

        liq = self._liq_price(side, mark, leverage)
        ts_ms = int(time.time() * 1000)
        trade = self.store.insert_paper_trade(
            uid, symbol, side, mark, size_usd, leverage, margin, liq, ts_ms,
        )
        self.store.update_paper_balance(uid, acct["balance"] - margin)
        return trade

    def close_trade(self, uid: str, trade_id: int) -> dict:
        t = self.store.load_paper_trade(trade_id)
        if not t or t["user_id"] != uid:
            raise ValueError("trade not found")
        if t["status"] != "open":
            raise ValueError("trade already closed")
        mark = self.get_mark(t["symbol"])
        if mark <= 0:
            raise ValueError(f"{t['symbol']} mark price unavailable")
        pnl = self._compute_pnl(t, mark)
        ts_ms = int(time.time() * 1000)
        self.store.close_paper_trade(t["id"], mark, ts_ms, pnl, "closed")
        new_balance = self.get_account(uid)["balance"] + t["margin"] + pnl
        self.store.update_paper_balance(uid, max(0.0, new_balance))
        return self.store.load_paper_trade(t["id"])

    def reset_account(self, uid: str):
        # Close any open as cancelled with zero pnl, return margins, reset balance.
        opens = self.store.load_open_paper_trades(uid)
        ts_ms = int(time.time() * 1000)
        for t in opens:
            self.store.close_paper_trade(t["id"], t["entry_price"], ts_ms, 0.0, "cancelled")
        self.store.update_paper_balance(uid, INITIAL_BALANCE)

    # ---------- snapshot ----------

    def account_snapshot(self, uid: str) -> dict:
        acct = self.get_account(uid)
        opens = self.store.load_open_paper_trades(uid)
        live_opens = []
        unrealized = 0.0
        locked = 0.0
        for t in opens:
            mark = self.get_mark(t["symbol"])
            pnl = self._compute_pnl(t, mark) if mark > 0 else 0.0
            unrealized += pnl
            locked += t["margin"]
            live_opens.append({**t, "unrealized_pnl": pnl, "mark": mark})
        equity = acct["balance"] + locked + unrealized
        return {
            "uid": uid,
            "balance": acct["balance"],
            "equity": equity,
            "locked": locked,
            "unrealized": unrealized,
            "open_trades": live_opens,
        }

    def history(self, uid: str, limit: int = 100) -> list[dict]:
        return self.store.load_paper_trades(uid, limit)

    # ---------- run loop ----------

    async def run(self):
        self._running = True
        backoff = 1
        while self._running:
            try:
                await asyncio.sleep(TICK_INTERVAL_SEC)
                users = self.store.users_with_open_paper_trades()
                for uid in users:
                    opens = self.store.load_open_paper_trades(uid)
                    liquidated_ids = []
                    for t in opens:
                        mark = self.get_mark(t["symbol"])
                        if mark <= 0:
                            continue   # no live mark — skip this trade this tick
                        breached = (
                            (t["side"] == "long" and mark <= t["liq_price"])
                            or (t["side"] == "short" and mark >= t["liq_price"])
                        )
                        if breached:
                            ts_ms = int(time.time() * 1000)
                            pnl = -t["margin"]
                            self.store.close_paper_trade(
                                t["id"], t["liq_price"], ts_ms, pnl, "liquidated",
                            )
                            liquidated_ids.append(t["id"])
                    for tid in liquidated_ids:
                        await self._emit(uid, "liquidated", {"trade_id": tid})
                    # Only emit a tick if anyone is subscribed to this uid.
                    if self._listeners.get(uid):
                        await self._emit(uid, "tick", self.account_snapshot(uid))
                backoff = 1
            except Exception as e:
                print(f"[paper] tick error, retry in {backoff}s: {e}")
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30)
