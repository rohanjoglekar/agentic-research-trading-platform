"""Agentic position manager for the wheel trader.

Replaces the *fixed* close/hold thresholds in ``wheel_runner.manage_open_positions``
with an LLM agent that, for positions in a "gray zone", observes the position +
underlying + fresh news and decides: HOLD, CLOSE, ROLL, or RIDE (to assignment).

Design contract (see config.settings.AGENT):
  * The deterministic rules remain the BASELINE ("outline"). Boring positions —
    far from any threshold, plenty of DTE — follow them with no LLM call. The
    baseline is also the FALLBACK whenever the agent fails / times out / returns
    low confidence / can't be parsed.
  * Hard guardrails the agent cannot override: a kill-switch force-close at
    ``kill_switch_loss_mult``× entry, and rolls must be net-credit and within the
    DTE cap.
  * ``mode`` gates execution:
        off          → this module is never reached (wheel_runner stays deterministic)
        advisory     → agent decides + is logged/notified, but the BASELINE executes
        full_execute → the guardrailed agent decision executes real (paper) orders

Every decision (including HOLDs) is appended to ``trader_state.json["agent_decisions"]``.
"""
from __future__ import annotations

import datetime
import os
import re
import time
from typing import Optional

from loguru import logger

from config.settings import AGENT, RISK, OPTIONS, ANTHROPIC_API_KEY


# Provider switch (reversible + safe): AGENT_LLM_PROVIDER ∈ {gemini, anthropic},
# default gemini. Read via os.getenv at call time (see agent_decide) so it can be
# flipped without a redeploy. The gemini path runs the EXACT SAME prompt/system
# through Google-Search grounding (this decision uses web_search) and its text is
# fed through the SAME _parse_decision downstream. If Gemini returns None OR its
# reply fails the strict DECISION-format parse, we fall back to the intact Claude
# call; if Claude also fails, agent_decide returns None and the caller uses the
# deterministic rule baseline. The text generator is the ONLY thing swapped — no
# decision logic, guardrail, threshold, or fallback changes.
_DEFAULT_AGENT_PROVIDER = "gemini"

# Gemini's grounded reply carries search citations that inflate length; give the
# strict 6-line format headroom so the DECISION/RATIONALE lines aren't truncated.
_GEMINI_MAX_TOKENS = 900


# ─── OCC parsing (local copy — avoids importing wheel_runner at module load) ──

def _occ_parse(occ: str) -> Optional[dict]:
    """HOOD260618P00070000 → {ticker, expiration, type, strike}."""
    try:
        if len(occ) < 16:
            return None
        ticker = occ[:-15]
        yymmdd = occ[-15:-9]
        cp = occ[-9]
        strike = int(occ[-8:]) / 1000
        exp = f"20{yymmdd[:2]}-{yymmdd[2:4]}-{yymmdd[4:6]}"
        return {"ticker": ticker, "expiration": exp,
                "type": "P" if cp == "P" else "C", "strike": strike}
    except Exception:
        return None


def _dte(expiration: str) -> Optional[int]:
    try:
        exp = datetime.date.fromisoformat(expiration)
        return (exp - datetime.date.today()).days
    except Exception:
        return None


# ─── Observation ─────────────────────────────────────────────────────────────

def build_observation(p: dict, regime: dict) -> Optional[dict]:
    """Cheap observation from an Alpaca position dict (no network calls).

    Returns None if ``p`` is not a manageable short option position.
    """
    sym = p.get("symbol", "")
    if len(sym) < 16:
        return None
    try:
        qty = int(p.get("qty", 0))
        avg = float(p.get("avg_entry_price", 0))
        cur = float(p.get("current_price", 0))
    except (TypeError, ValueError):
        # An option position we can't parse is one the kill-switch never
        # evaluates — that must be loud, not silent (a zeroed/garbage quote
        # could hide a blowing-up short put for the whole data outage).
        logger.warning(f"[Agent] {sym}: unparseable qty/avg/current_price "
                       f"({p.get('qty')!r}/{p.get('avg_entry_price')!r}/"
                       f"{p.get('current_price')!r}) — SKIPPED from management "
                       f"this run (kill-switch not evaluated)")
        return None
    if qty < 0 and (avg <= 0 or cur <= 0):
        logger.warning(f"[Agent] {sym}: short option with zeroed avg/price "
                       f"(avg={avg}, cur={cur}) — SKIPPED from management this "
                       f"run (kill-switch not evaluated)")
        return None
    if qty >= 0 or avg <= 0 or cur <= 0:
        return None  # only manage short options

    meta = _occ_parse(sym) or {}
    if meta.get("type") != "P":
        return None  # this manager handles short puts (the CSP wheel)

    captured = 1.0 - (cur / avg)          # 1.0 = premium decayed to $0 (max profit)
    loss_mult = cur / avg                 # 2.0 = premium doubled (loss)
    return {
        "occ": sym,
        "ticker": meta.get("ticker", ""),
        "strike": meta.get("strike"),
        "expiration": meta.get("expiration"),
        "dte": _dte(meta.get("expiration", "")),
        "qty": abs(qty),
        "entry_premium": round(avg, 3),
        "current_premium": round(cur, 3),
        "captured_pct": round(captured, 3),
        "loss_mult": round(loss_mult, 3),
        "unrealized_pl": _to_float(p.get("unrealized_pl")),
        "regime": regime.get("regime", "calm"),
        "vix": regime.get("vix"),
    }


