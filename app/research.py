"""Agentes de pesquisa e coletores de dados.

Coletores (fontes oficiais/públicas que permitem acesso automatizado):
  - SEC EDGAR: documentos oficiais das empresas (8-K, 10-Q, 10-K, Form 4...). A SEC exige
    um User-Agent com e-mail de contato → defina SEC_CONTACT_EMAIL.
  - StockTwits (API pública): mensagens marcadas como Bullish/Bearish por investidores.
  - Alpaca News: notícias de agências (requer chaves da Alpaca).

Agente de Pesquisa: a cada ciclo, monta um dossiê por ativo (preço, tendência, volume
consolidado, notícias, documentos, redes sociais) e pede à IA (local ou Claude) uma tese
estruturada: viés, confiança, horizonte, motivos, riscos, catalisadores. Os agentes de
trade usam o viés como filtro.
"""
from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import time
from datetime import datetime, timedelta, timezone

import requests

from trader.features import atr

log = logging.getLogger(__name__)

RESEARCH_SYSTEM = """Você é um analista de ações experiente. Recebe um dossiê com dados REAIS de um ativo
(preço, tendência, volume consolidado das bolsas, notícias com sentimento, documentos da SEC e
sentimento de redes sociais) e escreve uma tese curta e objetiva EM PORTUGUÊS.

- bias: −1 (fortemente baixista) a +1 (fortemente altista) para o horizonte indicado.
- confidence: 0 a 1. Seja conservador: dados fracos ou contraditórios → confiança baixa.
- Não invente fatos que não estão no dossiê. Cite os dados que sustentam cada motivo."""

RESEARCH_SCHEMA = {
    "type": "object",
    "properties": {
        "bias": {"type": "number"},
        "confidence": {"type": "number"},
        "horizon": {"type": "string", "enum": ["intraday", "swing", "longo"]},
        "summary": {"type": "string"},
        "drivers": {"type": "array", "items": {"type": "string"}},
        "risks": {"type": "array", "items": {"type": "string"}},
        "catalysts": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["bias", "confidence", "horizon", "summary", "drivers", "risks", "catalysts"],
    "additionalProperties": False,
}

RESEARCH_TABLE = """CREATE TABLE IF NOT EXISTS research (
  id INTEGER PRIMARY KEY AUTOINCREMENT, epic TEXT, time TEXT, provider TEXT, brief TEXT, dossier TEXT);
CREATE INDEX IF NOT EXISTS idx_research_epic ON research(epic, id);"""


# ============================================================== coletores
class SecEdgar:
    """Documentos recentes da SEC (fonte oficial)."""
    TICKERS = "https://www.sec.gov/files/company_tickers.json"
    SUBMISSIONS = "https://data.sec.gov/submissions/CIK{cik:010d}.json"

    def __init__(self, contact: str):
        self.contact = contact.strip()
        self._ciks: dict[str, int] = {}
        self._last = 0.0
        self.status = "ok" if self.contact else "defina SEC_CONTACT_EMAIL para ativar"

    @property
    def enabled(self) -> bool:
        return bool(self.contact)

    def _get(self, url: str) -> dict:
        wait = 0.15 - (time.time() - self._last)               # SEC: máx. 10 req/s
        if wait > 0:
            time.sleep(wait)
        self._last = time.time()
        r = requests.get(url, headers={"User-Agent": f"CapitalAITrader research {self.contact}"}, timeout=20)
        r.raise_for_status()
        return r.json()

    def filings(self, ticker: str, days: int = 14) -> list[dict]:
        if not self.enabled:
            return []
        if not self._ciks:
            self._ciks = {v["ticker"].upper(): int(v["cik_str"]) for v in self._get(self.TICKERS).values()}
        cik = self._ciks.get(ticker.upper())
        if not cik:
            return []
        rec = self._get(self.SUBMISSIONS.format(cik=cik)).get("filings", {}).get("recent", {})
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).date().isoformat()
        out = []
        for i, form in enumerate(rec.get("form", [])):
            date = rec["filingDate"][i]
            if date < cutoff:
                break
            acc = rec["accessionNumber"][i].replace("-", "")
            out.append({"form": form, "date": date, "items": (rec.get("items") or [""] * (i + 1))[i],
                        "description": (rec.get("primaryDocDescription") or [""] * (i + 1))[i],
                        "url": f"https://www.sec.gov/Archives/edgar/data/{cik}/{acc}/{rec['primaryDocument'][i]}"})
        return out[:25]


