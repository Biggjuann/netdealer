"""Net-dealer opportunity scanner.

Sweeps a curated list of liquid tickers, computes each one's net-dealer "C"
target on the nearest weekly expiry, and ranks them by the **projected option
% gain** if price pins to C by expiry — i.e. "which trade offers the biggest
bang if the dealer target plays out?"

Per ticker:
  1. pull the nearest-weekly chain (one API call) and compute C (see net_dealer).
  2. edge% = (C − spot)/spot. Sign picks the side:
        C > spot → dealers pulling UP  → BUY CALL
        C < spot → dealers pulling DOWN → BUY PUT
  3. among the listed strikes that finish IN-THE-MONEY at C and clear the
     liquidity floors (OI / ask), pick the one that MAXIMISES

        est_gain = (intrinsic_at_C − ask) / ask         (hold-to-expiry pin at C)

     which naturally selects the cheap-but-still-finishes-ITM sweet spot.

The scan runs synchronously with a small thread pool (Schwab's client is
blocking) and caches the last result briefly so the UI is instant on refresh.

Everything except the network call is pure and unit-tested with the mock feed.
"""
from __future__ import annotations

import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass, field
from typing import List, Optional

from app.config import settings
from app.net_dealer import compute
from app.timeutil import t_years

log = logging.getLogger("scanner")


@dataclass
class ScanRow:
    ticker: str
    spot: Optional[float]
    c_target: Optional[float]
    edge_pct: Optional[float]        # signed (C-spot)/spot
    direction: Optional[str]         # "CALL" / "PUT"
    expiry: Optional[str]
    dte: Optional[float]
    strike: Optional[float] = None
    premium: Optional[float] = None  # ask paid
    est_gain_pct: Optional[float] = None   # projected % gain at the C pin
    breakeven: Optional[float] = None
    contract_oi: Optional[float] = None
    contract_volume: Optional[float] = None
    max_pain: Optional[float] = None
    call_wall: Optional[float] = None
    put_wall: Optional[float] = None
    skip: Optional[str] = None       # reason it is not a ranked opportunity


def rank_contracts(rows, c_target: float, side: str, min_gain: Optional[float] = None,
                   min_oi: Optional[float] = None, min_ask: Optional[float] = None,
                   limit: Optional[int] = None) -> List[dict]:
    """Rank one side's strikes by projected %-gain if price pins to C.

    A contract is eligible when it (a) finishes in-the-money at C, (b) clears the
    liquidity floors (OI / ask), and (c) projects at least ``min_gain``. Returned
    sorted by est_gain descending. Shared by the scanner (single best pick) and
    the calculator (top-N picks for the selected ticker/expiry).
    """
    mg = settings.scan_min_gain_pct if min_gain is None else min_gain
    moi = settings.scan_min_oi if min_oi is None else min_oi
    mask = settings.scan_min_ask if min_ask is None else min_ask
    out: List[dict] = []
    for row in rows:
        if side == "CALL":
            oi, vol, ask, k = row.call_oi, row.call_volume, row.call_ask, row.strike
            intrinsic = max(c_target - k, 0.0)
            breakeven = k + ask
        else:
            oi, vol, ask, k = row.put_oi, row.put_volume, row.put_ask, row.strike
            intrinsic = max(k - c_target, 0.0)
            breakeven = k - ask
        if intrinsic <= 0 or ask < mask or oi < moi:
            continue
        gain = (intrinsic - ask) / ask
        if gain < mg:
            continue
        out.append({
            "side": side,
            "strike": k,
            "premium": round(ask, 2),
            "est_gain_pct": round(min(gain, settings.scan_max_gain_pct), 4),
            "breakeven": round(breakeven, 2),
            "intrinsic_at_c": round(intrinsic, 2),
            "contract_oi": oi,
            "contract_volume": vol,
        })
    out.sort(key=lambda d: d["est_gain_pct"], reverse=True)
    return out[:limit] if limit else out


