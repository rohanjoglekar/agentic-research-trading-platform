"""Unit tests for the two BLOCK-ONLY safety gates on the wheel runner.

Gates under test (trading/wheel_runner.py, added 2026-07-07):
  1. BUYING-POWER PREFLIGHT — before each CSP open, required collateral
     (strike×100×qty) must fit inside the broker's OPTIONS buying power minus a
     small buffer. Over-budget orders are SKIPPED and counted `risk_blocked` —
     never submitted. A running BP tally decrements as orders place so multiple
     orders in one run can't oversubscribe.
  2. FRESHNESS GATE — if a REQUIRED feed (grouped scan / research cache) is
     stale, the OPEN step DEGRADES to compute-but-don't-place (dry_run path) and
     `freshness_blocked` is set to the stale feed name. Closes/rolls are NEVER
     freshness-blocked.

Both gates are FAIL-SAFE: a gate that raises blocks the order (skip / degrade)
and records `gate_error` — it never crashes the run or falls through to placing.

No network, no real Alpaca — everything is monkeypatched. Run:

    PYTHONPATH=. .venv/bin/python -m pytest tests/test_wheel_safety_gates.py -x -q
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import trading.wheel_runner as w


@pytest.fixture(autouse=True)
def _isolate_run_summary(tmp_path, monkeypatch):
    """Redirect the run-summary JSONL to a tmp file so tests never pollute the
    real data/wheel_run_summary.jsonl the forge auditor reads."""
    monkeypatch.setenv("WHEEL_RUN_SUMMARY_PATH",
                       str(tmp_path / "wheel_run_summary.jsonl"))


# ─── Fakes ───────────────────────────────────────────────────────────────────

class FakeTrader:
    """Stands in for AlpacaTrader — no network. Records every submit."""

    def __init__(self, account=None, positions=None, open_orders=None,
                 reject_on=None, raise_on=None):
        self._account = account or {
            "equity": "100000", "cash": "100000",
            "options_buying_power": "100000", "buying_power": "100000",
        }
        self._positions = positions or []
        self._open_orders = open_orders or []
        self.submitted = []               # every submit_option_limit call
        self._reject_on = reject_on or set()   # OCC symbols the broker 4xx-rejects
        self._raise_on = raise_on or set()     # OCC symbols that raise a non-broker error

    # account / clock / positions
    def account(self):
        return dict(self._account)

    def clock(self):
        return {"is_open": True}

    def list_positions(self):
        return list(self._positions)

    def list_open_orders(self):
        return list(self._open_orders)

    def list_recent_orders(self, status="all", limit=100):
        return []

    def _get(self, path, params=None):
        return {}

    # orders
    def submit_option_limit(self, occ_symbol, qty, side, limit_price,
                            time_in_force="day"):
        self.submitted.append({"occ": occ_symbol, "qty": qty, "side": side,
                               "limit_price": limit_price})
        if occ_symbol in self._raise_on:
            raise TimeoutError("connection timed out")     # unexpected error
        if occ_symbol in self._reject_on:
            raise RuntimeError(
                f"Alpaca 403 on POST /v2/orders: insufficient options buying power")
        return {"id": f"ord-{len(self.submitted)}", "status": "accepted"}


def _calm_regime():
    return {"regime": "calm", "reason": "test", "vix": 15.0,
            "allow_new_opens": True, "force_close_speculative": False,
            "qty_multiplier": 1.0}


def _install_common(monkeypatch, trader, grouped, freshness=(False, None)):
    """Wire the module so run_wheel uses our fakes and never touches disk/net."""
    monkeypatch.setattr(w, "AlpacaTrader", lambda *a, **k: trader)
    monkeypatch.setattr(w, "classify_regime", _calm_regime)
    monkeypatch.setattr(w, "load_state", lambda: {"orders": [], "closes": []})
    monkeypatch.setattr(w, "save_state", lambda *a, **k: None)
    monkeypatch.setattr(w, "reconcile_order_statuses", lambda *a, **k: {"changed": 0, "total": 0})
    monkeypatch.setattr(w, "notify_discord", lambda *a, **k: None)
    monkeypatch.setattr(w, "_notify_summary", lambda *a, **k: None)
    monkeypatch.setattr(w, "manage_open_positions", lambda *a, **k: [])
    # No covered-call leg noise (mode "off" skips run_covered_calls entirely).
    monkeypatch.setitem(w.COVERED_CALLS, "enabled", False)
    monkeypatch.setitem(w.COVERED_CALLS, "mode", "off")
    # GROUPED_PATH must "exist"; feed the grouped scan directly.
    class _FakePath:
        def exists(self_):
            return True

        def stat(self_):
            import types
            return types.SimpleNamespace(st_mtime=__import__("time").time())

        def __fspath__(self_):
            return "/tmp/fake_grouped.json"
    monkeypatch.setattr(w, "GROUPED_PATH", _FakePath())
    # Freshness gate → deterministic result for the test.
    monkeypatch.setattr(w, "_check_required_feed_freshness", lambda: freshness)
    # `open(GROUPED_PATH)` reads our grouped list.
    import json as _json
    real_open = open

    def _fake_open(path, *a, **k):
        import io
        return io.StringIO(_json.dumps(grouped))
    monkeypatch.setattr("builtins.open", _fake_open)
    # POP calibration import must be a no-op.
    monkeypatch.setitem(sys.modules, "data.pop_calibration",
                        type(sys)("data.pop_calibration"))
    sys.modules["data.pop_calibration"].compute_bias = lambda: {}


def _stock(sym, strike, rating="A", premium=1.50, sector="Tech"):
    exp = "2026-08-21"
    return {
        "symbol": sym, "best_rating": rating, "best_score": 80, "sector": sector,
        "strikes": [{"Strike": strike, "Expiration": exp, "Premium": premium,
                     "POP": 0.75}],
    }


# ─── (a) BP gate skips an over-collateral order → risk_blocked, not submitted ──

def test_bp_gate_blocks_over_collateral_order(monkeypatch):
    # Options BP is tiny ($558, audit's PYPL case). A $70 strike needs
    # $70×100=$7,000 collateral — must be blocked pre-submit.
    trader = FakeTrader(account={
        "equity": "100000", "cash": "100000",
        "options_buying_power": "558", "buying_power": "100000",
    })
    grouped = [_stock("PYPL", 70.0, rating="C")]   # C → 1 contract
    _install_common(monkeypatch, trader, grouped)

    res = w.run_wheel(dry_run=False)

    assert res["risk_blocked"] == 1, res
    assert res["placed"] == 0
    assert res["broker_rejected"] == 0
    assert res["unexpected_errors"] == 0
    # The order must NOT have been submitted to the broker.
    assert trader.submitted == [], "BP-blocked order must never POST"


# ─── (b) BP tally decrements across orders — 2nd blocked after 1st consumes BP ─

def test_bp_tally_decrements_across_orders(monkeypatch):
    # BP = $8,000 (buffer $50 → $7,950 usable). Two $70 strikes each need $7,000.
    # First fits (7000 ≤ 7950); after it places, 950 remains → second is blocked.
    trader = FakeTrader(account={
        "equity": "100000", "cash": "100000",
        "options_buying_power": "8000", "buying_power": "100000",
    })
    grouped = [_stock("AAA", 70.0, rating="C"), _stock("BBB", 70.0, rating="C")]
    _install_common(monkeypatch, trader, grouped)

    res = w.run_wheel(dry_run=False)

    assert res["placed"] == 1, res
    assert res["risk_blocked"] == 1, res
    # Exactly one order reached the broker (the first); the second was pre-blocked.
    assert len(trader.submitted) == 1
    assert trader.submitted[0]["occ"].startswith("AAA")


# ─── (c) stale required feed degrades OPEN to no-submit, freshness_blocked set ─

def test_stale_feed_degrades_open_to_no_submit(monkeypatch):
    trader = FakeTrader()   # plenty of BP
    grouped = [_stock("AAA", 20.0, rating="A")]
    # Freshness gate reports the scan feed as stale.
    _install_common(monkeypatch, trader, grouped,
                    freshness=(True, "scan (30.0h > 20h)"))

    res = w.run_wheel(dry_run=False)

    assert res["freshness_blocked"] == "scan (30.0h > 20h)", res
    assert res["placed"] == 0, "stale feed must block all opens"
    # OPEN degraded to compute-but-don't-place → nothing POSTed.
    assert trader.submitted == []


def test_fresh_feed_allows_opens(monkeypatch):
    # Control: with fresh feeds and ample BP the same order DOES place, proving
    # the gates are block-only (they don't suppress legitimate opens).
    trader = FakeTrader()
    grouped = [_stock("AAA", 20.0, rating="A")]
    _install_common(monkeypatch, trader, grouped, freshness=(False, None))

    res = w.run_wheel(dry_run=False)

    assert res["freshness_blocked"] in (None, False), res
    assert res["placed"] >= 1, res
    assert len(trader.submitted) >= 1


# ─── (d) a gate that raises is caught → blocks (fail-safe), gate_error set ─────

def test_freshness_gate_exception_blocks_fail_safe(monkeypatch):
    trader = FakeTrader()
    grouped = [_stock("AAA", 20.0, rating="A")]
    _install_common(monkeypatch, trader, grouped)

    def _boom():
        raise ValueError("simulated freshness gate failure")
    monkeypatch.setattr(w, "_check_required_feed_freshness", _boom)

    res = w.run_wheel(dry_run=False)   # must NOT crash

    assert res["gate_error"] is not None and "freshness_gate" in res["gate_error"], res
    assert res["placed"] == 0, "gate error must block opens (fail-safe)"
    assert trader.submitted == []


def test_bp_gate_exception_blocks_fail_safe(monkeypatch):
    # If reading options BP raises, available BP → 0, all opens blocked, gate_error set.
    trader = FakeTrader()
    grouped = [_stock("AAA", 20.0, rating="A")]
    _install_common(monkeypatch, trader, grouped)

    def _boom(account, equity):
        raise KeyError("simulated BP read failure")
    monkeypatch.setattr(w, "_available_options_bp", _boom)

    res = w.run_wheel(dry_run=False)   # must NOT crash

    assert res["gate_error"] is not None and "bp_gate" in res["gate_error"], res
    assert res["placed"] == 0, "BP gate error must block opens (fail-safe)"
    assert trader.submitted == []


# ─── (e) closes/rolls are NOT freshness-blocked ───────────────────────────────

def test_closes_not_freshness_blocked(monkeypatch):
    # A stale feed blocks OPENS but management (closes) must still run.
    trader = FakeTrader()
    grouped = [_stock("AAA", 20.0, rating="A")]
    _install_common(monkeypatch, trader, grouped,
                    freshness=(True, "research_cache (60.0h > 48h)"))

    # manage_open_positions returns a close regardless of feed freshness.
    close_rec = {"symbol": "ZZZ260821P00020000", "ticker": "ZZZ", "qty": 1,
                 "side": "buy", "reason": "profit-take"}
    monkeypatch.setattr(w, "manage_open_positions", lambda *a, **k: [close_rec])

    res = w.run_wheel(dry_run=False)

    # Freshness only gates OPENS.
    assert res["freshness_blocked"] == "research_cache (60.0h > 48h)", res
    assert res["placed"] == 0
    # The close was NOT suppressed by the freshness gate.
    assert res["closed"] == 1, "management/closes must run despite a stale feed"


# ─── broker_rejected vs unexpected_errors accounting ──────────────────────────

def test_broker_rejection_vs_unexpected_error_accounting(monkeypatch):
    # Two fresh, well-budgeted orders: one the broker 4xx-rejects, one that
    # raises a non-broker (timeout) error. They must be counted separately and
    # NOT counted as risk_blocked.
    trader = FakeTrader()
    aaa_occ = w.build_occ("AAA", "2026-08-21", "put", 20.0)
    bbb_occ = w.build_occ("BBB", "2026-08-21", "put", 20.0)
    trader._reject_on = {aaa_occ}
    trader._raise_on = {bbb_occ}
    grouped = [_stock("AAA", 20.0, rating="C"), _stock("BBB", 20.0, rating="C")]
    _install_common(monkeypatch, trader, grouped)

    res = w.run_wheel(dry_run=False)

    assert res["broker_rejected"] == 1, res
    assert res["unexpected_errors"] == 1, res
    assert res["risk_blocked"] == 0, res
    assert res["placed"] == 0, res
    # Both were actually POSTed (they failed at the broker, not pre-submit).
    assert len(trader.submitted) == 2


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-x", "-q"]))


# ─── committed collateral: the reserve is measured against FREE cash ─────────
# Broker `cash` still contains the money promised to open short puts, so a floor
# measured against raw cash counted it as free. On 2026-11-20 the wheel held
# $68,850 of puts against $50,702 of cash (4x margin) while logging $36,000
# "deployable"; the options-BP preflight then blocked 4-14 orders a day.

def _put(sym, strike, qty, yymmdd="261218"):
    return {"symbol": f"{sym}{yymmdd}P{int(strike * 1000):08d}", "qty": str(qty),
            "market_value": "-100", "current_price": "1.00"}


def test_committed_collateral_counts_short_puts_and_resting_put_sells():
    positions = [
        _put("AAA", 50.0, -2),                                    # $10,000
        {"symbol": "AAA261218C00060000", "qty": "-1"},            # short call: shares cover it
        _put("LNG", 40.0, 1),                                     # long put: not collateral
        {"symbol": "AAA", "qty": "100", "market_value": "5000"},  # assigned shares: cash already spent
    ]
    orders = [
        {"symbol": "BBB261218P00020000", "side": "sell", "qty": "3", "filled_qty": "1"},   # 2 left → $4,000
        {"symbol": "CCC261218P00030000", "side": "buy", "qty": "1"},                       # buy-to-close
        {"symbol": "DDD261218P00030000", "side": "sell", "qty": "1",
         "position_intent": "sell_to_close"},
        {"symbol": "EEE261218C00030000", "side": "sell", "qty": "1"},                      # covered call
    ]
    assert w.committed_put_collateral(positions, orders) == 14_000.0
    assert w.committed_put_collateral([], []) == 0.0


def test_an_unreadable_record_is_not_counted_as_zero():
    with pytest.raises((TypeError, ValueError)):
        w.committed_put_collateral([_put("AAA", 50.0, "garbage")], [])


def test_no_new_puts_when_open_puts_already_use_the_cash(monkeypatch):
    # $100K cash, $90K already promised to puts → $10K free, under the 18% floor.
    # The broker (margin) would still allow it; the reserve must not.
    trader = FakeTrader(account={"equity": "100000", "cash": "100000",
                                 "options_buying_power": "100000", "buying_power": "200000"},
                        positions=[_put("AAA", 90.0, -10)])
    _install_common(monkeypatch, trader, [_stock("CCC", 70.0, rating="C")])

    res = w.run_wheel(dry_run=False)

    assert res["placed"] == 0 and res["risk_blocked"] == 0, res
    assert trader.submitted == []


def test_free_cash_above_the_floor_still_opens(monkeypatch):
    # $20K promised → $80K free, comfortably above the floor: opens as before.
    trader = FakeTrader(account={"equity": "100000", "cash": "100000",
                                 "options_buying_power": "100000", "buying_power": "200000"},
                        positions=[_put("AAA", 20.0, -10)])
    _install_common(monkeypatch, trader, [_stock("CCC", 70.0, rating="C")])

    res = w.run_wheel(dry_run=False)

    assert res["placed"] == 1, res
    assert trader.submitted[0]["occ"].startswith("CCC")


def test_resting_put_orders_count_against_free_cash(monkeypatch):
    # No positions yet, but $85K of sell-put orders are resting → $15K free < floor.
    trader = FakeTrader(account={"equity": "100000", "cash": "100000",
                                 "options_buying_power": "100000", "buying_power": "200000"},
                        open_orders=[{"symbol": "AAA261218P00085000", "side": "sell",
                                      "qty": "10", "filled_qty": "0"}])
    _install_common(monkeypatch, trader, [_stock("CCC", 70.0, rating="C")])

    assert w.run_wheel(dry_run=False)["placed"] == 0
    assert trader.submitted == []


def test_collateral_count_failure_blocks_opens_fail_safe(monkeypatch):
    trader = FakeTrader(positions=[_put("AAA", 50.0, "garbage")])
    _install_common(monkeypatch, trader, [_stock("CCC", 70.0, rating="C")])

    res = w.run_wheel(dry_run=False)

    assert res["placed"] == 0 and trader.submitted == []
    assert "collateral" in (res.get("gate_error") or ""), res
