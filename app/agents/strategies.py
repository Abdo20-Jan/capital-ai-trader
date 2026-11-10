"""Os agentes: Scalper, Day Trader (ML), Swing, Longo Prazo e Notícias."""
from __future__ import annotations

import asyncio
import math
import time
from datetime import datetime

from trader.features import build_features
from trader.model import TradingModel

from .base import AgentConfig, BaseAgent, Signal, indicators


def _f(x) -> float:
    return float(x) if x is not None and not (isinstance(x, float) and math.isnan(x)) else float("nan")


# ============================================================== SCALPER
class ScalperAgent(BaseAgent):
    """Candles de 1 minuto. Segue a micro-tendência: compra o recuo até a EMA 9 quando
    EMA 9 > EMA 21 e o RSI(7) volta acima de 50 (venda: espelho). Stop curto, alvo
    curto e no máximo 20 minutos posicionado. Só opera se o spread for pequeno."""

    def evaluate(self, epic, ind, sent):
        last, prev = ind.iloc[-1], ind.iloc[-2]
        a = _f(last["atr"])
        if not a or math.isnan(a):
            return None, "ATR indisponível"
        q = self.ctx.market.quotes.get(epic)
        spread = (q["ask"] - q["bid"]) if q else 0.0
        if spread > self.cfg.params.get("max_spread_atr", 0.3) * a:
            return None, f"spread alto ({spread:.4g} > {self.cfg.params.get('max_spread_atr', 0.3)}×ATR)"
        up = last["ema9"] > last["ema21"] and last["close"] > last["ema9"] and prev["low"] <= prev["ema9"]
        dn = last["ema9"] < last["ema21"] and last["close"] < last["ema9"] and prev["high"] >= prev["ema9"]
        sd, td = self.bracket(a)
        if up and 50 <= last["rsi7"] <= 75 and sent > -0.3:
            return Signal("BUY", sd, td, f"recuo na EMA9 em alta, RSI7 {last['rsi7']:.0f}"), "sinal de compra"
        if dn and 25 <= last["rsi7"] <= 50 and sent < 0.3:
            return Signal("SELL", sd, td, f"recuo na EMA9 em baixa, RSI7 {last['rsi7']:.0f}"), "sinal de venda"
        trend = "alta" if last["ema9"] > last["ema21"] else "baixa"
        return None, f"micro-tendência de {trend}, sem recuo válido (RSI7 {last['rsi7']:.0f})"


# ============================================================== DAY TRADER (ML)
class DayTraderAgent(BaseAgent):
    """Candles de 15 minutos. Usa o modelo de Machine Learning (probabilidade de o
    alvo vir antes do stop) ajustado pelo sentimento das notícias. Não abre posição
    perto do fim do dia e fecha tudo antes do encerramento (horário UTC)."""

    def __init__(self, cfg, ctx):
        super().__init__(cfg, ctx)
        self.model = TradingModel.load(ctx.settings.model_dir)

    def reload_model(self) -> None:
        self.model = TradingModel.load(self.ctx.settings.model_dir)

    def _hm(self, key: str) -> tuple[int, int]:
        h, m = self.cfg.params.get(key, "20:00").split(":")
        return int(h), int(m)

    def evaluate(self, epic, ind, sent):
        if not self.model.ready:
            return None, "sem modelo de ML ainda (treinando em segundo plano)"
        now = self.ctx.now()
        h, m = self._hm("no_entry_after")
        if (now.hour, now.minute) >= (h, m):
            return None, f"sem novas entradas após {h:02d}:{m:02d} UTC"
        feats = build_features(ind, sent).iloc[[-1]]
        if feats.isna().any(axis=None):
            return None, "indicadores incompletos"
        pl, ps = self.model.predict(feats)
        w = self.cfg.params.get("sentiment_weight", 0.10)
        thr = self.cfg.params.get("threshold", 0.52)
        long_p, short_p = float(pl[0]) + w * sent, float(ps[0]) - w * sent
        a = float(feats["atr"].iloc[0])
        sd, td = self.bracket(a)
        note = f"P(compra) {long_p:.2f} · P(venda) {short_p:.2f}"
        if long_p >= thr and long_p >= short_p:
            return Signal("BUY", sd, td, f"ML {note}", long_p), note
        if short_p >= thr and short_p > long_p:
            return Signal("SELL", sd, td, f"ML {note}", short_p), note
        return None, note + f" (< {thr:.2f})"

    async def exit_reason(self, position, now):
        h, m = self._hm("flat_at")
        if (now.hour, now.minute) >= (h, m):
            return f"fim do dia ({h:02d}:{m:02d} UTC)"
        return None


