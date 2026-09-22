"""US market-calendar tests — holidays + trading-session counting, no network."""
import datetime as dt
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.market_calendar import (close_hour_et, is_early_close, is_trading_day,
                                  market_holidays, sessions_to_expiry)


def test_known_2026_holidays():
    h = market_holidays(2026)
    for d in [dt.date(2026, 1, 1),    # New Year (Thu)
              dt.date(2026, 1, 19),   # MLK (3rd Mon)
              dt.date(2026, 4, 3),    # Good Friday
              dt.date(2026, 5, 25),   # Memorial (last Mon)
              dt.date(2026, 6, 19),   # Juneteenth (Fri)
              dt.date(2026, 7, 3),    # July 4 (Sat) -> observed Fri
              dt.date(2026, 11, 26),  # Thanksgiving (4th Thu)
              dt.date(2026, 12, 25)]: # Christmas (Fri)
        assert d in h, f"missing holiday {d}"
    assert not is_trading_day(dt.date(2026, 12, 25))
    assert is_trading_day(dt.date(2026, 9, 8))   # a normal Tuesday
    print("ok  2026 holidays detected")


def test_sessions_same_day_and_weekend():
    # same day = 0
    assert sessions_to_expiry(dt.date(2026, 9, 9), dt.date(2026, 9, 9)) == 0
    # normal Fri -> Mon = 1 session
    assert sessions_to_expiry(dt.date(2026, 9, 14), dt.date(2026, 9, 11)) == 1
    # Wed -> Thu = 1
    assert sessions_to_expiry(dt.date(2026, 9, 10), dt.date(2026, 9, 9)) == 1
    # Wed -> Fri = 2 (Fri excluded from <=1DTE)
    assert sessions_to_expiry(dt.date(2026, 9, 11), dt.date(2026, 9, 9)) == 2
    print("ok  session counts (same-day / Fri→Mon / Wed→Thu)")


def test_sessions_skip_holiday():
    # Fri Sep 4 2026 -> next session is Tue Sep 8 (Mon Sep 7 = Labor Day) = 1
    assert sessions_to_expiry(dt.date(2026, 9, 8), dt.date(2026, 9, 4)) == 1
    # Day before Thanksgiving (Wed Nov 25) -> Fri Nov 27 = 1 session (Thu is holiday)
    assert sessions_to_expiry(dt.date(2026, 11, 27), dt.date(2026, 11, 25)) == 1
    print("ok  session counts skip holidays (Labor Day, Thanksgiving)")


def test_early_close_half_days():
    # Friday after Thanksgiving 2026 (Nov 27) is a 1:00pm ET half-day.
    assert is_early_close(dt.date(2026, 11, 27))
    assert close_hour_et(dt.date(2026, 11, 27)) == 13
    # Christmas Eve 2026 (Thu Dec 24) is a weekday half-day.
    assert is_early_close(dt.date(2026, 12, 24))
    # A normal session closes at 16:00 and is not an early close.
    assert not is_early_close(dt.date(2026, 9, 22))
    assert close_hour_et(dt.date(2026, 9, 22)) == 16
    # A full holiday is not an "early close" (it's shut).
    assert not is_early_close(dt.date(2026, 11, 26))   # Thanksgiving
    print("ok  early-close half-days (Thanksgiving Fri, Christmas Eve) at 13:00 ET")


def main():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for t in tests:
        t()
    print(f"\nAll {len(tests)} calendar tests passed.")


if __name__ == "__main__":
    main()
