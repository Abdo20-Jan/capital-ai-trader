"""API do app (FastAPI).

Local:   python run.py app      → http://localhost:8000 (só no seu computador)
Nuvem:   python run.py serve    → escuta em 0.0.0.0:$PORT, EXIGE APP_PASSWORD
"""
from __future__ import annotations

import asyncio
import json
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from trader.sentiment import TOPIC_LABELS

from .runtime import Runtime
from .secure import VAULT_KEYS, Auth

STATIC = Path(__file__).parent / "static"
OPEN_PATHS = {"/api/health", "/api/login"}


class Login(BaseModel):
    password: str


class Creds(BaseModel):
    api_key: str = ""
    identifier: str = ""
    password: str = ""
    account_id: str = ""


class AgentPatch(BaseModel):
    enabled: bool | None = None
    risk_per_trade: float | None = None
    margin_share: float | None = None
    max_positions: int | None = None
    stop_atr: float | None = None
    tp_atr: float | None = None
    max_hold_min: float | None = None
    allow_short: bool | None = None


class RiskPatch(BaseModel):
    start_balance: float | None = None
    target_balance: float | None = None
    max_daily_loss: float | None = None
    stop_at_target: bool | None = None


class SettingsPatch(BaseModel):
    values: dict[str, str]
    restart: bool = True


