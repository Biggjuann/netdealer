"""Range-stats + achievability-filter tests — pure, no network.

The range math mirrors Biggjuann/Range's range_calc.py; these lock in the ADR /
widest values and the scanner's within-mean / within-max / out-of-range logic.
"""
import datetime as dt
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.range_stats import Candle, compute_range
from app.scanner import classify_range


def approx(a, b, tol=1e-6):
    return abs(a - b) <= tol


def _c(day, o, h, l, c):
    return Candle(dt.date(2026, 1, day), o, h, l, c, 1000)


def test_adr_and_widest():
    candles = [_c(1, 100, 101, 99, 100),   # range 2
               _c(2, 100, 102, 98, 100),   # range 4
               _c(3, 100, 103, 97, 100)]   # range 6
    rep = compute_range("TEST", candles, 3)
    assert rep["sessions_used"] == 3
    assert approx(rep["adr_dollars"], 4.0)             # (2+4+6)/3
    assert approx(rep["adr_percent"], 4.0)             # mean(2,4,6)/100*100
    assert approx(rep["max_range_dollars"], 6.0)       # widest single day
    assert approx(rep["max_range_percent"], 6.0)
    print(f"ok  ADR=${rep['adr_dollars']} maxday=${rep['max_range_dollars']}")


def test_window_trims_to_last_n():
    candles = [_c(d, 100, 100 + d, 100 - d, 100) for d in range(1, 11)]  # ranges 2..20
    rep = compute_range("T", candles, 3)                 # last 3 -> ranges 16,18,20
    assert rep["sessions_used"] == 3
    assert approx(rep["adr_dollars"], 18.0)
    assert approx(rep["max_range_dollars"], 20.0)
    print("ok  window trims to last N")


def test_empty_is_none():
    assert compute_range("T", [], 30) is None
    print("ok  empty -> None")


def test_classify_range_buckets():
    # ADR $2, widest $5. horizon=1 day (dte<1), mult=1.
    stats = {"adr_dollars": 2.0, "max_range_dollars": 5.0,
             "adr_percent": 2.0, "max_range_percent": 5.0}
    spot, dte = 100.0, 0.5
    # move 1.5 <= mean(2) -> within-mean
    assert classify_range(1.5, spot, dte, stats)[0] == "within-mean"
    # move 3.0 : > mean(2), <= max(5) -> within-max
    assert classify_range(3.0, spot, dte, stats)[0] == "within-max"
    # move 6.0 : > max(5) -> out-of-range
    assert classify_range(6.0, spot, dte, stats)[0] == "out-of-range"
    # no stats -> unknown (fails open)
    assert classify_range(6.0, spot, dte, None)[0] == "unknown"
    print("ok  classify buckets mean/max/out/unknown")


def test_classify_scales_with_horizon():
    stats = {"adr_dollars": 2.0, "max_range_dollars": 5.0}
    # Same 8-dollar move: unreachable in ~1 day, reachable over a multi-day horizon.
    assert classify_range(8.0, 100.0, 1.0, stats)[0] == "out-of-range"
    assert classify_range(8.0, 100.0, 5.0, stats)[0] in ("within-mean", "within-max")
    print("ok  horizon scaling widens reach")


def test_scanner_filter_end_to_end():
    from app.providers.mock import MockChainProvider
    from app.scanner import run_scan
    prov = MockChainProvider()
    tickers = ["NVDA", "AAPL", "TSLA", "AMD", "JPM", "GS", "NIO", "COIN"]
    on = run_scan(prov, tickers=tickers, use_cache=False, range_filter=True)
    off = run_scan(prov, tickers=tickers, use_cache=False, range_filter=False)
    assert on["range_filter"] is True and off["range_filter"] is False
    # filtering can only remove, never add, opportunities
    assert on["opportunities"] <= off["opportunities"]
    assert on["range_filtered_out"] == off["opportunities"] - on["opportunities"]
    # every surfaced row carries a non-out-of-range verdict
    for r in on["results"]:
        assert r["range_conf"] in ("within-mean", "within-max", "unknown")
        assert r["adr_percent"] is not None
    print(f"ok  filter e2e: on={on['opportunities']} off={off['opportunities']} "
          f"filtered={on['range_filtered_out']}")


def main():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for t in tests:
        t()
    print(f"\nAll {len(tests)} range tests passed.")


if __name__ == "__main__":
    main()
