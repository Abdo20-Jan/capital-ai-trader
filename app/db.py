"""Tabelas do app (agentes, trades por agente, decisões, patrimônio). Usa o mesmo
SQLite das notícias (trader.store.Store)."""
from __future__ import annotations

import json

from trader.store import Store, now

APP_SCHEMA = """
CREATE TABLE IF NOT EXISTS agent_trades (
  deal_id TEXT PRIMARY KEY, agent TEXT, epic TEXT, direction TEXT, size REAL, entry REAL,
  stop REAL, target REAL, risk REAL, opened TEXT, closed TEXT, exit_price REAL, pnl REAL, r REAL,
  reason_open TEXT, reason_close TEXT, status TEXT, broker TEXT);
CREATE TABLE IF NOT EXISTS decisions (
  id INTEGER PRIMARY KEY AUTOINCREMENT, time TEXT, agent TEXT, epic TEXT, action TEXT,
  direction TEXT, accepted INTEGER, reason TEXT);
CREATE TABLE IF NOT EXISTS app_equity (time TEXT, broker TEXT, equity REAL, balance REAL, real_equity REAL);
CREATE INDEX IF NOT EXISTS idx_at_agent ON agent_trades(agent, status);
CREATE INDEX IF NOT EXISTS idx_dec_time ON decisions(id);
"""


class AppDB(Store):
    def __init__(self, path):
        super().__init__(path)
        with self.conn() as c:
            c.executescript(APP_SCHEMA)

    def record_open(self, **k) -> None:
        with self.conn() as c:
            c.execute("INSERT OR REPLACE INTO agent_trades (deal_id, agent, epic, direction, size, entry, stop, target, "
                      "risk, opened, reason_open, status, broker) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                      (k["deal_id"], k["agent"], k["epic"], k["direction"], k["size"], k["entry"], k["stop"],
                       k["target"], k["risk"], now(), k["reason"], "OPEN", k["broker"]))

    def record_close(self, deal_id: str, pnl: float | None, exit_price: float | None, reason: str) -> dict | None:
        rows = self.rows("SELECT * FROM agent_trades WHERE deal_id=?", (deal_id,))
        if not rows:
            return None
        t = rows[0]
        r = (pnl / t["risk"]) if (pnl is not None and t["risk"]) else None
        with self.conn() as c:
            c.execute("UPDATE agent_trades SET closed=?, exit_price=?, pnl=?, r=?, reason_close=?, status='CLOSED' "
                      "WHERE deal_id=?", (now(), exit_price, pnl, r, reason, deal_id))
        t.update(closed=now(), exit_price=exit_price, pnl=pnl, r=r, reason_close=reason, status="CLOSED")
        return t

    def open_trades(self, broker: str) -> dict[str, dict]:
        return {r["deal_id"]: r for r in self.rows("SELECT * FROM agent_trades WHERE status='OPEN' AND broker=?", (broker,))}

    def decision(self, agent: str, epic: str, action: str, direction: str | None, accepted: bool, reason: str) -> dict:
        d = {"time": now(), "agent": agent, "epic": epic, "action": action, "direction": direction,
             "accepted": int(accepted), "reason": reason}
        with self.conn() as c:
            c.execute("INSERT INTO decisions (time, agent, epic, action, direction, accepted, reason) VALUES (?,?,?,?,?,?,?)",
                      tuple(d.values()))
        return d

    def agent_stats(self, agent: str, broker: str, last: int = 30) -> dict:
        closed = self.rows("SELECT pnl, r FROM agent_trades WHERE agent=? AND broker=? AND status='CLOSED' "
                           "ORDER BY closed DESC", (agent, broker))
        n = len(closed)
        wins = sum(1 for t in closed if (t["pnl"] or 0) > 0)
        rs = [t["r"] for t in closed[:last] if t["r"] is not None]
        return {"trades": n, "wins": wins, "win_rate": wins / n if n else None,
                "pnl": round(sum(t["pnl"] or 0 for t in closed), 2),
                "expectancy_r": round(sum(rs) / len(rs), 3) if rs else None, "recent_n": len(rs)}

    def add_app_equity(self, broker: str, equity: float, balance: float, real_equity: float) -> None:
        with self.conn() as c:
            c.execute("INSERT INTO app_equity VALUES (?,?,?,?,?)", (now(), broker, equity, balance, real_equity))

    def get_json(self, key: str, default=None):
        return self.get(key, default)

    def set_json(self, key: str, value) -> None:
        self.set(key, json.loads(json.dumps(value, default=str)))
