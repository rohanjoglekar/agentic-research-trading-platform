"""Weekly AI reads: the read itself, who gets read, and the money path.

Every test drives a fake client. Nothing here can reach the real API.
"""
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

import jobs.ai_universe_read as job
from ai_reads import reader, run as airun
from deep_dcf import budget

UTC = timezone.utc
NOW = datetime(2026, 11, 22, 20, 0, tzinfo=UTC)
CFG = {"enabled": True, "model": "claude-haiku-5-5", "effort": "medium", "max_tokens": 4000,
       "workers": 2, "refresh_days": 6, "run_cap_usd": 1.5, "monthly_ceiling_usd": 4.0,
       "cycle_day": 15, "input_tokens_worst_case": 3000, "max_consecutive_errors": 3,
       "rates_per_mtok": {"input": 0.10, "output": 0.50, "cache_read": 0.01, "cache_write": 0.125}}


def answer(**over):
    a = {"recognized": True, "kind": "company", "business_quality": 7, "growth_durability": 6,
         "financial_resilience": 8, "risk_outlook": 5, "valuation_vs_quality": 4,
         "conviction": "medium", "thesis": "Durable franchise.", "key_risk": "Regulation."}
    a.update(over)
    return a


def response(text, *, stop="end_turn", inp=1000, out=800, thinking=True):
    blocks = ([SimpleNamespace(type="thinking", thinking="…")] if thinking else [])
    if text is not None:
        blocks.append(SimpleNamespace(type="text", text=text))
    return SimpleNamespace(content=blocks, stop_reason=stop,
                           usage=SimpleNamespace(input_tokens=inp, output_tokens=out,
                                                 cache_read_input_tokens=0,
                                                 cache_creation_input_tokens=0))


class FakeClient:
    """`script` maps a ticker to a response or an exception; default is a good read."""

    def __init__(self, script=None):
        self.script = script or {}
        self.calls = []
        self.messages = self

    def create(self, **kw):
        self.calls.append(kw)
        sym = kw["messages"][0]["content"].split("(ticker ")[1].split(")")[0]
        r = self.script.get(sym)
        if isinstance(r, BaseException):
            raise r
        return r if r is not None else response(json.dumps(answer()))


# ─── the read ────────────────────────────────────────────────────────────────

def test_parse_read_scores_and_raw():
    r = reader.parse_read(json.dumps(answer()))
    assert r["sub"]["business_quality"] == 7 and r["raw"] == 60.0
    assert r["usable"] is True and r["kind"] == "company"


@pytest.mark.parametrize("bad", [
    "not json", "[]", json.dumps(answer(business_quality=11)),
    json.dumps(answer(growth_durability=True)), json.dumps(answer(risk_outlook=5.5)),
    json.dumps(answer(conviction="certain")), json.dumps(answer(kind="etf")),
    json.dumps(answer(thesis="  ")), json.dumps({k: v for k, v in answer().items() if k != "kind"}),
])
def test_parse_read_rejects_anything_off_schema(bad):
    with pytest.raises(reader.ReadError):
        reader.parse_read(bad)


def test_unrecognised_names_and_funds_are_kept_but_not_ranked():
    assert reader.parse_read(json.dumps(answer(recognized=False)))["usable"] is False
    assert reader.parse_read(json.dumps(answer(kind="fund")))["usable"] is False


def test_prompt_withholds_the_screens_own_answer():
    row = {"symbol": "ACME", "name": "Acme", "last": 50.0, "score": 88, "tier": "strong",
           "dcf": {"archetype": "MATURE", "fair_value": 99.0, "upside_pct": 98.0}}
    fund = {"name": "Acme Corp", "sector": "Industrials", "total_revenue": 5e9,
            "operating_margin": 0.2, "recommendation": "strong_buy", "target_mean": 80.0,
            "pe": 18.0}
    p = reader.build_user_prompt(row, fund, "2026-11-22")
    assert "Acme Corp (ticker ACME)" in p and "Operating margin: 20.0%" in p and "18.0x" in p
    for leak in ("88", "strong", "99", "98", "strong_buy", "80.0", "target"):
        assert leak not in p.lower().replace("operating margin: 20.0%", "")


