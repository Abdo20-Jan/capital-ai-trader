"""Backtest walk-forward + simulação de Monte Carlo da meta ($60 → $100).

Treina o modelo nos primeiros 60% do histórico e opera, barra a barra, nos 40%
restantes (dados que o modelo nunca viu), com o mesmo dimensionamento de risco
do bot. Observação: notícias históricas não estão disponíveis pela API, então o
sentimento no backtest é 0 — ele mede só a parte técnica/ML.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from .broker import Account
from .config import Settings
from .features import FEATURES, build_features
from .model import TradingModel, make_dataset
from .risk import position_size


@dataclass
class BacktestResult:
    start: float
    final: float
    trades: int
    wins: int
    max_drawdown: float
    r_multiples: list[float] = field(default_factory=list)
    equity_curve: list[float] = field(default_factory=list)
    skipped: dict[str, int] = field(default_factory=dict)
    leverage_used: list[float] = field(default_factory=list)   # exposição / patrimônio em cada entrada

    @property
    def win_rate(self) -> float:
        return self.wins / self.trades if self.trades else 0.0

    @property
    def expectancy_r(self) -> float:
        return float(np.mean(self.r_multiples)) if self.r_multiples else 0.0


def run_backtest(frames: dict[str, pd.DataFrame], rules: dict, s: Settings, train_frac: float = 0.6) -> BacktestResult:
    # --- treino nos primeiros 60% (por tempo)
    cut = {e: int(len(df) * train_frac) for e, df in frames.items()}
    train = make_dataset({e: df.iloc[:cut[e]].reset_index(drop=True) for e, df in frames.items()},
                         s.stop_atr, s.take_profit_atr)
    if len(train) < 200:
        raise ValueError(f"Histórico insuficiente para backtest ({len(train)} linhas de treino)")
    model = TradingModel.fit(train)

    # --- pré-calcula features e probabilidades no período de teste
    sig = {}
    for e, df in frames.items():
        f = build_features(df, 0.0)
        valid = f[FEATURES].replace([np.inf, -np.inf], np.nan).notna().all(axis=1)
        pl = np.full(len(df), np.nan)
        ps = np.full(len(df), np.nan)
        if valid.any():
            a, b = model.predict(f[valid])
            pl[valid.to_numpy()] = a
            ps[valid.to_numpy()] = b
        sig[e] = (f["atr"].to_numpy(), pl, ps)

    equity = s.start_balance
    res = BacktestResult(start=equity, final=equity, trades=0, wins=0, max_drawdown=0.0)
    peak = equity
    open_pos: dict[str, dict] = {}
    n = max(len(df) for df in frames.values())
    for i in range(min(cut.values()), n):
        # 1) saídas
        for e in list(open_pos):
            df = frames[e]
            if i >= len(df):
                continue
            p = open_pos[e]
            hi, lo = df["high"].iat[i], df["low"].iat[i]
            exit_px = None
            if p["dir"] == 1:
                if lo <= p["stop"]:
                    exit_px = p["stop"]
                elif hi >= p["target"]:
                    exit_px = p["target"]
            else:
                if hi >= p["stop"]:
                    exit_px = p["stop"]
                elif lo <= p["target"]:
                    exit_px = p["target"]
            if exit_px is not None:
                pnl = (exit_px - p["entry"]) * p["size"] * p["dir"]
                equity += pnl
                res.trades += 1
                res.wins += pnl > 0
                res.r_multiples.append(pnl / p["risk"] if p["risk"] else 0.0)
                del open_pos[e]
        # 2) entradas
        margin_used = sum(p["size"] * p["entry"] * rules[e].margin_factor for e, p in open_pos.items())
        exposure = sum(p["size"] * p["entry"] for p in open_pos.values())
        for e, df in frames.items():
            if i >= len(df) - 1 or e in open_pos or len(open_pos) >= s.max_open_positions:
                continue
            atr, pl, ps = sig[e]
            if np.isnan(pl[i]) or not np.isfinite(atr[i]):
                continue
            want = 1 if pl[i] >= s.prob_threshold and pl[i] >= ps[i] else \
                -1 if ps[i] >= s.prob_threshold and ps[i] > pl[i] else 0
            if not want:
                continue
            close, spread = df["close"].iat[i], df["spread"].iat[i] if "spread" in df else 0.0
            entry = close + want * spread / 2
            stop_dist = max(s.stop_atr * atr[i], rules[e].min_stop_distance * 1.1)
            acct = Account(balance=equity, equity=equity, available=equity - margin_used)
            sz = position_size(acct, rules[e], entry, stop_dist, s.risk_per_trade, s.max_risk_per_trade,
                               s.max_margin_usage, margin_used, s.max_exposure, exposure)
            if not sz.ok:
                key = sz.reason.split(":")[0].split(" ")[0]
                res.skipped[key] = res.skipped.get(key, 0) + 1
                continue
            margin_used += sz.margin
            exposure += sz.notional
            res.leverage_used.append(sz.notional / equity)
            open_pos[e] = {"dir": want, "entry": entry, "size": sz.size, "risk": sz.size * stop_dist,
                           "stop": entry - want * stop_dist,
                           "target": entry + want * stop_dist * s.take_profit_atr / s.stop_atr}
        peak = max(peak, equity)
        res.max_drawdown = max(res.max_drawdown, (peak - equity) / peak if peak else 0)
        res.equity_curve.append(round(equity, 2))
    res.final = round(equity, 2)
    return res


def monte_carlo(r_multiples: list[float], start: float, target: float, ruin: float,
                risk_per_trade: float, max_trades: int = 500, runs: int = 5000, seed: int = 1) -> dict:
    """Sorteia sequências de trades (com reposição) a partir dos resultados do backtest.

    Retorna a chance de chegar à meta antes de cair até `ruin`.
    """
    if not r_multiples:
        return {"p_target": 0.0, "p_ruin": 0.0, "p_neither": 1.0, "median_trades_to_target": None}
    rng = np.random.default_rng(seed)
    r = np.asarray(r_multiples)
    hit = ruined = 0
    trades_to_target = []
    for _ in range(runs):
        eq = start
        for k in range(max_trades):
            eq += eq * risk_per_trade * rng.choice(r)
            if eq >= target:
                hit += 1
                trades_to_target.append(k + 1)
                break
            if eq <= ruin:
                ruined += 1
                break
    return {"p_target": hit / runs, "p_ruin": ruined / runs, "p_neither": 1 - (hit + ruined) / runs,
            "median_trades_to_target": int(np.median(trades_to_target)) if trades_to_target else None}
