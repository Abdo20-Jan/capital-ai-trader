"""Análise de sentimento e classificação de tema das manchetes.

- ClaudeScorer: usa a API do Claude (structured outputs → JSON garantido).
- LexiconScorer: dicionário financeiro simples, gratuito e offline (fallback).
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass

log = logging.getLogger(__name__)

TOPICS = ["EARN", "CAPEX", "CLOUD", "SUPPLY", "REG", "LEGAL", "PROD", "MACRO", "ANALYST", "OTHER"]
TOPIC_LABELS = {
    "EARN": "Resultados e guidance", "CAPEX": "Investimentos (IA, data centers)", "CLOUD": "Cloud e demanda corporativa",
    "SUPPLY": "Chips e cadeia de suprimentos", "REG": "Regulação e antitruste", "LEGAL": "Processos e privacidade",
    "PROD": "Produtos e lançamentos", "MACRO": "Macro (juros, inflação)", "ANALYST": "Analistas (upgrade/downgrade)",
    "OTHER": "Outros",
}


@dataclass
class Scored:
    id: str
    ticker: str
    topic: str
    sentiment: float     # -1 (muito negativo) .. +1 (muito positivo)
    relevance: float     # 0..1 — quanto a notícia deve mexer com o preço da ação
    scorer: str


# --------------------------------------------------------------------- dicionário
POS = {"beat", "beats", "record", "raise", "raises", "raised", "upgrade", "upgrades", "upgraded", "strong",
       "growth", "surge", "surges", "soar", "soars", "jump", "jumps", "gain", "gains", "rally", "outperform",
       "buyback", "profit", "profits", "approval", "approved", "expands", "exceeds", "tops", "bullish", "win",
       "wins", "partnership", "accelerates", "rebound", "solid", "higher", "boost", "boosts"}
NEG = {"miss", "misses", "missed", "cut", "cuts", "downgrade", "downgrades", "downgraded", "weak", "fall",
       "falls", "drop", "drops", "plunge", "plunges", "slump", "lawsuit", "sues", "sued", "probe", "antitrust",
       "fine", "fined", "delay", "delays", "delayed", "recall", "layoffs", "warning", "warns", "decline",
       "declines", "loss", "losses", "bearish", "investigation", "ban", "tariff", "tariffs", "lower", "slows",
       "slowdown", "problems", "shortage", "halt", "halts"}
TOPIC_WORDS = {
    "EARN": {"earnings", "revenue", "guidance", "outlook", "quarter", "eps", "estimates", "profit", "results"},
    "CAPEX": {"capex", "spending", "data", "centre", "center", "investment", "invest", "compute", "buyback"},
    "CLOUD": {"cloud", "azure", "aws", "enterprise", "subscription", "retention"},
    "SUPPLY": {"supply", "chip", "chips", "semiconductor", "shortage", "foundry", "capacity", "tsmc"},
    "REG": {"antitrust", "regulator", "regulators", "ftc", "doj", "eu", "commission", "ban", "tariff", "tariffs"},
    "LEGAL": {"lawsuit", "sues", "sued", "court", "privacy", "settlement", "fined", "fine", "probe"},
    "PROD": {"launch", "product", "iphone", "model", "release", "unveils", "announces", "device", "ai"},
    "MACRO": {"fed", "rates", "inflation", "cpi", "jobs", "recession", "treasury", "yields"},
    "ANALYST": {"analyst", "analysts", "upgrade", "downgrade", "target", "rating", "overweight", "underweight"},
}
NEGATORS = {"not", "no", "never", "without", "despite"}


class LexiconScorer:
    name = "dicionario"

    def score(self, items: list[dict]) -> list[Scored]:
        out = []
        for it in items:
            words = re.findall(r"[a-z']+", it["title"].lower())
            s = 0.0
            for i, w in enumerate(words):
                v = 1.0 if w in POS else -1.0 if w in NEG else 0.0
                if v and i > 0 and words[i - 1] in NEGATORS:
                    v = -v
                s += v
            sentiment = max(-1.0, min(1.0, s / 2.0))
            counts = {t: len(set(words) & kw) for t, kw in TOPIC_WORDS.items()}
            topic = max(counts, key=counts.get) if max(counts.values()) > 0 else "OTHER"
            relevance = 0.4 + 0.15 * min(abs(s), 4) if it.get("ticker") else 0.2
            out.append(Scored(it["id"], it.get("ticker", ""), topic, round(sentiment, 3),
                              round(min(relevance, 1.0), 3), self.name))
        return out


# --------------------------------------------------------------------- Claude
SYSTEM = """You are a sell-side equity news analyst. For each headline, judge the likely SHORT-TERM
(next few hours) impact on the named stock's price.

