"""Logic tests for the agentic position manager.

Exercises the pure decision path (gray-zone gate, decision parsing, guardrails,
roll-target selection) with mock data — no live Anthropic/Alpaca calls.
"""
import datetime

import pandas as pd

from trading import agentic_manager as am


def _future(days: int) -> str:
    return (datetime.date.today() + datetime.timedelta(days=days)).isoformat()


def _pos(avg, cur, dte=30, strike=50.0, qty=-2, ticker="SOFI"):
    occ = f"{ticker}{_future(dte)[2:].replace('-','')}P{int(strike*1000):08d}"
    return {"symbol": occ, "qty": qty, "avg_entry_price": avg,
            "current_price": cur, "unrealized_pl": (avg - cur) * abs(qty) * 100}


REGIME = {"regime": "calm", "vix": 15.0}


# ─── build_observation ───────────────────────────────────────────────────────

def test_build_observation_basic():
    obs = am.build_observation(_pos(avg=1.00, cur=0.40, dte=30), REGIME)
    assert obs is not None
    assert obs["ticker"] == "SOFI"
    assert abs(obs["captured_pct"] - 0.60) < 1e-6     # 1 - 0.4/1.0
    assert abs(obs["loss_mult"] - 0.40) < 1e-6
    assert obs["dte"] == 30


def test_build_observation_skips_long_and_calls():
    assert am.build_observation(_pos(avg=1.0, cur=0.4, qty=2), REGIME) is None  # long
    # a call OCC should be skipped (this manager handles puts)
    call = _pos(avg=1.0, cur=0.4)
    call["symbol"] = call["symbol"].replace("P", "C", 1)
    assert am.build_observation(call, REGIME) is None


# ─── deterministic baseline (the outline / fallback) ─────────────────────────

def test_deterministic_profit_take():
    obs = am.build_observation(_pos(avg=1.00, cur=0.15), REGIME)  # captured 85%
    assert am.deterministic_decision(obs)["action"] == "CLOSE"


def test_deterministic_loss_rides_wheel_candidate():
    obs = am.build_observation(_pos(avg=1.00, cur=3.00), REGIME)  # 3x loss
    obs["would_own_score"] = 8                                    # quality → ride
    assert am.deterministic_decision(obs)["action"] == "RIDE"


def test_deterministic_loss_cuts_non_candidate():
    obs = am.build_observation(_pos(avg=1.00, cur=3.00), REGIME)
    obs["quality_tag"] = "SPECULATIVE"                           # bad tag → cut
    assert am.deterministic_decision(obs)["action"] == "CLOSE"


# ─── gray-zone gate ──────────────────────────────────────────────────────────

def test_boring_position_not_gray():
    obs = am.build_observation(_pos(avg=1.00, cur=0.85, dte=40), REGIME)  # captured 15%
    obs["days_to_earnings"] = None
    gray, reasons = am.in_gray_zone(obs)
    assert gray is False and reasons == []


def test_near_profit_is_gray():
    obs = am.build_observation(_pos(avg=1.00, cur=0.30, dte=40), REGIME)  # captured 70%
    gray, reasons = am.in_gray_zone(obs)
    assert gray is True


def test_stress_and_short_dte_are_gray():
    assert am.in_gray_zone(am.build_observation(_pos(1.0, 2.0, dte=40), REGIME))[0]   # 2x loss
    assert am.in_gray_zone(am.build_observation(_pos(1.0, 0.9, dte=5), REGIME))[0]    # 5 DTE


# ─── decision parsing ────────────────────────────────────────────────────────

def test_parse_full_decision():
    d = am._parse_decision(
        "DECISION: ROLL\nCONFIDENCE: 0.82\nROLL_OTM_PCT: 0.08\n"
        "RATIONALE: Earnings beat, thesis intact, push strike lower for safety."
    )
    assert d["action"] == "ROLL" and d["confidence"] == 0.82 and d["roll_otm_pct"] == 0.08


def test_parse_na_roll_and_garbage():
    d = am._parse_decision("DECISION: HOLD\nCONFIDENCE: 0.7\nROLL_OTM_PCT: NA\nRATIONALE: fine.")
    assert d["action"] == "HOLD" and d["roll_otm_pct"] is None
    assert am._parse_decision("the model rambled with no schema") is None


