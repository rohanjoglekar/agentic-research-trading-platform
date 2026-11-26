"""
scoring/composite_score.py

Final composite score for ranking options trades.

Takes individual scores from screener/options/risk modules and
produces a single 0-100 quality score plus an A/B/C/D rating.

The composite score intentionally penalizes high yield + high risk combinations.
A 4% monthly return with D-tier fundamentals scores WORSE than a 2% return
with A-tier fundamentals. That's the whole point.
"""

from typing import Optional
from config.settings import (
    TARGETS, FLAG_MULTIPLIERS, SCORE_WEIGHTS_BY_REGIME, CC_SCORE_WEIGHTS_BY_REGIME,
)


def _recent_spike_penalty(
    rsi_14: Optional[float],
    drawdown_from_52w_high: Optional[float],  # negative; -0.05 = 5% below high
    recent_30d_return_pct: Optional[float],   # 0.30 = up 30% over last 30 days
) -> tuple:
    """
    Penalize stocks that have rallied hard recently — selling puts into a
    spike is anti-mean-reversion. Returns (penalty_pts, list of reason strings).

    Stronger curve (cap 30) so that parabolic moves get nearly disqualified.
    """
    penalty = 0
    reasons = []
    if rsi_14 is not None:
        if rsi_14 > 85:
            penalty += 12
            reasons.append(f"RSI {rsi_14:.0f} extreme")
        elif rsi_14 > 75:
            penalty += 6
            reasons.append(f"RSI {rsi_14:.0f} overbought")
        elif rsi_14 > 70:
            penalty += 3
            reasons.append(f"RSI {rsi_14:.0f} stretched")
    if drawdown_from_52w_high is not None:
        if drawdown_from_52w_high > -0.01:
            penalty += 10
            reasons.append("at its 52-week high")
        elif drawdown_from_52w_high > -0.03:
            penalty += 6
            reasons.append("within 3% of 52w high")
        elif drawdown_from_52w_high > -0.05:
            penalty += 3
            reasons.append("within 5% of 52w high")
    if recent_30d_return_pct is not None:
        if recent_30d_return_pct > 1.00:
            penalty += 25
            reasons.append(f"+{recent_30d_return_pct*100:.0f}% last 30d (vertical)")
        elif recent_30d_return_pct > 0.60:
            penalty += 18
            reasons.append(f"+{recent_30d_return_pct*100:.0f}% last 30d (parabolic)")
        elif recent_30d_return_pct > 0.35:
            penalty += 10
            reasons.append(f"+{recent_30d_return_pct*100:.0f}% last 30d (spiked)")
        elif recent_30d_return_pct > 0.20:
            penalty += 4
            reasons.append(f"+{recent_30d_return_pct*100:.0f}% last 30d (rallied)")
    return min(penalty, 30), reasons


def _flag_multiplier(flag: str) -> float:
    """Map a flag string to its multiplier (default 0.92 = 8% haircut)."""
    f = flag.lower()
    if "earnings in" in f or "earnings within" in f:
        return FLAG_MULTIPLIERS["earnings"]
    if "wide spread" in f:
        return FLAG_MULTIPLIERS["wide_spread"]
    if "low oi" in f:
        return FLAG_MULTIPLIERS["low_oi"]
    if "itm" in f or "in the money" in f:
        return FLAG_MULTIPLIERS["itm"]
    if "locks in loss" in f:
        return FLAG_MULTIPLIERS["locks_loss"]
    if "bankruptcy" in f:
        return FLAG_MULTIPLIERS["bankruptcy"]
    if "dilut" in f:
        return FLAG_MULTIPLIERS["dilution"]
    if "declining" in f:
        return FLAG_MULTIPLIERS["declining"]
    if "below 200" in f:
        return FLAG_MULTIPLIERS["below_200ma"]
    return 0.92  # generic flag


