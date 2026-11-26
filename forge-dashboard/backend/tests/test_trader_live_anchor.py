"""The wheel chart's right edge must equal the headline equity.

Daily rows land at 21:00 UTC, so during the trading day the chart ended at
yesterday while the card showed live equity ($101,869 vs $103,069 measured
2026-09-11). get_trader appends a flagged live mark; analytics must ignore it.
"""
import datetime as dt
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from adapters import trader
from adapters import trader_analytics as ta

TODAY = dt.datetime.now(dt.timezone.utc).date()


def _row(days_ago, eq):
    return {"date": (TODAY - dt.timedelta(days=days_ago)).isoformat(),
            "equity": eq, "spy_equity": eq}


def test_live_mark_appended_when_newest_row_is_yesterday():
    s = trader._with_live_anchor([_row(2, 100.0), _row(1, 101.0)],
                                 103069.0, 93522.0, "alpaca_live")
    assert len(s) == 3 and s[-1]["live"] is True
    assert s[-1]["date"] == TODAY.isoformat()
    assert s[-1]["equity"] == 103069.0          # right edge == headline
    assert s[-1]["spy_equity"] is None          # no fake intraday SPY pairing


def test_no_duplicate_once_the_nightly_row_exists():
    base = [_row(1, 101.0), _row(0, 102.0)]
    assert trader._with_live_anchor(base, 103.0, 1.0, "alpaca_live") == base


def test_cached_fallback_never_invents_a_point():
    base = [_row(1, 101.0)]
    assert trader._with_live_anchor(base, 103.0, 1.0, "cached_state") == base


def test_empty_series_and_zero_equity_pass_through():
    assert trader._with_live_anchor([], 103.0, 1.0, "alpaca_live") == []
    base = [_row(1, 101.0)]
    assert trader._with_live_anchor(base, 0.0, 1.0, "alpaca_live") == base


def test_analytics_ignore_the_live_mark(monkeypatch):
    days = [_row(i, 100000.0 + (10 - i) * 50) for i in range(10, 0, -1)]
    live = {"date": TODAY.isoformat(), "equity": 150000.0,  # absurd on purpose
            "spy_equity": None, "live": True}
    monkeypatch.setattr(trader, "get_trader",
                        lambda: {"history": days + [live], "stats": {}, "lifetime": {}})
    out = ta.wheel_analytics()
    assert out["n_days"] == len(days)
    assert out["headline"]["total_return_pct"] < 1.0   # the 150k mark never counted
