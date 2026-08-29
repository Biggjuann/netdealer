"""Net Dealer Target ("C") engine.

This module reproduces the *concept* behind the "Wave" net-dealer framework
(the annotated $MU screenshot): a single price — the **net dealer target "C"** —
where the aggregate option book is delta-balanced and dealers, who are the
counterparty to that book, are directionally flat. That price is where dealer
hedging pressure nets to zero, i.e. the level a pinning market is drawn toward
("follow the dealer").

Methodology (fully transparent — no proprietary data required, just the option
chain the shared Schwab token already gives us):

  For a candidate underlying price ``P`` we compute the **net dealer directional
  exposure** of the open-interest book:

      netDelta(P) = 100 * Σ_K [ Δcall(P,K)·OI_call(K) + Δput(P,K)·OI_put(K) ]

  where Δcall ∈ [0,1] and Δput = Δcall − 1 ∈ [−1,0] are Black-Scholes deltas
  computed at the candidate price ``P`` using each strike's implied vol.

  * As ``P`` rises, every call delta → 1 and every put delta → 0, so netDelta
    → +ΣOI_call  (the book is net-long upside — dealers are net-short, they want
    price DOWN).  This matches the screenshot's ``netDIR`` reading ``+46%`` up at
    the 990 strike.
  * As ``P`` falls, call deltas → 0 and put deltas → −1, so netDelta → −ΣOI_put
    (dealers are net-long, they want price UP).  Matches ``-45%`` down at 760.

  netDelta(P) is strictly increasing in ``P`` (both call and put deltas rise with
  price and the OI weights are non-negative), so it has a single zero crossing.

      C  =  the price P* where netDelta(P*) = 0.

  That is the delta-neutral / net-dealer-flat price — the "C" target.  In the
  screenshot ``netDIR`` crosses from +0.4% (845 strike) to −0.5% (840 strike);
  interpolating the crossing lands on 840.72, exactly the printed "C" target.

Supporting levels computed alongside C (all from the same chain):

  * ``max_pain``      — classic min-total-intrinsic-payout strike (corroborates C).
  * ``call_wall``     — strike carrying the most call OI (upper "exposure" flip:
                        above it dealers are heavily short → downward pull).
  * ``put_wall``      — strike carrying the most put OI (lower flip: below it
                        dealers are heavily long → upward pull).
  * ``long_avg``      — call-OI-weighted average strike (the "LongAvg" line).
  * ``short_avg``     — put-OI-weighted average strike (the "ShortAvg" line).

Everything here is pure math over a list of ``StrikeRow`` values, so it is unit
tested with synthetic chains and never needs the network.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Dict, List, Optional

_SQRT2 = math.sqrt(2.0)
# Used when a strike has no listed implied vol; a neutral mid-range IV.
DEFAULT_IV = 0.40
# Annual risk-free rate used in the Black-Scholes delta (small; C barely moves
# with it, but keep it explicit and configurable).
DEFAULT_RATE = 0.043
# Floor on time-to-expiry (years) ~= 1 hour, so 0DTE deltas stay well-defined
# instead of collapsing to a hard step exactly at expiry.
_MIN_T = 1.0 / (365.0 * 24.0)


def norm_cdf(x: float) -> float:
    """Standard-normal CDF via the error function."""
    return 0.5 * (1.0 + math.erf(x / _SQRT2))


def bs_call_delta(S: float, K: float, T: float, sigma: float, r: float = DEFAULT_RATE) -> float:
    """Black-Scholes call delta N(d1). Robust to degenerate inputs."""
    if S <= 0 or K <= 0:
        return 0.0
    if T <= 0 or sigma <= 0:
        # At/after expiry the delta is a step function of moneyness.
        return 1.0 if S > K else (0.5 if S == K else 0.0)
    d1 = (math.log(S / K) + (r + 0.5 * sigma * sigma) * T) / (sigma * math.sqrt(T))
    return norm_cdf(d1)


def bs_put_delta(S: float, K: float, T: float, sigma: float, r: float = DEFAULT_RATE) -> float:
    """Black-Scholes put delta = call delta − 1  (∈ [−1, 0])."""
    return bs_call_delta(S, K, T, sigma, r) - 1.0


def bs_price(S: float, K: float, T: float, sigma: float, is_call: bool,
             r: float = DEFAULT_RATE) -> float:
    """Black-Scholes option price. Falls back to intrinsic at/near expiry."""
    if S <= 0 or K <= 0:
        return 0.0
    if T <= 0 or sigma <= 0:
        return max(S - K, 0.0) if is_call else max(K - S, 0.0)
    sqrtT = sigma * math.sqrt(T)
    d1 = (math.log(S / K) + (r + 0.5 * sigma * sigma) * T) / sqrtT
    d2 = d1 - sqrtT
    disc = math.exp(-r * T)
    if is_call:
        return S * norm_cdf(d1) - K * disc * norm_cdf(d2)
    return K * disc * norm_cdf(-d2) - S * norm_cdf(-d1)


@dataclass
class StrikeRow:
    """One strike's call + put open interest / vol / iv, merged across the two maps."""

    strike: float
    call_oi: float = 0.0
    put_oi: float = 0.0
    call_iv: float = 0.0          # implied vol as a decimal (0.35 == 35%)
    put_iv: float = 0.0
    call_volume: float = 0.0
    put_volume: float = 0.0
    call_ask: float = 0.0
    put_ask: float = 0.0

    def call_sigma(self, fallback: float = DEFAULT_IV) -> float:
        return self.call_iv if self.call_iv and self.call_iv > 0 else fallback

    def put_sigma(self, fallback: float = DEFAULT_IV) -> float:
        return self.put_iv if self.put_iv and self.put_iv > 0 else fallback


