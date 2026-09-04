"""Unit tests for the opportunity scanner — pure logic + mock feed, no network."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.config import settings
from app.net_dealer import StrikeRow, bs_price
from app.providers.mock import MockChainProvider
from app.scanner import _best_contract, evaluate, rank_contracts, run_scan


def test_bs_price_sane():
    # ATM call and put have positive time value; deep ITM ~ intrinsic.
    assert bs_price(100, 100, 30 / 365, 0.3, True) > 0
    assert bs_price(100, 100, 30 / 365, 0.3, False) > 0
    assert abs(bs_price(150, 100, 30 / 365, 0.3, True) - 50) < 5     # deep ITM call ~ 50
    assert bs_price(100, 100, 0, 0.3, True) == 0.0                   # expiry ATM -> intrinsic 0
    print("ok  bs_price sane")


def test_best_contract_picks_positive_gain_side():
    # C well above spot(100): a slightly-OTM call that finishes ITM at C should win.
    rows = [StrikeRow(strike=k, call_oi=5000, put_oi=5000, call_iv=0.3, put_iv=0.3,
                      call_ask=max(0.05, bs_price(100, k, 20/365, 0.3, True)),
                      put_ask=max(0.05, bs_price(100, k, 20/365, 0.3, False)))
            for k in range(90, 116, 1)]
    c = 112.0
    best = _best_contract(rows, c, "CALL")
    assert best is not None, "should find a positive-gain call"
    gain, k, ask, oi, vol, be = best
    assert gain >= settings.scan_min_gain_pct
    assert k < c, "winning call strike should finish ITM at C"
    assert abs(be - (k + ask)) < 1e-6
    print(f"ok  best contract: K={k} ask={ask:.2f} gain={gain*100:.0f}% be={be:.2f}")


def test_rank_contracts_topn_sorted():
    # C above spot -> rank calls; expect several picks, sorted by gain desc,
    # all finishing ITM at C.
    rows = [StrikeRow(strike=k, call_oi=5000, put_oi=5000, call_iv=0.3, put_iv=0.3,
                      call_ask=max(0.05, bs_price(100, k, 20/365, 0.3, True)),
                      put_ask=max(0.05, bs_price(100, k, 20/365, 0.3, False)))
            for k in range(90, 116, 1)]
    picks = rank_contracts(rows, 112.0, "CALL", min_gain=0.0, limit=5)
    assert 1 <= len(picks) <= 5
    gains = [p["est_gain_pct"] for p in picks]
    assert gains == sorted(gains, reverse=True)
    for p in picks:
        assert p["side"] == "CALL"
        assert p["strike"] < 112.0            # finishes ITM at C
        assert p["est_gain_pct"] >= 0
        assert p["intrinsic_at_c"] > 0
    print(f"ok  rank_contracts top-{len(picks)}: gains {[round(g*100) for g in gains]}")


def test_best_contract_respects_liquidity_floor():
    # Same chain but zero OI -> nothing clears the liquidity floor.
    rows = [StrikeRow(strike=k, call_oi=0, put_oi=0, call_iv=0.3, put_iv=0.3,
                      call_ask=max(0.05, bs_price(100, k, 20/365, 0.3, True)))
            for k in range(90, 116, 1)]
    assert _best_contract(rows, 112.0, "CALL") is None
    print("ok  liquidity floor enforced")


def test_evaluate_direction_matches_edge():
    prov = MockChainProvider()
    # Find one call-side and one put-side name from the mock's per-ticker skew.
    r = evaluate(prov, "PFE")
    assert r.spot and r.c_target is not None
    if r.skip is None:
        assert r.direction == ("CALL" if r.c_target > r.spot else "PUT")
        assert r.est_gain_pct is None or r.est_gain_pct >= settings.scan_min_gain_pct
    print(f"ok  evaluate PFE: C={r.c_target} spot={r.spot} side={r.direction} skip={r.skip}")


def test_run_scan_actionable_rows():
    prov = MockChainProvider()
    d = run_scan(prov, tickers=["NVDA", "AAPL", "TSLA", "PFE", "LLY", "MSFT", "JPM", "GS"],
                 use_cache=False)
    assert d["scanned"] == 8
    # every ranked row is a real, actionable opportunity (ordering is checked by
    # test_scan_ranks_by_opportunity_score)
    for r in d["results"]:
        assert r["direction"] in ("CALL", "PUT")
        assert r["strike"] is not None and r["premium"] is not None
        assert r["est_gain_pct"] >= settings.scan_min_gain_pct
    print(f"ok  run_scan: {d['opportunities']}/{d['scanned']} actionable opportunities")


def test_scan_dte_filter():
    prov = MockChainProvider()
    tickers = ["NVDA", "AAPL", "TSLA"]
    # 0DTE scope: only same-day expiries survive.
    d0 = run_scan(prov, tickers=tickers, use_cache=False, weeks=[0], max_dte=0.99)
    assert d0["max_dte"] == 0.99 and d0["results"], "0DTE scan should have same-day rows"
    for r in d0["results"]:
        assert r["dte"] < 1.0, f"0DTE scope must be same-day only: {r['dte']}"
    # <=1DTE scope: same-day + next-day, nothing longer-dated.
    d1 = run_scan(prov, tickers=tickers, use_cache=False, weeks=[0, 1], max_dte=1.99)
    assert d1["results"]
    for r in d1["results"]:
        assert r["dte"] <= 1.99, f"<=1DTE scope leaked a longer expiry: {r['dte']}"
    # a longer expiry is dropped as skipped, not ranked
    long_scan = run_scan(prov, tickers=["NVDA"], use_cache=False, weeks=[0, 1, 2, 3, 4],
                         max_dte=1.99)
    assert all(r["dte"] <= 1.99 for r in long_scan["results"])
    assert any("DTE cap" in s["reason"] for s in long_scan["skipped"])
    print(f"ok  DTE filter: 0DTE rows={len(d0['results'])}, ≤1DTE rows={len(d1['results'])}")


def test_scan_ranks_by_confidence():
    prov = MockChainProvider()
    d = run_scan(prov, tickers=["NVDA", "AAPL", "TSLA", "PFE", "LLY", "JPM", "GS", "COIN"],
                 use_cache=False)
    # Best setups first: sorted by Setup confidence, then est_gain as a tiebreaker.
    keys = [(r["confidence"], r["est_gain_pct"]) for r in d["results"]]
    assert keys == sorted(keys, reverse=True), "must rank by confidence, then gain, descending"
    for r in d["results"]:
        assert 0 <= r["confidence"] <= 100
        assert r["expensive_side"] in ("CALL", "PUT", None)
        assert r["sweet_spot_low"] is not None and r["sweet_spot_high"] is not None
    print(f"ok  scan ranked by confidence (best first); top conf={d['results'][0]['confidence']}")


def test_run_scan_cache():
    prov = MockChainProvider()
    tickers = ["NVDA", "AAPL"]
    run_scan(prov, tickers=tickers, use_cache=False)
    cached = run_scan(prov, tickers=tickers, use_cache=True)
    assert cached["cached"] is True
    print("ok  scan cache hit")


def main():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for t in tests:
        t()
    print(f"\nAll {len(tests)} scanner tests passed.")


if __name__ == "__main__":
    main()
