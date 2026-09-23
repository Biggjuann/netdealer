"""0DTE SPX trade plan — the end-of-day "pin to C" ticket.

Everything is computed **natively on the live SPX 0DTE chain**. Schwab reports
~zero resting open interest for the SPX same-day (`.SPXW`) options, but their
VOLUME is enormous — that live volume *is* the intraday dealer inventory the
framework calls "dynamic." So C, the walls, max-pain and the centroids all come
straight off the SPX volume (no SPY, no ×10 basis conversion — an earlier
version mapped SPY's stale OI onto SPX and mislocated the walls badly).

**The rule (EOD Pin).** Only inside the final ``ARM_MINUTES`` of the regular
session, with the side set by C vs spot (the pin target): spot at the ceiling
with C below → **buy the ATM PUT**; spot at the floor with C above → **buy the
ATM CALL**; ride price to C and exit at **C or the cash settlement, whichever
hits first**. Strictly ATM, no stop — max risk is the whole premium, so the arm
window, the payoff test (a perfect pin must beat the premium), and the
reachability read are the guardrails. Outside the window the ticket is disarmed
(WAIT / CLOSED).

This is a **trade plan / signal**, not an order router — the app is read-only
and never routes orders.
"""
from __future__ import annotations

import datetime as dt
import math
from typing import List, Optional

from app.market_calendar import close_hour_et, is_early_close, is_trading_day
from app.net_dealer import NetDealerResult

try:
    from zoneinfo import ZoneInfo
    _ET = ZoneInfo("America/New_York")
except Exception:  # pragma: no cover - zoneinfo always present on 3.11
    _ET = None

# EOD Pin: only arm inside the final N minutes of the regular session; the pin
# to C completes into the cash settlement. Regular open is 9:30 ET.
ARM_MINUTES = 15
_RTH_OPEN = (9, 30)
# Annualised regular-session trading minutes (252 × 390) for the remaining-time
# expected move; a target beyond this many σ is flagged unreachable.
_TRADING_MINUTES_YR = 252 * 390
REACH_SIGMA = 2.0


def _nearest_strike(rows, target: float) -> Optional[float]:
    strikes = [r.strike for r in rows if r.strike > 0]
    if not strikes:
        return None
    return min(strikes, key=lambda k: abs(k - target))


def _contract(rows, strike: float, side: str, c_spx: float, role: str,
              expiry: str) -> Optional[dict]:
    """Build one candidate SPX contract at ``strike`` for ``side`` (0DTE)."""
    row = next((r for r in rows if abs(r.strike - strike) < 1e-6), None)
    if row is None:
        return None
    if side == "CALL":
        ask, vol = row.call_ask, row.call_volume
        intrinsic = max(c_spx - strike, 0.0)          # value if price pins to C
        breakeven = strike + (ask or 0.0)
    else:
        ask, vol = row.put_ask, row.put_volume
        intrinsic = max(strike - c_spx, 0.0)
        breakeven = strike - (ask or 0.0)
    if not ask or ask <= 0:
        return None
    gain = (intrinsic - ask) / ask
    return {
        "role": role,
        "side": side,
        "strike": strike,
        "symbol": _spx_symbol(expiry, side, strike),
        "premium": round(ask, 2),
        "value_at_c": round(intrinsic, 2),
        "est_gain_pct": round(gain, 4),
        "breakeven": round(breakeven, 2),
        "volume": int(vol or 0),
    }


def _spx_symbol(expiry: str, side: str, strike: float) -> str:
    """OCC-style .SPXW reference symbol, e.g. .SPXW260922P7775."""
    try:
        y, m, d = expiry.split("-")
        ymd = f"{y[2:]}{m}{d}"
    except ValueError:
        ymd = expiry.replace("-", "")
    pc = "P" if side == "PUT" else "C"
    return f".SPXW{ymd}{pc}{int(round(strike))}"


