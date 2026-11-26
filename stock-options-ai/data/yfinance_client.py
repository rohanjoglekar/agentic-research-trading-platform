"""
data/yfinance_client.py

Primary data client using yfinance (free, unofficial Yahoo Finance API).
Provides: stock info, price history, options chains, financials, earnings dates.

NOTE: yfinance is unofficial and can break without notice. It's fine for MVP
and personal use. For production, add Tradier or Polygon as a fallback.
"""

import time
import datetime
from typing import Optional
import pandas as pd
import numpy as np
import yfinance as yf
from loguru import logger


class YFinanceClient:
    """
    Wrapper around yfinance with caching, error handling, and
    data normalization for the options research pipeline.
    """

    def __init__(self, cache_ttl_minutes: int = 60):
        self._cache: dict = {}
        self._cache_ttl = cache_ttl_minutes * 60  # seconds

    # ──────────────────────────────────────────────────────────────────────────
    # Internal helpers
    # ──────────────────────────────────────────────────────────────────────────

    def _is_stale(self, key: str) -> bool:
        if key not in self._cache:
            return True
        ts, _ = self._cache[key]
        return (time.time() - ts) > self._cache_ttl

    def _set(self, key: str, value) -> None:
        self._cache[key] = (time.time(), value)

    def _get(self, key: str):
        if self._is_stale(key):
            return None
        return self._cache[key][1]

    def _ticker(self, symbol: str) -> yf.Ticker:
        return yf.Ticker(symbol)

    # ──────────────────────────────────────────────────────────────────────────
    # Stock Info
    # ──────────────────────────────────────────────────────────────────────────

    def get_stock_info(self, symbol: str) -> dict:
        """
        Returns a flat dict with all key stock metadata.
        Catches and logs errors gracefully (some fields may be None).
        """
        key = f"info_{symbol}"
        cached = self._get(key)
        if cached:
            return cached

        try:
            info = self._ticker(symbol).info
            result = {
                "symbol": symbol,
                "name": info.get("longName", symbol),
                "sector": info.get("sector", "Unknown"),
                "industry": info.get("industry", "Unknown"),
                "market_cap": info.get("marketCap"),
                "price": info.get("currentPrice") or info.get("regularMarketPrice"),
                "previous_close": info.get("previousClose"),
                "volume": info.get("volume"),
                "avg_volume_30d": info.get("averageVolume"),
                "avg_volume_10d": info.get("averageVolume10days"),
                "beta": info.get("beta"),
                "pe_ratio": info.get("trailingPE"),
                "forward_pe": info.get("forwardPE"),
                "pb_ratio": info.get("priceToBook"),
                "ps_ratio": info.get("priceToSalesTrailingTwelveMonths"),
                "debt_to_equity": info.get("debtToEquity"),
                "current_ratio": info.get("currentRatio"),
                "quick_ratio": info.get("quickRatio"),
                "revenue_growth": info.get("revenueGrowth"),          # YoY
                "earnings_growth": info.get("earningsGrowth"),
                "profit_margin": info.get("profitMargins"),
                "operating_margin": info.get("operatingMargins"),
                "return_on_equity": info.get("returnOnEquity"),
                "return_on_assets": info.get("returnOnAssets"),
                "free_cash_flow": info.get("freeCashflow"),
                "total_cash": info.get("totalCash"),
                "total_debt": info.get("totalDebt"),
                "short_float": info.get("shortPercentOfFloat"),
                "short_ratio": info.get("shortRatio"),
                "52w_high": info.get("fiftyTwoWeekHigh"),
                "52w_low": info.get("fiftyTwoWeekLow"),
                "50d_avg": info.get("fiftyDayAverage"),
                "200d_avg": info.get("twoHundredDayAverage"),
                "analyst_target": info.get("targetMeanPrice"),
                "recommendation": info.get("recommendationKey"),  # buy/hold/sell
                "number_of_analysts": info.get("numberOfAnalystOpinions"),
                "earnings_date": self._parse_earnings_date(info),
                "ex_dividend_date": info.get("exDividendDate"),
                "dividend_yield": info.get("dividendYield"),
            }
            self._set(key, result)
            return result
        except Exception as e:
            logger.warning(f"[yfinance] Error fetching info for {symbol}: {e}")
            return {"symbol": symbol, "error": str(e)}

    def _parse_earnings_date(self, info: dict) -> Optional[datetime.date]:
        """Extract next earnings date from yfinance info dict."""
        try:
            ed = info.get("earningsTimestamp") or info.get("earningsDate")
            if isinstance(ed, (int, float)):
                return datetime.date.fromtimestamp(ed)
            if isinstance(ed, list) and ed:
                return datetime.date.fromtimestamp(ed[0])
        except Exception:
            pass
        return None

    # ──────────────────────────────────────────────────────────────────────────
    # Price History
    # ──────────────────────────────────────────────────────────────────────────

    def get_price_history(
        self, symbol: str, period: str = "1y", interval: str = "1d"
    ) -> pd.DataFrame:
        """
        Returns OHLCV DataFrame for the symbol.
        period: 1d, 5d, 1mo, 3mo, 6mo, 1y, 2y, 5y, 10y, ytd, max
        interval: 1m, 2m, 5m, 15m, 30m, 60m, 90m, 1h, 1d, 5d, 1wk, 1mo
        """
        key = f"hist_{symbol}_{period}_{interval}"
        cached = self._get(key)
        if cached is not None:
            return cached

        try:
            df = self._ticker(symbol).history(period=period, interval=interval)
            df.index = pd.to_datetime(df.index).tz_localize(None)
            self._set(key, df)
            return df
        except Exception as e:
            logger.warning(f"[yfinance] Error fetching history for {symbol}: {e}")
            return pd.DataFrame()

    def get_current_price(self, symbol: str) -> Optional[float]:
        """Fast path for current price only."""
        info = self.get_stock_info(symbol)
        return info.get("price")

    # ──────────────────────────────────────────────────────────────────────────
    # Technical Indicators (computed from price history)
    # ──────────────────────────────────────────────────────────────────────────

    def get_technical_data(self, symbol: str) -> dict:
        """
        Computes key technical indicators from price history.
        Returns MA50, MA200, RSI14, HV20, HV60, trend direction.
        """
        df = self.get_price_history(symbol, period="1y")
        if df.empty:
            return {}

        close = df["Close"]

        # Moving averages
        ma50 = close.rolling(50).mean().iloc[-1] if len(close) >= 50 else None
        ma200 = close.rolling(200).mean().iloc[-1] if len(close) >= 200 else None
        current = close.iloc[-1]

        # RSI (14-period)
        rsi = self._calc_rsi(close, 14)

        # Historical volatility (annualized)
        returns = close.pct_change().dropna()
        hv20 = returns.tail(20).std() * np.sqrt(252) if len(returns) >= 20 else None
        hv60 = returns.tail(60).std() * np.sqrt(252) if len(returns) >= 60 else None

        # Drawdown from 52-week high
        high_52w = close.tail(252).max() if len(close) >= 252 else close.max()
        drawdown_pct = (current - high_52w) / high_52w if high_52w else None

        # Trend direction
        above_50 = current > ma50 if ma50 else None
        above_200 = current > ma200 if ma200 else None

        return {
            "current_price": current,
            "ma_50": ma50,
            "ma_200": ma200,
            "above_ma50": above_50,
            "above_ma200": above_200,
            "rsi_14": rsi,
            "hv_20": hv20,
            "hv_60": hv60,
            "drawdown_from_52w_high": drawdown_pct,
        }

    @staticmethod
    def _calc_rsi(prices: pd.Series, period: int = 14) -> Optional[float]:
        """Calculate RSI for a price series."""
        try:
            delta = prices.diff()
            gain = delta.clip(lower=0).rolling(period).mean()
            loss = (-delta.clip(upper=0)).rolling(period).mean()
            rs = gain / loss.replace(0, np.nan)
            rsi = 100 - (100 / (1 + rs))
            return round(float(rsi.iloc[-1]), 1)
        except Exception:
            return None

    # ──────────────────────────────────────────────────────────────────────────
    # Options Chains
    # ──────────────────────────────────────────────────────────────────────────

    def get_options_expirations(self, symbol: str) -> list:
        """Returns list of available expiration date strings."""
        try:
            return list(self._ticker(symbol).options)
        except Exception as e:
            logger.warning(f"[yfinance] No options for {symbol}: {e}")
            return []

    def get_options_chain(self, symbol: str, expiration: str) -> dict:
        """
        Returns {"calls": DataFrame, "puts": DataFrame} for a given expiration.
        Columns: contractSymbol, strike, lastPrice, bid, ask, volume,
                 openInterest, impliedVolatility, inTheMoney
        """
        key = f"chain_{symbol}_{expiration}"
        cached = self._get(key)
        if cached:
            return cached

        try:
            chain = self._ticker(symbol).option_chain(expiration)
            result = {
                "calls": chain.calls.copy(),
                "puts": chain.puts.copy(),
            }
            self._set(key, result)
            return result
        except Exception as e:
            logger.warning(f"[yfinance] Options chain error {symbol} {expiration}: {e}")
            return {"calls": pd.DataFrame(), "puts": pd.DataFrame()}

    def get_atm_iv(self, symbol: str, expiration: str) -> Optional[float]:
        """Returns the at-the-money implied volatility for an expiration."""
        try:
            price = self.get_current_price(symbol)
            if not price:
                return None
            chain = self.get_options_chain(symbol, expiration)
            puts = chain["puts"]
            if puts.empty:
                return None
            puts = puts.copy()
            puts["moneyness"] = abs(puts["strike"] - price)
            atm = puts.sort_values("moneyness").iloc[0]
            return float(atm["impliedVolatility"])
        except Exception as e:
            logger.warning(f"[yfinance] ATM IV error {symbol}: {e}")
            return None

    def get_iv_rank(self, symbol: str, current_iv: float) -> Optional[float]:
        """
        Estimates IV rank using 1-year of historical ATM IV.
        Since we can't easily store 52w of IV history, we approximate
        using HV20/HV60 and a scaling factor.

        For a real IV rank, store weekly ATM IV and compute from that series.
        This function returns a reasonable approximation.
        """
        tech = self.get_technical_data(symbol)
        hv20 = tech.get("hv_20")
        hv60 = tech.get("hv_60")

        if not hv20 or not hv60:
            return None

        # Rough approximation: IV rank relative to recent HV range
        hv_min = min(hv20, hv60) * 0.7   # Assume IV can go 30% below recent HV
        hv_max = max(hv20, hv60) * 1.8   # Assume IV can spike 80% above recent HV
        rank = (current_iv - hv_min) / (hv_max - hv_min) * 100
        return round(max(0, min(100, rank)), 1)

    # ──────────────────────────────────────────────────────────────────────────
    # Earnings Calendar
    # ──────────────────────────────────────────────────────────────────────────

    def days_to_earnings(self, symbol: str) -> Optional[int]:
        """Returns number of days until next earnings date. None if unknown."""
        info = self.get_stock_info(symbol)
        ed = info.get("earnings_date")
        if not ed:
            return None
        today = datetime.date.today()
        delta = (ed - today).days
        return max(0, delta)

    # ──────────────────────────────────────────────────────────────────────────
    # News
    # ──────────────────────────────────────────────────────────────────────────

    def get_news(self, symbol: str, max_items: int = 5) -> list:
        """Returns list of recent news dicts with title, publisher, link, date."""
        try:
            raw = self._ticker(symbol).news
            result = []
            for item in (raw or [])[:max_items]:
                result.append({
                    "title": item.get("title", ""),
                    "publisher": item.get("publisher", ""),
                    "link": item.get("link", ""),
                    "date": datetime.datetime.fromtimestamp(
                        item.get("providerPublishTime", 0)
                    ).strftime("%Y-%m-%d"),
                })
            return result
        except Exception as e:
            logger.warning(f"[yfinance] News error for {symbol}: {e}")
            return []

    # ──────────────────────────────────────────────────────────────────────────
    # Bulk Screening Helper
    # ──────────────────────────────────────────────────────────────────────────

    def get_bulk_snapshot(self, symbols: list) -> pd.DataFrame:
        """
        Fast bulk price/volume snapshot for a list of symbols.
        Uses yfinance download() for efficiency.
        Returns DataFrame indexed by symbol with price, volume, change%.
        """
        try:
            data = yf.download(
                symbols,
                period="5d",
                interval="1d",
                auto_adjust=True,
                progress=False,
                threads=True,
            )
            if data.empty:
                return pd.DataFrame()

            # Get latest close and volume
            close = data["Close"].iloc[-1]
            volume = data["Volume"].iloc[-1]
            prev_close = data["Close"].iloc[-2] if len(data) >= 2 else close

            df = pd.DataFrame({
                "price": close,
                "volume": volume,
                "change_pct": (close - prev_close) / prev_close,
            })
            return df
        except Exception as e:
            logger.error(f"[yfinance] Bulk snapshot error: {e}")
            return pd.DataFrame()
