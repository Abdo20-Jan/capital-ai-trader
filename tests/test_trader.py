from datetime import datetime, timedelta, timezone

import pandas as pd
import pytest

from trader.broker import Account, Candle, MarketRules
from trader.capital import CapitalClient
from trader.config import Settings, WatchItem, parse_watchlist
from trader.engine import Engine, aggregate_sentiment
from trader.features import triple_barrier_labels
from trader.paper import PaperBroker
from trader.risk import Guard, position_size
from trader.sentiment import LexiconScorer
from trader.store import Store

RULES = MarketRules(epic="X", name="X", min_size=0.1, size_step=0.1, min_stop_distance=0.1, margin_factor=0.2)


# ------------------------------------------------------------------ risco
def test_position_size_risks_fixed_fraction():
    acct = Account(balance=60, equity=60, available=60)
    d = position_size(acct, RULES, price=100, stop_distance=1.0, risk_per_trade=0.02,
                      max_risk_per_trade=0.05, max_margin_usage=0.5)
    assert d.ok and d.size == pytest.approx(1.2)          # 2% de 60 = 1.20 / 1.0
    assert d.risk_amount == pytest.approx(1.2)


def test_position_size_refuses_when_min_size_too_risky():
    rules = MarketRules(epic="X", name="X", min_size=1, size_step=1, min_stop_distance=0.1, margin_factor=0.2)
    acct = Account(balance=60, equity=60, available=60)
    d = position_size(acct, rules, price=200, stop_distance=5.0, risk_per_trade=0.02,
                      max_risk_per_trade=0.05, max_margin_usage=0.5)
    assert not d.ok and "tamanho mínimo" in d.reason       # 1 × 5 = $5 > 5% de $60


def test_position_size_caps_margin():
    acct = Account(balance=60, equity=60, available=60)
    d = position_size(acct, RULES, price=1000, stop_distance=0.5, risk_per_trade=0.02,
                      max_risk_per_trade=0.05, max_margin_usage=0.5)
    # sem limite seria 2.4 unidades (margem $480); limite = 30 / (1000*0.2) = 0.15 → 0.1
    assert d.ok and d.size == pytest.approx(0.1) and d.margin <= 30


def test_guard_target_and_daily_loss():
    g = Guard(start_balance=60, target_balance=100, max_daily_loss=0.06)
    assert g.check(60, "d1")[0]
    assert not g.check(56, "d1")[0]                         # -6.7% no dia
    assert g.check(56, "d2")[0]                             # novo dia, nova base
    ok, msg = g.check(101, "d2")
    assert not ok and "META" in msg


# ------------------------------------------------------------------ features / sentimento
def test_triple_barrier():
    df = pd.DataFrame({"high": [10, 10.5, 12.5, 10], "low": [10, 9.8, 10, 9], "close": [10, 10, 12, 9]})
    atr = pd.Series([1.0] * 4)
    lab = triple_barrier_labels(df, atr, up_k=2, down_k=1, horizon=3)
    assert lab[0] == 1          # 12.5 >= 12 antes de 9
    assert lab[2] == 0          # cai para 9 (12 - 1×1 = 11)


def test_lexicon_scorer():
    s = LexiconScorer().score([
        {"id": "a", "ticker": "AAPL", "title": "Apple beats estimates and raises guidance"},
        {"id": "b", "ticker": "AAPL", "title": "Apple faces antitrust lawsuit, shares fall"},
    ])
    assert s[0].sentiment > 0 and s[0].topic == "EARN"
    assert s[1].sentiment < 0 and s[1].topic in ("REG", "LEGAL")


def test_aggregate_sentiment_decays_and_weights():
    now = datetime.now(timezone.utc)
    fresh = {"time": now.isoformat(), "sentiment": 1.0, "relevance": 1.0}
    old = {"time": (now - timedelta(hours=4)).isoformat(), "sentiment": -1.0, "relevance": 1.0}
    assert aggregate_sentiment([fresh, old], now) > 0.5      # notícia nova pesa 4× mais
    assert aggregate_sentiment([], now) == 0.0


def test_parse_watchlist():
    w = parse_watchlist("AAPL, US100:QQQ")
    assert w[0].epic == w[0].ticker == "AAPL"
    assert (w[1].epic, w[1].ticker) == ("US100", "QQQ")


# ------------------------------------------------------------------ cliente Capital.com (HTTP simulado)
class FakeResp:
    def __init__(self, status=200, body=None, headers=None):
        self.status_code, self._body, self.headers = status, body or {}, headers or {}
        self.content = b"x"
        self.text = str(self._body)

    def json(self):
        return self._body


