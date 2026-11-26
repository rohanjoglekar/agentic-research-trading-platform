"""The delivery check: PASS only when the workbook, recalculated three ways,
says what summary.json says and the economics are sane."""
import copy
import importlib.util
import json
import sys
from pathlib import Path

import pytest

from tests.deep_dcf_helpers import ACCEPT

TOOLS = Path(__file__).resolve().parent.parent / "deep_dcf" / "tools"
sys.path.insert(0, str(TOOLS))
import check_delivery as cd  # noqa: E402
import recalc  # noqa: E402

SUMMARY = {
    "symbol": "ACME", "price": 30.0, "scenario_selector_cell": "DCF!B6",
    "cells": {"implied_price": "DCF!C95", "wacc": "DCF!C60", "terminal_growth": "DCF!C61",
              "enterprise_value": "DCF!C90", "pv_terminal_value": "DCF!C89", "net_debt": "DCF!C91",
              "diluted_shares": "DCF!C93", "base_revenue": "DCF!D30",
              "sensitivity_centers": ["DCF!D110", "DCF!D120", "DCF!D130"]},
    "scenarios": {"bear": {"fair_value": 24.0, "wacc": 0.10, "terminal_growth": 0.02},
                  "base": {"fair_value": 36.0, "wacc": 0.092, "terminal_growth": 0.025},
                  "bull": {"fair_value": 48.0, "wacc": 0.088, "terminal_growth": 0.03}},
    "key_assumptions": ["revenue +8% fading to 3%", "EBIT margin 40% to 43%", "tax 21%"],
    "thesis": "Steady subscription business.", "caveats": ["debt load"],
}
INPUTS = {
    "market": {"price": 30.0, "shares_outstanding": 600e6},
    "capital_structure": {"net_debt": 7.5e9},
    "ltm": {"revenue": {"value": 5.25e9, "through": "2026-10-02"}},
    "annual": {"income_and_cash_flow": {"revenue": [3.3e9, 3.8e9, 3.9e9, 5.0e9]}},
    "ttm": {"revenue": 5.23e9},
}


CENTRES = ("DCF!D110", "DCF!D120", "DCF!D130")
GRID = "=(SUMPRODUCT($C$80:$J$80/(1+$A110)^$C$81:$J$81)+$J$80*(1+D$109)/($A110-D$109)-$C$91)/$C$93"


def _cells(price, wacc, tg, full=True):
    # the bridge holds by construction: EV = price x shares + net debt
    c = {"DCF!C95": {"value": price, "formula": "=(C90-C91)/C93"},
         "DCF!C60": {"value": wacc, "formula": "=INDEX(B50:D50,1,$B$6)"},
         "DCF!C61": {"value": tg, "formula": "=INDEX(B51:D51,1,$B$6)"},
         "DCF!C90": {"value": price * 600.0 + 7500.0, "formula": "=C88+C89"},
         "DCF!C91": {"value": 7500.0, "formula": None},
         "DCF!C93": {"value": 600.0, "formula": None}}
    if full:
        c.update({
            "DCF!B6": {"value": 2, "formula": None},
            "DCF!C89": {"value": 21000.0, "formula": "=C87/(1+C60)^4.5"},
            "DCF!D30": {"value": 5250.0, "formula": None}})
        for centre in CENTRES:
            c[centre] = {"value": price, "formula": GRID}
            for i, n in enumerate(cd.neighbours(centre)):
                c[n] = {"value": price + (i + 1) * 1.5, "formula": GRID}
    return c


def _res(price, wacc, tg, full=True, **over):
    r = {"status": "success", "total_formulas": 420, "total_errors": 0, "error_summary": {},
         "cells": _cells(price, wacc, tg, full), "recalculated_file": "/x/model.recalc.xlsx"}
    r.update(over)
    return r


def _good():
    return dict(summary=copy.deepcopy(SUMMARY), inputs=copy.deepcopy(INPUTS), accept=dict(ACCEPT),
                base=_res(36.0, 0.092, 0.025), bear=_res(24.0, 0.10, 0.02, full=False),
                bull=_res(48.0, 0.088, 0.03, full=False), validator={"status": "PASS", "warnings": []})


def _errs(**change):
    kw = _good()
    for k, fn in change.items():
        fn(kw[k])
    return cd.evaluate(**kw)


