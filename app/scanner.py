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
from app.net_dealer import compute, setup_confidence
from app.range_stats import compute_range
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
    week_index: int = 0              # 0 = this week, 1 = next week…
    strike: Optional[float] = None
    premium: Optional[float] = None  # ask paid
    est_gain_pct: Optional[float] = None   # projected % gain at the C pin
    breakeven: Optional[float] = None
    contract_oi: Optional[float] = None
    contract_volume: Optional[float] = None
    max_pain: Optional[float] = None
    call_wall: Optional[float] = None
    put_wall: Optional[float] = None
    # --- range achievability (Biggjuann/Range 30d ADR / widest) ---
    required_move_pct: Optional[float] = None   # |C - spot| / spot
    adr_percent: Optional[float] = None         # 30d mean daily range %
    max_range_percent: Optional[float] = None   # 30d widest single-day range %
    range_conf: Optional[str] = None            # within-mean / within-max / out-of-range / unknown
    range_days: Optional[int] = None
    # --- crush / zone / confidence (the creator's "will it work" signals) ---
    expensive_side: Optional[str] = None        # CALL / PUT — richer OTM premium
    crush_direction: Optional[str] = None       # DOWN / UP — where dealers push
    direction_agree: Optional[bool] = None      # crush dir agrees with C vs spot
    sweet_spot_low: Optional[float] = None
    sweet_spot_high: Optional[float] = None
    pin_steepness: Optional[float] = None
    confidence: Optional[float] = None          # 0..100 full setup confidence
    confidence_breakdown: Optional[dict] = None
    opportunity_score: Optional[float] = None   # est_gain × confidence (ranking key)
    skip: Optional[str] = None       # reason it is not a ranked opportunity


# ----- range cache (history changes slowly; cache well beyond one scan) -----
_range_lock = threading.Lock()
_range_cache: dict = {}


def _range_for(provider, ticker: str) -> Optional[dict]:
    key = (ticker.upper(), settings.range_days, "live" if settings.live else "mock")
    now = time.time()
    with _range_lock:
        hit = _range_cache.get(key)
        if hit and now - hit[0] < settings.range_cache_seconds:
            return hit[1]
    stats = None
    try:
        candles = provider.daily_history(ticker, settings.range_days)
        if candles:
            stats = compute_range(ticker, candles, settings.range_days)
    except Exception as exc:  # pragma: no cover - network
        log.warning("range %s failed: %s", ticker, exc)
        stats = None
    with _range_lock:
        _range_cache[key] = (now, stats)
    return stats


def classify_range(required_move: float, spot: float, dte: float, stats: Optional[dict]):
    """Is the move to C within the ticker's proven range?

    Achievable move by expiry = (daily range $) × horizon × reach_mult, where
    horizon = trading days to expiry, capped by RANGE_MAX_HORIZON. Compared
    against the 30d MEAN daily range (ADR) and the 30d MAX (widest) daily range.
    Returns (confidence, mean_reach$, max_reach$).
    """
    if not stats or not spot:
        return "unknown", None, None
    horizon = min(max(1.0, dte), settings.range_max_horizon)
    adr = stats.get("adr_dollars") or 0.0
    maxr = stats.get("max_range_dollars") or 0.0
    mean_reach = adr * horizon * settings.range_reach_mult
    max_reach = maxr * horizon * settings.range_reach_mult
    if mean_reach and required_move <= mean_reach:
        return "within-mean", mean_reach, max_reach
    if max_reach and required_move <= max_reach:
        return "within-max", mean_reach, max_reach
    return "out-of-range", mean_reach, max_reach


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