class FakeHTTP:
    def __init__(self):
        self.calls = []
        self.sessions = 0

    def post(self, url, headers=None, json=None, timeout=None):
        self.sessions += 1
        assert headers["X-CAP-API-KEY"] == "key"
        assert json == {"identifier": "me", "password": "pw", "encryptedPassword": False}
        return FakeResp(200, {"accountInfo": {}}, {"CST": "cst", "X-SECURITY-TOKEN": "tok"})

    def request(self, method, url, params=None, json=None, timeout=None, headers=None):
        self.calls.append((method, url.split(".com")[1], params, json))
        assert headers["CST"] == "cst" and headers["X-SECURITY-TOKEN"] == "tok"
        path = url.split(".com")[1]
        if path == "/api/v1/accounts":
            return FakeResp(200, {"accounts": [{"preferred": True, "currency": "USD",
                                                "balance": {"balance": 60, "profitLoss": 1.5, "available": 50}}]})
        if path == "/api/v1/markets/AAPL":
            return FakeResp(200, {"instrument": {"name": "Apple", "marginFactor": 20, "marginFactorUnit": "PERCENTAGE"},
                                  "dealingRules": {"minDealSize": {"unit": "POINTS", "value": 1},
                                                   "minSizeIncrement": {"value": 0.1},
                                                   "minStopOrProfitDistance": {"unit": "PERCENTAGE", "value": 0.5}},
                                  "snapshot": {"bid": 199.9, "offer": 200.1, "marketStatus": "TRADEABLE",
                                               "decimalPlacesFactor": 2}})
        if path == "/api/v1/prices/AAPL":
            p = {"bid": 1, "ask": 1.2}
            return FakeResp(200, {"prices": [{"snapshotTimeUTC": "2026-11-10T10:00:00", "openPrice": p,
                                              "closePrice": p, "highPrice": p, "lowPrice": p}]})
        if path == "/api/v1/positions" and method == "POST":
            return FakeResp(200, {"dealReference": "o_123"})
        if path == "/api/v1/confirms/o_123":
            return FakeResp(200, {"dealStatus": "ACCEPTED", "level": 200.1,
                                  "affectedDeals": [{"dealId": "D1", "status": "OPENED"}]})
        return FakeResp(404, {"errorCode": "not found"})


def test_capital_client_flow():
    http = FakeHTTP()
    c = CapitalClient("key", "me", "pw", http=http)
    acct = c.account()
    assert acct.equity == pytest.approx(61.5) and acct.available == 50
    r = c.rules("AAPL")
    assert r.min_size == 1 and r.size_step == 0.1 and r.margin_factor == pytest.approx(0.2)
    assert r.min_stop_distance == pytest.approx(1.0)        # 0.5% de 200
    assert c.candles("AAPL", "MINUTE_15", 10)[0].close == pytest.approx(1.1)
    res = c.open_position("AAPL", "BUY", 1, 198.0, 203.0)
    assert res.ok and res.deal_id == "D1"
    post = [x for x in http.calls if x[0] == "POST"][0]
    assert post[3] == {"epic": "AAPL", "direction": "BUY", "size": 1, "guaranteedStop": False,
                       "stopLevel": 198.0, "profitLevel": 203.0}
    assert http.sessions == 1


def test_capital_client_refuses_live_url():
    with pytest.raises(ValueError):
        CapitalClient("k", "i", "p", base_url="https://api-capital.backend-capital.com")


# ------------------------------------------------------------------ ponta a ponta (simulador)
def test_engine_end_to_end_paper(tmp_path):
    s = Settings(watchlist=[WatchItem("AAPL", "AAPL"), WatchItem("NVDA", "NVDA")],
                 db_path=tmp_path / "t.db", model_dir=tmp_path / "models")
    broker = PaperBroker(["AAPL", "NVDA"], start_balance=60, seed=3, history=1000)
    store = Store(s.db_path)
    eng = Engine(s, broker, store, LexiconScorer())
    try:
        eng.retrain()
    except ValueError:
        pass
    for _ in range(40):
        eng.step()
    assert store.rows("SELECT COUNT(*) n FROM signals")[0]["n"] >= 40
    assert store.rows("SELECT COUNT(*) n FROM equity")[0]["n"] == 40
    for t in store.rows("SELECT * FROM trades"):
        if t["direction"] == "BUY":
            assert t["stop"] < t["entry"] < t["target"]
        else:
            assert t["target"] < t["entry"] < t["stop"]
        assert t["risk"] <= 60 * s.max_risk_per_trade + 1e-9


