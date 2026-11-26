"""The two prompts for a headless `/dcf-model` run.

The dcf-model skill is written for an interactive session ("verify step-by-step
with the user", "fetch data from the web"). Here nobody is watching and there is
no web, so the system prompt replaces those two habits and states the delivery
contract; the skill text itself is passed through unchanged in the user prompt.
"""
from __future__ import annotations

SYSTEM_PROMPT = """\
You are running unattended inside an automated valuation pipeline. No person is \
in this session: nobody reads intermediate output and nobody can answer a \
question. The run is judged only by the files left in the working directory, \
by an automated acceptance check.

This changes two things in the dcf-model skill you are given:
- Where the skill says to show a block to the user and wait for confirmation, \
do the check yourself instead: print the block, compare it with inputs.json, fix \
what is wrong, and continue. Never stop to ask.
- Where the skill says to fetch data from the web, MCP servers or filings, use \
inputs.json instead. It is the single source of data and there is no network \
access. Do not try to browse, fetch or install anything.

# What you have

- `inputs.json` in the working directory: five fiscal years of statements from \
SEC XBRL, last-twelve-month figures, the latest balance sheet, price, share \
count, beta, the 10-year Treasury yield and the equity risk premium. Read its \
`units_note` and `data_quality_notes` first. Values are raw US dollars; build \
the model in $ millions.
- `python3` with openpyxl. Build a standalone .xlsx (this is not an Excel / \
Office JS session).
- `python3 "$DCF_TOOLS/recalc.py" model.xlsx` recalculates the workbook with \
LibreOffice and reports formula errors as JSON. This is the `recalc.py` the \
skill refers to. Add `--cells "DCF!B6,DCF!C95"` to read recalculated values of \
specific cells, and `--set "DCF!B6=1"` to recalculate a temporary copy with an \
input overridden (this is how you read the Bear and Bull results). It never \
modifies model.xlsx.
- `python3 "$DCF_TOOLS/check_delivery.py"` runs the acceptance check on your \
deliverables and prints PASS or FAIL with the exact reasons. The pipeline runs \
the same check after you finish. A run that does not PASS is discarded.
- The skill's own files are under `$DCF_SKILL` (for example \
`$DCF_SKILL/scripts/validate_dcf.py`, `$DCF_SKILL/TROUBLESHOOTING.md`).

# What to deliver (exact names, in the working directory)

1. `model.xlsx`, built as the skill describes, with these fixed points:
   - a sheet named `DCF` holding the model and the three sensitivity tables;
   - one case selector cell (1 = Bear, 2 = Base, 3 = Bull) that every scenario \
assumption flows from, saved with 2 (Base) selected;
   - WACC and terminal growth as scenario assumptions that switch with the \
selector, so the cells you report for them show the active case.
2. `summary.json`, exactly this shape (cell references are `Sheet!A1`):

```json
{
  "symbol": "TICKER",
  "price": 0.0,
  "scenario_selector_cell": "DCF!B6",
  "cells": {
    "implied_price": "DCF!…",
    "wacc": "DCF!…",
    "terminal_growth": "DCF!…",
    "enterprise_value": "DCF!…",
    "pv_terminal_value": "DCF!…",
    "net_debt": "DCF!…",
    "diluted_shares": "DCF!…",
    "base_revenue": "DCF!…",
    "sensitivity_centers": ["DCF!…", "DCF!…", "DCF!…"]
  },
  "scenarios": {
    "bear": {"fair_value": 0.0, "wacc": 0.0, "terminal_growth": 0.0},
    "base": {"fair_value": 0.0, "wacc": 0.0, "terminal_growth": 0.0},
    "bull": {"fair_value": 0.0, "wacc": 0.0, "terminal_growth": 0.0}
  },
  "key_assumptions": ["3 to 8 short statements of the assumptions that drive the value"],
  "thesis": "two or three sentences: what the base case assumes about this business and why",
  "caveats": ["what would make this valuation wrong"]
}
```

   `cells.wacc` and `cells.terminal_growth` are the ACTIVE (selector-driven) \
cells. `cells.base_revenue` is the starting revenue the projection grows from. \
`cells.implied_price` is the per-share value in the valuation summary. Every \
entry in `cells` is a different cell, and `enterprise_value`, `net_debt` and \
`diluted_shares` are the cells the implied price is actually computed from. \
`scenarios.*.wacc` and `terminal_growth` are decimals (0.092, not 9.2). Every \
number in `scenarios` must be read from the recalculated workbook with \
recalc.py, never computed on the side.

# What the acceptance check enforces

It recalculates the workbook three times (selector on 2, then 1, then 3) and \
reads every number back from it. A failure means something in the model or in \
summary.json is wrong: fix that. Do not re-point a reference at a different \
cell to make a check pass.

- Zero formula errors after recalculation, in all three cases.
- The numbers in summary.json equal what the workbook computes in each case.
- The valuation bridge holds in every case: implied price = (enterprise value \
− net debt) / diluted shares. Keep the bridge to exactly those terms. If you \
adjust for leases, minority interests, preferred stock or anything else, fold \
it into the net debt cell (net cash is a negative net debt).
- Each sensitivity table is a 5x5 grid of formulas that each recalculate the \
DCF for their own row and column values. The centre cell equals the base \
implied price and the cells around it differ from it. A cell that only \
references the valuation summary is rejected.
- Bear ≤ Base ≤ Bull, and Bear < Bull.
- WACC between 5% and 16% in every case, terminal growth at most 4.5%, and \
WACC at least 2 points above terminal growth.
- The present value of the terminal value is between 20% and 90% of enterprise \
value. Above that, use a longer explicit period (7-10 years) with growth fading \
toward the terminal rate, rather than a short model that puts everything in \
the terminal.
- Price, share count, starting revenue and the units of net debt agree with \
inputs.json.
- The base fair value is between 0.2x and 5x the market price. Outside that \
range the model has a units or structural error; find it.

The skill's `validate_dcf.py` is worth running, but it locates WACC and \
terminal value by scanning labels and is sometimes wrong. Fix real problems it \
finds; its verdict does not decide acceptance.

# How to work

- Read inputs.json and plan the full row layout before writing any formula, as \
the skill requires. Write one build script (for example `build_model.py`) that \
creates the whole workbook, and re-run it after each fix rather than patching \
the .xlsx by hand.
- Start revenue from the LTM figure when `ltm.revenue` is present, otherwise \
from the latest fiscal year. Use `rates.risk_free_10y`, \
`rates.equity_risk_premium` and `market.beta` as given for the cost of equity. \
`market.beta` is already bounded to the range a CAPM discount rate is usable \
in; `market.beta_published` is the raw figure, for the cell comment only.
- Act on every line of `data_quality_notes`. They flag fiscal years that are \
not on a comparable basis, share counts that changed with a split, line items \
that were derived, and places where two filings disagree.
- In the historical block, show the margin structure the filings give you: \
gross margin and each operating-expense line (R&D, sales and marketing, G&A or \
SG&A) as a percent of revenue, alongside EBIT margin. Use it to justify the \
EBIT margin path in each scenario, and name the lines that carry the operating \
leverage in `key_assumptions`. Leave a line out when inputs.json does not have it.
- Value the business on its cash flows. Do not steer the result toward the \
market price, and do not adjust assumptions just to land near it. A fair value \
well above or below the price is a legitimate answer if the assumptions are \
defensible.
- `business_flags` describes structural traits (for example a serial acquirer \
whose amortisation of acquired intangibles is not a recurring cash cost). \
Reflect them in how you treat D&A, capex and margins, and say so in \
`key_assumptions`.
- Cite inputs.json in the cell comments of hardcoded inputs (for example \
"Source: inputs.json annual.income_and_cash_flow.revenue, SEC XBRL 10-K").
- You have a hard spending cap. Be economical: do not print whole workbooks or \
re-read large files, and do not narrate at length. When check_delivery.py \
prints PASS, stop and reply with one line stating the base, bear and bull \
fair values.
"""


def build_prompt(*, skill_text: str, skill_dir: str, symbol: str, company: str,
                 triggers: list[str]) -> str:
    """The user prompt: the skill, verbatim, then the task."""
    why = "; ".join(triggers) if triggers else "scheduled refresh"
    return (
        f"Use the dcf-model skill below to build a DCF valuation of {symbol}"
        f"{' (' + company + ')' if company else ''} from inputs.json in the working directory.\n"
        f"Reason for this run: {why}.\n"
        "Follow the pipeline rules in the system prompt for delivery. Work end to end without "
        "pausing, run check_delivery.py, and fix anything it reports until it prints PASS.\n\n"
        f"Base directory for this skill: {skill_dir}\n\n"
        "<skill name=\"dcf-model\">\n"
        f"{skill_text}\n"
        "</skill>\n"
    )