def test_a_consistent_delivery_passes_and_reports_recalculated_values():
    out = cd.evaluate(**_good())
    assert out["verdict"] == "PASS", out["errors"]
    assert out["values"]["scenarios"]["base"] == {"fair_value": 36.0, "wacc": 0.092, "terminal_growth": 0.025}
    assert out["values"]["upside_pct"] == {"bear": -20.0, "base": 20.0, "bull": 60.0}
    assert out["values"]["tv_fraction"] == round(21000 / 29100, 4)
    assert out["values"]["bridge_unit_factor"] == 1.0
    assert out["warnings"] == []


def _set(path, value):
    def fn(obj):
        *head, last = path
        for k in head:
            obj = obj[k]
        obj[last] = value
    return fn


@pytest.mark.parametrize("change, needle", [
    (dict(base=lambda r: r.update(status="errors_found", total_errors=2,
                                  error_summary={"#REF!": {"count": 2, "locations": ["DCF!C5"]}})),
     "formula errors as delivered"),
    (dict(bull=lambda r: r.update(status="failed", message="LibreOffice timed out")), "with Bull selected failed"),
    (dict(base=_set(["total_formulas"], 40)), "only 40 formula cells"),
    (dict(base=_set(["cells", "DCF!B6", "value"], 3)), "must be saved as 2 (Base)"),
    (dict(base=_set(["cells", "DCF!B6", "formula"], "=1+1")), "not a formula"),
    (dict(summary=_set(["scenarios", "base", "fair_value"], 40.0)), "summary.scenarios.base.fair_value = 40"),
    (dict(summary=_set(["scenarios", "bull", "fair_value"], 55.0)), "selector on 3"),
    (dict(summary=_set(["scenarios", "bear", "wacc"], 0.12)), "summary.scenarios.bear.wacc"),
    (dict(base=_set(["cells", "DCF!D120", "value"], 31.0)), "sensitivity centre DCF!D120"),
    (dict(base=_set(["cells", "DCF!D110", "formula"], None)), "must be a formula"),
    (dict(base=_set(["cells", "DCF!C95", "formula"], None)), "cells.implied_price"),
    (dict(base=_set(["cells", "DCF!C95", "value"], "#N/A")), "is not a number"),
    (dict(base=_set(["cells", "DCF!C89", "value"], 27000.0)), "terminal value is 93%"),
    (dict(base=_set(["cells", "DCF!C89", "value"], 900.0)), "is not the present value of the terminal value"),
    (dict(base=_set(["cells", "DCF!C90", "value"], -5.0)), "valuation bridge does not hold with Base"),
    (dict(base=_set(["cells", "DCF!C95", "value"], 72.0), summary=_set(["scenarios", "base", "fair_value"], 72.0)),
     "valuation bridge does not hold with Base"),
    (dict(bull=_set(["cells", "DCF!C90", "value"], 29100.0)), "valuation bridge does not hold with Bull"),
    (dict(base=_set(["cells", "DCF!C91", "value"], 7.5)), "valuation bridge does not hold"),
    (dict(base=_set(["cells", "DCF!D110", "formula"], "=C95")), "is just a reference"),
    (dict(base=_set(["cells", "DCF!D120", "formula"], "=DCF!$C$95")), "is just a reference"),
    (dict(summary=_set(["cells", "sensitivity_centers"], ["DCF!D110", "DCF!D110", "DCF!D130"])),
     "point at the same cell"),
    (dict(summary=_set(["cells", "enterprise_value"], "DCF!C95")), "point at the same cell"),
    (dict(summary=_set(["price"], float("inf"))), "summary.price must be a finite number"),
    (dict(summary=_set(["scenarios", "base", "fair_value"], float("nan"))), "must be a finite number"),
    (dict(summary=_set(["price"], 33.0)), "does not match inputs.json price"),
    (dict(inputs=_set(["market", "shares_outstanding"], 1.2e9)), "diluted shares"),
    (dict(inputs=_set(["ltm"], None), base=_set(["cells", "DCF!D30", "value"], 9000.0)), "cells.base_revenue"),
    (dict(summary=_set(["key_assumptions"], ["one"])), "key_assumptions"),
    (dict(summary=_set(["cells", "sensitivity_centers"], ["DCF!D110"])), "sensitivity_centers"),
    (dict(summary=_set(["scenario_selector_cell"], "B6")), "scenario_selector_cell"),
    (dict(summary=_set(["cells", "wacc"], "the WACC cell")), "summary.cells.wacc"),
])
def test_each_defect_is_a_rejection(change, needle):
    out = _errs(**change)
    assert out["verdict"] == "FAIL"
    assert any(needle in e for e in out["errors"]), out["errors"]


