"""Cliente REST da Capital.com — restrito à conta DEMO por segurança.

Documentação: https://open-api.capital.com/
- Sessão: POST /api/v1/session (headers CST + X-SECURITY-TOKEN, expira após 10 min sem uso)
- Limites: 10 req/s no geral; 1 req/s em /session; 1000 ordens/hora na demo.
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Any

import requests

from .broker import Account, Candle, MarketRules, OrderResult, Position, Quote

log = logging.getLogger(__name__)

DEMO_BASE_URL = "https://demo-api-capital.backend-capital.com"
SESSION_TTL = 9 * 60          # renova antes dos 10 minutos de inatividade
MIN_INTERVAL = 0.12           # ~8 req/s, abaixo do limite de 10 req/s


class CapitalError(RuntimeError):
    def __init__(self, status: int, body: str, path: str):
        super().__init__(f"Capital.com {status} em {path}: {body[:300]}")
        self.status = status
        self.body = body


def _num(obj: Any, default: float = 0.0) -> float:
    if isinstance(obj, dict):
        obj = obj.get("value")
    try:
        return float(obj)
    except (TypeError, ValueError):
        return default


class CapitalClient:
    name = "capital_demo"

    def __init__(self, api_key: str, identifier: str, password: str,
                 base_url: str = DEMO_BASE_URL, http: requests.Session | None = None):
        if "demo-api-capital" not in base_url:
            raise ValueError("Este bot só opera na conta DEMO da Capital.com.")
        if not (api_key and identifier and password):
            raise ValueError("Defina CAPITAL_API_KEY, CAPITAL_IDENTIFIER e CAPITAL_PASSWORD no .env")
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.identifier = identifier
        self.password = password
        self.http = http or requests.Session()
        self._cst: str | None = None
        self._token: str | None = None
        self._last_used = 0.0
        self._last_call = 0.0
        self._lock = threading.Lock()
        self._rules_cache: dict[str, MarketRules] = {}

    # ------------------------------------------------------------------ sessão
    def login(self) -> dict:
        r = self.http.post(
            f"{self.base_url}/api/v1/session",
            headers={"X-CAP-API-KEY": self.api_key, "Content-Type": "application/json"},
            json={"identifier": self.identifier, "password": self.password, "encryptedPassword": False},
            timeout=20,
        )
        if r.status_code != 200:
            raise CapitalError(r.status_code, r.text, "/api/v1/session")
        self._cst = r.headers.get("CST")
        self._token = r.headers.get("X-SECURITY-TOKEN")
        if not (self._cst and self._token):
            raise CapitalError(r.status_code, "resposta sem CST/X-SECURITY-TOKEN", "/api/v1/session")
        self._last_used = time.time()
        log.info("Sessão Capital.com (demo) aberta")
        return r.json()

    def _ensure_session(self) -> None:
        if not self._cst or time.time() - self._last_used > SESSION_TTL:
            self.login()

    def _request(self, method: str, path: str, *, params: dict | None = None,
                 json: dict | None = None, _retry: int = 0) -> Any:
        with self._lock:
            self._ensure_session()
            wait = MIN_INTERVAL - (time.time() - self._last_call)
            if wait > 0:
                time.sleep(wait)
            r = self.http.request(
                method, f"{self.base_url}{path}", params=params, json=json, timeout=20,
                headers={"CST": self._cst or "", "X-SECURITY-TOKEN": self._token or "",
                         "Content-Type": "application/json"},
            )
            self._last_call = time.time()
            self._last_used = self._last_call
        if r.status_code == 401 and _retry == 0:
            self._cst = None                       # sessão expirada → novo login
            return self._request(method, path, params=params, json=json, _retry=1)
        if r.status_code == 429 and _retry < 3:
            time.sleep(2 ** (_retry + 1))
            return self._request(method, path, params=params, json=json, _retry=_retry + 1)
        if r.status_code >= 400:
            raise CapitalError(r.status_code, r.text, path)
        return r.json() if r.content else {}

    def ping(self) -> dict:
        return self._request("GET", "/api/v1/ping")

    # ------------------------------------------------------------------ conta
    def account(self) -> Account:
        data = self._request("GET", "/api/v1/accounts")
        accounts = data.get("accounts", [])
        if not accounts:
            raise CapitalError(200, "nenhuma conta retornada", "/api/v1/accounts")
        acc = next((a for a in accounts if a.get("preferred")), accounts[0])
        b = acc.get("balance", {})
        balance = _num(b.get("balance"))
        pnl = _num(b.get("profitLoss"))
        return Account(balance=balance, equity=balance + pnl,
                       available=_num(b.get("available")), currency=acc.get("currency", "USD"))

    # ------------------------------------------------------------------ mercado
    def market(self, epic: str) -> dict:
        return self._request("GET", f"/api/v1/markets/{epic}")

    def search_markets(self, term: str) -> list[dict]:
        return self._request("GET", "/api/v1/markets", params={"searchTerm": term}).get("markets", [])

    def rules(self, epic: str) -> MarketRules:
        if epic in self._rules_cache:
            return self._rules_cache[epic]
        m = self.market(epic)
        dr, ins, snap = m.get("dealingRules", {}), m.get("instrument", {}), m.get("snapshot", {})
        price = (_num(snap.get("bid")) + _num(snap.get("offer"))) / 2 or 1.0
        stop = dr.get("minStopOrProfitDistance") or {}
        stop_dist = _num(stop)
        if str(stop.get("unit", "")).upper() == "PERCENTAGE":
            stop_dist = price * stop_dist / 100
        margin = _num(ins.get("marginFactor"), 20.0)
        if str(ins.get("marginFactorUnit", "PERCENTAGE")).upper() == "PERCENTAGE":
            margin = margin / 100
        rules = MarketRules(
            epic=epic,
            name=ins.get("name", epic),
            min_size=_num(dr.get("minDealSize"), 1.0),
            size_step=_num(dr.get("minSizeIncrement"), 0.0) or _num(dr.get("minDealSize"), 1.0),
            min_stop_distance=stop_dist,
            margin_factor=margin or 0.2,
            currency=ins.get("currency", "USD"),
            decimals=int(_num(snap.get("decimalPlacesFactor"), 2)),
        )
        self._rules_cache[epic] = rules
        return rules

    def quote(self, epic: str) -> Quote:
        snap = self.market(epic).get("snapshot", {})
        return Quote(bid=_num(snap.get("bid")), ask=_num(snap.get("offer")),
                     tradeable=snap.get("marketStatus") == "TRADEABLE")

    def candles(self, epic: str, resolution: str = "MINUTE_15", count: int = 300) -> list[Candle]:
        """Busca `count` barras. A API entrega até 1000 por chamada; acima disso pagina para trás."""
        out = self._candles_page(epic, resolution, min(count, 1000))
        pages = 1
        while len(out) < count and out and pages < 20:
            oldest = out[0].time[:19]
            try:
                older = self._candles_page(epic, resolution, min(count - len(out), 1000), to=oldest)
            except CapitalError as e:
                log.warning("Paginação de %s parou: %s", epic, e)
                break
            older = [c for c in older if c.time[:19] < oldest]
            if not older:
                break
            out = older + out
            pages += 1
        return out

    def _candles_page(self, epic: str, resolution: str, count: int, to: str | None = None) -> list[Candle]:
        params = {"resolution": resolution, "max": count}
        if to:
            params["to"] = to
        data = self._request("GET", f"/api/v1/prices/{epic}", params=params)
        out = []
        for p in data.get("prices", []):
            def mid(k: str) -> float:
                x = p.get(k, {})
                return (_num(x.get("bid")) + _num(x.get("ask"))) / 2
            c = p.get("closePrice", {})
            out.append(Candle(
                time=p.get("snapshotTimeUTC") or p.get("snapshotTime", ""),
                open=mid("openPrice"), high=mid("highPrice"), low=mid("lowPrice"), close=mid("closePrice"),
                spread=abs(_num(c.get("ask")) - _num(c.get("bid"))),
                volume=_num(p.get("lastTradedVolume")),
            ))
        return out

    # ------------------------------------------------------------------ ordens
    def positions(self) -> list[Position]:
        out = []
        for item in self._request("GET", "/api/v1/positions").get("positions", []):
            p, m = item.get("position", {}), item.get("market", {})
            out.append(Position(
                deal_id=p.get("dealId", ""), epic=m.get("epic", ""), direction=p.get("direction", ""),
                size=_num(p.get("size")), level=_num(p.get("level")), upl=_num(p.get("upl")),
                stop_level=p.get("stopLevel"), profit_level=p.get("profitLevel"),
            ))
        return out

    def confirm(self, deal_reference: str) -> dict:
        return self._request("GET", f"/api/v1/confirms/{deal_reference}")

    def open_position(self, epic: str, direction: str, size: float,
                      stop_level: float, profit_level: float) -> OrderResult:
        body = {"epic": epic, "direction": direction, "size": size, "guaranteedStop": False,
                "stopLevel": stop_level, "profitLevel": profit_level}
        try:
            ref = self._request("POST", "/api/v1/positions", json=body).get("dealReference")
            if not ref:
                return OrderResult(False, None, None, "sem dealReference na resposta")
            conf = {}
            for _ in range(5):                       # a confirmação pode demorar alguns ms
                try:
                    conf = self.confirm(ref)
                    break
                except CapitalError as e:
                    if e.status != 404:
                        raise
                    time.sleep(0.5)
            status = conf.get("dealStatus", "UNKNOWN")
            deals = conf.get("affectedDeals") or []
            deal_id = deals[0].get("dealId") if deals else conf.get("dealId")
            ok = status == "ACCEPTED"
            return OrderResult(ok, deal_id, _num(conf.get("level")) or None,
                               f"{status} {conf.get('reason', '')}".strip())
        except CapitalError as e:
            return OrderResult(False, None, None, str(e))

    def close_position(self, deal_id: str) -> OrderResult:
        try:
            ref = self._request("DELETE", f"/api/v1/positions/{deal_id}").get("dealReference")
            return OrderResult(True, deal_id, None, f"fechada ({ref})")
        except CapitalError as e:
            return OrderResult(False, deal_id, None, str(e))