def _to_float(v) -> Optional[float]:
    try:
        return round(float(v), 2)
    except (TypeError, ValueError):
        return None


def enrich_observation(obs: dict, opt_client, candidates: dict) -> dict:
    """Add underlying technicals, earnings, current price, and prior research.

    Network-heavy — only called for gray-zone positions that will hit the agent.
    All lookups are best-effort; failures leave the field None.
    """
    ticker = obs["ticker"]
    try:
        obs["underlying_price"] = opt_client.get_current_price(ticker)
    except Exception:
        obs["underlying_price"] = None
    try:
        obs["technicals"] = opt_client.get_technical_data(ticker) or {}
    except Exception:
        obs["technicals"] = {}
    try:
        obs["days_to_earnings"] = opt_client.days_to_earnings(ticker)
    except Exception:
        obs["days_to_earnings"] = None

    px = obs.get("underlying_price")
    strike = obs.get("strike")
    if px and strike:
        obs["underlying_vs_strike_pct"] = round((px - strike) / strike, 4)  # >0 = above strike (good)
        obs["breakeven"] = round(strike - obs["entry_premium"], 2)
        obs["underlying_vs_breakeven_pct"] = round((px - obs["breakeven"]) / px, 4)

    rec = candidates.get(ticker.upper(), {})
    obs["would_own_score"] = rec.get("would_own")
    obs["quality_tag"] = rec.get("quality_tag")

    # Prior Ollama research memo — the "outline" the agent verifies, not trusts.
    obs["prior_memo"] = None
    try:
        from agents.ollama_researcher import load_memo
        memo = load_memo(ticker)
        if memo and memo.get("memo"):
            obs["prior_memo"] = memo["memo"].strip()[:1200]
    except Exception:
        pass

    # Tracked thesis — gives the agent MEMORY of how this thesis has drifted
    # across prior scans/decisions, so it can spot a BREAK vs an ordinary dip.
    obs["thesis"] = None
    try:
        from trading.thesis_store import thesis_summary
        obs["thesis"] = thesis_summary(ticker)
    except Exception:
        pass
    return obs


# ─── Deterministic baseline (the "outline" / fallback) ───────────────────────

def deterministic_decision(obs: dict) -> dict:
    """The original fixed-threshold rule, expressed as an agent decision.

    This is what the trader does today; it is the agent's starting outline and
    the fallback whenever the agent can't be trusted.
    """
    profit_pct = float(RISK.get("close_at_profit_pct", 0.80))
    loss_mult = float(RISK.get("close_at_loss_multiplier", 2.5))
    kill_mult = float(AGENT["kill_switch_loss_mult"])

    # Kill-switch belongs to the baseline too (mode-invariant): apply_guardrails
    # re-checks it, but the deterministic outline must reach the same answer.
    if obs["loss_mult"] >= kill_mult:
        return {"action": "CLOSE",
                "reason": f"KILL-SWITCH force-close (premium {obs['loss_mult']:.1f}× "
                          f"entry ≥ {kill_mult}×)"}
    if obs["captured_pct"] >= profit_pct:
        return {"action": "CLOSE",
                "reason": f"profit-take ({obs['captured_pct']*100:.0f}% of max profit)"}
    if obs["loss_mult"] >= loss_mult:
        # Loss threshold tripped: ride wheel candidates, cut the rest.
        from trading.wheel_runner import _is_wheel_candidate  # lazy: avoid cycle
        keep, why = _is_wheel_candidate(obs["ticker"], {obs["ticker"].upper(): {
            "would_own": obs.get("would_own_score"),
            "quality_tag": obs.get("quality_tag"),
        }})
        if keep:
            return {"action": "RIDE", "reason": f"loss threshold hit but wheel candidate ({why})"}
        return {"action": "CLOSE", "reason": f"loss-cut (premium {obs['loss_mult']:.1f}× entry; {why})"}
    return {"action": "HOLD", "reason": "inside thresholds — no action"}


# ─── Gray-zone gate ──────────────────────────────────────────────────────────

def in_gray_zone(obs: dict) -> tuple[bool, list]:
    """True when the position is interesting enough to consult the agent."""
    gz = AGENT["gray_zone"]
    reasons: list[str] = []

    lo, hi = gz["profit_capture_band"]
    if lo <= obs["captured_pct"] <= hi:
        reasons.append(f"captured {obs['captured_pct']*100:.0f}% (near profit target)")
    if obs["loss_mult"] >= gz["loss_mult_band_low"]:
        reasons.append(f"premium {obs['loss_mult']:.1f}× entry (under stress)")
    if obs.get("dte") is not None and obs["dte"] <= gz["dte_review_max"]:
        reasons.append(f"{obs['dte']} DTE (assignment zone)")
    de = obs.get("days_to_earnings")
    if de is not None and de <= gz["earnings_review_days"]:
        reasons.append(f"earnings in {de}d")
    if obs.get("regime") in gz["review_regimes"]:
        reasons.append(f"market regime {obs['regime']}")

    return (len(reasons) > 0, reasons)


# ─── LLM decision ────────────────────────────────────────────────────────────

