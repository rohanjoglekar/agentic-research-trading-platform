"""Beta needs a SAME-DAY benchmark.

Until 2026-09-17 the alpha runner resolved SPY from the price cache before
asking the broker, so each history row stored the PRIOR session's close under
today's date. Regressing the book on a one-day-lagged benchmark measures the
correlation between today's return and yesterday's market -- which is ~0 for
any book -- so beta collapses toward zero and CAPM alpha (return - beta x
benchmark) absorbs the market exposure that beta should have explained.

On the real 55-day window (2026-06-30 -> 2026-09-17) that was the difference
between beta +0.061 (corr 0.064, R^2 0.004) and the correctly aligned +0.490
(corr 0.473, R^2 0.224). The rows are not rewritten; the benchmark is resolved
from the daily closes cache at read time instead.
"""
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import adapters  # noqa: E402
from adapters import rh_analytics, trader_analytics  # noqa: E402
from adapters.trader_analytics import RF_DAILY, TRADING_DAYS  # noqa: E402


def _beta_one_book(n=120, seed=7, noise=0.002, drift=0.0004):
    """A book that IS the market (beta 1.0, optionally plus idiosyncratic
    noise), with the benchmark recorded one session late -- the exact defect."""
    random.seed(seed)
    spy = [700.0]
    for _ in range(n):
        spy.append(spy[-1] * (1 + random.gauss(drift, 0.01)))
    eq = [100_000.0]
    for i in range(1, n + 1):
        same_day = spy[i] / spy[i - 1] - 1          # book tracks the SAME day
        kick = random.gauss(0, noise) if noise else 0.0
        eq.append(eq[-1] * (1 + same_day + kick))
    dates = [f"2026-{(i // 28) + 1:02d}-{(i % 28) + 1:02d}" for i in range(n)]
    rows = [{"as_of": f"{d}T19:45:00Z", "equity": e, "spy": s}
            for d, e, s in zip(dates, eq[1:], spy[:-1])]  # spy[:-1] = LAGGED
    truth = dict(zip(dates, spy[1:]))                     # spy[1:]  = same day
    return rows, truth


def _run(monkeypatch, rows, truth):
    monkeypatch.setattr(adapters, "read_jsonl_tail", lambda p, n: rows)
    monkeypatch.setattr(adapters, "read_json", lambda p, default=None: default)
    monkeypatch.setattr(rh_analytics, "daily_closes", lambda sym="SPY": truth)
    return trader_analytics.alpha_analytics()


def test_daily_closes_reads_the_cache(tmp_path, monkeypatch):
    f = tmp_path / "SPY.json"
    f.write_text('{"dates": ["2026-09-15", "2026-09-16"], "closes": [757.39, 754.05]}')
    monkeypatch.setattr(rh_analytics, "_prices_path", lambda sym: f)
    assert rh_analytics.daily_closes("SPY") == {"2026-09-15": 757.39, "2026-09-16": 754.05}


def test_daily_closes_survives_a_missing_or_broken_cache(tmp_path, monkeypatch):
    monkeypatch.setattr(rh_analytics, "_prices_path", lambda sym: tmp_path / "absent.json")
    assert rh_analytics.daily_closes("SPY") == {}
    f = tmp_path / "SPY.json"
    f.write_text('{"dates": ["2026-09-15", "2026-09-16"], "closes": [757.39, null]}')
    monkeypatch.setattr(rh_analytics, "_prices_path", lambda sym: f)
    assert rh_analytics.daily_closes("SPY") == {"2026-09-15": 757.39}


def test_lagged_benchmark_hides_a_beta_of_one(monkeypatch):
    """Guard on the defect itself: the recorded column cannot recover beta,
    so an empty cache (no fallback available) must stay visibly broken."""
    rows, _ = _beta_one_book()
    rel = _run(monkeypatch, rows, {})["relative"]  # empty cache -> recorded
    assert abs(rel["beta"]) < 0.3, rel["beta"]
    assert rel["r_squared"] < 0.1, rel["r_squared"]


def test_same_day_close_recovers_the_true_beta(monkeypatch):
    rows, truth = _beta_one_book()
    rel = _run(monkeypatch, rows, truth)["relative"]
    assert 0.85 < rel["beta"] < 1.15, rel["beta"]
    assert rel["r_squared"] > 0.8, rel["r_squared"]


def test_beta_near_zero_inflates_alpha(monkeypatch):
    """Why it matters for the dashboard: alpha = excess - beta x benchmark, so
    a beta pinned at zero hands the market's own drift to the strategy. The
    fixture book is the market exactly -- no noise, no edge -- riding a market
    that drifts ~20%/yr, so its true alpha is zero by construction and anything
    the lagged benchmark reports is the market's drift wearing a skill label."""
    rows, truth = _beta_one_book(noise=0.0, drift=0.0008)
    lagged = _run(monkeypatch, rows, {})["relative"]
    aligned = _run(monkeypatch, rows, truth)["relative"]

    # The book's own annualized excess return, computed independently.
    eq = [float(r["equity"]) for r in rows]
    rets = [eq[i] / eq[i - 1] - 1 for i in range(1, len(eq))]
    excess_ann_pct = (sum(rets) / len(rets) - RF_DAILY) * TRADING_DAYS * 100

    # Aligned: the market explains the book, so alpha is ~0 by construction.
    assert abs(aligned["alpha_ann_pct"]) < 1.0, aligned["alpha_ann_pct"]
    # Lagged: beta ~ 0 subtracts nothing, so "alpha" IS the raw excess return.
    assert abs(lagged["alpha_ann_pct"] - excess_ann_pct) < 1.5, (
        lagged["alpha_ann_pct"], excess_ann_pct)
    assert excess_ann_pct > 3.0, excess_ann_pct  # the fixture does drift up


def test_today_keeps_its_live_recorded_price(monkeypatch):
    """The closes cache lags one session, so the newest row must fall back to
    the value the runner recorded -- which is now a live quote, not a stale
    one. Dropping that date must not drop the row."""
    rows, truth = _beta_one_book()
    truth.pop(rows[-1]["as_of"][:10])
    out = _run(monkeypatch, rows, truth)
    assert out["n_days"] == len(rows)
    assert out["relative"]["n_overlap_days"] == len(rows)
    assert 0.8 < out["relative"]["beta"] < 1.2, out["relative"]["beta"]
