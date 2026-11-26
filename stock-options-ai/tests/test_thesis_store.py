"""Unit tests for trading/thesis_store.py — runs without network or API keys.

Run: PYTHONPATH=. .venv/bin/python tests/test_thesis_store.py
"""
import tempfile
from pathlib import Path

import trading.thesis_store as ts

# Redirect storage to a throwaway temp dir so we never touch real state.
ts.THESIS_DIR = Path(tempfile.mkdtemp())

_passed = 0


def ok(name, cond):
    global _passed
    print(("PASS " if cond else "FAIL ") + name)
    assert cond, name
    _passed += 1


def test_split_scan_analysis():
    risk, bull = ts.split_scan_analysis(
        "**Risk assessment** Margins under pressure from fuel costs. Regulatory risk in CA. "
        "**Why it is a wheel candidate** Dominant rideshare share. Improving free cash flow. "
        "WOULD_OWN_SCORE: 7")
    ok("split: bull has rideshare", "rideshare" in bull.lower())
    ok("split: risk has fuel", "fuel" in risk.lower())
    ok("split: would_own stripped from bull", "would_own" not in bull.lower())


def test_upsert_creates_and_scores():
    t = ts.upsert_from_scan(
        "ACME", would_own=8,
        analysis_text="**Risk assessment** Some risk here today. "
                      "**Why it is a wheel candidate** Strong moat indeed. WOULD_OWN_SCORE: 8",
        price=70.0, score=82.0, sector="Industrials")
    ok("upsert: conviction 8", t["conviction"] == 8)
    ok("upsert: status intact", t["status"] == "intact")
    ok("upsert: pillars populated", len(t["pillars"]) >= 1)
    ok("upsert: sector stored", t.get("sector") == "Industrials")


def test_trend_and_status_drift():
    for c in (7, 6, 5, 4):
        ts.upsert_from_scan("ACME", would_own=c,
                            analysis_text="**Why it is a wheel candidate** still ok.")
    summ = ts.thesis_summary("ACME")
    ok("trend: falling", summ["trend"] == "falling")
    ok("status: weakening at conv 4", summ["status"] == "weakening")
    ok("summary: prev != now", summ["conviction_prev"] != summ["conviction"])
    ts.upsert_from_scan("ACME", would_own=2,
                        analysis_text="**Why it is a wheel candidate** thesis cracking.")
    ok("status: broken at conv 2", ts.thesis_summary("ACME")["status"] == "broken")


def test_record_agent_observation():
    ts.record_agent_observation("ACME", event="Q2 miss, guidance cut", impact="break",
                                conviction=2, status="broken", action="CLOSE")
    t2 = ts.load_thesis("ACME")
    ok("agent log appended", t2["log"][-1]["event"].startswith("Q2 miss"))
    ok("agent log action", t2["log"][-1]["action"] == "CLOSE")
    ok("summary recent_events present", len(ts.thesis_summary("ACME")["recent_events"]) >= 1)


def test_format_for_prompt():
    block = ts.format_for_prompt(ts.thesis_summary("ACME"))
    ok("prompt block mentions BROKEN", "BROKEN" in block)
    ok("prompt block mentions tracked thesis", "TRACKED THESIS" in block)


def test_graceful_missing():
    ok("missing thesis -> None", ts.thesis_summary("ZZZZ") is None)
    ok("load missing -> None", ts.load_thesis("ZZZZ") is None)
    ok("format None -> empty", ts.format_for_prompt(None) == "")


def test_agent_creates_when_absent():
    ts.record_agent_observation("BBB", event="strong AI demand", impact="strengthen",
                                conviction=9, action="HOLD")
    ok("agent-created thesis exists", ts.load_thesis("BBB") is not None)


if __name__ == "__main__":
    for fn in [test_split_scan_analysis, test_upsert_creates_and_scores,
               test_trend_and_status_drift, test_record_agent_observation,
               test_format_for_prompt, test_graceful_missing,
               test_agent_creates_when_absent]:
        fn()
    print(f"\n{_passed} checks passed")
