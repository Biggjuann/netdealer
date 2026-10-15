"""IV-wall credit-spread monitor.

The strategy (per ticker, with its IV walls supplied by the user):

  * PUT credit spread (bullish) — armed when price drops to the LOWER (put) IV
    wall. Build a ``width``-wide spread with the LONG (lower) strike ~ the bottom
    of the put wall, SHORT (higher) strike = long + width (lands near the wall's
    "true IV"). Collect credit; ENTER when the net credit reaches ``enter_credit``.
    Stop if price CLOSES below the wall. Take profit when the spread decays to
    ``take_profit``. Always ``contracts`` contracts.
  * CALL credit spread (bearish) — mirror at the UPPER (call) IV wall: LONG
    (upper) strike ~ the top of the call wall, SHORT = long − width. Stop if price
    closes ABOVE the wall.

This is a read-only monitor: it finds the legs, prices them off the live chain,
tracks the credit, and raises WAIT / WATCH / ENTER / STOP signals. It never
routes orders. Credit is estimated from chain marks; a live broker quote differs.
"""
from __future__ import annotations

from typing import List, Optional

from app.net_dealer import StrikeRow


def session_flags(is_open: bool, minutes_to_close: Optional[float],
                  force_close_min: int) -> dict:
    """RTH + force-close flags: trades are RTH only and closed ``force_close_min``
    before the session close (5 → 2:55pm CT / 3:55pm ET)."""
    is_rth = bool(is_open)
    force_close = bool(is_rth and minutes_to_close is not None
                       and minutes_to_close <= force_close_min)
    mtfc = (round(minutes_to_close - force_close_min, 1)
            if (is_rth and minutes_to_close is not None) else None)
    return {"is_rth": is_rth, "force_close": force_close,
            "minutes_to_force_close": mtfc, "force_close_min": force_close_min}


def _nearest_strike(rows: List[StrikeRow], target: float) -> Optional[float]:
    ks = [r.strike for r in rows if r.strike > 0]
    return min(ks, key=lambda k: abs(k - target)) if ks else None


def _leg_price(rows: List[StrikeRow], strike: Optional[float], is_put: bool) -> Optional[float]:
    if strike is None:
        return None
    row = next((r for r in rows if abs(r.strike - strike) < 1e-6), None)
    if row is None:
        return None
    px = row.put_ask if is_put else row.call_ask
    return px if (px and px > 0) else None


def build_spread(rows: List[StrikeRow], spot: float, side: str,
                 wall_low: float, wall_high: float,
                 width: float, enter_credit: float, take_profit: float,
                 contracts: int, entered: bool = False,
                 candle_close: Optional[float] = None) -> dict:
    """Assemble one credit spread (side = 'PUT' or 'CALL') against its IV wall.

    ``candle_close`` is the latest completed 15-minute candle close; the STOP
    fires on that close beyond the wall (per the rule), not an intraday touch.
    Falls back to live ``spot`` when no candle is available.
    """
    is_put = side == "PUT"
    close = candle_close if candle_close is not None else spot
    if is_put:
        long_strike = _nearest_strike(rows, wall_low)                 # outer (lower)
        short_strike = _nearest_strike(rows, (long_strike + width)) if long_strike is not None else None
        in_zone = spot <= wall_high                                   # price reached the wall
        broke = close < wall_low                                      # 15m close below → stop
    else:
        long_strike = _nearest_strike(rows, wall_high)                # outer (upper)
        short_strike = _nearest_strike(rows, (long_strike - width)) if long_strike is not None else None
        in_zone = spot >= wall_low
        broke = close > wall_high                                     # 15m close above → stop

    long_px = _leg_price(rows, long_strike, is_put)
    short_px = _leg_price(rows, short_strike, is_put)
    actual_width = round(abs(short_strike - long_strike), 2) if (long_strike is not None and short_strike is not None) else None
    credit = round(short_px - long_px, 2) if (long_px is not None and short_px is not None) else None
    tp_hit = credit is not None and credit <= take_profit

    # Signal: clock/price first, then the credit trigger. STOP dominates (wall gone).
    if long_strike is None or short_strike is None or credit is None or actual_width in (None, 0):
        state = "n/a"
    elif broke:
        state = "STOP" if entered else "AVOID"       # wall broken
    elif entered and tp_hit:
        state = "TAKE PROFIT"
    elif not in_zone:
        state = "WAIT"                                # price not at the wall yet
    elif credit >= enter_credit:
        state = "ENTER" if not entered else ("MANAGE")
    else:
        state = "WATCH"                               # at the wall, credit building

    # Economics at the planned $1.00 entry (per spread → ×100 ×contracts).
    max_risk = round((width - enter_credit) * 100 * contracts, 2)
    max_profit = round((enter_credit - take_profit) * 100 * contracts, 2)
    # Distance of spot to the wall edge that matters (negative once past it).
    if is_put:
        to_wall_pct = round((spot - wall_high) / spot, 4) if spot else None   # >0 = above wall
        stop_level = wall_low
    else:
        to_wall_pct = round((wall_low - spot) / spot, 4) if spot else None
        stop_level = wall_high

    return {
        "side": side,
        "long_strike": long_strike, "short_strike": short_strike,
        "width": actual_width,
        "long_price": long_px, "short_price": short_px,
        "credit": credit,
        "in_zone": in_zone, "broke": broke, "tp_hit": tp_hit,
        "candle_close": round(close, 2) if close is not None else None,
        "state": state,
        "enter_credit": enter_credit, "take_profit": take_profit,
        "contracts": contracts,
        "stop_level": stop_level, "to_wall_pct": to_wall_pct,
        "max_risk": max_risk, "max_profit": max_profit,
        "wall_low": wall_low, "wall_high": wall_high,
    }


def evaluate_ticker(rows: List[StrikeRow], spot: float,
                    cw_low: float, cw_high: float,
                    pw_low: float, pw_high: float,
                    width: float, enter_credit: float, take_profit: float,
                    contracts: int, candle_close: Optional[float] = None) -> dict:
    """Both spreads for one ticker from its chain + IV walls."""
    return {
        "put_spread": build_spread(rows, spot, "PUT", pw_low, pw_high,
                                   width, enter_credit, take_profit, contracts,
                                   candle_close=candle_close),
        "call_spread": build_spread(rows, spot, "CALL", cw_low, cw_high,
                                    width, enter_credit, take_profit, contracts,
                                    candle_close=candle_close),
    }