# ─── guardrails ──────────────────────────────────────────────────────────────

def test_kill_switch_overrides_agent_hold():
    obs = am.build_observation(_pos(avg=1.00, cur=4.50), REGIME)  # 4.5x ≥ kill switch
    agent = {"action": "HOLD", "confidence": 0.99, "roll_otm_pct": None, "rationale": "ride it"}
    final = am.apply_guardrails(obs, agent, {"action": "RIDE", "reason": "x"}, opt_client=None)
    assert final["action"] == "CLOSE" and final["source"] == "kill_switch"


def test_low_confidence_falls_back_to_baseline():
    obs = am.build_observation(_pos(avg=1.00, cur=0.30), REGIME)
    agent = {"action": "CLOSE", "confidence": 0.20, "roll_otm_pct": None, "rationale": "meh"}
    final = am.apply_guardrails(obs, agent, {"action": "HOLD", "reason": "baseline"}, opt_client=None)
    assert final["action"] == "HOLD" and final["source"] == "baseline_low_confidence"


def test_none_agent_falls_back():
    obs = am.build_observation(_pos(avg=1.00, cur=0.30), REGIME)
    final = am.apply_guardrails(obs, None, {"action": "HOLD", "reason": "baseline"}, opt_client=None)
    assert final["action"] == "HOLD" and final["source"] == "baseline_fallback"


# ─── roll-target selection ───────────────────────────────────────────────────

class _MockOptClient:
    """Returns a fixed chain so we can assert roll selection deterministically."""
    def __init__(self, price): self._price = price
    def get_current_price(self, t): return self._price
    def get_options_expirations(self, t): return [_future(7), _future(30), _future(90)]
    def get_options_chain(self, t, exp):
        # underlying 50; current strike 50. Further-OTM puts below 47.5 (5% bump).
        puts = pd.DataFrame([
            {"strike": 48.0, "bid": 1.20, "ask": 1.30, "openInterest": 800},  # not far enough OTM
            {"strike": 47.0, "bid": 0.95, "ask": 1.05, "openInterest": 900},  # qualifies, credit
            {"strike": 45.0, "bid": 0.55, "ask": 0.65, "openInterest": 700},  # further OTM, less credit
            {"strike": 44.0, "bid": 0.10, "ask": 0.20, "openInterest": 50},   # too thin OI
        ])
        return {"puts": puts, "calls": pd.DataFrame()}


def test_find_roll_target_picks_furthest_otm_with_credit():
    obs = am.build_observation(_pos(avg=1.00, cur=0.50, dte=30, strike=50.0), REGIME)
    obs["underlying_price"] = 50.0
    tgt = am.find_roll_target(obs, _MockOptClient(50.0))
    assert tgt is not None
    # 47 and 45 both clear the 5% OTM bump and net credit vs $0.50 close cost;
    # we prefer the furthest-OTM (safest) qualifying strike → 45.
    assert tgt["strike"] == 45.0
    assert tgt["credit_per_share"] > 0
    assert 21 <= tgt["dte"] <= 45   # picked the preferred-band expiration (~30d)


def test_roll_infeasible_falls_back():
    # current premium so high no roll pays a credit → guardrail degrades to baseline
    obs = am.build_observation(_pos(avg=1.00, cur=5.00, dte=30, strike=50.0), REGIME)
    obs["underlying_price"] = 50.0
    agent = {"action": "ROLL", "confidence": 0.9, "roll_otm_pct": 0.05, "rationale": "roll it"}
    # cur=5.0 also trips kill switch (5x), so kill-switch CLOSE wins — assert that path:
    final = am.apply_guardrails(obs, agent, {"action": "RIDE", "reason": "x"}, _MockOptClient(50.0))
    assert final["action"] == "CLOSE" and final["source"] == "kill_switch"


