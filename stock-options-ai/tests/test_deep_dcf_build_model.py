"""The batch engine's workbook builder: validation, the Python mirror, the workbook.

The decisive test — LibreOffice recalculating the workbook and agreeing with
the Python mirror in all three cases — needs soffice and runs where it is
installed (the VPS); it is skipped elsewhere.
"""
import copy
import importlib.util
import json
import math
import shutil
import sys
from pathlib import Path

import pytest

from tests.deep_dcf_helpers import ACCEPT

TOOLS = Path(__file__).resolve().parent.parent / "deep_dcf" / "tools"
sys.path.insert(0, str(TOOLS))
import build_model as bm  # noqa: E402
import check_delivery as cd  # noqa: E402

FLAT = [0.0] * 10


def scen(growth=0.05, gm=0.6, opex=0.3, da=0.05, capex=0.05, tax=0.2, nwc=0.0, tg=0.025, adj=0.0):
    return {"revenue_growth": [growth] * 10, "gross_margin": [gm] * 10, "operating_expense_pct": [opex] * 10,
            "da_pct": [da] * 10, "capex_pct": [capex] * 10, "tax_rate": tax,
            "nwc_pct_of_revenue_change": nwc, "terminal_growth": tg, "wacc_adjustment": adj}


def answer(**over):
    a = {"scenarios": {"bear": scen(growth=0.03, opex=0.33, adj=0.0075, tg=0.02),
                       "base": scen(),
                       "bull": scen(growth=0.08, opex=0.27, adj=-0.0075, tg=0.03)},
         "pretax_cost_of_debt": 0.05, "thesis": "A steady compounder.",
         "key_assumptions": ["growth 5%", "EBIT margin 30%", "capex equals D&A"], "caveats": ["cyclicality"]}
    a.update(over)
    return a


INPUTS = {
    "symbol": "TST", "company": "Test Co", "as_of": "2026-11-16",
    "market": {"price": 50.0, "shares_outstanding": 100e6, "beta": 1.1, "price_source": "test"},
    "rates": {"risk_free_10y": 0.045, "equity_risk_premium": 0.055},
    "capital_structure": {"total_debt": 1.0e9, "total_cash": 0.4e9, "net_debt": 0.6e9},
    "ltm": {"revenue": {"value": 2.0e9, "through": "2026-09-30"}},
    "annual": {"fiscal_year_ends": ["2024-12-31", "2025-12-31"],
               "income_and_cash_flow": {"revenue": [1.8e9, 1.95e9], "operating_income": [0.5e9, 0.56e9],
                                        "gross_profit": [1.05e9, 1.16e9], "capex": [0.09e9, None]}},
}


def mk():
    return bm.market_inputs(INPUTS)


# ── inputs ──────────────────────────────────────────────────────────────────

def test_base_revenue_prefers_ltm_then_latest_fiscal_year():
    assert bm.base_revenue(INPUTS) == (2.0e9, "LTM to 2026-09-30")
    no_ltm = {**INPUTS, "ltm": None}
    assert bm.base_revenue(no_ltm) == (1.95e9, "FY ending 2025-12-31")
    assert bm.base_revenue({"annual": {}}) == (None, None)


def test_market_inputs_are_in_millions_and_refuse_missing_facts():
    m = mk()
    assert (m["shares_m"], m["debt_m"], m["cash_m"], m["revenue_m"]) == (100.0, 1000.0, 400.0, 2000.0)
    broken = copy.deepcopy(INPUTS)
    broken["capital_structure"]["total_debt"] = None
    with pytest.raises(bm.BuildError, match="debt_m"):
        bm.market_inputs(broken)


# ── validation ──────────────────────────────────────────────────────────────

def test_a_good_answer_validates_and_text_is_neutralised():
    a, errs = bm.validate(answer(thesis="=HYPERLINK(\"x\")\x07 steady", caveats=["@cmd", "+1", "fine"]))
    assert errs == []
    assert not a["thesis"].startswith("=") and "\x07" not in a["thesis"]
    assert a["caveats"] == ["cmd", "1", "fine"]


