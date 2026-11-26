"""Batch engine: one structured request per company through the Message Batches API.

Why this is ~10x cheaper than the agentic engine (prompt.py + sandbox.run_claude):

  • Batch API: every token at 50% of the standard price, cache reads and writes
    included (the discounts stack).
  • One request instead of an agent loop. The agentic run re-sent a growing
    context over ~40 turns; here the model reads the company data once.
  • The model writes ASSUMPTIONS only, as schema-constrained JSON. The workbook
    (formulas, sensitivity grids, formatting) is built by tools/build_model.py
    for free, so no output tokens go to spreadsheet code.
  • The static system prompt is cached (1-hour TTL, the right choice for batches
    per the docs) and shared by every request in the batch.
  • Compact inputs: $ millions, no nulls, history pre-digested into ratios.
  • Effort `medium` with adaptive thinking; Opus only where real money sits.

Requests in a batch cannot use refusal `fallbacks` (the API rejects it), so a
refusal is recorded and the name is retried in a later window.

Pure helpers + a thin client wrapper; the orchestration lives in the runner.
"""
from __future__ import annotations

import importlib.util
import json
import math
from pathlib import Path
from typing import Optional

from deep_dcf.config import Settings

TOOLS = Path(__file__).resolve().parent / "tools"
_spec = importlib.util.spec_from_file_location("deep_dcf_build_model", TOOLS / "build_model.py")
build_model = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(build_model)          # openpyxl is imported lazily inside it

YEARS = build_model.YEARS
CHARS_PER_TOKEN_FLOOR = 1.5      # reservation estimate: real JSON runs ~2.5-4 chars/token


def _scen_schema() -> dict:
    vec = lambda d: {"type": "array", "items": {"type": "number"},   # noqa: E731
                     "description": f"Exactly {YEARS} decimals, year 1 to year {YEARS}. {d}"}
    num = lambda d: {"type": "number", "description": d}             # noqa: E731
    props = {
        "revenue_growth": vec("Year-over-year revenue growth, 0.08 = 8%."),
        "gross_margin": vec("Gross profit / revenue. Use 1.0 every year if the company reports no "
                            "gross profit, and put all costs in operating_expense_pct."),
        "operating_expense_pct": vec("All operating costs between gross profit and EBIT (R&D, sales, "
                                     "G&A, amortisation, other) as a share of revenue. EBIT margin = "
                                     "gross_margin - operating_expense_pct."),
        "da_pct": vec("Depreciation and amortisation / revenue (added back to NOPAT)."),
        "capex_pct": vec("Capital expenditure / revenue. Include an allowance for recurring "
                         "acquisitions if they are part of how the company grows."),
        "tax_rate": num("Cash tax rate on positive EBIT, 0.21 = 21%."),
        "nwc_pct_of_revenue_change": num("Increase in working capital per $1 of revenue growth "
                                         "(negative for businesses that collect cash up front)."),
        "terminal_growth": num("Perpetual growth after year 10, 0.025 = 2.5%. At most 0.045."),
        "wacc_adjustment": num("Added to the CAPM WACC for this case, e.g. 0.0075 for bear, 0 for "
                               "base, -0.0075 for bull. Between -0.03 and 0.03."),
    }
    return {"type": "object", "properties": props, "required": list(props),
            "additionalProperties": False}


SCHEMA = {
    "type": "object",
    "properties": {
        "scenarios": {
            "type": "object",
            "properties": {name: _scen_schema() for name in ("bear", "base", "bull")},
            "required": ["bear", "base", "bull"],
            "additionalProperties": False,
        },
        "pretax_cost_of_debt": {"type": "number",
                                "description": "Pre-tax cost of debt, 0.055 = 5.5%."},
        "thesis": {"type": "string",
                   "description": "Two or three sentences: what the base case assumes about this business and why."},
        "key_assumptions": {"type": "array", "items": {"type": "string"},
                            "description": "3 to 6 short statements of the assumptions that drive the value, "
                                           "each with its numbers."},
        "caveats": {"type": "array", "items": {"type": "string"},
                    "description": "1 to 4 things that would make this valuation wrong."},
    },
    "required": ["scenarios", "pretax_cost_of_debt", "thesis", "key_assumptions", "caveats"],
    "additionalProperties": False,
}