def test_roll_infeasible_below_killswitch_falls_back_to_baseline():
    obs = am.build_observation(_pos(avg=1.00, cur=3.00, dte=30, strike=50.0), REGIME)  # 3x, under kill
    obs["underlying_price"] = 50.0
    agent = {"action": "ROLL", "confidence": 0.9, "roll_otm_pct": 0.05, "rationale": "roll it"}
    # no put pays a $3.00+ credit → roll infeasible → baseline
    final = am.apply_guardrails(obs, agent, {"action": "RIDE", "reason": "wheel"}, _MockOptClient(50.0))
    assert final["source"] == "roll_infeasible" and final["action"] == "RIDE"


# ─── orchestration (manage_positions_agentic) — offline, no API/Alpaca ───────

class _FakeTrader:
    def __init__(self, positions): self._positions = positions
    def list_positions(self): return self._positions
    # The dedup ledger snapshots open orders twice before degrading; without
    # this the guard marks every closing submit as a duplicate and acted=[].
    def list_open_orders(self): return []
    def submit_option_limit(self, **kw): raise AssertionError("dry_run must not place orders")


def test_manage_positions_agentic_dry_run_orchestration(monkeypatch=None):
    import trading.wheel_runner as wr
    import data.alpaca_options_client as aoc

    # Force the baseline path (no Anthropic) and stub network deps. Save/restore
    # am.agent_decide so this global stub doesn't leak into later tests.
    _prev_agent_decide = am.agent_decide
    am.agent_decide = lambda obs, baseline: None
    wr._wheel_candidate_lookup = lambda: {"SOFI": {"would_own": 8, "quality_tag": "QUALITY"}}
    aoc.AlpacaOptionsClient = lambda *a, **k: _MockOptClient(50.0)

    try:
        positions = [
            _pos(avg=1.00, cur=0.02, dte=40, ticker="AAA"),   # captured 98% (above band) → deterministic CLOSE
            _pos(avg=1.00, cur=0.85, dte=40, ticker="BBB"),   # boring → HOLD, no record
            _pos(avg=1.00, cur=3.00, dte=40, ticker="SOFI"),  # 3x loss, gray → agent None → baseline RIDE (wheel candidate)
        ]
        state = {"last_regime": REGIME}
        acted = am.manage_positions_agentic(_FakeTrader(positions), state, dry_run=True, mode="full_execute")

        actions = {a["ticker"]: a["action"] for a in acted}
        assert actions.get("AAA") == "CLOSE"        # clear winner closed
        assert "BBB" not in actions                 # boring position untouched
        assert "SOFI" not in actions                # rode to assignment (wheel candidate), no order
        assert len(state["agent_decisions"]) == 3   # every position audited
        by_t = {d["ticker"]: d for d in state["agent_decisions"]}
        assert by_t["SOFI"]["executed"] == "RIDE"
        assert by_t["AAA"]["gray_zone"] is False     # 92% capture is above the gray band → deterministic
    finally:
        am.agent_decide = _prev_agent_decide


# ─── provider switch (gemini-first, Claude fallback, baseline last) ──────────
# agent_decide swaps ONLY the text generator: gemini_generate is tried first
# (default AGENT_LLM_PROVIDER=gemini), its text runs through the SAME strict
# _parse_decision, and any None/parse-failure falls back to the intact Claude
# call — then, if Claude also fails, apply_guardrails uses the rule baseline.
# All monkeypatching is offline (no Anthropic/Gemini network).

import agents.llm_gemini as _llm_gemini

_VALID_REPLY = (
    "DECISION: ROLL\nCONFIDENCE: 0.82\nROLL_OTM_PCT: 0.08\n"
    "THESIS_STATUS: INTACT\nTHESIS_CONVICTION: 8\n"
    "RATIONALE: Earnings beat, thesis intact, push strike lower for safety."
)
_GARBAGE_REPLY = "the model rambled with no schema and never emitted a DECISION line"


def _gray_obs():
    # 2x loss, ~30 DTE, quality name → gray zone, agent gets consulted.
    obs = am.build_observation(_pos(avg=1.00, cur=2.00, dte=30, strike=50.0), REGIME)
    obs["would_own_score"] = 8
    obs["underlying_price"] = 50.0
    return obs


