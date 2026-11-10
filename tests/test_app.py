"""Testes do app (agentes, gestor de risco, streaming, API)."""
import asyncio
import json
import time

import numpy as np
import pandas as pd
import pytest

from trader.broker import Account


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
    for k, v in {"APP_MODE": "sim", "BROKER": "paper", "DATA_DIR": str(tmp_path), "DB_PATH": str(tmp_path / "t.db"),
                 "SIM_SPEED": "60", "ANTHROPIC_API_KEY": "", "LOCAL_LLM_URL": "", "ALPACA_KEY_ID": "",
                 "WATCHLIST": "AAPL,NVDA,TSLA", "HISTORY_BARS": "800"}.items():
        monkeypatch.setenv(k, v)
    from app.main import create_app
    from trader.config import get_settings
    from app.runtime import Runtime
    s = get_settings()
    s.model_dir = tmp_path / "models"
    with TestClient(create_app(Runtime(s))) as c:
        c.headers["Authorization"] = "Bearer " + c.post("/api/login", json={"password": ""}).json()["token"]
        time.sleep(12)
        st = c.get("/api/state").json()
        assert st["status"]["mode"] == "simulador"
        assert {a["config"]["key"] for a in st["agents"]} == {"scalp", "day", "swing", "long", "news"}
        assert set(st["quotes"]) == {"AAPL", "NVDA", "TSLA"}
        assert st["account"]["equity"] > 0
        assert len(c.get("/api/candles", params={"epic": "AAPL", "res": "MINUTE_5"}).json()["candles"]) > 50
        assert c.post("/api/kill", params={"on": "true"}).json()["kill_switch"] is True
        r = c.post("/api/agents/scalp", json={"enabled": False, "risk_per_trade": 0.002}).json()
        assert r["config"]["enabled"] is False and r["config"]["risk_per_trade"] == 0.002
        assert c.post("/api/close-all").status_code == 200
        assert c.post("/api/connection/test-order", params={"epic": "AAPL"}).status_code == 400   # só com Capital
        sv = c.get("/api/settings").json()
        assert "ALPACA_SECRET_KEY" in sv["values"] and "llm" in sv and sv["mode"] == "simulador"
        assert c.get("/").status_code == 200 and "app.js" in c.get("/").text                 # frontend no mesmo servidor
        assert c.get("/config.js").status_code == 200


# ------------------------------------------------------------------ login e cofre
def test_auth_public_requires_password(tmp_path, monkeypatch):
    from app.secure import Auth
    monkeypatch.delenv("APP_PASSWORD", raising=False)
    a = Auth(tmp_path, public=True)
    assert a.blocked and not a.verify("x")
    monkeypatch.setenv("APP_PASSWORD", "segredo")
    a = Auth(tmp_path, public=True)
    assert not a.blocked and a.check_password("segredo") and not a.check_password("errada")
    tok = a.issue()
    assert a.verify(tok) and not a.verify(tok + "0") and not a.verify("123.abc")

    monkeypatch.delenv("APP_PASSWORD")
    assert Auth(tmp_path, public=False).verify(None)          # local sem senha: liberado


def test_vault_encrypts_and_respects_server_env(tmp_path, monkeypatch):
    from app.db import AppDB
    from app.secure import Vault
    monkeypatch.setenv("ALPACA_FEED", "sip")                  # definido no servidor
    monkeypatch.delenv("ALPACA_KEY_ID", raising=False)
    db = AppDB(tmp_path / "a.db")
    v = Vault(db, tmp_path)
    v.save({"ALPACA_KEY_ID": "PKSECRET123", "ALPACA_FEED": "iex", "NAO_PERMITIDA": "x"})
    raw = db.get("vault")
    assert "PKSECRET123" not in raw                           # criptografado no banco
    import os
    assert os.environ["ALPACA_KEY_ID"] == "PKSECRET123"
    assert os.environ["ALPACA_FEED"] == "sip"                 # variável do servidor tem prioridade
    view = v.public_view()
    assert view["ALPACA_KEY_ID"]["set"] and "PKSECRET123" not in json.dumps(view)
    monkeypatch.delenv("ALPACA_KEY_ID")