# LINE-ANCHORED (^…, re.M) + markdown-tolerant (\** around label/value) so that
# (a) a grounded PROSE reply that merely mentions "…my DECISION: CLOSE…" mid-
# sentence can NEVER match (the label must start its own line) — this is the
# armed-safety invariant: a garbled reply must fall back, never place a trade;
# and (b) Gemini's common "**DECISION:** CLOSE" bolding still parses (else the
# gemini path would silently fall back to Claude every time). _parse_decision
# takes the LAST anchored match for the execution-gating fields (DECISION,
# CONFIDENCE) so a restated block resolves to the model's final answer.
_LBL = r"^\s*\**\s*%s\s*\**\s*:\s*\**\s*"          # markdown-tolerant line-anchored label
_DECISION_RE = re.compile(_LBL % "DECISION" + r"(HOLD|CLOSE|ROLL|RIDE)", re.I | re.M)
_CONF_RE = re.compile(_LBL % "CONFIDENCE" + r"([01](?:\.\d+)?)", re.I | re.M)
_ROLL_PCT_RE = re.compile(_LBL % "ROLL_OTM_PCT" + r"(NA|\d?\.\d+|\d+)", re.I | re.M)
_THESIS_STATUS_RE = re.compile(_LBL % "THESIS_STATUS" + r"(INTACT|WEAKENING|BROKEN)", re.I | re.M)
_THESIS_CONV_RE = re.compile(_LBL % "THESIS_CONVICTION" + r"(\d{1,2})", re.I | re.M)
_RATIONALE_RE = re.compile(_LBL % "RATIONALE" + r"(.+)", re.I | re.M)


def _extract_text(response) -> str:
    parts: list[str] = []
    for block in response.content:
        if getattr(block, "type", None) == "text":
            parts.append(block.text)
        elif isinstance(block, dict) and block.get("type") == "text":
            parts.append(block.get("text", ""))
    return "\n".join(p for p in parts if p).strip()


def _build_prompt(obs: dict, baseline: dict) -> str:
    t = obs.get("technicals") or {}
    memo = obs.get("prior_memo")
    memo_block = ""
    if memo:
        memo_block = (
            "\n--- PRIOR RESEARCH MEMO (local model, possibly stale, MAY BE WRONG) ---\n"
            f"{memo}\n--- end memo ---\n"
            "Treat this as a starting outline/hypothesis, NOT ground truth. Use your web "
            "search to confirm or refute it; note any errors.\n"
        )
    thesis_block = ""
    try:
        from trading.thesis_store import format_for_prompt
        thesis_block = format_for_prompt(obs.get("thesis"))
    except Exception:
        thesis_block = ""
    return (
        f"You are managing ONE open short cash-secured put in a wheel strategy. We SOLD this "
        f"put to collect premium and are willing to be assigned 100×{obs['qty']} shares of "
        f"{obs['ticker']} at ${obs['strike']} IF it is still a quality stock we'd own for 3-6 "
        f"months. Cutting is for theses that BROKE (fundamental deterioration, bad earnings, "
        f"sector shock) — not for ordinary drawdowns. Rolling means buy-to-close this put and "
        f"sell a further-OTM put for net credit, to push the strike away from danger while the "
        f"thesis is intact.\n\n"
        f"POSITION\n"
        f"  underlying {obs['ticker']} now ${obs.get('underlying_price')}  | strike ${obs['strike']} "
        f"(put)  | {obs.get('dte')} DTE  | qty {obs['qty']}\n"
        f"  entry premium ${obs['entry_premium']}  → current ${obs['current_premium']}  "
        f"(captured {obs['captured_pct']*100:.0f}% of max profit; premium {obs['loss_mult']:.2f}× entry)\n"
        f"  underlying vs strike: {(obs.get('underlying_vs_strike_pct') or 0)*100:.1f}%  | "
        f"vs breakeven: {(obs.get('underlying_vs_breakeven_pct') or 0)*100:.1f}%  | "
        f"unrealized P/L ${obs.get('unrealized_pl')}\n"
        f"UNDERLYING  RSI14 {t.get('rsi_14')} | 30d move {(t.get('recent_30d_return_pct') or 0)*100:.0f}% | "
        f"drawdown from 52w high {(t.get('drawdown_from_52w_high') or 0)*100:.0f}% | "
        f"above200MA {t.get('above_ma200')} | days-to-earnings {obs.get('days_to_earnings')}\n"
        f"MARKET regime {obs.get('regime')} (VIX {obs.get('vix')})\n"
        f"PRIOR AI QUALITY  would_own {obs.get('would_own_score')}/10 | tag {obs.get('quality_tag')}\n"
        f"{memo_block}"
        f"{thesis_block}\n"
        f"RULE-ENGINE BASELINE (your default outline — override only if your research justifies it):\n"
        f"  → {baseline['action']}: {baseline['reason']}\n\n"
        f"Do 1-2 web searches for FRESH catalysts on {obs['ticker']} (latest earnings result/guidance, "
        f"analyst actions, M&A, legal/regulatory, sector news). Then decide.\n\n"
        f"Output EXACTLY these six lines and NOTHING else (no preamble):\n"
        f"DECISION: <HOLD|CLOSE|ROLL|RIDE>\n"
        f"CONFIDENCE: <0.00-1.00>\n"
        f"ROLL_OTM_PCT: <NA, or the extra %% further OTM to roll, e.g. 0.08>\n"
        f"THESIS_STATUS: <INTACT|WEAKENING|BROKEN>\n"
        f"THESIS_CONVICTION: <integer 1-10, your updated would-own conviction after today's research>\n"
        f"RATIONALE: <2-3 sentences citing the specific news/data that drove the call>\n\n"
        f"HOLD = keep as-is. CLOSE = buy-to-close now (take profit OR cut a broken thesis). "
        f"ROLL = thesis intact but want the strike safer/more credit. RIDE = accept assignment "
        f"(only for quality names you'd own). THESIS_STATUS/THESIS_CONVICTION update the tracked "
        f"thesis above for future runs — be honest so drift is visible over time. "
        f"Be decisive and honest about confidence."
    )


