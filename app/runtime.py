"""Orquestra o app: corretora, dados reais, notícias, pesquisa, agentes e gestor de risco.

Modos:
  papel    → preços, candles, volume e notícias REAIS (Alpaca + RSS); saldo e ordens virtuais.
  capital  → tudo real na sua conta DEMO da Capital.com (o saldo da demo é virtual).
  simulador→ só para testes automáticos (APP_MODE=sim). Nunca é escolhido sozinho.
"""
from __future__ import annotations

import asyncio
import logging
import os
import statistics
import time
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from trader.config import Settings, get_settings

from .agents.strategies import DayTraderAgent, NewsAgent, default_agents
from .bus import EventBus
from .data.alpaca import AlpacaData, is_crypto
from .db import AppDB
from .llm import LLMRouter, RouterScorer
from .market import MarketData
from .newsfeed import NewsFeed
from .orders import OrderManager
from .paper_real import PaperRealBroker
from .research import ResearchAgent
from .secure import Vault
from .sim import SimBroker

log = logging.getLogger(__name__)
NY = ZoneInfo("America/New_York")


class Runtime:
    def __init__(self, settings: Settings | None = None):
        given = settings is not None
        self.settings = settings or get_settings()
        self.data_dir = self.settings.db_path.parent
        self.db = AppDB(self.settings.db_path.with_name("app.db"))
        self.vault = Vault(self.db, self.data_dir)
        self.vault.apply()
        if not given:
            self.settings = get_settings()
        self._model_base = self.settings.model_dir
        self.bus = EventBus()
        self.tasks: list[asyncio.Task] = []
        self.broker = None
        self.broker_id = ""
        self.mode = ""
        self.connection_error = ""
        self.speed = 1.0
        self.max_total_positions = int(os.environ.get("MAX_TOTAL_POSITIONS", "6") or 6)
        self.model_status = "não treinado"
        self.last_account: dict = {}
        self.volumes: dict[str, dict] = {}
        self._daily_vol: dict[str, tuple[float, float]] = {}
        self.agents = []
        self.orders: OrderManager | None = None
        self.market: MarketData | None = None
        self.news: NewsFeed | None = None
        self.research: ResearchAgent | None = None
        self.router: LLMRouter | None = None
        self.alpaca: AlpacaData | None = None

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

    def research_bias(self, epic: str) -> float:
        return self.research.bias(epic) if self.research else 0.0

    def volume(self, epic: str) -> dict | None:
        return self.volumes.get(epic)

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

    # ------------------------------------------------------------ agentes
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
        if os.environ.get("APP_MODE", "").lower() == "sim":
            self.speed = float(os.environ.get("SIM_SPEED", "5") or 5)
            return (SimBroker(self.epics, start_balance=s.start_balance, leverage=s.max_leverage, speed=self.speed),
                    "simulador", "simulador")
        if s.broker == "capital_demo" and s.capital_api_key and s.capital_identifier and s.capital_password:
            from trader.capital import CapitalClient
            c = CapitalClient(s.capital_api_key, s.capital_identifier, s.capital_password,
                              account_id=s.capital_account_id)
            c.login()
            return c, "capital", f"capital:{c.current_account_id}"
        return PaperRealBroker(self.alpaca, self.db, s.start_balance, s.max_leverage), "papel", "papel"

    async def start(self) -> None:
        self.bus.bind(asyncio.get_running_loop())
        self.connection_error = ""
        self.alpaca = AlpacaData(os.environ.get("ALPACA_KEY_ID", ""), os.environ.get("ALPACA_SECRET_KEY", ""),
                                 os.environ.get("ALPACA_FEED", "iex"))
        self.router = LLMRouter()
        try:
            self.broker, self.mode, self.broker_id = await asyncio.to_thread(self._make_broker)
        except Exception as e:  # noqa: BLE001
            self.connection_error = str(e)
            log.error("Falha ao conectar na Capital.com: %s", e)
            self.settings.broker = "paper"
            self.broker, self.mode, self.broker_id = await asyncio.to_thread(self._make_broker)
            self.event(f"Não foi possível conectar na Capital.com ({e}). Operando em PAPEL com dados reais.", "error")
        if self.mode != "simulador":
            self.speed = 1.0
        # modelos separados por modo: o que aprendeu em dados sintéticos nunca vai para o mercado real
        self.settings.model_dir = self._model_base / ("simulador" if self.mode == "simulador" else "real")
        self.market = MarketData(self.broker, self.epics, self.bus)
        self.orders = OrderManager(self)
        self.orders.kill_switch = bool(self.db.get("kill_switch", False))
        self.news = NewsFeed(self, RouterScorer(self.router, [w.ticker for w in self.settings.watchlist]))
        self.research = ResearchAgent(self, self.router)
        self._build_agents()

        add = lambda coro: self.tasks.append(asyncio.create_task(coro))  # noqa: E731
        if self.mode == "simulador":
            add(self.market.run_sim_ticks(self.orders.on_sim_closed))
            add(self.news.run_sim())
        else:
            if self.mode == "capital":
                add(self.market.run_capital_stream())
            else:
                add(self.market.run_paper_quotes(self.alpaca, self.broker, self.orders.on_sim_closed))
            add(self.news.run())
            add(self._volume_loop())
            add(self.research.run())
            add(self._llm_health_loop())
        add(self._sync_loop())
        add(self._account_loop())
        add(self._train_model())
        await asyncio.sleep(1.5)
        for a in self.agents:
            add(a.run())
        names = {"capital": "Capital.com DEMO (dados e ordens reais, saldo da demo)",
                 "papel": "PAPEL com dados reais (saldo e ordens virtuais)", "simulador": "simulador (testes)"}
        self.event(f"App iniciado — {names[self.mode]}, {len(self.agents)} agentes + pesquisa, IA: {self.router.active()}", "info")
        if self.mode == "papel" and not self.alpaca.has_keys and any(not is_crypto(e) for e in self.epics):
            self.event("Configure as chaves da Alpaca em Configurações para receber preços e volume reais das ações.", "warn")
        self.bus.publish("status", self.status())

    async def stop(self) -> None:
        for t in self.tasks:
            t.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        self.tasks = []

    async def restart(self) -> None:
        await self.stop()
        self.vault.apply()
        self.settings = get_settings()
        self._model_base = self.settings.model_dir
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

    async def _llm_health_loop(self) -> None:
        while True:
            await asyncio.to_thread(self.router.health, True)
            self.bus.publish("status", {"llm": self.router.status()})
            await asyncio.sleep(120)

    async def _volume_loop(self) -> None:
        """Volume CONSOLIDADO das bolsas dos EUA (Alpaca/SIP) e volume relativo (RVOL)."""
        while True:
            try:
                if self.alpaca.has_keys:
                    stocks = [e for e in self.epics if not is_crypto(e)]
                    cons = await asyncio.to_thread(self.alpaca.consolidated_volume, stocks)
                    for epic, v in cons.items():
                        avg = await self._avg_daily_volume(epic)
                        frac = self._session_fraction(v.get("as_of"))
                        rvol = (v["day_volume"] / (avg * frac)) if (avg and v.get("day_volume") and frac) else None
                        self.volumes[epic] = {**v, "avg20": avg, "rvol": round(rvol, 2) if rvol else None,
                                              "source": "SIP (todas as bolsas dos EUA)",
                                              "delay": "15 min" if v.get("delayed") else "tempo real"}
                    if cons:
                        self.bus.publish("volume", self.volumes)
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                log.warning("volume: %s", e)
            await asyncio.sleep(60)

    async def _avg_daily_volume(self, epic: str) -> float | None:
        cached = self._daily_vol.get(epic)
        if cached and time.time() - cached[0] < 6 * 3600:
            return cached[1]
        bars = await asyncio.to_thread(self.alpaca.sip_candles, epic, "DAY", 25)
        today = datetime.now(NY).date().isoformat()
        vols = [b.volume for b in bars if not b.time.startswith(today)][-20:]
        avg = statistics.mean(vols) if vols else None
        self._daily_vol[epic] = (time.time(), avg)
        return avg

    @staticmethod
    def _session_fraction(as_of: str | None) -> float:
        try:
            t = datetime.fromisoformat(str(as_of).replace("Z", "+00:00")).astimezone(NY)
        except (TypeError, ValueError):
            return 1.0
        mins = (t.hour * 60 + t.minute) - (9 * 60 + 30)
        return max(0.05, min(1.0, mins / 390)) if mins > 0 else 1.0

    async def _train_model(self, force: bool = False) -> None:
        from trader.features import to_frame
        from trader.model import make_dataset, train_and_maybe_promote
        day = next((a for a in self.agents if isinstance(a, DayTraderAgent)), None)
        if day is None:
            return
        key = f"model_trained_at:{self.mode}"
        last = self.db.get(key) or 0
        if day.model.ready and not force and time.time() - last < 7 * 86400:
            self.model_status = f"pronto ({day.model.report.version})"
            return
        self.model_status = "treinando com dados reais…" if self.mode != "simulador" else "treinando…"
        self.bus.publish("status", self.status())
        try:
            def work():
                s = self.settings
                frames = {}
                for e in self.epics:
                    try:
                        df = to_frame(self.broker.candles(e, day.cfg.resolution, s.history_bars))
                    except Exception as ex:  # noqa: BLE001
                        log.warning("treino: sem candles de %s: %s", e, ex)
                        continue
                    if len(df) > 60:
                        frames[e] = df
                data = make_dataset(frames, day.cfg.stop_atr, day.cfg.tp_atr)
                return train_and_maybe_promote(data, s.model_dir, day.cfg.params.get("threshold", 0.52),
                                               day.cfg.stop_atr, day.cfg.tp_atr)
            rep = await asyncio.to_thread(work)
            self.db.add_model_report(rep)
            self.db.set(key, time.time())
            day.reload_model()
            self.model_status = (f"pronto ({day.model.report.version})" if day.model.ready else "sem modelo aprovado")
            self.event(f"Modelo ML: AUC {rep.auc_long:.3f}/{rep.auc_short:.3f}, {rep.expectancy_r:+.3f}R em "
                       f"{rep.trades} trades de teste — {rep.note}", "model")
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            self.model_status = f"sem modelo: {e}"
            self.event(f"Treino do modelo não concluído: {e}", "warn")
        self.bus.publish("status", self.status())

    # ------------------------------------------------------------ estado para o painel
    def status(self) -> dict:
        s = self.settings
        return {"mode": self.mode, "broker_id": self.broker_id, "connection_error": self.connection_error,
                "stream": self.market.stream_status if self.market else "", "speed": self.speed,
                "scorer": self.router.active() if self.router else "", "llm": self.router.status() if self.router else {},
                "model": self.model_status, "kill_switch": self.orders.kill_switch if self.orders else False,
                "news_sources": self.news.sources_status if self.news else {},
                "research": {"status": self.research.status if self.research else "",
                             "sec": self.research.sec.status if self.research else "",
                             "stocktwits": self.research.twits.status if self.research else "",
                             "interval_min": self.research.interval_min if self.research else None},
                "alpaca": {"keys": bool(self.alpaca and self.alpaca.has_keys), "feed": self.alpaca.feed if self.alpaca else "",
                           "error": self.alpaca.last_error if self.alpaca else ""},
                "start": s.start_balance, "target": s.target_balance, "max_daily_loss": s.max_daily_loss,
                "stop_at_target": s.stop_at_target, "epics": self.epics, "sim_time": self.now().isoformat()}
