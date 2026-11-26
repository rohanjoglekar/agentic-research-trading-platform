"""Unit tests for trading/covered_call_runner.py — runs without network or API keys.

Validates the thesis-tiered covered-call leg:
  - effective (premium-adjusted) cost basis math
  - coverable-lot discovery (stock vs. short-call cover vs. resting sell order)
  - posture-tiered strike selection (intact / weakening / broken)
  - the cost-basis FLOOR (never breached for intact/weakening; LIFTED for broken)
  - profit-take buy-to-close management
  - mode gating (off / advisory / full_execute)

The agent path is disabled here (no anthropic install / no key), so every pick
is the deterministic baseline. Run:

    PYTHONPATH=. .venv/bin/python tests/test_covered_call_runner.py
"""
import datetime

import pandas as pd
import pytest

import trading.covered_call_runner as cc

# Hard-disable the LLM override so choose_cc stays purely deterministic offline.
cc.COVERED_CALLS["agent"]["enabled"] = False

_passed = 0


def ok(name, cond):
    global _passed
    print(("PASS " if cond else "FAIL ") + name)
    assert cond, name
    _passed += 1


# ─── Fixtures ─────────────────────────────────────────────────────────────────

CURRENT = 70.38
EXP = (datetime.date.today() + datetime.timedelta(days=35)).isoformat()

# (strike, premium, delta).  bid/ask = premium ∓ 0.05, OI 500, vol 300.
#   70    ITM (below current)            → invalid, never a candidate
#   71    0.88% OTM, max premium         → broken pick (below basis 71.43)
#   72    delta .42                      → weakening "meeting 2%" but wrong delta
#   73    delta .30, ~2.07%/mo           → weakening pick
#   75    delta .20                      → intact pick (hug low delta)
#   77.5  delta .12                      → valid but low income
#   80    0.30%/mo + wide spread         → invalid ("too thin"/"Wide"), excluded
_STRIKES = [
    (70.0, 3.2, 0.62),
    (71.0, 2.6, 0.52),
    (72.0, 2.2, 0.42),
    (73.0, 1.7, 0.30),
    (75.0, 1.0, 0.20),
    (77.5, 0.55, 0.12),
    (80.0, 0.25, 0.07),
]


def _chain_df():
    rows = []
    for strike, prem, delta in _STRIKES:
        rows.append({
            "strike": strike,
            "bid": round(prem - 0.05, 2),
            "ask": round(prem + 0.05, 2),
            "lastPrice": prem,
            "volume": 300,
            "openInterest": 500,
            "impliedVolatility": 0.45,
            "delta": delta,
            "inTheMoney": strike < CURRENT,
        })
    return pd.DataFrame(rows)


class FakeClient:
    """Stands in for AlpacaOptionsClient — one expiration, one calls chain."""

    def get_options_expirations(self, symbol):
        return [EXP]

    def get_options_chain(self, symbol, expiration):
        return {"calls": _chain_df(), "puts": pd.DataFrame()}

    def get_current_price(self, symbol):
        return CURRENT


class FakeTrader:
    def __init__(self, positions=None, open_orders=None):
        self._positions = positions or []
        self._open_orders = open_orders or []
        self.submitted = []

    def list_positions(self):
        return self._positions

    def list_open_orders(self):
        return self._open_orders

    def submit_option_limit(self, occ_symbol, qty, side, limit_price, time_in_force="day"):
        self.submitted.append({
            "occ_symbol": occ_symbol, "qty": qty, "side": side,
            "limit_price": limit_price, "time_in_force": time_in_force,
        })
        return {"id": "fake-order-1", "status": "accepted"}


def make_lot(eff_basis=71.43, current=CURRENT, shares=100):
    return {
        "symbol": "ACME", "shares": shares, "raw_basis": eff_basis,
        "effective_basis": eff_basis, "current_price": current,
        "covered_shares": 0, "uncovered_lots": shares // 100,
    }


OCC_CALL = "ACME260618C00075000"  # a valid short-call OCC (covers 100 shares)


def _stock_pos(qty=100, avg=71.43, cur=CURRENT, sym="ACME"):
    return {"symbol": sym, "qty": qty, "avg_entry_price": avg, "current_price": cur}


def _short_call_pos(occ=OCC_CALL, qty=-1, avg=2.0, cur=0.6):
    return {"symbol": occ, "qty": qty, "avg_entry_price": avg, "current_price": cur}