def _reprice(kw, name, price):
    """Move one scenario's value consistently (summary + implied price + EV)."""
    kw["summary"]["scenarios"][name]["fair_value"] = price
    kw[name]["cells"]["DCF!C95"]["value"] = price
    kw[name]["cells"]["DCF!C90"]["value"] = price * 600.0 + 7500.0


def test_scenario_order_wacc_range_and_spread_are_enforced():
    kw = _good(); _reprice(kw, "bear", 50.0)
    assert any("out of order" in e for e in cd.evaluate(**kw)["errors"])

    kw = _good()
    kw["summary"]["scenarios"]["base"]["wacc"] = 0.20
    kw["base"]["cells"]["DCF!C60"]["value"] = 0.20
    assert any("base WACC 20.00% is outside" in e for e in cd.evaluate(**kw)["errors"])

    kw = _good()
    kw["summary"]["scenarios"]["bull"].update(wacc=0.06, terminal_growth=0.045)
    kw["bull"]["cells"]["DCF!C60"]["value"] = 0.06
    kw["bull"]["cells"]["DCF!C61"]["value"] = 0.045
    assert any("terminal value is unstable" in e for e in cd.evaluate(**kw)["errors"])


def test_a_spread_of_exactly_two_points_and_limits_hit_exactly_pass():
    # 0.06 - 0.04 is 0.0199999… in floating point
    kw = _good()
    kw["summary"]["scenarios"]["bull"].update(wacc=0.06, terminal_growth=0.04)
    kw["bull"]["cells"]["DCF!C60"]["value"] = 0.06
    kw["bull"]["cells"]["DCF!C61"]["value"] = 0.04
    kw["summary"]["scenarios"]["bear"]["wacc"] = 0.16
    kw["bear"]["cells"]["DCF!C60"]["value"] = 0.16
    assert cd.evaluate(**kw)["verdict"] == "PASS"


def test_hardcoded_scenario_prices_fail_the_bridge():
    """`=CHOOSE($B$6,24,36,48)` as the implied price passed,
    because only the Base case was tied to anything."""
    kw = _good()
    for name in ("bear", "bull"):                    # EV does not move with the selector
        kw[name]["cells"]["DCF!C90"]["value"] = 36.0 * 600.0 + 7500.0
    out = cd.evaluate(**kw)
    assert out["verdict"] == "FAIL"
    assert sum("valuation bridge does not hold" in e for e in out["errors"]) == 2


def test_a_centre_outside_a_working_grid_is_rejected():
    """All three centres pointing at one cell, or at `=C95+0`."""
    kw = _good()
    for n in cd.neighbours("DCF!D110"):
        kw["base"]["cells"][n] = {"value": 36.0, "formula": GRID}       # a flat table
    out = cd.evaluate(**kw)
    assert out["verdict"] == "FAIL" and any("not inside a working grid" in e for e in out["errors"])
    kw = _good()
    for n in cd.neighbours("DCF!D120"):
        kw["base"]["cells"][n] = {"value": 40.0, "formula": None}       # pasted numbers
    assert any("not inside a working grid" in e for e in cd.evaluate(**kw)["errors"])
    kw = _good()
    for n in cd.neighbours("DCF!D130"):
        del kw["base"]["cells"][n]                                       # nothing around it
    assert any("not inside a working grid" in e for e in cd.evaluate(**kw)["errors"])


def test_bridge_accepts_any_consistent_unit_pair_and_net_cash():
    kw = _good()                                        # money in $, shares in raw units
    for name in ("bear", "base", "bull"):
        c = kw[name]["cells"]
        price = c["DCF!C95"]["value"]
        c["DCF!C93"]["value"] = 600e6
        c["DCF!C91"]["value"] = 7500e6
        c["DCF!C90"]["value"] = price * 600e6 + 7500e6
    kw["base"]["cells"]["DCF!C89"]["value"] = 21000e6
    kw["base"]["cells"]["DCF!D30"]["value"] = 5250e6
    assert cd.evaluate(**kw)["verdict"] == "PASS"
    kw = _good()                                        # net cash: negative net debt adds to equity
    kw["inputs"]["capital_structure"]["net_debt"] = -5e9
    for name in ("bear", "base", "bull"):
        c = kw[name]["cells"]
        c["DCF!C91"]["value"] = -5000.0
        c["DCF!C90"]["value"] = c["DCF!C95"]["value"] * 600.0 - 5000.0      # EV = equity − net cash
    kw["base"]["cells"]["DCF!C89"]["value"] = 0.6 * kw["base"]["cells"]["DCF!C90"]["value"]
    out = cd.evaluate(**kw)
    assert out["verdict"] == "PASS", out["errors"]
    assert out["values"]["net_debt"] == -5000.0


