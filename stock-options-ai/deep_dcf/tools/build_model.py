#!/usr/bin/env python3
"""Build the DCF workbook from the model's assumptions — no model tokens spent on code.

    python3 build_model.py [--dir .] [--inputs inputs.json] [--assumptions assumptions.json]

The batch engine asks Claude for ASSUMPTIONS only (one structured JSON answer per
company, at batch prices). Everything else is deterministic and lives here:
the scenario blocks, the free-cash-flow build, discounting, the equity bridge
and three 5x5 sensitivity tables, all as live Excel formulas, plus a pure-Python
mirror of the same arithmetic that fills summary.json.

The mirror is the point of the design: summary.json says what PYTHON computed,
check_delivery.py then has LibreOffice recalculate the workbook three times
(Bear, Base, Bull) and requires the two to agree. A formula typo here cannot
publish a wrong number — it fails the check.

Prints ONE JSON object: {"ok": bool, "errors": [...], "values": {...}}.
Exit 0 = built (model.xlsx + summary.json written), 1 = the assumptions were
rejected (nothing written), 2 = the builder itself failed.

Runs inside the sandbox as the unprivileged user; standard library + openpyxl
only (openpyxl is imported lazily so the runner can import this module for
`base_revenue` and `validate` without it). Command-line tool: stdout is its
interface, so it prints rather than logs.
"""
from __future__ import annotations

import argparse
import json
import math
import re
import sys
from datetime import date
from pathlib import Path

YEARS = 10
SCENARIOS = ("bear", "base", "bull")
VECTORS = ("revenue_growth", "gross_margin", "operating_expense_pct", "da_pct", "capex_pct")
SCALARS = ("tax_rate", "nwc_pct_of_revenue_change", "terminal_growth", "wacc_adjustment")
# Inclusive bounds. Wide on purpose: they catch a broken answer (a percent sent
# as 15 instead of 0.15, a margin above 100%), not an aggressive view.
BOUNDS = {
    "revenue_growth": (-0.5, 1.5), "gross_margin": (0.0, 1.0), "operating_expense_pct": (0.0, 1.5),
    "da_pct": (0.0, 0.6), "capex_pct": (0.0, 0.8), "tax_rate": (0.0, 0.45),
    "nwc_pct_of_revenue_change": (-1.0, 1.0), "terminal_growth": (-0.01, 0.045),
    "wacc_adjustment": (-0.03, 0.03), "pretax_cost_of_debt": (0.0, 0.2),
}
MIN_SPREAD = 0.02                # WACC − terminal growth, every scenario
SENS_STEP_W = 0.005              # sensitivity axes
SENS_STEP_G = 0.005
SENS_GROWTH = (-0.02, -0.01, 0.0, 0.01, 0.02)
SENS_MARGIN = (-0.02, -0.01, 0.0, 0.01, 0.02)
SENS_BETA = (-0.2, -0.1, 0.0, 0.1, 0.2)
SENS_RF = (-0.005, -0.0025, 0.0, 0.0025, 0.005)
_CTRL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
COLS = [chr(ord("D") + i) for i in range(YEARS)]          # D..M = year 1..10
LAST = COLS[-1]


class BuildError(Exception):
    pass


# ── inputs ──────────────────────────────────────────────────────────────────

def _num(v):
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) else None


def base_revenue(inputs: dict):
    """(revenue in raw $, label) the projection grows from: LTM when the latest
    10-Q gave a clean one, else the latest fiscal year. One function, used by
    the prompt AND the workbook, so the model projects from the same number."""
    ltm = ((inputs.get("ltm") or {}).get("revenue")) or {}
    if _num(ltm.get("value")) and ltm["value"] > 0:
        return float(ltm["value"]), f"LTM to {ltm.get('through')}"
    annual = inputs.get("annual") or {}
    rev = (annual.get("income_and_cash_flow") or {}).get("revenue") or []
    ends = annual.get("fiscal_year_ends") or []
    if rev and _num(rev[-1]) and rev[-1] > 0:
        return float(rev[-1]), f"FY ending {ends[-1] if ends else '?'}"
    return None, None


def market_inputs(inputs: dict) -> dict:
    """The facts the workbook takes from inputs.json, in $M / millions of shares."""
    m = inputs.get("market") or {}
    cs = inputs.get("capital_structure") or {}
    rates = inputs.get("rates") or {}
    rev, label = base_revenue(inputs)
    out = {
        "price": _num(m.get("price")), "shares_m": (_num(m.get("shares_outstanding")) or 0) / 1e6,
        "debt_m": (_num(cs.get("total_debt")) or 0) / 1e6 if _num(cs.get("total_debt")) is not None else None,
        "cash_m": (_num(cs.get("total_cash")) or 0) / 1e6 if _num(cs.get("total_cash")) is not None else None,
        "revenue_m": rev / 1e6 if rev else None, "revenue_label": label,
        "rf": _num(rates.get("risk_free_10y")), "beta": _num(m.get("beta")),
        "erp": _num(rates.get("equity_risk_premium")),
    }
    missing = [k for k in ("price", "debt_m", "cash_m", "revenue_m", "rf", "beta", "erp") if out[k] is None]
    if out["shares_m"] <= 0:
        missing.append("shares_outstanding")
    if missing:
        raise BuildError("inputs.json lacks " + ", ".join(missing))
    return out


