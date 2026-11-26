"""Provider-switch tests for agents/per_stock_analyst.py (the per-top-20
risk/thesis analyst).

The site is web-search-grounded and env-switched via PER_STOCK_LLM_PROVIDER
(default 'gemini'). No network — gemini_generate / the Claude call are
monkeypatched. We assert:

  (a) the DEFAULT provider (gemini) is used when it returns a parseable reply,
  (b) when Gemini returns None OR a reply WITHOUT a parseable WOULD_OWN_SCORE,
      we FALL BACK to the intact Claude path, and
  (c) the output SHAPE is unchanged (a str carrying **Risk assessment** +
      **Why it is a wheel candidate** + a WOULD_OWN_SCORE line that parses to a
      clamped 1-10 int), and the score-parse + clamp still work end-to-end.
"""
import sys
from pathlib import Path


SO_AI_ROOT = Path(__file__).resolve().parents[1]
if str(SO_AI_ROOT) not in sys.path:
    sys.path.insert(0, str(SO_AI_ROOT))

import agents.per_stock_analyst as psa  # noqa: E402


_STOCK = {
    "symbol": "SOFI",
    "current_price": 9.0,
    "best_rating": "A",
    "best_score": 82,
    "strikes": [
        {"Strike": 8.0, "Expiration": "2026-08-01", "Premium": 0.50,
         "Monthly %": 2.5, "POP": 0.78},
    ],
    "analyst_consensus": None,
}

# A well-formed reply the SAME shape the Claude path would produce.
_GOOD = (
    "**Risk assessment** SOFI faces rate sensitivity and regulatory scrutiny "
    "on its lending book over the next few quarters.\n\n"
    "**Why it is a wheel candidate** Growing member base, improving credit, and "
    "a strengthening balance sheet support ownership if assigned.\n\n"
    "WOULD_OWN_SCORE: 7"
)
# Prose WITHOUT a parseable WOULD_OWN_SCORE line — must trigger fallback.
_NO_SCORE = (
    "**Risk assessment** Some risks here.\n\n"
    "**Why it is a wheel candidate** Some upside here.\n\n"
    "(no score line at all)"
)


# ─── (a) default provider = gemini ───────────────────────────────────────────

def test_default_provider_is_gemini(monkeypatch):
    monkeypatch.setattr(psa, "_PER_STOCK_PROVIDER", "gemini")
    calls = {"gemini": 0, "claude": 0}

    def fake_gemini(prompt, sym):
        calls["gemini"] += 1
        assert "**Risk assessment**" in prompt          # SAME prompt reaches gemini
        assert "WOULD_OWN_SCORE" in prompt
        return _GOOD

    def fake_claude(prompt, sym, model, max_uses):
        calls["claude"] += 1
        return "CLAUDE should not be used"

    monkeypatch.setattr(psa, "_gemini_analysis", fake_gemini)
    monkeypatch.setattr(psa, "_claude_analysis", fake_claude)

    out = psa.analyze_stock(_STOCK)
    assert out == _GOOD                                  # gemini's text passed through
    assert calls["gemini"] == 1 and calls["claude"] == 0  # default used, no fallback
    # shape: parses to a clamped 1-10 int, same as the Claude path would
    assert psa._parse_would_own(out) == 7
    assert isinstance(out, str)


def test_module_default_env_is_gemini():
    # The module-level default (no env override) is 'gemini'. Verified in a fresh
    # subprocess with the env var UNSET so we exercise the real os.getenv default
    # WITHOUT reloading the module in-process (a reload would repoison shared
    # module state that sibling test files monkeypatch).
    import os
    import subprocess
    env = {k: v for k, v in os.environ.items() if k != "PER_STOCK_LLM_PROVIDER"}
    code = ("import agents.per_stock_analyst as p;"
            "assert p._PER_STOCK_PROVIDER == 'gemini', p._PER_STOCK_PROVIDER;"
            "print('ok')")
    r = subprocess.run(
        [sys.executable, "-c", code],
        cwd=str(SO_AI_ROOT), env={**env, "PYTHONPATH": str(SO_AI_ROOT)},
        capture_output=True, text=True,
    )
    assert r.returncode == 0, r.stderr
    assert "ok" in r.stdout


# ─── (b) gemini None → fall back to Claude ───────────────────────────────────

