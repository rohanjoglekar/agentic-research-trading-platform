"""The no-overspend rule: reserve at the cap, settle at cost, ceiling, pacing."""
from datetime import datetime, timedelta, timezone

import pytest

from deep_dcf import budget

UTC = timezone.utc


def _dt(y, m, d, h=0):
    return datetime(y, m, d, h, tzinfo=UTC)


def test_cycle_bounds_mid_cycle_and_before_cycle_day():
    assert budget.cycle_bounds(_dt(2026, 11, 20), 15) == (_dt(2026, 11, 15), _dt(2026, 12, 15))
    assert budget.cycle_bounds(_dt(2026, 11, 14, 23), 15) == (_dt(2026, 10, 15), _dt(2026, 11, 15))
    assert budget.cycle_bounds(_dt(2026, 11, 15), 15) == (_dt(2026, 11, 15), _dt(2026, 12, 15))


def test_cycle_bounds_wraps_the_year():
    assert budget.cycle_bounds(_dt(2027, 1, 3), 15) == (_dt(2026, 12, 15), _dt(2027, 1, 15))
    assert budget.cycle_bounds(_dt(2026, 12, 20), 15) == (_dt(2026, 12, 15), _dt(2027, 1, 15))


def test_cycle_day_out_of_range_is_refused():
    with pytest.raises(ValueError):
        budget.cycle_bounds(_dt(2026, 1, 1), 31)


def test_missing_ledger_is_empty_but_corrupt_ledger_raises(tmp_path):
    p = tmp_path / "ledger.jsonl"
    assert budget.load_ledger(p) == []
    p.write_text('{"kind":"reserve","run_id":"a","cost_usd":8,"ts":"2026-11-16T00:00:00+00:00"}\nnot json\n')
    with pytest.raises(budget.LedgerError):
        budget.load_ledger(p)


@pytest.mark.parametrize("bad", [
    '{"kind":"reserve","run_id":"a","cost_usd":-1,"ts":"2026-11-16T00:00:00+00:00"}',
    '{"kind":"reserve","run_id":"a","cost_usd":"x","ts":"2026-11-16T00:00:00+00:00"}',
    '{"kind":"reserve","run_id":"a","cost_usd":NaN,"ts":"2026-11-16T00:00:00+00:00"}',
    '{"kind":"refund","run_id":"a","cost_usd":1,"ts":"2026-11-16T00:00:00+00:00"}',
    '{"kind":"reserve","cost_usd":1,"ts":"2026-11-16T00:00:00+00:00"}',
    '{"kind":"reserve","run_id":"a","cost_usd":1,"ts":"yesterday"}',
    '[1,2,3]',
])
def test_invalid_entries_fail_closed(tmp_path, bad):
    p = tmp_path / "ledger.jsonl"
    p.write_text(bad + "\n")
    with pytest.raises(budget.LedgerError):
        budget.load_ledger(p)


def test_unsettled_run_counts_at_its_cap_and_settle_replaces_it(tmp_path):
    p = tmp_path / "ledger.jsonl"
    now = _dt(2026, 11, 16)
    budget.reserve(p, run_id="r1", symbol="ACME", model="m", trigger="T1", cap_usd=8, now=now)
    start, end = budget.cycle_bounds(now, 15)
    assert budget.spent_between(budget.load_ledger(p), start, end) == 8.0     # crash = worst case
    budget.settle(p, run_id="r1", symbol="ACME", cost_usd=3.21, cost_source="reported",
                  status="accepted", now=now)
    assert budget.spent_between(budget.load_ledger(p), start, end) == 3.21


def test_run_belongs_to_the_cycle_it_was_reserved_in(tmp_path):
    p = tmp_path / "ledger.jsonl"
    budget.reserve(p, run_id="r1", symbol="A", model="m", trigger="T1", cap_usd=8,
                   now=_dt(2026, 11, 14, 23))
    budget.settle(p, run_id="r1", symbol="A", cost_usd=4, cost_source="reported",
                  status="accepted", now=_dt(2026, 11, 15, 1))          # settled after rollover
    e = budget.load_ledger(p)
    assert budget.spent_between(e, _dt(2026, 10, 15), _dt(2026, 11, 15)) == 4.0
    assert budget.spent_between(e, _dt(2026, 11, 15), _dt(2026, 12, 15)) == 0.0


def test_ceiling_blocks_and_manual_runs_cannot_bypass_it():
    now = _dt(2026, 12, 10)
    entries = [{"kind": "reserve", "run_id": f"r{i}", "cost_usd": 8, "ts": _dt(2026, 11, 20).isoformat()}
               for i in range(8)]                                       # $64 reserved
    ok, why, _ = budget.can_start(entries, now=now, ceiling=70, cycle_day=15, run_cap=8)
    assert not ok and "ceiling" in why
    ok, why, _ = budget.can_start(entries, now=now, ceiling=70, cycle_day=15, run_cap=8, pace=False)
    assert not ok and "ceiling" in why
    ok, _, _ = budget.can_start(entries, now=now, ceiling=70, cycle_day=15, run_cap=5, pace=False)
    assert ok                                                           # 64 + 5 ≤ 70