# ── assumptions ─────────────────────────────────────────────────────────────

def clean_text(text, limit: int) -> str:
    """Model-written text is untrusted. Control characters out, and no leading
    = + - @ so a sentence can never become a spreadsheet formula."""
    s = _CTRL.sub(" ", str(text if text is not None else "")).strip()
    return s.lstrip("=+-@ \t")[:limit]


def validate(a) -> tuple[dict, list[str]]:
    """Check the model's JSON → (normalised assumptions, errors)."""
    errs: list[str] = []
    if not isinstance(a, dict):
        return {}, ["assumptions must be a JSON object"]
    out: dict = {"scenarios": {}}
    scen = a.get("scenarios")
    if not isinstance(scen, dict):
        return {}, ["scenarios must be an object with bear, base and bull"]
    for name in SCENARIOS:
        s = scen.get(name)
        if not isinstance(s, dict):
            errs.append(f"scenarios.{name} is missing")
            continue
        norm: dict = {}
        for key in VECTORS:
            v = s.get(key)
            if not isinstance(v, list) or len(v) != YEARS:
                errs.append(f"scenarios.{name}.{key} must be a list of exactly {YEARS} numbers")
                continue
            vals = [_num(x) for x in v]
            if any(x is None for x in vals):
                errs.append(f"scenarios.{name}.{key} has a value that is not a finite number")
                continue
            lo, hi = BOUNDS[key]
            bad = [x for x in vals if not lo <= x <= hi]
            if bad:
                errs.append(f"scenarios.{name}.{key} has {bad[0]:g}, outside {lo:g}..{hi:g} (decimals: 0.15 = 15%)")
                continue
            norm[key] = vals
        for key in SCALARS:
            x = _num(s.get(key))
            lo, hi = BOUNDS[key]
            if x is None or not lo <= x <= hi:
                errs.append(f"scenarios.{name}.{key} must be a number in {lo:g}..{hi:g}")
                continue
            norm[key] = x
        if "gross_margin" in norm and "operating_expense_pct" in norm:
            margins = [g - o for g, o in zip(norm["gross_margin"], norm["operating_expense_pct"])]
            if any(not -1.0 <= m <= 0.95 for m in margins):
                errs.append(f"scenarios.{name}: gross margin minus operating expense gives an EBIT margin outside -100%..95%")
        out["scenarios"][name] = norm
    cod = _num(a.get("pretax_cost_of_debt"))
    lo, hi = BOUNDS["pretax_cost_of_debt"]
    if cod is None or not lo <= cod <= hi:
        errs.append(f"pretax_cost_of_debt must be a number in {lo:g}..{hi:g}")
    out["pretax_cost_of_debt"] = cod
    thesis = clean_text(a.get("thesis"), 700)
    if not thesis:
        errs.append("thesis must be a non-empty string")
    out["thesis"] = thesis
    ka = [clean_text(x, 300) for x in (a.get("key_assumptions") or []) if isinstance(x, str)]
    ka = [x for x in ka if x][:8]
    if len(ka) < 3:
        errs.append("key_assumptions must hold 3 to 8 non-empty strings")
    out["key_assumptions"] = ka
    out["caveats"] = [x for x in (clean_text(c, 300) for c in (a.get("caveats") or [])
                                  if isinstance(c, str)) if x][:6]
    return out, errs


# ── the arithmetic, in Python (the workbook's formulas must agree) ──────────

def wacc_parts(mk: dict, cod: float, tax: float) -> dict:
    coe = mk["rf"] + mk["beta"] * mk["erp"]
    mcap = mk["price"] * mk["shares_m"]
    net_debt = mk["debt_m"] - mk["cash_m"]
    we = mcap / (mcap + max(net_debt, 0.0))
    atcod = cod * (1.0 - tax)
    return {"coe": coe, "atcod": atcod, "we": we, "wd": 1.0 - we,
            "wacc_pre": coe * we + atcod * (1.0 - we), "net_debt": net_debt}