def _claude_decide(obs: dict, baseline: dict) -> Optional[dict]:
    """The original Claude (Anthropic + web_search) per-position call — kept fully
    intact as the fallback path. Anthropic import stays lazy/guarded (the SDK may be
    absent). Returns a parsed decision dict, or None on missing key / SDK / call
    failure / unparseable text (any of which the caller treats as 'try the next
    fallback', ending at the deterministic rule baseline)."""
    if not ANTHROPIC_API_KEY:
        logger.warning("[agent] no ANTHROPIC_API_KEY — falling back to baseline")
        return None
    try:
        import anthropic
    except ImportError:
        logger.warning("[agent] anthropic SDK missing — falling back to baseline")
        return None

    try:
        client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
        msg = client.messages.create(
            model=AGENT["model"],
            max_tokens=600,
            timeout=AGENT.get("decision_timeout_s", 45),
            tools=[{"type": "web_search_20250305", "name": "web_search",
                    "max_uses": AGENT["web_search_max_uses"]}],
            messages=[{"role": "user", "content": _build_prompt(obs, baseline)}],
        )
        text = _extract_text(msg)
    except Exception as e:
        logger.warning(f"[agent] Claude call failed for {obs['ticker']}: {e}")
        return None

    return _parse_decision(text)


def _gemini_decide(obs: dict, baseline: dict) -> Optional[dict]:
    """Gemini path — the EXACT SAME prompt as Claude, with Google-Search grounding
    ON (the Claude call uses web_search, so use_search=True restores live
    catalyst/news grounding). The reply is run through the SAME strict
    _parse_decision, so an unparseable/garbled Gemini answer returns None (never a
    default action) and the caller falls back to Claude. gemini_generate never
    raises and returns None on missing key / any failure."""
    try:
        from agents.llm_gemini import gemini_generate
    except ImportError:
        logger.warning("[agent] gemini helper unavailable — falling back to Claude")
        return None
    text = gemini_generate(
        _build_prompt(obs, baseline),
        use_search=True,
        max_tokens=_GEMINI_MAX_TOKENS,
        temperature=AGENT.get("temperature", 0.7),
        timeout=AGENT.get("decision_timeout_s", 45),
    )
    if not text:
        return None
    return _parse_decision(text)


def agent_decide(obs: dict, baseline: dict) -> Optional[dict]:
    """Produce a per-position decision (HOLD/CLOSE/ROLL/RIDE) via the configured
    LLM provider. None on any failure so the caller uses the deterministic baseline.

    Provider switch AGENT_LLM_PROVIDER ∈ {gemini, anthropic} (default gemini), read
    here at call time so it can be flipped without a redeploy. Gemini is tried
    first; if it returns None OR its text fails the strict DECISION-format parse,
    we fall back to the intact Claude call. If Claude also fails, this returns None
    and apply_guardrails falls back to the rule baseline. A parse failure is ALWAYS
    a fallback, never a default action — an unparseable reply never produces a trade.
    Only the text generator is swapped; the parser, guardrails, and thresholds are
    untouched."""
    provider = os.getenv("AGENT_LLM_PROVIDER", _DEFAULT_AGENT_PROVIDER).strip().lower()

    if provider == "gemini":
        decision = _gemini_decide(obs, baseline)
        if decision is not None:
            logger.info(f"[agent] provider=gemini decided {decision['action']} for {obs['ticker']}")
            return decision
        logger.info(f"[agent] gemini unavailable/unparseable for {obs['ticker']} — falling back to Claude")

    decision = _claude_decide(obs, baseline)
    if decision is not None:
        logger.info(f"[agent] provider=anthropic decided {decision['action']} for {obs['ticker']}")
    return decision


def _parse_decision(text: str) -> Optional[dict]:
    if not text:
        return None
    d_all = _DECISION_RE.findall(text)
    if not d_all:
        return None
    action = d_all[-1].upper()                     # last anchored DECISION = final answer
    c_all = _CONF_RE.findall(text)
    confidence = float(c_all[-1]) if c_all else 0.0
    roll_pct = None
    rp = _ROLL_PCT_RE.search(text)
    if rp and rp.group(1).upper() != "NA":
        try:
            roll_pct = float(rp.group(1))
        except ValueError:
            roll_pct = None
    rat = _RATIONALE_RE.search(text)
    rationale = rat.group(1).strip()[:600] if rat else ""
    ts = _THESIS_STATUS_RE.search(text)
    thesis_status = ts.group(1).lower() if ts else None
    tc = _THESIS_CONV_RE.search(text)
    thesis_conviction = None
    if tc:
        try:
            thesis_conviction = max(1, min(10, int(tc.group(1))))
        except ValueError:
            thesis_conviction = None
    return {"action": action, "confidence": round(confidence, 2),
            "roll_otm_pct": roll_pct, "rationale": rationale,
            "thesis_status": thesis_status, "thesis_conviction": thesis_conviction,
            "raw": text[:1500]}