def test_read_one_reads_text_after_a_thinking_block_and_prices_usage():
    out = reader.read_one(FakeClient(), row={"symbol": "ACME"}, fund={}, model="m", effort="medium",
                          max_tokens=4000, rates=CFG["rates_per_mtok"], today="2026-11-22")
    assert out["error"] is None and out["read"]["symbol"] == "ACME"
    assert out["cost_usd"] == pytest.approx((1000 * 0.10 + 800 * 0.50) / 1e6)


def test_read_one_sends_no_sampling_parameters_and_a_strict_schema():
    c = FakeClient()
    reader.read_one(c, row={"symbol": "ACME"}, fund={}, model="claude-haiku-5-5", effort="medium",
                    max_tokens=4000, rates=CFG["rates_per_mtok"], today="2026-11-22")
    kw = c.calls[0]
    assert not {"temperature", "top_p", "top_k", "thinking"} & set(kw)
    fmt = kw["output_config"]["format"]
    assert fmt["type"] == "json_schema" and fmt["schema"]["additionalProperties"] is False


@pytest.mark.parametrize("resp,err", [
    (response(None, stop="refusal"), "refusal"),
    (response('{"recognized": tr', stop="max_tokens"), "truncated"),
    (response(None), "no text"),
    (response(json.dumps(answer(business_quality=-1))), "integer 0-10"),
])
def test_model_side_problems_cost_money_but_never_raise(resp, err):
    out = reader.read_one(FakeClient({"ACME": resp}), row={"symbol": "ACME"}, fund={}, model="m",
                          effort="medium", max_tokens=4000, rates=CFG["rates_per_mtok"],
                          today="2026-11-22")
    assert out["read"] is None and err in out["error"] and out["cost_usd"] > 0


# ─── who gets read ───────────────────────────────────────────────────────────

def research(*syms):
    return {"stocks": [{"symbol": s, "name": s, "sector": "Tech", "last": 10.0,
                        "dcf": {"archetype": "MATURE"}} for s in syms]}


def test_every_screen_name_and_every_held_name_is_read_in_an_order_the_account_does_not_set():
    snap = {"positions": [{"symbol": "BBB"}, {"symbol": "FUNDX"}, {"symbol": "NEWCO"}]}
    fund = {"FUNDX": {"name": "Example Index ETF"}, "NEWCO": {"name": "Newco", "total_revenue": 7e8}}
    names = airun.names_to_read(research("AAA", "BBB", "CCC"), snap, fund)
    # the screen in screen order whoever holds what, then what only the account knows of
    assert [n["symbol"] for n in names] == ["AAA", "BBB", "CCC", "FUNDX", "NEWCO"]
    assert [n["symbol"] for n in airun.names_to_read(research("AAA", "BBB", "CCC"), {}, {})] == ["AAA", "BBB", "CCC"]
    by = {n["symbol"]: n for n in names}
    assert by["FUNDX"]["is_fund"] and not by["FUNDX"]["in_screen"]
    assert not by["NEWCO"]["is_fund"] and by["BBB"]["in_screen"] and by["BBB"]["held"]


def test_due_skips_fresh_reads_and_rereads_on_age_model_or_prompt_change():
    names = airun.names_to_read(research("A", "B", "C", "D", "E"), {}, {})
    fresh = (NOW - timedelta(days=2)).isoformat()
    old = (NOW - timedelta(days=7)).isoformat()
    base = {"model": "claude-haiku-5-5", "prompt_v": reader.PROMPT_VERSION}
    store = {"reads": {"A": {**base, "read_at": fresh}, "B": {**base, "read_at": old},
                       "C": {**base, "read_at": fresh, "model": "other"},
                       "D": {**base, "read_at": fresh, "prompt_v": -1}}}
    todo = airun.due(names, store, now=NOW, refresh_days=6, model="claude-haiku-5-5")
    assert [n["symbol"] for n in todo] == ["B", "C", "D", "E"]
    assert len(airun.due(names, store, now=NOW, refresh_days=6, model="claude-haiku-5-5",
                         force=True)) == 5


