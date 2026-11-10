"""Painel web (FastAPI). Lê o SQLite que o bot escreve; não envia ordens."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse

from trader.config import Settings
from trader.engine import SENT_WINDOW_H, aggregate_sentiment
from trader.sentiment import TOPIC_LABELS
from trader.store import Store

STATIC = Path(__file__).parent / "static"


def build_state(store: Store, s: Settings) -> dict:
    now = datetime.now(timezone.utc)
    since = (now - timedelta(hours=SENT_WINDOW_H)).isoformat()
    tickers = [(w.epic, w.ticker) for w in s.watchlist]

    sentiment = []
    for epic, ticker in tickers:
        rows = store.recent_news(ticker, since)
        last = store.rows("SELECT * FROM signals WHERE epic=? ORDER BY id DESC LIMIT 1", (epic,))
        sentiment.append({"epic": epic, "ticker": ticker, "score": aggregate_sentiment(rows, now),
                          "count": len(rows), "signal": last[0] if last else None})

    topics = store.rows("SELECT topic, COUNT(*) n, AVG(sentiment) s FROM news WHERE time>=? "
                        "GROUP BY topic ORDER BY n DESC", (since,))
    for t in topics:
        t["label"] = TOPIC_LABELS.get(t["topic"], t["topic"])

    equity = store.rows("SELECT time, balance, equity FROM (SELECT rowid, * FROM equity ORDER BY rowid DESC LIMIT 600) "
                        "ORDER BY rowid")
    closed = store.rows("SELECT * FROM trades WHERE status='CLOSED' ORDER BY closed DESC LIMIT 50")
    stats = store.rows("SELECT COUNT(*) n, SUM(pnl>0) wins, SUM(pnl) pnl FROM trades WHERE status='CLOSED'")[0]
    models = store.rows("SELECT report FROM models ORDER BY time DESC LIMIT 1")

    return {
        "now": now.isoformat(),
        "status": store.get("status", {}),
        "settings": {"start": s.start_balance, "target": s.target_balance, "risk": s.risk_per_trade,
                     "broker": s.broker, "resolution": s.resolution},
        "equity": equity,
        "sentiment": sentiment,
        "topics": topics,
        "news": store.rows("SELECT * FROM news ORDER BY ingested DESC, time DESC LIMIT 60"),
        "signals": store.rows("SELECT * FROM signals ORDER BY id DESC LIMIT 60"),
        "open_trades": store.rows("SELECT * FROM trades WHERE status='OPEN' ORDER BY opened DESC"),
        "closed_trades": closed,
        "stats": {"trades": stats["n"] or 0, "wins": stats["wins"] or 0, "pnl": stats["pnl"] or 0.0},
        "events": store.rows("SELECT * FROM events ORDER BY id DESC LIMIT 50"),
        "model": models[0]["report"] if models else None,
    }


def create_app(s: Settings) -> FastAPI:
    app = FastAPI(title="Capital AI Trader")
    store = Store(s.db_path)

    @app.get("/")
    def index():
        return FileResponse(STATIC / "index.html")

    @app.get("/api/state")
    def state():
        return build_state(store, s)

    return app