# ─── Roll target selection ───────────────────────────────────────────────────

def find_roll_target(obs: dict, opt_client, extra_otm: Optional[float] = None) -> Optional[dict]:
    """Find a further-OTM put to roll into for net credit. None if none qualifies."""
    cfg = AGENT["roll"]
    ticker = obs["ticker"]
    px = obs.get("underlying_price")
    cur_strike = obs.get("strike")
    cost_to_close = obs["current_premium"]  # per share
    if not px or not cur_strike:
        return None

    bump = max(cfg["min_otm_bump"], float(extra_otm or 0.0))
    strike_ceiling = cur_strike * (1.0 - bump)  # new strike must be at/below this

    try:
        exps = opt_client.get_options_expirations(ticker)
    except Exception as e:
        logger.warning(f"[roll] expirations failed for {ticker}: {e}")
        return None

    target_exp = _pick_roll_expiration(exps, cfg)
    if not target_exp:
        return None

    try:
        chain = opt_client.get_options_chain(ticker, target_exp)
    except Exception as e:
        logger.warning(f"[roll] chain failed for {ticker} {target_exp}: {e}")
        return None

    puts = chain.get("puts")
    if puts is None or getattr(puts, "empty", True):
        return None

    best = None
    for _, row in puts.iterrows():
        try:
            strike = float(row.get("strike", 0))
            bid = float(row.get("bid", 0))
            ask = float(row.get("ask", 0))
            oi = int(row.get("openInterest", 0) or 0)
        except (TypeError, ValueError):
            continue
        if strike <= 0 or strike > strike_ceiling or bid <= 0 or ask <= 0:
            continue
        if oi < OPTIONS.get("min_open_interest", 0):
            continue
        mid = (bid + ask) / 2
        credit = mid - cost_to_close            # per share, net of buying back current
        if credit < cfg["min_credit_per_share"]:
            continue
        otm = (px - strike) / px
        cand = {"expiration": target_exp, "strike": round(strike, 2),
                "premium": round(mid, 3), "credit_per_share": round(credit, 3),
                "otm_pct": round(otm, 4), "dte": _dte(target_exp)}
        # Prefer the furthest-OTM strike that still pays the required credit.
        if best is None or cand["otm_pct"] > best["otm_pct"]:
            best = cand
    return best


def _pick_roll_expiration(exps: list, cfg: dict) -> Optional[str]:
    if not exps:
        return None
    scored = []
    for e in exps:
        d = _dte(e)
        if d is None or d < 14 or d > cfg["max_dte"]:
            continue
        in_pref = cfg["target_dte_min"] <= d <= cfg["target_dte_max"]
        # prefer the preferred band, then closeness to its midpoint
        mid = (cfg["target_dte_min"] + cfg["target_dte_max"]) / 2
        scored.append((0 if in_pref else 1, abs(d - mid), e))
    if not scored:
        return None
    scored.sort()
    return scored[0][2]


# ─── Guardrails ──────────────────────────────────────────────────────────────

def _kill_switch_ride_ok(obs: dict, ride_ctx: Optional[dict]) -> tuple[bool, str]:
    """All AGENT["kill_switch_ride"] gates must pass to convert the kill switch
    into RIDE (accept assignment). Any failure → (False, reason) → force-close.

    Fails CLOSED on missing data (no underlying price, no account equity): the
    kill switch keeps its original semantics unless the ride is PROVABLY inside
    the gates. When it passes, the position's collateral is added to the
    shared riding-exposure budget in ride_ctx.
    """
    cfg = AGENT.get("kill_switch_ride") or {}
    if not cfg.get("enabled"):
        return False, "ride-through disabled"

    from trading.wheel_runner import _is_wheel_candidate  # lazy: avoid cycle
    keep, why = _is_wheel_candidate(obs["ticker"], {obs["ticker"].upper(): {
        "would_own": obs.get("would_own_score"),
        "quality_tag": obs.get("quality_tag"),
    }})
    if not keep:
        return False, f"not a wheel candidate ({why})"

    if cfg.get("require_thesis_not_broken", True):
        try:
            from trading.covered_call_runner import thesis_posture
            posture, preason = thesis_posture(obs["ticker"])
            if posture == "broken":
                return False, f"thesis broken ({preason})"
        except Exception:
            pass  # no thesis signal ≠ broken — the candidate gate above holds

    px = obs.get("underlying_price")
    strike = obs.get("strike") or 0
    if not px or not strike:
        return False, "no underlying price — can't verify ITM depth"
    max_itm = float(cfg.get("max_itm_pct", 0.15))
    if px < strike * (1 - max_itm):
        return False, f"too deep ITM (${px:.2f} < strike ${strike} −{max_itm*100:.0f}%)"

    if not ride_ctx or not ride_ctx.get("equity"):
        return False, "no account equity — can't verify assignment budget"
    exposure = float(strike) * 100 * abs(int(obs.get("qty") or 0))
    budget = float(ride_ctx["equity"]) * float(cfg.get("max_assignment_pct_equity", 0.35))
    riding = float(ride_ctx.get("riding_exposure", 0.0))
    if riding + exposure > budget:
        return False, (f"assignment budget full (${riding:,.0f} riding + "
                       f"${exposure:,.0f} new > ${budget:,.0f} cap)")

    ride_ctx["riding_exposure"] = riding + exposure
    return True, (f"wheel candidate ({why}), {((px - strike) / strike * 100):+.1f}% vs strike, "
                  f"riding ${ride_ctx['riding_exposure']:,.0f} of ${budget:,.0f} budget")


