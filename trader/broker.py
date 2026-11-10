"""Tipos comuns e interface que toda corretora (real ou simulada) implementa."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass
class Candle:
    time: str          # ISO UTC
    open: float
    high: float
    low: float
    close: float
    spread: float = 0.0
    volume: float = 0.0


@dataclass
class Quote:
    bid: float
    ask: float
    tradeable: bool = True

    @property
    def mid(self) -> float:
        return (self.bid + self.ask) / 2


@dataclass
class MarketRules:
    epic: str
    name: str
    min_size: float
    size_step: float
    min_stop_distance: float   # em preço (já convertido de % quando necessário)
    margin_factor: float       # fração do valor nocional exigida como margem (0.2 = 20% = alavancagem 1:5)
    currency: str = "USD"
    decimals: int = 2          # casas decimais aceitas no preço (stop/alvo)


@dataclass
class Account:
    balance: float
    equity: float
    available: float
    currency: str = "USD"


@dataclass
class Position:
    deal_id: str
    epic: str
    direction: str     # BUY | SELL
    size: float
    level: float       # preço de entrada
    upl: float         # lucro/prejuízo não realizado
    stop_level: float | None = None
    profit_level: float | None = None


@dataclass
class OrderResult:
    ok: bool
    deal_id: str | None
    level: float | None
    message: str


class Broker(Protocol):
    name: str

    def account(self) -> Account: ...
    def candles(self, epic: str, resolution: str, count: int) -> list[Candle]: ...
    def quote(self, epic: str) -> Quote: ...
    def rules(self, epic: str) -> MarketRules: ...
    def positions(self) -> list[Position]: ...
    def open_position(self, epic: str, direction: str, size: float,
                      stop_level: float, profit_level: float) -> OrderResult: ...
    def close_position(self, deal_id: str) -> OrderResult: ...
