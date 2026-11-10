"""Roteador de IA: modelo LOCAL (Qwen etc.) → Claude (API) → dicionário.

Modelos locais suportados (qualquer um que rode num destes servidores):
  - Ollama            LOCAL_LLM_KIND=ollama   LOCAL_LLM_URL=http://localhost:11434
  - OpenAI-compatível LOCAL_LLM_KIND=openai   LOCAL_LLM_URL=http://localhost:1234   (LM Studio, vLLM,
                      llama.cpp server, etc.)
Todos recebem um JSON Schema e devolvem JSON validado (structured output).

LLM_MODE: auto (local → Claude → dicionário), local, claude, off (só dicionário).
"""
from __future__ import annotations

import json
import logging
import os
import re
import threading
import time

import requests

from trader.sentiment import SCHEMA as SENT_SCHEMA
from trader.sentiment import SYSTEM as SENT_SYSTEM
from trader.sentiment import TOPICS, LexiconScorer, Scored

log = logging.getLogger(__name__)
THINK_RE = re.compile(r"<think>.*?</think>", re.S)


def _env(name: str, default: str = "") -> str:
    return (os.environ.get(name) or default).strip()


class LLMRouter:
    def __init__(self):
        self.mode = _env("LLM_MODE", "auto").lower()
        self.local_url = _env("LOCAL_LLM_URL").rstrip("/")
        self.local_kind = _env("LOCAL_LLM_KIND", "ollama").lower()
        self.local_model = _env("LOCAL_LLM_MODEL", "qwen3:32b")
        self.local_key = _env("LOCAL_LLM_API_KEY")
        self.local_timeout = float(_env("LOCAL_LLM_TIMEOUT", "120"))
        self.local_think = _env("LOCAL_LLM_THINK")           # "" = não envia; "0"/"1" para modelos com raciocínio
        self.claude_key = _env("ANTHROPIC_API_KEY")
        self.claude_model = _env("CLAUDE_MODEL", "claude-opus-5-5")
        self._claude = None
        self._lock = threading.Lock()
        self.stats = {"local": 0, "claude": 0, "dicionario": 0, "erros_local": 0, "erros_claude": 0}
        self.last_error = ""
        self.local_ok: bool | None = None
        self._last_health = 0.0

    # ------------------------------------------------------------ estado
    @property
    def local_configured(self) -> bool:
        return bool(self.local_url) and self.mode in ("auto", "local")

    @property
    def claude_configured(self) -> bool:
        return bool(self.claude_key) and self.mode in ("auto", "claude")

    def active(self) -> str:
        if self.local_configured and self.local_ok is not False:
            return f"local ({self.local_model})"
        if self.claude_configured:
            return f"Claude ({self.claude_model})"
        return "dicionário"

    def status(self) -> dict:
        return {"mode": self.mode, "active": self.active(), "local_url": self.local_url, "local_kind": self.local_kind,
                "local_model": self.local_model, "local_ok": self.local_ok, "claude": bool(self.claude_key),
                "claude_model": self.claude_model, "stats": self.stats, "last_error": self.last_error}

    def health(self, force: bool = False) -> bool | None:
        if not self.local_configured:
            self.local_ok = None
            return None
        if not force and time.time() - self._last_health < 60:
            return self.local_ok
        self._last_health = time.time()
        try:
            path = "/api/tags" if self.local_kind == "ollama" else "/v1/models"
            r = requests.get(self.local_url + path, headers=self._local_headers(), timeout=10)
            r.raise_for_status()
            names = [m.get("name") or m.get("model") or m.get("id") for m in (r.json().get("models") or r.json().get("data") or [])]
            self.local_ok = True
            if names and self.local_model not in names and not any(str(n).startswith(self.local_model) for n in names):
                self.last_error = f"modelo '{self.local_model}' não encontrado no servidor local (disponíveis: {', '.join(map(str, names[:8]))})"
                self.local_ok = False
        except Exception as e:  # noqa: BLE001
            self.local_ok = False
            self.last_error = f"IA local inacessível: {e.__class__.__name__}: {str(e)[:120]}"
        return self.local_ok

    def _local_headers(self) -> dict:
        h = {"Content-Type": "application/json"}
        if self.local_key:
            h["Authorization"] = f"Bearer {self.local_key}"
        return h

    # ------------------------------------------------------------ chamadas
    def json(self, system: str, user: str, schema: dict, max_tokens: int = 4000) -> tuple[dict | None, str]:
        """Devolve (objeto JSON, provedor) ou (None, 'dicionario') se nenhuma IA respondeu."""
        if self.local_configured and self.health() is not False:
            try:
                out = self._local(system, user, schema)
                self.stats["local"] += 1
                return out, "local"
            except Exception as e:  # noqa: BLE001
                self.stats["erros_local"] += 1
                self.last_error = f"IA local: {e.__class__.__name__}: {str(e)[:160]}"
                self.local_ok = False
                log.warning(self.last_error)
        if self.claude_configured:
            try:
                out = self._claude_call(system, user, schema, max_tokens)
                if out is not None:
                    self.stats["claude"] += 1
                    return out, "claude"
            except Exception as e:  # noqa: BLE001
                self.stats["erros_claude"] += 1
                self.last_error = f"Claude: {e.__class__.__name__}: {str(e)[:160]}"
                log.warning(self.last_error)
        self.stats["dicionario"] += 1
        return None, "dicionario"

    def _local(self, system: str, user: str, schema: dict) -> dict:
        msgs = [{"role": "system", "content": system}, {"role": "user", "content": user}]
        if self.local_kind == "ollama":
            body = {"model": self.local_model, "messages": msgs, "stream": False, "format": schema,
                    "options": {"temperature": 0}}
            if self.local_think in ("0", "1"):
                body["think"] = self.local_think == "1"
            r = requests.post(self.local_url + "/api/chat", json=body, headers=self._local_headers(),
                              timeout=self.local_timeout)
            r.raise_for_status()
            text = r.json()["message"]["content"]
        else:
            body = {"model": self.local_model, "messages": msgs, "temperature": 0,
                    "response_format": {"type": "json_schema",
                                        "json_schema": {"name": "resposta", "schema": schema, "strict": True}}}
            r = requests.post(self.local_url + "/v1/chat/completions", json=body, headers=self._local_headers(),
                              timeout=self.local_timeout)
            r.raise_for_status()
            text = r.json()["choices"][0]["message"]["content"]
        text = THINK_RE.sub("", text or "").strip()
        m = re.search(r"\{.*\}", text, re.S)
        return json.loads(m.group(0) if m else text)

    def _claude_call(self, system: str, user: str, schema: dict, max_tokens: int) -> dict | None:
        import anthropic
        with self._lock:
            if self._claude is None:
                self._claude = anthropic.Anthropic(api_key=self.claude_key)
        resp = self._claude.beta.messages.create(
            model=self.claude_model, max_tokens=max_tokens, system=system,
            messages=[{"role": "user", "content": user}],
            output_config={"effort": "low", "format": {"type": "json_schema", "schema": schema}},
            betas=["server-side-fallback-2026-07-01"], fallbacks="default",
        )
        if resp.stop_reason == "refusal":
            return None
        text = next((b.text for b in resp.content if b.type == "text"), "")
        return json.loads(text)


