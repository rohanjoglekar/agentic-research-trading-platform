"""Shared fixtures for the deep_dcf tests (not a test module)."""
from __future__ import annotations

from pathlib import Path

from deep_dcf.config import Settings

ACCEPT = {
    "value_tol": 0.005, "wacc_min": 0.05, "wacc_max": 0.16, "tg_min": 0.0, "tg_max": 0.045,
    "min_wacc_tg_spread": 0.02, "tv_fraction_warn": 0.80, "tv_fraction_max": 0.90,
    "tv_fraction_min": 0.20,
    "fair_to_price_min": 0.2, "fair_to_price_max": 5.0, "min_formulas": 150,
    "price_tol": 0.01, "shares_tol": 0.10, "revenue_base_tol": 0.10,
}


def make_settings(tmp: Path, **over) -> Settings:
    base = dict(
        research_path=tmp / "research.json", fundamentals_path=tmp / "fundamentals.json",
        alpha_state_path=tmp / "alpha_state.json", alpha_store_path=tmp / "alpha_positions.json",
        verdicts_url="http://127.0.0.1:9/none", data_dir=tmp / "deep_dcf",
        sandbox_user="nobody", sandbox_home=tmp / "home", runs_dir=tmp / "runs",
        opt_dir=tmp / "opt", claude_bin=str(tmp / "fake_claude"), accept=dict(ACCEPT),
        # The suites written for the original engine test the agentic path; the
        # batch path has its own tests (test_deep_dcf_batch*.py) and asks for it.
        engine="agentic",
    )
    base.update(over)
    return Settings(**base)


def research_row(sym, *, tier="buy", conf="medium", held=False, archetype="STABLE_COMPOUNDER",
                 available=True, dcf_conf="HIGH", upside=20.0, last=100.0, score=65, pl_pct=None):
    return {"symbol": sym, "name": sym + " Inc", "sector": "Technology", "tier": tier,
            "confidence_label": conf, "held": held, "last": last, "score": score, "pl_pct": pl_pct,
            "dcf": {"available": available, "archetype": archetype, "confidence": dcf_conf,
                    "upside_pct": upside if available else None,
                    "reason": None if available else "owner free cash flow ≤ 0"}}


def filing(date="2026-10-29", form="10-Q", cik=1234):
    return {"form": form, "filing_date": date, "accession": "0000-26-1", "period_end": "2026-09-30",
            "cik": cik}