class StockTwits:
    URL = "https://api.stocktwits.com/api/2/streams/symbol/{sym}.json"

    def __init__(self):
        self.status = "ok"

    def stats(self, ticker: str) -> dict | None:
        r = requests.get(self.URL.format(sym=ticker), headers={"User-Agent": "CapitalAITrader/1.0"}, timeout=15)
        if r.status_code != 200:
            self.status = f"HTTP {r.status_code}"
            return None
        msgs = r.json().get("messages", [])
        bull = sum(1 for m in msgs if ((m.get("entities") or {}).get("sentiment") or {}).get("basic") == "Bullish")
        bear = sum(1 for m in msgs if ((m.get("entities") or {}).get("sentiment") or {}).get("basic") == "Bearish")
        times = sorted(m["created_at"] for m in msgs if m.get("created_at"))
        per_hour = None
        if len(times) >= 2:
            span = (datetime.fromisoformat(times[-1].replace("Z", "+00:00"))
                    - datetime.fromisoformat(times[0].replace("Z", "+00:00"))).total_seconds() / 3600
            per_hour = round(len(times) / span, 1) if span > 0 else None
        tagged = bull + bear
        self.status = "ok"
        return {"messages": len(msgs), "bullish": bull, "bearish": bear,
                "bull_ratio": round(bull / tagged, 2) if tagged else None, "msgs_per_hour": per_hour,
                "sample": [m.get("body", "")[:140] for m in msgs[:5]]}


