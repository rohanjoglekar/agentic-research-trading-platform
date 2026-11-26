"""The read itself: prompt, structured output, validation, cost.

Pure except for `read_one`, which makes one Messages API call through the
client it is given (the real anthropic client in production, a fake in tests).
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from typing import Optional

PROMPT_VERSION = 1
DIMENSIONS = ("business_quality", "growth_durability", "financial_resilience",
              "risk_outlook", "valuation_vs_quality")
CONVICTIONS = ("low", "medium", "high")
KINDS = ("company", "fund")
_CTRL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")

SYSTEM_PROMPT = """\
You are an equity analyst giving a compact, independent read on one public \
company for a quantitative stock screen. The screen already measures valuation \
multiples, growth rates, margins, price momentum and analyst ratings \
mechanically. Your job is the part it cannot do: judge the business itself.

Score each dimension from 0 to 10. A 5 is the typical large US-listed company. \
An 8 or higher is rare, about one company in ten, and so is a 2 or lower. Use \
the whole scale: if you find yourself giving most companies a 6 or a 7, you \
are not discriminating.

- business_quality: how durable the competitive position is. Moat, pricing \
power, customer lock-in, market structure.
- growth_durability: how long and how reliably the business can keep growing \
from here, given its markets and competitors. This is not last year's growth rate.
- financial_resilience: how well it would get through two bad years. Leverage, \
cash generation, refinancing needs, how cyclical its cash flow is.
- risk_outlook: 10 means few serious threats, 0 means existential ones. \
Regulation, litigation, technology disruption, customer or supplier \
concentration, governance, accounting quality.
- valuation_vs_quality: whether today's price is reasonable for a business of \
this quality and durability. 10 is clearly cheap for what it is, 0 is priced \
for perfection or worse.

Set kind to "company" for an operating business. Set it to "fund" for an ETF, a \
closed-end fund, or a leveraged or option-income product, and score the fund as \
a holding instead: business_quality is the soundness of the structure (cost, \
tracking, decay from leverage or option overlays), growth_durability is the \
long-run return you would expect from what it owns, financial_resilience is how \
it behaves in a deep drawdown, risk_outlook is as above, and \
valuation_vs_quality is 5 unless you have a clear view.

Use the figures you are given and your own knowledge of the company and its \
industry. The figures can be a day or more old and come from a data vendor, so \
treat them as approximate. If you do not recognise the company, or cannot tell \
what it does, set recognized to false and conviction to "low".

thesis is one sentence: the single strongest reason this business is or is not \
attractive to own for the next few years. key_risk is one sentence: the single \
most important thing that could go wrong. Plain language, no boilerplate, at \
most 30 words each.
"""

OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "recognized": {"type": "boolean"},
        "kind": {"type": "string", "enum": list(KINDS)},
        **{d: {"type": "integer"} for d in DIMENSIONS},
        "conviction": {"type": "string", "enum": list(CONVICTIONS)},
        "thesis": {"type": "string"},
        "key_risk": {"type": "string"},
    },
    "required": ["recognized", "kind", *DIMENSIONS, "conviction", "thesis", "key_risk"],
    "additionalProperties": False,
}


def _num(v) -> Optional[float]:
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    v = float(v)
    return v if v == v and v not in (float("inf"), float("-inf")) else None


def _money(v) -> Optional[str]:
    v = _num(v)
    if v is None:
        return None
    a = abs(v)
    if a >= 1e9:
        return f"${v / 1e9:,.1f}B"
    if a >= 1e6:
        return f"${v / 1e6:,.0f}M"
    return f"${v:,.0f}"


def _pct(v, already_pct: bool = False) -> Optional[str]:
    v = _num(v)
    return None if v is None else f"{v if already_pct else v * 100:.1f}%"


def build_user_prompt(row: dict, fund: dict, today: str) -> str:
    """Facts for one company. Deliberately WITHOUT the screen's own verdict —
    composite score, tier, confidence, DCF fair value — or the tape (momentum,
    analyst rating, price target): the read is meant to be a second opinion on
    the business, and an LLM shown the answer repeats it."""
    fund = fund or {}
    dcf = (row or {}).get("dcf") or {}
    lines = [f"Company: {fund.get('name') or row.get('name') or row.get('symbol')} "
             f"(ticker {row.get('symbol')})",
             f"Date: {today}"]

    def add(label: str, value) -> None:
        if value not in (None, "", []):
            lines.append(f"{label}: {value}")

    if row.get("is_fund"):
        lines.append("Type: a fund (ETF or closed-end fund), not an operating company; "
                     "no company financials apply")
    add("Sector", fund.get("sector") or row.get("sector"))
    add("Industry", fund.get("industry"))
    add("Market cap", _money(fund.get("market_cap")))
    add("Share price", f"${_num(row.get('last')):,.2f}" if _num(row.get("last")) else None)
    lo, hi = _num(fund.get("wk52_low")), _num(fund.get("wk52_high"))
    add("52-week range", f"${lo:,.2f} to ${hi:,.2f}" if lo and hi else None)
    add("Revenue, trailing 12 months", _money(fund.get("total_revenue")))
    series = [x for x in (fund.get("revenue_series") or []) if _num(x)]
    add("Annual revenue, newest first", ", ".join(_money(x) for x in series[:4]) if series else None)
    add("Revenue growth, year on year", _pct(fund.get("revenue_growth")))
    add("Earnings growth, year on year", _pct(fund.get("earnings_growth")))
    add("Gross margin", _pct(fund.get("gross_margin")))
    add("Operating margin", _pct(fund.get("operating_margin")))
    add("Net margin", _pct(fund.get("profit_margin")))
    add("Return on equity", _pct(fund.get("roe")))
    add("Free cash flow margin", _pct(fund.get("fcf_margin_pct"), True))
    add("Free cash flow yield", _pct(fund.get("fcf_yield_pct"), True))
    add("Total debt", _money(fund.get("total_debt")))
    add("Cash", _money(fund.get("total_cash")))
    add("EBITDA", _money(fund.get("ebitda")))
    add("Stock-based compensation", _money(fund.get("sbc")))
    add("Capital expenditure", _money(fund.get("capex")))
    ga = _num(fund.get("goodwill_assets_ratio"))
    add("Goodwill as a share of assets", f"{ga * 100:.0f}%" if ga is not None else None)
    for label, key in (("Price / earnings", "pe"), ("Forward price / earnings", "forward_pe"),
                       ("EV / EBITDA", "ev_ebitda"), ("EV / revenue", "ev_rev"),
                       ("Price / book", "price_to_book")):
        v = _num(fund.get(key))
        add(label, f"{v:.1f}x" if v is not None and v > 0 else None)
    add("Dividend yield", _pct(fund.get("dividend_yield_pct"), True))
    add("Business type flagged by the screen", (dcf.get("why_archetype") or dcf.get("archetype")))
    add("Stage", row.get("stage"))
    return "\n".join(lines)


class ReadError(ValueError):
    """The model's answer could not be used (bad JSON, score out of range…)."""