def test_reads_for_names_that_left_long_ago_are_pruned():
    store = {"reads": {"GONE": {"read_at": (NOW - timedelta(days=60)).isoformat()},
                       "RECENT": {"read_at": (NOW - timedelta(days=3)).isoformat()},
                       "A": {"read_at": (NOW - timedelta(days=60)).isoformat()}}}
    assert airun.prune(store, airun.names_to_read(research("A"), {}, {}), now=NOW) == 1
    assert set(store["reads"]) == {"RECENT", "A"}


def test_an_unreadable_store_is_reported_and_starts_empty(tmp_path):
    p = tmp_path / "ai_reads.json"
    p.write_text("{torn")
    store, problem = airun.load_store(p)
    assert store["reads"] == {} and "unreadable" in problem


# ─── the money path ──────────────────────────────────────────────────────────

def _run(client, syms, cfg=CFG, limit=None):
    names = airun.names_to_read(research(*syms), {}, {})
    store, saves, tally = airun.empty_store(), [], {}
    res = airun.run(client, names, store, fundamentals={}, cfg=cfg,
                    limit_usd=limit if limit is not None else airun.reservation_usd(len(names), cfg),
                    tally=tally, save=lambda s: saves.append(len(s["reads"])), today="2026-11-22")
    return res, store, saves, tally


def test_a_clean_run_reads_everything_and_saves_after_every_chunk():
    res, store, saves, _ = _run(FakeClient(), ["A", "B", "C", "D", "E"])
    assert res["ok"] == 5 and res["remaining"] == 0 and res["stopped"] is None
    assert saves == [2, 4, 5] and store["reads"]["A"]["in_screen"] is True


def test_no_chunk_starts_unless_its_worst_case_fits_under_the_limit():
    bound = airun.name_bound_usd(CFG)
    limit = bound * 2 + 1e-5
    res, store, _, tally = _run(FakeClient(), ["A", "B", "C", "D", "E"], limit=limit)
    # chunk 1 (2 names) fits exactly; chunk 2 needs what chunk 1 really spent
    # plus two more worst cases, which does not
    assert res["ok"] == 2 and res["remaining"] == 3 and "budget" in res["stopped"]
    assert tally["spent"] <= limit


def test_a_nan_limit_stops_before_any_call():
    c = FakeClient()
    res, *_ = _run(c, ["A", "B"], limit=float("nan"))
    assert c.calls == [] and res["ok"] == 0 and res["stopped"]


def test_api_errors_are_charged_at_their_worst_case_and_stop_the_run():
    boom = RuntimeError("overloaded")
    c = FakeClient({s: boom for s in "ABCDEFGH"})
    res, _, _, tally = _run(c, list("ABCDEFGH"))
    assert res["api_errors"] == 4 and res["remaining"] == 4      # 3 in a row → stop after chunk 2
    assert tally["spent"] == pytest.approx(4 * airun.name_bound_usd(CFG)) and res["cost_assumed"]


def test_a_rejected_key_stops_at_once():
    e = RuntimeError("bad key")
    e.status_code = 401
    res, *_ = _run(FakeClient({"A": e}), ["A", "B", "C", "D"])
    assert "rejected" in res["stopped"] and res["remaining"] == 2


def test_reservation_is_capped_and_bounds_every_run():
    assert airun.reservation_usd(3, CFG) == pytest.approx(3 * airun.name_bound_usd(CFG), abs=1e-4)
    assert airun.reservation_usd(10_000, CFG) == CFG["run_cap_usd"]


# ─── the job, end to end ─────────────────────────────────────────────────────

