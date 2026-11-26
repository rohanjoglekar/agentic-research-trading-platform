"""Alpaca options data client.

Mirrors the public surface of ``YFinanceClient`` for options chains so
``OptionsAgent`` and ``RecommendationAgent`` can use Alpaca instead of
yfinance without further changes.

Endpoints:

  Trading API   /v2/options/contracts          contracts + open interest
  Data API      /v1beta1/options/snapshots/SYM bid/ask/Greeks/last
  Data API      /v2/stocks/SYM/snapshot        underlying price
"""

import datetime
import re
import time
from typing import Optional

import pandas as pd
import requests
from loguru import logger

from config.settings import ALPACA_API_KEY, ALPACA_SECRET_KEY


_DATA_HOST = "https://data.alpaca.markets"
_TRADING_HOST = "https://paper-api.alpaca.markets"

_OCC_RE = re.compile(r"^([A-Z0-9]{1,6})(\d{2})(\d{2})(\d{2})([CP])(\d{8})$")


def _parse_occ(sym: str) -> Optional[dict]:
    m = _OCC_RE.match(sym)
    if not m:
        return None
    _root, yy, mm, dd, t, strike = m.groups()
    return {
        "expiration": f"20{yy}-{mm}-{dd}",
        "type": "call" if t == "C" else "put",
        "strike": int(strike) / 1000.0,
    }


