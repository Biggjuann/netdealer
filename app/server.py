"""FastAPI app: serves the dashboard and the net-dealer API.

Endpoints
    GET /                       -> the dashboard (ticker + expiry selectors)
    GET /api/config             -> mode, tickers, model params (health check)
    GET /api/expiries?ticker=   -> available expiration dates
    GET /api/net-dealer?ticker=&expiry=  -> the "C" target + full chain analysis
"""
from __future__ import annotations

import datetime as dt
import logging
import os
from dataclasses import asdict

from fastapi import FastAPI, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse

from app.config import settings
from app.net_dealer import compute
from app.providers.factory import build_provider

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("server")

STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")

app = FastAPI(title="Net Dealer Target — 'C' Calculator")
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins or ["*"],
    allow_methods=["GET"],
    allow_headers=["*"],
)

provider = build_provider()


def _t_years(expiry: str) -> tuple[float, float]:
    """(years, calendar-days) from now to the expiry's 16:00 ET close-ish."""
    try:
        exp = dt.datetime.strptime(expiry, "%Y-%m-%d")
    except ValueError:
        return 1.0 / 365.0, 1.0
    # Approximate the expiry moment as 4pm ET (21:00 UTC) that day.
    exp = exp.replace(hour=21, minute=0)
    now = dt.datetime.utcnow()
    seconds = (exp - now).total_seconds()
    days = max(0.0, seconds / 86400.0)
    return max(seconds / (365.0 * 86400.0), 0.0), days


@app.get("/")
def index() -> FileResponse:
    return FileResponse(os.path.join(STATIC_DIR, "dashboard.html"))


@app.get("/api/config")
def config() -> JSONResponse:
    return JSONResponse({
        "mode": "live" if settings.live else "mock",
        "tickers": settings.tickers,
        "risk_free_rate": settings.risk_free_rate,
        "fallback_iv": settings.fallback_iv,
        "token_configured": bool(settings.token_share_key),
    })


@app.get("/api/expiries")
def expiries(ticker: str = Query(..., min_length=1)) -> JSONResponse:
    try:
        exps = provider.get_expirations(ticker)
    except Exception as exc:  # pragma: no cover - network
        log.warning("expiries %s failed: %s", ticker, exc)
        return JSONResponse({"ticker": ticker.upper(), "expiries": [], "error": str(exc)},
                            status_code=502)
    return JSONResponse({"ticker": ticker.upper(), "expiries": exps})


@app.get("/api/net-dealer")
def net_dealer(ticker: str = Query(..., min_length=1),
               expiry: str = Query(..., min_length=8)) -> JSONResponse:
    try:
        rows, spot = provider.get_chain(ticker, expiry, settings.strike_count)
    except Exception as exc:  # pragma: no cover - network
        log.warning("chain %s %s failed: %s", ticker, expiry, exc)
        return JSONResponse({"error": f"chain fetch failed: {exc}"}, status_code=502)

    t_years, dte = _t_years(expiry)
    result = compute(
        ticker=ticker, expiry=expiry, rows=rows, spot=spot,
        t_years=t_years, dte=dte,
        r=settings.risk_free_rate, fallback_iv=settings.fallback_iv,
    )
    payload = asdict(result)
    payload["mode"] = "live" if settings.live else "mock"
    return JSONResponse(payload)


def main() -> None:
    import uvicorn
    log.info("Net Dealer Target calculator on %s:%s (mode=%s)",
             settings.host, settings.port, "live" if settings.live else "mock")
    uvicorn.run(app, host=settings.host, port=settings.port)