@pytest.fixture(autouse=True)
def _offline_cc(monkeypatch):
    """Wire the network-free fakes for every test — the same overrides the
    __main__ runner applies, but for pytest (which never executes __main__).
    run_covered_calls builds a real AlpacaOptionsClient and calls the real
    thesis_posture internally; without these it would hit the network and the
    advisory/full_execute mode-gating test would see 0 opens. monkeypatch
    auto-undoes after each test, and posture is forced 'weakening' (the
    deterministic tier the mode-gating test asserts, strike 73). Tests that
    pass posture/FakeClient explicitly are unaffected."""
    monkeypatch.setattr(cc, "thesis_posture",
                        lambda s: ("weakening", "test override"))
    import data.alpaca_options_client as aoc
    monkeypatch.setattr(aoc, "AlpacaOptionsClient",
                        lambda *a, **k: FakeClient())
    orig_mode = cc.COVERED_CALLS.get("mode")
    yield
    cc.COVERED_CALLS["mode"] = orig_mode      # restore the mode the test mutated


# ─── Tests ────────────────────────────────────────────────────────────────────

def test_effective_cost_basis():
    # Only FILLED orders bank premium toward the effective-cost-basis floor
    # (unfilled/resting orders must not deflate it) — records carry status.
    state = {
        "orders": [{"symbol": "ACME", "filled_avg_price": 1.50, "qty": 1, "status": "filled"}],   # CSP credit +150
        "cc_orders": [{"symbol": "ACME", "limit_price": 0.80, "qty": 1, "status": "filled"}],      # CC credit  + 80
        "closes": [{"ticker": "ACME", "filled_avg_price": 0.30, "qty": 1, "status": "filled"}],    # BTC debit  - 30
    }
    eff = cc.effective_cost_basis("ACME", raw_basis=73.00, shares=100, state=state)
    ok("eff basis = raw - net premium/share", abs(eff - 71.00) < 1e-6)

    other = cc.effective_cost_basis("ACME", raw_basis=73.00, shares=100, state={})
    ok("eff basis = raw when no premium recorded", abs(other - 73.00) < 1e-6)


def test_find_coverable_lots():
    # stock only → 1 lot
    t = FakeTrader(positions=[_stock_pos(100)])
    lots = cc.find_coverable_lots(t, {})
    ok("lots: 100 shares → 1 coverable lot", len(lots) == 1 and lots[0]["uncovered_lots"] == 1)
    ok("lots: effective_basis present", "effective_basis" in lots[0])

    # stock fully covered by held short call → 0 lots
    t2 = FakeTrader(positions=[_stock_pos(100), _short_call_pos()])
    ok("lots: 100 shares + 1 short call → 0", len(cc.find_coverable_lots(t2, {})) == 0)

    # 200 shares, 1 short call covers 100 → 1 uncovered lot
    t3 = FakeTrader(positions=[_stock_pos(200), _short_call_pos()])
    lots3 = cc.find_coverable_lots(t3, {})
    ok("lots: 200 shares + 1 call → 1 uncovered lot",
       len(lots3) == 1 and lots3[0]["uncovered_lots"] == 1)

    # resting sell-to-open call order also counts as cover
    t4 = FakeTrader(positions=[_stock_pos(100)],
                    open_orders=[{"symbol": OCC_CALL, "side": "sell", "qty": 1}])
    ok("lots: resting sell-call order covers shares → 0",
       len(cc.find_coverable_lots(t4, {})) == 0)

    # sub-100-share lot is ignored
    t5 = FakeTrader(positions=[_stock_pos(80)])
    ok("lots: 80 shares → no lot", len(cc.find_coverable_lots(t5, {})) == 0)


def test_choose_cc_intact():
    pick, used = cc.choose_cc(FakeClient(), "ACME", make_lot(), "intact", use_agent=False)
    ok("intact: picks delta-0.20 strike 75", pick is not None and pick["strike"] == 75.0)
    ok("intact: engine=rules", pick["engine"] == "rules")
    ok("intact: did not use agent", used is False)
    ok("intact: strike >= effective basis", pick["strike"] >= make_lot()["effective_basis"])


def test_choose_cc_weakening():
    pick, _ = cc.choose_cc(FakeClient(), "ACME", make_lot(), "weakening", use_agent=False)
    ok("weakening: hits ~2%/mo at delta-0.30 strike 73", pick["strike"] == 73.0)
    ok("weakening: meets 2% target", pick["monthly_return"] >= cc.COVERED_CALLS["target_monthly_return"])
    ok("weakening: strike >= effective basis", pick["strike"] >= make_lot()["effective_basis"])