# ------------------------------------------------------------------ IA local (Ollama/OpenAI-compatível simulados)
def test_llm_router_local_ollama_then_fallback(monkeypatch):
    import requests
    from app.llm import LLMRouter, RouterScorer
    monkeypatch.setenv("LLM_MODE", "auto")
    monkeypatch.setenv("LOCAL_LLM_URL", "http://gpu:11434")
    monkeypatch.setenv("LOCAL_LLM_KIND", "ollama")
    monkeypatch.setenv("LOCAL_LLM_MODEL", "qwen3:32b")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    calls = []

    class R:
        def __init__(self, data, status=200): self._d, self.status_code = data, status
        def json(self): return self._d
        def raise_for_status(self):
            if self.status_code >= 400: raise requests.HTTPError(str(self.status_code))

    def fake_get(url, **k):
        return R({"models": [{"name": "qwen3:32b"}]})

    def fake_post(url, json=None, **k):
        calls.append((url, json))
        assert json["format"]["type"] == "object" and json["stream"] is False
        content = '<think>pensando</think>{"items":[{"id":"a","ticker":"AAPL","topic":"EARN","sentiment":0.8,"relevance":0.9}]}'
        return R({"message": {"content": content}})

    monkeypatch.setattr(requests, "get", fake_get)
    monkeypatch.setattr(requests, "post", fake_post)
    r = LLMRouter()
    sc = RouterScorer(r, ["AAPL"]).score([{"id": "a", "ticker": "AAPL", "title": "Apple beats"}])
    assert sc[0].scorer == "IA local" and sc[0].sentiment == 0.8 and calls[0][0] == "http://gpu:11434/api/chat"
    assert "local" in r.active()

    def broken_post(url, json=None, **k):
        raise requests.ConnectionError("túnel caiu")
    monkeypatch.setattr(requests, "post", broken_post)
    sc = RouterScorer(r, ["AAPL"]).score([{"id": "b", "ticker": "AAPL", "title": "Apple misses estimates"}])
    assert sc[0].scorer == "dicionario" and sc[0].sentiment < 0 and "túnel" in r.last_error


def test_llm_router_openai_compatible(monkeypatch):
    import requests
    from app.llm import LLMRouter
    monkeypatch.setenv("LLM_MODE", "local")
    monkeypatch.setenv("LOCAL_LLM_URL", "http://lmstudio:1234")
    monkeypatch.setenv("LOCAL_LLM_KIND", "openai")
    monkeypatch.setenv("LOCAL_LLM_MODEL", "qwen")

    class R:
        status_code = 200
        def __init__(self, d): self._d = d
        def json(self): return self._d
        def raise_for_status(self): pass
    monkeypatch.setattr(requests, "get", lambda url, **k: R({"data": [{"id": "qwen"}]}))
    seen = {}
    def post(url, json=None, **k):
        seen.update(url=url, body=json)
        return R({"choices": [{"message": {"content": '{"sentiment": 0.5}'}}]})
    monkeypatch.setattr(requests, "post", post)
    out, prov = LLMRouter().json("s", "u", {"type": "object"})
    assert out == {"sentiment": 0.5} and prov == "local"
    assert seen["url"].endswith("/v1/chat/completions") and seen["body"]["response_format"]["type"] == "json_schema"


# ------------------------------------------------------------------ papel com dados reais
class FakeAlpaca:
    has_keys = True
    feed = "iex"
    def __init__(self): self.px = {"AAPL": 200.0, "BTCUSD": 70000.0}
    def snapshots(self, epics): return {e: {"bid": self.px[e], "ask": self.px[e], "last": self.px[e]} for e in epics}
    def candles(self, *a, **k): return []


def test_paper_real_broker_uses_real_quotes_and_persists(tmp_path, monkeypatch):
    from app import paper_real
    from app.db import AppDB
    monkeypatch.setattr(paper_real, "us_market_open", lambda at=None: True)
    db = AppDB(tmp_path / "a.db")
    data = FakeAlpaca()
    b = paper_real.PaperRealBroker(data, db, start_balance=60)
    b.update_quotes(data.snapshots(["AAPL"]))
    res = b.open_position("AAPL", "BUY", 1, 198.0, 203.0)
    assert res.ok and res.level > 200.0                        # compra no ask (com spread estimado)
    b2 = paper_real.PaperRealBroker(data, db, start_balance=60)  # "reinício do servidor"
    assert [p.deal_id for p in b2.positions()] == [res.deal_id]
    data.px["AAPL"] = 203.5
    closed = b2.update_quotes(data.snapshots(["AAPL"]))
    assert closed and closed[0]["reason"] == "alvo" and b2.cash == pytest.approx(60 + (203.0 - res.level))
    monkeypatch.setattr(paper_real, "us_market_open", lambda at=None: False)
    r = b2.open_position("AAPL", "BUY", 1, 198, 203)
    assert not r.ok and "fechado" in r.message
    b2.update_quotes(data.snapshots(["BTCUSD"]))
    assert b2.open_position("BTCUSD", "SELL", 0.001, 71000, 69000).ok   # cripto: 24h