def _atm_contract(spx_rows, spx_spot: float, c_spx: float, side: str,
                  expiry: str) -> Optional[dict]:
    """The single ATM contract (strike nearest spot) on ``side`` — the EOD-Pin
    ticket. Held to settlement, so ``value_at_c`` is its intrinsic if price pins
    to C and the est_gain is the whole trade."""
    strike = _nearest_strike(spx_rows, spx_spot)
    if strike is None:
        return None
    return _contract(spx_rows, strike, side, c_spx, "ATM · ride to C", expiry)


def eod_state(now_utc: Optional[dt.datetime] = None) -> dict:
    """Where we are in the regular session, in ET (DST- and holiday-aware).

    ``minutes_to_close`` counts down to 16:00 ET (13:00 on half-days); ``armed``
    is true only inside the final ``ARM_MINUTES`` while the session is open.
    """
    now = now_utc or dt.datetime.now(dt.timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=dt.timezone.utc)
    et = now.astimezone(_ET) if _ET else now
    d = et.date()
    trading = is_trading_day(d)
    close_h = close_hour_et(d)
    close = et.replace(hour=close_h, minute=0, second=0, microsecond=0)
    open_t = et.replace(hour=_RTH_OPEN[0], minute=_RTH_OPEN[1], second=0, microsecond=0)
    mtc = (close - et).total_seconds() / 60.0
    is_open = trading and open_t <= et <= close
    armed = bool(is_open and 0 <= mtc <= ARM_MINUTES)
    return {
        "trading_day": trading,
        "is_open": is_open,
        "pre_open": trading and et < open_t,
        "after_close": trading and et > close,
        "armed": armed,
        "minutes_to_close": round(mtc, 1) if trading else None,
        "arms_in_min": round(mtc - ARM_MINUTES, 1) if (is_open and mtc > ARM_MINUTES) else None,
        "close_et": f"{close_h}:00 ET" + (" (half day)" if is_early_close(d) else ""),
        "now_et": et.strftime("%H:%M ET"),
    }


def _remaining_em(spot: float, iv: Optional[float], minutes_left: Optional[float]) -> Optional[float]:
    """1σ move (points) from ATM IV over the minutes left to the close."""
    if not iv or iv <= 0 or spot <= 0 or not minutes_left or minutes_left <= 0:
        return None
    return round(spot * iv * math.sqrt(minutes_left / _TRADING_MINUTES_YR), 2)


