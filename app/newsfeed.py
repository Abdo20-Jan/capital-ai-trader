"""Fluxo contínuo de notícias: várias fontes RSS consultadas em rodízio (uma a cada
poucos segundos), deduplicação, análise por IA e transmissão imediata ao painel e
ao agente de notícias."""
from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime, timedelta, timezone

import requests

from trader.engine import aggregate_sentiment
from trader.news import UA, YAHOO_RSS, _parse_time, news_id

log = logging.getLogger(__name__)

GENERAL_FEEDS = [
    ("MarketWatch", "https://feeds.content.dowjones.io/public/rss/mw_topstories"),
    ("MarketWatch Pulse", "https://feeds.content.dowjones.io/public/rss/mw_marketpulse"),
    ("Investing.com", "https://www.investing.com/rss/news_25.rss"),
    ("Seeking Alpha", "https://seekingalpha.com/market_currents.xml"),
]
GOOGLE_NEWS = "https://news.google.com/rss/search?q={q}+when:1d&hl=en-US&gl=US&ceid=US:en"
CRYPTO_NEWS = {"BTCUSD": ("BTC-USD", "bitcoin"), "ETHUSD": ("ETH-USD", "ethereum"), "SOLUSD": ("SOL-USD", "solana"),
               "XRPUSD": ("XRP-USD", "xrp+crypto"), "LTCUSD": ("LTC-USD", "litecoin")}

# palavras que ligam uma manchete genérica a um ticker
ALIASES = {
    "AAPL": ["apple", "iphone", "tim cook"], "MSFT": ["microsoft", "azure", "satya nadella"],
    "NVDA": ["nvidia", "jensen huang"], "GOOGL": ["google", "alphabet", "youtube", "waymo"],
    "AMZN": ["amazon", "aws", "andy jassy"], "META": ["meta platforms", "facebook", "instagram", "zuckerberg"],
    "TSLA": ["tesla", "elon musk"], "AVGO": ["broadcom"], "GOLD": ["gold price", "gold prices", "bullion"],
    "OIL_CRUDE": ["oil price", "crude", "opec", "brent", "wti"], "EURUSD": ["euro", "ecb", "lagarde"],
    "BTCUSD": ["bitcoin", "btc"], "ETHUSD": ["ethereum", "ether "], "US500": ["s&p 500", "wall street", "stocks close", "stock market"],
    "US100": ["nasdaq"],
}


def detect_ticker(title: str, tickers: list[str]) -> str:
    t = title.lower()
    for tk in tickers:
        if re.search(rf"\b{re.escape(tk.lower())}\b", t):
            return tk
        for alias in ALIASES.get(tk, []):
            if alias in t:
                return tk
    return ""


