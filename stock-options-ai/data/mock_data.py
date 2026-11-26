"""
data/mock_data.py

Generates plausible mock stock and options-chain data, in the same shape the
live clients return, so the pipeline can be run end to end offline.

Use this to rehearse the scan without market-data keys. The numbers are random
(unseeded), so two runs differ; nothing here is a market price.

Set USE_MOCK_OPTIONS=true in .env to enable, or run `jobs/daily_scan.py`
without `--live`.
"""

import datetime
import random
import numpy as np
import pandas as pd
from typing import Optional


def generate_mock_options_chain(
    symbol: str,
    stock_price: float,
    iv: float = 0.55,
    dte: int = 30,
    num_strikes: int = 10,
) -> dict:
    """
    Generates a realistic mock options chain for a given stock.

    Uses Black-Scholes-like approximation to generate realistic premiums.
    Returns {"calls": DataFrame, "puts": DataFrame} — same format as yfinance.

    Args:
        symbol: ticker symbol
        stock_price: current stock price
        iv: implied volatility (annualized, e.g. 0.55 = 55%)
        dte: days to expiration
        num_strikes: how many strikes to generate per side
    """
    expiration = (datetime.date.today() + datetime.timedelta(days=dte)).strftime("%Y-%m-%d")

    # Generate strike range around ATM
    # Range: ±20% from current price, spaced by ~2.5-5% increments
    strike_spacing = max(0.5, round(stock_price * 0.025, 1))  # ~2.5% of price
    if stock_price > 50:
        strike_spacing = round(strike_spacing / 2.5) * 2.5  # round to nearest $2.5

    atm_strike = round(stock_price / strike_spacing) * strike_spacing
    strikes = [
        round(atm_strike + (i - num_strikes // 2) * strike_spacing, 2)
        for i in range(num_strikes)
    ]
    strikes = [s for s in strikes if s > 0]

    calls_data = []
    puts_data = []

    t = dte / 365.0  # time in years
    r = 0.05         # risk-free rate assumption

    for strike in strikes:
        call_price, put_price = _bs_price(stock_price, strike, t, r, iv)

        # Add realistic bid-ask spread (tighter for ATM, wider for deep OTM)
        moneyness = abs(stock_price - strike) / stock_price
        spread_multiplier = 1.0 + moneyness * 3.0  # wider spread OTM
        spread = max(0.01, round(call_price * 0.08 * spread_multiplier, 2))

        call_bid = max(0.01, round(call_price - spread / 2, 2))
        call_ask = round(call_price + spread / 2, 2)
        put_bid = max(0.01, round(put_price - spread / 2, 2))
        put_ask = round(put_price + spread / 2, 2)

        # Mock volume/OI — higher near ATM
        atm_proximity = max(0.1, 1 - moneyness * 5)
        base_oi = int(random.uniform(200, 5000) * atm_proximity)
        base_vol = int(base_oi * random.uniform(0.05, 0.3))

        call_iv = iv * (1 + moneyness * 0.1 * random.uniform(-1, 1))  # IV smile
        put_iv = iv * (1 + moneyness * 0.15)  # puts have vol skew (steeper)
        put_iv = min(put_iv, 3.0)

        call_delta = _approx_delta(stock_price, strike, t, r, iv, "call")
        put_delta = _approx_delta(stock_price, strike, t, r, iv, "put")

        calls_data.append({
            "contractSymbol": f"{symbol}{expiration.replace('-','')}C{int(strike*1000):08d}",
            "strike": strike,
            "lastPrice": round(call_price * random.uniform(0.95, 1.05), 2),
            "bid": call_bid,
            "ask": call_ask,
            "volume": base_vol,
            "openInterest": base_oi,
            "impliedVolatility": round(call_iv, 4),
            "inTheMoney": strike < stock_price,
            "delta": round(call_delta, 3),
            "gamma": round(max(0.001, _approx_gamma(stock_price, strike, t, iv)), 4),
        })

        puts_data.append({
            "contractSymbol": f"{symbol}{expiration.replace('-','')}P{int(strike*1000):08d}",
            "strike": strike,
            "lastPrice": round(put_price * random.uniform(0.95, 1.05), 2),
            "bid": put_bid,
            "ask": put_ask,
            "volume": int(base_vol * 1.2),   # puts typically have more volume
            "openInterest": int(base_oi * 1.1),
            "impliedVolatility": round(put_iv, 4),
            "inTheMoney": strike > stock_price,
            "delta": round(put_delta, 3),
            "gamma": round(max(0.001, _approx_gamma(stock_price, strike, t, iv)), 4),
        })

    return {
        "calls": pd.DataFrame(calls_data),
        "puts": pd.DataFrame(puts_data),
        "expiration": expiration,
        "underlying_price": stock_price,
        "iv": iv,
        "dte": dte,
        "is_mock": True,
    }


def generate_mock_universe(tickers: Optional[list] = None) -> pd.DataFrame:
    """
    Generates a realistic mock stock universe for MVP testing.
    Returns a DataFrame with all screener-relevant fields.
    """
    if tickers is None:
        tickers = [
            "SOFI", "PLTR", "RIVN", "HOOD", "COIN", "SNAP", "UBER",
            "PINS", "RBLX", "DKNG", "AMD", "INTC", "F", "BAC", "WFC",
            "AAL", "DAL", "NCLH", "CCL", "MGM", "MARA", "RIOT",
        ]

    rows = []
    for sym in tickers:
        price = random.uniform(8, 95)
        iv = random.uniform(0.30, 1.20)
        market_cap = random.uniform(500e6, 50e9)
        avg_vol = random.uniform(2e6, 50e6)
        dte_next_earnings = random.randint(10, 120)

        rows.append({
            "symbol": sym,
            "price": round(price, 2),
            "market_cap": market_cap,
            "avg_volume_30d": avg_vol,
            "iv": round(iv, 3),
            "iv_rank": random.randint(20, 90),
            "hv_20": round(random.uniform(0.30, 1.10), 3),
            "beta": round(random.uniform(0.8, 3.0), 2),
            "revenue_growth": round(random.uniform(-0.15, 0.60), 3),
            "profit_margin": round(random.uniform(-0.30, 0.25), 3),
            "debt_to_equity": round(random.uniform(0, 4.0), 2),
            "current_ratio": round(random.uniform(0.5, 5.0), 2),
            "short_float": round(random.uniform(0.02, 0.35), 3),
            "days_to_earnings": dte_next_earnings,
            "above_ma50": random.random() > 0.4,
            "above_ma200": random.random() > 0.5,
            "rsi_14": round(random.uniform(30, 70), 1),
            "recommendation": random.choice(["buy", "hold", "hold", "buy", "underperform"]),
        })

    return pd.DataFrame(rows)


# ──────────────────────────────────────────────────────────────────────────────
# Black-Scholes helpers (simplified, no dividends)
# ──────────────────────────────────────────────────────────────────────────────

def _bs_price(S: float, K: float, t: float, r: float, sigma: float) -> tuple:
    """Returns (call_price, put_price) using Black-Scholes."""
    from scipy.stats import norm

    if t <= 0 or sigma <= 0:
        call = max(0, S - K)
        put = max(0, K - S)
        return call, put

    d1 = (np.log(S / K) + (r + 0.5 * sigma ** 2) * t) / (sigma * np.sqrt(t))
    d2 = d1 - sigma * np.sqrt(t)

    call = S * norm.cdf(d1) - K * np.exp(-r * t) * norm.cdf(d2)
    put = K * np.exp(-r * t) * norm.cdf(-d2) - S * norm.cdf(-d1)

    return max(0.01, round(call, 3)), max(0.01, round(put, 3))


def _approx_delta(S: float, K: float, t: float, r: float, sigma: float, option_type: str) -> float:
    """Approximate Black-Scholes delta."""
    from scipy.stats import norm
    if t <= 0 or sigma <= 0:
        return 1.0 if (option_type == "call" and S > K) else 0.0
    d1 = (np.log(S / K) + (r + 0.5 * sigma ** 2) * t) / (sigma * np.sqrt(t))
    if option_type == "call":
        return norm.cdf(d1)
    else:
        return norm.cdf(d1) - 1


def _approx_gamma(S: float, K: float, t: float, sigma: float) -> float:
    """Approximate Black-Scholes gamma."""
    from scipy.stats import norm
    if t <= 0 or sigma <= 0 or S <= 0:
        return 0.0
    d1 = (np.log(S / K) + 0.5 * sigma ** 2 * t) / (sigma * np.sqrt(t))
    return norm.pdf(d1) / (S * sigma * np.sqrt(t))


# ──────────────────────────────────────────────────────────────────────────────
# Quick demo
# ──────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    chain = generate_mock_options_chain("SOFI", stock_price=8.50, iv=0.75, dte=30)
    print("\n=== MOCK OPTIONS CHAIN (PUTS) ===")
    print(chain["puts"][["strike", "bid", "ask", "openInterest", "impliedVolatility"]].to_string())
