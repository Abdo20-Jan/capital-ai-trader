"""Testes do app (agentes, gestor de risco, streaming, API)."""
import asyncio
import json
import time

import numpy as np
import pandas as pd
import pytest

from trader.broker import Account


# ------------------------------------------------------------------ .env
def test_write_env_updates_and_appends(tmp_path, monkeypatch):
    from app.main import write_env
    p = tmp_path / ".env"
    p.write_text("# comentário\nBROKER=paper\nCAPITAL_API_KEY=\n")
    write_env({"BROKER": "capital_demo", "CAPITAL_API_KEY": "abc", "CAPITAL_ACCOUNT_ID": "123"}, p)
    txt = p.read_text()
    assert "# comentário" in txt and "BROKER=capital_demo" in txt and "CAPITAL_API_KEY=abc" in txt
    assert "CAPITAL_ACCOUNT_ID=123" in txt and txt.count("BROKER=") == 1
    assert oct(p.stat().st_mode)[-3:] == "600"


# ------------------------------------------------------------------ gestor de risco
class FakeCtx:
    def __init__(self, tmp_path):
        from app.db import AppDB
        from trader.config import Settings
        self.settings = Settings(db_path=tmp_path / "x.db")
        self.db = AppDB(tmp_path / "a.db")
        self.broker_id = "t"
        self.max_total_positions = 6
        from datetime import datetime, timezone
        self._now = datetime(2026, 11, 10, 15, tzinfo=timezone.utc)

    def now(self):
        return self._now


class FakeAgent:
    def __init__(self, key, max_positions=2):
        from types import SimpleNamespace
        self.cfg = SimpleNamespace(key=key, name=key, max_positions=max_positions, risk_per_trade=0.01, margin_share=0.25)


def test_risk_blocks_opposite_direction_and_limits(tmp_path):
    from app.orders import OrderManager
    om = OrderManager(FakeCtx(tmp_path))
    om.live = {"D1": {"agent": "swing", "epic": "AAPL", "direction": "BUY", "size": 1, "level": 100}}
    ok, why = om._pre_check(FakeAgent("scalp"), "AAPL", "SELL")
    assert not ok and "oposta" in why
    assert om._pre_check(FakeAgent("scalp"), "AAPL", "BUY")[0]          # mesma direção: permitido
    ok, why = om._pre_check(FakeAgent("swing"), "AAPL", "BUY")
    assert not ok and "já tem posição" in why
    ok, why = om._pre_check(FakeAgent("swing", max_positions=1), "MSFT", "BUY")
    assert not ok and "limite" in why


def test_guard_kill_switch_daily_loss_and_bankroll(tmp_path):
    from app.orders import OrderManager
    ctx = FakeCtx(tmp_path)
    om = OrderManager(ctx)
    v = om.bot_account(Account(balance=10_000, equity=10_000, available=10_000))
    assert v.equity == pytest.approx(60)
    assert om.guard(v)[0]
    om.kill_switch = True
    assert "PAUSA" in om.guard(v)[1]
    om.kill_switch = False
    v2 = om.bot_account(Account(balance=9_995, equity=9_995, available=9_995))   # −$5 = −8% no dia
    assert not om.guard(v2)[0]
    ctx.settings.max_daily_loss = 0                                            # modo aprendizado
    assert om.guard(v2)[0]


# ------------------------------------------------------------------ agentes
def _trend_df(n=300, up=True, seed=1):
    rng = np.random.default_rng(seed)
    drift = 0.0015 if up else -0.0015
    c = 100 * np.cumprod(1 + drift + rng.normal(0, 0.002, n))
    o = np.r_[c[0], c[:-1]]
    h = np.maximum(o, c) * 1.001
    lo = np.minimum(o, c) * 0.999
    t = pd.date_range("2026-01-01", periods=n, freq="min", tz="UTC")
    return pd.DataFrame({"time": t, "open": o, "high": h, "low": lo, "close": c})