def test_choose_cc_broken_lifts_floor():
    lot = make_lot(eff_basis=71.43)
    pick, _ = cc.choose_cc(FakeClient(), "ACME", lot, "broken", use_agent=False)
    ok("broken: max-premium near-ATM strike 71", pick["strike"] == 71.0)
    ok("broken: floor LIFTED — strike below effective basis",
       pick["strike"] < lot["effective_basis"])


def test_floor_blocks_when_basis_above_chain():
    # effective basis above every OTM strike → intact/weakening find nothing.
    lot = make_lot(eff_basis=90.0)
    pick_i, _ = cc.choose_cc(FakeClient(), "ACME", lot, "intact", use_agent=False)
    pick_w, _ = cc.choose_cc(FakeClient(), "ACME", lot, "weakening", use_agent=False)
    ok("floor: intact returns None when basis above chain", pick_i is None)
    ok("floor: weakening returns None when basis above chain", pick_w is None)
    # broken ignores the basis floor, so it still finds a strike.
    pick_b, _ = cc.choose_cc(FakeClient(), "ACME", lot, "broken", use_agent=False)
    ok("floor: broken still finds a strike (floor lifted)", pick_b is not None)


def test_manage_short_calls_profit_take():
    # captured 70% ≥ 65% → close (dry, execute=False)
    t = FakeTrader(positions=[_short_call_pos(avg=2.0, cur=0.6)])
    closed = cc.manage_short_calls(t, {}, execute=False)
    ok("BTC: 70% captured → closed", len(closed) == 1 and closed[0]["dry_run"] is True)

    # captured 50% < 65% → leave it
    t2 = FakeTrader(positions=[_short_call_pos(avg=2.0, cur=1.0)])
    ok("BTC: 50% captured → not closed", len(cc.manage_short_calls(t2, {}, execute=False)) == 0)

    # a long call (qty>0) is never treated as a short to buy back
    t3 = FakeTrader(positions=[_short_call_pos(qty=1, avg=2.0, cur=0.6)])
    ok("BTC: long call ignored", len(cc.manage_short_calls(t3, {}, execute=False)) == 0)


def test_manage_short_calls_executes_order():
    t = FakeTrader(positions=[_short_call_pos(avg=2.0, cur=0.6)])
    state = {}
    closed = cc.manage_short_calls(t, state, execute=True)
    ok("BTC exec: order submitted", len(t.submitted) == 1 and t.submitted[0]["side"] == "buy")
    ok("BTC exec: recorded in state['closes']", len(state.get("closes", [])) == 1)
    ok("BTC exec: rec has alpaca id", closed[0].get("alpaca_order_id") == "fake-order-1")


def test_run_covered_calls_mode_gating():
    state = {}
    trader = FakeTrader(positions=[_stock_pos(100, avg=71.43)])

    # off → no-op
    cc.COVERED_CALLS["mode"] = "off"
    res_off = cc.run_covered_calls(trader, state)
    ok("mode off: nothing opened", res_off["opened"] == [] and trader.submitted == [])

    # advisory → computes a pick but does NOT submit
    cc.COVERED_CALLS["mode"] = "advisory"
    trader2 = FakeTrader(positions=[_stock_pos(100, avg=71.43)])
    res_adv = cc.run_covered_calls(trader2, {})
    ok("mode advisory: one dry pick", len(res_adv["opened"]) == 1 and res_adv["opened"][0].get("dry_run") is True)
    ok("mode advisory: no order submitted", trader2.submitted == [])
    ok("mode advisory: weakening posture → strike 73", res_adv["opened"][0]["strike"] == 73.0)

    # full_execute → submits a real (paper) sell order
    cc.COVERED_CALLS["mode"] = "full_execute"
    trader3 = FakeTrader(positions=[_stock_pos(100, avg=71.43)])
    state3 = {}
    res_full = cc.run_covered_calls(trader3, state3)
    ok("mode full_execute: order submitted", len(trader3.submitted) == 1)
    ok("mode full_execute: sell side", trader3.submitted[0]["side"] == "sell")
    ok("mode full_execute: qty = 1 lot", trader3.submitted[0]["qty"] == 1)
    ok("mode full_execute: recorded in state['cc_orders']", len(state3.get("cc_orders", [])) == 1)


