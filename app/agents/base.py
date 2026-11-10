"""Base dos agentes de trading.

Cada agente tem um estilo (scalp, day trade, swing, longo prazo, notícias), um
tempo gráfico, regras de entrada/saída e um orçamento de risco. Ele só PROPÕE
ordens; quem decide se a ordem é enviada é o gestor de risco (OrderManager).
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import asdict, dataclass, field
from datetime import datetime

import pandas as pd

from trader.features import atr, ema, rsi

log = logging.getLogger(__name__)


@dataclass
class Signal:
    direction: str            # BUY | SELL
    stop_dist: float          # distância do stop em preço
    tp_dist: float            # distância do alvo em preço
    reason: str
    confidence: float = 0.5


@dataclass
class AgentConfig:
    key: str
    name: str
    style: str
    description: str
    resolution: str
    interval_s: float
    risk_per_trade: float      # fração da banca arriscada por trade
    margin_share: float        # fração máxima da banca que o agente pode prender em margem
    max_positions: int
    stop_atr: float
    tp_atr: float
    max_hold_min: float | None
    enabled: bool = True
    allow_short: bool = True
    lookback: int = 300
    params: dict = field(default_factory=dict)

    EDITABLE = ("enabled", "risk_per_trade", "margin_share", "max_positions", "stop_atr", "tp_atr",
                "max_hold_min", "allow_short")


def indicators(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    c = out["close"]
    for n in (9, 21, 50, 200):
        out[f"ema{n}"] = ema(c, n)
    out["sma50"] = c.rolling(50).mean()
    out["sma200"] = c.rolling(200).mean()
    out["rsi7"] = rsi(c, 7)
    out["rsi14"] = rsi(c, 14)
    out["atr"] = atr(out, 14)
    return out


class BaseAgent:
    min_bars = 60

    def __init__(self, cfg: AgentConfig, ctx):
        self.cfg = cfg
        self.ctx = ctx
        self.last_bar: dict[str, str] = {}
        self.state: dict[str, dict] = {}          # última leitura por ativo (para o painel)
        self.status = "aguardando"
        self.paused_reason = ""

    # ------------------------------------------------------------ painel
    def snapshot(self) -> dict:
        return {"config": asdict(self.cfg), "status": self.status, "paused_reason": self.paused_reason,
                "state": self.state}

    def update(self, changes: dict) -> None:
        for k, v in changes.items():
            if k in AgentConfig.EDITABLE:
                cur = getattr(self.cfg, k)
                if isinstance(cur, bool) or k in ("enabled", "allow_short"):
                    v = bool(v)
                elif isinstance(cur, int) and not isinstance(cur, bool):
                    v = int(v)
                elif v is not None:
                    v = float(v)
                setattr(self.cfg, k, v)
        if changes.get("enabled"):
            self.paused_reason = ""

    # ------------------------------------------------------------ ciclo
    def epics(self) -> list[str]:
        return self.ctx.epics

    async def run(self) -> None:
        while True:
            try:
                if self.cfg.enabled:
                    self.status = "ativo"
                    await self.manage_positions()
                    await self.scan()
                else:
                    self.status = "pausado" + (f": {self.paused_reason}" if self.paused_reason else "")
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001 — um agente com erro não derruba os outros
                log.exception("Agente %s", self.cfg.key)
                self.status = f"erro: {e}"
                self.ctx.event(f"{self.cfg.name}: erro {e}", "error")
            await asyncio.sleep(self.cfg.interval_s)

    async def scan(self) -> None:
        for epic in self.epics():
            try:
                df = await self.ctx.market.candles(epic, self.cfg.resolution, self.cfg.lookback)
            except Exception as e:  # noqa: BLE001 — sem dados reais para este ativo: pula, sem derrubar os outros
                self.state[epic] = {"note": f"sem dados reais: {e}"}
                continue
            if len(df) < self.min_bars + 1:
                self.state[epic] = {"note": f"histórico curto ({len(df)} barras)"}
                continue
            done = df.iloc[:-1]                    # decide só com candles FECHADOS
            bar = str(done["time"].iloc[-1])
            if self.last_bar.get(epic) == bar:
                continue
            self.last_bar[epic] = bar
            if any(p["epic"] == epic for p in self.ctx.orders.positions_of(self.cfg.key)):
                self.state[epic] = {"bar": bar, "note": "posição aberta — acompanhando saída", "signal": None}
                continue
            sent = self.ctx.sentiment(epic)
            ind = indicators(done)
            sig, note = self.evaluate(epic, ind, sent)
            self.state[epic] = {"bar": bar, "note": note, "sentiment": round(sent, 3),
                                "signal": sig.direction if sig else None}
            if sig and (sig.direction == "BUY" or self.cfg.allow_short):
                await self.ctx.orders.submit(self, epic, sig)

    def evaluate(self, epic: str, ind: pd.DataFrame, sentiment: float) -> tuple[Signal | None, str]:
        raise NotImplementedError

    # ------------------------------------------------------------ saídas
    async def manage_positions(self) -> None:
        now = self.ctx.now()
        for p in self.ctx.orders.positions_of(self.cfg.key):
            reason = None
            opened = p.get("opened_sim") or p.get("opened_at")
            if self.cfg.max_hold_min and opened:
                age = (now - datetime.fromisoformat(opened)).total_seconds() / 60
                if age >= self.cfg.max_hold_min:
                    reason = f"tempo máximo de {self.cfg.max_hold_min:g} min"
            if reason is None:
                reason = await self.exit_reason(p, now)
            if reason:
                await self.ctx.orders.close(p["deal_id"], reason)

    async def exit_reason(self, position: dict, now: datetime) -> str | None:
        return None

    def rvol(self, epic: str) -> float | None:
        """Volume relativo consolidado (1 = volume normal para o horário). None = sem dado."""
        fn = getattr(self.ctx, "volume", None)
        v = fn(epic) if fn else None
        return v.get("rvol") if v else None

    def rbias(self, epic: str) -> float:
        """Viés do Agente de Pesquisa (−1..+1, já ponderado pela confiança)."""
        fn = getattr(self.ctx, "research_bias", None)
        return float(fn(epic)) if fn else 0.0

    def bracket(self, a: float) -> tuple[float, float]:
        return self.cfg.stop_atr * a, self.cfg.tp_atr * a