@pytest.mark.parametrize("mutate, needle", [
    (lambda a: a["scenarios"]["base"].update(revenue_growth=[0.05] * 9), "exactly 10"),
    (lambda a: a["scenarios"]["base"].update(gross_margin=[60.0] * 10), "outside"),
    (lambda a: a["scenarios"]["base"].update(capex_pct=["x"] * 10), "not a finite number"),
    (lambda a: a["scenarios"]["base"].update(terminal_growth=0.06), "terminal_growth"),
    (lambda a: a["scenarios"]["base"].update(wacc_adjustment=0.08), "wacc_adjustment"),
    (lambda a: a["scenarios"].pop("bull"), "scenarios.bull is missing"),
    (lambda a: a.update(pretax_cost_of_debt=None), "pretax_cost_of_debt"),
    (lambda a: a.update(key_assumptions=["one", ""]), "key_assumptions"),
    (lambda a: a.update(thesis="   "), "thesis"),
    (lambda a: a["scenarios"]["base"].update(gross_margin=[0.1] * 10, operating_expense_pct=[1.3] * 10),
     "EBIT margin"),
])
def test_each_broken_answer_is_rejected(mutate, needle):
    a = answer()
    mutate(a)
    _, errs = bm.validate(a)
    assert any(needle in e for e in errs), errs


def test_non_object_answers_are_rejected():
    assert bm.validate([1, 2])[1] and bm.validate({"scenarios": "x"})[1]


# ── the Python mirror ───────────────────────────────────────────────────────

def test_mirror_matches_a_closed_form_case():
    """No growth, no working capital: FCF is the same every year, so the value
    is an annuity plus a perpetuity — computable by hand."""
    s = scen(growth=0.0, gm=0.5, opex=0.25, da=0.05, capex=0.05, tax=0.2, tg=0.0)
    m = mk()
    v = bm.scenario_value(m, s, 0.05)
    w = bm.wacc_parts(m, 0.05, 0.2)["wacc_pre"]
    fcf = 2000.0 * 0.25 * 0.8                               # EBIT 25% of revenue, taxed at 20%
    pv = sum(fcf / (1 + w) ** (t + 0.5) for t in range(10))
    pv_tv = fcf / w / (1 + w) ** 9.5
    assert v["fair_value"] == pytest.approx((pv + pv_tv - 600.0) / 100.0, rel=1e-12)
    assert v["wacc"] == pytest.approx(w)


def test_losses_are_not_taxed():
    m = mk()
    s = scen(gm=0.2, opex=0.3, tg=0.0)                     # EBIT margin −10%
    # cost of debt 0, so the tax rate cannot move WACC through the debt shield
    v0 = bm.scenario_value(m, {**s, "tax_rate": 0.0}, 0.0)
    v1 = bm.scenario_value(m, {**s, "tax_rate": 0.4}, 0.0)
    assert v0["enterprise_value"] == pytest.approx(v1["enterprise_value"], rel=1e-6)


def test_cases_out_of_order_or_too_close_to_growth_are_rejected_before_any_build():
    a, _ = bm.validate(answer())
    a["scenarios"]["bear"], a["scenarios"]["bull"] = a["scenarios"]["bull"], a["scenarios"]["bear"]
    assert any("out of order" in e for e in bm.evaluate(mk(), a)[1])
    a, _ = bm.validate(answer())
    a["scenarios"]["base"]["wacc_adjustment"] = -0.03
    a["scenarios"]["base"]["terminal_growth"] = 0.045
    low_beta = {**mk(), "beta": 0.5}                       # CAPM 7.25% → WACC ~3.9% after the cut
    assert any("minus terminal growth" in e for e in bm.evaluate(low_beta, a)[1])


# ── the workbook ────────────────────────────────────────────────────────────

def _build(tmp_path, a=None):
    (tmp_path / "inputs.json").write_text(json.dumps(INPUTS))
    (tmp_path / "assumptions.json").write_text(json.dumps(a or {**answer(), "_meta": {"model": "claude-sonnet-5-5"}}))
    return bm.build(tmp_path, tmp_path / "inputs.json", tmp_path / "assumptions.json")


