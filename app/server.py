"""FastAPI app: serves the dashboard and the net-dealer API.

Endpoints
    GET /                       -> the dashboard (ticker + expiry selectors)
    GET /api/config             -> mode, tickers, model params (health check)
    GET /api/expiries?ticker=   -> available expiration dates
    GET /api/net-dealer?ticker=&expiry=  -> the "C" target + full chain analysis
    GET /api/scan               -> curated universe ranked by projected %-gain to C
"""
from __future__ import annotations

import logging
import os
from dataclasses import asdict

from fastapi import FastAPI, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse

from app.config import settings
from app.net_dealer import compute, setup_confidence
from app.providers.factory import build_provider
from app.scanner import _range_for, classify_range, rank_contracts, run_scan
from app.timeutil import t_years

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

    ty, dte = t_years(expiry)
    result = compute(
        ticker=ticker, expiry=expiry, rows=rows, spot=spot,
        t_years=ty, dte=dte,
        r=settings.risk_free_rate, fallback_iv=settings.fallback_iv,
    )
    payload = asdict(result)
    payload["mode"] = "live" if settings.live else "mock"

    # Best option picks for THIS ticker/expiry: contracts that profit most if
    # price pins to C. Side follows the dealer pull (C above spot -> calls).
    side, picks = None, []
    if result.c_target is not None and spot:
        side = "CALL" if result.c_target > spot else "PUT"
        picks = rank_contracts(rows, result.c_target, side, min_gain=0.0, limit=6)
    payload["pick_side"] = side
    payload["picks"] = picks

    # Upgrade the chain-only confidence to the SAME full score the scanner shows:
    # fold in 30d reachability and the top pick's liquidity, so the Calculator and
    # Scanner agree for a given ticker/expiry.
    if result.c_target is not None and spot:
        stats = _range_for(provider, ticker)
        range_conf, _m, _x = classify_range(abs(result.c_target - spot), spot, dte, stats)
        liq_oi = picks[0]["contract_oi"] if picks else None
        expensive_crush = (result.call_crush_pct if result.expensive_side == "CALL"
                           else result.put_crush_pct if result.expensive_side == "PUT" else None)
        score, breakdown = setup_confidence(result.direction_agree, expensive_crush,
                                            result.pin_steepness, range_reach=range_conf,
                                            iv_reach=result.iv_reach, liquidity_oi=liq_oi)
        payload["confidence"] = score
        payload["confidence_breakdown"] = breakdown
        payload["range_conf"] = range_conf
        if stats:
            payload["adr_percent"] = stats.get("adr_percent")
            payload["max_range_percent"] = stats.get("max_range_percent")
    return JSONResponse(payload)


_WEEK_MAP = {"this": [0], "next": [1], "both": [0, 1]}


@app.get("/api/scan")
def scan(refresh: bool = Query(False, description="bypass the short-lived cache"),
         range_filter: bool = Query(None, description="override the 30d range achievability filter"),
         weeks: str = Query("this", description="which weekly expiry: this | next | both")
         ) -> JSONResponse:
    """Rank the curated liquid universe by projected option %-gain to the C pin.

    ``weeks`` picks the expiry: this week (nearest), next week, or both.
    By default only surfaces tickers whose move to C is within their proven
    ~30-day range (Biggjuann/Range methodology); pass range_filter=false to see all.
    """
    wk = _WEEK_MAP.get((weeks or "this").strip().lower(), [0])
    try:
        payload = run_scan(provider, use_cache=not refresh, range_filter=range_filter, weeks=wk)
    except Exception as exc:  # pragma: no cover - defensive
        log.warning("scan failed: %s", exc)
        return JSONResponse({"error": f"scan failed: {exc}"}, status_code=502)
    return JSONResponse(payload)


def main() -> None:
    import uvicorn
    log.info("Net Dealer Target calculator on %s:%s (mode=%s)",
             settings.host, settings.port, "live" if settings.live else "mock")
    uvicorn.run(app, host=settings.host, port=settings.port)