# ============================================================== SWING
class SwingAgent(BaseAgent):
    """Candles de 4 horas. Opera a favor da tendência (EMA 50 vs EMA 200): compra
    quando o RSI(14) volta acima de 40 depois de um recuo numa tendência de alta
    (venda: RSI volta abaixo de 60 numa tendência de baixa). Segura por dias."""
    min_bars = 210

    def evaluate(self, epic, ind, sent):
        last, prev = ind.iloc[-1], ind.iloc[-2]
        a = _f(last["atr"])
        if math.isnan(_f(last["ema200"])) or math.isnan(a):
            return None, "aguardando 200 candles"
        sd, td = self.bracket(a)
        uptrend = last["ema50"] > last["ema200"] and last["close"] > last["ema200"]
        downtrend = last["ema50"] < last["ema200"] and last["close"] < last["ema200"]
        if uptrend and prev["rsi14"] < 40 <= last["rsi14"] and sent > -0.3:
            return Signal("BUY", sd, td, f"recuo terminou em tendência de alta (RSI {last['rsi14']:.0f})"), "compra"
        if downtrend and prev["rsi14"] > 60 >= last["rsi14"] and sent < 0.3:
            return Signal("SELL", sd, td, f"repique terminou em tendência de baixa (RSI {last['rsi14']:.0f})"), "venda"
        trend = "alta" if uptrend else "baixa" if downtrend else "lateral"
        return None, f"tendência {trend}, RSI14 {last['rsi14']:.0f} — esperando recuo"


# ============================================================== LONGO PRAZO
class LongTermAgent(BaseAgent):
    """Candles diários. Só compra (por padrão) ativos em tendência de longo prazo:
    preço acima da média de 200 dias, média de 50 acima da de 200 e momento de
    ~6 meses positivo; entra quando o preço volta a fechar acima da média de 50.
    Sai quando o preço fecha abaixo da média de 200 (ou no stop largo)."""
    min_bars = 210

    def evaluate(self, epic, ind, sent):
        last, prev = ind.iloc[-1], ind.iloc[-2]
        a = _f(last["atr"])
        if math.isnan(_f(last["sma200"])) or math.isnan(a):
            return None, "aguardando 200 dias de histórico"
        mom = last["close"] / ind["close"].iloc[-min(len(ind), 121)] - 1
        regime = last["close"] > last["sma200"] and last["sma50"] > last["sma200"] and mom > 0
        sd, td = self.bracket(a)
        if regime and prev["close"] <= prev["sma50"] < last["close"] and sent > -0.5:
            return Signal("BUY", sd, td, f"tendência longa + retomada da média 50 (momento {mom:+.0%})"), "compra"
        bear = last["close"] < last["sma200"] and last["sma50"] < last["sma200"] and mom < 0
        if self.cfg.allow_short and bear and prev["close"] >= prev["sma50"] > last["close"] and sent < 0.5:
            return Signal("SELL", sd, td, f"tendência longa de baixa (momento {mom:+.0%})"), "venda"
        return None, f"{'regime de alta' if regime else 'sem regime de alta'} · momento 6m {mom:+.0%}"

    async def exit_reason(self, position, now):
        df = await self.ctx.market.candles(position["epic"], "DAY", 220)
        if len(df) < 205:
            return None
        ind = indicators(df.iloc[:-1])
        last = ind.iloc[-1]
        if position["direction"] == "BUY" and last["close"] < last["sma200"]:
            return "fechou abaixo da média de 200 dias"
        if position["direction"] == "SELL" and last["close"] > last["sma200"]:
            return "fechou acima da média de 200 dias"
        return None