def test_alpaca_parsing_and_pagination():
    from app.data.alpaca import AlpacaData

    class R:
        def __init__(self, d, code=200): self._d, self.status_code, self.text = d, code, ""
        def json(self): return self._d

    class H:
        def __init__(self): self.calls = []
        def get(self, url, params=None, headers=None, timeout=None):
            self.calls.append((url, dict(params), headers))
            if url.endswith("/v2/stocks/bars"):
                if "page_token" not in params:
                    return R({"bars": {"AAPL": [{"t": "2026-11-10T15:00:00Z", "o": 2, "h": 3, "l": 1, "c": 2.5, "v": 900}]},
                              "next_page_token": "p2"})
                return R({"bars": {"AAPL": [{"t": "2026-11-10T14:45:00Z", "o": 1, "h": 2, "l": 1, "c": 2, "v": 800}]},
                          "next_page_token": None})
            if url.endswith("/v2/stocks/snapshots"):
                return R({"AAPL": {"latestQuote": {"bp": 199.9, "ap": 200.1}, "latestTrade": {"p": 200.0, "t": "x"},
                                   "dailyBar": {"v": 12_000_000, "n": 90000, "vw": 199.5, "t": "2026-11-10T05:00:00Z"},
                                   "prevDailyBar": {"v": 40_000_000, "c": 198}}})
            return R({}, 404)

    h = H()
    a = AlpacaData("k", "s", "iex", http=h)
    c = a.candles("AAPL", "MINUTE_15", 2)
    assert [x.close for x in c] == [2, 2.5] and c[-1].volume == 900            # ordem cronológica, 2 páginas
    assert h.calls[0][2]["APCA-API-KEY-ID"] == "k" and h.calls[0][1]["feed"] == "iex"
    vol = a.consolidated_volume(["AAPL"])
    assert vol["AAPL"]["day_volume"] == 12_000_000 and vol["AAPL"]["delayed"] is True
    assert h.calls[-1][1]["feed"] == "delayed_sip"                                 # SIP com atraso no plano grátis
    snap = a.snapshots(["AAPL"])
    assert snap["AAPL"]["bid"] == 199.9 and snap["AAPL"]["ask"] == 200.1


def test_research_heuristic_without_ai():

    from app.research import ResearchAgent
    ra = ResearchAgent.__new__(ResearchAgent)
    d = {"preco": {"var_20d": 0.05, "acima_media_50": True, "acima_media_200": True},
         "noticias_48h": [{"relevance": 1, "sentiment": 0.8}], "stocktwits": {"bull_ratio": 0.8, "bullish": 8, "bearish": 2}}
    b = ra.heuristic(d)
    assert b["bias"] > 0.5 and b["confidence"] <= 0.3 and len(b["drivers"]) == 3


def test_session_fraction():
    from app.runtime import Runtime
    assert Runtime._session_fraction("2026-11-10T16:45:00Z") == pytest.approx(135 / 390)   # 11h45 em NY (horário de inverno)
    assert Runtime._session_fraction(None) == 1.0


def test_public_server_blocks_without_password_and_requires_login(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    for k, v in {"APP_MODE": "sim", "DATA_DIR": str(tmp_path), "DB_PATH": str(tmp_path / "t.db"), "WATCHLIST": "AAPL",
                 "LOCAL_LLM_URL": "", "ANTHROPIC_API_KEY": ""}.items():
        monkeypatch.setenv(k, v)
    monkeypatch.delenv("APP_PASSWORD", raising=False)
    from app.main import create_app
    from app.runtime import Runtime
    from trader.config import get_settings
    with TestClient(create_app(Runtime(get_settings()), public=True)) as c:
        assert c.get("/api/health").json()["blocked"] is True
        assert c.get("/api/state").status_code == 503
    monkeypatch.setenv("APP_PASSWORD", "abc123")
    with TestClient(create_app(Runtime(get_settings()), public=True)) as c:
        assert c.get("/api/state").status_code == 401
        assert c.post("/api/login", json={"password": "errada"}).status_code == 401
        tok = c.post("/api/login", json={"password": "abc123"}).json()["token"]
        assert c.get("/api/state", headers={"Authorization": f"Bearer {tok}"}).status_code == 200
        assert c.get(f"/api/candles?epic=AAPL&token={tok}").status_code == 200        # token na URL (SSE)