def test_scalper_needs_pullback_and_respects_spread():
    from types import SimpleNamespace
    from app.agents.base import indicators
    from app.agents.strategies import ScalperAgent, default_agents
    cfg = next(c for cls, c in default_agents() if c.key == "scalp")
    ctx = SimpleNamespace(market=SimpleNamespace(quotes={"X": {"bid": 100, "ask": 100.01}}))
    ag = ScalperAgent(cfg, ctx)
    ind = indicators(_trend_df())
    # força um recuo até a EMA9 na barra anterior e retomada na última
    ind.loc[ind.index[-2], "low"] = ind["ema9"].iloc[-2] * 0.999
    ind.loc[ind.index[-1], "rsi7"] = 60
    sig, note = ag.evaluate("X", ind, 0.0)
    assert sig is not None and sig.direction == "BUY" and sig.stop_dist > 0
    ctx.market.quotes["X"] = {"bid": 100, "ask": 105}                         # spread absurdo
    sig, note = ag.evaluate("X", ind, 0.0)
    assert sig is None and "spread" in note


def test_long_term_only_buys_in_uptrend():
    from types import SimpleNamespace
    from app.agents.base import indicators
    from app.agents.strategies import LongTermAgent, default_agents
    cfg = next(c for cls, c in default_agents() if c.key == "long")
    ag = LongTermAgent(cfg, SimpleNamespace())
    ind = indicators(_trend_df(up=False))
    sig, note = ag.evaluate("X", ind, 0.0)
    assert sig is None and "sem regime de alta" in note


# ------------------------------------------------------------------ streaming de preços (WebSocket simulado)
def test_capital_stream_parses_quotes(monkeypatch):
    import websockets
    from app.bus import EventBus
    from app.market import MarketData

    sent = []
    msgs = [json.dumps({"status": "OK", "destination": "marketData.subscribe", "payload": {"subscriptions": {"AAPL": "PROCESSED"}}}),
            json.dumps({"status": "OK", "destination": "quote", "payload": {"epic": "AAPL", "product": "CFD", "bid": 230.1,
                                                                             "bidQty": 1, "ofr": 230.3, "ofrQty": 1, "timestamp": 1794323294000}})]

    class FakeWS:
        async def send(self, m): sent.append(json.loads(m))
        async def recv(self):
            if msgs:
                return msgs.pop(0)
            raise asyncio.CancelledError()
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False

    monkeypatch.setattr(websockets, "connect", lambda *a, **k: FakeWS())

    class B:
        name = "capital"
        def session_tokens(self): return ("cst", "tok")

    md = MarketData(B(), ["AAPL"], EventBus())
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(md.run_capital_stream())
    assert sent[0]["destination"] == "marketData.subscribe" and sent[0]["payload"]["epics"] == ["AAPL"]
    assert sent[0]["cst"] == "cst" and sent[0]["securityToken"] == "tok"
    assert md.quotes["AAPL"]["bid"] == 230.1 and md.quotes["AAPL"]["ask"] == 230.3


# ------------------------------------------------------------------ app completo no simulador
def test_app_end_to_end_simulator(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    for k, v in {"BROKER": "paper", "DB_PATH": str(tmp_path / "t.db"), "SIM_SPEED": "60", "ANTHROPIC_API_KEY": "",
                 "WATCHLIST": "AAPL,NVDA,TSLA", "HISTORY_BARS": "800"}.items():
        monkeypatch.setenv(k, v)
    from app.main import create_app
    from trader.config import get_settings
    from app.runtime import Runtime
    s = get_settings()
    s.model_dir = tmp_path / "models"
    with TestClient(create_app(Runtime(s))) as c:
        time.sleep(12)
        st = c.get("/api/state").json()
        assert st["status"]["mode"] == "simulador"
        assert {a["config"]["key"] for a in st["agents"]} == {"scalp", "day", "swing", "long", "news"}
        assert set(st["quotes"]) == {"AAPL", "NVDA", "TSLA"}
        assert st["account"]["equity"] > 0
        assert len(c.get("/api/candles", params={"epic": "AAPL", "res": "MINUTE_5"}).json()) > 50
        assert c.post("/api/kill", params={"on": "true"}).json()["kill_switch"] is True
        r = c.post("/api/agents/scalp", json={"enabled": False, "risk_per_trade": 0.002}).json()
        assert r["config"]["enabled"] is False and r["config"]["risk_per_trade"] == 0.002
        assert c.get("/api/connection").json()["mode"] == "simulador"
        assert c.post("/api/close-all").status_code == 200
        assert c.post("/api/connection/test-order", params={"epic": "AAPL"}).status_code == 400   # só com Capital
