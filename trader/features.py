"""Indicadores técnicos, features para o modelo e rótulos (triple barrier)."""
from __future__ import annotations

import numpy as np
import pandas as pd

from .broker import Candle

FEATURES = ["ret_1", "ret_3", "ret_6", "ret_12", "rsi", "ema_gap", "ema_slope",
            "atr_pct", "vol_ratio", "range_pos", "sentiment"]


def to_frame(candles: list[Candle]) -> pd.DataFrame:
    df = pd.DataFrame([c.__dict__ for c in candles])
    if df.empty:
        return df
    df["time"] = pd.to_datetime(df["time"], utc=True, errors="coerce", format="mixed")
    return df.reset_index(drop=True)


def ema(s: pd.Series, n: int) -> pd.Series:
    return s.ewm(span=n, adjust=False).mean()


def rsi(close: pd.Series, n: int = 14) -> pd.Series:
    d = close.diff()
    up = d.clip(lower=0).ewm(alpha=1 / n, adjust=False).mean()
    down = (-d.clip(upper=0)).ewm(alpha=1 / n, adjust=False).mean()
    rs = up / down.replace(0, np.nan)
    return (100 - 100 / (1 + rs)).fillna(50)


def atr(df: pd.DataFrame, n: int = 14) -> pd.Series:
    prev = df["close"].shift(1)
    tr = pd.concat([df["high"] - df["low"], (df["high"] - prev).abs(), (df["low"] - prev).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / n, adjust=False).mean()


def build_features(df: pd.DataFrame, sentiment: float | pd.Series = 0.0) -> pd.DataFrame:
    """Calcula as features em cada barra. `sentiment` pode ser um valor (barra atual) ou uma série."""
    out = pd.DataFrame(index=df.index)
    c = df["close"]
    for n in (1, 3, 6, 12):
        out[f"ret_{n}"] = c.pct_change(n)
    out["rsi"] = (rsi(c) - 50) / 50
    e9, e21 = ema(c, 9), ema(c, 21)
    out["ema_gap"] = e9 / e21 - 1
    out["ema_slope"] = e21.pct_change(3)
    a = atr(df)
    out["atr"] = a
    out["atr_pct"] = a / c
    out["vol_ratio"] = c.pct_change().rolling(6).std() / c.pct_change().rolling(48).std()
    hi, lo = df["high"].rolling(24).max(), df["low"].rolling(24).min()
    out["range_pos"] = ((c - lo) / (hi - lo).replace(0, np.nan)) * 2 - 1
    out["sentiment"] = sentiment
    return out


def triple_barrier_labels(df: pd.DataFrame, atr_s: pd.Series, up_k: float, down_k: float,
                          horizon: int = 16) -> pd.Series:
    """1 = alvo de alta atingido antes do stop; 0 = stop atingido antes; NaN = nenhum dentro do horizonte.

    Com up_k = take_profit_atr e down_k = stop_atr, o rótulo responde exatamente à
    pergunta do trade: "uma compra agora bateria o alvo antes do stop?".
    """
    highs, lows, closes = df["high"].to_numpy(), df["low"].to_numpy(), df["close"].to_numpy()
    a = atr_s.to_numpy()
    labels = np.full(len(df), np.nan)
    for i in range(len(df) - 1):
        if not np.isfinite(a[i]) or a[i] <= 0:
            continue
        up, dn = closes[i] + up_k * a[i], closes[i] - down_k * a[i]
        for j in range(i + 1, min(i + 1 + horizon, len(df))):
            hit_up, hit_dn = highs[j] >= up, lows[j] <= dn
            if hit_up and hit_dn:          # mesma barra: assume o pior caso (stop)
                labels[i] = 0
                break
            if hit_up:
                labels[i] = 1
                break
            if hit_dn:
                labels[i] = 0
                break
    return pd.Series(labels, index=df.index)
