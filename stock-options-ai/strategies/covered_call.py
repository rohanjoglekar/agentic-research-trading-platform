"""
strategies/covered_call.py

Covered Call (CC) scanner and calculator.

A covered call: you own 100 shares and sell a call option above the current price.
You collect premium. If the stock rises above the strike, shares get called away.
If it stays below, you keep both the shares and the premium.

Best used when: you already own the stock and want income from shares you're
comfortable holding long-term. Don't sell covered calls on stocks you're
desperately bullish on — you cap your upside.
"""

from typing import Optional
import pandas as pd

from config.settings import OPTIONS


def calculate_cc_metrics(
    stock_price: float,
    cost_basis: float,         # What you paid for the shares (or current price if unknown)
    strike: float,
    bid: float,
    ask: float,
    dte: int,
    open_interest: int,
    volume: Optional[int] = None,
    delta: Optional[float] = None,
) -> dict:
    """
    Calculates all metrics for a single covered call contract.

    Args:
        stock_price: current stock price
        cost_basis: your cost basis per share (use stock_price if entering fresh)
        strike: call strike price
        bid/ask: option bid and ask prices
        dte: days to expiration
        open_interest: open interest
        volume: today's volume (optional)
        delta: call delta (0 to 1)

    Returns:
        dict with all CC metrics
    """
    premium = (bid + ask) / 2
    spread = ask - bid
    spread_pct = spread / premium if premium > 0 else 1.0

    # Return on capital (based on current stock price, not cost basis)
    gross_return = premium / stock_price
    monthly_return = gross_return * (30 / dte) if dte > 0 else 0
    annualized_return = gross_return * (365 / dte) if dte > 0 else 0

    # Return if called away (total, including stock appreciation)
    called_away_profit_per_share = (strike - stock_price) + premium
    called_away_return = called_away_profit_per_share / stock_price
    called_away_annualized = called_away_return * (365 / dte) if dte > 0 else 0

    # Protection / breakeven
    breakeven = stock_price - premium              # new effective cost basis
    downside_protection = premium / stock_price    # % price can fall before losing money
    upside_cap = strike                            # maximum profit price

    # How far OTM
    otm_pct = (strike - stock_price) / stock_price  # positive = OTM call

    # Probability of profit (call won't be exercised = stock stays below strike)
    if delta is not None:
        # P(call exercised) ≈ delta; P(keep premium) ≈ 1 - delta
        pop = 1.0 - abs(delta)
    else:
        pop = min(0.85, max(0.30, 0.50 + otm_pct * 2.0))

    # Expected value
    expected_gain = pop * premium                              # keep premium
    expected_loss_if_called = (1 - pop) * max(0, cost_basis - strike)  # called below cost basis
    ev_per_share = expected_gain - expected_loss_if_called
    ev_per_contract = ev_per_share * 100

    # Liquidity score
    liquidity = _liquidity_score(open_interest, spread_pct, volume)

    # Risk flags
    flags = []
    if spread_pct > OPTIONS["max_spread_pct"]:
        flags.append(f"Wide spread ({spread_pct:.0%})")
    if open_interest < OPTIONS["min_open_interest"]:
        flags.append(f"Low OI ({open_interest})")
    if otm_pct < 0.01:
        flags.append("Strike almost ATM — heavy assignment risk")
    if otm_pct < 0:
        flags.append("Strike is ITM — immediate assignment risk")
    if monthly_return < 0.005:
        flags.append(f"Premium too thin ({monthly_return:.1%} monthly)")
    if strike < cost_basis:
        flags.append(f"Strike ${strike:.2f} below cost basis ${cost_basis:.2f} — locks in loss")
    if downside_protection < 0.02:
        flags.append("Minimal downside protection from premium")

    return {
        "strategy": "CC",
        "strike": round(strike, 2),
        "premium": round(premium, 3),
        "bid": round(bid, 3),
        "ask": round(ask, 3),
        "spread_pct": round(spread_pct * 100, 2),
        "cost_basis": round(cost_basis, 2),
        "gross_return_pct": round(gross_return * 100, 3),
        "monthly_return_pct": round(monthly_return * 100, 3),
        "annualized_return_pct": round(annualized_return * 100, 2),
        "called_away_return_pct": round(called_away_return * 100, 2),
        "called_away_annualized_pct": round(called_away_annualized * 100, 2),
        "breakeven": round(breakeven, 2),
        "downside_protection_pct": round(downside_protection * 100, 2),
        "upside_cap": round(upside_cap, 2),
        "otm_pct": round(otm_pct * 100, 2),
        "pop_estimate": round(pop, 3),
        "expected_value": round(ev_per_contract, 2),
        "open_interest": open_interest,
        "volume": volume,
        "liquidity_score": round(liquidity, 1),
        "dte": dte,
        "flags": flags,
        "is_valid": len([f for f in flags if "ITM" in f or "too thin" in f or "Wide" in f or "Low OI" in f]) == 0,
    }


def scan_ccs_for_stock(
    symbol: str,
    stock_price: float,
    options_chain: dict,
    expiration: str,
    dte: int,
    cost_basis: Optional[float] = None,
    screener_score: float = 50,
) -> list:
    """
    Scans calls chain for covered call opportunities.

    Args:
        symbol: ticker
        stock_price: current price
        options_chain: {"calls": DataFrame, "puts": DataFrame}
        expiration: expiration string
        dte: days to expiration
        cost_basis: your cost basis (defaults to stock_price for fresh entry)
        screener_score: stock's screener quality score

    Returns:
        List of valid CC opportunity dicts
    """
    if cost_basis is None:
        cost_basis = stock_price  # assume fresh entry at market

    calls = options_chain.get("calls", pd.DataFrame())
    if calls.empty:
        return []

    results = []

    for _, row in calls.iterrows():
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

        # Only evaluate OTM calls
        otm_pct = (strike - stock_price) / stock_price
        if otm_pct < 0.01 or otm_pct > OPTIONS["otm_pct_max"] + 0.05:
            continue

        metrics = calculate_cc_metrics(
            stock_price=stock_price,
            cost_basis=cost_basis,
            strike=strike,
            bid=bid,
            ask=ask,
            dte=dte,
            open_interest=oi,
            volume=vol,
            delta=delta,
        )

        metrics["symbol"] = symbol
        metrics["expiration"] = expiration
        metrics["stock_price"] = stock_price
        metrics["screener_score"] = screener_score

        results.append(metrics)

    return results


def _liquidity_score(oi: int, spread_pct: float, volume: Optional[int] = None) -> float:
    from config.settings import LIQUIDITY
    oi_score = min(100, (oi / LIQUIDITY["oi_for_max_score"]) * 100)
    spread_score = max(0, 100 * (1 - spread_pct / LIQUIDITY["spread_pct_for_zero"]))
    vol_score = min(100, (volume / LIQUIDITY["volume_for_max_score"]) * 100) if volume else 50
    return (oi_score * 0.50) + (spread_score * 0.35) + (vol_score * 0.15)
