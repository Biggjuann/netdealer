"""Unit tests for the opportunity scanner — pure logic + mock feed, no network."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.config import settings
from app.net_dealer import StrikeRow, bs_price
from app.providers.mock import MockChainProvider
from app.scanner import _best_contract, evaluate, run_scan


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


def test_run_scan_ranks_by_gain_desc():
    prov = MockChainProvider()
    d = run_scan(prov, tickers=["NVDA", "AAPL", "TSLA", "PFE", "LLY", "MSFT", "JPM", "GS"],
                 use_cache=False)
    assert d["scanned"] == 8
    gains = [r["est_gain_pct"] for r in d["results"]]
    assert gains == sorted(gains, reverse=True), "results must be ranked by est_gain desc"
    # every ranked row is a real, actionable opportunity
    for r in d["results"]:
        assert r["direction"] in ("CALL", "PUT")
        assert r["strike"] is not None and r["premium"] is not None
        assert r["est_gain_pct"] >= settings.scan_min_gain_pct
    print(f"ok  run_scan: {d['opportunities']}/{d['scanned']} opportunities, "
          f"top gain {gains[0]*100:.0f}%" if gains else "ok  run_scan (no opps)")


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