SYSTEM_PROMPT = f"""\
You are an equity analyst setting the assumptions for a discounted cash flow \
valuation of one company. You do not build the spreadsheet. A program builds it \
from your answer, exactly as described below, and an automated check verifies \
the result. Your answer is the JSON object the response schema defines, with no \
other text.

# How your numbers are used

The model projects {YEARS} years from the base-period revenue given in the \
company data (the last twelve months when available, otherwise the latest \
fiscal year). For each year t and each case (bear, base, bull):

- revenue_t = revenue_(t-1) x (1 + revenue_growth_t)
- EBIT_t = revenue_t x (gross_margin_t - operating_expense_pct_t)
- NOPAT_t = EBIT_t - max(0, EBIT_t) x tax_rate
- FCF_t = NOPAT_t + revenue_t x da_pct_t - revenue_t x capex_pct_t \
- (revenue_t - revenue_(t-1)) x nwc_pct_of_revenue_change
- cash flows are discounted at mid-year (period t - 0.5)
- terminal value = FCF_{YEARS} x (1 + terminal_growth) / (WACC - terminal_growth), \
discounted at period {YEARS} - 0.5
- WACC = CAPM cost of equity (risk-free rate + beta x equity risk premium, all \
given) weighted with your after-tax cost of debt by market cap and net debt, \
plus your wacc_adjustment for the case
- value per share = (enterprise value - net debt) / shares outstanding

Stock-based compensation is a real cost here: it stays inside operating \
expenses and is never added back.

# What makes a good answer

- Start from the history. The company data includes each fiscal year's growth, \
margins, D&A, capex and SBC as ratios. Year-1 assumptions should connect to \
the recent run rate, and any break from it needs a reason in key_assumptions.
- Fade toward a steady state. Growth should approach the terminal rate by year \
{YEARS}; margins should settle at a level the business can hold. Year {YEARS} \
must look like a mature year, because the terminal value capitalises it.
- In the terminal years, capex should cover depreciation plus the investment \
that terminal growth requires. Capex far below D&A forever is not credible.
- Keep the terminal value below about 85% of enterprise value. A valuation \
that is almost all terminal value is rejected.
- Make the cases genuinely different. Bear must be worth less than base and \
base less than bull. Move growth, margins and the WACC adjustment together in \
the direction each case implies.
- The terminal growth must stay at least 2 percentage points below the WACC in \
every case, and no higher than 4.5%.
- Use business_flags. A capital-intensive company is mid build-out: normalise \
capex toward a sustainable level over the period. A serial acquirer grows by \
buying companies, so fund that growth through capex_pct rather than assuming \
it for free. Base-effect earnings jumps are not a run rate.
- Do not anchor on the share price. Value the cash flows; a fair value well \
above or below the price is a legitimate answer when the assumptions are \
defensible. A result below 0.2x or above 5x the price is treated as an error.
- Every rate, margin and percentage is a decimal: 0.15 means 15%. Each \
yearly list has exactly {YEARS} values.
- Write key_assumptions and caveats as plain, specific sentences with the \
numbers in them.
"""


# ── inputs → prompt ─────────────────────────────────────────────────────────

def _to_millions(obj, key: str = ""):
    """Recursively shrink inputs for the prompt: drop nulls and source strings,
    and express every large number (dollars, shares) in millions."""
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if v is None or k in ("xbrl_tags_used", "units_note") or k.endswith("_source") or k == "source":
                continue
            v2 = _to_millions(v, k)
            if v2 not in (None, {}, []):
                out[k] = v2
        return out
    if isinstance(obj, list):
        return [_to_millions(x, key) for x in obj]
    if isinstance(obj, float) or isinstance(obj, int) and not isinstance(obj, bool):
        if not math.isfinite(float(obj)):
            return None
        if abs(obj) >= 1e4:
            return round(obj / 1e6, 1)
        return round(float(obj), 4) if isinstance(obj, float) else obj
    return obj


