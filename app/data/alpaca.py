"""Dados de mercado REAIS via API oficial da Alpaca (https://docs.alpaca.markets).

Plano grátis (Basic): cotações em tempo real da bolsa IEX + dados CONSOLIDADOS de todas
as bolsas dos EUA (SIP) com 15 min de atraso, 200 chamadas/min.
Plano pago (Algo Trader Plus): SIP em tempo real. Defina ALPACA_FEED=sip.
Cripto (BTC/USD...): histórico sem chave.
"""
from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timedelta, timezone

import requests

from trader.broker import Candle, Quote

log = logging.getLogger(__name__)

BASE = "https://data.alpaca.markets"
TIMEFRAME = {"MINUTE": ("1Min", 1), "MINUTE_5": ("5Min", 5), "MINUTE_15": ("15Min", 15), "MINUTE_30": ("30Min", 30),
             "HOUR": ("1Hour", 60), "HOUR_4": ("4Hour", 240), "DAY": ("1Day", 1440), "WEEK": ("1Week", 10080)}

# EPIC da Capital.com → símbolo na Alpaca (ações dos EUA usam o mesmo ticker)
CRYPTO = {"BTCUSD": "BTC/USD", "ETHUSD": "ETH/USD", "SOLUSD": "SOL/USD", "LTCUSD": "LTC/USD", "XRPUSD": "XRP/USD"}


def is_crypto(epic: str) -> bool:
    return epic.upper() in CRYPTO


class AlpacaError(RuntimeError):
    pass


