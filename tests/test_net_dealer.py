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
    max_pain, net_delta_at, netdir_pct_at,
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
