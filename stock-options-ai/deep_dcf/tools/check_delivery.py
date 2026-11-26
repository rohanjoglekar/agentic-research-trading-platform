#!/usr/bin/env python3
"""Acceptance check for a delivered DCF model: model.xlsx + summary.json.

    python3 check_delivery.py [--dir .] [--inputs inputs.json] [--accept-json '{…}']

Prints ONE JSON object: {"verdict": "PASS"|"FAIL", "errors": […], "warnings": […],
"values": {…}}. Exit 0 = PASS, 1 = FAIL, 2 = the check itself could not run.

The same script is run twice: by the model on its own work before it finishes
(so it can fix what fails), and afterwards by the pipeline, which is the run
that counts. Nothing is taken from summary.json on trust — every number is read
back from the workbook after LibreOffice recalculates it, three times: as
delivered (Base), with the case selector set to Bear, and set to Bull.

Because the model iterates against this check until it passes, the cheapest
"fix" for a failing check is to re-point a cell reference at something that
satisfies it. So the reported cells are also tied to EACH OTHER: in every case
the implied price must equal (enterprise value − net debt) / shares, the cells
must be distinct, and a sensitivity centre must be a real recalculation sitting
in a grid whose neighbours differ from it.

Command-line tool: stdout is its interface, so it prints rather than logs.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import re
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import recalc  # noqa: E402  (sibling tool)

MAX_JSON_BYTES = 2 * 1024 * 1024
SCENARIOS = ("bear", "base", "bull")
SELECTOR_VALUE = {"bear": 1, "base": 2, "bull": 3}
REQUIRED_CELLS = ("implied_price", "wacc", "terminal_growth", "enterprise_value",
                  "pv_terminal_value", "net_debt", "diluted_shares", "base_revenue")
BRIDGE_CELLS = ("implied_price", "wacc", "terminal_growth", "enterprise_value",
                "net_debt", "diluted_shares")          # re-read under Bear and Bull
DEFAULT_ACCEPT = {
    "value_tol": 0.005, "wacc_min": 0.05, "wacc_max": 0.16, "tg_min": 0.0, "tg_max": 0.045,
    "min_wacc_tg_spread": 0.02, "tv_fraction_warn": 0.80, "tv_fraction_max": 0.90,
    "tv_fraction_min": 0.20, "fair_to_price_min": 0.2, "fair_to_price_max": 5.0,
    "min_formulas": 150, "price_tol": 0.01, "shares_tol": 0.10, "revenue_base_tol": 0.10,
}
_UNIT_FACTORS = (1.0, 1e3, 1e6, 1e9)                    # a model may be in $, $K, $M or $B
_BRIDGE_FACTORS = (1.0, 1e3, 1e6, 1e9, 1e-3, 1e-6, 1e-9)   # money unit ÷ share unit
_BARE_REF = re.compile(r"^=\s*\+?\s*(?:(?:'[^']+'|[A-Za-z0-9_.]+)!)?\$?[A-Za-z]{1,3}\$?\d+\s*$")
_COORD = re.compile(r"^([A-Z]{1,3})(\d+)$")
EPS = 1e-9


def _isnum(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def _close(a: float, b: float, tol: float) -> bool:
    return abs(a - b) <= tol * max(abs(a), abs(b), 1e-12)


def _scaled_match(model_val: float, raw_val: float, tol: float):
    """Does model_val equal raw_val under some unit scale? → the factor, or None."""
    for f in _UNIT_FACTORS:
        if _close(model_val * f, raw_val, tol):
            return f
    return None


def _col_to_n(col: str) -> int:
    n = 0
    for ch in col:
        n = n * 26 + (ord(ch) - 64)
    return n


def _n_to_col(n: int) -> str:
    out = ""
    while n > 0:
        n, r = divmod(n - 1, 26)
        out = chr(65 + r) + out
    return out


def neighbours(ref: str) -> list[str]:
    """The four cells around `ref` (up, down, left, right) that exist."""
    sheet, coord = recalc.parse_cell(ref)
    m = _COORD.match(coord)
    col, row = _col_to_n(m.group(1)), int(m.group(2))
    out = []
    for dc, dr in ((0, -1), (0, 1), (-1, 0), (1, 0)):
        c, r = col + dc, row + dr
        if c >= 1 and r >= 1:
            out.append(f"{sheet}!{_n_to_col(c)}{r}")
    return out


def load_json(path: Path) -> dict:
    if not path.is_file():
        raise FileNotFoundError(f"{path.name} is missing")
    if path.stat().st_size > MAX_JSON_BYTES:
        raise ValueError(f"{path.name} is too large")
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"{path.name} must be a JSON object")
    return data


def summary_shape_errors(summary: dict) -> list[str]:
    """Structural problems in summary.json (types, required keys, distinct cells)."""
    errs: list[str] = []
    if not isinstance(summary.get("symbol"), str):
        errs.append("summary.symbol must be a string")
    if not _isnum(summary.get("price")):
        errs.append("summary.price must be a finite number")
    refs: dict[str, str] = {}

    def ref(value, label: str, want: str) -> None:
        try:
            sheet, coord = recalc.parse_cell(value if isinstance(value, str) else "")
        except ValueError:
            errs.append(f"{label} must be a cell reference like {want}")
            return
        key = f"{sheet}!{coord}"
        if key in refs:
            errs.append(f"{label} and {refs[key]} point at the same cell {key}; each must be its own cell")
        else:
            refs[key] = label

    ref(summary.get("scenario_selector_cell"), "summary.scenario_selector_cell", "DCF!B6")
    cells = summary.get("cells")
    if not isinstance(cells, dict):
        errs.append("summary.cells must be an object")
        cells = {}
    for k in REQUIRED_CELLS:
        ref(cells.get(k), f"summary.cells.{k}", "DCF!C95")
    centers = cells.get("sensitivity_centers")
    if not isinstance(centers, list) or len(centers) < 3:
        errs.append("summary.cells.sensitivity_centers must list the centre cell of each of the three sensitivity tables")
    else:
        for i, c in enumerate(centers):
            ref(c, f"summary.cells.sensitivity_centers[{i}]", "DCF!D110")
    scen = summary.get("scenarios")
    if not isinstance(scen, dict):
        errs.append("summary.scenarios must be an object with bear/base/bull")
        scen = {}
    for name in SCENARIOS:
        s = scen.get(name)
        if not isinstance(s, dict):
            errs.append(f"summary.scenarios.{name} is missing")
            continue
        for k in ("fair_value", "wacc", "terminal_growth"):
            if not _isnum(s.get(k)):
                errs.append(f"summary.scenarios.{name}.{k} must be a finite number")
    ka = summary.get("key_assumptions")
    if (not isinstance(ka, list) or not 3 <= len(ka) <= 8
            or not all(isinstance(x, str) and x.strip() for x in ka)):
        errs.append("summary.key_assumptions must be 3-8 non-empty strings")
    return errs


def _cell(result: dict, ref: str) -> dict:
    sheet, coord = recalc.parse_cell(ref)
    return (result.get("cells") or {}).get(f"{sheet}!{coord}") or {}


def evaluate(summary: dict, inputs: dict, accept: dict, base: dict, bear: dict, bull: dict,
             validator: dict | None) -> dict:
    """PURE: decide PASS/FAIL from summary.json, inputs.json, the three recalc
    results (as delivered, selector=Bear, selector=Bull) and the validator."""
    A = {**DEFAULT_ACCEPT, **(accept or {})}
    errors: list[str] = summary_shape_errors(summary)
    warnings: list[str] = []
    values: dict = {}

    def done() -> dict:
        return {"verdict": "PASS" if not errors else "FAIL", "errors": errors,
                "warnings": warnings, "values": values}

    if errors:
        return done()
    cells = summary["cells"]
    scen = summary["scenarios"]
    tol = float(A["value_tol"])
    results = {"bear": bear, "base": base, "bull": bull}

    # ── the workbook itself ─────────────────────────────────────────────────
    for name, res in (("as delivered", base), ("with Bear selected", bear), ("with Bull selected", bull)):
        if res.get("status") == "failed":
            errors.append(f"recalculation {name} failed: {res.get('message')}")
        elif res.get("total_errors"):
            where = "; ".join(f"{k} x{v['count']} at {', '.join(v['locations'][:5])}"
                              for k, v in (res.get("error_summary") or {}).items())
            errors.append(f"formula errors {name}: {where}")
    if errors:
        return done()
    n_formulas = int(base.get("total_formulas") or 0)
    values["total_formulas"] = n_formulas
    if n_formulas < int(A["min_formulas"]):
        errors.append(f"only {n_formulas} formula cells (need ≥ {int(A['min_formulas'])}) — "
                      "projections and sensitivity cells must be live formulas, not pasted numbers")

    sel = _cell(base, summary["scenario_selector_cell"])
    if sel.get("value") != SELECTOR_VALUE["base"]:
        errors.append(f"the case selector {summary['scenario_selector_cell']} must be saved as 2 (Base); "
                      f"it is {sel.get('value')!r}")
    if sel.get("formula"):
        errors.append("the case selector must be a plain input (1/2/3), not a formula")

    def read(res: dict, key: str, label: str):
        c = _cell(res, cells[key])
        if "error" in c:
            errors.append(f"cells.{key} → {cells[key]}: {c['error']}")
            return None
        if not _isnum(c.get("value")):
            errors.append(f"cells.{key} → {cells[key]} is not a number {label} (got {c.get('value')!r})")
            return None
        return float(c["value"])

    got: dict[str, dict] = {}
    for name in SCENARIOS:
        label = f"with {name.title()} selected"
        got[name] = {k: read(results[name], k, label) for k in BRIDGE_CELLS}
    for key in ("implied_price", "enterprise_value", "pv_terminal_value"):
        if not _cell(base, cells[key]).get("formula"):
            errors.append(f"cells.{key} → {cells[key]} must be a formula")
    pv_tv = read(base, "pv_terminal_value", "as delivered")
    base_rev = read(base, "base_revenue", "as delivered")
    if errors:
        return done()
    ev, net_debt, shares = (got["base"][k] for k in ("enterprise_value", "net_debt", "diluted_shares"))

    # ── summary.json must say what the workbook computes ────────────────────
    for name in SCENARIOS:
        for k, s_key, abs_tol in (("implied_price", "fair_value", None), ("wacc", "wacc", 0.0005),
                                  ("terminal_growth", "terminal_growth", 0.0005)):
            said, real = float(scen[name][s_key]), got[name][k]
            ok = abs(said - real) <= abs_tol if abs_tol is not None else _close(said, real, tol)
            if not ok:
                errors.append(f"summary.scenarios.{name}.{s_key} = {said:g} but the recalculated workbook "
                              f"gives {real:g} with the selector on {SELECTOR_VALUE[name]}")
    fv = {n: got[n]["implied_price"] for n in SCENARIOS}
    values["scenarios"] = {n: {"fair_value": round(fv[n], 2), "wacc": round(got[n]["wacc"], 4),
                               "terminal_growth": round(got[n]["terminal_growth"], 4)}
                           for n in SCENARIOS}

    # ── the reported cells must be tied to each other, in every case ────────
    bridge_factor = None
    for name in SCENARIOS:
        g = got[name]
        if not g["diluted_shares"] > 0:
            errors.append(f"diluted shares are not positive with {name.title()} selected ({g['diluted_shares']:g})")
            continue
        per_share = (g["enterprise_value"] - g["net_debt"]) / g["diluted_shares"]
        hit = next((f for f in _BRIDGE_FACTORS if _close(per_share * f, g["implied_price"], tol)), None)
        if hit is None:
            errors.append(
                f"valuation bridge does not hold with {name.title()} selected: (enterprise value "
                f"{g['enterprise_value']:g} − net debt {g['net_debt']:g}) / shares {g['diluted_shares']:g} "
                f"= {per_share:g}, but cells.implied_price is {g['implied_price']:g}. Point "
                "cells.enterprise_value, net_debt and diluted_shares at the cells the implied price is "
                "actually computed from, and fold any other claims (leases, minorities, preferred) into "
                "the net debt cell")
        elif name == "base":
            bridge_factor = hit

    for ref in cells["sensitivity_centers"]:
        c = _cell(base, ref)
        formula = c.get("formula")
        if not formula:
            errors.append(f"sensitivity centre {ref} must be a formula (a full DCF recalculation)")
            continue
        if _BARE_REF.match(formula):
            errors.append(f"sensitivity centre {ref} is just a reference ({formula}); every cell of a "
                          "sensitivity table must recalculate the DCF for its own row and column values")
            continue
        if not _isnum(c.get("value")) or not _close(float(c["value"]), fv["base"], tol):
            errors.append(f"sensitivity centre {ref} = {c.get('value')!r} but the base implied price is "
                          f"{fv['base']:.4f}; the middle of each table must equal the base case")
            continue
        around = [_cell(base, n) for n in neighbours(ref)]
        varying = sum(1 for n in around if n.get("formula") and _isnum(n.get("value"))
                      and not _close(float(n["value"]), float(c["value"]), 1e-4))
        if varying < 3:
            errors.append(f"sensitivity centre {ref} is not inside a working grid: only {varying} of the "
                          "four cells around it are formulas with a different value. The centre must sit "
                          "in the middle of a 5x5 table whose cells each recalculate the DCF")

    # ── economics ───────────────────────────────────────────────────────────
    if not (fv["bear"] < fv["bull"] and fv["bear"] <= fv["base"] <= fv["bull"]):
        errors.append(f"scenario values out of order: bear {fv['bear']:.2f}, base {fv['base']:.2f}, "
                      f"bull {fv['bull']:.2f} (need bear ≤ base ≤ bull, bear < bull)")
    for name in SCENARIOS:
        w, g = got[name]["wacc"], got[name]["terminal_growth"]
        if not A["wacc_min"] - EPS <= w <= A["wacc_max"] + EPS:
            errors.append(f"{name} WACC {w:.2%} is outside {A['wacc_min']:.0%}-{A['wacc_max']:.0%}")
        if not A["tg_min"] - EPS <= g <= A["tg_max"] + EPS:
            errors.append(f"{name} terminal growth {g:.2%} is outside {A['tg_min']:.1%}-{A['tg_max']:.1%}")
        if w - g < A["min_wacc_tg_spread"] - EPS:
            errors.append(f"{name} WACC {w:.2%} minus terminal growth {g:.2%} is under "
                          f"{A['min_wacc_tg_spread']:.0%} — the terminal value is unstable")
    if not ev > 0:
        errors.append(f"enterprise value is not positive ({ev:g})")
    else:
        tvf = pv_tv / ev
        values["tv_fraction"] = round(tvf, 4)
        if tvf > A["tv_fraction_max"]:
            errors.append(f"terminal value is {tvf:.0%} of enterprise value (limit {A['tv_fraction_max']:.0%}); "
                          "lengthen the projection or fade growth explicitly instead of leaning on the terminal")
        elif tvf < A["tv_fraction_min"]:
            errors.append(f"cells.pv_terminal_value is {tvf:.0%} of enterprise value — under "
                          f"{A['tv_fraction_min']:.0%}, so that cell is not the present value of the terminal value")
        elif tvf > A["tv_fraction_warn"]:
            warnings.append(f"terminal value is {tvf:.0%} of enterprise value")

    # ── fidelity to inputs.json ─────────────────────────────────────────────
    market = inputs.get("market") or {}
    price = market.get("price")
    if _isnum(price) and price > 0:
        if not _close(float(summary["price"]), float(price), A["price_tol"]):
            errors.append(f"summary.price {summary['price']:g} does not match inputs.json price {price:g}")
        ratio = fv["base"] / float(price)
        values["price"] = float(price)
        values["upside_pct"] = {n: round((fv[n] / float(price) - 1.0) * 100.0, 1) for n in SCENARIOS}
        if not A["fair_to_price_min"] <= ratio <= A["fair_to_price_max"]:
            errors.append(f"base fair value is {ratio:.2f}x the price — outside "
                          f"{A['fair_to_price_min']}x-{A['fair_to_price_max']}x, which is a modelling "
                          "artifact (units, share count or margin path), not a price target")
    else:
        errors.append("inputs.json has no price")

    raw_shares = market.get("shares_outstanding")
    if _isnum(raw_shares) and raw_shares > 0:
        # The current count is the reference. The SEC diluted average and the
        # cover-page count are accepted too, but only when they agree with it
        # (within 10%) — after a split they describe a different share.
        refs = [float(raw_shares)]
        annual_shares = ((inputs.get("annual") or {}).get("shares") or {}).get("diluted_shares") or []
        extra = [v for v in annual_shares if _isnum(v)][-1:]
        cover = (inputs.get("cover_page_shares") or {}).get("value")
        if _isnum(cover):
            extra.append(cover)
        refs += [float(v) for v in extra if v > 0 and abs(float(v) / float(raw_shares) - 1.0) <= 0.10]
        if not any(_scaled_match(shares, r, A["shares_tol"]) is not None for r in refs):
            errors.append(f"diluted shares in the model ({shares:g}) are not within "
                          f"{A['shares_tol']:.0%} of the share count in inputs.json "
                          f"({', '.join(f'{r:g}' for r in refs)}) under any unit ($, K, M, B)")

    rev_refs = []
    ltm = (inputs.get("ltm") or {}).get("revenue") or {}
    if _isnum(ltm.get("value")):
        rev_refs.append(float(ltm["value"]))
    annual_rev = (((inputs.get("annual") or {}).get("income_and_cash_flow") or {}).get("revenue") or [])
    if annual_rev and _isnum(annual_rev[-1]):
        rev_refs.append(float(annual_rev[-1]))
    if _isnum((inputs.get("ttm") or {}).get("revenue")):
        rev_refs.append(float(inputs["ttm"]["revenue"]))
    money_factor = next((f for f in (_scaled_match(base_rev, r, A["revenue_base_tol"]) for r in rev_refs)
                         if f is not None), None)
    if rev_refs and money_factor is None:
        errors.append(f"cells.base_revenue ({base_rev:g}) matches neither LTM nor latest-fiscal-year "
                      "revenue in inputs.json under any unit")

    raw_nd = (inputs.get("capital_structure") or {}).get("net_debt")
    if _isnum(raw_nd) and abs(raw_nd) > 0:
        nd_factor = _scaled_match(net_debt, float(raw_nd), 0.25)
        if nd_factor is None:
            warnings.append(f"net debt in the model ({net_debt:g}) differs from inputs.json "
                            f"net_debt ({raw_nd:g}) by more than 25%")
        elif money_factor is not None and nd_factor != money_factor:
            errors.append(f"net debt ({net_debt:g}) is in different units from revenue ({base_rev:g}): "
                          "one of them is off by a factor of a thousand or more")
    values.update({"enterprise_value": ev, "pv_terminal_value": pv_tv, "net_debt": net_debt,
                   "diluted_shares": shares, "base_revenue": base_rev, "bridge_unit_factor": bridge_factor})

    # ── the skill's own validator: recorded, never decisive ─────────────────
    # It finds WACC, terminal growth and terminal value by scanning for labels
    # and misreads them ("Terminal value is 0.0% of EV" on a model where it is
    # 57%; a label containing "terminal" and "growth" next to 0.62 becomes
    # "terminal growth 62% >= WACC"). Everything it looks for is verified
    # exactly above, so its output is kept for the record only.
    if validator is not None:
        notes = [str(x)[:200] for x in ((validator.get("errors") or []) + (validator.get("warnings") or []))[:10]]
        values["validator"] = {"status": validator.get("status"), "notes": notes}
    return done()


def run_validator(xlsx: Path) -> dict | None:
    """The dcf-model skill's validate_dcf.py on the recalculated workbook."""
    skill = os.environ.get("DCF_SKILL", "")
    script = Path(skill) / "scripts" / "validate_dcf.py" if skill else None
    if not script or not script.is_file():
        return None
    try:
        p = subprocess.run([sys.executable, str(script), str(xlsx)], capture_output=True,
                           text=True, timeout=120)
        return json.loads(p.stdout)
    except Exception as e:
        return {"status": "ERROR", "errors": [f"validator crashed: {type(e).__name__}: {e}"]}