def apply_guardrails(obs: dict, agent: Optional[dict], baseline: dict,
                     opt_client, ride_ctx: Optional[dict] = None) -> dict:
    """Resolve the FINAL executable decision from the agent + hard rules.

    Returns {action, source, notes:[...], roll_target?, confidence?, rationale?}.
    """
    notes: list[str] = []

    # 1. Kill-switch — overrides everything, even the agent. A FRESH wheel
    #    candidate inside the ride gates converts to RIDE (accept assignment)
    #    instead of surrendering at max loss; see AGENT["kill_switch_ride"].
    if obs["loss_mult"] >= AGENT["kill_switch_loss_mult"]:
        ok, why = _kill_switch_ride_ok(obs, ride_ctx)
        if ok:
            return {"action": "RIDE", "source": "kill_switch_ride",
                    "notes": [f"premium {obs['loss_mult']:.1f}× ≥ kill-switch "
                              f"{AGENT['kill_switch_loss_mult']}× but riding to assignment — {why}"]}
        return {"action": "CLOSE", "source": "kill_switch",
                "notes": [f"premium {obs['loss_mult']:.1f}× entry ≥ kill-switch "
                          f"{AGENT['kill_switch_loss_mult']}× — force close ({why})"]}

    # 2. No trustworthy agent decision → deterministic baseline.
    if agent is None:
        return {"action": baseline["action"], "source": "baseline_fallback",
                "notes": ["agent unavailable/failed — using rule baseline"],
                "rationale": baseline["reason"]}
    if agent["confidence"] < AGENT["min_confidence"]:
        return {"action": baseline["action"], "source": "baseline_low_confidence",
                "notes": [f"agent confidence {agent['confidence']} < "
                          f"{AGENT['min_confidence']} — using rule baseline"],
                "rationale": baseline["reason"], "agent_wanted": agent["action"]}

    # 3. Accept the agent's action. ROLL must clear the credit/DTE guardrail.
    if agent["action"] == "ROLL":
        target = find_roll_target(obs, opt_client, extra_otm=agent.get("roll_otm_pct"))
        if target is None:
            notes.append("agent chose ROLL but no net-credit target within DTE cap — "
                         f"falling back to baseline ({baseline['action']})")
            return {"action": baseline["action"], "source": "roll_infeasible",
                    "notes": notes, "rationale": baseline["reason"],
                    "agent_wanted": "ROLL", "confidence": agent["confidence"]}
        return {"action": "ROLL", "source": "agent", "notes": notes,
                "roll_target": target, "confidence": agent["confidence"],
                "rationale": agent["rationale"]}

    return {"action": agent["action"], "source": "agent", "notes": notes,
            "confidence": agent["confidence"], "rationale": agent["rationale"]}


# ─── Execution ───────────────────────────────────────────────────────────────

def _close_limit(cur: float) -> float:
    return round(cur * 1.05 + 0.01, 2)


def _poll_order(trader, order_id: str, timeout_s: float, poll_s: float) -> dict:
    """Poll an order until a terminal status or timeout. Returns the last order
    dict seen (possibly {} if every fetch failed)."""
    terminal = {"filled", "canceled", "cancelled", "rejected", "expired",
                "done_for_day", "replaced"}
    deadline = time.time() + max(0.0, timeout_s)
    last: dict = {}
    while True:
        try:
            last = trader._get(f"/v2/orders/{order_id}") or {}
        except Exception as e:
            logger.debug(f"[agent] order poll failed for {order_id}: {e}")
        if (last.get("status") or "").lower() in terminal or time.time() >= deadline:
            return last
        time.sleep(poll_s)