@dataclass
class NetDealerResult:
    ticker: str
    expiry: str
    spot: Optional[float]
    dte: float                      # calendar days to expiry
    c_target: Optional[float]       # the net-dealer "C" price
    c_netdir: Optional[float]       # residual netDIR at C (≈ 0)
    max_pain: Optional[float]
    call_wall: Optional[float]
    put_wall: Optional[float]
    long_avg: Optional[float]       # call-OI-weighted strike
    short_avg: Optional[float]      # put-OI-weighted strike
    total_call_oi: float
    total_put_oi: float
    # --- crush / zone / pin signals (the creator's "how it trades" logic) ---
    call_crush_pct: Optional[float] = None   # share of call OI that is OTM
    put_crush_pct: Optional[float] = None     # share of put OI that is OTM
    # --- volume skew: where LIVE activity is entering vs the stale OI ---
    total_call_volume: float = 0.0
    total_put_volume: float = 0.0
    volume_skew: Optional[float] = None       # call-vol share − call-OI share (+ = call-side building)
    volume_skew_side: Optional[str] = None    # CALL / PUT — side volume is skewing toward vs OI
    volume_bias: Optional[str] = None         # DOWN / UP — call-side skew → pullback (down)
    volume_agree: Optional[bool] = None       # volume bias agrees with the crush direction
    expensive_side: Optional[str] = None      # CALL / PUT — where the OTM premium sits
    crush_direction: Optional[str] = None     # DOWN / UP — where dealers push price
    trade_side: Optional[str] = None          # CALL / PUT to buy toward C
    direction_agree: Optional[bool] = None    # crush direction agrees with C vs spot
    sweet_spot_low: Optional[float] = None    # crush-zone floor (≈ C)
    sweet_spot_high: Optional[float] = None   # crush-zone ceiling (≈ ShortAvg)
    pin_steepness: Optional[float] = None      # 0..1, how decisive the netDIR crossing is
    # --- IV expected-move (forward-looking σ bands from ATM implied vol) ---
    atm_iv: Optional[float] = None             # at-the-money implied vol (decimal)
    expected_move: Optional[float] = None      # 1σ move in $ to expiry
    expected_move_pct: Optional[float] = None  # 1σ as a share of spot
    em_low: Optional[float] = None             # spot − 1σ
    em_high: Optional[float] = None            # spot + 1σ
    em_low_2: Optional[float] = None           # spot − 2σ
    em_high_2: Optional[float] = None          # spot + 2σ
    c_sigma: Optional[float] = None            # |C − spot| in σ units
    iv_reach: Optional[str] = None             # within-1sig / within-2sig / beyond-2sig
    confidence: Optional[float] = None         # 0..100 chain-based setup confidence
    confidence_breakdown: Optional[dict] = None
    rows: List[dict] = field(default_factory=list)
    note: str = ""