def test_falls_back_when_gemini_none(monkeypatch):
    monkeypatch.setattr(psa, "_PER_STOCK_PROVIDER", "gemini")
    calls = {"gemini": 0, "claude": 0}

    def fake_gemini(prompt, sym):
        calls["gemini"] += 1
        return ""      # gemini_generate returned None → helper yields "" → fall back

    def fake_claude(prompt, sym, model, max_uses):
        calls["claude"] += 1
        return _GOOD

    monkeypatch.setattr(psa, "_gemini_analysis", fake_gemini)
    monkeypatch.setattr(psa, "_claude_analysis", fake_claude)

    out = psa.analyze_stock(_STOCK)
    assert out == _GOOD                                  # Claude's text used
    assert calls["gemini"] == 1 and calls["claude"] == 1  # tried gemini, then claude
    assert psa._parse_would_own(out) == 7


# ─── (b') gemini reply has no parseable score → fall back to Claude ──────────

def test_falls_back_when_gemini_score_unparseable(monkeypatch):
    monkeypatch.setattr(psa, "_PER_STOCK_PROVIDER", "gemini")
    calls = {"gemini": 0, "claude": 0}

    def fake_gemini(prompt, sym):
        calls["gemini"] += 1
        return _NO_SCORE          # non-empty but no WOULD_OWN_SCORE → must fall back

    def fake_claude(prompt, sym, model, max_uses):
        calls["claude"] += 1
        return _GOOD

    monkeypatch.setattr(psa, "_gemini_analysis", fake_gemini)
    monkeypatch.setattr(psa, "_claude_analysis", fake_claude)

    out = psa.analyze_stock(_STOCK)
    assert out == _GOOD                                  # fell back, not the scoreless text
    assert calls["gemini"] == 1 and calls["claude"] == 1
    assert psa._parse_would_own(out) == 7


# ─── (b'') end-to-end through the REAL _gemini_analysis (patch gemini_generate) ──

def test_parse_fail_fallback_through_real_gemini_helper(monkeypatch):
    """Patch gemini_generate itself (no network) so the REAL _gemini_analysis
    runs: a scoreless Gemini reply must route to the Claude fallback, and
    use_search=True must be passed (this site is web-grounded)."""
    monkeypatch.setattr(psa, "_PER_STOCK_PROVIDER", "gemini")
    import agents.llm_gemini as gem
    seen = {}

    def fake_generate(prompt, **kw):
        seen.update(kw)
        return _NO_SCORE          # non-empty, unparseable → fall back to Claude

    monkeypatch.setattr(gem, "gemini_generate", fake_generate)

    claude_calls = {"n": 0}

    def fake_claude(prompt, sym, model, max_uses):
        claude_calls["n"] += 1
        return _GOOD

    monkeypatch.setattr(psa, "_claude_analysis", fake_claude)

    out = psa.analyze_stock(_STOCK)
    assert out == _GOOD                                  # fell back through real helper
    assert claude_calls["n"] == 1
    assert seen.get("use_search") is True                # web-grounding preserved


# ─── (c) explicit anthropic provider skips gemini entirely ───────────────────

def test_anthropic_provider_skips_gemini(monkeypatch):
    monkeypatch.setattr(psa, "_PER_STOCK_PROVIDER", "anthropic")
    calls = {"gemini": 0, "claude": 0}

    def fake_gemini(prompt, sym):
        calls["gemini"] += 1
        return _GOOD

    def fake_claude(prompt, sym, model, max_uses):
        calls["claude"] += 1
        return _GOOD

    monkeypatch.setattr(psa, "_gemini_analysis", fake_gemini)
    monkeypatch.setattr(psa, "_claude_analysis", fake_claude)

    out = psa.analyze_stock(_STOCK)
    assert out == _GOOD
    assert calls["gemini"] == 0 and calls["claude"] == 1  # gemini never tried


# ─── score clamp preserved (shape/contract unchanged) ────────────────────────

def test_score_clamp_unchanged():
    assert psa._parse_would_own("WOULD_OWN_SCORE: 12") == 10   # clamp high
    assert psa._parse_would_own("WOULD_OWN_SCORE: 0") == 1     # clamp low
    assert psa._parse_would_own("WOULD_OWN_SCORE: 6") == 6
    assert psa._parse_would_own("no score here") is None
