"""What the public is told about one company, checked at the adapter.

The API server is not needed for these: the rules live in the adapters so that
they hold for whatever serves them.
"""
import datetime as dt
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from adapters import rh_ai_reads, rh_analytics  # noqa: E402


def _never(*_a, **_k):
    raise AssertionError("must not be called")


def test_only_a_screened_company_is_answered_and_nothing_is_fetched_for_the_rest(monkeypatch):
    """A held company outside the screen has warm caches, a stranger has none.
    Reading or fetching anything for either would let the difference show."""
    monkeypatch.setattr(rh_ai_reads, "screen_symbols", lambda: ["AAA"])
    for fn in ("get_prices", "get_fundamentals", "symbol_stats", "get_news", "_get_chart5y", "_get_intraday"):
        monkeypatch.setattr(rh_analytics, fn, _never)
    monkeypatch.setattr(rh_analytics.brokerage, "snapshot", _never)
    for sym in ("OFFX", "ZZZZ"):
        assert rh_analytics.get_symbol(sym, account=False) == {"symbol": sym, "error": rh_analytics.UNSCREENED}
        for rng in ("1D", "1W", "1Y", "5Y"):
            got = rh_analytics.chart_series(sym, rng, public=True)
            assert got["dates"] == [] and got["closes"] == []
    assert rh_analytics.screened("aaa") and not rh_analytics.screened("OFFX")


def test_the_public_read_carries_the_day_of_the_read_not_the_time(monkeypatch):
    """The job reads one name after another. Times would give the order away."""
    now = dt.datetime.now(dt.timezone.utc)
    reads = {s: {"usable": True, "raw": 60.0 + i, "conviction": "high", "thesis": "t", "key_risk": "r",
                 "sub": {}, "kind": "company", "read_at": (now - dt.timedelta(seconds=40 * i)).isoformat()}
             for i, s in enumerate(["AAA", "BBB", "CCC"])}
    monkeypatch.setattr(rh_ai_reads, "load_store", lambda: {"reads": reads})
    monkeypatch.setattr(rh_ai_reads, "screen_symbols", lambda: ["AAA", "BBB", "CCC"])
    stamps = {rh_ai_reads.public_read(s)["ai_read_at"] for s in reads}
    assert all(len(x) == 10 for x in stamps)
    assert stamps <= {now.date().isoformat(), (now - dt.timedelta(seconds=80)).date().isoformat()}
    # the owner's view keeps the full timestamp
    assert "T" in rh_ai_reads.fields(rh_ai_reads.for_screen()["reads"]["AAA"])["ai_read_at"]