# ------------------------------------------------------------------ Claude (cliente simulado)
def test_claude_scorer_parses_and_falls_back():
    import json
    from types import SimpleNamespace

    from trader.sentiment import ClaudeScorer

    sc = ClaudeScorer(api_key="test", watchlist=["AAPL"])
    captured = {}

    def fake_create(**kw):
        captured.update(kw)
        out = {"items": [{"id": "a", "ticker": "AAPL", "topic": "EARN", "sentiment": 1.7, "relevance": 0.9}]}
        return SimpleNamespace(stop_reason="end_turn", content=[SimpleNamespace(type="text", text=json.dumps(out))])

    sc.client = SimpleNamespace(beta=SimpleNamespace(messages=SimpleNamespace(create=fake_create)))
    res = sc.score([{"id": "a", "ticker": "AAPL", "title": "Apple beats"},
                    {"id": "b", "ticker": "AAPL", "title": "Apple misses estimates"}])
    assert res[0].scorer == "claude" and res[0].sentiment == 1.0       # limitado a [-1, 1]
    assert res[1].scorer == "dicionario" and res[1].sentiment < 0      # item ausente → fallback
    assert captured["output_config"]["format"]["type"] == "json_schema"
    assert captured["model"] == "claude-opus-5-5"


def test_exposure_cap_with_200x_leverage():
    rules = MarketRules(epic="EURUSD", name="EUR/USD", min_size=100, size_step=100,
                        min_stop_distance=0.0001, margin_factor=1 / 200)
    acct = Account(balance=60, equity=60, available=60)
    # stop de 2 pips: pelo risco seriam 6.000 unidades (~$6.500 de exposição, ~108×)
    d = position_size(acct, rules, price=1.08, stop_distance=0.0002, risk_per_trade=0.02,
                      max_risk_per_trade=0.05, max_margin_usage=0.5, max_exposure=30)
    assert d.ok and d.notional <= 60 * 30 + 1e-6
    assert d.risk_amount < 1.2            # exposição limitada → risco menor que 2%
    assert d.margin == pytest.approx(d.notional / 200)


# ------------------------------------------------------------------ banca virtual / modo aprendizado
def test_virtual_bankroll_on_big_demo_account(tmp_path):
    s = Settings(watchlist=[WatchItem("AAPL", "AAPL")], db_path=tmp_path / "t.db", model_dir=tmp_path / "m")
    eng = Engine(s, PaperBroker(["AAPL"], seed=1), Store(s.db_path), LexiconScorer())
    v = eng.virtual_account(Account(balance=10_000, equity=10_000, available=10_000))
    assert v.equity == pytest.approx(60) and v.available == pytest.approx(60)
    v = eng.virtual_account(Account(balance=10_000, equity=10_012, available=9_990))
    assert v.equity == pytest.approx(72)                 # +$12 de lucro desde a referência
    assert v.available == pytest.approx(50)              # 72 − 22 de margem em uso
    ok, _ = eng.guard.check(v.equity, "d1")
    assert ok                                            # não confunde $10.000 com "meta atingida"


def test_learning_mode_disables_daily_stop_and_target():
    g = Guard(start_balance=60, target_balance=100, max_daily_loss=0, stop_at_target=False)
    assert g.check(60, "d1")[0]
    assert g.check(50, "d1")[0]                          # −17% no dia, sem pausa
    assert g.check(150, "d1")[0]                         # passou da meta, continua


def test_candles_pagination():
    from trader.capital import CapitalClient

    class PagingHTTP(FakeHTTP):
        def request(self, method, url, params=None, json=None, timeout=None, headers=None):
            if "/api/v1/prices/" not in url:
                return super().request(method, url, params, json, timeout, headers)
            self.calls.append(params)
            end = datetime(2026, 11, 10, 12, 0)
            if params.get("to"):
                end = datetime.fromisoformat(params["to"]) - timedelta(minutes=15)
            n = min(params["max"], 1000)
            start = end - timedelta(minutes=15 * (n - 1))
            if start < datetime(2026, 9, 1):                     # histórico "acaba" aqui
                start = datetime(2026, 9, 1)
            p = {"bid": 1, "ask": 1}
            prices, t = [], start
            while t <= end:
                prices.append({"snapshotTimeUTC": t.isoformat(), "openPrice": p, "closePrice": p,
                               "highPrice": p, "lowPrice": p})
                t += timedelta(minutes=15)
            return FakeResp(200, {"prices": prices})

    http = PagingHTTP()
    c = CapitalClient("key", "me", "pw", http=http)
    bars = c.candles("AAPL", "MINUTE_15", 2500)
    assert len(bars) == 2500
    times = [b.time for b in bars]
    assert times == sorted(times) and len(set(times)) == len(times)
    assert len(http.calls) == 3
    assert len(c.candles("AAPL", "MINUTE_15", 9000)) < 9000           # para quando o histórico acaba