def test_net_debt_in_the_wrong_unit_is_an_error_not_a_warning():
    # revenue in $M but net debt in $B, with EV adjusted so the bridge still holds
    kw = _good()
    for name in ("bear", "base", "bull"):
        c = kw[name]["cells"]
        c["DCF!C91"]["value"] = 7.5
        c["DCF!C90"]["value"] = c["DCF!C95"]["value"] * 600.0 + 7.5
    kw["base"]["cells"]["DCF!C89"]["value"] = 15000.0
    out = cd.evaluate(**kw)
    assert out["verdict"] == "FAIL" and any("different units from revenue" in e for e in out["errors"])


def test_fair_value_far_from_price_is_an_artifact_not_a_target():
    kw = _good()
    for name, v in (("bear", 200.0), ("base", 300.0), ("bull", 400.0)):
        _reprice(kw, name, v)
    for ref in CENTRES:
        kw["base"]["cells"][ref]["value"] = 300.0
    kw["base"]["cells"]["DCF!C89"]["value"] = 0.7 * kw["base"]["cells"]["DCF!C90"]["value"]
    out = cd.evaluate(**kw)
    assert any("modelling artifact" in e for e in out["errors"])


def test_terminal_value_between_warn_and_max_is_a_warning_only():
    kw = _good()
    kw["base"]["cells"]["DCF!C89"]["value"] = 24500.0                    # 84% of EV
    out = cd.evaluate(**kw)
    assert out["verdict"] == "PASS" and any("84%" in w for w in out["warnings"])


def test_units_may_be_dollars_thousands_or_millions():
    for factor in (1.0, 1e-3, 1e-6):                 # shares in units, thousands, millions
        kw = _good()
        for name in ("bear", "base", "bull"):
            kw[name]["cells"]["DCF!C93"]["value"] = 600e6 * factor
        assert cd.evaluate(**kw)["verdict"] == "PASS", factor


def test_share_count_may_match_the_sec_diluted_average_but_not_a_pre_split_one():
    kw = _good()
    def with_shares(model_shares, listed, diluted):
        kw = _good()
        kw["inputs"]["market"]["shares_outstanding"] = listed
        kw["inputs"]["annual"]["shares"] = {"diluted_shares": diluted}
        for name in ("bear", "base", "bull"):                         # keep the bridge consistent
            c = kw[name]["cells"]
            c["DCF!C93"]["value"] = model_shares
            c["DCF!C90"]["value"] = c["DCF!C95"]["value"] * model_shares + 7500.0
        kw["base"]["cells"]["DCF!C89"]["value"] = 0.7 * kw["base"]["cells"]["DCF!C90"]["value"]
        return cd.evaluate(**kw)

    assert with_shares(12230.0, 12.416e9, [12.4e9, 12.23e9])["verdict"] == "PASS"   # SEC diluted
    out = with_shares(5888.0, 12.416e9, [12.4e9, 12.23e9])                          # one share class
    assert out["verdict"] == "FAIL" and any("diluted shares" in e for e in out["errors"])
    # a pre-split SEC average is not a valid reference for the post-split price
    assert any("diluted shares" in e for e in with_shares(150.0, 600e6, [150e6])["errors"])
    assert with_shares(600.0, 600e6, [150e6])["verdict"] == "PASS"
    # tolerances no longer stack: 0.80x the current count is out (was accepted up to 0.64x)
    assert any("diluted shares" in e for e in with_shares(480.0, 600e6, [560e6])["errors"])


def test_net_debt_mismatch_and_missing_validator_are_warnings():
    kw = _good()
    for name in ("bear", "base", "bull"):               # the model added leases to net debt
        c = kw[name]["cells"]
        c["DCF!C91"]["value"] = 12000.0
        c["DCF!C90"]["value"] = c["DCF!C95"]["value"] * 600.0 + 12000.0
    kw["validator"] = None
    out = cd.evaluate(**kw)
    assert out["verdict"] == "PASS", out["errors"]
    assert any("net debt" in w for w in out["warnings"]) and "validator" not in out["values"]


