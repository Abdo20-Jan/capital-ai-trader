"""Corretora simulada em TEMPO REAL (sem conta): ticks a cada segundo, candles em
todas as resoluções, stops/alvos executados tick a tick e manchetes sintéticas.

Serve para usar o app inteiro antes de conectar a Capital.com. Os preços são
aleatórios — nada aqui prova que uma estratégia funciona no mercado real.
"""
from __future__ import annotations

import itertools
import math
import random
import threading
import time
from datetime import datetime, timezone

from trader.broker import Account, Candle, MarketRules, OrderResult, Position, Quote

RES_MIN = {"MINUTE": 1, "MINUTE_5": 5, "MINUTE_15": 15, "MINUTE_30": 30,
           "HOUR": 60, "HOUR_4": 240, "DAY": 1440, "WEEK": 10080}

BASE = {"AAPL": 230.0, "MSFT": 420.0, "NVDA": 130.0, "GOOGL": 170.0, "AMZN": 190.0, "META": 560.0,
        "TSLA": 250.0, "AVGO": 170.0, "US500": 5800.0, "US100": 20500.0, "GOLD": 2650.0,
        "EURUSD": 1.08, "GBPUSD": 1.27, "USDJPY": 152.0, "BTCUSD": 76000.0, "OIL_CRUDE": 71.0}

POS = ["{t} beats earnings estimates and raises full-year guidance",
       "{t} reports record cloud revenue growth, analysts upgrade",
       "{t} announces new product launch, strong early demand",
       "{t} expands buyback program after solid quarter",
       "Analysts upgrade {t} citing accelerating AI demand"]
NEG = ["{t} misses revenue estimates, cuts outlook",
       "{t} faces antitrust lawsuit from regulators",
       "{t} delays product launch amid supply chain problems",
       "{t} shares fall after analyst downgrade on weak demand",
       "Regulators open probe into {t} business practices"]
NOISE = ["What to watch in {t} stock this week", "{t}: 3 things investors should know",
         "Is {t} a buy? Here's what analysts say", "{t} trading volume rises in afternoon session"]


def _is_fx(epic: str) -> bool:
    return len(epic) == 6 and epic.isalpha() and epic.isupper() and epic not in BASE or epic in ("EURUSD", "GBPUSD", "USDJPY")


class _Book:
    """Estado de um ativo: preço, regime e candles por resolução."""

    def __init__(self, epic: str, rng: random.Random, now_s: float, history: int):
        self.epic = epic
        self.rng = rng
        self.price = BASE.get(epic, 100.0) * rng.uniform(0.9, 1.1)
        self.sigma_min = 0.0002 if _is_fx(epic) else 0.0007     # volatilidade por minuto
        self.drift_min = 0.0
        self.spread_pct = 0.00006 if _is_fx(epic) else 0.00015
        self.candles: dict[str, list[Candle]] = {}
        for res, m in RES_MIN.items():
            self.candles[res] = self._history(res, m, now_s, history)

    def _history(self, res: str, minutes: int, now_s: float, n: int) -> list[Candle]:
        step = minutes * 60
        start = math.floor(now_s / step) * step
        closes = [self.price]
        drift = 0.0
        sig = self.sigma_min * math.sqrt(minutes)
        for _ in range(n - 1):                                 # gera para trás
            if self.rng.random() < 0.04:
                drift = self.rng.choice([-1, 1]) * self.rng.uniform(0.1, 0.35) * sig
            closes.append(closes[-1] / (1 + drift + self.rng.gauss(0, sig)))
        closes.reverse()
        out = []
        for i, c in enumerate(closes):
            o = closes[i - 1] if i else c * (1 + self.rng.gauss(0, sig / 3))
            h = max(o, c) * (1 + abs(self.rng.gauss(0, sig / 2)))
            lo = min(o, c) * (1 - abs(self.rng.gauss(0, sig / 2)))
            t = datetime.fromtimestamp(start - (n - 1 - i) * step, timezone.utc).isoformat()
            out.append(Candle(t, o, h, lo, c, spread=c * self.spread_pct, volume=self.rng.randint(500, 5000)))
        return out

    def tick(self, now_s: float, dt_s: float) -> None:
        mins = dt_s / 60
        self.price *= 1 + self.drift_min * mins + self.rng.gauss(0, self.sigma_min * math.sqrt(mins))
        for res, m in RES_MIN.items():
            step = m * 60
            bucket = math.floor(now_s / step) * step
            series = self.candles[res]
            last = series[-1]
            last_t = datetime.fromisoformat(last.time).timestamp()
            if bucket > last_t:
                series.append(Candle(datetime.fromtimestamp(bucket, timezone.utc).isoformat(),
                                     last.close, max(last.close, self.price), min(last.close, self.price),
                                     self.price, spread=self.price * self.spread_pct, volume=0))
                if len(series) > 1500:
                    del series[:len(series) - 1500]
            else:
                last.close = self.price
                last.high = max(last.high, self.price)
                last.low = min(last.low, self.price)
                last.volume += 1

    @property
    def quote(self) -> Quote:
        half = self.price * self.spread_pct / 2
        return Quote(bid=self.price - half, ask=self.price + half)


