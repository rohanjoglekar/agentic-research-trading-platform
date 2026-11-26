"""The weekly AI read, calibrated into the score.

The factor is mean-neutral and the same yardstick for every name: a read
counts as its percentile among this week's reads of the screened universe, and
it is switched on for everyone or for no one.
"""
import datetime as dt
import json

import pytest

from adapters import rh_ai_reads, rh_factors

NOW = dt.datetime(2026, 11, 23, 12, tzinfo=dt.timezone.utc)
FRESH = (NOW - dt.timedelta(days=1)).isoformat()
OLD = (NOW - dt.timedelta(days=30)).isoformat()


def read(raw, *, at=FRESH, usable=True, kind="company", conviction="medium", **kw):
    return {"raw": raw, "read_at": at, "usable": usable, "kind": kind, "conviction": conviction,
            "sub": {"business_quality": 7}, "thesis": "t", "key_risk": "r", **kw}


def store(**reads):
    return {"reads": reads}


# ─── calibration ─────────────────────────────────────────────────────────────

def test_percentiles_average_exactly_fifty_even_with_ties():
    s = store(A=read(40), B=read(60), C=read(60), D=read(80), E=read(20))
    cal = rh_ai_reads.calibrate(s, "ABCDE", NOW)
    pcts = [cal["reads"][k]["pct"] for k in "ABCDE"]
    assert sum(pcts) / 5 == pytest.approx(50.0)
    assert cal["reads"]["B"]["pct"] == cal["reads"]["C"]["pct"] == 60.0
    assert cal["reads"]["D"]["pct"] == 90.0 and cal["reads"]["E"]["pct"] == 10.0


def test_the_factor_is_off_for_everyone_until_ninety_percent_are_read():
    s = store(**{f"S{i}": read(50 + i) for i in range(8)})
    uni = [f"S{i}" for i in range(10)]
    cal = rh_ai_reads.calibrate(s, uni, NOW)
    assert cal["coverage_ok"] is False and cal["pool"] == 8
    f = rh_ai_reads.factor(cal["reads"]["S7"], cal["coverage_ok"], cal["pool"], cal["universe"])
    assert f["score"] is None and f["coverage"] == 0 and "8 of 10" in f["components"][0]["detail"]
    s["reads"]["S8"] = read(10)
    s["reads"]["S9"] = read(90)
    assert rh_ai_reads.calibrate(s, uni, NOW)["coverage_ok"] is True


def test_stale_unrecognised_and_fund_reads_are_not_ranked():
    s = store(A=read(50), B=read(70), OLD=read(99, at=OLD), GUESS=read(99, usable=False),
              FUND=read(80, kind="fund", usable=False))
    cal = rh_ai_reads.calibrate(s, ["A", "B", "OLD", "GUESS"], NOW)
    assert cal["pool"] == 2
    for sym in ("OLD", "GUESS", "FUND"):
        assert cal["reads"][sym]["pct"] is None
    assert cal["reads"]["FUND"]["kind"] == "fund"     # kept for display


def test_a_held_company_outside_the_universe_is_ranked_without_joining_the_pool():
    s = store(A=read(20), B=read(40), C=read(60), D=read(80), OUTSIDE=read(100))
    cal = rh_ai_reads.calibrate(s, "ABCD", NOW)
    assert cal["pool"] == 4 and cal["reads"]["OUTSIDE"]["pct"] == 100.0
    assert sum(cal["reads"][k]["pct"] for k in "ABCD") / 4 == pytest.approx(50.0)


def test_low_conviction_counts_half_and_the_score_is_the_percentile():
    cal = rh_ai_reads.calibrate(store(A=read(20), B=read(80, conviction="low")), "AB", NOW)
    f = rh_ai_reads.factor(cal["reads"]["B"], True)
    assert f["score"] == 75 and f["coverage"] == 0.5


