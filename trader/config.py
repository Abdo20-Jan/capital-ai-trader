"""Configuração lida do arquivo .env (ou de variáveis de ambiente)."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def load_dotenv(path: Path | None = None) -> None:
    """Parser mínimo de .env: KEY=VALUE, ignora comentários. Não sobrescreve variáveis já definidas."""
    path = path or ROOT / ".env"
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def _f(name: str, default: float) -> float:
    v = os.environ.get(name, "")
    return float(v) if v.strip() else default


def _i(name: str, default: int) -> int:
    v = os.environ.get(name, "")
    return int(v) if v.strip() else default


def _s(name: str, default: str) -> str:
    v = os.environ.get(name, "")
    return v.strip() if v.strip() else default


@dataclass
class WatchItem:
    epic: str      # identificador do mercado na Capital.com
    ticker: str    # ticker usado para buscar notícias


@dataclass
class Settings:
    broker: str = "paper"
    capital_api_key: str = ""
    capital_identifier: str = ""
    capital_password: str = ""
    anthropic_api_key: str = ""
    claude_model: str = "claude-opus-5-5"
    watchlist: list[WatchItem] = field(default_factory=list)
    resolution: str = "MINUTE_15"
    loop_seconds: int = 60
    start_balance: float = 60.0
    target_balance: float = 100.0
    risk_per_trade: float = 0.02
    max_risk_per_trade: float = 0.05
    max_daily_loss: float = 0.06
    max_open_positions: int = 2
    max_margin_usage: float = 0.5
    max_leverage: float = 200.0          # alavancagem máxima da conta (só usada pelo simulador)
    max_exposure: float = 30.0           # teto de exposição: valor nocional total ≤ 30× o patrimônio
    stop_atr: float = 1.5
    take_profit_atr: float = 2.25
    prob_threshold: float = 0.50
    sentiment_weight: float = 0.10
    db_path: Path = ROOT / "data" / "trader.db"
    model_dir: Path = ROOT / "models"

    @property
    def use_claude(self) -> bool:
        return bool(self.anthropic_api_key)


def parse_watchlist(raw: str) -> list[WatchItem]:
    items = []
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        epic, _, ticker = part.partition(":")
        items.append(WatchItem(epic=epic.strip(), ticker=(ticker or epic).strip()))
    return items


def get_settings() -> Settings:
    load_dotenv()
    db = Path(_s("DB_PATH", "data/trader.db"))
    if not db.is_absolute():
        db = ROOT / db
    s = Settings(
        broker=_s("BROKER", "paper").lower(),
        capital_api_key=_s("CAPITAL_API_KEY", ""),
        capital_identifier=_s("CAPITAL_IDENTIFIER", ""),
        capital_password=_s("CAPITAL_PASSWORD", ""),
        anthropic_api_key=_s("ANTHROPIC_API_KEY", ""),
        claude_model=_s("CLAUDE_MODEL", "claude-opus-5-5"),
        watchlist=parse_watchlist(_s("WATCHLIST", "AAPL,MSFT,NVDA,GOOGL,AMZN,META,TSLA")),
        resolution=_s("RESOLUTION", "MINUTE_15"),
        loop_seconds=_i("LOOP_SECONDS", 60),
        start_balance=_f("START_BALANCE", 60.0),
        target_balance=_f("TARGET_BALANCE", 100.0),
        risk_per_trade=_f("RISK_PER_TRADE", 0.02),
        max_risk_per_trade=_f("MAX_RISK_PER_TRADE", 0.05),
        max_daily_loss=_f("MAX_DAILY_LOSS", 0.06),
        max_open_positions=_i("MAX_OPEN_POSITIONS", 2),
        max_margin_usage=_f("MAX_MARGIN_USAGE", 0.5),
        max_leverage=_f("MAX_LEVERAGE", 200.0),
        max_exposure=_f("MAX_EXPOSURE", 30.0),
        stop_atr=_f("STOP_ATR", 1.5),
        take_profit_atr=_f("TAKE_PROFIT_ATR", 2.25),
        prob_threshold=_f("PROB_THRESHOLD", 0.50),
        sentiment_weight=_f("SENTIMENT_WEIGHT", 0.10),
        db_path=db,
    )
    if s.broker not in ("paper", "capital_demo"):
        raise ValueError(f"BROKER inválido: {s.broker!r} (use 'paper' ou 'capital_demo')")
    return s
