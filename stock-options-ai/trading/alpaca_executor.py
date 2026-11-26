"""Thin Alpaca paper-trading wrapper used by the wheel runner.

Uses raw HTTP because the alpaca-py SDK's option-trading surface lags the
underlying REST API.
"""
from typing import Optional

import requests

from config.settings import ALPACA_API_KEY, ALPACA_SECRET_KEY


_TRADING_HOST_DEFAULT = "https://paper-api.alpaca.markets"


def build_occ(ticker: str, expiration: str, opt_type: str, strike: float) -> str:
    """Construct a 21-char OCC option symbol.

    Example: ``build_occ("F", "2026-06-18", "put", 11.0) → "F     260618P00011000"``,
    but with no padding spaces in the actual call format Alpaca accepts:
    ``F260618P00011000``.
    """
    yy = expiration[2:4]
    mm = expiration[5:7]
    dd = expiration[8:10]
    cp = "P" if opt_type.lower().startswith("p") else "C"
    strike_int = int(round(float(strike) * 1000))
    return f"{ticker.upper()}{yy}{mm}{dd}{cp}{strike_int:08d}"


class AlpacaTrader:
    def __init__(self, api_key: Optional[str] = None, api_secret: Optional[str] = None,
                 base_url: str = _TRADING_HOST_DEFAULT, timeout: int = 15,
                 allow_env_fallback: bool = True):
        # allow_env_fallback=False → explicit credentials are REQUIRED; never fall back
        # to the wheel's ALPACA_API_KEY/SECRET env keys (used by the alpha runner so a
        # missing dedicated key can never silently trade the wheel's account).
        if not allow_env_fallback and not (api_key and api_secret):
            raise ValueError("dedicated credentials required (allow_env_fallback=False)")
        self.key = api_key or ALPACA_API_KEY
        self.secret = api_secret or ALPACA_SECRET_KEY
        self.base = base_url.rstrip("/")
        self.data_base = "https://data.alpaca.markets"   # market-data host (free IEX feed on paper)
        self.timeout = timeout
        if not self.key or not self.secret:
            raise RuntimeError("ALPACA_API_KEY / ALPACA_SECRET_KEY missing")
        self.headers = {
            "APCA-API-KEY-ID": self.key,
            "APCA-API-SECRET-KEY": self.secret,
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    def _get(self, path: str, params: dict = None) -> dict | list:
        r = requests.get(f"{self.base}{path}", headers=self.headers, params=params or {}, timeout=self.timeout)
        if r.status_code >= 400:
            raise RuntimeError(f"Alpaca {r.status_code} on GET {path}: {r.text[:200]}")
        return r.json()

    def _post(self, path: str, body: dict) -> dict:
        r = requests.post(f"{self.base}{path}", headers=self.headers, json=body, timeout=self.timeout)
        if r.status_code >= 400:
            raise RuntimeError(f"Alpaca {r.status_code} on POST {path}: {r.text[:300]}")
        return r.json()

    def latest_price(self, symbol: str) -> Optional[float]:
        """Latest trade price for an equity/ETF via the market-data API (free IEX feed
        on paper). Returns None on any failure so callers can fall back gracefully —
        used by the alpha runner to price the anchor (VOO/QQQ) and reserve (BIL) ETFs
        without a yfinance/price-cache dependency."""
        try:
            r = requests.get(f"{self.data_base}/v2/stocks/{symbol.upper()}/trades/latest",
                             headers=self.headers, params={"feed": "iex"}, timeout=self.timeout)
            if r.status_code >= 400:
                return None
            px = float((r.json().get("trade") or {}).get("p") or 0)
            return px or None
        except Exception:
            return None

    # ─── Account / state ────────────────────────────────────────────────────

    def account(self) -> dict:
        return self._get("/v2/account")

    def clock(self) -> dict:
        """Market clock (GET /v2/clock): {'is_open': bool, 'next_open': …, 'next_close': …}."""
        return self._get("/v2/clock")

    def list_positions(self) -> list:
        return self._get("/v2/positions")

    def list_open_orders(self) -> list:
        return self._get("/v2/orders", params={"status": "open", "limit": 200})

    def list_recent_orders(self, status: str = "all", limit: int = 100) -> list:
        return self._get("/v2/orders", params={"status": status, "limit": limit, "direction": "desc"})

    # ─── Orders ─────────────────────────────────────────────────────────────

    def submit_option_limit(self, occ_symbol: str, qty: int, side: str, limit_price: float,
                            time_in_force: str = "day") -> dict:
        body = {
            "symbol": occ_symbol,
            "qty": str(qty),
            "side": side,
            "type": "limit",
            "time_in_force": time_in_force,
            "limit_price": f"{limit_price:.2f}",
        }
        return self._post("/v2/orders", body)

    # ─── Equity orders (used by the alpha portfolio runner) ──────────────────

    def submit_equity_notional(self, symbol: str, notional: float, side: str,
                               time_in_force: str = "day") -> dict:
        """Market order for a dollar amount (fractional shares). side: buy|sell."""
        body = {
            "symbol": symbol.upper(),
            "notional": f"{abs(notional):.2f}",
            "side": side,
            "type": "market",
            "time_in_force": time_in_force,
        }
        return self._post("/v2/orders", body)

    def submit_equity_qty(self, symbol: str, qty: float, side: str,
                          time_in_force: str = "day") -> dict:
        """Market order for a share quantity (supports fractional). side: buy|sell."""
        body = {
            "symbol": symbol.upper(),
            "qty": f"{abs(qty)}",
            "side": side,
            "type": "market",
            "time_in_force": time_in_force,
        }
        return self._post("/v2/orders", body)

    def close_position(self, symbol: str) -> dict:
        """Liquidate an entire equity position."""
        r = requests.delete(f"{self.base}/v2/positions/{symbol.upper()}",
                            headers=self.headers, timeout=self.timeout)
        if r.status_code >= 400:
            raise RuntimeError(f"Alpaca {r.status_code} on DELETE position {symbol}: {r.text[:200]}")
        return r.json()
