"""Motor do bot: notícias → sentimento → features → modelo → risco → ordem."""
from __future__ import annotations

import logging
import math
import time
from datetime import datetime, timedelta, timezone

import pandas as pd

from .broker import Broker
from .config import Settings
from .features import build_features, to_frame
from .model import TradingModel, make_dataset, train_and_maybe_promote
from .news import fetch_rss, synthetic
from .risk import Guard, position_size
from .store import Store

log = logging.getLogger(__name__)

SENT_WINDOW_H = 6        # considera notícias das últimas 6 horas
SENT_HALF_LIFE_H = 2.0   # notícia perde metade do peso a cada 2 horas
NEWS_EVERY_S = 180
RETRAIN_EVERY_S = 7 * 24 * 3600


def aggregate_sentiment(rows: list[dict], at: datetime) -> float:
    """Média do sentimento ponderada por relevância e por quão recente é a notícia."""
    num = den = 0.0
    for r in rows:
        try:
            t = datetime.fromisoformat(r["time"])
        except (TypeError, ValueError):
            continue
        age_h = max((at - t).total_seconds() / 3600, 0)
        w = (r.get("relevance") or 0) * 0.5 ** (age_h / SENT_HALF_LIFE_H)
        num += w * (r.get("sentiment") or 0)
        den += w
    if den == 0:
        return 0.0
    # encolhe em direção a 0 quando há pouca evidência (den pequeno)
    return round(num / den * min(1.0, den / 1.0), 4)


