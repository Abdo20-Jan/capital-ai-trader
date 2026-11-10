"""Gestão de risco: tamanho da posição e travas de segurança."""
from __future__ import annotations

import math
from dataclasses import dataclass

from .broker import Account, MarketRules


@dataclass
class SizeDecision:
    ok: bool
    size: float
    risk_amount: float
    margin: float
    reason: str


def round_down(x: float, step: float) -> float:
    if step <= 0:
        return x
    return math.floor(x / step + 1e-9) * step


def position_size(account: Account, rules: MarketRules, price: float, stop_distance: float,
                  risk_per_trade: float, max_risk_per_trade: float, max_margin_usage: float,
                  margin_in_use: float = 0.0) -> SizeDecision:
    """Tamanho tal que, se o stop for atingido, a perda ≈ risk_per_trade × patrimônio.

    Se o tamanho mínimo do ativo exigir arriscar mais que max_risk_per_trade, NÃO opera.
    """
    equity = account.equity
    if equity <= 0 or price <= 0:
        return SizeDecision(False, 0, 0, 0, "patrimônio ou preço inválido")
    if stop_distance <= 0:
        return SizeDecision(False, 0, 0, 0, "distância do stop inválida")

    risk_amount = equity * risk_per_trade
    size = round_down(risk_amount / stop_distance, rules.size_step)
    if size < rules.min_size:
        min_risk = rules.min_size * stop_distance
        if min_risk > equity * max_risk_per_trade:
            return SizeDecision(False, 0, min_risk, 0,
                                f"tamanho mínimo {rules.min_size:g} arriscaria ${min_risk:.2f} "
                                f"(> {max_risk_per_trade:.0%} de ${equity:.2f})")
        size = rules.min_size
    size = round(size, 6)
    margin = size * price * rules.margin_factor
    limit = equity * max_margin_usage - margin_in_use
    if margin > limit:
        size = round_down(limit / (price * rules.margin_factor), rules.size_step)
        if size < rules.min_size:
            need = rules.min_size * price * rules.margin_factor
            return SizeDecision(False, 0, 0, need,
                                f"margem insuficiente: mínimo exige ${need:.2f}, limite livre ${max(limit, 0):.2f}")
        margin = size * price * rules.margin_factor
    if margin > account.available:
        return SizeDecision(False, 0, 0, margin, f"saldo disponível ${account.available:.2f} < margem ${margin:.2f}")
    return SizeDecision(True, size, size * stop_distance, margin, "ok")


@dataclass
class Guard:
    """Travas diárias e de meta."""
    start_balance: float
    target_balance: float
    max_daily_loss: float
    day_start_equity: float | None = None
    day: str = ""

    def check(self, equity: float, today: str) -> tuple[bool, str]:
        if today != self.day or self.day_start_equity is None:
            self.day, self.day_start_equity = today, equity
        if equity >= self.target_balance:
            return False, f"META ATINGIDA: ${equity:.2f} ≥ ${self.target_balance:.2f} — novas entradas pausadas"
        dd = (self.day_start_equity - equity) / self.day_start_equity if self.day_start_equity else 0
        if dd >= self.max_daily_loss:
            return False, f"limite de perda diária atingido ({dd:.1%}) — pausado até amanhã"
        if equity <= self.start_balance * 0.5:
            return False, f"conta caiu 50% (${equity:.2f}) — bot parado para revisão"
        return True, "ok"