class AlpacaOptionsClient:
    """Drop-in replacement for the options portion of YFinanceClient."""

    def __init__(self, api_key: str = None, api_secret: str = None,
                 cache_ttl_minutes: int = 30, request_timeout: int = 15):
        self.key = api_key or ALPACA_API_KEY
        self.secret = api_secret or ALPACA_SECRET_KEY
        if not self.key or not self.secret:
            raise RuntimeError("ALPACA_API_KEY / ALPACA_SECRET_KEY missing in environment")
        self._cache: dict = {}
        self._cache_ttl = cache_ttl_minutes * 60
        self._timeout = request_timeout
        self._headers = {
            "APCA-API-KEY-ID": self.key,
            "APCA-API-SECRET-KEY": self.secret,
            "Accept": "application/json",
        }
        # Optional Finnhub backfill for fundamentals + earnings dates
        self._finnhub = None
        try:
            from data.finnhub_client import FinnhubClient
            from config.settings import FINNHUB_API_KEY
            if FINNHUB_API_KEY:
                self._finnhub = FinnhubClient()
        except Exception as e:
            logger.warning(f"[alpaca] Finnhub backfill unavailable: {e}")

    # ─── caching ────────────────────────────────────────────────────────────

    def _cache_get(self, key: str):
        item = self._cache.get(key)
        if not item:
            return None
        ts, value = item
        if time.time() - ts > self._cache_ttl:
            self._cache.pop(key, None)
            return None
        return value

    def _cache_set(self, key: str, value) -> None:
        self._cache[key] = (time.time(), value)

    # ─── HTTP ───────────────────────────────────────────────────────────────

    def _get(self, host: str, path: str, params: dict = None) -> dict:
        url = f"{host}{path}"
        r = requests.get(url, headers=self._headers, params=params or {}, timeout=self._timeout)
        if r.status_code != 200:
            raise RuntimeError(f"Alpaca {r.status_code} on {path}: {r.text[:200]}")
        return r.json()

    # ─── Underlying price ──────────────────────────────────────────────────

    def get_current_price(self, symbol: str) -> Optional[float]:
        cached = self._cache_get(f"px_{symbol}")
        if cached is not None:
            return cached
        try:
            data = self._get(_DATA_HOST, f"/v2/stocks/{symbol}/snapshot")
            px = (data.get("latestTrade") or {}).get("p")
            if px is None:
                px = (data.get("dailyBar") or {}).get("c")
            if px is not None:
                px = float(px)
                self._cache_set(f"px_{symbol}", px)
            return px
        except Exception as e:
            logger.warning(f"[alpaca] price lookup failed for {symbol}: {e}")
            return None

    # ─── Contracts (paginated) ─────────────────────────────────────────────

    def _list_contracts(self, symbol: str) -> list:
        """All active contracts for symbol whose expiration is today or later.

        Cached for cache_ttl_minutes; the chain doesn't change intraday in any
        way that matters for our scoring.
        """
        cached = self._cache_get(f"contracts_{symbol}")
        if cached is not None:
            return cached
        today = datetime.date.today().isoformat()
        out: list = []
        complete = True
        page_token: Optional[str] = None
        for _ in range(30):  # hard ceiling — Alpaca paginates ~150-1000 per page
            params = {
                "underlying_symbols": symbol,
                "limit": 1000,
                "status": "active",
                "expiration_date_gte": today,
            }
            if page_token:
                params["page_token"] = page_token
            try:
                data = self._get(_TRADING_HOST, "/v2/options/contracts", params)
            except Exception as e:
                logger.warning(f"[alpaca] contracts page failed for {symbol}: {e}")
                complete = False
                break
            out.extend(data.get("option_contracts") or [])
            page_token = data.get("next_page_token")
            if not page_token:
                break
        if complete:
            # a failed fetch is not cached: an empty or partial chain would
            # otherwise stand in for the real one until the cache expires
            self._cache_set(f"contracts_{symbol}", out)
        return out

    def get_options_expirations(self, symbol: str) -> list:
        """Sorted list of unique expiration date strings (YYYY-MM-DD)."""
        contracts = self._list_contracts(symbol)
        exps = sorted({c["expiration_date"] for c in contracts if c.get("expiration_date")})
        if not exps:
            logger.warning(f"[alpaca] no options expirations for {symbol}")
        return exps

    # ─── Snapshots → chain DataFrames ──────────────────────────────────────

    def _list_snapshots(self, symbol: str, expiration: str) -> dict:
        """All snapshots for one expiration. Returns ``{ occ_symbol: snapshot }``."""
        out: dict = {}
        page_token: Optional[str] = None
        for _ in range(8):
            params = {"limit": 1000, "expiration_date": expiration}
            if page_token:
                params["page_token"] = page_token
            try:
                data = self._get(_DATA_HOST, f"/v1beta1/options/snapshots/{symbol}", params)
            except Exception as e:
                logger.warning(f"[alpaca] snapshots page failed for {symbol} {expiration}: {e}")
                break
            out.update(data.get("snapshots") or {})
            page_token = data.get("next_page_token")
            if not page_token:
                break
        return out

    # ─── Bars + technicals ────────────────────────────────────────────────

    def get_year_bars(self, symbol: str) -> list:
        """Daily bars for the past 12 months. Cached."""
        cached = self._cache_get(f"bars_{symbol}")
        if cached is not None:
            return cached
        start = (datetime.date.today() - datetime.timedelta(days=400)).isoformat()
        try:
            data = self._get(_DATA_HOST, f"/v2/stocks/{symbol}/bars",
                             {"timeframe": "1D", "start": start, "limit": 400, "adjustment": "all"})
            bars = data.get("bars") or []
            self._cache_set(f"bars_{symbol}", bars)
            return bars
        except Exception as e:
            logger.warning(f"[alpaca] bars failed for {symbol}: {e}")
            return []

    def get_technical_data(self, symbol: str) -> dict:
        """RSI, MAs, HV — computed from Alpaca daily bars."""
        bars = self.get_year_bars(symbol)
        if len(bars) < 25:
            return {}
        closes = [float(b["c"]) for b in bars]
        s = pd.Series(closes)

        ma50 = float(s.rolling(50).mean().iloc[-1]) if len(s) >= 50 else None
        ma200 = float(s.rolling(200).mean().iloc[-1]) if len(s) >= 200 else None
        current = closes[-1]

        # RSI(14)
        delta = s.diff()
        gain = delta.clip(lower=0).rolling(14).mean()
        loss = (-delta.clip(upper=0)).rolling(14).mean()
        last_gain, last_loss = gain.iloc[-1], loss.iloc[-1]
        if pd.isna(last_gain) or pd.isna(last_loss):
            rsi = None                                   # fewer than 14 changes
        elif last_loss == 0:
            rsi = 100.0 if last_gain > 0 else None       # no down day in the window
        else:
            rsi = float(100 - (100 / (1 + last_gain / last_loss)))

        # Annualized historical vol
        rets = s.pct_change().dropna()
        hv20 = float(rets.tail(20).std() * (252 ** 0.5)) if len(rets) >= 20 else None
        hv60 = float(rets.tail(60).std() * (252 ** 0.5)) if len(rets) >= 60 else None

        high_52w = max(closes[-252:]) if len(closes) >= 252 else max(closes)
        drawdown = (current - high_52w) / high_52w if high_52w else None

        return {
            "current_price": current,
            "ma_50": ma50,
            "ma_200": ma200,
            "above_ma50": current > ma50 if ma50 else None,
            "above_ma200": current > ma200 if ma200 else None,
            "rsi_14": round(rsi, 1) if rsi is not None else None,
            "hv_20": hv20,
            "hv_60": hv60,
            "drawdown_from_52w_high": drawdown,
            "recent_30d_return_pct": ((closes[-1] / closes[-21]) - 1) if len(closes) >= 22 else None,
            "52w_high": high_52w,
            "52w_low": min(closes[-252:]) if len(closes) >= 252 else min(closes),
        }

    def get_stock_info(self, symbol: str) -> dict:
        """Merged Alpaca (price, bars, MAs, 52w) + Finnhub (fundamentals)."""
        bars = self.get_year_bars(symbol)
        tech = self.get_technical_data(symbol)
        price = self.get_current_price(symbol)
        avg_vol_30 = None
        if len(bars) >= 30:
            avg_vol_30 = sum(int(b.get("v") or 0) for b in bars[-30:]) / 30

        # Start with Alpaca-only fields
        info = {
            "price": price,
            "market_cap": None,
            "avg_volume_30d": avg_vol_30,
            "beta": None,
            "revenue_growth": None,
            "profit_margin": None,
            "debt_to_equity": None,
            "current_ratio": None,
            "short_float": None,
            "recommendation": None,
            "analyst_target": None,
            "52w_high": tech.get("52w_high"),
            "52w_low": tech.get("52w_low"),
            "50d_avg": tech.get("ma_50"),
            "200d_avg": tech.get("ma_200"),
            "free_cash_flow": None,
            "total_cash": None,
            "total_debt": None,
            "sector": None,
        }
        # Backfill from Finnhub
        if self._finnhub:
            try:
                fh = self._finnhub.get_stock_info(symbol)
                for k, v in fh.items():
                    if v is not None and (info.get(k) is None or k in ("market_cap", "sector", "industry")):
                        info[k] = v
            except Exception as e:
                logger.warning(f"[alpaca] Finnhub merge failed for {symbol}: {e}")
        return info

    def days_to_earnings(self, symbol: str) -> Optional[int]:
        """Earnings calendar via Finnhub if available."""
        if self._finnhub:
            try:
                return self._finnhub.days_to_earnings(symbol)
            except Exception as e:
                logger.warning(f"[alpaca] earnings lookup failed for {symbol}: {e}")
        return None

    def get_asset_info(self, symbol: str) -> dict:
        """Basic asset metadata (name, exchange, tradable, marginable)."""
        cached = self._cache_get(f"asset_{symbol}")
        if cached is not None:
            return cached
        try:
            data = self._get(_TRADING_HOST, f"/v2/assets/{symbol}")
            self._cache_set(f"asset_{symbol}", data)
            return data
        except Exception as e:
            logger.warning(f"[alpaca] asset info failed for {symbol}: {e}")
            return {}

    def get_options_chain(self, symbol: str, expiration: str) -> dict:
        """Returns ``{"calls": DataFrame, "puts": DataFrame}`` matching the
        column shape that strategies/cash_secured_put.py and covered_call.py
        already expect: strike, bid, ask, lastPrice, openInterest, volume,
        impliedVolatility, delta, contractSymbol, inTheMoney.
        """
        cache_key = f"chain_{symbol}_{expiration}"
        cached = self._cache_get(cache_key)
        if cached is not None:
            return cached

        snapshots = self._list_snapshots(symbol, expiration)
        if not snapshots:
            empty = {"calls": pd.DataFrame(), "puts": pd.DataFrame()}
            return empty

        # contracts list carries the open_interest field; snapshots don't.
        contracts = self._list_contracts(symbol)
        oi_by_sym = {c["symbol"]: int(c.get("open_interest") or 0) for c in contracts}

        # underlying price for inTheMoney
        underlying = self.get_current_price(symbol)

        rows: list[dict] = []
        for occ_sym, snap in snapshots.items():
            parsed = _parse_occ(occ_sym)
            if not parsed or parsed["expiration"] != expiration:
                continue
            quote = snap.get("latestQuote") or {}
            trade = snap.get("latestTrade") or {}
            daily = snap.get("dailyBar") or {}
            greeks = snap.get("greeks") or {}
            strike = parsed["strike"]
            opt_type = parsed["type"]
            itm = (
                strike <= underlying if opt_type == "call" and underlying is not None
                else (strike >= underlying if opt_type == "put" and underlying is not None
                      else None)
            )
            rows.append({
                "contractSymbol": occ_sym,
                "_type": opt_type,  # internal — used to split calls vs puts
                "strike": strike,
                "bid": quote.get("bp"),
                "ask": quote.get("ap"),
                "lastPrice": trade.get("p"),
                "volume": daily.get("v") or 0,
                "openInterest": oi_by_sym.get(occ_sym, 0),
                "impliedVolatility": snap.get("impliedVolatility"),
                "delta": greeks.get("delta"),
                "inTheMoney": itm,
            })

        if not rows:
            return {"calls": pd.DataFrame(), "puts": pd.DataFrame()}

        df = pd.DataFrame(rows)
        calls = df[df["_type"] == "call"].drop(columns=["_type"]).reset_index(drop=True)
        puts = df[df["_type"] == "put"].drop(columns=["_type"]).reset_index(drop=True)
        result = {"calls": calls, "puts": puts}
        self._cache_set(cache_key, result)
        return result
