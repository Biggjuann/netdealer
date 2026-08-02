"""Time-to-expiry helper shared by the server and the scanner."""
from __future__ import annotations

import datetime as dt
from typing import Tuple


def t_years(expiry: str) -> Tuple[float, float]:
    """(years, calendar-days) from now to the expiry's ~16:00 ET close.

    Expiry is a ``YYYY-MM-DD`` string. The moment is approximated as 21:00 UTC
    (4pm ET) on that date. Years floors at 0; used for Black-Scholes deltas.
    """
    try:
        exp = dt.datetime.strptime(expiry, "%Y-%m-%d")
    except (ValueError, TypeError):
        return 1.0 / 365.0, 1.0
    exp = exp.replace(hour=21, minute=0)
    seconds = (exp - dt.datetime.utcnow()).total_seconds()
    days = max(0.0, seconds / 86400.0)
    return max(seconds / (365.0 * 86400.0), 0.0), days
