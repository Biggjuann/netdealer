"""Unit tests for the net-dealer "C" engine — pure math, no network.

Run with either:
    python tests/test_net_dealer.py     (bare, used by CI)
    pytest tests/                       (if pytest is available)
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.net_dealer import (
    StrikeRow, bs_call_delta, bs_put_delta, compute, find_c_target,
    max_pain, net_delta_at, netdir_pct_at, setup_confidence,
)
from app.providers.mock import MockChainProvider


T = 5 / 365.0   # ~5 days to expiry


def approx(a, b, tol=1e-6):
    return abs(a - b) <= tol


def _symmetric_chain():
    """A perfectly symmetric book: equal call/put OI mirrored around 100.
    The net-dealer C must land at the center of symmetry (100)."""
    rows = []
    for k in range(80, 121, 5):
        dist = abs(k - 100)
        oi = max(100.0, 2000.0 - dist * 60.0)
        rows.append(StrikeRow(strike=float(k), call_oi=oi, put_oi=oi,
                              call_iv=0.35, put_iv=0.35))
    return rows


def test_bs_delta_bounds():
    assert 0.0 <= bs_call_delta(100, 100, T, 0.3) <= 1.0
    assert approx(bs_put_delta(100, 100, T, 0.3),
                  bs_call_delta(100, 100, T, 0.3) - 1.0)
    # Deep ITM call ~ delta 1, deep OTM call ~ delta 0.
    assert bs_call_delta(200, 100, T, 0.3) > 0.98
    assert bs_call_delta(50, 100, T, 0.3) < 0.02
    # Degenerate (expiry) -> step function.
    assert bs_call_delta(101, 100, 0, 0.3) == 1.0
    assert bs_call_delta(99, 100, 0, 0.3) == 0.0
    print("ok  bs delta bounds")


def test_net_delta_monotonic_increasing():
    rows = _symmetric_chain()
    prices = [70, 80, 90, 100, 110, 120, 130]
    vals = [net_delta_at(p, rows, T) for p in prices]
    for a, b in zip(vals, vals[1:]):
        assert b > a, f"netDelta must strictly increase: {a} !< {b}"
    print("ok  net delta monotonic increasing")


def test_c_target_symmetric_center():
    rows = _symmetric_chain()
    c = find_c_target(rows, T)
    assert c is not None
    assert approx(c, 100.0, 0.5), f"symmetric book should pin C at 100, got {c}"
    # netDIR at C is ~0.
    assert abs(netdir_pct_at(c, rows, T)) < 0.01
    print(f"ok  symmetric C = {c} (~100)")


def test_c_shifts_with_oi_skew():
    """Directional pull matches the framework:
      * a big CALL wall above → dealers net-short overhead calls → downward pull,
        so the delta-neutral C sits LOWER (price must stay low to keep those
        calls OTM / the book delta-balanced);
      * a big PUT wall below → dealers net-long → upward pull → C sits HIGHER.
    """
    base = _symmetric_chain()
    c0 = find_c_target(base, T)

    heavy_calls = _symmetric_chain()
    for r in heavy_calls:
        if r.strike >= 105:
            r.call_oi *= 6           # big upside call wall
    c_calls = find_c_target(heavy_calls, T)

    heavy_puts = _symmetric_chain()
    for r in heavy_puts:
        if r.strike <= 95:
            r.put_oi *= 6            # big downside put wall
    c_puts = find_c_target(heavy_puts, T)

    assert c_calls < c0, f"call wall above should pull C down: {c_calls} !< {c0}"
    assert c_puts > c0, f"put wall below should pull C up: {c_puts} !> {c0}"
    print(f"ok  walls pull C: calls↓={c_calls} < base={c0} < puts↑={c_puts}")


def test_max_pain_symmetric():
    rows = _symmetric_chain()
    assert approx(max_pain(rows), 100.0, 0.01)
    print("ok  max pain = 100 on symmetric book")


def test_max_pain_known_case():
    # All OI at one strike -> max pain is that strike (zero payout there).
    rows = [StrikeRow(strike=100.0, call_oi=1000, put_oi=1000, call_iv=0.3, put_iv=0.3),
            StrikeRow(strike=110.0, call_oi=10, put_oi=10, call_iv=0.3, put_iv=0.3),
            StrikeRow(strike=90.0, call_oi=10, put_oi=10, call_iv=0.3, put_iv=0.3)]
    assert max_pain(rows) == 100.0
    print("ok  max pain concentrated case")


def test_compute_result_shape():
    rows = _symmetric_chain()
    res = compute("TEST", "2099-01-15", rows, spot=100.0, t_years=T, dte=5.0)
    assert res.ticker == "TEST"
    assert res.c_target is not None
    assert res.max_pain is not None
    assert res.call_wall is not None and res.put_wall is not None
    assert len(res.rows) == len(rows)
    assert all("netdir" in r for r in res.rows)
    # netDIR should be increasing across strikes (sorted low->high).
    nds = [r["netdir"] for r in res.rows]
    assert nds == sorted(nds), "per-strike netDIR should rise with strike"
    print(f"ok  compute() shape; C={res.c_target}, pain={res.max_pain}, "
          f"cw={res.call_wall}, pw={res.put_wall}")


def test_crush_signals_and_zone():
    # Build a book with a heavy, expensive OTM CALL wall above spot(100) and a
    # C below spot -> expensive side CALL, crush DOWN, trade PUT, directions agree.
    rows = []
    for k in range(80, 121, 5):
        call_oi = 8000 if k > 100 else 500      # calls stacked above -> OTM heavy
        put_oi = 3000 if k < 100 else 500
        rows.append(StrikeRow(strike=float(k), call_oi=call_oi, put_oi=put_oi,
                              call_iv=0.35, put_iv=0.35,
                              call_ask=max(0.5, 108 - k) if k < 108 else 1.0,
                              put_ask=max(0.5, k - 92) if k > 92 else 1.0))
    res = compute("T", "2099-01-15", rows, spot=100.0, t_years=5/365.0, dte=5.0)
    assert res.expensive_side == "CALL"
    assert res.crush_direction == "DOWN"
    # C should sit below spot (call-heavy book), so the trade is a PUT and agrees.
    assert res.c_target < 100.0
    assert res.trade_side == "PUT"
    assert res.direction_agree is True
    # sweet spot is an ordered band including C
    assert res.sweet_spot_low <= res.c_target <= res.sweet_spot_high or \
           res.sweet_spot_low <= res.sweet_spot_high
    assert res.call_crush_pct is not None and 0 <= res.call_crush_pct <= 1
    assert res.confidence is not None and 0 <= res.confidence <= 100
    print(f"ok  crush signals: exp={res.expensive_side} dir={res.crush_direction} "
          f"agree={res.direction_agree} zone=[{res.sweet_spot_low},{res.sweet_spot_high}] "
          f"conf={res.confidence}")


def test_setup_confidence_monotonic():
    # Agreement, more fuel, sharper pin, better range & IV reachability all raise it.
    base = setup_confidence(True, 0.8, 0.8, range_reach="within-mean",
                            iv_reach="within-1sig", liquidity_oi=5000)[0]
    assert base > setup_confidence(False, 0.8, 0.8, range_reach="within-mean",
                                   iv_reach="within-1sig", liquidity_oi=5000)[0], "disagreement hurts"
    assert base > setup_confidence(True, 0.4, 0.8, range_reach="within-mean",
                                   iv_reach="within-1sig", liquidity_oi=5000)[0], "less fuel hurts"
    assert base > setup_confidence(True, 0.8, 0.8, range_reach="out-of-range",
                                   iv_reach="within-1sig", liquidity_oi=5000)[0], "out of 30d range hurts"
    assert base > setup_confidence(True, 0.8, 0.8, range_reach="within-mean",
                                   iv_reach="beyond-2sig", liquidity_oi=5000)[0], "beyond IV move hurts"
    s, comp = setup_confidence(None, None, None)
    assert 0 <= s <= 100
    assert set(comp) == {"direction", "range", "iv", "fuel", "steepness", "liquidity"}
    print(f"ok  confidence monotonic (range+iv); strong setup = {base}")


def test_expected_move_bands():
    from app.net_dealer import atm_iv, expected_move
    rows = [StrikeRow(strike=float(k), call_oi=1000, put_oi=1000,
                      call_iv=0.30, put_iv=0.30) for k in range(90, 111, 5)]
    iv = atm_iv(rows, 100.0)
    assert approx(iv, 0.30, 0.01)
    # 30 days out: EM = 100 * 0.30 * sqrt(30/365) ≈ 8.6
    em = expected_move(100.0, iv, 30 / 365.0)
    assert 8.0 < em < 9.2, em
    res = compute("T", "2099-01-15", rows, spot=100.0, t_years=30 / 365.0, dte=30.0)
    assert res.expected_move is not None
    assert res.em_low < 100.0 < res.em_high
    assert res.em_low_2 < res.em_low and res.em_high_2 > res.em_high
    if res.c_target is not None:
        assert res.c_sigma is not None
        assert res.iv_reach in ("within-1sig", "within-2sig", "beyond-2sig")
    print(f"ok  IV bands: EM=±{res.expected_move} ({res.expected_move_pct*100:.1f}%) "
          f"1σ=[{res.em_low},{res.em_high}] Cσ={res.c_sigma} reach={res.iv_reach}")


def test_volume_blend_shifts_c_and_skew():
    # Base symmetric OI (C at center). Add call-side VOLUME above the money -> the
    # effective book tilts upside -> C moves DOWN, and volume skew reads call-side.
    rows = []
    for k in range(80, 121, 5):
        dist = abs(k - 100)
        oi = max(100.0, 2000.0 - dist * 60.0)
        cvol = 3000.0 if k > 100 else 100.0     # incoming call volume above spot
        rows.append(StrikeRow(strike=float(k), call_oi=oi, put_oi=oi,
                              call_volume=cvol, put_volume=100.0,
                              call_iv=0.35, put_iv=0.35))
    c_oi = find_c_target(rows, T, volume_weight=0.0)
    c_blend = find_c_target(rows, T, volume_weight=1.0)
    assert c_blend < c_oi, f"call-side volume should pull C down: {c_blend} !< {c_oi}"
    res = compute("T", "2099-01-15", rows, 100.0, T, 5.0, volume_weight=1.0)
    assert res.volume_skew_side == "CALL"
    assert res.volume_bias == "DOWN"           # call-side skew → pullback bias
    assert res.total_call_volume > res.total_put_volume
    print(f"ok  volume blend: C {c_oi}→{c_blend}, skew={res.volume_skew} "
          f"side={res.volume_skew_side} bias={res.volume_bias}")


def test_volume_disagreement_lowers_confidence():
    # Same setup, direction agrees, but volume fighting the crush cuts the score.
    agree = setup_confidence(True, 0.8, 0.8, range_reach="within-mean",
                             iv_reach="within-1sig", liquidity_oi=5000, volume_agree=True)[0]
    conflict = setup_confidence(True, 0.8, 0.8, range_reach="within-mean",
                                iv_reach="within-1sig", liquidity_oi=5000, volume_agree=False)[0]
    assert conflict < agree, "volume skewing against the thesis should lower confidence"
    print(f"ok  volume conflict lowers confidence: {agree} -> {conflict}")


def test_empty_chain_is_safe():
    res = compute("X", "2099-01-15", [], spot=None, t_years=T, dte=5.0)
    assert res.c_target is None
    assert res.note
    print("ok  empty chain handled")


def test_mock_provider_end_to_end():
    prov = MockChainProvider()
    exps = prov.get_expirations("MU")
    assert exps, "mock should return expirations"
    rows, spot = prov.get_chain("MU", exps[0], 60)
    assert rows and spot
    res = compute("MU", exps[0], rows, spot, t_years=T, dte=5.0)
    assert res.c_target is not None
    # C should be in the neighbourhood of spot (within ~15%).
    assert abs(res.c_target - spot) / spot < 0.15, \
        f"C {res.c_target} unreasonably far from spot {spot}"
    print(f"ok  mock MU: spot={spot} C={res.c_target} pain={res.max_pain} "
          f"cw={res.call_wall} pw={res.put_wall}")


def main():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for t in tests:
        t()
    print(f"\nAll {len(tests)} net-dealer tests passed.")


if __name__ == "__main__":
    main()