def _clean(text, limit: int) -> str:
    return _CTRL.sub(" ", str(text if text is not None else "")).strip()[:limit]


def parse_read(text: str) -> dict:
    """Validate one structured answer → the stored read (without metadata)."""
    try:
        data = json.loads(text)
    except (TypeError, ValueError) as e:
        raise ReadError(f"not JSON: {e}") from e
    if not isinstance(data, dict):
        raise ReadError("answer is not an object")
    sub = {}
    for d in DIMENSIONS:
        v = data.get(d)
        if isinstance(v, bool) or not isinstance(v, int) or not 0 <= v <= 10:
            raise ReadError(f"{d} must be an integer 0-10, got {v!r}")
        sub[d] = v
    conviction = data.get("conviction")
    if conviction not in CONVICTIONS:
        raise ReadError(f"conviction must be one of {CONVICTIONS}, got {conviction!r}")
    recognized = data.get("recognized")
    if not isinstance(recognized, bool):
        raise ReadError("recognized must be true or false")
    kind = data.get("kind")
    if kind not in KINDS:
        raise ReadError(f"kind must be one of {KINDS}, got {kind!r}")
    thesis, risk = _clean(data.get("thesis"), 300), _clean(data.get("key_risk"), 300)
    if not thesis or not risk:
        raise ReadError("thesis and key_risk must not be empty")
    return {
        "recognized": recognized,
        "kind": kind,
        "sub": sub,
        # equal weights: no dimension is privileged, and the rank that matters
        # is computed across the universe by the dashboard, not here
        "raw": round(sum(sub.values()) / len(DIMENSIONS) * 10.0, 1),
        "conviction": conviction,
        "thesis": thesis,
        "key_risk": risk,
        # Only a recognised operating company goes into the cross-universe
        # ranking. An unrecognised name's scores are guesses, and a fund is
        # scored on a different meaning of the same five words.
        "usable": recognized and kind == "company",
    }


def cost_usd(usage, rates: dict) -> float:
    """Dollar cost of one response from its usage block. `rates` are $ per
    million tokens: input, output, cache_read, cache_write."""
    def n(name: str) -> float:
        v = getattr(usage, name, None) if usage is not None else None
        if v is None and isinstance(usage, dict):
            v = usage.get(name)
        return float(v or 0)

    return (n("input_tokens") * rates["input"]
            + n("output_tokens") * rates["output"]
            + n("cache_read_input_tokens") * rates.get("cache_read", rates["input"] * 0.1)
            + n("cache_creation_input_tokens") * rates.get("cache_write", rates["input"] * 1.25)) / 1e6


def read_one(client, *, row: dict, fund: dict, model: str, effort: str, max_tokens: int,
             rates: dict, today: str) -> dict:
    """One company → {"read": {...} | None, "error": str | None, "cost_usd": float}.

    Never raises for a model-side problem (refusal, truncation, bad JSON): that
    is one name without a read, not a failed run. API/transport errors DO
    propagate so the caller can stop the run instead of burning the universe.
    """
    resp = client.messages.create(
        model=model,
        max_tokens=max_tokens,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": build_user_prompt(row, fund, today)}],
        output_config={"effort": effort,
                       "format": {"type": "json_schema", "schema": OUTPUT_SCHEMA}},
    )
    cost = cost_usd(getattr(resp, "usage", None), rates)
    stop = getattr(resp, "stop_reason", None)
    if stop == "refusal":
        return {"read": None, "error": "refusal", "cost_usd": cost}
    if stop == "max_tokens":
        return {"read": None, "error": "truncated (max_tokens)", "cost_usd": cost}
    # thinking is on by default, so the answer is not necessarily the first block
    text = next((b.text for b in (getattr(resp, "content", None) or [])
                 if getattr(b, "type", None) == "text"), None)
    if not text:
        return {"read": None, "error": f"no text in the response (stop_reason {stop})", "cost_usd": cost}
    try:
        read = parse_read(text)
    except ReadError as e:
        return {"read": None, "error": str(e), "cost_usd": cost}
    read.update({
        "symbol": row.get("symbol"),
        "sector": (fund or {}).get("sector") or row.get("sector"),
        "model": model,
        "prompt_v": PROMPT_VERSION,
        "read_at": datetime.now(timezone.utc).isoformat(),
    })
    return {"read": read, "error": None, "cost_usd": cost}