- sentiment: -1.0 (clearly bad for the stock) to +1.0 (clearly good). 0 if neutral or already priced in.
- relevance: 0.0 (noise, opinion piece, listicle, unrelated) to 1.0 (material, company-specific, new information).
- topic: the single best category.
- ticker: the stock most affected, from the provided watchlist; use the item's given ticker if unsure.

Be conservative: generic market commentary and "should you buy X" articles get relevance <= 0.2."""

SCHEMA = {
    "type": "object",
    "properties": {
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "ticker": {"type": "string"},
                    "topic": {"type": "string", "enum": TOPICS},
                    "sentiment": {"type": "number"},
                    "relevance": {"type": "number"},
                },
                "required": ["id", "ticker", "topic", "sentiment", "relevance"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["items"],
    "additionalProperties": False,
}


class ClaudeScorer:
    name = "claude"

    def __init__(self, api_key: str, model: str = "claude-opus-5-5", watchlist: list[str] | None = None):
        import anthropic  # importação tardia: só exige o pacote quando a chave está configurada

        self.anthropic = anthropic
        self.client = anthropic.Anthropic(api_key=api_key)
        self.model = model
        self.watchlist = watchlist or []
        self.fallback = LexiconScorer()

    def score(self, items: list[dict], batch: int = 25) -> list[Scored]:
        out: list[Scored] = []
        for i in range(0, len(items), batch):
            out.extend(self._score_batch(items[i:i + batch]))
        return out

    def _score_batch(self, items: list[dict]) -> list[Scored]:
        payload = [{"id": it["id"], "ticker": it.get("ticker", ""), "headline": it["title"],
                    "source": it.get("source", "")} for it in items]
        prompt = (f"Watchlist: {', '.join(self.watchlist)}\n\nHeadlines (JSON):\n"
                  f"{json.dumps(payload, ensure_ascii=False)}\n\nScore every item.")
        try:
            resp = self.client.beta.messages.create(
                model=self.model,
                max_tokens=8000,
                system=SYSTEM,
                messages=[{"role": "user", "content": prompt}],
                output_config={"effort": "low", "format": {"type": "json_schema", "schema": SCHEMA}},
                betas=["server-side-fallback-2026-07-01"],
                fallbacks="default",
            )
            if resp.stop_reason == "refusal":
                log.warning("Claude recusou o lote; usando dicionário")
                return self.fallback.score(items)
            text = next((b.text for b in resp.content if b.type == "text"), "")
            data = json.loads(text)
        except (self.anthropic.APIError, json.JSONDecodeError, StopIteration) as e:
            log.warning("Falha no Claude (%s); usando dicionário", e)
            return self.fallback.score(items)

        by_id = {d["id"]: d for d in data.get("items", [])}
        out = []
        for it in items:
            d = by_id.get(it["id"])
            if d is None:
                out.extend(self.fallback.score([it]))
                continue
            topic = d["topic"] if d["topic"] in TOPICS else "OTHER"
            out.append(Scored(it["id"], d.get("ticker") or it.get("ticker", ""), topic,
                              round(max(-1.0, min(1.0, float(d["sentiment"]))), 3),
                              round(max(0.0, min(1.0, float(d["relevance"]))), 3), self.name))
        return out


def make_scorer(api_key: str, model: str, watchlist: list[str]):
    if api_key:
        try:
            return ClaudeScorer(api_key, model, watchlist)
        except ImportError:
            log.warning("Pacote 'anthropic' não instalado; usando dicionário")
    return LexiconScorer()
