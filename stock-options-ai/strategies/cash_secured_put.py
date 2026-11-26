"""
strategies/cash_secured_put.py

Cash-Secured Put (CSP) scanner and calculator.

A CSP means: you sell the right for someone to sell you 100 shares
at the strike price. You collect a premium upfront. You must hold
enough cash to buy those shares if assigned.

Best used when: you WANT to own the stock at the strike price.
Never use CSPs on stocks you'd hate to own at assignment.
"""

import math
from typing import Optional
import pandas as pd

from config.settings import OPTIONS, RISK


# ─── Probability of profit estimators ───────────────────────────────────────

def _norm_cdf(x: float) -> float:
    """Standard normal CDF — pure Python, no scipy."""
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2)))


def _norm_div_yield(dy) -> float:
    """Annual dividend yield as a FRACTION, defensively normalized.

    yfinance has flip-flopped between fraction (0.021) and percent (2.1)
    across versions, so anything > 0.5 is treated as percent. Clamped to
    [0, 0.12]; None/NaN -> 0 (a missing yield must never move a score)."""
    try:
        q = float(dy)
    except (TypeError, ValueError):
        return 0.0
    if q != q or q < 0:            # NaN or negative
        return 0.0
    if q > 0.5:                    # percent form
        q = q / 100.0
    return min(q, 0.12)


def _bs_pop_short_put(stock_price: float, strike: float, dte: int,
                       iv: Optional[float] = None,
                       delta: Optional[float] = None,
                       otm_pct: float = 0.0,
                       r: float = 0.045,
                       q: float = 0.0) -> float:
    """
    Probability of profit for a short put.

    A short put is profitable if the underlying expires above the breakeven
    price = strike − premium. As a tractable approximation, we use:
        POP ≈ P(S_T > strike) = N(d2)
    where d2 = (ln(S/K) + (r − q − σ²/2) × T) / (σ × √T)

    q is the continuous dividend yield: dividends lower the forward price, so
    ignoring them OVERSTATES a short put's POP on payers. American exercise
    style does not change this number — P(S_T > K) is a statement about the
    terminal distribution; early-assignment risk is handled by the rules
    layer (ITM haircut, assignment chip), not the probability model.

    Falls back to (1 − |delta|) if IV unavailable, then to OTM% heuristic.
    """
    if iv is not None and iv > 0 and dte > 0 and stock_price > 0 and strike > 0:
        T = dte / 365.0
        sigma = float(iv)
        d2 = (math.log(stock_price / strike) + (r - q - 0.5 * sigma * sigma) * T) / (sigma * math.sqrt(T))
        return max(0.05, min(0.95, _norm_cdf(d2)))
    if delta is not None:
        # Delta of a short put ≈ −N(−d1). 1−|delta| is the standard practitioner POP.
        return max(0.05, min(0.95, 1.0 - abs(delta)))
    # No IV, no delta — pure moneyness heuristic
    return max(0.30, min(0.85, 0.50 + otm_pct * 2.0))


