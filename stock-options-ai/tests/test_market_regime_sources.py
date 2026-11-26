"""VIX source chain (2026-09-11).

yfinance led the chain and had been failing silently, so the regime ran on the
SPY realized-vol proxy (~0.55x the real index); on 09-11 an Alpaca 504 took the
proxy out too and sizing halved. CBOE's official quote now leads, yfinance is
wall-clock bounded, and every read records its source.
"""
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from data import market_regime as m


@pytest.fixture(autouse=True)
def fresh_cache(monkeypatch):
    monkeypatch.setattr(m, "_cache", {"value": None, "ts": 0, "source": None})
    # offline by default: a test that wants FRED patches it back in
    monkeypatch.setattr(m, "_fetch_vix_fred", lambda: None)


def _boom(*a, **k):
    raise AssertionError("a later source was consulted after an earlier one succeeded")


def test_cboe_leads_and_is_labeled(monkeypatch):
    monkeypatch.setattr(m, "_fetch_vix_cboe", lambda: 15.68)
    monkeypatch.setattr(m, "_fetch_vix_yfinance_bounded", _boom)
    monkeypatch.setattr(m, "_fetch_vix_spy_proxy", _boom)
    r = m.classify_regime()
    assert r["vix"] == 15.68 and r["vix_source"] == "cboe" and r["regime"] == "calm"
    assert "PROXY" not in r["reason"]


def test_a_hung_yfinance_cannot_stall_the_run(monkeypatch):
    monkeypatch.setattr(m, "_YF_TIMEOUT_SEC", 0.2)
    monkeypatch.setattr(m, "_fetch_vix_cboe", lambda: None)
    monkeypatch.setattr(m, "_fetch_vix_yfinance", lambda: time.sleep(30))
    monkeypatch.setattr(m, "_fetch_vix_spy_proxy", lambda: 10.36)
    t = time.time()
    r = m.classify_regime()
    assert time.time() - t < 3.0
    assert r["vix"] == 10.36 and r["vix_source"] == "spy_hv_proxy"


def test_a_proxy_read_says_so(monkeypatch):
    monkeypatch.setattr(m, "_fetch_vix_cboe", lambda: None)
    monkeypatch.setattr(m, "_fetch_vix_yfinance_bounded", lambda: None)
    monkeypatch.setattr(m, "_fetch_vix_spy_proxy", lambda: 10.36)
    assert "PROXY" in m.classify_regime()["reason"]


def test_total_blackout_is_still_unknown_half_sizing(monkeypatch):
    for f in ("_fetch_vix_cboe", "_fetch_vix_yfinance_bounded", "_fetch_vix_spy_proxy"):
        monkeypatch.setattr(m, f, lambda: None)
    r = m.classify_regime()
    assert r["regime"] == "unknown" and r["qty_multiplier"] == 0.5
    assert r["vix"] is None and r["vix_source"] is None


def test_an_explicit_vix_is_labeled_provided_and_skips_the_network(monkeypatch):
    monkeypatch.setattr(m, "get_vix", _boom)
    r = m.classify_regime(30.0)
    assert r["vix_source"] == "provided" and r["regime"] == "caution"


def test_cboe_rejects_garbage_prices(monkeypatch):
    class R:
        status_code = 200
        def __init__(self, v): self.v = v
        def json(self): return {"data": {"current_price": self.v}}
    import requests
    for bad in (0, -3, 0.5, 400):
        monkeypatch.setattr(requests, "get", lambda *a, _b=bad, **k: R(_b))
        assert m._fetch_vix_cboe() is None
    monkeypatch.setattr(requests, "get", lambda *a, **k: R(15.68))
    assert m._fetch_vix_cboe() == 15.68


# ── FRED previous close (2026-11-20): CBOE began answering the server with 403 ──

def test_the_previous_close_is_used_before_the_proxy(monkeypatch):
    monkeypatch.setattr(m, "_fetch_vix_cboe", lambda: None)
    monkeypatch.setattr(m, "_fetch_vix_yfinance_bounded", lambda: None)
    monkeypatch.setattr(m, "_fetch_vix_fred", lambda: 36.2)
    monkeypatch.setattr(m, "_fetch_vix_spy_proxy", _boom)
    r = m.classify_regime()
    assert r["vix"] == 36.2 and r["vix_source"] == "fred_prev_close"
    assert r["regime"] == "paused" and r["allow_new_opens"] is False
    assert "previous close" in r["reason"] and "PROXY" not in r["reason"]


def test_fred_csv_parsing_takes_the_newest_real_observation():
    import datetime
    today = datetime.date(2026, 11, 20)
    csv = "observation_date,VIXCLS\n2026-11-17,24.1\n2026-11-18,.\n2026-11-19,28.44\n"
    assert m._parse_fred_vix(csv, today) == 28.44
    # nothing usable, a stale series, and a garbage value are all refused
    assert m._parse_fred_vix("observation_date,VIXCLS\n2026-11-19,.\n", today) is None
    assert m._parse_fred_vix("observation_date,VIXCLS\n2026-11-10,19.0\n", today) is None
    assert m._parse_fred_vix("observation_date,VIXCLS\n2026-11-19,0.0\n", today) is None
    assert m._parse_fred_vix("", today) is None