def scenario_value(mk: dict, s: dict, cod: float) -> dict:
    """Implied price for one scenario, exactly as the DCF sheet computes it."""
    w = wacc_parts(mk, cod, s["tax_rate"])
    wacc = w["wacc_pre"] + s["wacc_adjustment"]
    g = s["terminal_growth"]
    if wacc - g < MIN_SPREAD - 1e-12:
        raise BuildError(f"WACC {wacc:.2%} minus terminal growth {g:.2%} is under {MIN_SPREAD:.0%}")
    rev_prev = mk["revenue_m"]
    pv_sum, fcf = 0.0, 0.0
    for t in range(YEARS):
        rev = rev_prev * (1.0 + s["revenue_growth"][t])
        ebit = rev * s["gross_margin"][t] - rev * s["operating_expense_pct"][t]
        nopat = ebit - max(0.0, ebit) * s["tax_rate"]
        fcf = (nopat + rev * s["da_pct"][t] - rev * s["capex_pct"][t]
               - (rev - rev_prev) * s["nwc_pct_of_revenue_change"])
        pv_sum += fcf * (1.0 / (1.0 + wacc) ** (t + 0.5))
        rev_prev = rev
    tv = fcf * (1.0 + g) / (wacc - g)
    pv_tv = tv / (1.0 + wacc) ** (YEARS - 0.5)
    ev = pv_sum + pv_tv
    price = (ev - w["net_debt"]) / mk["shares_m"]
    return {"fair_value": price, "wacc": wacc, "terminal_growth": g, "enterprise_value": ev,
            "pv_terminal_value": pv_tv, "tv_fraction": (pv_tv / ev) if ev else None}


def evaluate(mk: dict, a: dict) -> tuple[dict, list[str]]:
    """Python values for all three cases + the economic sanity a model must
    clear before LibreOffice is worth starting."""
    errs: list[str] = []
    vals = {}
    for name in SCENARIOS:
        try:
            vals[name] = scenario_value(mk, a["scenarios"][name], a["pretax_cost_of_debt"])
        except BuildError as e:
            errs.append(f"{name}: {e}")
    if errs:
        return vals, errs
    fv = {n: vals[n]["fair_value"] for n in SCENARIOS}
    if not all(math.isfinite(v) for v in fv.values()):
        errs.append("a scenario value is not finite")
    elif not (fv["bear"] < fv["bull"] and fv["bear"] <= fv["base"] <= fv["bull"]):
        errs.append(f"scenario values out of order: bear {fv['bear']:.2f}, base {fv['base']:.2f}, "
                    f"bull {fv['bull']:.2f}")
    if vals["base"]["enterprise_value"] <= 0:
        errs.append("base enterprise value is not positive")
    return vals, errs


# ── the workbook ────────────────────────────────────────────────────────────

class Book:
    """Thin layout helper: one place for fonts, fills and number formats."""

    def __init__(self):
        import openpyxl
        from openpyxl.styles import Alignment, Border, Font, PatternFill, Side

        self.openpyxl = openpyxl
        self.wb = openpyxl.Workbook()
        self.f_input = Font(color="0000FF")
        self.f_bold = Font(bold=True)
        self.f_head = Font(bold=True, color="FFFFFF")
        self.f_title = Font(bold=True, size=14)
        self.fill_head = PatternFill("solid", fgColor="1F4E79")
        self.fill_sub = PatternFill("solid", fgColor="D9E1F2")
        self.fill_center = PatternFill("solid", fgColor="BDD7EE")
        self.fill_key = PatternFill("solid", fgColor="FFF2CC")
        side = Side(style="thin", color="8EA9DB")
        self.border = Border(top=side, bottom=side, left=side, right=side)
        self.wrap = Alignment(wrap_text=True, vertical="top")
        self.center_al = Alignment(horizontal="center")

    def header(self, ws, row: int, text: str, last_col: str = LAST):
        ws[f"A{row}"] = text
        for col in ["A", "B", "C"] + COLS:
            ws[f"{col}{row}"].fill = self.fill_head
            ws[f"{col}{row}"].font = self.f_head
            if col == last_col:
                break

    def put(self, ws, ref: str, value, fmt: str | None = None, *, inp: bool = False,
            comment: str | None = None, bold: bool = False):
        c = ws[ref]
        c.value = value
        if fmt:
            c.number_format = fmt
        if inp:
            c.font = self.f_input
        elif bold:
            c.font = self.f_bold
        if comment:
            from openpyxl.comments import Comment
            c.comment = Comment(comment, "Forge deep DCF")
        c.border = self.border
        return c


M_FMT = '#,##0.0;(#,##0.0)'
PCT = '0.0%'
PX = '$#,##0.00'