def calculate_csp_metrics(
    stock_price: float,
    strike: float,
    bid: float,
    ask: float,
    dte: int,
    open_interest: int,
    volume: Optional[int] = None,
    delta: Optional[float] = None,
    iv: Optional[float] = None,
    dividend_yield: Optional[float] = None,
) -> dict:
    """
    Calculates all metrics for a single cash-secured put contract.

    Args:
        stock_price: current stock price
        strike: put strike price
        bid: option bid price
        ask: option ask price
        dte: days to expiration
        open_interest: open interest for this strike
        volume: today's volume (optional)
        delta: option delta (negative for puts; use abs value if provided as negative)

    Returns:
        dict with all CSP metrics
    """
    premium = (bid + ask) / 2  # use mid price, not ask
    spread = ask - bid
    spread_pct = spread / premium if premium > 0 else 1.0

    # Core return calculations
    capital_required = strike * 100           # cash you must hold
    gross_return = premium / strike           # return on capital (per unit, not per contract)
    monthly_return = gross_return * (30 / dte) if dte > 0 else 0
    annualized_return = gross_return * (365 / dte) if dte > 0 else 0

    # Breakeven and buffer
    breakeven = strike - premium
    downside_buffer = (stock_price - breakeven) / stock_price  # how far stock can fall

    # Distance OTM
    otm_pct = (stock_price - strike) / stock_price  # positive = OTM put

    # Probability of profit: full Black-Scholes if IV available, else delta, else heuristic
    q = _norm_div_yield(dividend_yield)
    pop = _bs_pop_short_put(stock_price, strike, dte, iv=iv, delta=delta,
                            otm_pct=otm_pct, q=q)

    # Expected P&L at expiry (risk-neutral, closed form): premium − E[(K − S_T)⁺],
    # with E[(K − S_T)⁺] = K·N(−d2) − S·e^{rT}·N(−d1) (undiscounted BS put payoff).
    # The old "simplified" version set loss-given-loss = (strike − breakeven)/2,
    # but strike − breakeven IS the premium — it modeled the average assignment
    # loss as half a premium and massively understated tail losses. None when the
    # chain has no IV (we don't invent a distribution).
    ev_per_contract = None
    if iv is not None and iv > 0 and dte > 0 and stock_price > 0 and strike > 0:
        _T = dte / 365.0
        _sig = float(iv)
        _d1 = (math.log(stock_price / strike) + (0.045 - q + 0.5 * _sig * _sig) * _T) / (_sig * math.sqrt(_T))
        _d2 = _d1 - _sig * math.sqrt(_T)
        exp_assign_loss = strike * _norm_cdf(-_d2) - stock_price * math.exp((0.045 - q) * _T) * _norm_cdf(-_d1)
        ev_per_contract = (premium - max(0.0, exp_assign_loss)) * 100

    # Liquidity score (0-100)
    liquidity = _liquidity_score(open_interest, spread_pct, volume)

    # Risk flags
    flags = []
    if spread_pct > OPTIONS["max_spread_pct"]:
        flags.append(f"Wide spread ({spread_pct:.0%})")
    if open_interest < OPTIONS["min_open_interest"]:
        flags.append(f"Low OI ({open_interest})")
    if otm_pct < OPTIONS["otm_pct_min"]:
        flags.append(f"Strike too close to price ({otm_pct:.1%} OTM)")
    if otm_pct > OPTIONS["otm_pct_max"]:
        flags.append(f"Strike too far OTM ({otm_pct:.1%})")
    if monthly_return < 0.005:
        flags.append(f"Premium too thin ({monthly_return:.1%} monthly)")
    if downside_buffer < RISK["min_downside_buffer_pct"]:
        flags.append(f"Thin downside buffer ({downside_buffer:.1%})")
    # ─── NEW: POP floor + delta-band check ────────────────────────────────
    if pop < OPTIONS.get("min_pop", 0.50):
        flags.append(f"Low POP ({pop:.0%} — below 50% floor)")
    elif pop < OPTIONS.get("preferred_pop_min", 0.65):
        flags.append(f"Below preferred POP ({pop:.0%})")
    # Delta-band: prefer strikes in the 10-30% delta "sweet spot" for short puts
    if delta is not None:
        abs_delta = abs(float(delta))
        d_low = OPTIONS.get("delta_target_low", 0.10)
        d_high = OPTIONS.get("delta_target_high", 0.30)
        if abs_delta > d_high + 0.05:  # too aggressive (deep ITM-ish)
            flags.append(f"Delta {abs_delta:.2f} above sweet-spot ({d_high})")
        elif abs_delta < max(0.0, d_low - 0.03):  # too far OTM, premium too thin
            flags.append(f"Delta {abs_delta:.2f} below sweet-spot ({d_low})")

    return {
        "strategy": "CSP",
        "strike": round(strike, 2),
        "premium": round(premium, 3),
        "bid": round(bid, 3),
        "ask": round(ask, 3),
        "spread_pct": round(spread_pct * 100, 2),
        "capital_required": round(capital_required, 2),
        "gross_return_pct": round(gross_return * 100, 3),
        "monthly_return_pct": round(monthly_return * 100, 3),
        "annualized_return_pct": round(annualized_return * 100, 2),
        "breakeven": round(breakeven, 2),
        "downside_buffer_pct": round(downside_buffer * 100, 2),
        "otm_pct": round(otm_pct * 100, 2),
        "pop_estimate": round(pop, 3),
        "expected_value": round(ev_per_contract, 2) if ev_per_contract is not None else None,
        "open_interest": open_interest,
        "volume": volume,
        "liquidity_score": round(liquidity, 1),
        "dte": dte,
        "flags": flags,
        "is_valid": len([f for f in flags
                         if "too" in f.lower()
                         or "wide" in f.lower()
                         or "low oi" in f.lower()
                         or "below 50% floor" in f.lower()
                         or "above sweet-spot" in f.lower()]) == 0,
    }