def composite_trade_score(
    monthly_return_pct: float,
    pop_estimate: float,
    liquidity_score: float,
    fundamental_score: float,
    technical_score: float,
    iv_rank: Optional[float],
    days_to_earnings: Optional[int],
    risk_flags: list,
    strategy: str = "CSP",
    rsi_14: Optional[float] = None,
    drawdown_from_52w_high: Optional[float] = None,
    recent_30d_return_pct: Optional[float] = None,
    regime: str = "calm",
    pop_bias: float = 1.0,
    downside_buffer_pct: Optional[float] = None,
) -> dict:
    """Composite quality score.

    New args:
      regime: "calm" | "caution" | "paused" | "extreme" — picks weight set.
      pop_bias: multiplier applied to pop_score from realized-vs-predicted
                calibration. 1.0 = no adjustment. <1.0 = our model is
                over-confident, <1.0 haircuts every POP score uniformly.

    Flag handling switched from additive to multiplicative — each flag
    haircuts the raw score by FLAG_MULTIPLIERS[name]. Multiple flags compound.

    ETFs are no longer special-cased — they're now filtered upstream
    via ETF_BLOCKLIST. Ad-hoc ETF lookups in the dashboard bypass the
    blocklist and still call this with regular weights.
    """
    # ─── Component Scores ─────────────────────────────────────────────────
    max_return = TARGETS["monthly_return_max_score"] * 100
    target_return = TARGETS["monthly_return_target"] * 100
    return_score = min(1.0, max(0.0, monthly_return_pct / max_return))

    pop_score = min(1.0, max(0.0, pop_estimate)) * float(pop_bias)
    pop_score = min(1.0, max(0.0, pop_score))

    liq_score = min(1.0, liquidity_score / 100)

    fund_normalized = min(1.0, fundamental_score / 100)
    tech_normalized = min(1.0, technical_score / 100)
    combined_fundamental = fund_normalized * 0.65 + tech_normalized * 0.35

    iv_score = max(0.0, 1.0 - iv_rank / 100) if iv_rank is not None else 0.5

    if days_to_earnings is None:
        earnings_score = 0.8
    elif days_to_earnings >= 45:
        earnings_score = 1.0
    elif days_to_earnings >= 30:
        earnings_score = 0.7
    elif days_to_earnings >= 14:
        earnings_score = 0.3
    else:
        earnings_score = 0.0

    # Downside buffer (moneyness): how far the underlying can fall before the
    # breakeven. A point-in-time backtest (2,150 CSPs replayed to expiry) showed
    # buffer + POP are the signals that actually avoid assignment losses, so it's
    # now an explicit scored component. 20% buffer = full marks; None → neutral 0.5
    # (e.g. covered calls, which carry their own moneyness logic).
    if downside_buffer_pct is None:
        buffer_score = 0.5
    else:
        buffer_score = min(1.0, max(0.0, (downside_buffer_pct / 100.0) / 0.20))

    # ─── Regime-aware weights ─────────────────────────────────────────────
    # Covered calls use their own (pre-reweight, fundamentals-heavy, no-buffer)
    # weight set; the POP+buffer reweight was backtested on put-selling only.
    wmap = CC_SCORE_WEIGHTS_BY_REGIME if strategy == "CC" else SCORE_WEIGHTS_BY_REGIME
    # An unknown regime is scored as caution: not being able to read the market
    # must not be treated as a calm one.
    weights = wmap.get(regime) or wmap["caution"]

    raw_score = (
        return_score           * weights["monthly_return"]
        + pop_score            * weights["pop"]
        + buffer_score         * weights.get("buffer", 0.0)
        + liq_score            * weights["liquidity"]
        + combined_fundamental * weights["fundamentals"]
        + iv_score             * weights["iv_rank"]
        + earnings_score       * weights["earnings_safety"]
    ) * 100

    # ─── Multiplicative flag haircut ──────────────────────────────────────
    multiplier = 1.0
    applied_flags = []
    for flag in risk_flags:
        m = _flag_multiplier(flag)
        multiplier *= m
        applied_flags.append((flag, m))

    # Strategy-specific haircuts (in addition to flag list)
    if strategy == "CC" and monthly_return_pct > 5.0:
        multiplier *= 0.85   # suspicious CC yield → underlying is crashing
    if strategy == "WHEEL" and monthly_return_pct > 6.0:
        multiplier *= 0.90

    # ─── Recent-spike penalty (still subtractive — caps at 30) ───────────
    spike_penalty, spike_reasons = _recent_spike_penalty(
        rsi_14, drawdown_from_52w_high, recent_30d_return_pct
    )
    if spike_reasons:
        risk_flags = list(risk_flags) + [f"recent spike: {r}" for r in spike_reasons]

    final_score = max(0, min(100, raw_score * multiplier - spike_penalty))
    rating = score_to_rating(final_score)
    at_target = monthly_return_pct >= target_return

    explanation = _build_explanation(
        final_score, rating, monthly_return_pct, pop_estimate,
        liquidity_score, fundamental_score, iv_rank, days_to_earnings,
        risk_flags, strategy
    )

    return {
        "final_score": round(final_score, 1),
        "rating": rating,
        "regime": regime,
        "at_2pct_target": at_target,
        "component_scores": {
            "return_score": round(return_score * 100, 1),
            "pop_score": round(pop_score * 100, 1),
            # Only surface a buffer score when the buffer term actually counts
            # (CSP/WHEEL) — for CCs the weight is 0, so a synthetic 50 would mislead.
            "buffer_score": (round(buffer_score * 100, 1) if weights.get("buffer", 0) else None),
            "liquidity_score": round(liq_score * 100, 1),
            "fundamental_score": round(combined_fundamental * 100, 1),
            "iv_rank_score": round(iv_score * 100, 1),
            "earnings_score": round(earnings_score * 100, 1),
        },
        "flag_multiplier": round(multiplier, 3),
        "applied_flags": [(f, round(m, 2)) for f, m in applied_flags],
        "spike_penalty": round(spike_penalty, 1),
        "spike_reasons": spike_reasons,
        "raw_score": round(raw_score, 1),
        "pop_bias_used": round(pop_bias, 3),
        "explanation": explanation,
    }