# ============================================================== NOTÍCIAS
class NewsAgent(BaseAgent):
    """Reage em segundos a manchetes relevantes: se a IA classificar a notícia como
    muito relevante e claramente positiva/negativa para um ativo da lista, abre na
    direção da notícia com stop pelo ATR de 5 minutos. Uma operação por notícia e
    uma pausa por ativo depois de cada operação."""

    def __init__(self, cfg, ctx):
        super().__init__(cfg, ctx)
        self.queue: asyncio.Queue = asyncio.Queue()
        self.cooldown: dict[str, float] = {}

    def on_news(self, item: dict) -> None:
        self.queue.put_nowait(item)

    async def scan(self) -> None:
        while not self.queue.empty():
            item = self.queue.get_nowait()
            await self._consider(item)

    async def _consider(self, n: dict) -> None:
        epic = self.ctx.epic_for_ticker(n.get("ticker", ""))
        if not epic:
            return
        p = self.cfg.params
        rel, s = n.get("relevance") or 0, n.get("sentiment") or 0
        title = n.get("title", "")[:90]
        if rel < p.get("min_relevance", 0.6) or abs(s) < p.get("min_sentiment", 0.5):
            self.state[epic] = {"note": f"ignorada (rel {rel:.2f}, sent {s:+.2f}): {title}"}
            return
        try:
            age = (self.ctx.now() - datetime.fromisoformat(n["time"])).total_seconds() / 60
        except (KeyError, ValueError, TypeError):
            age = 0
        if age > p.get("max_age_min", 15):
            self.state[epic] = {"note": f"notícia velha ({age:.0f} min): {title}"}
            return
        if time.time() - self.cooldown.get(epic, 0) < p.get("cooldown_min", 30) * 60 / max(self.ctx.speed, 1):
            self.state[epic] = {"note": f"em pausa após trade recente: {title}"}
            return
        df = await self.ctx.market.candles(epic, self.cfg.resolution, 60)
        if len(df) < 20:
            return
        a = float(indicators(df.iloc[:-1])["atr"].iloc[-1])
        sd, td = self.bracket(a)
        direction = "BUY" if s > 0 else "SELL"
        self.state[epic] = {"note": f"{'COMPRA' if s > 0 else 'VENDA'} pela notícia: {title}", "signal": direction}
        self.cooldown[epic] = time.time()
        await self.ctx.orders.submit(self, epic, Signal(direction, sd, td,
                                                        f"notícia (sent {s:+.2f}, rel {rel:.2f}): {title}", rel))


# ============================================================== catálogo
def default_agents() -> list[tuple[type, AgentConfig]]:
    return [
        (ScalperAgent, AgentConfig(
            key="scalp", name="Scalper", style="Scalp", resolution="MINUTE", interval_s=3,
            description="Operações de minutos no gráfico de 1 min: recuos na micro-tendência, stop e alvo curtos.",
            risk_per_trade=0.005, margin_share=0.15, max_positions=2, stop_atr=1.0, tp_atr=1.5,
            max_hold_min=20, lookback=120, params={"max_spread_atr": 0.3})),
        (DayTraderAgent, AgentConfig(
            key="day", name="Day Trader (ML)", style="Day trade", resolution="MINUTE_15", interval_s=10,
            description="Modelo de Machine Learning + sentimento no gráfico de 15 min. Zera tudo antes do fim do dia.",
            risk_per_trade=0.01, margin_share=0.25, max_positions=2, stop_atr=1.5, tp_atr=2.25,
            max_hold_min=390, lookback=300,
            params={"threshold": 0.52, "sentiment_weight": 0.10, "no_entry_after": "20:00", "flat_at": "20:45"})),
        (SwingAgent, AgentConfig(
            key="swing", name="Swing Trader", style="Swing", resolution="HOUR_4", interval_s=60,
            description="Tendência no gráfico de 4 h (EMA 50/200), entra no fim dos recuos. Segura por dias.",
            risk_per_trade=0.015, margin_share=0.25, max_positions=2, stop_atr=2.0, tp_atr=4.0,
            max_hold_min=60 * 24 * 10, lookback=300)),
        (LongTermAgent, AgentConfig(
            key="long", name="Longo Prazo", style="Posição", resolution="DAY", interval_s=300, allow_short=False,
            description="Gráfico diário: só compra ativos em tendência de longo prazo e sai abaixo da média de 200 dias.",
            risk_per_trade=0.02, margin_share=0.25, max_positions=3, stop_atr=3.0, tp_atr=9.0,
            max_hold_min=60 * 24 * 120, lookback=320)),
        (NewsAgent, AgentConfig(
            key="news", name="Notícias (IA)", style="Evento", resolution="MINUTE_5", interval_s=1,
            description="Reage em segundos a notícias relevantes classificadas pela IA, na direção do sentimento.",
            risk_per_trade=0.01, margin_share=0.15, max_positions=2, stop_atr=1.5, tp_atr=3.0,
            max_hold_min=120, lookback=60,
            params={"min_relevance": 0.6, "min_sentiment": 0.5, "max_age_min": 15, "cooldown_min": 30})),
    ]
