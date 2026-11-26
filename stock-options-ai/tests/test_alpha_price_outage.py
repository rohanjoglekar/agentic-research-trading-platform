"""A missing quote must never liquidate a held anchor/reserve ETF.

2026-09-11 16:45 UTC: Alpaca market data 504'd, BIL went unpriced, the book
dropped the reserve and computed `sell BIL $179,870.60` plus `sell QQQ
$42,521.14` — blocked only because the clock check failed in the same outage.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from strategies.alpha_portfolio import TargetBook
from trading import alpha_runner as ar

CFG = {"core_etfs": {"VOO": 0.6, "QQQ": 0.4}, "reserve_etf": "BIL", "benchmark": "SPY",
       "drift_band": 0.02, "min_trade_usd": 50.0}
EQUITY = 1060544.48


class NoQuotes:
    def latest_price(self, s):
        return None


def _book(dollars):
    b = TargetBook()
    b.dollars = dict(dollars)
    return b


def test_held_etf_priced_from_its_position_when_quotes_fail(monkeypatch):
    monkeypatch.setattr(ar, "_price_cache_last", lambda s: None)
    prices = {}
    ar._ensure_etf_prices(CFG, prices, NoQuotes(), [{"symbol": "BIL", "current_price": 91.5}])
    assert prices["BIL"] == 91.5
    assert "VOO" not in prices                  # unheld + unquoted still degrades to cash


def test_a_live_quote_still_wins_over_the_position_mark(monkeypatch):
    monkeypatch.setattr(ar, "_price_cache_last", lambda s: None)

    class Q:
        def latest_price(self, s):
            return 91.52
    prices = {}
    ar._ensure_etf_prices(CFG, prices, Q(), [{"symbol": "BIL", "current_price": 91.5}])
    assert prices["BIL"] == 91.52


def test_replay_0911_1645_the_reserve_is_not_liquidated():
    current = {"BIL": 179870.6, "QQQ": 42521.14, "VOO": 90000.0, "AAPL": 40000.0}
    book = _book({"VOO": 149780.89, "AAPL": 40000.0})   # BIL + QQQ unpriced → dropped
    before = ar.diff_orders(book, current, EQUITY, CFG)
    assert {"symbol": "BIL", "side": "sell", "notional": 179870.6} == \
        {k: v for k, v in next(o for o in before if o["symbol"] == "BIL").items()
         if k in ("symbol", "side", "notional")}      # the bug, reproduced exactly
    after = ar.diff_orders(book, current, EQUITY, CFG, hold={"BIL", "QQQ"})
    assert not {"BIL", "QQQ"} & {o["symbol"] for o in after}


def test_hold_never_blocks_an_explicit_discipline_sell():
    orders = ar.diff_orders(_book({}), {"BIL": 1000.0}, 100000.0, CFG,
                            force={"BIL"}, hold={"BIL"})
    assert orders and orders[0]["side"] == "sell"


def test_clock_gate_names_the_cause():
    class Open:
        def clock(self): return {"is_open": True}

    class Closed:
        def clock(self): return {"is_open": False}

    class Broken:
        def clock(self): raise RuntimeError("Alpaca 500 on GET /v2/clock")
    assert ar._clock_gate(Open()) is None
    assert ar._clock_gate(Closed()) == "market_closed"
    assert ar._clock_gate(Broken()) == "clock_unavailable"