# Confidence weights (sum to 1.0). range/iv reachability + liquidity default to
# neutral when unknown (e.g. the calculator's chain-only base score).
_CONF_W = {"direction": 0.26, "range": 0.16, "iv": 0.13, "fuel": 0.18,
           "steepness": 0.15, "liquidity": 0.12}
# A netDIR jump of this size across the strikes bracketing C reads as a fully
# decisive pin (steepness = 1.0).
PIN_STEEP_REF = 0.12
# Contract OI at/above this reads as full liquidity for the score.
LIQ_REF = 2000.0


def _reach_component(reachability: Optional[str]) -> float:
    return {"within-mean": 1.0, "within-max": 0.6, "out-of-range": 0.1,
            "unknown": 0.5, None: 0.5}.get(reachability, 0.5)


def _iv_reach_component(iv_reach: Optional[str]) -> float:
    return {"within-1sig": 1.0, "within-2sig": 0.55, "beyond-2sig": 0.1,
            "unknown": 0.5, None: 0.5}.get(iv_reach, 0.5)


def setup_confidence(direction_agree: Optional[bool], expensive_crush_pct: Optional[float],
                     pin_steepness: Optional[float], range_reach: Optional[str] = None,
                     iv_reach: Optional[str] = None, liquidity_oi: Optional[float] = None,
                     volume_agree: Optional[bool] = None):
    """Blend the creator's confirmations into a 0..100 setup score.

    * direction  — does the crush (expensive-side) direction agree with C vs spot?
                   Penalised when live VOLUME is entering against that thesis.
    * range      — is the move to C within the 30d realized range? (neutral if unknown)
    * iv         — is C within the IV-implied expected move (±σ)? (neutral if unknown)
    * fuel       — how much of the expensive side's OI is OTM premium to crush.
    * steepness  — how decisive the netDIR zero-crossing is (sharp pin vs drift).
    * liquidity  — is the target contract liquid? (neutral if unknown)
    range + iv are two independent reachability reads; strongest when they agree.
    Returns (score 0..100, breakdown dict of each 0..1 component).
    """
    direction = 1.0 if direction_agree else (0.15 if direction_agree is False else 0.5)
    if volume_agree is False:
        direction *= 0.75    # new session volume is skewing against the crush thesis
    fuel = 0.0
    if expensive_crush_pct is not None:
        fuel = max(0.0, min(1.0, (expensive_crush_pct - 0.40) / 0.50))
    steep = max(0.0, min(1.0, pin_steepness if pin_steepness is not None else 0.0))
    liq = 0.5 if liquidity_oi is None else max(0.0, min(1.0, liquidity_oi / LIQ_REF))
    comp = {"direction": direction, "range": _reach_component(range_reach),
            "iv": _iv_reach_component(iv_reach), "fuel": fuel,
            "steepness": steep, "liquidity": liq}
    score = 100.0 * sum(_CONF_W[k] * comp[k] for k in _CONF_W)
    return round(score, 1), {k: round(v, 3) for k, v in comp.items()}


def atm_iv(rows: List[StrikeRow], spot: float) -> Optional[float]:
    """Implied vol at the strike closest to spot (mean of call/put IV present)."""
    best, best_d = None, float("inf")
    for row in rows:
        d = abs(row.strike - spot)
        ivs = [x for x in (row.call_iv, row.put_iv) if x and x > 0]
        if ivs and d < best_d:
            best_d, best = d, sum(ivs) / len(ivs)
    return best