# ============================================================== agente de pesquisa
class ResearchAgent:
    def __init__(self, ctx, router):
        self.ctx = ctx
        self.router = router
        self.sec = SecEdgar(os.environ.get("SEC_CONTACT_EMAIL", ""))
        self.twits = StockTwits()
        self.interval_min = float(os.environ.get("RESEARCH_INTERVAL_MIN", "30") or 30)
        self.briefs: dict[str, dict] = {}
        self.social: dict[str, dict] = {}
        self.status = "aguardando"
        with ctx.db.conn() as c:
            c.executescript(RESEARCH_TABLE)
        for epic in ctx.epics:                               # recarrega a última tese salva
            rows = ctx.db.rows("SELECT * FROM research WHERE epic=? ORDER BY id DESC LIMIT 1", (epic,))
            if rows:
                self.briefs[epic] = {**json.loads(rows[0]["brief"]), "time": rows[0]["time"], "provider": rows[0]["provider"]}

    def bias(self, epic: str, max_age_h: float = 24) -> float:
        b = self.briefs.get(epic)
        if not b:
            return 0.0
        try:
            age = (datetime.now(timezone.utc) - datetime.fromisoformat(b["time"])).total_seconds() / 3600
        except (KeyError, ValueError):
            return 0.0
        return float(b.get("bias", 0)) * float(b.get("confidence", 0)) if age <= max_age_h else 0.0

    async def run(self) -> None:
        await asyncio.sleep(20)                               # deixa preços e notícias chegarem primeiro
        while True:
            epics = self.ctx.epics
            per = max(10.0, self.interval_min * 60 / max(len(epics), 1))
            researched = 0
            for epic in epics:
                alp = getattr(self.ctx, "alpaca", None)
                if alp is not None and self.ctx.mode == "papel" and not alp.supports(epic):
                    self.briefs.pop(epic, None)
                    continue
                researched += 1
                try:
                    self.status = f"pesquisando {epic}"
                    await self.research(epic)
                except asyncio.CancelledError:
                    raise
                except Exception as e:  # noqa: BLE001
                    log.exception("pesquisa %s", epic)
                    self.ctx.event(f"Pesquisa {epic} falhou: {e}", "error")
                self.status = "ativo"
                await asyncio.sleep(per)
            if researched == 0:
                # nenhum ativo com dados reais (ex.: sem chaves da Alpaca): ESPERA antes de tentar de novo.
                # Sem esta pausa o laço girava sem parar e travava o servidor inteiro.
                self.status = "aguardando dados reais (configure a Alpaca)"
                await asyncio.sleep(60)

    async def dossier(self, epic: str) -> dict:
        ticker = self.ctx.ticker_for(epic)
        d: dict = {"ativo": epic, "ticker": ticker, "gerado_em": datetime.now(timezone.utc).isoformat()}
        df = await self.ctx.market.candles(epic, "DAY", 260)
        if len(df) > 30:
            c = df["close"]
            last = float(c.iloc[-1])
            d["preco"] = {
                "ultimo": round(last, 4),
                "var_1d": round(last / float(c.iloc[-2]) - 1, 4),
                "var_5d": round(last / float(c.iloc[-6]) - 1, 4),
                "var_20d": round(last / float(c.iloc[-21]) - 1, 4),
                "var_1a": round(last / float(c.iloc[0]) - 1, 4),
                "pos_52s": round((last - float(df["low"].min())) / max(float(df["high"].max() - df["low"].min()), 1e-9), 2),
                "acima_media_50": bool(last > c.tail(50).mean()),
                "acima_media_200": bool(len(c) >= 200 and last > c.tail(200).mean()),
                "volatilidade_atr_pct": round(float(atr(df).iloc[-1]) / last, 4),
            }
        vol = self.ctx.volume(epic)
        if vol:
            d["volume_consolidado"] = vol
        rows = self.ctx.db.rows("SELECT time, title, source, topic, sentiment, relevance FROM news WHERE ticker=? AND time>=? "
                                "ORDER BY relevance DESC, time DESC LIMIT 20",
                                (ticker, (datetime.now(timezone.utc) - timedelta(hours=48)).isoformat()))
        d["noticias_48h"] = rows
        if self.sec.enabled and not d["ticker"].endswith("USD"):
            try:
                d["documentos_sec_14d"] = await asyncio.to_thread(self.sec.filings, ticker)
                self.sec.status = "ok"
            except Exception as e:  # noqa: BLE001
                self.sec.status = f"erro: {e.__class__.__name__}"
        try:
            st = await asyncio.to_thread(self.twits.stats, ticker)
            if st:
                self.social[epic] = {**st, "time": datetime.now(timezone.utc).isoformat()}
                d["stocktwits"] = {k: v for k, v in st.items() if k != "sample"}
        except Exception as e:  # noqa: BLE001
            self.twits.status = f"erro: {e.__class__.__name__}"
        return d

    def heuristic(self, d: dict) -> dict:
        p = d.get("preco") or {}
        news = d.get("noticias_48h") or []
        w = sum((n.get("relevance") or 0) for n in news)
        ns = sum((n.get("relevance") or 0) * (n.get("sentiment") or 0) for n in news) / w if w else 0.0
        trend = (0.5 if p.get("acima_media_50") else -0.5) + (0.5 if p.get("acima_media_200") else -0.5) if p else 0
        st = d.get("stocktwits") or {}
        social = (st["bull_ratio"] - 0.5) * 2 if st.get("bull_ratio") is not None else 0.0
        bias = max(-1.0, min(1.0, 0.45 * ns + 0.35 * trend + 0.2 * social))
        drivers = []
        if p:
            drivers.append(f"preço {p['var_20d']:+.1%} em 20 dias, {'acima' if p.get('acima_media_200') else 'abaixo'} da média de 200")
        if news:
            drivers.append(f"sentimento médio das notícias (48h): {ns:+.2f} em {len(news)} manchetes")
        if st.get("bull_ratio") is not None:
            drivers.append(f"StockTwits: {st['bullish']} otimistas × {st['bearish']} pessimistas")
        return {"bias": round(bias, 3), "confidence": 0.3, "horizon": "swing",
                "summary": "Tese automática por regras (sem IA configurada): combina tendência, notícias e redes sociais.",
                "drivers": drivers, "risks": ["Análise sem IA: não lê o conteúdo das notícias nem dos documentos."],
                "catalysts": [f"{f['form']} em {f['date']}" for f in (d.get("documentos_sec_14d") or [])[:3]]}

    async def research(self, epic: str) -> dict:
        d = await self.dossier(epic)
        user = "Dossiê (JSON):\n" + json.dumps(d, ensure_ascii=False, default=str)[:24000]
        brief, provider = await asyncio.to_thread(self.router.json, RESEARCH_SYSTEM, user, RESEARCH_SCHEMA, 3000)
        if not brief:
            brief, provider = self.heuristic(d), "regras"
        brief["bias"] = max(-1.0, min(1.0, float(brief.get("bias", 0))))
        brief["confidence"] = max(0.0, min(1.0, float(brief.get("confidence", 0))))
        if math.isnan(brief["bias"]):
            brief["bias"] = 0.0
        now = datetime.now(timezone.utc).isoformat()
        with self.ctx.db.conn() as c:
            c.execute("INSERT INTO research (epic, time, provider, brief, dossier) VALUES (?,?,?,?,?)",
                      (epic, now, provider, json.dumps(brief, ensure_ascii=False), json.dumps(d, ensure_ascii=False, default=str)))
        self.briefs[epic] = {**brief, "time": now, "provider": provider}
        self.ctx.bus.publish("research", {"epic": epic, **self.briefs[epic], "social": self.social.get(epic),
                                          "filings": d.get("documentos_sec_14d", [])})
        return brief
