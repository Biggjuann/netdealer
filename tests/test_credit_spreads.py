"""Unit tests for the IV-wall credit-spread monitor — pure logic, no network."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.credit_spreads import build_spread, evaluate_ticker
from app.net_dealer import StrikeRow

# AAPL-like chain, 2.5-wide strikes around the walls, with simple put/call asks.
AAPL_CW = (345.56, 350.07)   # call IV wall band
AAPL_PW = (332.39, 336.95)   # put IV wall band


def _rows(spot, credit_scale=1.0):
    rows = []
    for k10 in range(3200, 3601, 25):            # 320.0, 322.5, ... 360.0
        k = k10 / 10.0
        # Monotonic proxies: put value rises with strike, call value falls with
        # strike, so a credit spread's short leg is always the richer one.
        put_ask = max(0.05, credit_scale * (3.0 + 0.5 * (k - spot)))
        call_ask = max(0.05, credit_scale * (3.0 + 0.5 * (spot - k)))
        rows.append(StrikeRow(strike=k, call_oi=1000, put_oi=1000,
                              call_volume=100, put_volume=100,
                              call_iv=0.30, put_iv=0.30,
                              call_ask=round(call_ask, 2), put_ask=round(put_ask, 2)))
    return rows


def test_put_spread_strikes_and_width():
    # Long (lower) strike ~ bottom of the put wall; short = long + 2.5.
    s = build_spread(_rows(340.0), 340.0, "PUT", *AAPL_PW,
                     width=2.5, enter_credit=1.0, take_profit=0.10, contracts=10)
    assert s["long_strike"] == 332.5 and s["short_strike"] == 335.0   # 332.39→332.5, +2.5
    assert s["width"] == 2.5
    assert s["max_risk"] == (2.5 - 1.0) * 100 * 10
    print(f"ok  put spread legs: long {s['long_strike']} / short {s['short_strike']} w{s['width']}")


def test_call_spread_strikes_and_width():
    # Long (upper) strike ~ top of the call wall; short = long − 2.5.
    s = build_spread(_rows(348.0), 348.0, "CALL", *AAPL_CW,
                     width=2.5, enter_credit=1.0, take_profit=0.10, contracts=10)
    assert s["long_strike"] == 350.0 and s["short_strike"] == 347.5
    assert s["width"] == 2.5
    print(f"ok  call spread legs: long {s['long_strike']} / short {s['short_strike']} w{s['width']}")


def test_put_wait_when_price_above_wall():
    # Spot well above the put wall → not at the wall yet → WAIT.
    s = build_spread(_rows(345.0), 345.0, "PUT", *AAPL_PW,
                     width=2.5, enter_credit=1.0, take_profit=0.10, contracts=10)
    assert s["in_zone"] is False and s["state"] == "WAIT"
    print(f"ok  price above put wall → WAIT (to_wall {s['to_wall_pct']*100:.1f}%)")


def test_put_enter_when_credit_reaches_target():
    # Price in the put wall band and a rich credit (>= $1) → ENTER.
    s = build_spread(_rows(335.0, credit_scale=1.0), 335.0, "PUT", *AAPL_PW,
                     width=2.5, enter_credit=1.0, take_profit=0.10, contracts=10)
    assert s["in_zone"] is True and s["credit"] >= 1.0 and s["state"] == "ENTER"
    print(f"ok  at put wall, credit {s['credit']} ≥ $1 → ENTER")


def test_watch_when_credit_too_thin():
    # In the zone but credit below $1 (thin premiums) → WATCH, not ENTER.
    s = build_spread(_rows(335.0, credit_scale=0.05), 335.0, "PUT", *AAPL_PW,
                     width=2.5, enter_credit=1.0, take_profit=0.10, contracts=10)
    assert s["in_zone"] is True and s["credit"] < 1.0 and s["state"] == "WATCH"
    print(f"ok  at wall but credit {s['credit']} < $1 → WATCH")


def test_stop_when_price_breaks_below_wall():
    # Spot below the put wall floor → wall broken → AVOID (or STOP if in a trade).
    rows = _rows(330.0)
    s = build_spread(rows, 330.0, "PUT", *AAPL_PW,
                     width=2.5, enter_credit=1.0, take_profit=0.10, contracts=10)
    assert s["broke"] is True and s["state"] == "AVOID"
    s_in = build_spread(rows, 330.0, "PUT", *AAPL_PW,
                        width=2.5, enter_credit=1.0, take_profit=0.10, contracts=10, entered=True)
    assert s_in["state"] == "STOP"
    print("ok  below put wall → AVOID (flat) / STOP (in trade)")


def test_take_profit_when_entered_and_credit_decays():
    # In a trade, spread value decayed to <= $0.10 → TAKE PROFIT.
    s = build_spread(_rows(338.0, credit_scale=0.01), 335.0, "PUT", *AAPL_PW,
                     width=2.5, enter_credit=1.0, take_profit=0.10, contracts=10, entered=True)
    assert s["tp_hit"] is True and s["state"] == "TAKE PROFIT"
    print(f"ok  entered + credit {s['credit']} ≤ $0.10 → TAKE PROFIT")


def test_evaluate_ticker_has_both_sides():
    d = evaluate_ticker(_rows(340.0), 340.0, *AAPL_CW, *AAPL_PW,
                        width=2.5, enter_credit=1.0, take_profit=0.10, contracts=10)
    assert d["put_spread"]["side"] == "PUT" and d["call_spread"]["side"] == "CALL"
    print("ok  evaluate_ticker returns both put and call spreads")


def main():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for t in tests:
        t()
    print(f"\nAll {len(tests)} credit-spread tests passed.")


if __name__ == "__main__":
    main()