def test_pacing_holds_back_an_early_backlog_but_not_a_manual_run():
    now = _dt(2026, 11, 15, 1)                                          # first hour of the cycle
    entries = [{"kind": "settle", "run_id": f"r{i}", "cost_usd": 4, "ts": now.isoformat()}
               for i in range(3)]                                       # $12 spent
    ok, why, info = budget.can_start(entries, now=now, ceiling=70, cycle_day=15, run_cap=8)
    assert info["pace_allowance_usd"] < 20
    assert not ok and "pacing" in why
    assert budget.can_start(entries, now=now, ceiling=70, cycle_day=15, run_cap=8, pace=False)[0]


def test_pace_line_reaches_the_ceiling_before_the_cycle_ends():
    start, end = budget.cycle_bounds(_dt(2026, 11, 20), 15)
    assert budget.pace_allowance(end - timedelta(days=7), start, end, 70) == 70
    assert budget.pace_allowance(start, start, end, 70) == pytest.approx(70 * 7 / 30, abs=0.01)


def test_worst_case_month_never_exceeds_the_ceiling():
    """Every run costs its full cap and the runner tries constantly: total spend
    in the cycle must still be ≤ the monthly ceiling."""
    entries, now, n = [], _dt(2026, 11, 15), 0
    while now < _dt(2026, 12, 15):
        for cap in (8.0, 5.0):
            while True:
                ok, _, _ = budget.can_start(entries, now=now, ceiling=70, cycle_day=15, run_cap=cap)
                if not ok:
                    break
                n += 1
                entries.append({"kind": "reserve", "run_id": f"r{n}", "cost_usd": cap,
                                "ts": now.isoformat()})
        now += timedelta(hours=6)
    spent = budget.spent_between(entries, _dt(2026, 11, 15), _dt(2026, 12, 15))
    assert 60 <= spent <= 70


@pytest.mark.parametrize("ceiling, cap", [(float("nan"), 4.0), (float("inf"), 4.0), (70.0, float("nan")),
                                          (70.0, float("inf")), (-1.0, 4.0)])
def test_non_finite_or_negative_limits_refuse_instead_of_passing(ceiling, cap):
    # every comparison against nan is False, so `spent + cap > ceiling` let a nan
    # ceiling pass with any amount already spent
    spent = [{"kind": "settle", "run_id": "r", "cost_usd": 5000, "ts": _dt(2026, 11, 20).isoformat()}]
    for entries in ([], spent):
        assert not budget.can_start(entries, now=_dt(2026, 11, 25), ceiling=ceiling, cycle_day=15,
                                    run_cap=cap, pace=False)[0]


def test_reservations_are_counted_by_lane_and_old_entries_count_as_manual(tmp_path):
    p = tmp_path / "ledger.jsonl"
    now = _dt(2026, 11, 20)
    budget.reserve(p, run_id="a", symbol="A", model="m", trigger="T3", cap_usd=4, now=now, lane="window")
    budget.reserve(p, run_id="b", symbol="B", model="m", trigger="T3", cap_usd=4, now=now, lane="fast")
    budget.reserve(p, run_id="c", symbol="C", model="m", trigger="T6", cap_usd=4, now=now)
    with open(p, "a") as fh:                                           # written before lanes existed
        fh.write('{"kind":"reserve","run_id":"d","cost_usd":4,"trigger":"T1","ts":"%s"}\n' % now.isoformat())
    e = budget.load_ledger(p)
    since = now - timedelta(days=1)
    assert budget.count_reservations(e, since=since, lane="window") == 1
    assert budget.count_reservations(e, since=since, lane="fast") == 1
    assert budget.count_reservations(e, since=since, lane="manual") == 2
    assert budget.count_reservations(e, since=now + timedelta(seconds=1), lane="window") == 0


def test_hostile_env_values_cannot_break_settings_or_lift_a_limit(monkeypatch):
    import importlib

    import config.settings as cs
    for k, v in {"DEEP_DCF_MONTHLY_CEILING_USD": "nan", "DEEP_DCF_RUN_CAP_OPUS_USD": "inf",
                 "DEEP_DCF_RUN_CAP_SONNET_USD": "-5", "DEEP_DCF_CYCLE_DAY": "abc",
                 "DEEP_DCF_WINDOW_CAP": "1e9", "DEEP_DCF_RUN_TIMEOUT_S": ""}.items():
        monkeypatch.setenv(k, v)
    try:
        importlib.reload(cs)                                           # must not raise: traders import this
        d = cs.DEEP_DCF
        assert d["monthly_ceiling_usd"] == 70.0 and d["cycle_day"] == 15 and d["window_cap"] == 10
        assert d["run_cap_usd"] == {"claude-opus-5-5": 4.0, "claude-sonnet-5-5": 0.25}
        assert d["run_timeout_s"] == 1500
    finally:
        monkeypatch.undo()
        importlib.reload(cs)


def test_non_positive_cap_never_starts():
    assert not budget.can_start([], now=_dt(2026, 11, 20), ceiling=70, cycle_day=15, run_cap=0)[0]


def test_settings_ceiling_cannot_be_raised_past_70_by_env(monkeypatch):
    import importlib

    monkeypatch.setenv("DEEP_DCF_MONTHLY_CEILING_USD", "500")
    import config.settings as cs
    importlib.reload(cs)
    try:
        assert cs.DEEP_DCF["monthly_ceiling_usd"] == 70.0
    finally:
        monkeypatch.delenv("DEEP_DCF_MONTHLY_CEILING_USD")
        importlib.reload(cs)
    assert cs.DEEP_DCF["monthly_ceiling_usd"] <= 70.0
