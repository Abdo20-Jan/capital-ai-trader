"""Login do app e cofre de credenciais.

- APP_PASSWORD: senha de acesso ao app. OBRIGATÓRIA quando o app roda na nuvem (link público).
- APP_SECRET: chave usada para assinar sessões e criptografar as credenciais salvas pelo painel.
- Credenciais digitadas no painel (Capital.com, Alpaca, IA...) ficam CRIPTOGRAFADAS no banco,
  que no Railway fica num volume persistente. Variáveis de ambiente têm prioridade.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import time
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

# chaves que o painel pode salvar no cofre
VAULT_KEYS = ("BROKER", "CAPITAL_API_KEY", "CAPITAL_IDENTIFIER", "CAPITAL_PASSWORD", "CAPITAL_ACCOUNT_ID",
              "ALPACA_KEY_ID", "ALPACA_SECRET_KEY", "ALPACA_FEED", "ANTHROPIC_API_KEY", "CLAUDE_MODEL",
              "LLM_MODE", "LOCAL_LLM_URL", "LOCAL_LLM_KIND", "LOCAL_LLM_MODEL", "LOCAL_LLM_API_KEY", "LOCAL_LLM_THINK",
              "SEC_CONTACT_EMAIL", "WATCHLIST", "START_BALANCE", "TARGET_BALANCE", "MAX_DAILY_LOSS", "STOP_AT_TARGET")
SECRET_FIELDS = {"CAPITAL_API_KEY", "CAPITAL_PASSWORD", "ALPACA_SECRET_KEY", "ANTHROPIC_API_KEY", "LOCAL_LLM_API_KEY",
                 "ALPACA_KEY_ID"}


def _secret(data_dir: Path) -> bytes:
    s = os.environ.get("APP_SECRET", "").strip()
    if s:
        return hashlib.sha256(s.encode()).digest()
    f = data_dir / ".app_secret"                      # gerado uma vez e guardado no volume
    if not f.exists():
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(secrets.token_hex(32))
        try:
            os.chmod(f, 0o600)
        except OSError:
            pass
    return hashlib.sha256(f.read_text().strip().encode()).digest()


class Vault:
    def __init__(self, db, data_dir: Path):
        self.db = db
        self.key = _secret(data_dir)
        self.fernet = Fernet(base64.urlsafe_b64encode(self.key))
        self.env_keys = {k for k in VAULT_KEYS if os.environ.get(k, "").strip()}   # definidos no servidor

    def load(self) -> dict:
        raw = self.db.get("vault")
        if not raw:
            return {}
        try:
            return json.loads(self.fernet.decrypt(raw.encode()).decode())
        except (InvalidToken, ValueError):
            return {}

    def save(self, values: dict) -> None:
        cur = self.load()
        for k, v in values.items():
            if k in VAULT_KEYS and v is not None:
                cur[k] = str(v)
        self.db.set("vault", self.fernet.encrypt(json.dumps(cur).encode()).decode())
        self.apply()

    def apply(self) -> None:
        """Coloca as credenciais do cofre no ambiente (sem sobrescrever variáveis do servidor)."""
        for k, v in self.load().items():
            if k not in self.env_keys:
                os.environ[k] = v

    def public_view(self) -> dict:
        out = {}
        for k in VAULT_KEYS:
            v = os.environ.get(k, "")
            src = "servidor" if k in self.env_keys else ("painel" if v else "")
            if k in SECRET_FIELDS:
                out[k] = {"set": bool(v), "hint": (v[:3] + "…" + v[-2:]) if len(v) > 8 else ("•••" if v else ""), "source": src}
            else:
                out[k] = {"set": bool(v), "value": v, "source": src}
        return out


class Auth:
    def __init__(self, data_dir: Path, public: bool):
        self.password = os.environ.get("APP_PASSWORD", "")
        self.public = public
        self.key = _secret(data_dir)

    @property
    def enabled(self) -> bool:
        return bool(self.password)

    @property
    def blocked(self) -> bool:
        """Na nuvem sem senha, o app recusa acesso (senão qualquer pessoa operaria sua conta)."""
        return self.public and not self.password

    def check_password(self, pw: str) -> bool:
        return bool(self.password) and hmac.compare_digest(pw.encode(), self.password.encode())

    def issue(self, days: int = 30) -> str:
        exp = str(int(time.time()) + days * 86400)
        sig = hmac.new(self.key, exp.encode(), hashlib.sha256).hexdigest()
        return f"{exp}.{sig}"

    def verify(self, token: str | None) -> bool:
        if not self.enabled:
            return not self.public
        if not token or "." not in token:
            return False
        exp, sig = token.split(".", 1)
        good = hmac.new(self.key, exp.encode(), hashlib.sha256).hexdigest()
        return hmac.compare_digest(sig, good) and exp.isdigit() and int(exp) > time.time()
