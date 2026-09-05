"""Lightweight, dependency-free US equity-market calendar.

Just enough to answer "how many TRADING sessions until this expiry?" so the
scanner can treat 0DTE = expires today and 1DTE = the next trading session —
skipping weekends *and* market holidays (so Fri→Mon and holiday gaps are right).

Holidays follow the NYSE/Nasdaq schedule with the usual observance rule
(Saturday → observed Friday, Sunday → observed Monday). Good Friday is derived
from Easter (computus). This is a close approximation, not an exchange feed; the
rare New-Year-on-Saturday edge is not special-cased.
"""
from __future__ import annotations

import datetime as dt
from functools import lru_cache
from typing import Optional, Set

_ONE = dt.timedelta(days=1)


def _nth_weekday(year: int, month: int, weekday: int, n: int) -> dt.date:
    """The n-th ``weekday`` (Mon=0) of ``month`` (n=1 → first)."""
    d = dt.date(year, month, 1)
    offset = (weekday - d.weekday()) % 7
    return d + dt.timedelta(days=offset + 7 * (n - 1))


def _last_weekday(year: int, month: int, weekday: int) -> dt.date:
    """The last ``weekday`` of ``month``."""
    if month == 12:
        nxt = dt.date(year + 1, 1, 1)
    else:
        nxt = dt.date(year, month + 1, 1)
    d = nxt - _ONE
    return d - dt.timedelta(days=(d.weekday() - weekday) % 7)


def _easter(year: int) -> dt.date:
    """Anonymous Gregorian computus."""
    a = year % 19
    b, c = divmod(year, 100)
    d, e = divmod(b, 4)
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month = (h + l - 7 * m + 114) // 31
    day = ((h + l - 7 * m + 114) % 31) + 1
    return dt.date(year, month, day)


def _observed(d: dt.date) -> dt.date:
    """Weekend holiday observance: Sat → Fri, Sun → Mon."""
    if d.weekday() == 5:
        return d - _ONE
    if d.weekday() == 6:
        return d + _ONE
    return d


@lru_cache(maxsize=32)
def market_holidays(year: int) -> Set[dt.date]:
    """Observed NYSE/Nasdaq full-day closures for ``year``."""
    h = {
        _observed(dt.date(year, 1, 1)),          # New Year's Day
        _nth_weekday(year, 1, 0, 3),             # MLK Jr. — 3rd Mon Jan
        _nth_weekday(year, 2, 0, 3),             # Washington's Birthday — 3rd Mon Feb
        _easter(year) - dt.timedelta(days=2),    # Good Friday
        _last_weekday(year, 5, 0),               # Memorial Day — last Mon May
        _observed(dt.date(year, 7, 4)),          # Independence Day
        _nth_weekday(year, 9, 0, 1),             # Labor Day — 1st Mon Sep
        _nth_weekday(year, 11, 3, 4),            # Thanksgiving — 4th Thu Nov
        _observed(dt.date(year, 12, 25)),        # Christmas
    }
    if year >= 2022:
        h.add(_observed(dt.date(year, 6, 19)))   # Juneteenth
    return h


def is_trading_day(d: dt.date) -> bool:
    """Weekday that is not a market holiday."""
    return d.weekday() < 5 and d not in market_holidays(d.year)


def sessions_to_expiry(expiry: dt.date, today: Optional[dt.date] = None) -> int:
    """Trading sessions from ``today`` to ``expiry``.

    0 = expires today (or already), 1 = the next trading session, etc. Weekends
    and holidays are not counted, so Fri→Mon is 1 and a holiday Monday pushes the
    next session to Tuesday.
    """
    today = today or dt.date.today()
    if expiry <= today:
        return 0
    count, d = 0, today
    while d < expiry:
        d += _ONE
        if is_trading_day(d):
            count += 1
    return count