# ─── Gemini-first provider swap (network-free) ────────────────────────────────
# The agentic strike RE-PICK swaps its TEXT GENERATOR via COVERED_CALLS_LLM_PROVIDER
# (default gemini) but keeps the SAME STRIKE/CONFIDENCE parser + the in-candidate-set
# + confidence-floor guardrails. These tests monkeypatch the two text generators
# (no network / no API keys) and assert: (a) default provider=gemini is used,
# (b) a gemini None or out-of-set/parse-fail reply falls back to the Claude path,
# (c) the output shape/keys are unchanged and the vetted-list + floor still bind.
#
# WEAKENING candidate set for make_lot() (eff basis 71.43, price 70.38) is
# [72, 73, 75, 77.5]; baseline pick = 73. So 75 is an in-set override and 999 is
# out-of-set. min_confidence is 0.55.

_GEM = "agents.llm_gemini.gemini_generate"


def _reply(strike, conf=0.9):
    return f"STRIKE: {strike}\nCONFIDENCE: {conf}"


def _enable_agent(monkeypatch):
    """Re-enable the agent (module import hard-disables it) for the duration of a
    test; monkeypatch auto-restores. thesis_posture is already forced 'weakening'
    by the autouse fixture."""
    monkeypatch.setitem(cc.COVERED_CALLS["agent"], "enabled", True)


def test_agent_pick_defaults_to_gemini(monkeypatch):
    _enable_agent(monkeypatch)
    monkeypatch.delenv("COVERED_CALLS_LLM_PROVIDER", raising=False)  # → default 'gemini'
    calls = {"gemini": 0, "claude": 0}

    def fake_gemini(prompt, **kw):
        calls["gemini"] += 1
        ok("gemini: web-search grounding on (matches Claude web_search)", kw.get("use_search") is True)
        return _reply(75)  # a valid in-set override (baseline is 73)

    def boom_claude(prompt, cfg, symbol):
        calls["claude"] += 1
        raise AssertionError("Claude must not be called when gemini succeeds")

    monkeypatch.setattr(_GEM, fake_gemini)
    monkeypatch.setattr(cc, "_claude_pick_text", boom_claude)

    pick, used = cc.choose_cc(FakeClient(), "ACME", make_lot(), "weakening", use_agent=True)
    ok("gemini default: provider is gemini", calls["gemini"] == 1 and calls["claude"] == 0)
    ok("gemini default: agent engine used", used is True and pick["engine"] == "agent")
    ok("gemini default: recorded provider tag", pick.get("agent_provider") == "gemini")
    ok("gemini default: override picked in-set strike 75", pick["strike"] == 75.0)
    ok("gemini default: strike >= effective basis (floor holds)",
       pick["strike"] >= make_lot()["effective_basis"])
    ok("gemini default: shape has agent_confidence", "agent_confidence" in pick)


def test_agent_pick_gemini_none_falls_back_to_claude(monkeypatch):
    _enable_agent(monkeypatch)
    monkeypatch.delenv("COVERED_CALLS_LLM_PROVIDER", raising=False)

    monkeypatch.setattr(_GEM, lambda prompt, **kw: None)  # gemini unavailable
    monkeypatch.setattr(cc, "_claude_pick_text",
                        lambda prompt, cfg, symbol: _reply(75))  # Claude succeeds

    pick, used = cc.choose_cc(FakeClient(), "ACME", make_lot(), "weakening", use_agent=True)
    ok("gemini None → Claude fallback: engine=agent", pick["engine"] == "agent")
    ok("gemini None → Claude fallback: provider=anthropic", pick.get("agent_provider") == "anthropic")
    ok("gemini None → Claude fallback: in-set strike 75", pick["strike"] == 75.0)
    ok("gemini None → Claude fallback: floor holds", pick["strike"] >= make_lot()["effective_basis"])


def test_agent_pick_gemini_out_of_set_falls_back_to_claude(monkeypatch):
    _enable_agent(monkeypatch)
    monkeypatch.delenv("COVERED_CALLS_LLM_PROVIDER", raising=False)

    # Gemini names a strike that is NOT in the vetted candidate set → rejected,
    # so it must fall back to Claude rather than pick the out-of-set strike.
    monkeypatch.setattr(_GEM, lambda prompt, **kw: _reply(999))
    monkeypatch.setattr(cc, "_claude_pick_text",
                        lambda prompt, cfg, symbol: _reply(75))

    pick, used = cc.choose_cc(FakeClient(), "ACME", make_lot(), "weakening", use_agent=True)
    ok("gemini out-of-set → Claude fallback: provider=anthropic",
       pick.get("agent_provider") == "anthropic")
    ok("gemini out-of-set → Claude fallback: picked in-set 75 (never 999)", pick["strike"] == 75.0)
    ok("gemini out-of-set: 999 never selected", pick["strike"] != 999.0)


