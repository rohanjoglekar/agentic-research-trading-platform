"""trader_analytics.compute — verified against an independent numpy
recomputation (house rule: never trust one code path for a money statistic).

The seeded random-walk fixture has a TRUE beta of 1.3 baked into its
construction; the identity case (book == benchmark) must produce beta 1,
R^2 1, alpha ~= 0 exactly.
"""
import math
import random
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from adapters.trader_analytics import compute, RF_DAILY, TRADING_DAYS  # noqa: E402

np = pytest.importorskip("numpy")


def _fixture(n=120, seed=42):
    random.seed(seed)
    eq, bench = [100_000.0], [100_000.0]
    for _ in range(n - 1):
        rb = random.gauss(0.0004, 0.01)
        rr = 0.0002 + 1.3 * rb + random.gauss(0, 0.004)
        bench.append(bench[-1] * (1 + rb))
        eq.append(eq[-1] * (1 + rr))
    dates = [f"2026-{(i // 30) + 1:02d}-{(i % 30) + 1:02d}" for i in range(n)]
    return dates, eq, bench


def test_matches_numpy_recomputation():
    dates, eq, bench = _fixture()
    out = compute(dates, eq, bench, "SPY")

    r = np.diff(eq) / np.array(eq[:-1])
    b = np.diff(bench) / np.array(bench[:-1])
    beta = np.cov(r, b, ddof=1)[0, 1] / np.var(b, ddof=1)
    alpha = ((r.mean() - RF_DAILY) - beta * (b.mean() - RF_DAILY)) * TRADING_DAYS * 100
    sharpe = (r.mean() - RF_DAILY) / r.std(ddof=1) * math.sqrt(TRADING_DAYS)
    vol = r.std(ddof=1) * math.sqrt(TRADING_DAYS) * 100
    mdd = ((np.array(eq) / np.maximum.accumulate(eq)) - 1).min() * 100

    assert abs(out["relative"]["beta"] - beta) <= 1e-3
    assert abs(out["relative"]["alpha_ann_pct"] - alpha) <= 0.05
    assert abs(out["headline"]["sharpe"] - sharpe) <= 1e-3
    assert abs(out["headline"]["vol_ann_pct"] - vol) <= 0.01
    assert abs(out["headline"]["max_drawdown_pct"] - mdd) <= 0.01
    # sanity: the fixture's constructed beta is 1.3
    assert 1.1 < out["relative"]["beta"] < 1.5


def test_identity_benchmark_is_exact():
    dates, eq, _ = _fixture()
    out = compute(dates, eq, list(eq), "SELF")
    rel = out["relative"]
    assert rel["beta"] == 1.0
    assert rel["r_squared"] == 1.0
    assert abs(rel["alpha_ann_pct"]) < 0.01
    assert rel["active_return_pct"] == 0.0


def test_leading_none_benchmark_still_computes():
    # the wheel's SPY overlay starts on day 2 — leading Nones must not crash
    # or poison the relative block
    dates, eq, bench = _fixture()
    out = compute(dates, eq, [None, None] + bench[2:], "SPY")
    assert out["available"]
    assert out["relative"]["beta"] is not None
    assert out["relative"]["n_overlap_days"] == len(dates) - 2


def test_too_short_series_fails_closed():
    out = compute(["2026-01-01", "2026-01-02"], [100.0, 101.0], None)
    assert out["available"] is False


# ── intraday resampling ──────────────────────────────────────────────────────
# Every statistic annualizes by sqrt(252) and reads elapsed time off the point
# COUNT, so the series must carry one observation per trading day. The alpha
# runner fires 3x a weekday, which silently violated that.

def test_intraday_points_collapse_to_one_per_day():
    from adapters import trader_analytics as ta
    dates = ["2026-07-06", "2026-07-07", "2026-07-07", "2026-07-07",
             "2026-07-08", "2026-07-08"]
    eq = [100.0, 101.0, 102.0, 103.0, 104.0, 105.0]
    d2, e2, b2 = ta._to_daily(dates, eq, None)
    assert d2 == ["2026-07-06", "2026-07-07", "2026-07-08"]
    assert e2 == [100.0, 103.0, 105.0]          # last mark of each day wins
    assert b2 is None


def test_benchmark_is_resampled_in_lockstep():
    from adapters import trader_analytics as ta
    dates = ["2026-07-06", "2026-07-07", "2026-07-07"]
    d2, e2, b2 = ta._to_daily(dates, [100.0, 101.0, 102.0], [50.0, 51.0, 52.0])
    assert d2 == ["2026-07-06", "2026-07-07"]
    assert e2 == [100.0, 102.0] and b2 == [50.0, 52.0]


def test_an_already_daily_series_passes_through_untouched():
    from adapters import trader_analytics as ta
    dates = ["2026-07-06", "2026-07-07", "2026-07-08"]
    eq = [100.0, 101.0, 102.0]
    d2, e2, b2 = ta._to_daily(dates, eq, None)
    assert d2 is dates and e2 is eq and b2 is None


def test_triplicated_days_do_not_deflate_volatility():
    """The same price path sampled 3x/day must report the same annualized
    volatility as the daily path — that equality is the whole point."""
    from adapters import trader_analytics as ta
    daily_d, daily_e = [], []
    intra_d, intra_e = [], []
    px = 100.0
    for i in range(60):
        day = f"2026-07-{i % 28 + 1:02d}-{i // 28}"   # unique per i
        px *= 1.004 if i % 2 else 0.997
        daily_d.append(day); daily_e.append(px)
        for k in range(3):                            # 3 marks, same close last
            intra_d.append(day); intra_e.append(px * (1 + 0.01 * (2 - k)))
    a = ta.compute(daily_d, daily_e)
    b = ta.compute(intra_d, intra_e)
    assert abs(a["headline"]["vol_ann_pct"] - b["headline"]["vol_ann_pct"]) < 1e-6
    assert a["n_days"] == b["n_days"] == 60