def history_ratios(inputs: dict) -> list[dict]:
    """Per fiscal year: growth and the ratios the projection uses, so the model
    reads them instead of spending tokens computing them."""
    a = inputs.get("annual") or {}
    ends = a.get("fiscal_year_ends") or []
    f = a.get("income_and_cash_flow") or {}

    def ser(k):
        v = f.get(k) or []
        return [x if isinstance(x, (int, float)) and not isinstance(x, bool) else None for x in v]

    rev, gp, oi = ser("revenue"), ser("gross_profit"), ser("operating_income")
    da, cx, sbc, ocf = ser("depreciation_amortization"), ser("capex"), ser("stock_based_compensation"), ser("operating_cash_flow")
    tax, pre = ser("income_tax"), ser("pretax_income")
    rows = []
    for i, e in enumerate(ends):
        r = rev[i] if i < len(rev) else None

        def ratio(s):
            x = s[i] if i < len(s) else None
            return round(x / r, 4) if (x is not None and r) else None
        row = {"fiscal_year_end": e, "revenue_musd": round(r / 1e6, 1) if r else None,
               "revenue_growth": round(r / rev[i - 1] - 1, 4) if (i and r and rev[i - 1]) else None,
               "gross_margin": ratio(gp), "operating_margin": ratio(oi), "da_pct": ratio(da),
               "capex_pct": ratio(cx), "sbc_pct": ratio(sbc), "ocf_pct": ratio(ocf),
               "tax_rate": round(tax[i] / pre[i], 4) if (i < len(tax) and i < len(pre) and tax[i] is not None
                                                         and pre[i]) else None}
        rows.append({k: v for k, v in row.items() if v is not None})
    return rows


def user_message(inputs: dict) -> str:
    rev, label = build_model.base_revenue(inputs)
    mk = inputs.get("market") or {}
    rates = inputs.get("rates") or {}
    cs = inputs.get("capital_structure") or {}
    frame = {
        "symbol": inputs.get("symbol"), "company": inputs.get("company"),
        "sector": inputs.get("sector"), "industry": inputs.get("industry"), "as_of": inputs.get("as_of"),
        "projection_base": {"period": label, "revenue_musd": round(rev / 1e6, 1) if rev else None},
        "fixed_by_the_model": {
            "price": mk.get("price"),
            "shares_outstanding_millions": round((mk.get("shares_outstanding") or 0) / 1e6, 2),
            "net_debt_musd": round(((cs.get("total_debt") or 0) - (cs.get("total_cash") or 0)) / 1e6, 1),
            "risk_free_10y": rates.get("risk_free_10y"), "beta": mk.get("beta"),
            "equity_risk_premium": rates.get("equity_risk_premium"),
            "implied_pretax_cost_of_debt_from_interest": cs.get("implied_pretax_cost_of_debt"),
            "effective_tax_rate_reported": cs.get("effective_tax_rate"),
        },
        "history_ratios": history_ratios(inputs),
    }
    detail = _to_millions({k: inputs.get(k) for k in ("ttm", "annual", "ltm", "latest_balance_sheet",
                                                      "consensus", "business_flags", "data_quality_notes")})
    return ("Set the bear, base and bull assumptions for this company. All money values are $ millions "
            "and share counts are millions; rates are decimals.\n\n"
            + json.dumps(frame, separators=(",", ":"), sort_keys=False)
            + "\n\nFull reported data:\n"
            + json.dumps(detail, separators=(",", ":"), sort_keys=True))


def request_params(model: str, inputs: dict, s: Settings) -> dict:
    """Messages API params for one company (batch requests are non-streaming)."""
    return {
        "model": model,
        "max_tokens": int(s.batch_max_tokens),
        "system": [{"type": "text", "text": SYSTEM_PROMPT,
                    "cache_control": {"type": "ephemeral", "ttl": "1h"}}],
        "messages": [{"role": "user", "content": user_message(inputs)}],
        "thinking": {"type": "adaptive"},
        "output_config": {"effort": s.batch_effort,
                          "format": {"type": "json_schema", "schema": SCHEMA}},
    }


# ── money ───────────────────────────────────────────────────────────────────

def _rates(model: str, s: Settings) -> dict:
    """Standard $/MTok for `model`. An unknown model gets the most expensive
    configured rates, so a reservation is never too small."""
    table = s.token_rates or {}
    if model in table:
        return table[model]
    return max(table.values(), key=lambda r: r["output"]) if table else \
        {"input": 10.0, "output": 50.0, "cache_read": 1.0, "cache_write_1h": 20.0}


