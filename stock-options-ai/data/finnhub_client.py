"""Finnhub fundamentals client. Free tier: 60 req/min.

Used as a backfill for the fundamentals fields the screener needs:
profit margin, revenue growth, debt-to-equity, current ratio, market cap,
short interest, and (importantly) days-to-earnings — none of which Alpaca
exposes.
"""

import datetime
import time
from typing import Optional

import requests
from loguru import logger

from config.settings import FINNHUB_API_KEY


_HOST = "https://finnhub.io/api/v1"


class FinnhubClient:
    def __init__(self, api_key: Optional[str] = None, cache_ttl_minutes: int = 360,
                 timeout: int = 10):
        self.key = api_key or FINNHUB_API_KEY
        if not self.key:
            raise RuntimeError("FINNHUB_API_KEY missing")
        self.timeout = timeout
        self._cache: dict = {}
        self._cache_ttl = cache_ttl_minutes * 60
        # crude rate limiter — Finnhub free tier is 60/min
        self._last_call_ts = 0.0
        self._min_gap = 1.05  # seconds between calls

    def _get(self, path: str, params: dict = None) -> dict:
        # Soft rate limit so we don't hit 429s
        gap = time.time() - self._last_call_ts
        if gap < self._min_gap:
            time.sleep(self._min_gap - gap)
        params = dict(params or {})
        # The key goes in a header, never in the URL: `requests` puts the full
        # URL in its error text, and callers log those errors.
        headers = {"X-Finnhub-Token": self.key}
        r = requests.get(f"{_HOST}{path}", params=params, headers=headers, timeout=self.timeout)
        self._last_call_ts = time.time()
        if r.status_code == 429:
            logger.warning("[finnhub] 429 rate limit — backing off 6s")
            time.sleep(6)
            r = requests.get(f"{_HOST}{path}", params=params, headers=headers, timeout=self.timeout)
        if r.status_code >= 400:
            raise RuntimeError(f"Finnhub {r.status_code} on {path}: {r.text[:200]}")
        return r.json()

    def _cache_get(self, key: str):
        item = self._cache.get(key)
        if item and time.time() - item[0] < self._cache_ttl:
            return item[1]
        return None

    def _cache_set(self, key: str, value):
        self._cache[key] = (time.time(), value)

    # ─── Fundamentals ───────────────────────────────────────────────────────

    def get_metrics(self, symbol: str) -> dict:
        """Return the raw ``metric`` dict from /stock/metric, cached."""
        key = f"metrics_{symbol}"
        cached = self._cache_get(key)
        if cached is not None:
            return cached
        try:
            data = self._get("/stock/metric", {"symbol": symbol, "metric": "all"})
            metrics = data.get("metric") or {}
            self._cache_set(key, metrics)
            return metrics
        except Exception as e:
            logger.warning(f"[finnhub] metrics failed for {symbol}: {e}")
            return {}

    def get_company_info(self, symbol: str) -> dict:
        """Return /stock/profile2 (sector, industry, market cap, etc.)."""
        key = f"profile_{symbol}"
        cached = self._cache_get(key)
        if cached is not None:
            return cached
        try:
            data = self._get("/stock/profile2", {"symbol": symbol})
            self._cache_set(key, data)
            return data or {}
        except Exception as e:
            logger.warning(f"[finnhub] profile failed for {symbol}: {e}")
            return {}

    def days_to_earnings(self, symbol: str) -> Optional[int]:
        """Days until next earnings, or None if unknown."""
        key = f"earnings_{symbol}"
        cached = self._cache_get(key)
        if cached is not None:
            return cached
        try:
            today = datetime.date.today()
            far = today + datetime.timedelta(days=365)
            data = self._get(
                "/calendar/earnings",
                {"symbol": symbol, "from": today.isoformat(), "to": far.isoformat()},
            )
            cal = data.get("earningsCalendar") or []
            future = sorted([c["date"] for c in cal if c.get("date")])
            if not future:
                self._cache_set(key, None)
                return None
            d = datetime.date.fromisoformat(future[0])
            days = (d - today).days
            self._cache_set(key, days)
            return days
        except Exception as e:
            logger.warning(f"[finnhub] earnings failed for {symbol}: {e}")
            return None

    # ─── Convenience: combined info dict in screener-shaped form ───────────

    def get_stock_info(self, symbol: str) -> dict:
        """Return fields the screener expects, in screener naming convention.

        Combines /stock/metric + /stock/profile2 into one shape compatible
        with the existing _fetch_universe loop.
        """
        m = self.get_metrics(symbol)
        p = self.get_company_info(symbol)

        def _f(key):
            v = m.get(key)
            try:
                return float(v) if v is not None else None
            except (TypeError, ValueError):
                return None

        market_cap = (p.get("marketCapitalization") or 0) * 1_000_000  # Finnhub returns in $M
        if market_cap == 0:
            market_cap = None

        return {
            "price": None,  # taken from Alpaca elsewhere
            "market_cap": market_cap,
            "avg_volume_30d": _f("10DayAverageTradingVolume") and _f("10DayAverageTradingVolume") * 1_000_000,
            "beta": _f("beta"),
            "revenue_growth": _f("revenueGrowthTTMYoy") and _f("revenueGrowthTTMYoy") / 100,
            "profit_margin": _f("netProfitMarginTTM") and _f("netProfitMarginTTM") / 100,
            "debt_to_equity": _f("totalDebt/totalEquityAnnual"),
            "current_ratio": _f("currentRatioAnnual"),
            "short_float": (_f("shortPercentSharesOutstandingFloat") or 0) / 100 if _f("shortPercentSharesOutstandingFloat") else None,
            "recommendation": None,
            "analyst_target": _f("priceTargetMean"),
            "52w_high": _f("52WeekHigh"),
            "52w_low": _f("52WeekLow"),
            "50d_avg": None,  # we get this from Alpaca bars
            "200d_avg": None,
            "free_cash_flow": _f("freeCashFlowTTM"),
            "total_cash": _f("cashAndCashEquivalentsTTM"),
            "total_debt": _f("totalDebtTTM"),
            "sector": p.get("finnhubIndustry"),
            "industry": p.get("finnhubIndustry"),
        }