def _best_contract(rows, c_target: float, side: str):
    """The single highest-projected-gain liquid contract (scanner's pick)."""
    ranked = rank_contracts(rows, c_target, side)
    if not ranked:
        return None
    b = ranked[0]
    return (b["est_gain_pct"], b["strike"], b["premium"],
            b["contract_oi"], b["contract_volume"], b["breakeven"])


def evaluate(provider, ticker: str) -> ScanRow:
    try:
        expiry, rows, spot = provider.get_nearest_chain(
            ticker, within_days=settings.scan_within_days, strike_count=settings.strike_count)
    except Exception as exc:  # pragma: no cover - network
        return ScanRow(ticker=ticker, spot=None, c_target=None, edge_pct=None,
                       direction=None, expiry=None, dte=None, skip=f"fetch error: {exc}")

    if not rows or not spot:
        return ScanRow(ticker=ticker, spot=spot, c_target=None, edge_pct=None,
                       direction=None, expiry=expiry, dte=None, skip="no chain / spot")

    ty, dte = t_years(expiry)
    res = compute(ticker, expiry, rows, spot, ty, dte,
                  r=settings.risk_free_rate, fallback_iv=settings.fallback_iv)
    c = res.c_target
    base = ScanRow(ticker=ticker, spot=spot, c_target=c,
                   edge_pct=None, direction=None, expiry=expiry, dte=round(dte, 2),
                   max_pain=res.max_pain, call_wall=res.call_wall, put_wall=res.put_wall)
    if c is None or not spot:
        base.skip = "no C target"
        return base

    edge = (c - spot) / spot
    base.edge_pct = round(edge, 4)
    base.direction = "CALL" if edge > 0 else "PUT"
    if abs(edge) < settings.scan_min_edge_pct:
        base.skip = "C ~= spot (no edge)"
        return base

    best = _best_contract(rows, c, base.direction)
    if best is None:
        base.skip = "no liquid contract clears min gain"
        return base

    gain, k, ask, oi, vol, be = best
    base.strike = k
    base.premium = round(ask, 2)
    base.est_gain_pct = round(min(gain, settings.scan_max_gain_pct), 4)
    base.breakeven = round(be, 2)
    base.contract_oi = oi
    base.contract_volume = vol
    return base


# ----- cached scan ----------------------------------------------------------
_lock = threading.Lock()
_cache: dict = {"ts": 0.0, "key": None, "payload": None}


def run_scan(provider, tickers: Optional[List[str]] = None, use_cache: bool = True) -> dict:
    tickers = [t.upper() for t in (tickers or settings.scan_tickers)]
    key = (tuple(tickers), "live" if settings.live else "mock")
    now = time.time()
    if use_cache:
        with _lock:
            if (_cache["key"] == key and _cache["payload"]
                    and now - _cache["ts"] < settings.scan_cache_seconds):
                cached = dict(_cache["payload"])
                cached["cached"] = True
                cached["age_seconds"] = round(now - _cache["ts"], 1)
                return cached

    t0 = time.time()
    results: List[ScanRow] = []
    workers = max(1, min(settings.scan_workers, len(tickers)))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futs = {pool.submit(evaluate, provider, t): t for t in tickers}
        for fut in as_completed(futs):
            try:
                results.append(fut.result())
            except Exception as exc:  # pragma: no cover - defensive
                results.append(ScanRow(ticker=futs[fut], spot=None, c_target=None,
                                       edge_pct=None, direction=None, expiry=None,
                                       dte=None, skip=f"error: {exc}"))

    ranked = [r for r in results if r.skip is None and r.est_gain_pct is not None]
    ranked.sort(key=lambda r: r.est_gain_pct, reverse=True)
    skipped = [r for r in results if r.skip is not None]

    payload = {
        "mode": "live" if settings.live else "mock",
        "scanned": len(tickers),
        "opportunities": len(ranked),
        "elapsed_seconds": round(time.time() - t0, 2),
        "results": [asdict(r) for r in ranked],
        "skipped": [{"ticker": r.ticker, "reason": r.skip} for r in skipped],
        "cached": False,
        "age_seconds": 0.0,
    }
    with _lock:
        _cache.update(ts=time.time(), key=key, payload=payload)
    return payload