class RouterScorer:
    """Sentimento/tema/relevância das manchetes usando o roteador (local → Claude → dicionário)."""

    def __init__(self, router: LLMRouter, watchlist: list[str]):
        self.router = router
        self.watchlist = watchlist
        self.fallback = LexiconScorer()

    @property
    def name(self) -> str:
        return self.router.active()

    def score(self, items: list[dict], batch: int = 12) -> list[Scored]:
        out: list[Scored] = []
        for i in range(0, len(items), batch):
            out.extend(self._batch(items[i:i + batch]))
        return out

    def _batch(self, items: list[dict]) -> list[Scored]:
        payload = [{"id": it["id"], "ticker": it.get("ticker", ""), "headline": it["title"],
                    "summary": (it.get("summary") or "")[:300], "source": it.get("source", "")} for it in items]
        user = (f"Watchlist: {', '.join(self.watchlist)}\n\nHeadlines (JSON):\n"
                f"{json.dumps(payload, ensure_ascii=False)}\n\nScore every item.")
        data, provider = self.router.json(SENT_SYSTEM, user, SENT_SCHEMA)
        if not data:
            return self.fallback.score(items)
        by_id = {d.get("id"): d for d in data.get("items", []) if isinstance(d, dict)}
        out = []
        for it in items:
            d = by_id.get(it["id"])
            if not d:
                out.extend(self.fallback.score([it]))
                continue
            try:
                topic = d.get("topic") if d.get("topic") in TOPICS else "OTHER"
                out.append(Scored(it["id"], d.get("ticker") or it.get("ticker", ""), topic,
                                  round(max(-1.0, min(1.0, float(d["sentiment"]))), 3),
                                  round(max(0.0, min(1.0, float(d["relevance"]))), 3),
                                  "IA local" if provider == "local" else "claude"))
            except (KeyError, TypeError, ValueError):
                out.extend(self.fallback.score([it]))
        return out