def test_agent_pick_both_fail_keeps_baseline(monkeypatch):
    _enable_agent(monkeypatch)
    monkeypatch.delenv("COVERED_CALLS_LLM_PROVIDER", raising=False)

    # Gemini out-of-set AND Claude garbage → both reject → deterministic baseline.
    monkeypatch.setattr(_GEM, lambda prompt, **kw: _reply(999))
    monkeypatch.setattr(cc, "_claude_pick_text",
                        lambda prompt, cfg, symbol: "no strike here")

    pick, used = cc.choose_cc(FakeClient(), "ACME", make_lot(), "weakening", use_agent=True)
    ok("both fail: baseline stands (engine=rules)", pick["engine"] == "rules")
    ok("both fail: deterministic weakening baseline strike 73", pick["strike"] == 73.0)
    ok("both fail: no agent_provider tag on baseline", "agent_provider" not in pick)
    ok("both fail: floor still holds", pick["strike"] >= make_lot()["effective_basis"])


def test_agent_pick_provider_anthropic_skips_gemini(monkeypatch):
    _enable_agent(monkeypatch)
    monkeypatch.setenv("COVERED_CALLS_LLM_PROVIDER", "anthropic")

    def boom_gemini(prompt, **kw):
        raise AssertionError("Gemini must not be called when provider=anthropic")

    monkeypatch.setattr(_GEM, boom_gemini)
    monkeypatch.setattr(cc, "_claude_pick_text", lambda prompt, cfg, symbol: _reply(75))

    pick, _ = cc.choose_cc(FakeClient(), "ACME", make_lot(), "weakening", use_agent=True)
    ok("provider=anthropic: gemini skipped, Claude used", pick.get("agent_provider") == "anthropic")
    ok("provider=anthropic: in-set strike 75", pick["strike"] == 75.0)


def test_agent_pick_low_confidence_keeps_baseline_no_fallback(monkeypatch):
    _enable_agent(monkeypatch)
    monkeypatch.delenv("COVERED_CALLS_LLM_PROVIDER", raising=False)

    # A VALID reply below the confidence floor is a real "keep baseline" decision,
    # NOT a failure — it must NOT fall through to Claude.
    monkeypatch.setattr(_GEM, lambda prompt, **kw: _reply(75, conf=0.10))

    def boom_claude(prompt, cfg, symbol):
        raise AssertionError("low-confidence keep-baseline must not trigger fallback")

    monkeypatch.setattr(cc, "_claude_pick_text", boom_claude)

    pick, used = cc.choose_cc(FakeClient(), "ACME", make_lot(), "weakening", use_agent=True)
    ok("low-conf: baseline kept (engine=rules)", pick["engine"] == "rules")
    ok("low-conf: weakening baseline strike 73", pick["strike"] == 73.0)


if __name__ == "__main__":
    # Force a deterministic neutral posture + patch the options client into the
    # runner so run_covered_calls never touches the network.
    cc.thesis_posture = lambda t: ("weakening", "test override")
    import data.alpaca_options_client as aoc
    aoc.AlpacaOptionsClient = lambda *a, **k: FakeClient()

    test_effective_cost_basis()
    test_find_coverable_lots()
    test_choose_cc_intact()
    test_choose_cc_weakening()
    test_choose_cc_broken_lifts_floor()
    test_floor_blocks_when_basis_above_chain()
    test_manage_short_calls_profit_take()
    test_manage_short_calls_executes_order()
    test_run_covered_calls_mode_gating()

    # Gemini-first provider tests need a MonkeyPatch + the autouse fixture's
    # network-free overrides (thesis_posture is already patched above; the agent
    # tests call choose_cc with FakeClient directly so no AlpacaOptionsClient is hit).
    import pytest as _pytest
    for _t in (test_agent_pick_defaults_to_gemini,
               test_agent_pick_gemini_none_falls_back_to_claude,
               test_agent_pick_gemini_out_of_set_falls_back_to_claude,
               test_agent_pick_both_fail_keeps_baseline,
               test_agent_pick_provider_anthropic_skips_gemini,
               test_agent_pick_low_confidence_keeps_baseline_no_fallback):
        with _pytest.MonkeyPatch.context() as _mp:
            _t(_mp)

    print(f"\n{_passed} checks passed")

    print(f"\n{_passed} checks passed")
