"""US market-calendar tests — holidays + trading-session counting, no network."""
import datetime as dt
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.market_calendar import is_trading_day, market_holidays, sessions_to_expiry


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


def main():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for t in tests:
        t()
    print(f"\nAll {len(tests)} calendar tests passed.")


if __name__ == "__main__":
    main()