def expected_move(spot: Optional[float], iv: Optional[float], t_years: float) -> Optional[float]:
    """1σ expected move to expiry (dollars): spot × IV × √T."""
    if not spot or not iv or t_years <= 0:
        return None
    return spot * iv * math.sqrt(t_years)


def _eff_call(row: StrikeRow, w: float) -> float:
    """Effective call inventory = OI (known) + w × Volume (dynamic)."""
    return row.call_oi + w * row.call_volume


def _eff_put(row: StrikeRow, w: float) -> float:
    return row.put_oi + w * row.put_volume


def build_weights(rows: List[StrikeRow], volume_weight: float = 0.0,
                  blend_mode: str = "additive", alpha: float = 0.5) -> List[tuple]:
    """Per-strike (call_weight, put_weight) effective inventory, aligned to ``rows``.

    * **additive**   — OI + volume_weight × Volume (raw counts). Simple, but for
      names where Volume ≫ OI the flow can swamp the standing inventory.
    * **normalized** — a scale-invariant blend of the OI and Volume *distributions*:
      ``(1-α)·share_of_OI + α·share_of_Volume`` (each normalised by its own total
      across the whole book). Volume gets an equal vote regardless of whether it
      dwarfs or trails OI, so C behaves the same across tickers and through the
      session. Falls back to OI-only when there is no volume yet (pre-market).
    """
    if blend_mode == "normalized":
        t_oi = sum(r.call_oi + r.put_oi for r in rows)
        t_vol = sum(r.call_volume + r.put_volume for r in rows)
        if t_oi > 0 and t_vol > 0:
            return [((1 - alpha) * (r.call_oi / t_oi) + alpha * (r.call_volume / t_vol),
                     (1 - alpha) * (r.put_oi / t_oi) + alpha * (r.put_volume / t_vol))
                    for r in rows]
        return [(r.call_oi, r.put_oi) for r in rows]   # no volume/OI → OI-only
    return [(_eff_call(r, volume_weight), _eff_put(r, volume_weight)) for r in rows]


def net_delta_at(price: float, rows: List[StrikeRow], t_years: float,
                 r: float = DEFAULT_RATE, fallback_iv: float = DEFAULT_IV,
                 volume_weight: float = 0.0, weights: Optional[List[tuple]] = None) -> float:
    """Aggregate net dealer directional exposure at ``price``.

    Weighted by **effective inventory** (see ``build_weights``). Pass a precomputed
    ``weights`` list to use a specific blend; otherwise falls back to additive
    OI + volume_weight × Volume.

    Positive → book is net-long upside (dealers net-short → want price down).
    Negative → book is net-long downside (dealers net-long → want price up).
    """
    T = max(t_years, _MIN_T)
    if weights is None:
        weights = [(_eff_call(row, volume_weight), _eff_put(row, volume_weight)) for row in rows]
    total = 0.0
    for row, (cw, pw) in zip(rows, weights):
        if cw:
            total += bs_call_delta(price, row.strike, T, row.call_sigma(fallback_iv), r) * cw
        if pw:
            total += bs_put_delta(price, row.strike, T, row.put_sigma(fallback_iv), r) * pw
    return total * 100.0


def netdir_pct_at(price: float, rows: List[StrikeRow], t_years: float,
                  r: float = DEFAULT_RATE, fallback_iv: float = DEFAULT_IV,
                  volume_weight: float = 0.0, weights: Optional[List[tuple]] = None) -> float:
    """``net_delta_at`` normalised to [-1, 1] by total effective inventory."""
    if weights is None:
        weights = [(_eff_call(row, volume_weight), _eff_put(row, volume_weight)) for row in rows]
    total = sum(cw + pw for cw, pw in weights)
    if total <= 0:
        return 0.0
    return net_delta_at(price, rows, t_years, r, fallback_iv, weights=weights) / (total * 100.0)


