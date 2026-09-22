"""0DTE SPX trade plan — turn the net-dealer map into an actionable ticket.

Why two symbols? Intraday, Schwab reports **zero open interest** for the SPX
same-day (`.SPXW`) options, so every OI-driven dealer signal (crush direction,
walls, max-pain, the pin) is blind on SPX itself. SPY carries real OI, so we
read the *map* off SPY — exactly what the framework's calculator screenshot
shows — and trade the *cash-settled SPX daily* against it. SPX ≈ SPY × 10 plus
a small, live "basis" (index vs ETF), so we align SPY's levels onto the live
SPX strike grid with ``spx = spy * 10 + basis`` before picking a contract.

This is a **trade plan / signal**, not an order router — the app is read-only
and never routes orders. It surfaces direction, entry / target / stop, and the
specific SPX contracts, with projected P&L if price pins to C.
"""
from __future__ import annotations

import datetime as dt
import math
from typing import List, Optional

from app.net_dealer import NetDealerResult
from app.scanner import rank_contracts

# SPX index vs SPY ETF: SPX quotes ~10× SPY, but not exactly (dividends / expense
# drag), so we measure the live basis instead of assuming a clean ×10.
SPX_MULT = 10.0

# Confidence gates for the GO / CAUTION / STAND DOWN banner.
GO_MIN = 70.0
CAUTION_MIN = 55.0


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


def _pick_contracts(spx_rows, spx_spot: float, c_spx: float, side: str,
                    expiry: str, min_ask: float, min_vol: float) -> List[dict]:
    """Three roles: Conviction (deep-delta ITM), Balanced (ATM), Leverage
    (cheapest strike that still finishes ITM at C — highest % if it works)."""
    # SPX 0DTE has ~zero OI, so rank on live VOLUME, not OI (min_oi=0).
    ranked = rank_contracts(spx_rows, c_spx, side, min_gain=0.0,
                            min_oi=0.0, min_ask=min_ask)
    ranked = [r for r in ranked if r["contract_volume"] >= min_vol] or ranked
    picks: List[dict] = []
    used = set()

    def add(strike: Optional[float], role: str):
        if strike is None or strike in used:
            return
        c = _contract(spx_rows, strike, side, c_spx, role, expiry)
        if c:
            picks.append(c)
            used.add(strike)

    # Balanced = the ATM strike (nearest to spot) — highest gamma, moves now.
    atm = _nearest_strike(spx_rows, spx_spot)
    # Conviction = deepest-ITM ranked strike with real volume (moves ~1:1, least
    # theta risk): for a PUT that's the highest strike, for a CALL the lowest.
    conviction = None
    if ranked:
        conviction = (max(ranked, key=lambda r: r["strike"])["strike"] if side == "PUT"
                      else min(ranked, key=lambda r: r["strike"])["strike"])
    # Leverage = best projected %-gain (cheapest that still finishes ITM at C).
    leverage = ranked[0]["strike"] if ranked else None

    add(conviction, "Conviction · deep ITM")
    add(atm, "Balanced · ATM")
    add(leverage, "Leverage · best %")
    # Keep at most three, in a sensible strike order for the chosen side.
    picks.sort(key=lambda p: p["strike"], reverse=(side == "PUT"))
    return picks[:3]


def _intraday_em(spot: float, iv: Optional[float], t_years: float) -> Optional[float]:
    """Live 1σ move in points from ATM IV over the time left to the close."""
    if not iv or iv <= 0 or spot <= 0:
        return None
    return round(spot * iv * math.sqrt(max(t_years, 1e-6)), 2)