def build_workbook(path: Path, inputs: dict, a: dict, mk: dict, meta: dict) -> dict:
    """Write model.xlsx. → the cell map summary.json reports."""
    b = Book()
    ws = b.wb.active
    ws.title = "DCF"
    sym = str(inputs.get("symbol") or "?")
    company = clean_text(inputs.get("company") or sym, 80)
    ws.column_dimensions["A"].width = 46
    ws.column_dimensions["B"].width = 4
    ws.column_dimensions["C"].width = 16
    for col in COLS:
        ws.column_dimensions[col].width = 12

    ws["A1"] = f"{company} ({sym}) — discounted cash flow"
    ws["A1"].font = b.f_title
    ws["A2"] = (f"$ millions except per-share data. Built {meta['as_of']} from SEC XBRL filings and market "
                f"data; assumptions by {meta['model']}. Blue = input, black = formula.")

    # ── selector + market data ──────────────────────────────────────────────
    b.put(ws, "A4", "Case (1 = Bear, 2 = Base, 3 = Bull)", bold=True)
    b.put(ws, "C4", 2, "0", inp=True, comment="Change to 1 or 3 to switch every scenario assumption.")
    ws["C4"].fill = b.fill_key
    b.put(ws, "A5", "Active case")
    b.put(ws, "C5", '=CHOOSE($C$4,"Bear","Base","Bull")')
    b.header(ws, 7, "MARKET DATA, COST OF CAPITAL")
    cs = inputs.get("capital_structure") or {}
    rates = inputs.get("rates") or {}
    rows = [
        (8, "Share price ($)", mk["price"], PX, f"Source: inputs.json market.price — {(inputs.get('market') or {}).get('price_source', '')}"),
        (9, "Shares outstanding (millions)", mk["shares_m"], '#,##0.00', "Source: inputs.json market.shares_outstanding"),
        (10, "Market capitalisation", "=C8*C9", M_FMT, None),
        (11, "Total debt (incl. leases where reported)", mk["debt_m"], M_FMT, f"Source: inputs.json capital_structure.total_debt — {cs.get('source', '')}"),
        (12, "Cash and short-term investments", mk["cash_m"], M_FMT, f"Source: inputs.json capital_structure.total_cash — {cs.get('source', '')}"),
        (13, "Net debt", "=C11-C12", M_FMT, None),
        (14, f"Base-period revenue ({mk['revenue_label']})", mk["revenue_m"], M_FMT, "Source: inputs.json ltm.revenue / annual revenue, SEC XBRL"),
        (15, "Risk-free rate (10-year Treasury)", mk["rf"], '0.00%', f"Source: {rates.get('risk_free_source', 'inputs.json rates')}"),
        (16, "Beta", mk["beta"], '0.00', f"Source: {(inputs.get('market') or {}).get('beta_source', 'inputs.json market.beta')}"),
        (17, "Equity risk premium", mk["erp"], '0.00%', f"Source: {rates.get('equity_risk_premium_source', 'Forge standard assumption')}"),
        (18, "Cost of equity (CAPM)", "=C15+C16*C17", '0.00%', None),
        (19, "Pre-tax cost of debt", a["pretax_cost_of_debt"], '0.00%', f"Assumption by {meta['model']}"),
        # row 20 (tax for the debt shield) is written once the active block exists
        (21, "After-tax cost of debt", "=C19*(1-C20)", '0.00%', None),
        (22, "Equity weight", "=C10/(C10+MAX(C13,0))", PCT, None),
        (23, "Debt weight", "=1-C22", PCT, None),
        (24, "WACC before scenario adjustment", "=C18*C22+C21*C23", '0.00%', None),
    ]
    for r, label, val, fmt, src in rows:
        b.put(ws, f"A{r}", label)
        b.put(ws, f"C{r}", val, fmt, inp=src is not None and not str(val).startswith("="), comment=src)

    # ── scenario blocks ─────────────────────────────────────────────────────
    labels = {"revenue_growth": "Revenue growth", "gross_margin": "Gross margin",
              "operating_expense_pct": "Operating expenses (% of revenue)",
              "da_pct": "D&A (% of revenue)", "capex_pct": "Capex (% of revenue)",
              "tax_rate": "Tax rate on EBIT", "nwc_pct_of_revenue_change": "Working capital (% of revenue change)",
              "terminal_growth": "Terminal growth", "wacc_adjustment": "WACC adjustment to the CAPM rate"}
    pos: dict[str, dict] = {}
    r = 26
    for name in SCENARIOS + ("active",):
        title = ("ACTIVE CASE (follows the selector in C4)" if name == "active"
                 else f"{name.upper()} CASE ASSUMPTIONS")
        b.header(ws, r, title)
        r += 1
        b.put(ws, f"A{r}", "Assumption", bold=True)
        b.put(ws, f"C{r}", "Whole period", bold=True)
        for i, col in enumerate(COLS):
            b.put(ws, f"{col}{r}", f"Year {i + 1}", bold=True)
            ws[f"{col}{r}"].fill = b.fill_sub
        r += 1
        pos[name] = {}
        for key in VECTORS:
            b.put(ws, f"A{r}", labels[key])
            for i, col in enumerate(COLS):
                if name == "active":
                    v = (f"=CHOOSE($C$4,{col}{pos['bear'][key]},{col}{pos['base'][key]},"
                         f"{col}{pos['bull'][key]})")
                    b.put(ws, f"{col}{r}", v, PCT)
                else:
                    b.put(ws, f"{col}{r}", a["scenarios"][name][key][i], PCT, inp=True)
            pos[name][key] = r
            r += 1
        for key in SCALARS:
            b.put(ws, f"A{r}", labels[key])
            if name == "active":
                v = f"=CHOOSE($C$4,C{pos['bear'][key]},C{pos['base'][key]},C{pos['bull'][key]})"
                b.put(ws, f"C{r}", v, '0.00%')
            else:
                b.put(ws, f"C{r}", a["scenarios"][name][key], '0.00%', inp=True,
                      comment=f"Assumption by {meta['model']}" if key == "terminal_growth" else None)
            pos[name][key] = r
            r += 1
        if name == "active":
            b.put(ws, f"A{r}", "WACC (active case)", bold=True)
            b.put(ws, f"C{r}", f"=C24+C{pos['active']['wacc_adjustment']}", '0.00%', bold=True)
            pos["active"]["wacc"] = r
            r += 1
        r += 1
    act = pos["active"]
    b.put(ws, "A20", "Tax rate for the debt shield (active case)")
    b.put(ws, "C20", f"=C{act['tax_rate']}", PCT)
    WACC = f"$C${act['wacc']}"
    TG = f"$C${act['terminal_growth']}"

    # ── projections ─────────────────────────────────────────────────────────
    b.header(ws, r, "PROJECTED UNLEVERED FREE CASH FLOW")
    r += 1
    b.put(ws, f"A{r}", "$ millions", bold=True)
    b.put(ws, f"C{r}", mk["revenue_label"], bold=True)
    for i, col in enumerate(COLS):
        b.put(ws, f"{col}{r}", f"Year {i + 1}", bold=True)
        ws[f"{col}{r}"].fill = b.fill_sub
    r += 1
    P = {}
    for key in ("rev", "growth", "gp", "opex", "ebit", "margin", "tax", "nopat", "da", "capex",
                "nwc", "fcf", "per", "df", "pv"):
        P[key] = r
        r += 1
    titles = {"rev": "Revenue", "growth": "Revenue growth", "gp": "Gross profit",
              "opex": "Operating expenses", "ebit": "EBIT", "margin": "EBIT margin",
              "tax": "Taxes on EBIT", "nopat": "NOPAT", "da": "Plus: D&A", "capex": "Less: capex",
              "nwc": "Less: increase in working capital", "fcf": "Unlevered free cash flow",
              "per": "Discount period (mid-year)", "df": "Discount factor", "pv": "PV of free cash flow"}
    for key, row in P.items():
        b.put(ws, f"A{row}", titles[key], bold=key in ("rev", "ebit", "fcf"))
    b.put(ws, f"C{P['rev']}", "=C14", M_FMT)
    prev = "C"
    for col in COLS:
        b.put(ws, f"{col}{P['rev']}", f"={prev}{P['rev']}*(1+{col}{act['revenue_growth']})", M_FMT)
        b.put(ws, f"{col}{P['growth']}", f"={col}{P['rev']}/{prev}{P['rev']}-1", PCT)
        b.put(ws, f"{col}{P['gp']}", f"={col}{P['rev']}*{col}{act['gross_margin']}", M_FMT)
        b.put(ws, f"{col}{P['opex']}", f"={col}{P['rev']}*{col}{act['operating_expense_pct']}", M_FMT)
        b.put(ws, f"{col}{P['ebit']}", f"={col}{P['gp']}-{col}{P['opex']}", M_FMT, bold=True)
        b.put(ws, f"{col}{P['margin']}", f"={col}{P['ebit']}/{col}{P['rev']}", PCT)
        b.put(ws, f"{col}{P['tax']}", f"=MAX(0,{col}{P['ebit']})*$C${act['tax_rate']}", M_FMT)
        b.put(ws, f"{col}{P['nopat']}", f"={col}{P['ebit']}-{col}{P['tax']}", M_FMT)
        b.put(ws, f"{col}{P['da']}", f"={col}{P['rev']}*{col}{act['da_pct']}", M_FMT)
        b.put(ws, f"{col}{P['capex']}", f"={col}{P['rev']}*{col}{act['capex_pct']}", M_FMT)
        b.put(ws, f"{col}{P['nwc']}",
              f"=({col}{P['rev']}-{prev}{P['rev']})*$C${act['nwc_pct_of_revenue_change']}", M_FMT)
        b.put(ws, f"{col}{P['fcf']}",
              f"={col}{P['nopat']}+{col}{P['da']}-{col}{P['capex']}-{col}{P['nwc']}", M_FMT, bold=True)
        b.put(ws, f"{col}{P['per']}", 0.5 if col == "D" else f"={prev}{P['per']}+1", '0.0')
        b.put(ws, f"{col}{P['df']}", f"=1/(1+{WACC})^{col}{P['per']}", '0.0000')
        b.put(ws, f"{col}{P['pv']}", f"={col}{P['fcf']}*{col}{P['df']}", M_FMT)
        prev = col
    r += 1

    # ── valuation ───────────────────────────────────────────────────────────
    b.header(ws, r, "VALUATION")
    r += 1
    V = {}
    vrows = [
        ("sum_pv", "Sum of PV of free cash flow", f"=SUM(D{P['pv']}:{LAST}{P['pv']})", M_FMT),
        ("tfcf", "Terminal-year FCF x (1 + g)", f"={LAST}{P['fcf']}*(1+{TG})", M_FMT),
        ("tv", "Terminal value (Gordon growth)", None, M_FMT),
        ("pv_tv", "PV of terminal value", None, M_FMT),
        ("ev", "Enterprise value", None, M_FMT),
        ("nd", "Less: net debt", "=C13", M_FMT),
        ("eq", "Equity value", None, M_FMT),
        ("sh", "Shares outstanding (millions)", "=C9", '#,##0.00'),
        ("px", "Implied value per share ($)", None, PX),
        ("cur", "Current share price ($)", "=C8", PX),
        ("up", "Upside / (downside)", None, PCT),
        ("tvf", "Terminal value as % of enterprise value", None, PCT),
    ]
    for key, label, _, _ in vrows:
        V[key] = r
        r += 1
    formulas = {
        "tv": f"=C{V['tfcf']}/({WACC}-{TG})",
        "pv_tv": f"=C{V['tv']}/(1+{WACC})^{LAST}{P['per']}",
        "ev": f"=C{V['sum_pv']}+C{V['pv_tv']}",
        "eq": f"=C{V['ev']}-C13",
        "px": f"=(C{V['ev']}-C13)/C9",
        "up": f"=C{V['px']}/C8-1",
        "tvf": f"=C{V['pv_tv']}/C{V['ev']}",
    }
    for key, label, f, fmt in vrows:
        b.put(ws, f"A{V[key]}", label, bold=key in ("ev", "px"))
        b.put(ws, f"C{V[key]}", formulas.get(key, f), fmt, bold=key in ("ev", "px"))
    ws[f"C{V['px']}"].fill = b.fill_key
    r += 1

    # ── sensitivity tables ──────────────────────────────────────────────────
    FCF = f"$D${P['fcf']}:${LAST}${P['fcf']}"
    PER = f"$D${P['per']}:${LAST}${P['per']}"
    LFCF, LPER = f"${LAST}${P['fcf']}", f"${LAST}${P['per']}"

    def price_expr(w: str, g: str, fcf_rng: str = FCF, last_fcf: str = LFCF) -> str:
        return (f"(SUMPRODUCT({fcf_rng},1/(1+{w})^{PER})+{last_fcf}*(1+{g})/({w}-{g})/(1+{w})^{LPER}"
                f"-$C$13)/$C$9")

    centers = []

    def grid(title: str, corner: str, row_vals, col_vals, cell, row_fmt, col_fmt):
        nonlocal r
        b.header(ws, r, title, last_col="H")
        r += 1
        b.put(ws, f"C{r}", corner, bold=True)
        hdr = r
        for j, v in enumerate(col_vals):
            b.put(ws, f"{COLS[j]}{hdr}", v, col_fmt, bold=True)
            ws[f"{COLS[j]}{hdr}"].fill = b.fill_sub
        r += 1
        for i, v in enumerate(row_vals):
            b.put(ws, f"C{r}", v, row_fmt, bold=True)
            ws[f"C{r}"].fill = b.fill_sub
            for j in range(len(col_vals)):
                ref = f"{COLS[j]}{r}"
                b.put(ws, ref, cell(i, j, f"$C{r}", f"{COLS[j]}${hdr}"), PX)
                if i == 2 and j == 2:
                    ws[ref].fill = b.fill_center
                    ws[ref].font = b.f_bold
                    centers.append(f"DCF!{ref}")
            r += 1
        r += 1

    # 1. WACC x terminal growth — a full re-discount for every pair
    grid("SENSITIVITY: VALUE PER SHARE — WACC (rows) x TERMINAL GROWTH (columns)", "WACC \\ g",
         [f"={WACC}{k * SENS_STEP_W:+.4f}" if k else f"={WACC}" for k in (-2, -1, 0, 1, 2)],
         [f"={TG}{k * SENS_STEP_G:+.4f}" if k else f"={TG}" for k in (-2, -1, 0, 1, 2)],
         lambda i, j, w, g: f'=IF({w}-{g}<=0.0025,"n/m",{price_expr(w, g)})', '0.00%', '0.00%')

    # 2. revenue growth x EBIT margin — every cell re-projects the cash flows
    sc = b.wb.create_sheet("SensCalc")
    sc["A1"] = "Helper rows for the growth x margin sensitivity table on the DCF sheet (live formulas)."
    sc.column_dimensions["A"].width = 34
    rev_rows = {}
    rr = 3
    for i, dg in enumerate(SENS_GROWTH):
        sc[f"A{rr}"] = f"Revenue, growth shifted {dg * 100:+.0f} pts"
        sc[f"B{rr}"] = dg
        sc[f"C{rr}"] = "=DCF!$C$14"
        prev = "C"
        for col in COLS:
            sc[f"{col}{rr}"] = f"={prev}{rr}*(1+DCF!{col}${act['revenue_growth']}+$B{rr})"
            prev = col
        rev_rows[i] = rr
        rr += 1
    rr += 1
    fcf_rows = {}
    for i, dg in enumerate(SENS_GROWTH):
        for j, dm in enumerate(SENS_MARGIN):
            sc[f"A{rr}"] = f"FCF, growth {dg * 100:+.0f} pts, margin {dm * 100:+.0f} pts"
            sc[f"B{rr}"] = dg
            sc[f"C{rr}"] = dm
            rv = rev_rows[i]
            prev = "C"
            for col in COLS:
                ebit = f"{col}{rv}*(DCF!{col}${act['gross_margin']}-DCF!{col}${act['operating_expense_pct']}+$C{rr})"
                sc[f"{col}{rr}"] = (f"={ebit}-MAX(0,{ebit})*DCF!$C${act['tax_rate']}"
                                    f"+{col}{rv}*(DCF!{col}${act['da_pct']}-DCF!{col}${act['capex_pct']})"
                                    f"-({col}{rv}-{prev}{rv})*DCF!$C${act['nwc_pct_of_revenue_change']}")
                prev = col
            fcf_rows[(i, j)] = rr
            rr += 1

    def growth_margin_cell(i, j, _w, _g):
        k = fcf_rows[(i, j)]
        return "=" + price_expr(WACC, TG, f"SensCalc!$D${k}:${LAST}${k}", f"SensCalc!${LAST}${k}")

    grid("SENSITIVITY: VALUE PER SHARE — REVENUE GROWTH SHIFT (rows) x EBIT MARGIN SHIFT (columns)",
         "growth \\ margin", list(SENS_GROWTH), list(SENS_MARGIN), growth_margin_cell, '+0.0%;-0.0%;0.0%',
         '+0.0%;-0.0%;0.0%')

    # 3. beta x risk-free rate — CAPM rebuilt for every pair
    def beta_rf_cell(_i, _j, beta, rf):
        w = f"(({rf}+{beta}*$C$17)*$C$22+$C$21*$C$23+$C${act['wacc_adjustment']})"
        return f'=IF({w}-{TG}<=0.0025,"n/m",{price_expr(w, TG)})'

    grid("SENSITIVITY: VALUE PER SHARE — BETA (rows) x RISK-FREE RATE (columns)", "beta \\ rf",
         [f"=$C$16{k:+.2f}" if k else "=$C$16" for k in SENS_BETA],
         [f"=$C$15{k:+.4f}" if k else "=$C$15" for k in SENS_RF], beta_rf_cell, '0.00', '0.00%')

    _history_sheet(b, inputs, mk)
    _notes_sheet(b, a, meta, mk)
    ws.freeze_panes = "B4"
    b.wb.save(path)
    return {
        "implied_price": f"DCF!C{V['px']}", "wacc": f"DCF!C{act['wacc']}",
        "terminal_growth": f"DCF!C{act['terminal_growth']}", "enterprise_value": f"DCF!C{V['ev']}",
        "pv_terminal_value": f"DCF!C{V['pv_tv']}", "net_debt": "DCF!C13", "diluted_shares": "DCF!C9",
        "base_revenue": "DCF!C14", "sensitivity_centers": centers,
    }