def find_c_target(rows: List[StrikeRow], t_years: float, r: float = DEFAULT_RATE,
                  fallback_iv: float = DEFAULT_IV, iters: int = 60,
                  volume_weight: float = 0.0, weights: Optional[List[tuple]] = None) -> Optional[float]:
    """Bisect the (monotone-increasing) netDelta curve for its zero crossing."""
    if weights is None:
        weights = [(_eff_call(row, volume_weight), _eff_put(row, volume_weight)) for row in rows]
    strikes = [row.strike for row, (cw, pw) in zip(rows, weights) if (cw or pw)]
    if len(strikes) < 2:
        return None
    lo, hi = min(strikes), max(strikes)
    # Widen slightly so a crossing just outside the listed range is still found.
    span = hi - lo
    lo -= 0.5 * span
    hi += 0.5 * span
    f_lo = net_delta_at(lo, rows, t_years, r, fallback_iv, weights=weights)
    f_hi = net_delta_at(hi, rows, t_years, r, fallback_iv, weights=weights)
    if f_lo > 0:          # dealers net-short across the whole range → C at/below floor
        return round(min(strikes), 2)
    if f_hi < 0:          # dealers net-long across the whole range → C at/above cap
        return round(max(strikes), 2)
    for _ in range(iters):
        mid = 0.5 * (lo + hi)
        if net_delta_at(mid, rows, t_years, r, fallback_iv, weights=weights) < 0:
            lo = mid
        else:
            hi = mid
    return round(0.5 * (lo + hi), 2)


def max_pain(rows: List[StrikeRow]) -> Optional[float]:
    """Strike that minimises total intrinsic value paid out to option holders."""
    candidates = [row.strike for row in rows if (row.call_oi or row.put_oi)]
    if not candidates:
        return None
    best_strike, best_cost = None, None
    for settle in candidates:
        cost = 0.0
        for row in rows:
            if row.call_oi and settle > row.strike:
                cost += (settle - row.strike) * row.call_oi
            if row.put_oi and settle < row.strike:
                cost += (row.strike - settle) * row.put_oi
        if best_cost is None or cost < best_cost:
            best_cost, best_strike = cost, settle
    return best_strike


def _oi_weighted_strike(rows: List[StrikeRow], side: str, volume_weight: float = 0.0,
                        weights: Optional[List[tuple]] = None) -> Optional[float]:
    """Effective-inventory-weighted average strike (the Long/Short-Avg centroid)."""
    if weights is None:
        weights = [(_eff_call(row, volume_weight), _eff_put(row, volume_weight)) for row in rows]
    num = den = 0.0
    for row, (cw, pw) in zip(rows, weights):
        w = cw if side == "call" else pw
        num += w * row.strike
        den += w
    return round(num / den, 2) if den > 0 else None


def _peak_oi_strike(rows: List[StrikeRow], side: str) -> Optional[float]:
    best, best_oi = None, -1.0
    for row in rows:
        oi = row.call_oi if side == "call" else row.put_oi
        if oi > best_oi:
            best_oi, best = oi, row.strike
    return best