def assemble_plan(res: NetDealerResult, spx_spot: float,
                  spx_rows, expiry: str, dte: float,
                  t_years: float, sessions: Optional[int],
                  min_ask: float = 0.10, min_vol: float = 500.0,
                  mode: str = "live", now: Optional[dt.datetime] = None) -> dict:
    """Compose the EOD-Pin ticket from levels computed natively on the SPX chain.

    ``res`` is a ``NetDealerResult`` computed on the live SPX 0DTE chain — its
    C, centroids and (volume-based) walls are already in SPX points, so there is
    no conversion. The trade only exists inside the final ``ARM_MINUTES``:
    ceiling + C below → ATM put, floor + C above → ATM call, ride to C.
    """
    eod = eod_state(now)

    # Everything is already in SPX points — no SPY, no ×10 basis.
    c_spx = res.c_target
    em = _remaining_em(spx_spot, res.atm_iv, eod["minutes_to_close"])
    lvl = {
        "spot": round(spx_spot, 2),
        "c": c_spx,
        "sweet_low": res.sweet_spot_low,
        "sweet_high": res.sweet_spot_high,
        "call_wall": res.call_wall,
        "put_wall": res.put_wall,
        "max_pain": res.max_pain,
        "long_avg": res.long_avg,
        "short_avg": res.short_avg,
        "em": em,
    }

    # The trade targets the C pin, so the SIDE follows C vs spot. Ceiling (C
    # below spot) → price pulled DOWN → ATM PUT; floor (C above spot) → ATM
    # CALL. Live volume skew is a *confirmation* only, surfaced below.
    if c_spx is not None:
        side = "PUT" if c_spx <= spx_spot else "CALL"
    else:
        side = "PUT" if res.volume_bias == "DOWN" else "CALL"
    action = "BUY ATM PUT" if side == "PUT" else "BUY ATM CALL"
    pin_dir = "DOWN" if side == "PUT" else "UP"          # direction of the pull to C

    # Edge = the gap from spot to the C pin (the intrinsic an ATM captures if
    # price pins). Always ≥ 0 now that the side follows C; a near-zero gap means
    # there is nothing to capture.
    edge = round(abs(spx_spot - c_spx), 2) if c_spx is not None else None

    # Strictly the ATM contract, held to the settlement print.
    atm = _atm_contract(spx_rows, spx_spot, c_spx, side, expiry) if c_spx is not None else None
    contracts = [atm] if atm else []

    reach_sigma = round(abs(edge) / em, 2) if (em and edge is not None) else None
    reachable = reach_sigma is not None and reach_sigma <= REACH_SIGMA
    reward = abs(edge) if edge is not None else None      # points to the pin
    payoff = round(atm["value_at_c"] - atm["premium"], 2) if atm else None

    # --- EOD-Pin gate: the trade is real only inside the arm window ---
    reasons: List[str] = []
    warnings: List[str] = []
    if edge is not None:
        where = ("spot at the ceiling, C below" if side == "PUT"
                 else "spot at the floor, C above")
        reasons.append(f"{where} — {edge} pt to the C pin → {action} to ride to C")
    # Live volume skew is a confirmation of the pin direction, not the selector.
    if res.volume_bias == pin_dir:
        reasons.append(f"Live SPX volume skewing {res.volume_bias} — confirms the pull to C")
    elif res.volume_bias:
        warnings.append(f"Live SPX volume skewing {res.volume_bias}, against the pin")
    if payoff is not None and payoff <= 0:
        warnings.append("A perfect pin to C still loses — the ATM premium exceeds "
                        "the gap to C; no edge here")
    if reach_sigma is not None:
        if reachable:
            reasons.append(f"C is {reach_sigma}× the 1σ move to close ({em} pt) — reachable")
        else:
            warnings.append(f"C is {reach_sigma}× the 1σ move to close ({em} pt) — "
                            f"may not reach C before the bell")

    # Signal is driven by the clock first, then the setup. The go/no-go test is
    # the payoff: even a perfect pin must beat the premium paid.
    if not eod["trading_day"]:
        signal = "MARKET CLOSED"
    elif eod["after_close"]:
        signal = "CLOSED"
    elif not eod["armed"]:
        signal = "WAIT"
        if eod["pre_open"]:
            reasons.insert(0, "Pre-market — the pin trade arms in the final "
                           f"{ARM_MINUTES} min of the session")
        elif eod["arms_in_min"] is not None:
            reasons.insert(0, f"Arms in {eod['arms_in_min']:.0f} min "
                           f"(final {ARM_MINUTES} min before {eod['close_et']})")
    elif not atm or payoff is None or payoff <= 0:
        signal = "STAND DOWN"
    else:
        signal = "TAKE"

    return {
        "asof": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "expiry": expiry, "dte": round(dte, 3), "sessions": sessions,
        "mode": mode,
        "signal": signal, "action": action, "side": side,
        "confidence": round(res.confidence or 0.0, 1),
        "reasons": reasons, "warnings": warnings,
        "eod": eod,
        "arm_minutes": ARM_MINUTES,
        "levels": lvl,
        "reachable": reachable, "reach_sigma": reach_sigma,
        "plan": {
            "entry": round(spx_spot, 2),
            "target": c_spx,                        # the pin
            "exit": f"C or {eod['close_et']} settlement — whichever first",
            "reward_pts": reward, "edge_pts": edge,
            "max_risk": "100% of premium (no stop)",
            "premium": atm["premium"] if atm else None,
            "payoff_at_c": payoff,                  # $ per contract-point if C prints
        },
        "contracts": contracts,
        "source": {
            "computed_on": "$SPX 0DTE volume", "volume_bias": res.volume_bias,
            "call_wall": res.call_wall, "put_wall": res.put_wall,
            "total_call_volume": res.total_call_volume,
            "total_put_volume": res.total_put_volume,
        },
    }