def assemble_plan(spy: NetDealerResult, spy_spot: float,
                  spx_rows, spx_spot: float, expiry: str, dte: float,
                  t_years: float, sessions: Optional[int],
                  min_ask: float = 0.10, min_vol: float = 500.0,
                  mode: str = "live") -> dict:
    """Compose the SPX 0DTE trade ticket from the SPY dealer map + live SPX chain."""
    basis = round(spx_spot - spy_spot * SPX_MULT, 2)

    # Translate every SPY level onto the live SPX ladder.
    c_spx = to_spx(spy.c_target, basis)
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
        "em": _intraday_em(spx_spot, spy.atm_iv, t_years),
    }

    # Direction comes from the crush: DOWN crush → dealers pull price down → BUY
    # PUTS toward C; UP crush → BUY CALLS. Fall back to C-vs-spot if crush blank.
    crush = spy.crush_direction
    if crush not in ("DOWN", "UP") and c_spx is not None:
        crush = "UP" if c_spx > spx_spot else "DOWN"
    side = "PUT" if crush == "DOWN" else "CALL"
    action = "BUY PUTS" if side == "PUT" else "BUY CALLS"

    # Edge = how far price still has to travel to the C pin (positive = room).
    edge = None
    if c_spx is not None:
        edge = round((spx_spot - c_spx) if side == "PUT" else (c_spx - spx_spot), 2)

    contracts = _pick_contracts(spx_rows, spx_spot, c_spx, side, expiry,
                                min_ask, min_vol) if c_spx is not None else []

    # Trade levels (SPX points). T1 = the C pin; T2 = the far edge of the crush
    # (sweet-spot) zone; stop = a reclaim through the wall that should hold.
    if side == "PUT":
        t1, t2 = c_spx, lvl["sweet_low"]
        stop = lvl["call_wall"]
    else:
        t1, t2 = c_spx, lvl["sweet_high"]
        stop = lvl["put_wall"]
    reward = round(abs(spx_spot - t1), 2) if t1 is not None else None
    risk = round(abs((stop if stop is not None else spx_spot) - spx_spot), 2)
    rr = round(reward / risk, 2) if reward and risk else None

    # --- GO / CAUTION / STAND DOWN gate ---
    conf = spy.confidence or 0.0
    reasons: List[str] = []
    warnings: List[str] = []
    if spy.crush_direction:
        reasons.append(f"Crush {spy.crush_direction} — "
                       f"{spy.expensive_side or '?'}s expensive, dealers pull toward C")
    if spy.volume_agree:
        reasons.append(f"Live volume skew agrees ({spy.volume_bias})")
    elif spy.volume_agree is False:
        warnings.append(f"Volume skew disagrees ({spy.volume_bias}) with the crush")
    if spy.direction_agree is False:
        warnings.append("Crush direction and C-vs-spot disagree")
    if edge is not None and edge <= 0:
        warnings.append("Price already at/through C — the move has largely played out")
    if not contracts:
        warnings.append("No liquid SPX contract finishes ITM at C on this side")
    if lvl["em"] and reward and reward > 2.0 * lvl["em"]:
        warnings.append(f"Target is {reward/lvl['em']:.1f}× the 1σ move ({lvl['em']} pt) — a stretch")
    else:
        if lvl["em"] and reward:
            reasons.append(f"C is within reach — {reward/lvl['em']:.1f}× the 1σ move")

    hard_block = (edge is not None and edge <= 0) or not contracts
    if hard_block or conf < CAUTION_MIN:
        signal = "STAND DOWN"
    elif conf >= GO_MIN and not warnings:
        signal = "GO"
    else:
        signal = "CAUTION"

    return {
        "asof": dt.datetime.utcnow().isoformat(timespec="seconds") + "Z",
        "expiry": expiry, "dte": round(dte, 3), "sessions": sessions,
        "mode": mode,
        "signal": signal, "action": action, "side": side,
        "confidence": round(conf, 1),
        "reasons": reasons, "warnings": warnings,
        "levels": lvl,
        "plan": {
            "entry": round(spx_spot, 2),
            "target1": t1, "target2": t2, "stop": stop,
            "reward_pts": reward, "risk_pts": risk, "rr": rr, "edge_pts": edge,
            "premium_stop_pct": 50,
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