def create_app(runtime: Runtime | None = None, public: bool = False) -> FastAPI:
    rt = runtime or Runtime()
    auth = Auth(rt.data_dir, public)

    @asynccontextmanager
    async def lifespan(app):
        if not auth.blocked:
            await rt.start()
        yield
        await rt.stop()

    app = FastAPI(title="Capital AI Trader", lifespan=lifespan)
    app.state.rt = rt
    origins = [o.strip() for o in os.environ.get("FRONTEND_ORIGINS", "").split(",") if o.strip()]
    if origins:                                          # ex.: o domínio do Vercel
        app.add_middleware(CORSMiddleware, allow_origins=origins, allow_methods=["*"],
                           allow_headers=["Authorization", "Content-Type"])

    @app.middleware("http")
    async def guard(request: Request, call_next):
        path = request.url.path
        if path.startswith("/api/") and path not in OPEN_PATHS and request.method != "OPTIONS":
            if auth.blocked:
                return JSONResponse({"detail": "Defina APP_PASSWORD no servidor para liberar o app."}, status_code=503)
            token = request.headers.get("authorization", "").removeprefix("Bearer ").strip() or request.query_params.get("token")
            if not auth.verify(token):
                return JSONResponse({"detail": "login necessário"}, status_code=401)
        return await call_next(request)

    # ---------------------------------------------------------------- login
    @app.get("/api/health")
    def health():
        return {"ok": True, "auth_required": auth.enabled or public, "blocked": auth.blocked, "mode": rt.mode}

    @app.post("/api/login")
    def login(body: Login):
        if not auth.enabled:
            if public:
                raise HTTPException(503, "Defina APP_PASSWORD no servidor.")
            return {"token": "local"}
        if not auth.check_password(body.password):
            raise HTTPException(401, "senha incorreta")
        return {"token": auth.issue()}

    # ---------------------------------------------------------------- tempo real
    @app.get("/api/stream")
    async def stream():
        return StreamingResponse(rt.bus.stream(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    @app.get("/api/state")
    def state():
        db = rt.db
        agents = []
        for a in rt.agents:
            snap = a.snapshot()
            snap["stats"] = db.agent_stats(a.cfg.key, rt.broker_id)
            snap["open"] = len(rt.orders.positions_of(a.cfg.key)) if rt.orders else 0
            agents.append(snap)
        research = {}
        if rt.research:
            for e in rt.epics:
                b = rt.research.briefs.get(e)
                if b:
                    research[e] = {**b, "social": rt.research.social.get(e)}
        return {
            "status": rt.status(),
            "account": rt.last_account,
            "agents": agents,
            "quotes": rt.market.quotes if rt.market else {},
            "volumes": rt.volumes,
            "research": research,
            "positions": list(rt.orders.live.values()) if rt.orders else [],
            "news": db.rows("SELECT * FROM news ORDER BY ingested DESC LIMIT 120"),
            "sentiment": [{"epic": e, "ticker": rt.ticker_for(e), "score": rt.sentiment(e)} for e in rt.epics],
            "topics": TOPIC_LABELS,
            "decisions": db.rows("SELECT * FROM decisions ORDER BY id DESC LIMIT 120"),
            "trades": db.rows("SELECT * FROM agent_trades WHERE broker=? AND status='CLOSED' ORDER BY closed DESC LIMIT 100",
                              (rt.broker_id,)),
            "equity": db.rows("SELECT time, equity, real_equity FROM (SELECT rowid, * FROM app_equity WHERE broker=? "
                              "ORDER BY rowid DESC LIMIT 720) ORDER BY rowid", (rt.broker_id,)),
            "events": db.rows("SELECT * FROM events ORDER BY id DESC LIMIT 80"),
            "model": (db.rows("SELECT report FROM models ORDER BY time DESC LIMIT 1") or [{}])[0].get("report"),
        }

    @app.get("/api/candles")
    async def candles(epic: str, res: str = "MINUTE_15", count: int = 150):
        if epic not in rt.epics:
            raise HTTPException(404, "ativo fora da lista")
        try:
            df = await rt.market.candles(epic, res, min(count, 500))
        except Exception as e:  # noqa: BLE001
            raise HTTPException(502, f"sem candles reais para {epic}: {e}") from e
        out = {"candles": [{"t": str(r.time), "o": r.open, "h": r.high, "l": r.low, "c": r.close, "v": r.volume}
                           for r in df.itertuples()], "volume": [], "volume_source": ""}
        if rt.alpaca and rt.alpaca.supports(epic) and rt.mode != "simulador":
            try:
                bars = await asyncio.to_thread(rt.alpaca.sip_candles, epic, res, min(count, 500))
                out["volume"] = [{"t": b.time, "v": b.volume} for b in bars]
                out["volume_source"] = ("cripto: só o volume da própria Alpaca, não o volume global" if epic.endswith("USD") and len(epic) <= 7
                                        else "consolidado SIP" + ("" if rt.alpaca.realtime_volume else " · atraso 15 min"))
            except Exception as e:  # noqa: BLE001
                out["volume_source"] = f"volume indisponível: {e}"
        return out

    @app.get("/api/research/{epic}")
    def research_history(epic: str):
        rows = rt.db.rows("SELECT id, time, provider, brief FROM research WHERE epic=? ORDER BY id DESC LIMIT 10", (epic,))
        last = rt.db.rows("SELECT dossier FROM research WHERE epic=? ORDER BY id DESC LIMIT 1", (epic,))
        return {"history": [{**r, "brief": json.loads(r["brief"])} for r in rows],
                "dossier": json.loads(last[0]["dossier"]) if last else None}

    @app.post("/api/research/{epic}/run")
    async def research_now(epic: str):
        if epic not in rt.epics or not rt.research:
            raise HTTPException(404, "ativo fora da lista")
        brief = await rt.research.research(epic)
        return {"ok": True, "brief": brief}

    # ---------------------------------------------------------------- controles
    @app.post("/api/agents/{key}")
    def patch_agent(key: str, patch: AgentPatch):
        a = next((x for x in rt.agents if x.cfg.key == key), None)
        if not a:
            raise HTTPException(404, "agente não encontrado")
        changes = {k: v for k, v in patch.model_dump().items() if v is not None}
        a.update(changes)
        rt.save_agent_configs()
        rt.event(f"{a.cfg.name}: configuração alterada ({', '.join(changes)})")
        return a.snapshot()

    @app.post("/api/kill")
    def kill(on: bool = True):
        rt.orders.kill_switch = on
        rt.db.set("kill_switch", on)
        rt.event("PAUSA GERAL ligada — nenhum agente abre posição" if on else "Pausa geral desligada", "warn")
        rt.bus.publish("status", rt.status())
        return {"kill_switch": on}

    @app.post("/api/close/{deal_id}")
    async def close(deal_id: str):
        return {"ok": await rt.orders.close(deal_id, "fechada manualmente no painel")}

    @app.post("/api/close-all")
    async def close_all():
        ids = list(rt.orders.live)
        results = [await rt.orders.close(d, "fechar todas (painel)") for d in ids]
        return {"closed": sum(results), "total": len(ids)}

    @app.post("/api/risk")
    def risk(patch: RiskPatch):
        s = rt.settings
        vals = {}
        if patch.start_balance is not None:
            s.start_balance = vals["START_BALANCE"] = float(patch.start_balance)
        if patch.target_balance is not None:
            s.target_balance = vals["TARGET_BALANCE"] = float(patch.target_balance)
        if patch.max_daily_loss is not None:
            s.max_daily_loss = vals["MAX_DAILY_LOSS"] = float(patch.max_daily_loss)
        if patch.stop_at_target is not None:
            s.stop_at_target = bool(patch.stop_at_target)
            vals["STOP_AT_TARGET"] = "1" if patch.stop_at_target else "0"
        if vals:
            rt.vault.save({k: str(v) for k, v in vals.items()})
            rt.event("Regras de risco atualizadas: " + ", ".join(f"{k}={v}" for k, v in vals.items()), "warn")
        return rt.status()

    @app.post("/api/bankroll/reset")
    def reset_bankroll():
        rt.orders.reset_bankroll()
        if rt.mode == "papel":
            rt.db.set("paper_cash", rt.settings.start_balance)
        rt.event(f"Banca virtual reiniciada em ${rt.settings.start_balance:.2f}", "info")
        return {"ok": True}

    @app.post("/api/model/retrain")
    async def retrain():
        asyncio.create_task(rt._train_model(force=True))
        return {"ok": True}

    # ---------------------------------------------------------------- configurações
    @app.get("/api/settings")
    def get_settings_view():
        return {"values": rt.vault.public_view(), "mode": rt.mode, "broker_id": rt.broker_id,
                "error": rt.connection_error, "llm": rt.router.status() if rt.router else {},
                "stream": rt.market.stream_status if rt.market else "", "public": public}

    @app.post("/api/settings")
    async def save_settings(body: SettingsPatch):
        vals = {k: v.strip() for k, v in body.values.items() if k in VAULT_KEYS and v is not None and v.strip() != ""}
        if not vals:
            raise HTTPException(400, "nada para salvar")
        rt.vault.save(vals)
        rt.event("Configurações salvas: " + ", ".join(sorted(vals)), "info")
        if body.restart:
            await rt.restart()
        return {"mode": rt.mode, "error": rt.connection_error, "llm": rt.router.status() if rt.router else {}}

    def _capital(c: Creds):
        from trader.capital import CapitalClient
        s = rt.settings
        return CapitalClient(c.api_key or s.capital_api_key, c.identifier or s.capital_identifier,
                             c.password or s.capital_password, account_id=c.account_id or s.capital_account_id)

    @app.post("/api/settings/test-capital")
    async def test_capital(c: Creds):
        def work():
            cl = _capital(c)
            cl.login()
            accts = cl.accounts()
            try:
                hedging = cl.preferences().get("hedgingMode")
            except Exception:  # noqa: BLE001
                hedging = None
            return {"ok": True, "current": cl.current_account_id, "hedging": hedging,
                    "accounts": [{"id": a.get("accountId"), "name": a.get("accountName"), "type": a.get("accountType"),
                                  "currency": a.get("currency"), "balance": (a.get("balance") or {}).get("balance")}
                                 for a in accts]}
        try:
            return await asyncio.to_thread(work)
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "error": str(e)}

    @app.post("/api/settings/test-alpaca")
    async def test_alpaca(body: SettingsPatch):
        from .data.alpaca import AlpacaData, is_crypto
        v = body.values
        a = AlpacaData(v.get("ALPACA_KEY_ID") or os.environ.get("ALPACA_KEY_ID", ""),
                       v.get("ALPACA_SECRET_KEY") or os.environ.get("ALPACA_SECRET_KEY", ""),
                       v.get("ALPACA_FEED") or os.environ.get("ALPACA_FEED", "iex"))
        stocks = [e for e in rt.epics if not is_crypto(e)][:3] or ["AAPL"]
        try:
            snap = await asyncio.to_thread(a.snapshots, stocks)
            vol = await asyncio.to_thread(a.consolidated_volume, stocks)
            return {"ok": True, "quotes": {k: {"last": s["last"], "bid": s["bid"], "ask": s["ask"]} for k, s in snap.items()},
                    "volume": vol}
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "error": str(e)}

    @app.post("/api/settings/test-llm")
    async def test_llm(body: SettingsPatch):
        from .llm import LLMRouter
        saved = {k: os.environ.get(k) for k in body.values}
        for k, val in body.values.items():
            if k in VAULT_KEYS and val:
                os.environ[k] = val
        r = LLMRouter()
        for k, old in saved.items():                          # teste não altera a configuração ativa
            if old is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = old
        ok = await asyncio.to_thread(r.health, True)
        schema = {"type": "object", "properties": {"sentiment": {"type": "number"}}, "required": ["sentiment"],
                  "additionalProperties": False}
        t0 = asyncio.get_running_loop().time()
        out, provider = await asyncio.to_thread(
            r.json, "Return the sentiment (-1..1) of the headline for the stock.",
            "Headline: Apple beats earnings estimates and raises guidance", schema, 200)
        return {"local_ok": ok, "provider": provider, "result": out, "seconds": round(asyncio.get_running_loop().time() - t0, 1),
                "error": r.last_error, "active": r.active()}

    @app.post("/api/connection/test-order")
    async def test_order(epic: str):
        if rt.mode != "capital":
            raise HTTPException(400, "Conecte a Capital.com primeiro")
        b = rt.broker

        def work():
            import time as _t
            r, q = b.rules(epic), b.quote(epic)
            if not q.tradeable:
                return {"ok": False, "error": f"{epic} está fechado agora"}
            entry = q.ask
            dist = max(entry * 0.01, r.min_stop_distance * 1.5)
            res = b.open_position(epic, "BUY", r.min_size, round(entry - dist, r.decimals), round(entry + dist, r.decimals))
            if not res.ok:
                return {"ok": False, "error": res.message}
            _t.sleep(2)
            seen = any(p.deal_id == res.deal_id for p in b.positions())
            closed = b.close_position(res.deal_id)
            return {"ok": seen and closed.ok, "deal_id": res.deal_id, "level": res.level, "seen": seen,
                    "closed": closed.message}
        out = await asyncio.to_thread(work)
        rt.event(f"Ordem de teste em {epic}: {'OK' if out.get('ok') else 'FALHOU'} {out}", "trade" if out.get("ok") else "error")
        return out

    # ---------------------------------------------------------------- frontend (mesmo servidor)
    app.mount("/", StaticFiles(directory=STATIC, html=True), name="static")
    return app