def check(workdir: Path, inputs_path: Path, accept: dict, timeout: int = 180) -> dict:
    summary = load_json(workdir / "summary.json")
    inputs = load_json(inputs_path)
    model = workdir / "model.xlsx"
    if not model.is_file():
        return {"verdict": "FAIL", "errors": ["model.xlsx is missing"], "warnings": [], "values": {}}
    shape = summary_shape_errors(summary)
    if shape:
        return {"verdict": "FAIL", "errors": shape, "warnings": [], "values": {}}
    cells = summary["cells"]
    selector = summary["scenario_selector_cell"]
    centres = list(cells["sensitivity_centers"])
    wanted = [selector] + [cells[k] for k in REQUIRED_CELLS] + centres
    for c in centres:
        wanted += neighbours(c)

    def safe(**kw) -> dict:
        try:
            return recalc.run(model, timeout=timeout, **kw)
        except Exception as e:       # RecalcError, bad reference, corrupt workbook…
            return {"status": "failed", "message": f"{type(e).__name__}: {e}"}

    base = safe(cells=wanted)
    scenario_cells = [cells[k] for k in BRIDGE_CELLS]
    bear = safe(cells=scenario_cells, sets=[f"{selector}=1"])
    bull = safe(cells=scenario_cells, sets=[f"{selector}=3"])
    validator = None
    if base.get("status") != "failed" and base.get("recalculated_file"):
        validator = run_validator(Path(base["recalculated_file"]))
    return evaluate(summary, inputs, accept, base, bear, bull, validator)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--dir", default=".")
    ap.add_argument("--inputs", default=None)
    ap.add_argument("--accept-json", default=None)
    ap.add_argument("--timeout", type=int, default=180)
    a = ap.parse_args(argv)
    workdir = Path(a.dir).resolve()
    try:
        raw = a.accept_json or os.environ.get("DCF_ACCEPT_JSON") or "{}"
        accept = json.loads(raw)
        if not isinstance(accept, dict):
            raise ValueError("accept must be a JSON object")
        result = check(workdir, Path(a.inputs) if a.inputs else workdir / "inputs.json",
                       accept, timeout=a.timeout)
    except Exception as e:
        sys.stdout.write(json.dumps({"verdict": "ERROR",
                                     "errors": [f"{type(e).__name__}: {e}"],
                                     "warnings": [], "values": {}}, indent=1) + "\n")
        return 2
    sys.stdout.write(json.dumps(result, indent=1) + "\n")
    return 0 if result["verdict"] == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
