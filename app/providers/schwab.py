"""Schwab market-data adapter — option chains + underlying quote.

Read-only: this app never routes orders, it only reads the chain to compute the
net-dealer "C" target. The OAuth access token is the *shared* token pulled by
``SharedTokenProvider`` (same authorization the 0DTE / MM services use), so no
Schwab credentials live here.
"""
from __future__ import annotations

import datetime as dt
import logging
from typing import Callable, List, Optional, Tuple

import httpx

from app.config import settings
from app.net_dealer import StrikeRow
from app.range_stats import Candle

log = logging.getLogger("schwab")

TokenFn = Callable[[], Optional[str]]


def _f(v, default: float = 0.0) -> float:
    """Schwab uses -999.0 / None as 'no data' sentinels; coerce to a real float."""
    try:
        x = float(v)
    except (TypeError, ValueError):
        return default
    if x <= -999.0:
        return default
    return x


class SchwabChainClient:
    def __init__(self, token_fn: TokenFn, invalidate_fn: Optional[Callable[[], None]] = None,
                 base_url: Optional[str] = None, timeout: float = 12.0):
        self.token_fn = token_fn
        self.invalidate_fn = invalidate_fn or (lambda: None)
        self.base = (base_url or settings.schwab_base_url).rstrip("/")
        self.client = httpx.Client(timeout=timeout)
        self.last_error: Optional[str] = None

    def _headers(self) -> dict:
        tok = self.token_fn()
        return {"Authorization": f"Bearer {tok}", "Accept": "application/json"} if tok else {}

    def _get(self, url: str, params: Optional[dict] = None) -> Optional[httpx.Response]:
        """GET with one retry: a 401 means the shared token rotated — refresh."""
        r = None
        for attempt in (0, 1):
            try:
                r = self.client.get(url, headers=self._headers(), params=params)
            except Exception as exc:  # pragma: no cover - network
                self.last_error = f"network: {exc}"
                return None
            if r.status_code == 401 and attempt == 0:
                log.warning("schwab 401 — refreshing shared token and retrying")
                self.invalidate_fn()
                continue
            self.last_error = None if r.status_code == 200 else f"HTTP {r.status_code}"
            return r
        return r

    def _chains_raw(self, ticker: str, from_date: str, to_date: str,
                    strike_count: int, contract_type: str = "ALL") -> Optional[dict]:
        r = self._get(f"{self.base}/marketdata/v1/chains", params={
            "symbol": ticker.upper(),
            "contractType": contract_type,
            "fromDate": from_date,
            "toDate": to_date,
            "strikeCount": strike_count,
            "includeUnderlyingQuote": "true",
        })
        if r is None or r.status_code != 200:
            log.warning("schwab chains %s %s..%s -> %s", ticker, from_date, to_date,
                        r.status_code if r else "no response")
            return None
        try:
            return r.json()
        except Exception as exc:  # pragma: no cover - network
            log.warning("schwab chains %s parse failed: %s", ticker, exc)
            return None

    def get_expirations(self, ticker: str) -> List[str]:
        """Distinct expiration dates (YYYY-MM-DD) available for ``ticker``."""
        today = dt.date.today()
        horizon = today + dt.timedelta(days=settings.expiry_horizon_days)
        data = self._chains_raw(ticker, today.isoformat(), horizon.isoformat(),
                                strike_count=2)
        if not data:
            return []
        expiries = set()
        for map_key in ("callExpDateMap", "putExpDateMap"):
            for exp_key in (data.get(map_key) or {}).keys():
                expiries.add(exp_key.split(":")[0])   # "2026-07-31:0" -> "2026-07-31"
        return sorted(expiries)

    @staticmethod
    def _spot_from(data: dict) -> Optional[float]:
        spot = _f(data.get("underlyingPrice")) or None
        if not spot:
            under = data.get("underlying") or {}
            spot = _f(under.get("last") or under.get("mark") or under.get("close")) or None
        return spot

    @staticmethod
    def _rows_for_expiry(data: dict, expiry: str) -> List[StrikeRow]:
        """Merge the call + put maps of one expiry into a per-strike table."""
        rows: dict = {}

        def ingest(map_key: str, is_call: bool) -> None:
            for _exp, strikes in (data.get(map_key) or {}).items():
                if _exp.split(":")[0] != expiry:
                    continue
                for strike_str, legs in strikes.items():
                    if not legs:
                        continue
                    leg = legs[0]
                    strike = _f(leg.get("strikePrice")) or _f(strike_str)
                    if not strike:
                        continue
                    row = rows.setdefault(strike, StrikeRow(strike=strike))
                    oi = _f(leg.get("openInterest"))
                    vol = _f(leg.get("totalVolume"))
                    iv = _f(leg.get("volatility")) / 100.0     # Schwab IV is a percent
                    ask = _f(leg.get("ask") or leg.get("mark"))
                    if is_call:
                        row.call_oi, row.call_volume, row.call_iv, row.call_ask = oi, vol, iv, ask
                    else:
                        row.put_oi, row.put_volume, row.put_iv, row.put_ask = oi, vol, iv, ask

        ingest("callExpDateMap", True)
        ingest("putExpDateMap", False)
        return sorted(rows.values(), key=lambda x: x.strike)

    @staticmethod
    def _expiries_in(data: dict) -> List[str]:
        exps = set()
        for map_key in ("callExpDateMap", "putExpDateMap"):
            for exp_key in (data.get(map_key) or {}).keys():
                exps.add(exp_key.split(":")[0])
        return sorted(exps)

    def get_chain(self, ticker: str, expiry: str,
                  strike_count: Optional[int] = None) -> Tuple[List[StrikeRow], Optional[float]]:
        """Merged per-strike call+put table for one expiry, plus the underlying price."""
        sc = strike_count or settings.strike_count
        data = self._chains_raw(ticker, expiry, expiry, strike_count=sc)
        if not data:
            return [], None
        return self._rows_for_expiry(data, expiry), self._spot_from(data)

    def get_weekly_chain(self, ticker: str, week_index: int = 0,
                         within_days: Optional[int] = None,
                         strike_count: Optional[int] = None
                         ) -> Tuple[Optional[str], List[StrikeRow], Optional[float]]:
        """The ``week_index``-th nearest expiry (0 = this week, 1 = next week…)
        in a SINGLE chains call. Returns (expiry, rows, spot)."""
        sc = strike_count or settings.strike_count
        # Widen the window so it always spans far enough to include the target week.
        within = within_days if within_days is not None else settings.scan_within_days + 7 * week_index
        today = dt.date.today()
        end = today + dt.timedelta(days=within)
        data = self._chains_raw(ticker, today.isoformat(), end.isoformat(), strike_count=sc)
        if not data:
            return None, [], None
        exps = self._expiries_in(data)
        if len(exps) <= week_index:
            return None, [], None
        expiry = exps[week_index]
        return expiry, self._rows_for_expiry(data, expiry), self._spot_from(data)

    def get_nearest_chain(self, ticker: str, within_days: int = 9,
                          strike_count: Optional[int] = None
                          ) -> Tuple[Optional[str], List[StrikeRow], Optional[float]]:
        """Backward-compatible alias for the nearest (week 0) expiry."""
        return self.get_weekly_chain(ticker, 0, within_days, strike_count)

    def daily_history(self, symbol: str, days: int) -> List[Candle]:
        """Last ``days`` completed daily candles (for range stats).

        Vendored from Biggjuann/Range app/schwab.py. Returns [] on failure so the
        scanner degrades gracefully (range simply reads as unavailable).
        """
        symbol = symbol.strip().upper()
        need = max(days, 1)
        years = max(1, (need // 240) + 1)
        r = self._get(f"{self.base}/marketdata/v1/pricehistory", params={
            "symbol": symbol, "periodType": "year", "period": years,
            "frequencyType": "daily", "frequency": 1, "needExtendedHoursData": "false",
        })
        if r is None or r.status_code != 200:
            log.warning("schwab pricehistory %s -> %s", symbol, r.status_code if r else "no response")
            return []
        try:
            body = r.json()
        except Exception as exc:  # pragma: no cover - network
            log.warning("schwab pricehistory %s bad JSON: %s", symbol, exc)
            return []
        if body.get("empty") or not body.get("candles"):
            return []
        candles = [Candle.from_schwab(c) for c in body["candles"]
                   if c.get("high") is not None and c.get("low") is not None]
        candles.sort(key=lambda c: c.date)
        today = dt.datetime.now(tz=dt.timezone.utc).date()
        completed = [c for c in candles if c.date < today] or candles
        return completed[-need:]