class SimBroker:
    name = "simulador"

    def __init__(self, epics: list[str], start_balance: float = 60.0, leverage: float = 200.0,
                 speed: float = 5.0, seed: int | None = None, history: int = 420):
        self.rng = random.Random(seed)
        self.speed = speed
        self.cash = start_balance
        self.margin_factor = 1 / max(leverage, 1)
        self.lock = threading.RLock()
        self.sim_time = time.time()
        self._wall = time.time()
        self.books = {e: _Book(e, self.rng, self.sim_time, history) for e in epics}
        self._positions: dict[str, Position] = {}
        self._ids = itertools.count(1)
        self.closed: list[dict] = []
        self.pending_news: list[tuple[str, str, float]] = []    # (ticker, título, sinal real)

    # ------------------------------------------------------------------ relógio
    def now(self) -> datetime:
        return datetime.fromtimestamp(self.sim_time, timezone.utc)

    def step(self) -> list[dict]:
        """Avança o relógio pelo tempo real decorrido × velocidade. Retorna fechamentos."""
        with self.lock:
            wall = time.time()
            dt = max(0.0, (wall - self._wall) * self.speed)
            self._wall = wall
            if dt <= 0:
                return []
            self.sim_time += dt
            for b in self.books.values():
                if self.rng.random() < 0.0025 * dt / 60 * 60:      # ~1 mudança de regime a cada ~7 min simulados
                    b.drift_min = self.rng.choice([-1, 1]) * self.rng.uniform(0.3, 0.9) * b.sigma_min
                    if self.rng.random() < 0.75:
                        tpl = POS if b.drift_min > 0 else NEG
                        self.pending_news.append((b.epic, self.rng.choice(tpl).format(t=b.epic), b.drift_min))
                elif self.rng.random() < 0.002 * dt:
                    b.drift_min *= 0.5
                if self.rng.random() < 0.0008 * dt:
                    self.pending_news.append((b.epic, self.rng.choice(NOISE).format(t=b.epic), 0.0))
                b.tick(self.sim_time, dt)
            return self._check_exits()

    def pop_news(self) -> list[tuple[str, str, float]]:
        with self.lock:
            out, self.pending_news = self.pending_news, []
            return out

    def _check_exits(self) -> list[dict]:
        done = []
        for p in list(self._positions.values()):
            q = self.books[p.epic].quote
            px = q.bid if p.direction == "BUY" else q.ask
            hit = None
            if p.direction == "BUY":
                if p.stop_level is not None and px <= p.stop_level:
                    hit = ("stop", p.stop_level)
                elif p.profit_level is not None and px >= p.profit_level:
                    hit = ("alvo", p.profit_level)
            else:
                if p.stop_level is not None and px >= p.stop_level:
                    hit = ("stop", p.stop_level)
                elif p.profit_level is not None and px <= p.profit_level:
                    hit = ("alvo", p.profit_level)
            if hit:
                done.append(self._realize(p, hit[1], hit[0]))
        return done

    def _realize(self, p: Position, price: float, reason: str) -> dict:
        pnl = (price - p.level) * p.size * (1 if p.direction == "BUY" else -1)
        self.cash += pnl
        self._positions.pop(p.deal_id, None)
        rec = {"deal_id": p.deal_id, "epic": p.epic, "pnl": pnl, "price": price, "reason": reason}
        self.closed.append(rec)
        return rec

    # ------------------------------------------------------------------ interface Broker
    def account(self) -> Account:
        with self.lock:
            upl = sum(self._upl(p) for p in self._positions.values())
            margin = sum(p.size * p.level * self.margin_factor for p in self._positions.values())
            return Account(balance=self.cash, equity=self.cash + upl, available=self.cash + upl - margin)

    def _upl(self, p: Position) -> float:
        q = self.books[p.epic].quote
        px = q.bid if p.direction == "BUY" else q.ask
        return (px - p.level) * p.size * (1 if p.direction == "BUY" else -1)

    def candles(self, epic: str, resolution: str = "MINUTE_15", count: int = 300) -> list[Candle]:
        with self.lock:
            return [Candle(**c.__dict__) for c in self.books[epic].candles[resolution][-count:]]

    def quote(self, epic: str) -> Quote:
        with self.lock:
            return self.books[epic].quote

    def quotes(self) -> dict[str, Quote]:
        with self.lock:
            return {e: b.quote for e, b in self.books.items()}

    def rules(self, epic: str) -> MarketRules:
        with self.lock:
            price = self.books[epic].price
        fx = _is_fx(epic)
        return MarketRules(epic=epic, name=f"{epic} (simulado)", min_size=100 if fx else 0.1,
                           size_step=100 if fx else 0.1, min_stop_distance=price * 0.0005,
                           margin_factor=self.margin_factor, decimals=5 if fx else 2)

    def positions(self) -> list[Position]:
        with self.lock:
            out = []
            for p in self._positions.values():
                p.upl = self._upl(p)
                out.append(Position(**p.__dict__))
            return out

    def open_position(self, epic: str, direction: str, size: float, stop_level: float,
                      profit_level: float) -> OrderResult:
        with self.lock:
            q = self.books[epic].quote
            level = q.ask if direction == "BUY" else q.bid
            deal_id = f"SIM{next(self._ids):05d}"
            self._positions[deal_id] = Position(deal_id, epic, direction, size, level, 0.0, stop_level, profit_level)
            return OrderResult(True, deal_id, level, "ACCEPTED (simulado)")

    def close_position(self, deal_id: str) -> OrderResult:
        with self.lock:
            p = self._positions.get(deal_id)
            if not p:
                return OrderResult(False, deal_id, None, "posição não encontrada")
            q = self.books[p.epic].quote
            rec = self._realize(p, q.bid if p.direction == "BUY" else q.ask, "fechada pelo agente")
            return OrderResult(True, deal_id, rec["price"], f"fechada ({rec['pnl']:+.2f})")
