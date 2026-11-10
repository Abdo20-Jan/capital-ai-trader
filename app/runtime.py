"""Orquestra o app: corretora, preços ao vivo, notícias, agentes, gestor de risco."""
from __future__ import annotations

import asyncio
import logging
import os
import time
from datetime import datetime, timezone

from trader.config import Settings, get_settings
from trader.sentiment import make_scorer

from .agents.strategies import DayTraderAgent, NewsAgent, default_agents
from .bus import EventBus
from .db import AppDB
from .market import MarketData
from .newsfeed import NewsFeed
from .orders import OrderManager
from .sim import SimBroker

log = logging.getLogger(__name__)


class Runtime:
    def __init__(self, settings: Settings | None = None):
        self.settings = settings or get_settings()
        self.bus = EventBus()
        self.db = AppDB(self.settings.db_path.with_name("app.db"))
        self.tasks: list[asyncio.Task] = []
        self.broker = None
        self.broker_id = ""
        self.mode = ""
        self.connection_error = ""
        self.speed = 1.0
        self.max_total_positions = int(os.environ.get("MAX_TOTAL_POSITIONS", "6") or 6)
        self.model_status = "não treinado"
        self.last_account: dict = {}
        self.agents = []
        self.orders: OrderManager | None = None
        self.market: MarketData | None = None
        self.news: NewsFeed | None = None

    # ------------------------------------------------------------ helpers usados pelos agentes
    @property
    def epics(self) -> list[str]:
        return [w.epic for w in self.settings.watchlist]

    def now(self) -> datetime:
        b = self.broker
        return b.now() if b is not None and hasattr(b, "now") else datetime.now(timezone.utc)

    def ticker_for(self, epic: str) -> str:
        return next((w.ticker for w in self.settings.watchlist if w.epic == epic), epic)

    def epic_for_ticker(self, ticker: str) -> str | None:
        return next((w.epic for w in self.settings.watchlist if w.ticker == ticker), None)

    def sentiment(self, epic: str) -> float:
        return self.news.sentiment_for(self.ticker_for(epic)) if self.news else 0.0

    def agent_name(self, key: str | None) -> str:
        a = next((a for a in self.agents if a.cfg.key == key), None)
        return a.cfg.name if a else (key or "?")

    def event(self, message: str, level: str = "info") -> None:
        self.db.event(message, level)
        self.bus.publish("event", {"time": datetime.now(timezone.utc).isoformat(), "level": level, "message": message})

    def on_news(self, item: dict) -> None:
        for a in self.agents:
            if isinstance(a, NewsAgent) and a.cfg.enabled:
                a.on_news(item)

    def on_trade_closed(self, agent_key: str | None) -> None:
        """Auto-avaliação: pausa um agente cujo resultado recente está claramente ruim."""
        if not agent_key:
            return
        st = self.db.agent_stats(agent_key, self.broker_id, last=20)
        a = next((x for x in self.agents if x.cfg.key == agent_key), None)
        if a and st["recent_n"] >= 15 and (st["expectancy_r"] or 0) < -0.25 and a.cfg.enabled:
            a.cfg.enabled = False
            a.paused_reason = f"auto-avaliação: {st['expectancy_r']:+.2f}R nos últimos {st['recent_n']} trades"
            self.event(f"{a.cfg.name} pausado — {a.paused_reason}. Reative no painel quando quiser.", "warn")
            self.save_agent_configs()

    # ------------------------------------------------------------ configuração dos agentes
    def save_agent_configs(self) -> None:
        self.db.set_json("agents", {a.cfg.key: {k: getattr(a.cfg, k) for k in a.cfg.EDITABLE} for a in self.agents})

    def _build_agents(self) -> None:
        saved = self.db.get_json("agents", {}) or {}
        self.agents = []
        for cls, cfg in default_agents():
            a = cls(cfg, self)
            if cfg.key in saved:
                a.update(saved[cfg.key])
            self.agents.append(a)

    # ------------------------------------------------------------ corretora
    def _make_broker(self):
        s = self.settings
        if s.broker == "capital_demo" and s.capital_api_key and s.capital_identifier and s.capital_password:
            from trader.capital import CapitalClient
            c = CapitalClient(s.capital_api_key, s.capital_identifier, s.capital_password,
                              account_id=s.capital_account_id)
            c.login()
            return c, "capital", f"capital:{c.current_account_id}"
        self.speed = float(os.environ.get("SIM_SPEED", "5") or 5)
        return (SimBroker(self.epics, start_balance=s.start_balance, leverage=s.max_leverage, speed=self.speed),
                "simulador", "simulador")

    async def start(self) -> None:
        self.bus.bind(asyncio.get_running_loop())
        self.connection_error = ""
        try:
            self.broker, self.mode, self.broker_id = await asyncio.to_thread(self._make_broker)
        except Exception as e:  # noqa: BLE001
            self.connection_error = str(e)
            log.error("Falha ao conectar na Capital.com: %s — usando o simulador", e)
            self.settings.broker = "paper"
            self.broker, self.mode, self.broker_id = await asyncio.to_thread(self._make_broker)
            self.event(f"Não foi possível conectar na Capital.com ({e}). Rodando no SIMULADOR.", "error")
        if self.mode == "capital":
            self.speed = 1.0
        # modelos separados: o que aprendeu no simulador NUNCA é usado no mercado real
        base_dir = self._model_base = getattr(self, "_model_base", None) or self.settings.model_dir
        self.settings.model_dir = base_dir / ("capital" if self.mode == "capital" else "simulador")
        self.market = MarketData(self.broker, self.epics, self.bus)
        self.orders = OrderManager(self)
        self.orders.kill_switch = bool(self.db.get("kill_switch", False))
        self.news = NewsFeed(self, make_scorer(self.settings.anthropic_api_key, self.settings.claude_model,
                                               [w.ticker for w in self.settings.watchlist]))
        self._build_agents()

        if self.mode == "simulador":
            self.tasks.append(asyncio.create_task(self.market.run_sim_ticks(self.orders.on_sim_closed)))
            self.tasks.append(asyncio.create_task(self.news.run_sim()))
        else:
            self.tasks.append(asyncio.create_task(self.market.run_capital_stream()))
            self.tasks.append(asyncio.create_task(self.news.run()))
        self.tasks.append(asyncio.create_task(self._sync_loop()))
        self.tasks.append(asyncio.create_task(self._account_loop()))
        self.tasks.append(asyncio.create_task(self._train_model()))
        await asyncio.sleep(1.5)                              # primeiras cotações antes dos agentes
        for a in self.agents:
            self.tasks.append(asyncio.create_task(a.run()))
        self.event(f"App iniciado — corretora: {'Capital.com DEMO' if self.mode == 'capital' else 'simulador'}, "
                   f"{len(self.agents)} agentes, sentimento: {self.news.scorer.name}", "info")
        self.bus.publish("status", self.status())

    async def stop(self) -> None:
        for t in self.tasks:
            t.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        self.tasks = []

    async def restart(self) -> None:
        await self.stop()
        self.settings = get_settings()
        if getattr(self, "_model_base", None):
            self.settings.model_dir = self._model_base
        await self.start()

    # ------------------------------------------------------------ laços
    async def _sync_loop(self) -> None:
        while True:
            try:
                await self.orders.sync()
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                log.warning("sync: %s", e)
            await asyncio.sleep(3)

    async def _account_loop(self) -> None:
        while True:
            try:
                real = await asyncio.to_thread(self.broker.account)
                acct = self.orders.bot_account(real)
                ok, msg = self.orders.guard(acct)
                if msg != self.orders.guard_msg:
                    self.orders.guard_msg = msg
                    if not ok:
                        self.event(msg, "warn")
                self.last_account = {"equity": acct.equity, "balance": acct.balance, "available": acct.available,
                                     "real_equity": real.equity, "currency": real.currency, "can_trade": ok,
                                     "guard": msg, "time": time.time()}
                self.db.add_app_equity(self.broker_id, acct.equity, acct.balance, real.equity)
                self.bus.publish("account", self.last_account)
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                log.warning("conta: %s", e)
            await asyncio.sleep(5)

    async def _train_model(self, force: bool = False) -> None:
        from trader.model import make_dataset, train_and_maybe_promote
        from trader.features import to_frame
        day = next((a for a in self.agents if isinstance(a, DayTraderAgent)), None)
        if day is None:
            return
        last = self.db.get(f"model_trained_at:{self.mode}") or 0
        if day.model.ready and not force and time.time() - last < 7 * 86400:
            self.model_status = f"pronto ({day.model.report.version})"
            return
        self.model_status = "treinando…"
        self.bus.publish("status", self.status())
        try:
            def work():
                s = self.settings
                frames = {}
                for e in self.epics:
                    df = to_frame(self.broker.candles(e, day.cfg.resolution, s.history_bars))
                    if len(df) > 60:
                        frames[e] = df
                data = make_dataset(frames, day.cfg.stop_atr, day.cfg.tp_atr)
                return train_and_maybe_promote(data, s.model_dir, day.cfg.params.get("threshold", 0.52),
                                               day.cfg.stop_atr, day.cfg.tp_atr)
            rep = await asyncio.to_thread(work)
            self.db.add_model_report(rep)
            self.db.set(f"model_trained_at:{self.mode}", time.time())
            day.reload_model()
            self.model_status = (f"pronto ({day.model.report.version})" if day.model.ready
                                 else "sem modelo aprovado")
            self.event(f"Modelo ML: AUC {rep.auc_long:.3f}/{rep.auc_short:.3f}, {rep.expectancy_r:+.3f}R em "
                       f"{rep.trades} trades de teste — {rep.note}", "model")
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            self.model_status = f"erro no treino: {e}"
            self.event(f"Treino do modelo falhou: {e}", "error")
        self.bus.publish("status", self.status())

    # ------------------------------------------------------------ estado para o painel
    def status(self) -> dict:
        s = self.settings
        return {"mode": self.mode, "broker_id": self.broker_id, "connection_error": self.connection_error,
                "stream": self.market.stream_status if self.market else "", "speed": self.speed,
                "scorer": self.news.scorer.name if self.news else "", "model": self.model_status,
                "kill_switch": self.orders.kill_switch if self.orders else False,
                "news_sources": self.news.sources_status if self.news else {},
                "start": s.start_balance, "target": s.target_balance, "max_daily_loss": s.max_daily_loss,
                "stop_at_target": s.stop_at_target, "epics": self.epics,
                "sim_time": self.now().isoformat()}