def execute_decision(final: dict, obs: dict, trader, dry_run: bool,
                     pending=None) -> Optional[dict]:
    """Carry out the final decision. Returns a record for CLOSE/ROLL, None for HOLD/RIDE.

    ``pending`` is the run's PendingLedger (wheel_runner) — closing submits are
    skipped when a matching order is already resting/submitted this run.
    """
    action = final["action"]
    occ = obs["occ"]
    qty = obs["qty"]
    now = datetime.datetime.utcnow().isoformat() + "Z"

    if action in ("HOLD", "RIDE"):
        return None

    if action == "CLOSE":
        if pending is not None and pending.has(occ, "buy"):
            pending.skip_duplicate(occ, "buy", context=f"agent CLOSE [{final['source']}]")
            return None
        limit = _close_limit(obs["current_premium"])
        rec = {"submitted_at": now, "symbol": occ, "ticker": obs["ticker"],
               "strike": obs["strike"], "expiration": obs["expiration"], "qty": qty,
               "side": "buy", "limit_price": limit, "entry_premium": obs["entry_premium"],
               "exit_premium_est": obs["current_premium"], "captured_pct": obs["captured_pct"] * 100,
               "reason": f"agent CLOSE [{final['source']}]: {final.get('rationale','')}"[:300],
               "action": "CLOSE", "dry_run": dry_run}
        if dry_run:
            logger.info(f"[agent] DRY close {occ} @ ${limit}")
            return rec
        try:
            order = trader.submit_option_limit(occ_symbol=occ, qty=qty, side="buy", limit_price=limit)
            rec["alpaca_order_id"] = order.get("id")
            rec["status"] = order.get("status")
            if pending is not None:
                pending.record(occ, "buy", qty)
            logger.info(f"[agent] CLOSE {occ} qty {qty} @ ${limit}")
        except Exception as e:
            logger.error(f"[agent] close {occ} failed: {e}")
            rec["error"] = str(e)
        return rec

    if action == "ROLL":
        tgt = final["roll_target"]
        from trading.alpaca_executor import build_occ
        new_occ = build_occ(obs["ticker"], tgt["expiration"], "put", tgt["strike"])
        close_limit = _close_limit(obs["current_premium"])
        open_limit = round(tgt["premium"], 2)
        rec = {"submitted_at": now, "symbol": occ, "ticker": obs["ticker"],
               "strike": obs["strike"], "expiration": obs["expiration"], "qty": qty,
               "side": "buy", "limit_price": close_limit, "action": "ROLL",
               "roll_to_occ": new_occ, "roll_to_strike": tgt["strike"],
               "roll_to_expiration": tgt["expiration"],
               "roll_credit_per_share": tgt["credit_per_share"],
               "reason": (f"agent ROLL [{final['source']}] → ${tgt['strike']}P {tgt['expiration']} "
                          f"(+${tgt['credit_per_share']}/sh credit): {final.get('rationale','')}")[:300],
               "dry_run": dry_run}
        if dry_run:
            logger.info(f"[agent] DRY roll {occ} → {new_occ} (close ${close_limit} / open ${open_limit})")
            return rec
        if pending is not None and pending.has(occ, "buy"):
            pending.skip_duplicate(occ, "buy", context="agent ROLL close-leg")
            return None
        try:
            close_order = trader.submit_option_limit(occ_symbol=occ, qty=qty, side="buy",
                                                     limit_price=close_limit)
            rec["alpaca_order_id"] = close_order.get("id")
            rec["status"] = close_order.get("status")
            if pending is not None:
                pending.record(occ, "buy", qty)
        except Exception as e:
            logger.error(f"[agent] roll close-leg {occ} failed: {e}")
            rec["error"] = f"close leg: {e}"
            return rec  # don't open the new leg if we couldn't close the old one

        # Two-phase: only sell the new leg once the close leg actually FILLED —
        # otherwise a resting/rejected close leaves a naked extra short put.
        roll_cfg = AGENT["roll"]
        live = _poll_order(trader, close_order.get("id"),
                           timeout_s=float(roll_cfg.get("fill_timeout_s", 10)),
                           poll_s=float(roll_cfg.get("fill_poll_s", 2)))
        close_status = (live.get("status") or "").lower()
        if close_status:
            rec["status"] = close_status
        if close_status != "filled":
            rec["roll_open_status"] = "skipped_close_leg_not_filled"
            logger.warning(f"[agent] roll close-leg {occ} status={close_status or 'unknown'} "
                           f"after {roll_cfg.get('fill_timeout_s', 10)}s — NOT opening the new "
                           f"leg; next run's management handles the position")
            return rec

        # Re-verify the net-credit guardrail against the ACTUAL close fill price.
        try:
            fill_px = float(live.get("filled_avg_price") or 0)
        except (TypeError, ValueError):
            fill_px = 0.0
        actual_close = fill_px if fill_px > 0 else obs["current_premium"]
        actual_credit = round(tgt["premium"] - actual_close, 3)
        if actual_credit < float(roll_cfg["min_credit_per_share"]):
            rec["roll_open_status"] = "skipped_credit_guardrail"
            rec["roll_credit_per_share"] = actual_credit
            logger.warning(f"[agent] roll {occ} → {new_occ}: net credit ${actual_credit}/sh at "
                           f"actual close fill ${actual_close} < required "
                           f"${roll_cfg['min_credit_per_share']} — NOT opening the new leg")
            return rec
        rec["roll_credit_per_share"] = actual_credit

        if pending is not None and pending.has(new_occ, "sell"):
            pending.skip_duplicate(new_occ, "sell", context="agent ROLL open-leg")
            rec["roll_open_status"] = "skipped_duplicate"
            return rec
        try:
            open_order = trader.submit_option_limit(occ_symbol=new_occ, qty=qty, side="sell",
                                                    limit_price=open_limit)
            rec["roll_open_order_id"] = open_order.get("id")
            rec["roll_open_status"] = open_order.get("status")
            if pending is not None:
                pending.record(new_occ, "sell", qty)
            logger.info(f"[agent] ROLL {occ} → {new_occ} qty {qty} "
                        f"(close ${close_limit} / open ${open_limit})")
        except Exception as e:
            logger.error(f"[agent] roll open-leg {new_occ} failed: {e}")
            rec["error"] = f"open leg: {e} (CLOSE leg already filled)"
        return rec

    return None


# ─── Top-level entry point (called by wheel_runner) ──────────────────────────