def test_validator_heuristic_warnings_are_recorded_but_are_not_findings():
    # seen on the first real model: "Terminal value is 0.0% of EV" from the
    # skill's label-scanning validator, on a workbook where it is 57%
    kw = _good()
    kw["validator"] = {"status": "PASS", "warnings": ["Terminal value is 0.0% of EV (typically 50-70%)",
                                                      "Recommended sheet missing: Sensitivity"]}
    out = cd.evaluate(**kw)
    assert out["verdict"] == "PASS" and out["warnings"] == []
    assert len(out["values"]["validator"]["notes"]) == 2
    # …and its "errors" come from the same label scan: a correct workbook with the
    # label "Terminal value share of EV (growth method)" = 0.62 was called invalid
    kw["validator"] = {"status": "FAIL", "errors": ["CRITICAL: Terminal growth (62.00%) >= WACC (9.20%)"]}
    out = cd.evaluate(**kw)
    assert out["verdict"] == "PASS" and out["values"]["validator"]["status"] == "FAIL"


def test_malformed_summary_fails_before_anything_else():
    out = cd.evaluate({"symbol": "ACME"}, INPUTS, ACCEPT, {}, {}, {}, None)
    assert out["verdict"] == "FAIL" and len(out["errors"]) >= 5


# ── recalc helpers ──────────────────────────────────────────────────────────

def test_cell_and_override_parsing():
    assert recalc.parse_cell("DCF!$C$95") == ("DCF", "C95")
    assert recalc.parse_cell("'Sens Table'!b7") == ("Sens Table", "B7")
    for bad in ("C95", "DCF!95", "DCF!C", "", "DCF!C95:D96"):
        with pytest.raises(ValueError):
            recalc.parse_cell(bad)
    assert recalc.parse_set("DCF!B6=1") == ("DCF", "B6", 1)
    assert recalc.parse_set("DCF!B6=0.095") == ("DCF", "B6", 0.095)
    assert recalc.parse_set("DCF!A1=Base") == ("DCF", "A1", "Base")
    with pytest.raises(ValueError):
        recalc.parse_set("DCF!B6")


def test_scan_counts_formulas_and_finds_error_cells(tmp_path):
    openpyxl = pytest.importorskip("openpyxl")
    f = openpyxl.Workbook(); wf = f.active; wf.title = "DCF"
    wf["A1"], wf["A2"], wf["A3"], wf["B1"] = 10, "=A1*2", "=A1/0", "label"
    f.save(tmp_path / "formulas.xlsx")
    v = openpyxl.Workbook(); wv = v.active; wv.title = "DCF"         # what LibreOffice would write
    wv["A1"], wv["A2"], wv["A3"], wv["B1"] = 10, 20, "#DIV/0!", "label"
    v.save(tmp_path / "values.xlsx")
    out = recalc.scan(tmp_path / "values.xlsx", tmp_path / "formulas.xlsx",
                      [("DCF", "A2"), ("DCF", "A1"), ("Nope", "A1")])
    assert out["status"] == "errors_found" and out["total_formulas"] == 2
    assert out["error_summary"] == {"#DIV/0!": {"count": 1, "locations": ["DCF!A3"]}}
    assert out["cells"]["DCF!A2"] == {"value": 20, "formula": "=A1*2"}
    assert out["cells"]["DCF!A1"] == {"value": 10, "formula": None}
    assert "error" in out["cells"]["Nope!A1"]


def test_recalc_cli_answers_in_json_even_when_it_cannot_run(tmp_path, capsys):
    assert recalc.main([str(tmp_path / "missing.xlsx")]) == 2
    assert json.loads(capsys.readouterr().out)["status"] == "failed"


def test_check_cli_reports_missing_deliverables_as_json(tmp_path, capsys):
    (tmp_path / "inputs.json").write_text(json.dumps(INPUTS))
    assert cd.main(["--dir", str(tmp_path)]) == 2                       # no summary.json
    assert json.loads(capsys.readouterr().out)["verdict"] == "ERROR"
    (tmp_path / "summary.json").write_text(json.dumps(SUMMARY))
    assert cd.main(["--dir", str(tmp_path)]) == 1                       # no model.xlsx
    out = json.loads(capsys.readouterr().out)
    assert out["verdict"] == "FAIL" and out["errors"] == ["model.xlsx is missing"]


def test_tools_use_only_the_standard_library_and_openpyxl():
    # they run in the sandbox venv, which has openpyxl and nothing else
    for name in ("recalc.py", "check_delivery.py"):
        src = (TOOLS / name).read_text()
        for banned in ("loguru", "requests", "deep_dcf", "config.settings", "pandas", "numpy"):
            assert f"import {banned}" not in src and f"from {banned}" not in src, (name, banned)
    assert importlib.util.find_spec("recalc") is not None