def _history_sheet(b: Book, inputs: dict, mk: dict) -> None:
    ws = b.wb.create_sheet("History")
    ws.column_dimensions["A"].width = 30
    annual = inputs.get("annual") or {}
    ends = list(annual.get("fiscal_year_ends") or [])
    flows = annual.get("income_and_cash_flow") or {}
    ltm = inputs.get("ltm") or {}
    ws["A1"] = "Reported history, $ millions (SEC XBRL; LTM = latest fiscal year + current YTD − prior YTD)"
    ws["A1"].font = b.f_bold
    cols = [chr(ord("B") + i) for i in range(len(ends) + 1)]
    for i, e in enumerate(ends):
        b.put(ws, f"{cols[i]}3", f"FY {e}", bold=True)
    b.put(ws, f"{cols[len(ends)]}3", "LTM", bold=True)
    lines = [("revenue", "Revenue"), ("gross_profit", "Gross profit"), ("operating_income", "Operating income"),
             ("net_income", "Net income"), ("depreciation_amortization", "D&A"), ("capex", "Capex"),
             ("operating_cash_flow", "Operating cash flow"), ("stock_based_compensation", "Stock-based comp")]
    row_of = {}
    r = 4
    for key, label in lines:
        b.put(ws, f"A{r}", label)
        series = list(flows.get(key) or []) + [((ltm.get(key) or {}).get("value"))]
        for i, col in enumerate(cols):
            v = _num(series[i]) if i < len(series) else None
            if v is not None:
                b.put(ws, f"{col}{r}", v / 1e6, M_FMT, inp=True)
        row_of[key] = r
        r += 1
    r += 1
    ratios = [("Revenue growth", None), ("Gross margin", "gross_profit"), ("Operating margin", "operating_income"),
              ("D&A % of revenue", "depreciation_amortization"), ("Capex % of revenue", "capex"),
              ("SBC % of revenue", "stock_based_compensation")]
    rv = row_of["revenue"]
    for label, key in ratios:
        b.put(ws, f"A{r}", label)
        for i, col in enumerate(cols):
            if key is None:
                if 0 < i < len(ends):
                    p = cols[i - 1]
                    f = f'=IF(AND(ISNUMBER({col}{rv}),ISNUMBER({p}{rv}),{p}{rv}<>0),{col}{rv}/{p}{rv}-1,"")'
                    b.put(ws, f"{col}{r}", f, PCT)
                continue
            n = row_of[key]
            f = f'=IF(AND(ISNUMBER({col}{n}),ISNUMBER({col}{rv}),{col}{rv}<>0),{col}{n}/{col}{rv},"")'
            b.put(ws, f"{col}{r}", f, PCT)
        r += 1