def compute(ticker: str, expiry: str, rows: List[StrikeRow], spot: Optional[float],
            t_years: float, dte: float, r: float = DEFAULT_RATE,
            fallback_iv: float = DEFAULT_IV, volume_weight: float = 0.0,
            blend_mode: str = "additive", blend_alpha: float = 0.5) -> NetDealerResult:
    """Assemble the full net-dealer picture from a merged strike table.

    netDIR / C / the inventory centroids are weighted by effective inventory that
    blends stale OI with live Volume — see ``build_weights`` (additive vs normalized).
    """
    rows = sorted(rows, key=lambda x: x.strike)
    weights = build_weights(rows, volume_weight, blend_mode, blend_alpha)
    total_call_oi = sum(r_.call_oi for r_ in rows)
    total_put_oi = sum(r_.put_oi for r_ in rows)

    c_target = find_c_target(rows, t_years, r, fallback_iv, weights=weights)
    c_netdir = (round(netdir_pct_at(c_target, rows, t_years, r, fallback_iv, weights=weights), 4)
                if c_target is not None else None)

    per_strike: List[dict] = []
    for row in rows:
        per_strike.append({
            "strike": row.strike,
            "call_oi": row.call_oi,
            "put_oi": row.put_oi,
            "call_volume": row.call_volume,
            "put_volume": row.put_volume,
            "call_iv": round(row.call_iv, 4) if row.call_iv else None,
            "put_iv": round(row.put_iv, 4) if row.put_iv else None,
            "call_ask": row.call_ask,
            "put_ask": row.put_ask,
            # netDIR evaluated at this strike's price (the screenshot's column).
            "netdir": round(netdir_pct_at(row.strike, rows, t_years, r, fallback_iv, weights=weights), 4),
        })

    note = ""
    if not rows:
        note = "No option chain returned for this ticker/expiry."
    elif total_call_oi + total_put_oi == 0:
        note = "Chain has no open interest yet (pre-market or brand-new expiry)."

    short_avg = _oi_weighted_strike(rows, "put", weights=weights)
    sig = _crush_signals(rows, spot, c_target, short_avg, per_strike, t_years)

    return NetDealerResult(
        ticker=ticker.upper(),
        expiry=expiry,
        spot=round(spot, 2) if spot else None,
        dte=round(dte, 2),
        c_target=c_target,
        c_netdir=c_netdir,
        max_pain=max_pain(rows),
        call_wall=_peak_oi_strike(rows, "call"),
        put_wall=_peak_oi_strike(rows, "put"),
        long_avg=_oi_weighted_strike(rows, "call", weights=weights),
        short_avg=short_avg,
        total_call_oi=total_call_oi,
        total_put_oi=total_put_oi,
        rows=per_strike,
        note=note,
        **sig,
    )


