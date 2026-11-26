"""The benchmark line must be today's price, not yesterday's close.

The closes cache refreshes ~06:56 UTC, so it holds the PRIOR session's close.
_ensure_etf_prices read the cache first for every symbol, so alpha_history
recorded a one-session-stale SPY: 09-17 stored 754.05, the 09-16 close, and the
same off-by-one held every day. That skewed the equity chart's benchmark line
and the "active vs SPY" headline. Tradeable ETFs keep cache-first ordering —
their price feeds order sizing and is deliberately not changed here.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from trading import alpha_runner as ar

CFG = {"core_etfs": {"VOO": 0.6, "QQQ": 0.4}, "reserve_etf": "BIL", "benchmark": "SPY"}


class Trader:
    def __init__(self, live=None):
        self.live = live or {}
        self.asked = []

    def latest_price(self, s):
        self.asked.append(s)
        return self.live.get(s)


def test_benchmark_prefers_the_live_quote_over_a_stale_close(monkeypatch):
    monkeypatch.setattr(ar, "_price_cache_last", lambda s: 754.05)   # yesterday
    prices, t = {}, Trader({"SPY": 757.20})                          # today
    ar._ensure_etf_prices(CFG, prices, t)
    assert prices["SPY"] == 757.20
    assert "SPY" in t.asked


def test_tradeable_etfs_still_use_the_cache_first(monkeypatch):
    monkeypatch.setattr(ar, "_price_cache_last", lambda s: 91.50)
    prices, t = {}, Trader({"VOO": 999.0, "QQQ": 999.0, "BIL": 999.0, "SPY": 757.2})
    ar._ensure_etf_prices(CFG, prices, t)
    for etf in ("VOO", "QQQ", "BIL"):
        assert prices[etf] == 91.50, f"{etf} must not change pricing order"
    assert t.asked == ["SPY"]          # only the benchmark asks for a live quote


def test_benchmark_falls_back_to_the_cache_when_the_quote_fails(monkeypatch):
    monkeypatch.setattr(ar, "_price_cache_last", lambda s: 754.05)
    prices = {}
    ar._ensure_etf_prices(CFG, prices, Trader({}))                   # quote returns None
    assert prices["SPY"] == 754.05


def test_an_already_known_price_is_never_overwritten(monkeypatch):
    monkeypatch.setattr(ar, "_price_cache_last", lambda s: 1.0)
    prices = {"SPY": 760.0}
    ar._ensure_etf_prices(CFG, prices, Trader({"SPY": 757.2}))
    assert prices["SPY"] == 760.0


def test_the_held_position_fallback_still_applies(monkeypatch):
    monkeypatch.setattr(ar, "_price_cache_last", lambda s: None)
    prices = {}
    ar._ensure_etf_prices(CFG, prices, Trader({}), [{"symbol": "BIL", "current_price": 91.5}])
    assert prices["BIL"] == 91.5
