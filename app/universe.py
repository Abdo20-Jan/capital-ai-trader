"""Agente Scanner: escolhe, entre TODOS os mercados da Capital.com, os que os agentes vão operar.

- Baixa o catálogo inteiro (GET /api/v1/markets, uma chamada) a cada poucos minutos.
- Exclui criptomoedas, mercados fechados e mercados só de visualização.
- Ordena pelo movimento do dia (|variação %|) dentro de cada classe (ações, commodities,
  índices, moedas...) e monta uma seleção diversificada de até ACTIVE_MAX ativos
  (a Capital.com aceita até 40 no canal de preços ao vivo).
- Ativos com posição aberta e os "favoritos" (WATCHLIST) entram sempre.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time

from trader.config import WatchItem

log = logging.getLogger(__name__)

EXCLUDED_TYPES = {"CRYPTOCURRENCIES", "CRYPTOCURRENCY", "CRYPTO"}
# participação de cada classe na seleção (o que sobrar vai para as maiores variações de qualquer classe)
SHARE_OF = {"SHARES": 0.45, "COMMODITIES": 0.2, "INDICES": 0.15, "CURRENCIES": 0.2}


def _pct(m: dict) -> float:
    try:
        return abs(float(m.get("percentageChange") or 0))
    except (TypeError, ValueError):
        return 0.0


def eligible(m: dict) -> bool:
    t = str(m.get("instrumentType", "")).upper()
    if t in EXCLUDED_TYPES:
        return False
    if m.get("marketStatus") != "TRADEABLE":
        return False
    if "VIEW_ONLY" in (m.get("marketModes") or []):
        return False
    return bool(m.get("epic"))


def select(markets: list[dict], max_n: int, must: list[str]) -> list[dict]:
    """Seleção diversificada: cotas por classe + maiores variações; `must` sempre incluídos."""
    by_epic = {m["epic"]: m for m in markets if m.get("epic")}
    pool = sorted((m for m in markets if eligible(m)), key=_pct, reverse=True)
    chosen: list[dict] = [by_epic[e] for e in must if e in by_epic]
    seen = {m["epic"] for m in chosen}
    room = max(0, max_n - len(chosen))
    for t, share in SHARE_OF.items():
        quota = int(room * share)
        for m in (x for x in pool if str(x.get("instrumentType", "")).upper() == t and x["epic"] not in seen):
            if quota <= 0:
                break
            chosen.append(m)
            seen.add(m["epic"])
            quota -= 1
    for m in pool:                                            # completa com as maiores variações
        if len(chosen) >= max_n:
            break
        if m["epic"] not in seen:
            chosen.append(m)
            seen.add(m["epic"])
    return chosen[:max(max_n, len([e for e in must if e in by_epic]))]


class UniverseScanner:
    def __init__(self, rt):
        self.rt = rt
        self.max_n = int(os.environ.get("ACTIVE_MAX", "30") or 30)
        self.every = float(os.environ.get("UNIVERSE_REFRESH_S", "300") or 300)
        self.catalog: list[dict] = []
        self.selected: list[dict] = []
        self.status = "aguardando"
        self.last = 0.0

    def info(self) -> dict:
        types: dict[str, int] = {}
        for m in self.catalog:
            if eligible(m):
                t = str(m.get("instrumentType", "?"))
                types[t] = types.get(t, 0) + 1
        return {"status": self.status, "catalog": len(self.catalog), "eligible_by_type": types, "max": self.max_n,
                "selected": [{"epic": m["epic"], "name": m.get("instrumentName"), "type": m.get("instrumentType"),
                              "pct": m.get("percentageChange")} for m in self.selected],
                "updated": self.last}

    async def run(self) -> None:
        while True:
            try:
                await self.scan()
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                self.status = f"erro: {e}"
                log.warning("scanner: %s", e)
            await asyncio.sleep(self.every)

    async def scan(self) -> None:
        self.status = "lendo o catálogo da Capital.com…"
        self.catalog = await asyncio.to_thread(self.rt.broker.all_markets)
        favorites = [e.strip() for e in os.environ.get("WATCHLIST", "").split(",") if e.strip()]
        held = [p["epic"] for p in self.rt.orders.live.values()] if self.rt.orders else []
        must = list(dict.fromkeys(held + favorites))
        self.selected = select(self.catalog, self.max_n, must)
        self.last = time.time()
        n_ok = sum(1 for m in self.catalog if eligible(m))
        self.status = f"{len(self.selected)} ativos escolhidos entre {n_ok} mercados abertos (sem cripto)"
        await self.rt.set_universe([WatchItem(m["epic"], m["epic"]) for m in self.selected],
                                   {m["epic"]: {"name": m.get("instrumentName", m["epic"]),
                                                "type": m.get("instrumentType", "")} for m in self.selected})