def score_to_rating(score: float) -> str:
    """Converts numeric score to letter grade."""
    if score >= 75:
        return "A"
    elif score >= 60:
        return "B"
    elif score >= 45:
        return "C"
    else:
        return "D"


def _build_explanation(
    score, rating, monthly_ret, pop, liq, fund_score, iv_rank,
    dte_earnings, flags, strategy
) -> str:
    """Builds a template-based plain English explanation. No LLM cost."""

    parts = []

    # Lead with rating
    rating_text = {
        "A": "Strong opportunity",
        "B": "Decent trade with manageable risk",
        "C": "Marginal — proceed with caution",
        "D": "Poor trade, avoid or reduce size",
    }
    parts.append(rating_text.get(rating, ""))

    # Return context
    if monthly_ret >= 2.0:
        parts.append(f"{monthly_ret:.1f}%/month meets the 2% target")
    elif monthly_ret >= 1.0:
        parts.append(f"{monthly_ret:.1f}%/month is below target but acceptable for lower risk")
    else:
        parts.append(f"{monthly_ret:.1f}%/month is thin — barely worth the trade")

    # POP context
    if pop >= 0.70:
        parts.append(f"~{pop:.0%} probability of keeping full premium")
    elif pop >= 0.55:
        parts.append(f"moderate {pop:.0%} chance of success")
    else:
        parts.append(f"low {pop:.0%} probability — this trade is a coin flip")

    # Fundamentals
    if fund_score >= 70:
        parts.append("solid fundamentals — comfortable holding if assigned")
    elif fund_score >= 50:
        parts.append("average fundamentals — acceptable wheel candidate")
    else:
        parts.append("weak fundamentals — risky to hold if assigned")

    # IV context
    if iv_rank is not None:
        if iv_rank >= 60:
            parts.append(f"IV rank {iv_rank:.0f} — elevated: richer premium, more risk priced in")
        elif iv_rank >= 35:
            parts.append(f"IV rank {iv_rank:.0f} — moderate, OK to sell")
        else:
            parts.append(f"IV rank {iv_rank:.0f} — low IV, premium may not be worth the risk")

    # Earnings
    if dte_earnings is not None and dte_earnings < 30:
        parts.append(f"⚠️ Earnings in {dte_earnings:.0f} days — elevated risk")

    # Top flags
    if flags:
        parts.append(f"Flags: {'; '.join(flags[:2])}")

    return ". ".join(parts) + "."


def rank_trades(trades: list) -> list:
    """
    Takes a list of trade dicts (with composite_score already computed)
    and returns them sorted by final_score descending.

    Also assigns a rank number.
    """
    sorted_trades = sorted(trades, key=lambda t: t.get("final_score", 0), reverse=True)
    for i, trade in enumerate(sorted_trades):
        trade["rank"] = i + 1
    return sorted_trades