class Engine:
    def __init__(self, settings: Settings, broker: Broker, store: Store, scorer, model: TradingModel | None = None):
        self.s = settings
        self.broker = broker
        self.store = store
        self.scorer = scorer
        self.model = model or TradingModel.load(settings.model_dir)
        self.guard = Guard(settings.start_balance, settings.target_balance, settings.max_daily_loss)
        self._last_news = 0.0
        self._last_bar: dict[str, str] = {}
        self._guard_msg = ""
        self._margin_in_use = 0.0
        self.paper = broker.name == "paper"

    # ----------------------------------------------------------------- notícias
    def ingest_news(self, force: bool = False) -> int:
        if self.paper:
            items = synthetic(self.broker.pop_news())
        else:
            if not force and time.time() - self._last_news < NEWS_EVERY_S:
                return 0
            self._last_news = time.time()
            items = fetch_rss([w.ticker for w in self.s.watchlist])
        known = self.store.known_news_ids([i["id"] for i in items])
        new = [i for i in items if i["id"] not in known]
        if not new:
            return 0
        by_id = {i["id"]: i for i in new}
        for sc in self.scorer.score(new):
            self.store.add_news(by_id[sc.id], sc)
        log.info("%d notícias novas analisadas (%s)", len(new), self.scorer.name)
        return len(new)

    def sentiment_for(self, ticker: str) -> float:
        at = datetime.now(timezone.utc)
        since = (at - timedelta(hours=SENT_WINDOW_H)).isoformat()
        return aggregate_sentiment(self.store.recent_news(ticker, since), at)

    # ----------------------------------------------------------------- posições
    def sync_positions(self, positions) -> None:
        """Detecta posições fechadas pela corretora (stop/alvo) e registra o resultado."""
        open_now = {p.deal_id: p for p in positions}
        for p in positions:
            self.store.set(f"upl:{p.deal_id}", p.upl)
        closed_paper = {d: pnl for d, _, pnl in getattr(self.broker, "closed", [])}
        for deal_id, t in self.store.open_trade_ids().items():
            if deal_id in open_now:
                continue
            pnl = closed_paper.get(deal_id, self.store.get(f"upl:{deal_id}"))
            self.store.close_trade(deal_id, pnl)
            res = f"{pnl:+.2f}" if pnl is not None else "resultado indisponível"
            self.store.event(f"Posição fechada {t['epic']} {t['direction']} ({deal_id}): {res}",
                             "win" if (pnl or 0) > 0 else "loss")

    # ----------------------------------------------------------------- ciclo
    def step(self) -> None:
        self.ingest_news()
        acct = self.broker.account()
        self.store.add_equity(acct.balance, acct.equity)
        positions = self.broker.positions()
        self.sync_positions(positions)

        can_trade, msg = self.guard.check(acct.equity, datetime.now(timezone.utc).date().isoformat())
        if msg != self._guard_msg:
            self._guard_msg = msg
            if not can_trade:
                self.store.event(msg, "warn")
        self.store.set("status", {"broker": self.broker.name, "scorer": self.scorer.name,
                                  "model": self.model.report.version if self.model.ready else None,
                                  "can_trade": can_trade, "guard": msg, "updated": time.time(),
                                  "start": self.s.start_balance, "target": self.s.target_balance,
                                  "watchlist": [w.epic for w in self.s.watchlist]})

        open_epics = {p.epic for p in positions}
        self._margin_in_use = 0.0
        for p in positions:
            try:
                self._margin_in_use += p.size * p.level * self.broker.rules(p.epic).margin_factor
            except Exception:  # noqa: BLE001 — regra indisponível não deve travar o ciclo
                pass

        for w in self.s.watchlist:
            try:
                self._evaluate(w, acct, positions, open_epics, can_trade)
            except Exception as e:  # noqa: BLE001
                log.exception("Erro avaliando %s", w.epic)
                self.store.event(f"Erro em {w.epic}: {e}", "error")

        if self.paper:
            self.broker.advance(1)

    def _evaluate(self, w, acct, positions, open_epics, can_trade) -> None:
        df = to_frame(self.broker.candles(w.epic, self.s.resolution, 300))
        if len(df) < 60:
            return
        bar_time = str(df["time"].iloc[-1])
        if self._last_bar.get(w.epic) == bar_time:
            return                                          # só decide uma vez por barra
        self._last_bar[w.epic] = bar_time

        sent = self.sentiment_for(w.ticker)
        feats = build_features(df, sent).iloc[[-1]]
        price = float(df["close"].iloc[-1])
        atr = float(feats["atr"].iloc[0])
        p_long = p_short = float("nan")
        decision, reason = "NADA", ""

        if not self.model.ready:
            reason = "sem modelo treinado (rode: python run.py train)"
        elif feats.isna().any(axis=None):
            reason = "indicadores incompletos"
        else:
            pl, ps = self.model.predict(feats)
            p_long, p_short = float(pl[0]), float(ps[0])
            adj_long = p_long + self.s.sentiment_weight * sent
            adj_short = p_short - self.s.sentiment_weight * sent
            thr = self.s.prob_threshold
            want = None
            if adj_long >= thr and adj_long >= adj_short:
                want = "BUY"
            elif adj_short >= thr and adj_short > adj_long:
                want = "SELL"
            if want is None:
                reason = f"sem vantagem (long {adj_long:.2f}, short {adj_short:.2f} < {thr:.2f})"
            elif not can_trade:
                reason = f"{want} bloqueado: {self._guard_msg}"
            elif w.epic in open_epics:
                reason = f"{want} ignorado: já há posição em {w.epic}"
            elif len(positions) >= self.s.max_open_positions:
                reason = f"{want} ignorado: limite de {self.s.max_open_positions} posições"
            else:
                decision, reason = self._enter(w.epic, want, atr, acct,
                                               adj_long if want == "BUY" else adj_short, sent)
                if decision in ("BUY", "SELL"):
                    open_epics.add(w.epic)
                    positions.append(None)                 # conta para o limite neste ciclo

        self.store.add_signal(bar_time=bar_time, epic=w.epic, price=price, atr=atr,
                              p_long=None if math.isnan(p_long) else round(p_long, 4),
                              p_short=None if math.isnan(p_short) else round(p_short, 4),
                              sentiment=sent, decision=decision, reason=reason)

    def _enter(self, epic: str, direction: str, atr: float, acct,
               prob: float, sent: float) -> tuple[str, str]:
        q = self.broker.quote(epic)
        if not q.tradeable:
            return "NADA", f"{direction} ignorado: mercado fechado"
        rules = self.broker.rules(epic)
        entry = q.ask if direction == "BUY" else q.bid
        stop_dist = max(self.s.stop_atr * atr, rules.min_stop_distance * 1.1, q.ask - q.bid)
        tp_dist = stop_dist * self.s.take_profit_atr / self.s.stop_atr
        sz = position_size(acct, rules, entry, stop_dist, self.s.risk_per_trade, self.s.max_risk_per_trade,
                           self.s.max_margin_usage, self._margin_in_use)
        if not sz.ok:
            return "NADA", f"{direction} ignorado: {sz.reason}"
        self._margin_in_use += sz.margin
        sign = 1 if direction == "BUY" else -1
        stop = round(entry - sign * stop_dist, rules.decimals)
        target = round(entry + sign * tp_dist, rules.decimals)
        res = self.broker.open_position(epic, direction, sz.size, stop, target)
        if not res.ok:
            self.store.event(f"Ordem {direction} {epic} recusada: {res.message}", "error")
            return "NADA", f"ordem recusada: {res.message}"
        self.store.open_trade(deal_id=res.deal_id, epic=epic, direction=direction, size=sz.size,
                              entry=res.level or entry, stop=stop, target=target, risk=round(sz.risk_amount, 2),
                              p=round(prob, 4), sentiment=sent)
        self.store.event(f"{'COMPRA' if direction == 'BUY' else 'VENDA'} {epic} tam {sz.size:g} @ {entry:.2f} "
                         f"stop {stop} alvo {target} (risco ${sz.risk_amount:.2f}, p={prob:.2f}, sent={sent:+.2f})",
                         "trade")
        return direction, f"aberta {res.deal_id}"

    # ----------------------------------------------------------------- treino
    def retrain(self, count: int = 1000) -> object:
        frames, sentiment = {}, {}
        for w in self.s.watchlist:
            df = to_frame(self.broker.candles(w.epic, self.s.resolution, count))
            if len(df) < 60:
                continue
            frames[w.epic] = df
            hist = self.store.sentiment_history(w.epic)
            if hist:
                m = {pd.Timestamp(h["bar_time"]): h["sentiment"] for h in hist if h["bar_time"]}
                sentiment[w.epic] = pd.Series([m.get(t, 0.0) for t in df["time"]], index=df.index)
        data = make_dataset(frames, self.s.stop_atr, self.s.take_profit_atr, sentiment)
        report = train_and_maybe_promote(data, self.s.model_dir, self.s.prob_threshold,
                                         self.s.stop_atr, self.s.take_profit_atr)
        self.store.add_model_report(report)
        self.store.set("last_retrain", time.time())
        self.store.event(f"Retreino {report.version}: AUC long {report.auc_long:.3f} / short {report.auc_short:.3f}, "
                         f"expectativa {report.expectancy_r:+.3f}R em {report.trades} trades de teste — {report.note}",
                         "model")
        if report.promoted:
            self.model = TradingModel.load(self.s.model_dir)
        return report

    def run_forever(self, max_steps: int | None = None) -> None:
        interval = min(self.s.loop_seconds, 2) if self.paper else self.s.loop_seconds
        self.store.event(f"Bot iniciado ({self.broker.name}, sentimento: {self.scorer.name})", "info")
        if not self.model.ready:
            try:
                self.retrain()
            except ValueError as e:
                self.store.event(str(e), "warn")
        steps = 0
        while max_steps is None or steps < max_steps:
            t0 = time.time()
            try:
                if time.time() - (self.store.get("last_retrain") or 0) > RETRAIN_EVERY_S:
                    self.retrain()
                self.step()
            except Exception as e:  # noqa: BLE001 — o bot nunca deve morrer por um erro isolado
                log.exception("Erro no ciclo")
                self.store.event(f"Erro no ciclo: {e}", "error")
            steps += 1
            if max_steps is None or steps < max_steps:
                time.sleep(max(0.0, interval - (time.time() - t0)))
