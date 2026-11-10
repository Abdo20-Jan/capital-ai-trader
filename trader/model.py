"""Modelo de Machine Learning com retreino e promoção "campeão × desafiante".

Dois classificadores (gradient boosting):
  - long : P(alvo de alta antes do stop | features)
  - short: P(alvo de baixa antes do stop | features)

Auto-aperfeiçoamento: `train_and_maybe_promote` treina um desafiante com os dados
mais recentes, compara com o campeão em dados que nenhum dos dois viu (últimos
30% no tempo) e só substitui o campeão se a expectativa simulada for melhor.
"""
from __future__ import annotations

import json
import pickle
import time
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import pandas as pd
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.metrics import roc_auc_score

from .features import FEATURES, build_features, triple_barrier_labels

MIN_ROWS = 300


@dataclass
class ModelReport:
    version: str
    trained_at: float
    rows: int
    auc_long: float
    auc_short: float
    expectancy_r: float          # resultado médio por trade em múltiplos do risco (R)
    trades: int
    win_rate: float
    promoted: bool = False
    note: str = ""
    feature_importance: dict = field(default_factory=dict)


def make_dataset(frames: dict[str, pd.DataFrame], stop_atr: float, tp_atr: float,
                 sentiment: dict[str, pd.Series] | None = None) -> pd.DataFrame:
    rows = []
    for epic, df in frames.items():
        if len(df) < 60:
            continue
        sent = 0.0
        if sentiment and epic in sentiment:
            sent = sentiment[epic].reindex(df.index).fillna(0.0)
        f = build_features(df, sent)
        f["y_long"] = triple_barrier_labels(df, f["atr"], tp_atr, stop_atr)
        inv = df.rename(columns={"high": "low", "low": "high"}).copy()
        inv[["open", "high", "low", "close"]] = -inv[["open", "high", "low", "close"]]
        f["y_short"] = triple_barrier_labels(inv, f["atr"], tp_atr, stop_atr)
        f["time"] = df["time"]
        f["epic"] = epic
        rows.append(f.iloc[50:])                 # descarta aquecimento dos indicadores
    if not rows:
        return pd.DataFrame()
    data = pd.concat(rows).replace([np.inf, -np.inf], np.nan)
    return data.dropna(subset=FEATURES).sort_values("time").reset_index(drop=True)


def _clf() -> HistGradientBoostingClassifier:
    return HistGradientBoostingClassifier(max_iter=200, learning_rate=0.05, max_depth=3,
                                          min_samples_leaf=40, l2_regularization=1.0, random_state=7)


class TradingModel:
    def __init__(self, long_clf=None, short_clf=None, report: ModelReport | None = None):
        self.long_clf = long_clf
        self.short_clf = short_clf
        self.report = report

    @property
    def ready(self) -> bool:
        return self.long_clf is not None and self.short_clf is not None

    def predict(self, feats: pd.DataFrame) -> tuple[np.ndarray, np.ndarray]:
        X = feats[FEATURES].to_numpy(dtype=float)
        return self.long_clf.predict_proba(X)[:, 1], self.short_clf.predict_proba(X)[:, 1]

    # ------------------------------------------------------------- treino
    @classmethod
    def fit(cls, data: pd.DataFrame) -> "TradingModel":
        m = cls(_clf(), _clf())
        dl = data.dropna(subset=["y_long"])
        ds = data.dropna(subset=["y_short"])
        m.long_clf.fit(dl[FEATURES].to_numpy(float), dl["y_long"].astype(int))
        m.short_clf.fit(ds[FEATURES].to_numpy(float), ds["y_short"].astype(int))
        return m

    def evaluate(self, test: pd.DataFrame, threshold: float, stop_atr: float, tp_atr: float,
                 cost_r: float = 0.05) -> tuple[float, float, float, int, float]:
        """Retorna (auc_long, auc_short, expectativa em R, nº trades, taxa de acerto) no conjunto de teste."""
        p_long, p_short = self.predict(test)

        def auc(y, p):
            mask = ~np.isnan(y)
            yy = y[mask]
            return float(roc_auc_score(yy, p[mask])) if len(set(yy)) == 2 else 0.5

        a_l = auc(test["y_long"].to_numpy(float), p_long)
        a_s = auc(test["y_short"].to_numpy(float), p_short)
        rr = tp_atr / stop_atr
        results = []
        for pl, ps, yl, ys in zip(p_long, p_short, test["y_long"], test["y_short"]):
            if pl >= threshold and pl >= ps and not np.isnan(yl):
                results.append(rr if yl == 1 else -1.0)
            elif ps >= threshold and ps > pl and not np.isnan(ys):
                results.append(rr if ys == 1 else -1.0)
        if not results:
            return a_l, a_s, 0.0, 0, 0.0
        r = np.array(results) - cost_r            # custo (spread) aproximado em R
        return a_l, a_s, float(r.mean()), len(r), float((r > 0).mean())

    # ------------------------------------------------------------- persistência
    def save(self, directory: Path) -> None:
        directory.mkdir(parents=True, exist_ok=True)
        with open(directory / "model.pkl", "wb") as fh:
            pickle.dump({"long": self.long_clf, "short": self.short_clf}, fh)
        (directory / "model.json").write_text(json.dumps(self.report.__dict__, indent=2), encoding="utf-8")

    @classmethod
    def load(cls, directory: Path) -> "TradingModel":
        pkl, meta = directory / "model.pkl", directory / "model.json"
        if not (pkl.exists() and meta.exists()):
            return cls()
        with open(pkl, "rb") as fh:
            obj = pickle.load(fh)
        return cls(obj["long"], obj["short"], ModelReport(**json.loads(meta.read_text(encoding="utf-8"))))


def train_and_maybe_promote(data: pd.DataFrame, directory: Path, threshold: float,
                            stop_atr: float, tp_atr: float) -> ModelReport:
    if len(data) < MIN_ROWS:
        raise ValueError(f"Poucos dados para treinar ({len(data)} linhas; mínimo {MIN_ROWS}).")
    split = int(len(data) * 0.7)
    train, test = data.iloc[:split], data.iloc[split:]

    challenger = TradingModel.fit(train)
    a_l, a_s, exp_r, n, wr = challenger.evaluate(test, threshold, stop_atr, tp_atr)
    champion = TradingModel.load(directory)
    champ_exp = None
    if champion.ready:
        champ_exp = champion.evaluate(test, threshold, stop_atr, tp_atr)[2]

    promote = n >= 20 and exp_r > 0 and (champ_exp is None or exp_r > champ_exp)
    if not promote:
        why = []
        if n < 20:
            why.append(f"só {n} trades no teste")
        if exp_r <= 0:
            why.append("expectativa ≤ 0")
        if champ_exp is not None and exp_r <= champ_exp:
            why.append(f"não supera o campeão ({champ_exp:+.3f}R)")
        note = "Desafiante rejeitado: " + ", ".join(why)
    else:
        note = "Desafiante promovido" + (f" (campeão anterior: {champ_exp:+.3f}R)" if champ_exp is not None else "")

    report = ModelReport(version=time.strftime("%Y%m%d-%H%M%S"), trained_at=time.time(), rows=len(data),
                         auc_long=round(a_l, 4), auc_short=round(a_s, 4), expectancy_r=round(exp_r, 4),
                         trades=n, win_rate=round(wr, 4), promoted=promote, note=note)
    if promote:
        # Campeão final usa TODOS os dados (inclusive o período de teste)
        final = TradingModel.fit(data)
        final.report = report
        final.save(directory)
    return report