def test_the_ai_factor_moves_a_composite_by_a_few_points_at_most():
    base = {"value": {"score": 60, "coverage": 1}, "quality": {"score": 60, "coverage": 1},
            "growth": {"score": 60, "coverage": 1}, "momentum": {"score": 60, "coverage": 1},
            "analyst": {"score": 60, "coverage": 1}}
    lo, _ = rh_factors.composite({**base, "ai": {"score": 0, "coverage": 1}})
    hi, _ = rh_factors.composite({**base, "ai": {"score": 100, "coverage": 1}})
    none, _ = rh_factors.composite(base)
    assert hi - lo <= 5 and lo <= none <= hi


def test_describe_and_band():
    assert rh_ai_reads.describe({"pct": 88}) == "AI read top 12%"
    assert rh_ai_reads.describe({"pct": 9}) == "AI read bottom 9%"
    assert rh_ai_reads.describe({"pct": 55}) == "AI read 55th percentile"
    assert rh_ai_reads.band(None) is None


def test_a_missing_or_torn_store_means_no_factor(tmp_path, monkeypatch):
    monkeypatch.setattr(rh_ai_reads.config, "RH_AI_READS_PATH", tmp_path / "ai_reads.json")
    assert rh_ai_reads.for_universe(["A"])["pool"] == 0
    (tmp_path / "ai_reads.json").write_text("{torn")
    assert rh_ai_reads.for_universe(["A"])["coverage_ok"] is False


def test_every_path_measures_against_the_names_the_screen_carried(tmp_path, monkeypatch):
    research = tmp_path / "research.json"
    research.write_text(json.dumps({"stocks": [{"symbol": "A"}, {"symbol": "B"}]}))
    reads = tmp_path / "ai_reads.json"
    reads.write_text(json.dumps(store(A=read(20, at=dt.datetime.now(dt.timezone.utc).isoformat()),
                                      B=read(80, at=dt.datetime.now(dt.timezone.utc).isoformat()))))
    monkeypatch.setattr(rh_ai_reads.config, "RH_RESEARCH_CACHE_PATH", research)
    monkeypatch.setattr(rh_ai_reads.config, "RH_AI_READS_PATH", reads)
    cal = rh_ai_reads.for_screen()
    assert rh_ai_reads.screen_symbols() == ["A", "B"]
    assert cal["coverage_ok"] is True and cal["universe"] == 2 and cal["reads"]["B"]["pct"] == 75.0


def test_the_public_read_is_only_for_companies_the_screen_carries(tmp_path, monkeypatch):
    """The weekly job also reads companies the account holds outside the screen.
    A read shown publicly for such a name would say the owner holds it."""
    now = dt.datetime.now(dt.timezone.utc).isoformat()
    research = tmp_path / "research.json"
    research.write_text(json.dumps({"stocks": [{"symbol": "A"}, {"symbol": "B"}]}))
    reads = tmp_path / "ai_reads.json"
    reads.write_text(json.dumps(store(A=read(20, at=now), B=read(80, at=now), OUTSIDE=read(99, at=now))))
    monkeypatch.setattr(rh_ai_reads.config, "RH_RESEARCH_CACHE_PATH", research)
    monkeypatch.setattr(rh_ai_reads.config, "RH_AI_READS_PATH", reads)
    pub = rh_ai_reads.public_read("b")
    assert pub["ai_pct"] == 75.0 and pub["ai_thesis"] == "t" and pub["ai_risk"] == "r"
    assert all(k.startswith("ai_") for k in pub)
    assert rh_ai_reads.for_screen()["reads"]["OUTSIDE"]["pct"] is not None      # the read exists
    assert rh_ai_reads.public_read("OUTSIDE") is None                           # and is not shown
    assert rh_ai_reads.public_read("NOPE") is None


def test_a_middle_percentile_is_written_with_the_right_ordinal():
    from adapters import rh_ai_reads
    assert [rh_ai_reads._ordinal(n) for n in (21, 22, 23, 24, 11, 12, 13, 41, 52, 63, 70)] == [
        "21st", "22nd", "23rd", "24th", "11th", "12th", "13th", "41st", "52nd", "63rd", "70th"]
    assert rh_ai_reads.describe({"pct": 52.4}) == "AI read 52nd percentile"
