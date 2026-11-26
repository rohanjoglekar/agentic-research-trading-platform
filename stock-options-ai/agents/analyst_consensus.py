"""Wall-Street analyst consensus → a wheel-ranking conviction signal.

The CSP wheel can be ASSIGNED the underlying, so the Street's Buy/Hold/Sell
view and the mean price target (implied upside) are conviction inputs, not
trivia. This module turns yfinance's ``recommendationKey`` + ``targetMeanPrice``
+ analyst count into:
  (a) a MULTIPLIER on the final composite_rank_score, and
  (b) a short summary + flags fed to the AI would-own analyst and the UI.

It is NOT a hard filter — a Hold name with strong trade metrics can still rank,
just not above comparable Buy names. Thresholds live in
``config.settings.ANALYST_CONSENSUS``. Every function degrades to a neutral
(multiplier 1.0) signal on missing/thin data and never raises.
"""
from __future__ import annotations

from typing import Optional

from loguru import logger

from config.settings import ANALYST_CONSENSUS as CFG

_LABELS = {
    "strong_buy": "Strong Buy", "buy": "Buy", "outperform": "Buy",
    "hold": "Hold", "neutral": "Hold",
    "underperform": "Underperform", "sell": "Sell", "strong_sell": "Strong Sell",
}
_BEARISH = {"underperform", "sell", "strong_sell"}


def _norm_key(rec: Optional[str]) -> str:
    return (rec or "").strip().lower().replace(" ", "_").replace("-", "_")


def _num(x) -> Optional[float]:
    try:
        return round(float(x), 2)
    except (TypeError, ValueError):
        return None


def _pct(x) -> Optional[float]:
    try:
        return round(float(x) * 100, 1)
    except (TypeError, ValueError):
        return None


def consensus_signal(recommendation, analyst_target, number_of_analysts, price) -> dict:
    """Build the consensus signal dict from raw fields. Pure + safe.

    Returns keys: recommendation (label), n_analysts, target, implied_upside_pct,
    multiplier, flags, summary.
    """
    key = _norm_key(recommendation)
    n = int(number_of_analysts or 0)
    label = _LABELS.get(key)

    upside = None
    try:
        if analyst_target and price and float(price) > 0:
            upside = (float(analyst_target) - float(price)) / float(price)
    except (TypeError, ValueError):
        upside = None

    base = {
        "recommendation": label or (recommendation or None),
        "n_analysts": n or None,
        "target": _num(analyst_target),
        "implied_upside_pct": _pct(upside),
        "multiplier": 1.0,
        "flags": [],
    }

    # Thin coverage, disabled, or unrecognized rating → report but don't weight.
    if not CFG.get("enabled", True) or n < CFG["min_analysts"] or key not in CFG["rating_mult"]:
        base["summary"] = _summary(label, n, analyst_target, upside,
                                   thin=(n < CFG["min_analysts"]))
        return base

    mult = CFG["rating_mult"][key]
    if upside is not None:
        for thr, m in CFG["upside_bands"]:
            if upside >= thr:
                mult *= m
                break
    mult = max(CFG["mult_floor"], min(CFG["mult_cap"], mult))

    flags = []
    if key in _BEARISH:
        flags.append(f"Street rates {label} ({n} analysts)")
    if upside is not None and upside < -0.05 and price and analyst_target:
        flags.append(f"${float(price):.0f} is above mean target ${float(analyst_target):.0f} "
                     f"({upside*100:+.0f}%) — limited upside")

    base.update(multiplier=round(mult, 3), flags=flags,
                summary=_summary(label, n, analyst_target, upside))
    return base


def _summary(label, n, target, upside, thin=False) -> str:
    if not label and not target:
        return "no analyst coverage"
    parts = []
    if label:
        parts.append(f"Street: {label}" + (f" ({n} analysts)" if n else ""))
    if target:
        if upside is not None:
            parts.append(f"{upside*100:+.0f}% to mean target ${float(target):.0f}")
        else:
            parts.append(f"mean target ${float(target):.0f}")
    s = " · ".join(parts) if parts else "no consensus"
    if thin and label:
        s += " — thin coverage, not weighted"
    return s


def fetch(symbol: str, price: Optional[float] = None, yf_client=None) -> dict:
    """Fetch consensus for ``symbol`` from yfinance and build the signal.

    Reuses a passed ``yf_client`` (cached) when available. Returns a neutral
    signal on any error.
    """
    try:
        if yf_client is None:
            # Without a client that carries analyst fields (the scan passes a
            # YFinanceClient), this path has price only and the signal stays neutral.
            from data.alpaca_options_client import AlpacaOptionsClient
            yf_client = AlpacaOptionsClient()
        info = yf_client.get_stock_info(symbol) or {}
        return consensus_signal(
            recommendation=info.get("recommendation"),
            analyst_target=info.get("analyst_target"),
            number_of_analysts=info.get("number_of_analysts"),
            price=price if price is not None else info.get("price"),
        )
    except Exception as e:
        logger.warning(f"[analyst_consensus] fetch failed for {symbol}: {e}")
        return consensus_signal(None, None, 0, price)