def reservation_usd(params: dict, s: Settings) -> float:
    """An upper bound on what one request can cost, reserved before submission.
    Output can never exceed max_tokens (thinking included); input is estimated
    from characters at a pessimistic ratio and priced as a 1-hour cache write,
    the most expensive way an input token can be billed."""
    r = _rates(params["model"], s)
    chars = len(json.dumps(params.get("system"))) + len(json.dumps(params.get("messages")))
    chars += len(json.dumps(SCHEMA))
    in_tokens = chars / CHARS_PER_TOKEN_FLOOR
    worst_in = max(r["input"], r.get("cache_write_1h", 2 * r["input"]))
    usd = (in_tokens * worst_in + params["max_tokens"] * r["output"]) / 1e6
    return round(usd * s.batch_discount, 4)


def usage_cost_usd(usage, model: str, s: Settings) -> float:
    """What a succeeded request cost, from its usage block, at batch prices."""
    def g(obj, name):
        v = getattr(obj, name, None) if not isinstance(obj, dict) else obj.get(name)
        return int(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else 0

    r = _rates(model, s)
    cc = getattr(usage, "cache_creation", None) if not isinstance(usage, dict) else usage.get("cache_creation")
    w1h = g(cc, "ephemeral_1h_input_tokens") if cc is not None else 0
    w5m = g(cc, "ephemeral_5m_input_tokens") if cc is not None else max(g(usage, "cache_creation_input_tokens") - w1h, 0)
    if cc is None:
        w1h, w5m = g(usage, "cache_creation_input_tokens"), 0      # unknown split: price as the dearer TTL
    usd = (g(usage, "input_tokens") * r["input"]
           + g(usage, "output_tokens") * r["output"]
           + g(usage, "cache_read_input_tokens") * r["cache_read"]
           + w1h * r.get("cache_write_1h", 2 * r["input"])
           + w5m * r.get("cache_write_5m", 1.25 * r["input"])) / 1e6
    return round(usd * s.batch_discount, 6)


def parse_answer(message) -> tuple[Optional[dict], Optional[str], str]:
    """→ (assumptions, error, status) from a succeeded batch message.
    status: ok | refused | truncated | unparseable."""
    stop = getattr(message, "stop_reason", None)
    if stop == "refusal":
        sd = getattr(message, "stop_details", None)
        cat = getattr(sd, "category", None) if sd is not None else None
        return None, f"the model declined (refusal, category {cat})", "refused"
    if stop == "max_tokens":
        return None, "the answer hit max_tokens before the JSON was complete", "truncated"
    text = next((getattr(b, "text", "") for b in (getattr(message, "content", None) or [])
                 if getattr(b, "type", None) == "text"), "")
    try:
        data = json.loads(text)
    except (TypeError, ValueError) as e:
        return None, f"answer is not JSON ({e})", "unparseable"
    if not isinstance(data, dict):
        return None, "answer is not a JSON object", "unparseable"
    return data, None, "ok"


# ── client ──────────────────────────────────────────────────────────────────

class BatchClient:
    """The three calls the runner makes, behind one seam tests can replace."""

    def __init__(self, api_key: str):
        import anthropic

        self._client = anthropic.Anthropic(api_key=api_key, max_retries=4)

    def submit(self, requests: list[tuple[str, dict]]) -> str:
        from anthropic.types.message_create_params import MessageCreateParamsNonStreaming
        from anthropic.types.messages.batch_create_params import Request

        batch = self._client.messages.batches.create(requests=[
            Request(custom_id=cid, params=MessageCreateParamsNonStreaming(**params))
            for cid, params in requests])
        return batch.id

    def status(self, batch_id: str) -> dict:
        b = self._client.messages.batches.retrieve(batch_id)
        rc = b.request_counts
        return {"processing_status": b.processing_status,
                "counts": {k: getattr(rc, k, 0) for k in ("processing", "succeeded", "errored",
                                                          "canceled", "expired")}}

    def results(self, batch_id: str):
        """Yields (custom_id, result_type, message_or_None, error_text)."""
        for item in self._client.messages.batches.results(batch_id):
            res = item.result
            kind = getattr(res, "type", "?")
            if kind == "succeeded":
                yield item.custom_id, kind, res.message, None
            elif kind == "errored":
                err = getattr(res, "error", None)
                inner = getattr(err, "error", err)
                etype = getattr(inner, "type", None) or getattr(err, "type", None)
                msg = getattr(inner, "message", None) or str(err)[:200]
                yield item.custom_id, kind, None, f"{etype}: {msg}"
            else:
                yield item.custom_id, kind, None, kind