def _notes_sheet(b: Book, a: dict, meta: dict, mk: dict) -> None:
    ws = b.wb.create_sheet("Notes")
    ws.column_dimensions["A"].width = 22
    ws.column_dimensions["B"].width = 110
    rows = [("Thesis", a["thesis"])]
    rows += [(f"Assumption {i + 1}", t) for i, t in enumerate(a["key_assumptions"])]
    rows += [(f"Caveat {i + 1}", t) for i, t in enumerate(a["caveats"])]
    rows += [
        ("Method", f"{YEARS}-year unlevered free cash flow (NOPAT + D&A − capex − increase in working "
                   "capital), mid-year discounting at WACC, Gordon-growth terminal value on year-"
                   f"{YEARS} free cash flow, equity value = enterprise value − net debt. Stock-based "
                   "compensation is treated as a real cost (not added back)."),
        ("Base period", f"{mk['revenue_label']}: revenue ${mk['revenue_m']:,.1f}M"),
        ("Assumptions by", f"{meta['model']} via the Message Batches API ({meta['as_of']})"),
        ("Data", "SEC XBRL company facts (statements), Forge fundamentals cache (price, beta, debt, cash), "
                 "FRED DGS10 (risk-free rate)"),
        ("Status", "Advisory second opinion. Not used by any Forge trader or score."),
    ]
    for i, (k, v) in enumerate(rows, start=1):
        ws[f"A{i}"] = k
        ws[f"A{i}"].font = b.f_bold
        ws[f"B{i}"] = clean_text(v, 1500)
        ws[f"B{i}"].alignment = b.wrap


