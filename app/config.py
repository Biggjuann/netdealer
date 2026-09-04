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

from app.universe import LIQUID, UNIVERSE

try:
    from dotenv import load_dotenv

    load_dotenv()
except Exception:  # pragma: no cover - dotenv optional
    pass


def _bool(name: str, default: bool) -> bool:
    return os.getenv(name, str(default)).strip().lower() in {"1", "true", "yes", "on"}


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
    # "live" -> pull real chains from Schwab using the shared token (default).
    # "mock" -> synthetic chain, no network (for offline/CI/demo only).
    data_mode: str = field(default_factory=lambda: os.getenv("DATA_MODE", "live").lower())

    # Tickers offered in the dropdown. Defaults to the full S&P 500 + Nasdaq-100
    # universe; the UI search box also accepts any symbol Schwab recognises.
    # Set TICKERS to a comma-separated list to override (e.g. a watchlist).
    tickers: List[str] = field(default_factory=lambda: _list("TICKERS", UNIVERSE))

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
    # How OI (stale "known" inventory) and Volume (live "dynamic" inventory) are
    # combined when deriving netDIR / C / centroids.
    #   BLEND_MODE=additive   -> effective = OI + VOLUME_WEIGHT × Volume (raw counts).
    #                            Simple, but for high-volume names volume can swamp OI.
    #   BLEND_MODE=normalized -> scale-invariant blend of the OI and Volume *shapes*:
    #                            (1-α)·share_of_OI + α·share_of_Volume. Volume gets an
    #                            equal vote regardless of how it compares to OI.
    # Revert anytime by flipping these env vars — no redeploy of code needed.
    blend_mode: str = field(default_factory=lambda: os.getenv("BLEND_MODE", "additive").lower())
    volume_weight: float = field(default_factory=lambda: _float("VOLUME_WEIGHT", 0.5))
    blend_alpha: float = field(default_factory=lambda: _float("BLEND_ALPHA", 0.5))
    # How many strikes (each side of the money) to pull for the chain.
    strike_count: int = field(default_factory=lambda: _int("STRIKE_COUNT", 80))
    # How far out (days) to look when listing available expirations.
    expiry_horizon_days: int = field(default_factory=lambda: _int("EXPIRY_HORIZON_DAYS", 60))

    # ----- Scanner ---------------------------------------------------------
    # Symbols the fast scanner sweeps. Defaults to the curated LIQUID list;
    # override with SCAN_TICKERS to point it at a custom watchlist.
    scan_tickers: List[str] = field(default_factory=lambda: _list("SCAN_TICKERS", LIQUID))
    # A candidate contract must clear these floors to be tradeable (keeps penny/
    # dead strikes out of the rankings).
    scan_min_oi: float = field(default_factory=lambda: _float("SCAN_MIN_OI", 100))
    scan_min_ask: float = field(default_factory=lambda: _float("SCAN_MIN_ASK", 0.10))
    # Ignore tickers whose C is within this fraction of spot (no real edge).
    scan_min_edge_pct: float = field(default_factory=lambda: _float("SCAN_MIN_EDGE_PCT", 0.002))
    # A ranked opportunity must project at least this gain (0.10 = +10%);
    # anything less is not worth surfacing.
    scan_min_gain_pct: float = field(default_factory=lambda: _float("SCAN_MIN_GAIN_PCT", 0.10))
    # Cap the projected gain shown (guards against a stale/penny ask blowing up).
    scan_max_gain_pct: float = field(default_factory=lambda: _float("SCAN_MAX_GAIN_PCT", 20.0))
    # Only look at expiries within this many days for the "nearest weekly".
    scan_within_days: int = field(default_factory=lambda: _int("SCAN_WITHIN_DAYS", 9))
    scan_workers: int = field(default_factory=lambda: _int("SCAN_WORKERS", 8))
    scan_cache_seconds: int = field(default_factory=lambda: _int("SCAN_CACHE_SECONDS", 60))
    # Only surface same-day (0DTE) / next-day (1DTE) expirations. Max calendar
    # days-to-expiry a scan row may have; longer-dated expiries are dropped.
    scan_max_dte: float = field(default_factory=lambda: _float("SCAN_MAX_DTE", 1.99))

    # ----- Range achievability filter (Biggjuann/Range methodology) --------
    # Drop scan candidates whose required move to C exceeds what the ticker has
    # proven it can travel over the recent window — so surfaced trades are
    # "within reason" of the ticker's demonstrated range.
    range_filter: bool = field(default_factory=lambda: _bool("RANGE_FILTER", True))
    range_days: int = field(default_factory=lambda: _int("RANGE_DAYS", 30))
    # Achievable move by expiry = (daily range) × horizon_days × this multiplier.
    # Raise to loosen the filter, lower to tighten.
    range_reach_mult: float = field(default_factory=lambda: _float("RANGE_REACH_MULT", 1.0))
    # Cap the horizon (trading days to expiry) used to scale the daily range.
    range_max_horizon: float = field(default_factory=lambda: _float("RANGE_MAX_HORIZON", 5.0))
    # Range/daily-history changes slowly — cache it longer than the chain scan.
    range_cache_seconds: int = field(default_factory=lambda: _int("RANGE_CACHE_SECONDS", 3600))

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