class NewsFeed:
    def __init__(self, ctx, scorer):
        self.ctx = ctx
        self.scorer = scorer
        self.sources_status: dict[str, str] = {}
        self._rot = 0

    def _sources(self) -> list[tuple[str, str, str]]:
        """(nome, url, ticker fixo ou '' para detectar pelo título)."""
        out = []
        names = getattr(self.ctx, "names", {}) or {}
        for w in self.ctx.settings.watchlist[:24]:          # fontes por ativo: os 24 primeiros da seleção
            info = names.get(w.epic) or {}
            if info.get("type") and info["type"] != "SHARES":
                q = requests.utils.quote(str(info.get("name") or w.ticker))
                out.append((f"Google News {w.ticker}", GOOGLE_NEWS.format(q=q), w.ticker))
                continue
            ysym, query = CRYPTO_NEWS.get(w.ticker.upper(), (w.ticker, f"{w.ticker}+stock"))
            out.append((f"Yahoo {w.ticker}", YAHOO_RSS.format(ticker=ysym), w.ticker))
            out.append((f"Google News {w.ticker}", GOOGLE_NEWS.format(q=query), w.ticker))
        out += [(n, u, "") for n, u in GENERAL_FEEDS]
        alp = getattr(self.ctx, "alpaca", None)
        if alp is not None and alp.has_keys:            # agências de notícias via Alpaca, 3× por rodízio
            for i in range(3):
                out.insert(i * max(1, len(out) // 3), ("Alpaca News", "alpaca", ""))
        return out

    def _fetch_alpaca(self) -> list[dict]:
        tickers = [w.ticker for w in self.ctx.settings.watchlist]
        items = []
        for n in self.ctx.alpaca.news(tickers, limit=50):
            title = (n.get("headline") or "").strip()
            if not title:
                continue
            syms = [s for s in (n.get("symbols") or []) if s in tickers]
            items.append({"id": news_id(title), "ticker": syms[0] if syms else detect_ticker(title, tickers),
                          "title": title, "summary": n.get("summary") or "", "url": n.get("url") or "",
                          "source": f"Alpaca/{n.get('source', '')}",
                          "time": (n.get("created_at") or "").replace("Z", "+00:00")})
        return items

    def _fetch(self, name: str, url: str, fixed: str) -> list[dict]:
        if url == "alpaca":
            return self._fetch_alpaca()
        import feedparser
        r = requests.get(url, headers=UA, timeout=10)
        r.raise_for_status()
        tickers = [w.ticker for w in self.ctx.settings.watchlist]
        items = []
        for e in feedparser.parse(r.content).entries[:30]:
            title = (e.get("title") or "").strip()
            if not title:
                continue
            items.append({"id": news_id(title), "ticker": fixed or detect_ticker(title, tickers), "title": title,
                          "url": e.get("link", ""), "source": name, "time": _parse_time(e)})
        return items

    async def run(self, interval_s: float = 6.0) -> None:
        """Consulta UMA fonte a cada `interval_s` segundos, em rodízio → fluxo constante."""
        while True:
            srcs = self._sources()
            name, url, fixed = srcs[self._rot % len(srcs)]
            self._rot += 1
            try:
                items = await asyncio.to_thread(self._fetch, name, url, fixed)
                self.sources_status[name] = f"ok · {len(items)} itens"
                await self.ingest(items)
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                self.sources_status[name] = f"erro: {e.__class__.__name__}"
            await asyncio.sleep(interval_s)

    async def run_sim(self) -> None:
        while True:
            pairs = self.ctx.broker.pop_news()
            if pairs:
                now = self.ctx.now().isoformat()
                items = [{"id": news_id(f"{t} {now} {i}"), "ticker": tk, "title": t, "url": "",
                          "source": "Simulador", "time": now} for i, (tk, t, _) in enumerate(pairs)]
                await self.ingest(items)
            self.sources_status["Simulador"] = "ok · manchetes sintéticas"
            await asyncio.sleep(1)

    async def ingest(self, items: list[dict]) -> None:
        cutoff = (self.ctx.now() - timedelta(days=2)).isoformat()
        items = [i for i in items if i["time"] >= cutoff]
        known = self.ctx.db.known_news_ids([i["id"] for i in items])
        new = [i for i in items if i["id"] not in known]
        if not new:
            return
        by_id = {i["id"]: i for i in new}
        scored = await asyncio.to_thread(self.scorer.score, new)
        for sc in sorted(scored, key=lambda s: by_id[s.id]["time"]):
            item = by_id[sc.id]
            self.ctx.db.add_news(item, sc)
            out = {**item, "ticker": sc.ticker or item.get("ticker", ""), "topic": sc.topic,
                   "sentiment": sc.sentiment, "relevance": sc.relevance, "scorer": sc.scorer,
                   "ingested": datetime.now(timezone.utc).isoformat()}
            self.ctx.bus.publish("news", out)
            self.ctx.on_news(out)

    def sentiment_for(self, ticker: str, hours: float = 6) -> float:
        at = self.ctx.now()
        rows = self.ctx.db.recent_news(ticker, (at - timedelta(hours=hours)).isoformat())
        return aggregate_sentiment(rows, at)
