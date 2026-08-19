"""30-day range statistics — vendored methodology from Biggjuann/Range.

Measures how much a symbol actually moves, from its daily candles, so the
scanner can reject net-dealer targets that sit outside what a ticker has proven
it can travel recently.

Definitions (over the supplied completed daily sessions) — matching the Range
app's ``range_calc.py`` exactly:

* **Daily range**  high − low for a session.
* **ADR ($)**      mean of the daily ranges  (the "mean range").
* **ADR (%)**      mean of (range / close × 100).
* **ATR ($)**      mean True Range = mean of max(H−L, |H−prevClose|, |L−prevClose|).
* **widest**       the largest single-day range in the window (the "max range").

Source: https://github.com/Biggjuann/Range (app/range_calc.py, app/schwab.py).
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import List, Optional


@dataclass
class Candle:
    date: dt.date
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0

    @classmethod
    def from_schwab(cls, c: dict) -> "Candle":
        d = dt.datetime.fromtimestamp(int(c["datetime"]) / 1000, tz=dt.timezone.utc).date()
        return cls(d, float(c["open"]), float(c["high"]), float(c["low"]),
                   float(c["close"]), float(c.get("volume") or 0))


def _mean(xs: List[float]) -> Optional[float]:
    return sum(xs) / len(xs) if xs else None


def _true_ranges(candles: List[Candle]) -> List[float]:
    trs: List[float] = []
    prev: Optional[Candle] = None
    for c in candles:
        if prev is None:
            trs.append(c.high - c.low)
        else:
            trs.append(max(c.high - c.low, abs(c.high - prev.close), abs(c.low - prev.close)))
        prev = c
    return trs


def compute_range(symbol: str, candles: List[Candle], days: int) -> Optional[dict]:
    """ADR / ATR / widest over the last ``days`` completed sessions."""
    window = candles[-days:] if days else candles
    if not window:
        return None
    ranges = [c.high - c.low for c in window]
    trs = _true_ranges(window)
    widest = max(window, key=lambda c: c.high - c.low)
    last = window[-1]

    def rnd(x, nd=4):
        return round(x, nd) if x is not None else None

    return {
        "symbol": symbol.upper(),
        "sessions_used": len(window),
        "from_date": window[0].date.isoformat(),
        "to_date": window[-1].date.isoformat(),
        "last_close": last.close,
        "adr_dollars": rnd(_mean(ranges)),                                    # mean range $
        "adr_percent": rnd(_mean([(c.high - c.low) / c.close * 100
                                  for c in window if c.close])),              # mean range %
        "atr_dollars": rnd(_mean(trs)),
        "max_range_dollars": rnd(widest.high - widest.low),                   # widest single day $
        "max_range_percent": rnd((widest.high - widest.low) / widest.close * 100
                                 if widest.close else None),                  # widest single day %
        "max_range_date": widest.date.isoformat(),
    }