def manage_positions_agentic(trader, state: dict, dry_run: bool = False,
                             mode: str = "full_execute", pending=None) -> list:
    """Agentic replacement for manage_open_positions. Returns close/roll records."""
    from trading.wheel_runner import _wheel_candidate_lookup, PendingLedger  # lazy: avoid import cycle
    from data.alpaca_options_client import AlpacaOptionsClient

    if pending is None:
        pending = PendingLedger(trader)
    candidates = _wheel_candidate_lookup()
    regime = state.get("last_regime") or {"regime": "calm", "vix": None}
    opt_client = AlpacaOptionsClient()

    # Build observations for every manageable short put.
    observed = []
    for p in trader.list_positions():
        obs = build_observation(p, regime)
        if obs:
            observed.append(obs)

    # Sort by stress (highest loss_mult first) so the per-run agent-call cap
    # spends budget on the positions that matter most.
    observed.sort(key=lambda o: o["loss_mult"], reverse=True)

    # Assignment-budget context for the kill-switch ride gate. Pre-seed the
    # riding exposure with every position ALREADY in baseline-ride territory
    # (loss ≥ threshold, below the kill switch, wheel candidate) so the budget
    # is order-independent — those positions are heading to assignment too.
    try:
        _equity = float(trader.account().get("equity", 0) or 0)
    except Exception:
        _equity = 0.0
    ride_ctx = {"equity": _equity, "riding_exposure": 0.0}
    _loss_floor = float(RISK.get("close_at_loss_multiplier", 2.5))
    _ks_mult = float(AGENT["kill_switch_loss_mult"])
    from trading.wheel_runner import _is_wheel_candidate as _iwc  # lazy: avoid cycle
    for o in observed:
        if _loss_floor <= o["loss_mult"] < _ks_mult and _iwc(o["ticker"], candidates)[0]:
            ride_ctx["riding_exposure"] += float(o.get("strike") or 0) * 100 * abs(int(o.get("qty") or 0))

    acted: list[dict] = []
    agent_calls = 0
    max_calls = AGENT["max_agent_calls_per_run"]
    decisions_log = state.setdefault("agent_decisions", [])

    for obs in observed:
        baseline = deterministic_decision(obs)
        gray, gz_reasons = in_gray_zone(obs)

        agent = None
        if gray and agent_calls < max_calls:
            obs = enrich_observation(obs, opt_client, candidates)
            gray, gz_reasons = in_gray_zone(obs)  # re-check now that earnings is known
            if gray:
                agent = agent_decide(obs, baseline)
                agent_calls += 1

        final = apply_guardrails(obs, agent, baseline, opt_client, ride_ctx=ride_ctx)

        # advisory mode: the agent's decision is recorded/notified, but the
        # deterministic BASELINE is what actually executes. The KILL-SWITCH is
        # exempt from this swap — it is a hard deterministic guardrail, not an
        # agentic decision, and must execute in every mode. Its gated ride-
        # through (kill_switch_ride) is equally deterministic and rides in
        # every mode too (RIDE executes as a no-op).
        exec_decision = final
        if mode == "advisory" and final.get("source") not in ("kill_switch", "kill_switch_ride"):
            exec_decision = {"action": baseline["action"], "source": "advisory_baseline",
                             "notes": [f"advisory mode — agent wanted {final['action']}"],
                             "rationale": baseline["reason"]}

        record = execute_decision(exec_decision, obs, trader, dry_run, pending=pending)

        audit = {
            "ts": datetime.datetime.utcnow().isoformat() + "Z",
            "ticker": obs["ticker"], "occ": obs["occ"],
            "mode": mode, "gray_zone": gray, "gray_reasons": gz_reasons,
            "baseline": baseline,
            "agent": agent,
            "final": {k: v for k, v in final.items() if k != "roll_target"},
            "roll_target": final.get("roll_target"),
            "executed": exec_decision["action"],
            "dry_run": dry_run,
            "snapshot": {k: obs.get(k) for k in
                         ("strike", "dte", "captured_pct", "loss_mult",
                          "underlying_price", "would_own_score", "quality_tag",
                          "days_to_earnings", "regime")},
        }
        decisions_log.append(audit)

        # Feed the agent's read back into the tracked thesis so conviction/status
        # drift is visible on the next run. Only when the agent actually ran
        # (boring baseline-only positions don't move the thesis).
        if agent is not None:
            try:
                from trading.thesis_store import record_agent_observation
                _status = agent.get("thesis_status")
                _impact = {"broken": "break", "weakening": "weaken",
                           "intact": "neutral"}.get(_status, "neutral")
                record_agent_observation(
                    obs["ticker"],
                    event=agent.get("rationale") or final.get("rationale", ""),
                    impact=_impact,
                    conviction=agent.get("thesis_conviction"),
                    status=_status,
                    action=final["action"],
                )
            except Exception as e:
                logger.warning(f"[agent] thesis update failed for {obs['ticker']}: {e}")

        if record:
            if not dry_run:
                state.setdefault("closes", []).append(record)
            acted.append(record)

    # Keep the audit log bounded.
    if len(decisions_log) > 500:
        state["agent_decisions"] = decisions_log[-500:]

    logger.info(f"[agent] mode={mode} positions={len(observed)} agent_calls={agent_calls} "
                f"acted={len(acted)} (dry_run={dry_run})")
    return acted
