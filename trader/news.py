"""Ingestão de notícias (RSS) com remoção de duplicatas."""
from __future__ import annotations

import hashlib
import logging
import re
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

import requests

log = logging.getLogger(__name__)

YAHOO_RSS = "https://feeds.finance.yahoo.com/rss/2.0/headline?s={ticker}&region=US&lang=en-US"
UA = {"User-Agent": "Mozilla/5.0 (compatible; news-sentiment-bot/1.0)"}


def news_id(title: str) -> str:
    norm = re.sub(r"[^a-z0-9 ]", "", title.lower())
    norm = re.sub(r"\s+", " ", norm).strip()
    return hashlib.sha1(norm.encode()).hexdigest()[:16]


def _parse_time(entry) -> str:
    for key in ("published", "updated"):
        raw = entry.get(key)
        if raw:
            try:
                return parsedate_to_datetime(raw).astimezone(timezone.utc).isoformat()
            except (TypeError, ValueError):
                pass
    return datetime.now(timezone.utc).isoformat()


def fetch_rss(tickers: list[str], timeout: int = 10) -> list[dict]:
    """Busca manchetes recentes do Yahoo Finance RSS para cada ticker."""
    import feedparser

    items: dict[str, dict] = {}
    for t in tickers:
        try:
            r = requests.get(YAHOO_RSS.format(ticker=t), headers=UA, timeout=timeout)
            r.raise_for_status()
        except requests.RequestException as e:
            log.warning("RSS %s indisponível: %s", t, e)
            continue
        for e in feedparser.parse(r.content).entries:
            title = (e.get("title") or "").strip()
            if not title:
                continue
            nid = news_id(title)
            if nid in items:
                continue
            items[nid] = {"id": nid, "ticker": t, "title": title, "url": e.get("link", ""),
                          "source": "Yahoo Finance", "time": _parse_time(e)}
    return list(items.values())


def synthetic(pairs: list[tuple[str, str]]) -> list[dict]:
    now = datetime.now(timezone.utc).isoformat()
    return [{"id": news_id(f"{title} {now}"), "ticker": t, "title": f"[SIM] {title}", "url": "",
             "source": "Simulador", "time": now} for t, title in pairs]