def test_agent_decide_default_provider_is_gemini(monkeypatch=None):
    """(a) Default provider=gemini: gemini_generate is tried FIRST and, when it
    returns a parseable reply, Claude is NEVER called."""
    import os
    prev_env = os.environ.pop("AGENT_LLM_PROVIDER", None)  # exercise the default
    prev_gemini = _llm_gemini.gemini_generate
    prev_claude = am._claude_decide
    calls = {"gemini": 0, "claude": 0}
    try:
        def fake_gemini(prompt, **kw):
            calls["gemini"] += 1
            assert kw.get("use_search") is True   # web-search grounding stays ON
            return _VALID_REPLY
        def fake_claude(obs, baseline):
            calls["claude"] += 1
            return {"action": "HOLD", "confidence": 0.9, "roll_otm_pct": None,
                    "rationale": "claude", "thesis_status": None, "thesis_conviction": None}
        _llm_gemini.gemini_generate = fake_gemini
        am._claude_decide = fake_claude

        d = am.agent_decide(_gray_obs(), {"action": "HOLD", "reason": "baseline"})
        assert calls["gemini"] == 1 and calls["claude"] == 0   # gemini first, Claude untouched
        assert d is not None and d["action"] == "ROLL" and d["confidence"] == 0.82
        assert d["roll_otm_pct"] == 0.08 and d["thesis_conviction"] == 8
    finally:
        _llm_gemini.gemini_generate = prev_gemini
        am._claude_decide = prev_claude
        if prev_env is not None:
            os.environ["AGENT_LLM_PROVIDER"] = prev_env


def test_agent_decide_gemini_none_falls_back_to_claude(monkeypatch=None):
    """(b1) Gemini returns None (missing key / failure) → fall back to Claude,
    whose decision is returned with the SAME shape."""
    prev_gemini = _llm_gemini.gemini_generate
    prev_claude = am._claude_decide
    try:
        _llm_gemini.gemini_generate = lambda prompt, **kw: None
        claude_out = {"action": "CLOSE", "confidence": 0.7, "roll_otm_pct": None,
                      "rationale": "claude cut", "thesis_status": "broken",
                      "thesis_conviction": 3}
        am._claude_decide = lambda obs, baseline: dict(claude_out)

        d = am.agent_decide(_gray_obs(), {"action": "HOLD", "reason": "baseline"})
        assert d == claude_out   # Claude's decision, unchanged shape/keys
    finally:
        _llm_gemini.gemini_generate = prev_gemini
        am._claude_decide = prev_claude


def test_agent_decide_gemini_parsefail_falls_back_to_claude(monkeypatch=None):
    """(b2) Gemini returns GARBAGE (fails the strict DECISION parse) → fall back to
    Claude. A parse failure is a fallback, NEVER a default/garbled action."""
    prev_gemini = _llm_gemini.gemini_generate
    prev_claude = am._claude_decide
    try:
        _llm_gemini.gemini_generate = lambda prompt, **kw: _GARBAGE_REPLY
        assert am._parse_decision(_GARBAGE_REPLY) is None   # sanity: truly unparseable
        claude_out = {"action": "RIDE", "confidence": 0.8, "roll_otm_pct": None,
                      "rationale": "claude ride", "thesis_status": "intact",
                      "thesis_conviction": 8}
        am._claude_decide = lambda obs, baseline: dict(claude_out)

        d = am.agent_decide(_gray_obs(), {"action": "HOLD", "reason": "baseline"})
        assert d == claude_out
    finally:
        _llm_gemini.gemini_generate = prev_gemini
        am._claude_decide = prev_claude


def test_agent_decide_both_fail_returns_none_baseline_stands(monkeypatch=None):
    """(b3) Gemini garbles AND Claude fails → agent_decide is None, so
    apply_guardrails uses the deterministic rule baseline (never a garbled trade)."""
    prev_gemini = _llm_gemini.gemini_generate
    prev_claude = am._claude_decide
    try:
        _llm_gemini.gemini_generate = lambda prompt, **kw: _GARBAGE_REPLY
        am._claude_decide = lambda obs, baseline: None   # Claude also fails

        obs = _gray_obs()
        baseline = {"action": "RIDE", "reason": "wheel candidate"}
        d = am.agent_decide(obs, baseline)
        assert d is None   # no trustworthy agent decision

        final = am.apply_guardrails(obs, d, baseline, opt_client=None)
        assert final["action"] == "RIDE" and final["source"] == "baseline_fallback"
    finally:
        _llm_gemini.gemini_generate = prev_gemini
        am._claude_decide = prev_claude


