"""0DTE SPX trade plan — the end-of-day "pin to C" ticket.

Why two symbols? Intraday, Schwab reports **zero open interest** for the SPX
same-day (`.SPXW`) options, so every OI-driven dealer signal (crush direction,
walls, max-pain, the pin) is blind on SPX itself. SPY carries real OI, so we
read the *map* off SPY — exactly what the framework's calculator screenshot
shows — and trade the *cash-settled SPX daily* against it. SPX ≈ SPY × 10 plus
a small, live "basis" (index vs ETF), so we align SPY's levels onto the live
SPX strike grid with ``spx = spy * 10 + basis`` before picking a contract.

**The rule (EOD Pin).** Only inside the final ``ARM_MINUTES`` of the regular
session: if SPY is pinned to the ceiling with C below → **buy the ATM PUT**; if
it's on the floor with C above → **buy the ATM CALL**; hold to the 4:00pm cash
settlement so price prints at C. Strictly ATM, no stop — max risk is the whole
premium if the pin misses, so the arm window and reachability read are the
guardrails. Outside the window the ticket is disarmed (WAIT / CLOSED).

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

# SPX index vs SPY ETF: SPX quotes ~10× SPY, but not exactly (dividends / expense
# drag), so we measure the live basis instead of assuming a clean ×10.
SPX_MULT = 10.0

# EOD Pin: only arm inside the final N minutes of the regular session; the pin
# to C completes into the cash settlement. Regular open is 9:30 ET.
ARM_MINUTES = 15
_RTH_OPEN = (9, 30)
# Annualised regular-session trading minutes (252 × 390) for the remaining-time
# expected move; a target beyond this many σ is flagged unreachable.
_TRADING_MINUTES_YR = 252 * 390
REACH_SIGMA = 2.0


def to_spx(spy_level: Optional[float], basis: float) -> Optional[float]:
    """Map a SPY price level onto the live SPX ladder."""
    if spy_level is None:
        return None
    return round(spy_level * SPX_MULT + basis, 2)


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
    return _contract(spx_rows, strike, side, c_spx, "ATM · held to close", expiry)


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


def assemble_plan(spy: NetDealerResult, spy_spot: float,
                  spx_rows, spx_spot: float, expiry: str, dte: float,
                  t_years: float, sessions: Optional[int],
                  min_ask: float = 0.10, min_vol: float = 500.0,
                  mode: str = "live", now: Optional[dt.datetime] = None) -> dict:
    """Compose the EOD-Pin ticket from the SPY dealer map + live SPX chain.

    The trade only exists inside the final ``ARM_MINUTES``: ceiling + C below →
    ATM put, floor + C above → ATM call, held to the cash settlement.
    """
    eod = eod_state(now)
    basis = round(spx_spot - spy_spot * SPX_MULT, 2)

    # Translate every SPY level onto the live SPX ladder.
    c_spx = to_spx(spy.c_target, basis)
    em = _remaining_em(spx_spot, spy.atm_iv, eod["minutes_to_close"])
    lvl = {
        "spot": round(spx_spot, 2),
        "c": c_spx,
        "sweet_low": to_spx(spy.sweet_spot_low, basis),
        "sweet_high": to_spx(spy.sweet_spot_high, basis),
        "call_wall": to_spx(spy.call_wall, basis),
        "put_wall": to_spx(spy.put_wall, basis),
        "max_pain": to_spx(spy.max_pain, basis),
        "long_avg": to_spx(spy.long_avg, basis),
        "short_avg": to_spx(spy.short_avg, basis),
        "basis": basis,
        "em": em,
    }

    # Direction from the crush: DOWN crush (calls expensive, price at the ceiling)
    # → dealers pull down → ATM PUT; UP crush (floor) → ATM CALL. Fall back to
    # C-vs-spot if the crush read is blank.
    crush = spy.crush_direction
    if crush not in ("DOWN", "UP") and c_spx is not None:
        crush = "UP" if c_spx > spx_spot else "DOWN"
    side = "PUT" if crush == "DOWN" else "CALL"
    action = "BUY ATM PUT" if side == "PUT" else "BUY ATM CALL"

    # Edge = points from spot to the C pin in the trade's favour (positive = room
    # to run). For a put that's ceiling→C (spot above C); for a call floor→C.
    edge = None
    if c_spx is not None:
        edge = round((spx_spot - c_spx) if side == "PUT" else (c_spx - spx_spot), 2)

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
    if spy.crush_direction:
        reasons.append(f"Crush {spy.crush_direction} — "
                       f"{spy.expensive_side or '?'}s expensive; "
                       f"{'ceiling, C below' if side == 'PUT' else 'floor, C above'}")
    if spy.volume_agree:
        reasons.append(f"Live volume skew agrees ({spy.volume_bias})")
    elif spy.volume_agree is False:
        warnings.append(f"Volume skew disagrees ({spy.volume_bias}) with the crush")
    if spy.direction_agree is False:
        warnings.append("Crush direction and C-vs-spot disagree")
    if edge is not None and edge <= 0:
        warnings.append("Price already at/through C — no room left to pin")
    if reach_sigma is not None:
        if reachable:
            reasons.append(f"C is {reach_sigma}× the 1σ move to close ({em} pt) — reachable")
        else:
            warnings.append(f"C is {reach_sigma}× the 1σ move to close ({em} pt) — "
                            f"unlikely to fully pin; ATM can decay to $0")

    # Signal is driven by the clock first, then the setup.
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
    elif edge is None or edge <= 0 or not atm:
        signal = "STAND DOWN"
    else:
        signal = "TAKE"

    return {
        "asof": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "expiry": expiry, "dte": round(dte, 3), "sessions": sessions,
        "mode": mode,
        "signal": signal, "action": action, "side": side,
        "confidence": round(spy.confidence or 0.0, 1),
        "reasons": reasons, "warnings": warnings,
        "eod": eod,
        "arm_minutes": ARM_MINUTES,
        "levels": lvl,
        "reachable": reachable, "reach_sigma": reach_sigma,
        "plan": {
            "entry": round(spx_spot, 2),
            "target": c_spx,                        # the pin
            "exit": f"settle at {eod['close_et']}",  # held to cash settlement
            "reward_pts": reward, "edge_pts": edge,
            "max_risk": "100% of premium (no stop)",
            "premium": atm["premium"] if atm else None,
            "payoff_at_c": payoff,                  # $ per contract-point if C prints
        },
        "contracts": contracts,
        "spy": {
            "spot": round(spy_spot, 2),
            "c": spy.c_target,
            "crush_direction": spy.crush_direction,
            "expensive_side": spy.expensive_side,
            "volume_bias": spy.volume_bias,
            "call_wall": spy.call_wall, "put_wall": spy.put_wall,
            "sweet_low": spy.sweet_spot_low, "sweet_high": spy.sweet_spot_high,
            "confidence": spy.confidence,
        },
    }