@pytest.fixture
def box(tmp_path, monkeypatch):
    (tmp_path / "research.json").write_text(json.dumps(research("A", "B", "C")))
    (tmp_path / "snapshot.json").write_text(json.dumps({"positions": [{"symbol": "HELDX"}]}))
    (tmp_path / "fundamentals.json").write_text(json.dumps({"HELDX": {"name": "Held Co"}}))
    monkeypatch.setenv("AI_READS_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(job, "AI_READS", dict(CFG))
    return tmp_path


def _ledger(box):
    return budget.load_ledger(box / "ai_reads_ledger.jsonl")


def test_job_reads_everything_and_settles_at_the_reported_cost(box):
    assert job.main([], client=FakeClient(), now=NOW) == 0
    store = json.loads((box / "ai_reads.json").read_text())
    assert set(store["reads"]) == {"HELDX", "A", "B", "C"}
    assert store["reads"]["HELDX"]["in_screen"] is False
    led = _ledger(box)
    res, sett = [e for e in led if e["kind"] == "reserve"], [e for e in led if e["kind"] == "settle"]
    assert len(res) == 1 and len(sett) == 1 and sett[0]["status"] == "done"
    assert sett[0]["cost_usd"] == pytest.approx(4 * (1000 * 0.10 + 800 * 0.50) / 1e6, abs=1e-4)
    assert sett[0]["cost_usd"] <= res[0]["cost_usd"]
    hist = (box / "ai_reads_history.jsonl").read_text().splitlines()
    assert set(json.loads(hist[0])["raw"]) == {"A", "B", "C", "HELDX"}


def test_job_second_run_in_the_week_spends_nothing(box):
    job.main([], client=FakeClient(), now=NOW)
    c = FakeClient()
    assert job.main([], client=c, now=NOW + timedelta(days=1)) == 0
    assert c.calls == [] and len(_ledger(box)) == 2


def test_job_dry_run_and_status_spend_nothing(box):
    c = FakeClient()
    assert job.main(["--dry-run"], client=c, now=NOW) == 0
    assert job.main(["--status"], client=c, now=NOW) == 0
    assert c.calls == [] and not (box / "ai_reads_ledger.jsonl").exists()


def test_job_unreadable_ledger_refuses_to_spend(box):
    (box / "ai_reads_ledger.jsonl").write_text("garbage\n")
    c = FakeClient()
    assert job.main([], client=c, now=NOW) == 3 and c.calls == []


def test_job_respects_the_monthly_ceiling(box):
    budget.reserve(box / "ai_reads_ledger.jsonl", run_id="x", symbol="*", model="m",
                   trigger="t", cap_usd=3.99, now=NOW - timedelta(days=1), lane="weekly")
    c = FakeClient()
    assert job.main([], client=c, now=NOW) == 0 and c.calls == []


def test_job_off_switch(box, monkeypatch):
    monkeypatch.setattr(job, "AI_READS", {**CFG, "enabled": False})
    c = FakeClient()
    assert job.main([], client=c, now=NOW) == 0 and c.calls == []


def test_job_missing_research_is_an_error(box):
    (box / "research.json").write_text("{}")
    assert job.main([], client=FakeClient(), now=NOW) == 2


def test_settings_caps_cannot_be_raised_from_the_environment(monkeypatch):
    from config import settings
    monkeypatch.setenv("AI_READS_MONTHLY_CEILING_USD", "500")
    assert settings._dd_num("AI_READS_MONTHLY_CEILING_USD", 4.0, 0.0, 4.0) == 4.0
    monkeypatch.setenv("AI_READS_RUN_CAP_USD", "nan")
    assert settings._dd_num("AI_READS_RUN_CAP_USD", 1.5, 0.05, 1.5) == 1.5
    assert settings.AI_READS["monthly_ceiling_usd"] <= 4.0
    # the two ceilings together stay under the overall monthly limit for model spend
    assert settings.AI_READS["monthly_ceiling_usd"] + settings.DEEP_DCF["monthly_ceiling_usd"] < 75


def test_the_weekly_cadence_never_lets_reads_go_stale_on_the_dashboard():
    """The oldest read the dashboard can see is refresh_days + 7 (a read just
    young enough to be skipped, then a week to the next run). It must stay
    inside the dashboard's FRESH_DAYS (11) or the factor switches off."""
    from config.settings import AI_READS
    assert AI_READS["refresh_days"] + 7 < 11
