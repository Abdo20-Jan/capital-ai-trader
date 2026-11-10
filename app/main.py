"""API do app (FastAPI). Rode com:  python run.py app   → http://localhost:8000"""
from __future__ import annotations

import asyncio
import os
import re
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from trader.config import ROOT
from trader.sentiment import TOPIC_LABELS

from .runtime import Runtime

STATIC = Path(__file__).parent / "static"
ENV_PATH = ROOT / ".env"


def write_env(values: dict[str, str], path: Path = ENV_PATH) -> None:
    """Atualiza (ou acrescenta) chaves no .env sem mexer no resto do arquivo."""
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    done = set()
    for i, line in enumerate(lines):
        m = re.match(r"\s*([A-Z0-9_]+)\s*=", line)
        if m and m.group(1) in values:
            lines[i] = f"{m.group(1)}={values[m.group(1)]}"
            done.add(m.group(1))
    lines += [f"{k}={v}" for k, v in values.items() if k not in done]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    try:
        os.chmod(path, 0o600)                 # só o seu usuário lê (contém a chave da API)
    except OSError:
        pass
    for k, v in values.items():
        os.environ[k] = v


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


def create_app(runtime: Runtime | None = None) -> FastAPI:
    rt = runtime or Runtime()

    @asynccontextmanager
    async def lifespan(app):
        await rt.start()
        yield
        await rt.stop()

    app = FastAPI(title="Capital AI Trader", lifespan=lifespan)
    app.state.rt = rt
    app.mount("/static", StaticFiles(directory=STATIC), name="static")

    @app.get("/")
    def index():
        return FileResponse(STATIC / "index.html")

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
        news = db.rows("SELECT * FROM news ORDER BY ingested DESC LIMIT 80")
        sentiment = [{"epic": e, "ticker": rt.ticker_for(e), "score": rt.sentiment(e)} for e in rt.epics]
        return {
            "status": rt.status(),
            "account": rt.last_account,
            "agents": agents,
            "quotes": rt.market.quotes if rt.market else {},
            "positions": list(rt.orders.live.values()) if rt.orders else [],
            "news": news,
            "sentiment": sentiment,
            "topics": TOPIC_LABELS,
            "decisions": db.rows("SELECT * FROM decisions ORDER BY id DESC LIMIT 120"),
            "trades": db.rows("SELECT * FROM agent_trades WHERE broker=? AND status='CLOSED' "
                              "ORDER BY closed DESC LIMIT 100", (rt.broker_id,)),
            "equity": db.rows("SELECT time, equity, real_equity FROM (SELECT rowid, * FROM app_equity WHERE broker=? "
                              "ORDER BY rowid DESC LIMIT 720) ORDER BY rowid", (rt.broker_id,)),
            "events": db.rows("SELECT * FROM events ORDER BY id DESC LIMIT 80"),
            "model": (db.rows("SELECT report FROM models ORDER BY time DESC LIMIT 1") or [{}])[0].get("report"),
        }

    @app.get("/api/candles")
    async def candles(epic: str, res: str = "MINUTE_15", count: int = 150):
        if epic not in rt.epics:
            raise HTTPException(404, "ativo fora da lista")
        df = await rt.market.candles(epic, res, min(count, 500))
        return [{"t": str(r.time), "o": r.open, "h": r.high, "l": r.low, "c": r.close} for r in df.itertuples()]

    # ---------------------------------------------------------------- controles
    @app.post("/api/agents/{key}")
    def patch_agent(key: str, patch: AgentPatch):
        a = next((x for x in rt.agents if x.cfg.key == key), None)
        if not a:
            raise HTTPException(404, "agente não encontrado")
        a.update({k: v for k, v in patch.model_dump().items() if v is not None})
        rt.save_agent_configs()
        rt.event(f"{a.cfg.name}: configuração alterada ({', '.join(k for k, v in patch.model_dump().items() if v is not None)})")
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
        ok = await rt.orders.close(deal_id, "fechada manualmente no painel")
        return {"ok": ok}

    @app.post("/api/close-all")
    async def close_all():
        ids = list(rt.orders.live)
        results = [await rt.orders.close(d, "fechar todas (painel)") for d in ids]
        return {"closed": sum(results), "total": len(ids)}

    @app.post("/api/risk")
    async def risk(patch: RiskPatch):
        vals = {}
        if patch.start_balance is not None:
            vals["START_BALANCE"] = str(patch.start_balance)
        if patch.target_balance is not None:
            vals["TARGET_BALANCE"] = str(patch.target_balance)
        if patch.max_daily_loss is not None:
            vals["MAX_DAILY_LOSS"] = str(patch.max_daily_loss)
        if patch.stop_at_target is not None:
            vals["STOP_AT_TARGET"] = "1" if patch.stop_at_target else "0"
        if vals:
            write_env(vals)
            s = rt.settings
            s.start_balance = float(os.environ.get("START_BALANCE", s.start_balance))
            s.target_balance = float(os.environ.get("TARGET_BALANCE", s.target_balance))
            s.max_daily_loss = float(os.environ.get("MAX_DAILY_LOSS", s.max_daily_loss))
            s.stop_at_target = os.environ.get("STOP_AT_TARGET", "1") not in ("0", "false")
            rt.event("Regras de risco atualizadas: " + ", ".join(f"{k}={v}" for k, v in vals.items()), "warn")
        return rt.status()

    @app.post("/api/bankroll/reset")
    def reset_bankroll():
        rt.orders.reset_bankroll()
        rt.event(f"Banca virtual reiniciada em ${rt.settings.start_balance:.2f}", "info")
        return {"ok": True}

    @app.post("/api/model/retrain")
    async def retrain():
        asyncio.create_task(rt._train_model(force=True))
        return {"ok": True}

    # ---------------------------------------------------------------- conexão Capital.com
    @app.get("/api/connection")
    def connection():
        s = rt.settings
        key = s.capital_api_key
        return {"mode": rt.mode, "broker_id": rt.broker_id, "error": rt.connection_error,
                "configured": bool(key and s.capital_identifier and s.capital_password),
                "api_key_hint": (key[:3] + "…" + key[-3:]) if len(key) > 8 else ("definida" if key else ""),
                "identifier": s.capital_identifier, "account_id": s.capital_account_id,
                "anthropic": bool(s.anthropic_api_key), "stream": rt.market.stream_status if rt.market else ""}

    def _client(c: Creds):
        from trader.capital import CapitalClient
        s = rt.settings
        return CapitalClient(c.api_key or s.capital_api_key, c.identifier or s.capital_identifier,
                             c.password or s.capital_password, account_id=c.account_id or s.capital_account_id)

    @app.post("/api/connection/test")
    async def test_connection(c: Creds):
        def work():
            cl = _client(c)
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

    @app.post("/api/connection/save")
    async def save_connection(c: Creds):
        vals = {"BROKER": "capital_demo"}
        if c.api_key:
            vals["CAPITAL_API_KEY"] = c.api_key.strip()
        if c.identifier:
            vals["CAPITAL_IDENTIFIER"] = c.identifier.strip()
        if c.password:
            vals["CAPITAL_PASSWORD"] = c.password
        vals["CAPITAL_ACCOUNT_ID"] = c.account_id.strip()
        write_env(vals)
        await rt.restart()
        return {"mode": rt.mode, "error": rt.connection_error}

    @app.post("/api/connection/simulator")
    async def use_simulator():
        write_env({"BROKER": "paper"})
        await rt.restart()
        return {"mode": rt.mode}

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

    return app
