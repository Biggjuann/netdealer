"""Synthetic chain provider so the app (and CI, and the GitHub Pages demo) runs
with zero network access. Shapes the open interest the way the framework expects:
call OI skewed above spot, put OI skewed below, an IV smile, and a couple of near
expirations — so the computed "C" target lands near, but not exactly on, spot.
"""
from __future__ import annotations

import datetime as dt
import math
from typing import List, Optional, Tuple

from app.net_dealer import StrikeRow

# A few anchor prices so well-known tickers look realistic; anything else gets a
# deterministic price derived from its symbol.
_ANCHORS = {
    "MU": 840.0, "SPY": 560.0, "QQQ": 490.0, "AAPL": 232.0, "NVDA": 178.0,
    "TSLA": 340.0, "AMD": 168.0, "META": 720.0, "AMZN": 218.0, "GOOGL": 200.0,
}


def _anchor_price(ticker: str) -> float:
    t = ticker.upper()
    if t in _ANCHORS:
        return _ANCHORS[t]
    h = sum(ord(c) for c in t)
    return 50.0 + (h % 400)


def _step(price: float) -> float:
    if price >= 500:
        return 5.0
    if price >= 100:
        return 2.5
    if price >= 25:
        return 1.0
    return 0.5


def _next_fridays(n: int) -> List[str]:
    today = dt.date.today()
    out, d = [], today
    while len(out) < n:
        if d.weekday() == 4 and d >= today:   # Friday
            out.append(d.isoformat())
        d += dt.timedelta(days=1)
    return out


class MockChainProvider:
    def get_expirations(self, ticker: str) -> List[str]:
        return _next_fridays(5)

    def get_chain(self, ticker: str, expiry: str,
                  strike_count: Optional[int] = None) -> Tuple[List[StrikeRow], Optional[float]]:
        spot = _anchor_price(ticker)
        step = _step(spot)
        n = (strike_count or 60) // 2
        # Center the ladder on the nearest step to spot.
        center = round(spot / step) * step
        rows: List[StrikeRow] = []
        for i in range(-n, n + 1):
            k = round(center + i * step, 2)
            if k <= 0:
                continue
            moneyness = (k - spot) / spot
            # Call OI peaks a bit ABOVE spot; put OI peaks a bit BELOW spot.
            call_oi = 4000 * math.exp(-((moneyness - 0.03) ** 2) / (2 * 0.05 ** 2))
            put_oi = 4200 * math.exp(-((moneyness + 0.035) ** 2) / (2 * 0.05 ** 2))
            # Round contract lots + a little structural noise via a cheap hash.
            jitter = ((int(abs(k) * 7) % 13) - 6) * 30
            call_oi = max(0.0, round(call_oi + jitter))
            put_oi = max(0.0, round(put_oi - jitter))
            iv = 0.30 + 1.6 * moneyness ** 2      # smile
            rows.append(StrikeRow(
                strike=k,
                call_oi=call_oi, put_oi=put_oi,
                call_volume=round(call_oi * 0.4), put_volume=round(put_oi * 0.4),
                call_iv=round(iv, 4), put_iv=round(iv + 0.01, 4),
                call_ask=round(max(0.05, (spot - k) + spot * iv * 0.04), 2),
                put_ask=round(max(0.05, (k - spot) + spot * iv * 0.04), 2),
            ))
        return rows, spot