def scan_csps_for_stock(
    symbol: str,
    stock_price: float,
    options_chain: dict,
    expiration: str,
    dte: int,
    screener_score: float = 50,
    dividend_yield: Optional[float] = None,
) -> list:
    """
    Scans an options chain and returns a list of valid CSP opportunities.

    Args:
        symbol: ticker symbol
        stock_price: current stock price
        options_chain: {"puts": DataFrame, "calls": DataFrame}
        expiration: expiration date string (YYYY-MM-DD)
        dte: days to expiration
        screener_score: stock's screener score (used in final ranking)

    Returns:
        List of dicts, one per valid CSP opportunity
    """
    puts = options_chain.get("puts", pd.DataFrame())
    if puts.empty:
        return []

    results = []

    for _, row in puts.iterrows():
        strike = float(row.get("strike", 0))
        if strike <= 0:
            continue

        bid = float(row.get("bid", 0))
        ask = float(row.get("ask", 0))
        if bid <= 0 or ask <= 0:
            continue

        oi = int(row.get("openInterest", 0))
        vol = int(row.get("volume", 0)) if pd.notna(row.get("volume")) else None
        delta = float(row.get("delta")) if pd.notna(row.get("delta", None)) else None
        iv = float(row.get("impliedVolatility")) if pd.notna(row.get("impliedVolatility", None)) else None

        # Pre-filter: only evaluate OTM puts in our target range
        otm_pct = (stock_price - strike) / stock_price
        if otm_pct < OPTIONS["otm_pct_min"] - 0.02 or otm_pct > OPTIONS["otm_pct_max"] + 0.05:
            continue

        metrics = calculate_csp_metrics(
            stock_price=stock_price,
            strike=strike,
            bid=bid,
            ask=ask,
            dte=dte,
            open_interest=oi,
            dividend_yield=dividend_yield,
            volume=vol,
            delta=delta,
            iv=iv,
        )

        metrics["symbol"] = symbol
        metrics["expiration"] = expiration
        metrics["stock_price"] = stock_price
        metrics["screener_score"] = screener_score

        results.append(metrics)

    return results


def _liquidity_score(oi: int, spread_pct: float, volume: Optional[int] = None) -> float:
    """
    Calculates a 0-100 liquidity score.
    OI weight: 50%, spread weight: 35%, volume weight: 15%.
    """
    from config.settings import LIQUIDITY

    oi_score = min(100, (oi / LIQUIDITY["oi_for_max_score"]) * 100)
    spread_score = max(0, 100 * (1 - spread_pct / LIQUIDITY["spread_pct_for_zero"]))

    if volume is not None:
        vol_score = min(100, (volume / LIQUIDITY["volume_for_max_score"]) * 100)
    else:
        vol_score = 50  # neutral if unknown

    return (oi_score * 0.50) + (spread_score * 0.35) + (vol_score * 0.15)