def test_build_writes_a_workbook_the_checker_can_read(tmp_path):
    openpyxl = pytest.importorskip("openpyxl")
    out = _build(tmp_path)
    assert out["ok"], out
    summary = json.loads((tmp_path / "summary.json").read_text())
    assert cd.summary_shape_errors(summary) == []                       # the check's own shape rules
    assert summary["engine"] == "batch" and summary["price"] == 50.0
    wb = openpyxl.load_workbook(tmp_path / "model.xlsx")
    assert wb.sheetnames == ["DCF", "SensCalc", "History", "Notes"]
    ws = wb["DCF"]
    assert ws["C4"].value == 2                                           # saved on Base
    formulas = sum(1 for sh in wb for row in sh.iter_rows() for c in row
                   if isinstance(c.value, str) and c.value.startswith("="))
    assert formulas >= ACCEPT["min_formulas"]
    cells = summary["cells"]
    for key in ("implied_price", "enterprise_value", "pv_terminal_value", "wacc", "terminal_growth"):
        sheet, coord = cells[key].split("!")
        assert str(wb[sheet][coord].value).startswith("="), key
    for ref in cells["sensitivity_centers"]:                            # centre + 4 formula neighbours
        sheet, coord = ref.split("!")
        assert "SUMPRODUCT" in wb[sheet][coord].value
        for n in cd.neighbours(ref):
            s2, c2 = n.split("!")
            assert str(wb[s2][c2].value).startswith("=")
    assert "steady compounder" in wb["Notes"]["B1"].value


def test_build_values_come_from_the_mirror(tmp_path):
    pytest.importorskip("openpyxl")
    out = _build(tmp_path)
    a, _ = bm.validate(answer())
    want = bm.evaluate(mk(), a)[0]
    summary = json.loads((tmp_path / "summary.json").read_text())
    for n in ("bear", "base", "bull"):
        assert summary["scenarios"][n]["fair_value"] == pytest.approx(want[n]["fair_value"])
        assert math.isfinite(out["values"][n]["tv_fraction"])


def test_rejected_answer_writes_nothing(tmp_path):
    a = answer()
    a["scenarios"]["base"]["revenue_growth"] = [0.05] * 3
    out = _build(tmp_path, a)
    assert not out["ok"] and not (tmp_path / "model.xlsx").exists() and not (tmp_path / "summary.json").exists()


def test_cli_answers_in_json(tmp_path, capsys):
    pytest.importorskip("openpyxl")
    (tmp_path / "inputs.json").write_text(json.dumps(INPUTS))
    (tmp_path / "assumptions.json").write_text("not json")
    assert bm.main(["--dir", str(tmp_path)]) == 2
    assert json.loads(capsys.readouterr().out)["crash"] is True
    (tmp_path / "assumptions.json").write_text(json.dumps(answer()))
    assert bm.main(["--dir", str(tmp_path)]) == 0
    assert json.loads(capsys.readouterr().out)["ok"] is True


def test_builder_uses_only_the_standard_library_and_openpyxl():
    src = (TOOLS / "build_model.py").read_text()
    for banned in ("loguru", "requests", "deep_dcf", "config.settings", "pandas", "numpy", "anthropic"):
        assert f"import {banned}" not in src and f"from {banned}" not in src, banned
    spec = importlib.util.find_spec("build_model")
    assert spec is not None


@pytest.mark.skipif(not shutil.which("soffice"), reason="LibreOffice not installed here")
def test_libreoffice_agrees_with_the_mirror_in_all_three_cases(tmp_path):
    """The real acceptance check, end to end, on a freshly built workbook. Needs
    openpyxl too: on the VPS that lives in the sandbox venv, not the runner's."""
    pytest.importorskip("openpyxl")
    assert _build(tmp_path)["ok"]
    verdict = cd.check(tmp_path, tmp_path / "inputs.json", ACCEPT, timeout=180)
    assert verdict["verdict"] == "PASS", verdict["errors"]
    assert verdict["values"]["total_formulas"] >= ACCEPT["min_formulas"]