def _crush_signals(rows: List[StrikeRow], spot: Optional[float], c_target: Optional[float],
                   short_avg: Optional[float], per_strike: List[dict],
                   t_years: float) -> dict:
    """Compute the crush / zone / pin signals the creator trades off of.

    * crush % — share of each side's OI that is OTM (premium that decays on a pin).
    * expensive side — where the OTM premium notional (OI × ask) is largest; dealers
      crush that side, pushing price the other way.
    * sweet-spot zone — the band between C and ShortAvg where dealer liability is
      lowest (price gets pinned here).
    * pin steepness — how sharply netDIR flips across the strikes bracketing C.
    * confidence — chain-based blend (reachability/liquidity left neutral here).
    """
    out = dict(call_crush_pct=None, put_crush_pct=None,
               total_call_volume=0.0, total_put_volume=0.0, volume_skew=None,
               volume_skew_side=None, volume_bias=None, volume_agree=None,
               expensive_side=None,
               crush_direction=None, trade_side=None, direction_agree=None,
               sweet_spot_low=None, sweet_spot_high=None, pin_steepness=None,
               atm_iv=None, expected_move=None, expected_move_pct=None,
               em_low=None, em_high=None, em_low_2=None, em_high_2=None,
               c_sigma=None, iv_reach=None,
               confidence=None, confidence_breakdown=None)
    if not spot or not rows:
        return out

    # IV expected-move bands (forward-looking ±σ from ATM implied vol).
    iv = atm_iv(rows, spot)
    em1 = expected_move(spot, iv, t_years)
    if iv:
        out["atm_iv"] = round(iv, 4)
    if em1:
        out["expected_move"] = round(em1, 2)
        out["expected_move_pct"] = round(em1 / spot, 4)
        out["em_low"] = round(spot - em1, 2)
        out["em_high"] = round(spot + em1, 2)
        out["em_low_2"] = round(spot - 2 * em1, 2)
        out["em_high_2"] = round(spot + 2 * em1, 2)
        if c_target is not None:
            sigma = abs(c_target - spot) / em1
            out["c_sigma"] = round(sigma, 2)
            out["iv_reach"] = ("within-1sig" if sigma <= 1.0
                               else "within-2sig" if sigma <= 2.0 else "beyond-2sig")

    call_otm_oi = sum(r.call_oi for r in rows if r.strike > spot)
    call_tot_oi = sum(r.call_oi for r in rows) or 0.0
    put_otm_oi = sum(r.put_oi for r in rows if r.strike < spot)
    put_tot_oi = sum(r.put_oi for r in rows) or 0.0
    out["call_crush_pct"] = round(call_otm_oi / call_tot_oi, 4) if call_tot_oi else None
    out["put_crush_pct"] = round(put_otm_oi / put_tot_oi, 4) if put_tot_oi else None

    call_otm_prem = sum(r.call_oi * r.call_ask for r in rows if r.strike > spot)
    put_otm_prem = sum(r.put_oi * r.put_ask for r in rows if r.strike < spot)
    if call_otm_prem or put_otm_prem:
        out["expensive_side"] = "CALL" if call_otm_prem >= put_otm_prem else "PUT"
        out["crush_direction"] = "DOWN" if out["expensive_side"] == "CALL" else "UP"

    # Volume skew: is live activity entering more call- or put-side than the OI
    # already implies? A call-side volume skew = writers positioning for a pullback.
    call_vol = sum(r.call_volume for r in rows)
    put_vol = sum(r.put_volume for r in rows)
    out["total_call_volume"], out["total_put_volume"] = call_vol, put_vol
    vol_tot, oi_tot = call_vol + put_vol, call_tot_oi + put_tot_oi
    if vol_tot > 0 and oi_tot > 0:
        skew = (call_vol / vol_tot) - (call_tot_oi / oi_tot)   # + = call-side building
        out["volume_skew"] = round(skew, 4)
        out["volume_skew_side"] = "CALL" if skew >= 0 else "PUT"
        out["volume_bias"] = "DOWN" if skew >= 0 else "UP"     # call-skew → pullback
        if out["crush_direction"] is not None:
            out["volume_agree"] = (out["volume_bias"] == out["crush_direction"])

    if c_target is not None:
        out["trade_side"] = "CALL" if c_target > spot else "PUT"
        # crush of the expensive side implies buying the opposite side
        crush_trade = ("PUT" if out["expensive_side"] == "CALL"
                       else "CALL" if out["expensive_side"] == "PUT" else None)
        if crush_trade is not None:
            out["direction_agree"] = (out["trade_side"] == crush_trade)
        if short_avg is not None:
            out["sweet_spot_low"] = round(min(c_target, short_avg), 2)
            out["sweet_spot_high"] = round(max(c_target, short_avg), 2)
        out["pin_steepness"] = _pin_steepness(per_strike, c_target)

    expensive_crush = (out["call_crush_pct"] if out["expensive_side"] == "CALL"
                       else out["put_crush_pct"] if out["expensive_side"] == "PUT" else None)
    # Chain-only base score: range reachability + liquidity stay neutral (the
    # scanner/endpoint recompute the full score with those). IV reach IS known
    # here, so it contributes even to the calculator's base confidence.
    score, breakdown = setup_confidence(out["direction_agree"], expensive_crush,
                                        out["pin_steepness"], iv_reach=out["iv_reach"],
                                        volume_agree=out["volume_agree"])
    out["confidence"] = score
    out["confidence_breakdown"] = breakdown
    return out


def _pin_steepness(per_strike: List[dict], c_target: float) -> Optional[float]:
    """netDIR jump across the two strikes bracketing C, normalised to 0..1."""
    below = above = None
    for row in per_strike:
        if row["strike"] <= c_target:
            below = row
        elif above is None:
            above = row
    if not below or not above:
        return None
    jump = abs(above["netdir"] - below["netdir"])
    return round(max(0.0, min(1.0, jump / PIN_STEEP_REF)), 3)