def build(workdir: Path, inputs_path: Path, assumptions_path: Path) -> dict:
    inputs = json.loads(inputs_path.read_text(encoding="utf-8"))
    raw = json.loads(assumptions_path.read_text(encoding="utf-8"))
    meta_in = raw.get("_meta") if isinstance(raw, dict) else None
    a, errs = validate(raw)
    if errs:
        return {"ok": False, "errors": errs, "values": {}}
    mk = market_inputs(inputs)
    vals, errs = evaluate(mk, a)
    if errs:
        return {"ok": False, "errors": errs, "values": {}}
    meta = {"model": clean_text((meta_in or {}).get("model") or "Claude", 40),
            "as_of": clean_text(inputs.get("as_of") or date.today().isoformat(), 10)}
    cells = build_workbook(workdir / "model.xlsx", inputs, a, mk, meta)
    summary = {
        "symbol": str(inputs.get("symbol") or ""),
        "price": mk["price"],
        "scenario_selector_cell": "DCF!C4",
        "cells": cells,
        "scenarios": {n: {"fair_value": vals[n]["fair_value"], "wacc": vals[n]["wacc"],
                          "terminal_growth": vals[n]["terminal_growth"]} for n in SCENARIOS},
        "key_assumptions": a["key_assumptions"],
        "thesis": a["thesis"],
        "caveats": a["caveats"],
        "engine": "batch",
    }
    tmp = workdir / "summary.json.tmp"
    tmp.write_text(json.dumps(summary, indent=1), encoding="utf-8")
    tmp.replace(workdir / "summary.json")
    return {"ok": True, "errors": [], "values": {
        n: {"fair_value": round(vals[n]["fair_value"], 4), "wacc": round(vals[n]["wacc"], 5),
            "tv_fraction": round(vals[n]["tv_fraction"], 4)} for n in SCENARIOS}}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--dir", default=".")
    ap.add_argument("--inputs", default=None)
    ap.add_argument("--assumptions", default=None)
    a = ap.parse_args(argv)
    workdir = Path(a.dir).resolve()
    try:
        result = build(workdir, Path(a.inputs) if a.inputs else workdir / "inputs.json",
                       Path(a.assumptions) if a.assumptions else workdir / "assumptions.json")
    except BuildError as e:
        sys.stdout.write(json.dumps({"ok": False, "errors": [str(e)], "values": {}}) + "\n")
        return 1
    except Exception as e:                      # a tool must always answer in JSON
        sys.stdout.write(json.dumps({"ok": False, "errors": [f"builder failed: {type(e).__name__}: {e}"],
                                     "values": {}, "crash": True}) + "\n")
        return 2
    sys.stdout.write(json.dumps(result) + "\n")
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
