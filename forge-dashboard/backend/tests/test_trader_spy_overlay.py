"""The options chart's S&P line must be as current as its equity line.

2026-09-11: the equity line reached today (live anchor) while the S&P line
stopped a day behind — the closes cache refreshes once a day (~06:56 UTC).
Days newer than the cache now take SPY's latest trade; days the cache covers
(holidays included) still carry the prior close forward.
"""
import datetime as dt
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from adapters import rh_analytics, trader

TODAY = dt.datetime.now(dt.timezone.utc).date()
LIVE = {"cash": 10.0, "source": "alpaca_live"}


def D(n):
    return (TODAY - dt.timedelta(days=n)).isoformat()


def _cache(tmp_path, monkeypatch, closes):
    f = tmp_path / "SPY.json"
    f.write_text(json.dumps({"dates": list(closes), "closes": list(closes.values())}))
    monkeypatch.setattr(rh_analytics, "_prices_path", lambda sym: f)


def _hist(*rows):
    return {"days": [{"date": d, "equity": e} for d, e in rows]}


def test_live_point_is_priced_from_the_live_spy_trade(tmp_path, monkeypatch):
    _cache(tmp_path, monkeypatch, {D(2): 100.0, D(1): 101.0})
    s = trader._history_series(_hist((D(2), 1000.0), (D(1), 1005.0)),
                               live={"equity": 1012.0, **LIVE}, spy_today=102.0)
    assert s[-1]["live"] and s[-1]["equity"] == 1012.0
    assert s[-2]["spy_equity"] == 1010.0
    assert s[-1]["spy_equity"] == 1020.0          # same start capital, SPY 100 → 102


def test_no_quote_means_no_stale_pairing(tmp_path, monkeypatch):
    _cache(tmp_path, monkeypatch, {D(2): 100.0, D(1): 101.0})
    s = trader._history_series(_hist((D(2), 1000.0), (D(1), 1005.0)),
                               live={"equity": 1012.0, **LIVE}, spy_today=None)
    assert s[-1]["spy_equity"] is None and s[-2]["spy_equity"] == 1010.0


def test_after_the_close_todays_row_uses_the_live_price(tmp_path, monkeypatch):
    _cache(tmp_path, monkeypatch, {D(2): 100.0, D(1): 101.0})   # cache lacks today
    s = trader._history_series(_hist((D(2), 1000.0), (D(1), 1005.0), (D(0), 1011.0)),
                               live={"equity": 1011.0, **LIVE}, spy_today=103.0)
    assert not s[-1].get("live")                  # nightly row exists → no anchor
    assert s[-1]["spy_equity"] == 1030.0          # not yesterday's 1010 copied forward


def test_holidays_inside_the_cache_carry_the_prior_close(tmp_path, monkeypatch):
    _cache(tmp_path, monkeypatch, {D(5): 100.0, D(3): 101.0, D(1): 102.0})
    s = trader._history_series(
        _hist((D(5), 1000.0), (D(4), 1000.0), (D(3), 1002.0), (D(1), 1004.0)),
        live={"equity": 1004.0, "cash": 1.0, "source": "cached_state"}, spy_today=999.0)
    assert s[1]["spy_equity"] == s[0]["spy_equity"] == 1000.0   # closed market stays flat


def test_a_long_stale_cache_does_not_paint_old_days_with_todays_price(tmp_path, monkeypatch):
    _cache(tmp_path, monkeypatch, {D(10): 100.0})
    s = trader._history_series(_hist((D(10), 1000.0), (D(8), 1000.0), (D(1), 1000.0)),
                               live={"equity": 1000.0, "cash": 1.0, "source": "cached_state"},
                               spy_today=150.0)
    assert s[1]["spy_equity"] == 1000.0           # 8 days old: carried, not repainted
    assert s[2]["spy_equity"] == 1500.0           # recent uncached day: live price