class AlpacaData:
    def __init__(self, key_id: str = "", secret: str = "", feed: str = "iex", http: requests.Session | None = None):
        self.key_id, self.secret = key_id.strip(), secret.strip()
        self.feed = (feed or "iex").lower()                    # iex (grátis) ou sip (pago)
        self.http = http or requests.Session()
        self._lock = threading.Lock()
        self._calls: list[float] = []
        self.max_per_min = 190 if self.feed != "sip" else 9000
        self.last_error = ""

    @property
    def has_keys(self) -> bool:
        return bool(self.key_id and self.secret)

    @property
    def realtime_volume(self) -> bool:
        return self.feed == "sip"

    def supports(self, epic: str) -> bool:
        return is_crypto(epic) or self.has_keys

    # ------------------------------------------------------------ HTTP
    def _get(self, path: str, params: dict, auth: bool = True) -> dict:
        with self._lock:                                       # respeita o limite de chamadas/min
            now = time.time()
            self._calls = [t for t in self._calls if now - t < 60]
            if len(self._calls) >= self.max_per_min:
                time.sleep(max(0.0, 60 - (now - self._calls[0])) + 0.05)
            self._calls.append(time.time())
        headers = {"Accept": "application/json"}
        if auth:
            if not self.has_keys:
                raise AlpacaError("chaves da Alpaca não configuradas")
            headers.update({"APCA-API-KEY-ID": self.key_id, "APCA-API-SECRET-KEY": self.secret})
        r = self.http.get(BASE + path, params=params, headers=headers, timeout=15)
        if r.status_code == 429:
            time.sleep(3)
            r = self.http.get(BASE + path, params=params, headers=headers, timeout=15)
        if r.status_code >= 400:
            self.last_error = f"Alpaca {r.status_code}: {r.text[:200]}"
            raise AlpacaError(self.last_error)
        return r.json()

    # ------------------------------------------------------------ candles
    def candles(self, epic: str, resolution: str = "MINUTE_15", count: int = 300,
                feed: str | None = None, end: datetime | None = None) -> list[Candle]:
        tf, minutes = TIMEFRAME[resolution]
        # janela generosa (fins de semana, feriados, pregão de 6,5 h)
        span = timedelta(minutes=minutes * count * (1 if is_crypto(epic) else 5)) + timedelta(days=5)
        start = (datetime.now(timezone.utc) - span).strftime("%Y-%m-%dT%H:%M:%SZ")
        if is_crypto(epic):
            sym, path, auth = CRYPTO[epic.upper()], "/v1beta3/crypto/us/bars", False
            params = {"symbols": sym, "timeframe": tf, "start": start, "limit": min(count, 10000), "sort": "desc"}
        else:
            sym, path, auth = epic, "/v2/stocks/bars", True
            params = {"symbols": epic, "timeframe": tf, "start": start, "limit": min(count, 10000), "sort": "desc",
                      "adjustment": "split", "feed": feed or self.feed}
        if end is not None:
            params["end"] = end.strftime("%Y-%m-%dT%H:%M:%SZ")
        bars: list[dict] = []
        for _ in range(20):                                    # a API pagina mesmo abaixo do limite
            data = self._get(path, params, auth=auth)
            bars += (data.get("bars") or {}).get(sym, [])
            token = data.get("next_page_token")
            if not token or len(bars) >= count:
                break
            params = {**params, "page_token": token}
        bars = list(reversed(bars[:count]))
        return [Candle(time=b["t"], open=b["o"], high=b["h"], low=b["l"], close=b["c"], volume=b.get("v", 0) or 0)
                for b in bars]

    # ------------------------------------------------------------ cotações e volume
    def snapshots(self, epics: list[str]) -> dict[str, dict]:
        """Preço em tempo real (IEX no plano grátis) + volume do dia."""
        out: dict[str, dict] = {}
        stocks = [e for e in epics if not is_crypto(e)]
        if stocks and self.has_keys:
            data = self._get("/v2/stocks/snapshots", {"symbols": ",".join(stocks), "feed": self.feed})
            for sym, s in (data or {}).items():
                q, t = s.get("latestQuote") or {}, s.get("latestTrade") or {}
                bid, ask, last = q.get("bp") or 0, q.get("ap") or 0, t.get("p") or 0
                if not bid or not ask or ask < bid or (last and abs(ask - bid) > last * 0.01):
                    bid = ask = last                         # cotação IEX às vezes vem vazia/larga
                out[sym] = {"bid": bid, "ask": ask, "last": last, "time": t.get("t") or q.get("t"),
                            "day_volume": (s.get("dailyBar") or {}).get("v"),
                            "prev_volume": (s.get("prevDailyBar") or {}).get("v"),
                            "prev_close": (s.get("prevDailyBar") or {}).get("c"), "feed": self.feed}
        for e in epics:
            if is_crypto(e):
                try:
                    c = self.candles(e, "MINUTE", 2)
                    if c:
                        out[e] = {"bid": c[-1].close, "ask": c[-1].close, "last": c[-1].close, "time": c[-1].time,
                                  "day_volume": None, "prev_close": None, "feed": "crypto"}
                except AlpacaError:
                    pass
        return out

    def consolidated_volume(self, epics: list[str]) -> dict[str, dict]:
        """Volume CONSOLIDADO (todas as bolsas dos EUA, SIP). Grátis = atraso de 15 min."""
        stocks = [e for e in epics if not is_crypto(e)]
        if not stocks or not self.has_keys:
            return {}
        feed = "sip" if self.feed == "sip" else "delayed_sip"
        data = self._get("/v2/stocks/snapshots", {"symbols": ",".join(stocks), "feed": feed})
        out = {}
        for sym, s in (data or {}).items():
            d, p = s.get("dailyBar") or {}, s.get("prevDailyBar") or {}
            out[sym] = {"day_volume": d.get("v"), "day_trades": d.get("n"), "vwap": d.get("vw"),
                        "prev_volume": p.get("v"), "as_of": d.get("t") or (s.get("minuteBar") or {}).get("t"),
                        "delayed": feed == "delayed_sip"}
        return out

    def sip_candles(self, epic: str, resolution: str, count: int) -> list[Candle]:
        """Candles com volume CONSOLIDADO (todas as bolsas). Plano grátis: até 15 min atrás."""
        if is_crypto(epic):
            return self.candles(epic, resolution, count)
        if self.feed == "sip":
            return self.candles(epic, resolution, count, feed="sip")
        return self.candles(epic, resolution, count, feed="sip",
                            end=datetime.now(timezone.utc) - timedelta(minutes=16))

    def quote(self, epic: str) -> Quote:
        s = self.snapshots([epic]).get(epic)
        if not s:
            raise AlpacaError(f"sem cotação para {epic}")
        return Quote(bid=s["bid"], ask=s["ask"])

    # ------------------------------------------------------------ notícias
    def news(self, symbols: list[str], limit: int = 50, since: str | None = None) -> list[dict]:
        if not self.has_keys:
            return []
        params = {"symbols": ",".join(symbols), "limit": min(limit, 50), "sort": "desc"}
        if since:
            params["start"] = since
        data = self._get("/v1beta1/news", params)
        return data.get("news", [])
