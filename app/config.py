"""Runtime configuration for the Net Dealer Target calculator.

Loaded from the environment (and an optional local ``.env``). The Schwab
access is the *shared* token mechanism used by the 0DTE / MM services, so this
app never holds Schwab credentials directly — it pulls a short-lived access
token over HTTP from the MM token endpoint.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import List

try:
    from dotenv import load_dotenv

    load_dotenv()
except Exception:  # pragma: no cover - dotenv optional
    pass


def _int(name: str, default: int) -> int:
    try:
        return int(float(os.getenv(name, default)))
    except (TypeError, ValueError):
        return default


def _float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, default))
    except (TypeError, ValueError):
        return default


def _list(name: str, default: List[str]) -> List[str]:
    raw = os.getenv(name)
    if not raw:
        return default
    return [x.strip().upper() for x in raw.split(",") if x.strip()]


@dataclass
class Settings:
    # "mock" -> synthetic chain, no network (default so it runs anywhere).
    # "live" -> pull real chains from Schwab using the shared token.
    data_mode: str = field(default_factory=lambda: os.getenv("DATA_MODE", "mock").lower())

    # Tickers offered in the dropdown (the user can also type any symbol).
    tickers: List[str] = field(default_factory=lambda: _list(
        "TICKERS", ["MU", "SPY", "QQQ", "AAPL", "NVDA", "TSLA", "AMD", "META"]))

    # ----- Schwab market data (shared token; read-only, chains + quotes) -----
    schwab_base_url: str = field(default_factory=lambda: os.getenv(
        "SCHWAB_BASE_URL", "https://api.schwabapi.com"))

    # Shared-token mechanism (identical vars to the 0DTE / MM services).
    schwab_auth_mode: str = field(default_factory=lambda: os.getenv(
        "SCHWAB_AUTH_MODE", "shared").lower())
    schwab_token_url: str = field(default_factory=lambda: os.getenv("SCHWAB_TOKEN_URL", ""))
    schwab_token_share_key: str = field(default_factory=lambda: os.getenv(
        "SCHWAB_TOKEN_SHARE_KEY", ""))

    # MM base is the default source of the token endpoint + share-key fallback,
    # so pointing this app at the same MM service is enough to go live.
    mm_base_url: str = field(default_factory=lambda: os.getenv(
        "MM_BASE_URL", "https://web-production-fff5c.up.railway.app"))
    mm_api_key: str = field(default_factory=lambda: os.getenv("MM_API_KEY", ""))

    # ----- Net-dealer model knobs -------------------------------------------
    risk_free_rate: float = field(default_factory=lambda: _float("RISK_FREE_RATE", 0.043))
    fallback_iv: float = field(default_factory=lambda: _float("FALLBACK_IV", 0.40))
    # How many strikes (each side of the money) to pull for the chain.
    strike_count: int = field(default_factory=lambda: _int("STRIKE_COUNT", 80))
    # How far out (days) to look when listing available expirations.
    expiry_horizon_days: int = field(default_factory=lambda: _int("EXPIRY_HORIZON_DAYS", 60))

    # ----- Web -------------------------------------------------------------
    # CORS origins allowed to call the API (for a GitHub Pages front-end that
    # is hosted on a different origin than the deployed backend). "*" allowed.
    cors_origins: List[str] = field(default_factory=lambda: [
        x.strip() for x in os.getenv("CORS_ORIGINS", "*").split(",") if x.strip()])

    host: str = field(default_factory=lambda: os.getenv("HOST", "0.0.0.0"))
    port: int = field(default_factory=lambda: _int("PORT", 8080))

    @property
    def live(self) -> bool:
        return self.data_mode == "live"

    @property
    def token_url(self) -> str:
        """Resolved Schwab token endpoint (defaults to MM's /auth/token)."""
        return self.schwab_token_url or f"{self.mm_base_url.rstrip('/')}/auth/token"

    @property
    def token_share_key(self) -> str:
        """Bearer key for the shared token (SCHWAB_TOKEN_SHARE_KEY, MM_API_KEY fallback)."""
        return self.schwab_token_share_key or self.mm_api_key


settings = Settings()
