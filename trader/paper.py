"""Corretora SIMULADA (offline): preços e manchetes sintéticas.

Serve para testar o bot e o painel sem conta nem internet. Os preços são um
passeio aleatório com mudanças de regime; as manchetes sintéticas carregam um
pouco de informação sobre o próximo regime, só para demonstrar o fluxo
notícia → sentimento → sinal. NÃO use os resultados daqui como prova de que a
estratégia funciona no mercado real.
"""
from __future__ import annotations

import itertools
import random
from datetime import datetime, timedelta, timezone

from .broker import Account, Candle, MarketRules, OrderResult, Position, Quote

BASE_PRICES = {"AAPL": 230.0, "MSFT": 420.0, "NVDA": 130.0, "GOOGL": 170.0,
               "AMZN": 190.0, "META": 560.0, "TSLA": 250.0, "AVGO": 170.0}

POS_TEMPLATES = [
    "{t} beats earnings estimates and raises full-year guidance",
    "{t} reports record cloud revenue growth, analysts upgrade",
    "{t} announces new product launch, strong early demand",
    "{t} expands buyback program after solid quarter",
]
NEG_TEMPLATES = [
    "{t} misses revenue estimates, cuts outlook",
    "{t} faces antitrust lawsuit from regulators",
    "{t} delays product launch amid supply chain problems",
    "{t} shares fall after analyst downgrade on weak demand",
]


class PaperBroker:
    name = "paper"

    def __init__(self, epics: list[str], start_balance: float = 60.0, seed: int | None = None,
                 history: int = 600, bar_minutes: int = 15):
        self.rng = random.Random(seed)
        self.cash = start_balance
        self.bar = timedelta(minutes=bar_minutes)
        self._ids = itertools.count(1)
        self._positions: dict[str, Position] = {}
        self.closed: list[tuple[str, str, float]] = []      # (deal_id, epic, pnl)
        self._series: dict[str, list[Candle]] = {}
        self._drift: dict[str, float] = {}
        self._pending_news: list[tuple[str, str]] = []      # (ticker, título)
        now = datetime.now(timezone.utc).replace(second=0, microsecond=0)
        for epic in epics:
            self._drift[epic] = 0.0
            price = BASE_PRICES.get(epic, 100.0)
            t0 = now - self.bar * history
            series: list[Candle] = []
            for i in range(history):
                price = self._step(epic, price, series, t0 + self.bar * i, emit_news=False)
            self._series[epic] = series

    # ---------------------------------------------------------------- simulação
    def _step(self, epic: str, price: float, series: list[Candle], t: datetime, emit_news: bool = True) -> float:
        if self.rng.random() < 0.03:                         # mudança de regime (≈ 1 a cada 33 barras)
            self._drift[epic] = self.rng.choice([-1, 1]) * self.rng.uniform(0.0004, 0.0012)
            if emit_news and self.rng.random() < 0.7:      # 70% das mudanças viram manchete
                tpl = POS_TEMPLATES if self._drift[epic] > 0 else NEG_TEMPLATES
                self._pending_news.append((epic, self.rng.choice(tpl).format(t=epic)))
        elif self.rng.random() < 0.05:
            self._drift[epic] *= 0.5
        vol = 0.0035
        o = price
        c = o * (1 + self._drift[epic] + self.rng.gauss(0, vol))
        h = max(o, c) * (1 + abs(self.rng.gauss(0, vol / 2)))
        lo = min(o, c) * (1 - abs(self.rng.gauss(0, vol / 2)))
        series.append(Candle(time=t.isoformat(), open=o, high=h, low=lo, close=c,
                             spread=c * 0.0005, volume=self.rng.randint(1000, 5000)))
        return c

    def advance(self, bars: int = 1) -> None:
        """Avança o relógio simulado e executa stops/alvos atingidos."""
        for _ in range(bars):
            for epic, series in self._series.items():
                last = series[-1]
                self._step(epic, last.close, series, datetime.fromisoformat(last.time) + self.bar)
                self._check_exits(epic, series[-1])

    def pop_news(self) -> list[tuple[str, str]]:
        news, self._pending_news = self._pending_news, []
        return news

    def _check_exits(self, epic: str, c: Candle) -> None:
        for p in [p for p in self._positions.values() if p.epic == epic]:
            exit_price = None
            if p.direction == "BUY":
                if p.stop_level is not None and c.low <= p.stop_level:
                    exit_price = p.stop_level
                elif p.profit_level is not None and c.high >= p.profit_level:
                    exit_price = p.profit_level
            else:
                if p.stop_level is not None and c.high >= p.stop_level:
                    exit_price = p.stop_level
                elif p.profit_level is not None and c.low <= p.profit_level:
                    exit_price = p.profit_level
            if exit_price is not None:
                self._realize(p, exit_price)

    def _realize(self, p: Position, price: float) -> float:
        sign = 1 if p.direction == "BUY" else -1
        pnl = (price - p.level) * p.size * sign
        self.cash += pnl
        self._positions.pop(p.deal_id, None)
        self.closed.append((p.deal_id, p.epic, pnl))
        return pnl

    def _mark(self, p: Position) -> float:
        q = self.quote(p.epic)
        exit_px = q.bid if p.direction == "BUY" else q.ask
        return (exit_px - p.level) * p.size * (1 if p.direction == "BUY" else -1)

    # ---------------------------------------------------------------- interface Broker
    def account(self) -> Account:
        upl = sum(self._mark(p) for p in self._positions.values())
        margin = sum(p.size * p.level * self.rules(p.epic).margin_factor for p in self._positions.values())
        return Account(balance=self.cash, equity=self.cash + upl, available=self.cash + upl - margin)

    def candles(self, epic: str, resolution: str = "MINUTE_15", count: int = 300) -> list[Candle]:
        return list(self._series[epic][-count:])

    def quote(self, epic: str) -> Quote:
        c = self._series[epic][-1]
        return Quote(bid=c.close - c.spread / 2, ask=c.close + c.spread / 2)

    def rules(self, epic: str) -> MarketRules:
        price = self._series[epic][-1].close
        return MarketRules(epic=epic, name=f"{epic} (simulado)", min_size=0.1, size_step=0.1,
                           min_stop_distance=price * 0.001, margin_factor=0.2)

    def positions(self) -> list[Position]:
        out = []
        for p in self._positions.values():
            p.upl = self._mark(p)
            out.append(p)
        return out

    def open_position(self, epic: str, direction: str, size: float,
                      stop_level: float, profit_level: float) -> OrderResult:
        q = self.quote(epic)
        level = q.ask if direction == "BUY" else q.bid
        deal_id = f"SIM{next(self._ids):05d}"
        self._positions[deal_id] = Position(deal_id=deal_id, epic=epic, direction=direction, size=size,
                                            level=level, upl=0.0, stop_level=stop_level,
                                            profit_level=profit_level)
        return OrderResult(True, deal_id, level, "ACCEPTED (simulado)")

    def close_position(self, deal_id: str) -> OrderResult:
        p = self._positions.get(deal_id)
        if not p:
            return OrderResult(False, deal_id, None, "posição não encontrada")
        q = self.quote(p.epic)
        self._realize(p, q.bid if p.direction == "BUY" else q.ask)
        return OrderResult(True, deal_id, None, "fechada (simulado)")
