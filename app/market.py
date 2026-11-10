"""Dados de mercado: cotações ao vivo (WebSocket da Capital.com ou simulador) e
candles com cache por resolução."""
from __future__ import annotations

import asyncio
import json
import logging
import time

import pandas as pd

from trader.capital import STREAM_URL
from trader.features import to_frame

from .bus import EventBus

log = logging.getLogger(__name__)

# por quanto tempo um conjunto de candles pode ser reutilizado (segundos)
CANDLE_TTL = {"MINUTE": 20, "MINUTE_5": 45, "MINUTE_15": 90, "MINUTE_30": 150,
              "HOUR": 300, "HOUR_4": 900, "DAY": 1800, "WEEK": 3600}


class MarketData:
    def __init__(self, broker, epics: list[str], bus: EventBus):
        self.broker = broker
        self.epics = epics
        self.bus = bus
        self.quotes: dict[str, dict] = {}
        self._cache: dict[tuple[str, str], tuple[float, pd.DataFrame]] = {}
        self._last_pub: dict[str, float] = {}
        self.stream_status = "parado"
        self.is_sim = getattr(broker, "name", "") == "simulador"

    # ------------------------------------------------------------- cotações
    def on_quote(self, epic: str, bid: float, ask: float, ts: float | None = None) -> None:
        prev = self.quotes.get(epic)
        q = {"epic": epic, "bid": bid, "ask": ask, "mid": (bid + ask) / 2,
             "time": ts or time.time(), "open": (prev or {}).get("open", (bid + ask) / 2)}
        self.quotes[epic] = q
        now = time.time()
        if now - self._last_pub.get(epic, 0) >= 1.0:          # no máximo 1 atualização/s por ativo
            self._last_pub[epic] = now
            self.bus.publish("quote", q)

    def mid(self, epic: str) -> float | None:
        q = self.quotes.get(epic)
        return q["mid"] if q else None

    async def run_sim_ticks(self, on_closed) -> None:
        self.stream_status = "simulador ao vivo"
        while True:
            closed = self.broker.step()
            for epic, q in self.broker.quotes().items():
                self.on_quote(epic, q.bid, q.ask)
            if closed:
                on_closed(closed)
            await asyncio.sleep(1)

    async def run_capital_stream(self) -> None:
        """WebSocket oficial: marketData.subscribe (máx. 40 ativos) + ping a cada 5 min."""
        import websockets

        backoff = 2
        epics = self.epics[:40]
        while True:
            try:
                cst, token = await asyncio.to_thread(self.broker.session_tokens)
                async with websockets.connect(STREAM_URL, ping_interval=None, open_timeout=20) as ws:
                    await ws.send(json.dumps({"destination": "marketData.subscribe", "correlationId": "1",
                                              "cst": cst, "securityToken": token, "payload": {"epics": epics}}))
                    self.stream_status = "conectado (WebSocket)"
                    self.bus.publish("status", {"stream": self.stream_status})
                    backoff = 2
                    last_ping = time.time()
                    cid = 2
                    while True:
                        try:
                            raw = await asyncio.wait_for(ws.recv(), timeout=30)
                        except asyncio.TimeoutError:
                            raw = None
                        if time.time() - last_ping > 300:
                            cst, token = await asyncio.to_thread(self.broker.session_tokens)
                            await ws.send(json.dumps({"destination": "ping", "correlationId": str(cid),
                                                      "cst": cst, "securityToken": token}))
                            cid += 1
                            last_ping = time.time()
                        if not raw:
                            continue
                        msg = json.loads(raw)
                        if msg.get("destination") == "quote":
                            p = msg.get("payload", {})
                            if p.get("bid") is not None and p.get("ofr") is not None:
                                self.on_quote(p["epic"], float(p["bid"]), float(p["ofr"]),
                                              (p.get("timestamp") or 0) / 1000 or None)
                        elif msg.get("status") not in (None, "OK"):
                            log.warning("WebSocket: %s", msg)
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                self.stream_status = f"reconectando ({e.__class__.__name__})"
                self.bus.publish("status", {"stream": self.stream_status})
                log.warning("WebSocket de preços caiu: %s — nova tentativa em %ss", e, backoff)
                await self._poll_quotes_once()            # mantém preços vivos enquanto reconecta
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60)

    async def _poll_quotes_once(self) -> None:
        for e in self.epics:
            try:
                q = await asyncio.to_thread(self.broker.quote, e)
                self.on_quote(e, q.bid, q.ask)
            except Exception:  # noqa: BLE001
                pass

    # ------------------------------------------------------------- candles
    async def candles(self, epic: str, resolution: str, count: int = 300) -> pd.DataFrame:
        key = (epic, resolution)
        ttl = 0 if self.is_sim else CANDLE_TTL.get(resolution, 60)
        hit = self._cache.get(key)
        if hit and time.time() - hit[0] < ttl and len(hit[1]) >= min(count, 50):
            df = hit[1]
        else:
            raw = await asyncio.to_thread(self.broker.candles, epic, resolution, count)
            df = to_frame(raw)
            self._cache[key] = (time.time(), df)
        # usa a cotação ao vivo para atualizar o candle em formação
        m = self.mid(epic)
        if m is not None and len(df) and not self.is_sim:
            df = df.copy()
            i = df.index[-1]
            df.at[i, "close"] = m
            df.at[i, "high"] = max(df.at[i, "high"], m)
            df.at[i, "low"] = min(df.at[i, "low"], m)
        return df.tail(count).reset_index(drop=True)
