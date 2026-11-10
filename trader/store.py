"""Armazenamento em SQLite (o painel lê daqui; o bot escreve)."""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS news (
  id TEXT PRIMARY KEY, time TEXT, ingested TEXT, ticker TEXT, title TEXT, url TEXT, source TEXT,
  topic TEXT, sentiment REAL, relevance REAL, scorer TEXT);
CREATE TABLE IF NOT EXISTS signals (
  id INTEGER PRIMARY KEY AUTOINCREMENT, time TEXT, bar_time TEXT, epic TEXT, price REAL, atr REAL,
  p_long REAL, p_short REAL, sentiment REAL, decision TEXT, reason TEXT);
CREATE TABLE IF NOT EXISTS trades (
  deal_id TEXT PRIMARY KEY, opened TEXT, closed TEXT, epic TEXT, direction TEXT, size REAL,
  entry REAL, stop REAL, target REAL, risk REAL, p REAL, sentiment REAL, status TEXT, pnl REAL);
CREATE TABLE IF NOT EXISTS equity (time TEXT, balance REAL, equity REAL);
CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY AUTOINCREMENT, time TEXT, level TEXT, message TEXT);
CREATE TABLE IF NOT EXISTS models (version TEXT PRIMARY KEY, time TEXT, report TEXT);
CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT);
CREATE INDEX IF NOT EXISTS idx_news_time ON news(time);
CREATE INDEX IF NOT EXISTS idx_signals_epic ON signals(epic, bar_time);
"""


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Store:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.conn() as c:
            c.execute("PRAGMA journal_mode=WAL")
            c.executescript(SCHEMA)

    @contextmanager
    def conn(self):
        c = sqlite3.connect(self.path, timeout=10)
        c.row_factory = sqlite3.Row
        try:
            yield c
            c.commit()
        finally:
            c.close()

    def rows(self, sql: str, args: tuple = ()) -> list[dict]:
        with self.conn() as c:
            return [dict(r) for r in c.execute(sql, args).fetchall()]

    # ------------------------------------------------------------------ escrita
    def known_news_ids(self, ids: list[str]) -> set[str]:
        if not ids:
            return set()
        q = ",".join("?" * len(ids))
        return {r["id"] for r in self.rows(f"SELECT id FROM news WHERE id IN ({q})", tuple(ids))}

    def add_news(self, item: dict, scored) -> None:
        with self.conn() as c:
            c.execute("INSERT OR IGNORE INTO news VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                      (item["id"], item["time"], now(), scored.ticker or item.get("ticker", ""), item["title"],
                       item.get("url", ""), item.get("source", ""), scored.topic, scored.sentiment,
                       scored.relevance, scored.scorer))

    def add_signal(self, **kw) -> None:
        with self.conn() as c:
            c.execute("INSERT INTO signals (time,bar_time,epic,price,atr,p_long,p_short,sentiment,decision,reason) "
                      "VALUES (?,?,?,?,?,?,?,?,?,?)",
                      (now(), kw["bar_time"], kw["epic"], kw["price"], kw["atr"], kw["p_long"], kw["p_short"],
                       kw["sentiment"], kw["decision"], kw["reason"]))

    def open_trade(self, **kw) -> None:
        with self.conn() as c:
            c.execute("INSERT OR REPLACE INTO trades VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                      (kw["deal_id"], now(), None, kw["epic"], kw["direction"], kw["size"], kw["entry"],
                       kw["stop"], kw["target"], kw["risk"], kw["p"], kw["sentiment"], "OPEN", None))

    def close_trade(self, deal_id: str, pnl: float | None) -> None:
        with self.conn() as c:
            c.execute("UPDATE trades SET closed=?, status='CLOSED', pnl=? WHERE deal_id=?", (now(), pnl, deal_id))

    def open_trade_ids(self) -> dict[str, dict]:
        return {r["deal_id"]: r for r in self.rows("SELECT * FROM trades WHERE status='OPEN'")}

    def add_equity(self, balance: float, equity: float) -> None:
        with self.conn() as c:
            c.execute("INSERT INTO equity VALUES (?,?,?)", (now(), balance, equity))

    def event(self, message: str, level: str = "info") -> None:
        with self.conn() as c:
            c.execute("INSERT INTO events (time, level, message) VALUES (?,?,?)", (now(), level, message))

    def add_model_report(self, report) -> None:
        with self.conn() as c:
            c.execute("INSERT OR REPLACE INTO models VALUES (?,?,?)",
                      (report.version, now(), json.dumps(report.__dict__)))

    def set(self, key: str, value) -> None:
        with self.conn() as c:
            c.execute("INSERT OR REPLACE INTO kv VALUES (?,?)", (key, json.dumps(value)))

    def get(self, key: str, default=None):
        r = self.rows("SELECT value FROM kv WHERE key=?", (key,))
        return json.loads(r[0]["value"]) if r else default

    # ------------------------------------------------------------------ leitura
    def recent_news(self, ticker: str, since_iso: str) -> list[dict]:
        return self.rows("SELECT * FROM news WHERE ticker=? AND time>=? ORDER BY time", (ticker, since_iso))

    def sentiment_history(self, epic: str) -> list[dict]:
        return self.rows("SELECT bar_time, sentiment FROM signals WHERE epic=? ORDER BY id", (epic,))
