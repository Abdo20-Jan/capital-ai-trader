"""Conta em PAPEL com dados REAIS: preços, candles e volume reais (Alpaca); só o saldo e as
ordens são virtuais. Posições e saldo ficam no banco (sobrevivem a reinícios na nuvem)."""
from __future__ import annotations

import itertools
import threading
import time
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from trader.broker import Account, MarketRules, OrderResult, Position, Quote

from .data.alpaca import AlpacaData, is_crypto

NY = ZoneInfo("America/New_York")
SCHEMA = """CREATE TABLE IF NOT EXISTS paper_positions (
  deal_id TEXT PRIMARY KEY, epic TEXT, direction TEXT, size REAL, level REAL, stop REAL, target REAL, opened TEXT);"""


def us_market_open(at: datetime | None = None) -> bool:
    """Pregão regular de NY (seg–sex, 9h30–16h). Feriados não são considerados."""
    t = (at or datetime.now(timezone.utc)).astimezone(NY)
    if t.weekday() >= 5:
        return False
    return (9, 30) <= (t.hour, t.minute) < (16, 0)


class PaperRealBroker:
    name = "papel"

    def __init__(self, data: AlpacaData, db, start_balance: float, max_leverage: float = 200.0):
        self.data = data
        self.db = db
        self.lock = threading.RLock()
        self.max_leverage = max_leverage
        with db.conn() as c:
            c.executescript(SCHEMA)
        if db.get("paper_cash") is None:
            db.set("paper_cash", start_balance)
        self.quotes: dict[str, Quote] = {}
        self.closed: list[dict] = []
        n = db.rows("SELECT COUNT(*) n FROM paper_positions")[0]["n"] + len(db.rows("SELECT deal_id FROM agent_trades WHERE broker LIKE 'papel%'"))
        self._ids = itertools.count(n + 1)

    @property
    def cash(self) -> float:
        return float(self.db.get("paper_cash", 0.0))

    def now(self) -> datetime:
        return datetime.now(timezone.utc)

    def market_open(self, epic: str) -> bool:
        return True if is_crypto(epic) else us_market_open()

    # ------------------------------------------------------------ preços
    def update_quotes(self, snaps: dict[str, dict]) -> list[dict]:
        done = []
        with self.lock:
            for epic, s in snaps.items():
                bid, ask = s.get("bid") or s.get("last"), s.get("ask") or s.get("last")
                if not bid or not ask:
                    continue
                if bid == ask:                                  # sem livro: estima o spread
                    half = bid * (0.0003 if is_crypto(epic) else 0.0001)
                    bid, ask = bid - half, ask + half
                self.quotes[epic] = Quote(bid=bid, ask=ask, tradeable=self.market_open(epic))
            for p in self.positions():
                q = self.quotes.get(p.epic)
                if not q or not q.tradeable:
                    continue
                px = q.bid if p.direction == "BUY" else q.ask
                hit = None
                if p.direction == "BUY":
                    hit = ("stop", p.stop_level) if p.stop_level and px <= p.stop_level else \
                          ("alvo", p.profit_level) if p.profit_level and px >= p.profit_level else None
                else:
                    hit = ("stop", p.stop_level) if p.stop_level and px >= p.stop_level else \
                          ("alvo", p.profit_level) if p.profit_level and px <= p.profit_level else None
                if hit:
                    done.append(self._realize(p, hit[1], hit[0]))
        return done

    def _realize(self, p: Position, price: float, reason: str) -> dict:
        pnl = (price - p.level) * p.size * (1 if p.direction == "BUY" else -1)
        self.db.set("paper_cash", self.cash + pnl)
        with self.db.conn() as c:
            c.execute("DELETE FROM paper_positions WHERE deal_id=?", (p.deal_id,))
        rec = {"deal_id": p.deal_id, "epic": p.epic, "pnl": pnl, "price": price, "reason": reason}
        self.closed.append(rec)
        self.closed = self.closed[-500:]
        return rec

    # ------------------------------------------------------------ interface Broker
    def account(self) -> Account:
        with self.lock:
            ps = self.positions()
            upl = sum(p.upl for p in ps)
            margin = sum(p.size * p.level * self.rules(p.epic).margin_factor for p in ps)
            cash = self.cash
            return Account(balance=cash, equity=cash + upl, available=cash + upl - margin)

    def candles(self, epic, resolution="MINUTE_15", count=300):
        return self.data.candles(epic, resolution, count)

    def quote(self, epic: str) -> Quote:
        q = self.quotes.get(epic)
        if q is None:
            s = self.data.snapshots([epic]).get(epic)
            if not s:
                raise RuntimeError(f"sem preço real para {epic} (configure a Alpaca)")
            self.update_quotes({epic: s})
            q = self.quotes[epic]
        return Quote(q.bid, q.ask, self.market_open(epic))

    def rules(self, epic: str) -> MarketRules:
        q = self.quotes.get(epic)
        price = q.mid if q else 100.0
        if is_crypto(epic):
            return MarketRules(epic, f"{epic} (papel, dados reais)", 0.0001, 0.0001, price * 0.0005,
                               1 / min(self.max_leverage, 2), "USD", 2)
        return MarketRules(epic, f"{epic} (papel, dados reais)", 0.1, 0.1, price * 0.0005,
                           1 / min(self.max_leverage, 5), "USD", 2)

    def positions(self) -> list[Position]:
        out = []
        for r in self.db.rows("SELECT * FROM paper_positions"):
            q = self.quotes.get(r["epic"])
            upl = 0.0
            if q:
                px = q.bid if r["direction"] == "BUY" else q.ask
                upl = (px - r["level"]) * r["size"] * (1 if r["direction"] == "BUY" else -1)
            out.append(Position(r["deal_id"], r["epic"], r["direction"], r["size"], r["level"], upl, r["stop"], r["target"]))
        return out

    def open_position(self, epic, direction, size, stop_level, profit_level) -> OrderResult:
        with self.lock:
            try:
                q = self.quote(epic)
            except Exception as e:  # noqa: BLE001
                return OrderResult(False, None, None, str(e))
            if not q.tradeable:
                return OrderResult(False, None, None, "mercado fechado (pregão de NY: 9h30–16h, seg–sex)")
            level = q.ask if direction == "BUY" else q.bid
            deal_id = f"PAP{int(time.time())}{next(self._ids):04d}"
            with self.db.conn() as c:
                c.execute("INSERT INTO paper_positions VALUES (?,?,?,?,?,?,?,?)",
                          (deal_id, epic, direction, size, level, stop_level, profit_level, self.now().isoformat()))
            return OrderResult(True, deal_id, level, "ACCEPTED (papel, preço real)")

    def close_position(self, deal_id: str) -> OrderResult:
        with self.lock:
            p = next((x for x in self.positions() if x.deal_id == deal_id), None)
            if not p:
                return OrderResult(False, deal_id, None, "posição não encontrada")
            q = self.quotes.get(p.epic)
            if not q:
                return OrderResult(False, deal_id, None, "sem preço para fechar")
            rec = self._realize(p, q.bid if p.direction == "BUY" else q.ask, "fechada pelo agente")
            return OrderResult(True, deal_id, rec["price"], f"fechada ({rec['pnl']:+.2f})")