def test_agent_decide_valid_gemini_matches_claude_structure(monkeypatch=None):
    """(c) A valid Gemini reply parses to the SAME decision structure the identical
    text would produce via the Claude path — proving only the text generator swapped,
    the shape/keys/contract downstream are byte-for-byte identical."""
    prev_gemini = _llm_gemini.gemini_generate
    prev_claude = am._claude_decide
    try:
        # Same raw text through both paths → identical parsed dicts.
        _llm_gemini.gemini_generate = lambda prompt, **kw: _VALID_REPLY
        am._claude_decide = lambda obs, baseline: am._parse_decision(_VALID_REPLY)

        import os
        prev_env = os.environ.get("AGENT_LLM_PROVIDER")
        try:
            os.environ["AGENT_LLM_PROVIDER"] = "gemini"
            via_gemini = am.agent_decide(_gray_obs(), {"action": "HOLD", "reason": "b"})
            os.environ["AGENT_LLM_PROVIDER"] = "anthropic"
            via_claude = am.agent_decide(_gray_obs(), {"action": "HOLD", "reason": "b"})
        finally:
            if prev_env is None:
                os.environ.pop("AGENT_LLM_PROVIDER", None)
            else:
                os.environ["AGENT_LLM_PROVIDER"] = prev_env

        assert via_gemini == via_claude       # same keys, same values, same contract
        assert set(via_gemini) == {"action", "confidence", "roll_otm_pct",
                                   "rationale", "thesis_status", "thesis_conviction", "raw"}
    finally:
        _llm_gemini.gemini_generate = prev_gemini
        am._claude_decide = prev_claude


if __name__ == "__main__":
    import sys
    funcs = [(n, f) for n, f in sorted(globals().items())
             if n.startswith("test_") and callable(f)]
    failures = 0
    for name, fn in funcs:
        try:
            fn()
            print(f"PASS {name}")
        except Exception as e:
            failures += 1
            print(f"FAIL {name}: {type(e).__name__}: {e}")
    print(f"\n{len(funcs)-failures}/{len(funcs)} passed")
    sys.exit(1 if failures else 0)


# ─── parser hardening (armed-safety: prose can't trigger a trade) ─────────────

def test_parse_decision_rejects_prose_hijack():
    """A grounded PROSE reply that merely mentions DECISION:/CONFIDENCE: mid-
    sentence must NOT parse — else it could place a real buy-to-close."""
    prose = ("In summary, my DECISION: CLOSE is warranted — the FDA setback broke "
             "the thesis. CONFIDENCE: 0.9 given the catalyst.")
    assert am._parse_decision(prose) is None


def test_parse_decision_tolerates_markdown():
    """Gemini often outer-bolds the label; **DECISION:** CLOSE must still parse."""
    md = ("**DECISION:** CLOSE\n**CONFIDENCE:** 0.80\n**ROLL_OTM_PCT:** NA\n"
          "**THESIS_STATUS:** BROKEN\n**THESIS_CONVICTION:** 3\n**RATIONALE:** bad earnings")
    d = am._parse_decision(md)
    assert d and d["action"] == "CLOSE" and abs(d["confidence"] - 0.80) < 1e-9
    assert d["thesis_status"] == "broken" and d["thesis_conviction"] == 3


def test_parse_decision_takes_last_anchored_block():
    """A prose line before the real block resolves to the model's final answer."""
    mixed = ("Based on my research the DECISION: HOLD might seem right but...\n"
             "DECISION: CLOSE\nCONFIDENCE: 0.70\nTHESIS_STATUS: BROKEN")
    d = am._parse_decision(mixed)
    assert d and d["action"] == "CLOSE" and abs(d["confidence"] - 0.70) < 1e-9