def evaluate(provider, ticker: str, week_index: int = 0) -> ScanRow:
    try:
        expiry, rows, spot = provider.get_weekly_chain(
            ticker, week_index=week_index, strike_count=settings.strike_count)
    except Exception as exc:  # pragma: no cover - network
        return ScanRow(ticker=ticker, spot=None, c_target=None, edge_pct=None,
                       direction=None, expiry=None, dte=None, week_index=week_index,
                       skip=f"fetch error: {exc}")

    if not rows or not spot:
        return ScanRow(ticker=ticker, spot=spot, c_target=None, edge_pct=None,
                       direction=None, expiry=expiry, dte=None, week_index=week_index,
                       skip="no chain / spot")

    ty, dte = t_years(expiry)
    res = compute(ticker, expiry, rows, spot, ty, dte,
                  r=settings.risk_free_rate, fallback_iv=settings.fallback_iv)
    c = res.c_target
    base = ScanRow(ticker=ticker, spot=spot, c_target=c,
                   edge_pct=None, direction=None, expiry=expiry, dte=round(dte, 2),
                   week_index=week_index,
                   max_pain=res.max_pain, call_wall=res.call_wall, put_wall=res.put_wall)
    if c is None or not spot:
        base.skip = "no C target"
        return base

    edge = (c - spot) / spot
    base.edge_pct = round(edge, 4)
    base.direction = "CALL" if edge > 0 else "PUT"
    base.required_move_pct = round(abs(edge), 4)
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

    # Range achievability: can the ticker realistically travel to C by expiry?
    base.range_days = settings.range_days
    stats = _range_for(provider, ticker)
    if stats:
        base.adr_percent = stats.get("adr_percent")
        base.max_range_percent = stats.get("max_range_percent")
    range_conf, _mean_reach, _max_reach = classify_range(abs(c - spot), spot, dte, stats)
    base.range_conf = range_conf

    # Crush / zone / pin signals from the engine, + full confidence score that
    # folds in range reachability and the picked contract's liquidity.
    base.expensive_side = res.expensive_side
    base.crush_direction = res.crush_direction
    base.direction_agree = res.direction_agree
    base.sweet_spot_low = res.sweet_spot_low
    base.sweet_spot_high = res.sweet_spot_high
    base.pin_steepness = res.pin_steepness
    expensive_crush = (res.call_crush_pct if res.expensive_side == "CALL"
                       else res.put_crush_pct if res.expensive_side == "PUT" else None)
    score, breakdown = setup_confidence(res.direction_agree, expensive_crush,
                                        res.pin_steepness, reachability=range_conf,
                                        liquidity_oi=base.contract_oi)
    base.confidence = score
    base.confidence_breakdown = breakdown
    base.opportunity_score = round(base.est_gain_pct * score / 100.0, 4)
    return base


# ----- cached scan ----------------------------------------------------------
_lock = threading.Lock()
_cache: dict = {"ts": 0.0, "key": None, "payload": None}


def run_scan(provider, tickers: Optional[List[str]] = None, use_cache: bool = True,
             range_filter: Optional[bool] = None, weeks: Optional[List[int]] = None) -> dict:
    tickers = [t.upper() for t in (tickers or settings.scan_tickers)]
    rfilter = settings.range_filter if range_filter is None else range_filter
    weeks = sorted(set(weeks if weeks else [0]))
    # ticker × week combinations to evaluate
    tasks = [(t, w) for t in tickers for w in weeks]
    key = (tuple(tickers), tuple(weeks), "live" if settings.live else "mock", rfilter)
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
    workers = max(1, min(settings.scan_workers, len(tasks)))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futs = {pool.submit(evaluate, provider, t, w): (t, w) for (t, w) in tasks}
        for fut in as_completed(futs):
            try:
                results.append(fut.result())
            except Exception as exc:  # pragma: no cover - defensive
                tk, wk = futs[fut]
                results.append(ScanRow(ticker=tk, spot=None, c_target=None,
                                       edge_pct=None, direction=None, expiry=None,
                                       dte=None, week_index=wk, skip=f"error: {exc}"))

    tradeable = [r for r in results if r.skip is None and r.est_gain_pct is not None]

    # Range achievability filter: drop candidates whose move to C is beyond what
    # the ticker has proven it can travel (out-of-range). "unknown" (range data
    # unavailable) fails open — kept, but flagged — so a data hiccup never blanks
    # the scan.
    filtered_out = []
    if rfilter:
        keep = []
        for r in tradeable:
            if r.range_conf == "out-of-range":
                mv = (r.required_move_pct or 0) * 100
                mx = r.max_range_percent
                r.skip = (f"move to C {mv:.1f}% exceeds {r.range_days}d range"
                          + (f" (max daily {mx:.1f}%)" if mx else ""))
                filtered_out.append(r)
            else:
                keep.append(r)
        ranked = keep
    else:
        ranked = tradeable

    # Rank by reward × confidence, so a slightly-smaller gain with a much
    # stronger setup (direction agrees, high crush fuel, sharp pin, reachable)
    # outranks a big-gain-but-shaky one. Falls back to raw gain if unscored.
    ranked.sort(key=lambda r: (r.opportunity_score if r.opportunity_score is not None
                               else r.est_gain_pct), reverse=True)
    skipped = [r for r in results if r.skip is not None]

    payload = {
        "mode": "live" if settings.live else "mock",
        "scanned": len(tickers),
        "weeks": weeks,
        "evaluations": len(tasks),
        "opportunities": len(ranked),
        "range_filter": rfilter,
        "range_days": settings.range_days,
        "range_filtered_out": len(filtered_out),
        "elapsed_seconds": round(time.time() - t0, 2),
        "results": [asdict(r) for r in ranked],
        "skipped": [{"ticker": r.ticker, "reason": r.skip} for r in skipped],
        "cached": False,
        "age_seconds": 0.0,
    }
    with _lock:
        _cache.update(ts=time.time(), key=key, payload=payload)
    return payload
