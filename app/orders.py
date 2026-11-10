"""Gestor de risco + execução de ordens.

Todo pedido de um agente passa por aqui. A ordem só vai para a corretora se
passar por TODAS as travas abaixo; cada decisão (aceita ou recusada, com o
motivo) é gravada e transmitida ao painel em tempo real.
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections import deque

from trader.broker import Account
from trader.risk import position_size

log = logging.getLogger(__name__)

MAX_ORDERS_PER_HOUR = 800          # a demo da Capital.com permite 1000/h


class OrderManager:
    def __init__(self, ctx):
        self.ctx = ctx
        self.kill_switch = False
        self.live: dict[str, dict] = {}            # deal_id -> posição atual (corretora + dono)
        self.meta: dict[str, dict] = {}            # deal_id -> {agent, opened_at (relógio da corretora)}
        self._order_times: deque = deque()
        self._rules: dict[str, object] = {}
        self._lock = asyncio.Lock()
        self.day = ""
        self.day_start_equity: float | None = None
        self.guard_msg = "ok"

    # ------------------------------------------------------------ banca virtual
    def bot_account(self, real: Account) -> Account:
        s = self.ctx.settings
        key = f"baseline:{self.ctx.broker_id}"
        base = self.ctx.db.get(key)
        if not base or base.get("start") != s.start_balance:
            base = {"equity": real.equity, "balance": real.balance, "start": s.start_balance}
            self.ctx.db.set(key, base)
        equity = s.start_balance + (real.equity - base["equity"])
        balance = s.start_balance + (real.balance - base["balance"])
        available = min(real.available, equity - (real.equity - real.available))
        return Account(balance=balance, equity=equity, available=available, currency=real.currency)

    def reset_bankroll(self) -> None:
        with self.ctx.db.conn() as c:
            c.execute("DELETE FROM kv WHERE key=?", (f"baseline:{self.ctx.broker_id}",))

    # ------------------------------------------------------------ travas
    def guard(self, acct: Account) -> tuple[bool, str]:
        s = self.ctx.settings
        today = self.ctx.now().date().isoformat()
        if today != self.day or self.day_start_equity is None:
            self.day, self.day_start_equity = today, acct.equity
        if self.kill_switch:
            return False, "PAUSA GERAL ligada no painel"
        if s.stop_at_target and acct.equity >= s.target_balance:
            return False, f"META ATINGIDA (${acct.equity:.2f} ≥ ${s.target_balance:.2f})"
        dd = (self.day_start_equity - acct.equity) / self.day_start_equity if self.day_start_equity else 0
        if s.max_daily_loss > 0 and dd >= s.max_daily_loss:
            return False, f"perda do dia {dd:.1%} ≥ limite {s.max_daily_loss:.0%}"
        if acct.equity <= s.start_balance * 0.5:
            return False, f"banca caiu 50% (${acct.equity:.2f}) — revise antes de continuar"
        return True, "ok"

    def _pre_check(self, agent, epic: str, direction: str) -> tuple[bool, str]:
        mine = [p for p in self.live.values() if p["agent"] == agent.cfg.key]
        if any(p["epic"] == epic for p in mine):
            return False, "o agente já tem posição neste ativo"
        if len(mine) >= agent.cfg.max_positions:
            return False, f"agente no limite de {agent.cfg.max_positions} posições"
        if len(self.live) >= self.ctx.max_total_positions:
            return False, f"limite global de {self.ctx.max_total_positions} posições"
        opposite = [p for p in self.live.values() if p["epic"] == epic and p["direction"] != direction]
        if opposite:
            return False, f"outro agente ({opposite[0]['agent']}) está na direção oposta neste ativo"
        now = time.time()
        while self._order_times and now - self._order_times[0] > 3600:
            self._order_times.popleft()
        if len(self._order_times) >= MAX_ORDERS_PER_HOUR:
            return False, "limite de ordens por hora da API"
        return True, "ok"

    def performance_multiplier(self, agent_key: str) -> tuple[float, str]:
        """Auto-avaliação: agentes com bom histórico recente arriscam um pouco mais, ruins menos."""
        st = self.ctx.db.agent_stats(agent_key, self.ctx.broker_id, last=20)
        if st["recent_n"] < 10 or st["expectancy_r"] is None:
            return 1.0, ""
        m = max(0.5, min(1.5, 1 + st["expectancy_r"]))
        return m, f" ×{m:.2f} pelo desempenho ({st['expectancy_r']:+.2f}R)"

    # ------------------------------------------------------------ envio
    async def submit(self, agent, epic: str, sig) -> None:
        async with self._lock:
            await self._submit(agent, epic, sig)

    def _decide(self, agent, epic, sig, ok: bool, reason: str) -> None:
        d = self.ctx.db.decision(agent.cfg.key, epic, "abrir", sig.direction, ok, reason)
        d["signal"] = sig.reason
        self.ctx.bus.publish("decision", d)

    async def _submit(self, agent, epic: str, sig) -> None:
        b = self.ctx.broker
        real = await asyncio.to_thread(b.account)
        acct = self.bot_account(real)
        ok, why = self.guard(acct)
        if ok:
            ok, why = self._pre_check(agent, epic, sig.direction)
        if not ok:
            return self._decide(agent, epic, sig, False, why)

        q = self.ctx.market.quotes.get(epic)
        if not q:
            qq = await asyncio.to_thread(b.quote, epic)
            if not qq.tradeable:
                return self._decide(agent, epic, sig, False, "mercado fechado")
            q = {"bid": qq.bid, "ask": qq.ask}
        spread = q["ask"] - q["bid"]
        if spread > 0.5 * sig.stop_dist:
            return self._decide(agent, epic, sig, False, f"spread {spread:.4g} grande demais para o stop")
        if epic not in self._rules:
            self._rules[epic] = await asyncio.to_thread(b.rules, epic)
        rules = self._rules[epic]
        entry = q["ask"] if sig.direction == "BUY" else q["bid"]
        stop_dist = max(sig.stop_dist, rules.min_stop_distance * 1.1)
        tp_dist = max(sig.tp_dist, rules.min_stop_distance * 1.1)

        mult, mult_note = self.performance_multiplier(agent.cfg.key)
        risk = agent.cfg.risk_per_trade * mult
        agent_margin = sum(p["size"] * p["level"] * self._rules.get(p["epic"], rules).margin_factor
                           for p in self.live.values() if p["agent"] == agent.cfg.key)
        exposure = sum(p["size"] * p["level"] for p in self.live.values())
        sz = position_size(acct, rules, entry, stop_dist, risk, max(risk * 2, 0.02), agent.cfg.margin_share,
                           agent_margin, self.ctx.settings.max_exposure, exposure)
        if not sz.ok:
            return self._decide(agent, epic, sig, False, sz.reason)

        sign = 1 if sig.direction == "BUY" else -1
        stop = round(entry - sign * stop_dist, rules.decimals)
        target = round(entry + sign * tp_dist, rules.decimals)
        res = await asyncio.to_thread(b.open_position, epic, sig.direction, sz.size, stop, target)
        self._order_times.append(time.time())
        if not res.ok:
            self.ctx.event(f"{agent.cfg.name}: ordem {sig.direction} {epic} recusada pela corretora: {res.message}", "error")
            return self._decide(agent, epic, sig, False, f"corretora recusou: {res.message}")

        level = res.level or entry
        self.ctx.db.record_open(deal_id=res.deal_id, agent=agent.cfg.key, epic=epic, direction=sig.direction,
                                size=sz.size, entry=level, stop=stop, target=target, risk=round(sz.risk_amount, 4),
                                reason=sig.reason, broker=self.ctx.broker_id)
        from datetime import datetime, timezone
        self.meta[res.deal_id] = {"agent": agent.cfg.key, "opened_sim": self.ctx.now().isoformat(),
                                  "opened_at": datetime.now(timezone.utc).isoformat()}
        self.live[res.deal_id] = {"deal_id": res.deal_id, "agent": agent.cfg.key, "epic": epic,
                                  "direction": sig.direction, "size": sz.size, "level": level, "upl": 0.0,
                                  "stop": stop, "target": target, "risk": sz.risk_amount,
                                  "opened_at": self.meta[res.deal_id]["opened_at"],
                                  "opened_sim": self.meta[res.deal_id]["opened_sim"], "reason": sig.reason}
        self._decide(agent, epic, sig, True,
                     f"{'COMPRA' if sig.direction == 'BUY' else 'VENDA'} {sz.size:g} @ {level:.{rules.decimals}f} · "
                     f"stop {stop} · alvo {target} · risco ${sz.risk_amount:.2f}{mult_note}")
        self.ctx.bus.publish("order", {**self.live[res.deal_id], "agent_name": agent.cfg.name})
        self.ctx.event(f"{agent.cfg.name}: {'COMPRA' if sig.direction == 'BUY' else 'VENDA'} {epic} "
                       f"{sz.size:g} @ {level:.{rules.decimals}f} — {sig.reason}", "trade")

    async def close(self, deal_id: str, reason: str) -> bool:
        res = await asyncio.to_thread(self.ctx.broker.close_position, deal_id)
        p = self.live.get(deal_id, {})
        if res.ok:
            pnl = p.get("upl")
            for rec in reversed(getattr(self.ctx.broker, "closed", [])):
                if rec["deal_id"] == deal_id:
                    pnl = rec["pnl"]
                    break
            self._finish(deal_id, pnl, res.level, reason)
        else:
            self.ctx.event(f"Falha ao fechar {deal_id}: {res.message}", "error")
        return res.ok

    def close_all_requested(self) -> list[str]:
        return list(self.live)

    # ------------------------------------------------------------ sincronização
    def _finish(self, deal_id: str, pnl, exit_price, reason: str) -> None:
        p = self.live.pop(deal_id, None)
        t = self.ctx.db.record_close(deal_id, pnl, exit_price, reason)
        self.meta.pop(deal_id, None)
        if t is None and p is None:
            return
        info = {**(t or {}), **({"agent": p["agent"]} if p else {}), "pnl": pnl, "reason_close": reason}
        self.ctx.bus.publish("closed", info)
        res = f"{pnl:+.2f}" if pnl is not None else "?"
        self.ctx.event(f"{self.ctx.agent_name(info.get('agent'))}: fechou {info.get('epic')} {res} ({reason})",
                       "win" if (pnl or 0) > 0 else "loss")
        self.ctx.on_trade_closed(info.get("agent"))

    def on_sim_closed(self, records: list[dict]) -> None:
        for r in records:
            if r["deal_id"] in self.live:
                self._finish(r["deal_id"], r["pnl"], r["price"], r["reason"])

    async def sync(self) -> None:
        positions = await asyncio.to_thread(self.ctx.broker.positions)
        known = self.ctx.db.open_trades(self.ctx.broker_id)
        seen = set()
        for p in positions:
            seen.add(p.deal_id)
            t = known.get(p.deal_id)
            cur = self.live.get(p.deal_id)
            if cur is None:
                cur = {"deal_id": p.deal_id, "agent": t["agent"] if t else "manual", "epic": p.epic,
                       "direction": p.direction, "size": p.size, "level": p.level,
                       "stop": t["stop"] if t else p.stop_level, "target": t["target"] if t else p.profit_level,
                       "risk": t["risk"] if t else None,
                       "opened_at": self.meta.get(p.deal_id, {}).get("opened_at") or (t["opened"] if t else None),
                       "opened_sim": self.meta.get(p.deal_id, {}).get("opened_sim") or (t["opened"] if t else None),
                       "reason": t["reason_open"] if t else "aberta fora do app"}
                self.live[p.deal_id] = cur
            cur["upl"] = p.upl
        for deal_id in list(self.live):
            if deal_id not in seen:                        # a corretora fechou (stop/alvo)
                p = self.live[deal_id]
                self._finish(deal_id, p.get("upl"), None, "stop/alvo na corretora")
        for deal_id in known:
            if deal_id not in seen and deal_id not in self.live:
                self.ctx.db.record_close(deal_id, None, None, "fechada enquanto o app estava desligado")
        self.ctx.bus.publish("positions", list(self.live.values()))

    def positions_of(self, agent_key: str) -> list[dict]:
        return [p for p in self.live.values() if p["agent"] == agent_key]
