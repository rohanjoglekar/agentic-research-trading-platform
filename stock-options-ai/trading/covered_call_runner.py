"""Covered-call leg of the wheel — sell-side income on owned/assigned shares.

Closes the loop the CSP side opens: when a cash-secured put is assigned we hold
100 shares per contract. This module auto-sells out-of-the-money covered calls
against those shares and buy-to-closes them at a profit, so the shares earn
income instead of sitting idle.

Design (see config.settings.COVERED_CALLS):
  - Driven by LIVE Alpaca stock positions, so assigned shares are covered
    automatically with zero manual tracking.
  - Strike selection is THESIS-TIERED: trading/thesis_store.py conviction picks
    the posture (intact→keep / weakening→income / broken→exit). Deterministic
    rules produce a baseline pick; an optional agent may RE-PICK among the
    strikes that already pass the cost-basis + liquidity filters (smart but
    guardrailed — it can never breach the floor).
  - HARD FLOOR: never sell a call below the EFFECTIVE cost basis (assignment
    price minus all premium collected on the ticker) so assignment is net-green
    — lifted only when the thesis is BROKEN and we want the shares called away.

Like the CSP open, the open here is rules-first; the LLM only adapts within the
guardrails. Buy-to-close management is deterministic profit-taking.
"""
from __future__ import annotations

import datetime
import os
import re
from typing import Optional

import pandas as pd
from loguru import logger

from config.settings import (
    ANTHROPIC_API_KEY,
    COVERED_CALLS,
    ETF_BLOCKLIST,
)
from trading.alpaca_executor import build_occ


# Provider switch (reversible + safe): COVERED_CALLS_LLM_PROVIDER ∈ {gemini, anthropic},
# default gemini. The Gemini path sends the EXACT SAME prompt (web-search-grounded, to
# match the Claude call's web_search tool) and its text is run through the SAME
# STRIKE/CONFIDENCE parse + in-candidate-set + confidence-floor validation. If Gemini
# returns None OR its reply doesn't parse into a vetted candidate, we FALL BACK to the
# intact Claude call, then to the deterministic baseline. Read via os.getenv so it can be
# flipped without a redeploy. This ONLY swaps the text generator — the pre-filtered
# candidate list, the effective-cost-basis FLOOR, and the posture logic are enforced
# OUTSIDE the LLM call and are never touched here.
def _cc_provider() -> str:
    return os.getenv("COVERED_CALLS_LLM_PROVIDER", "gemini").strip().lower()


_OCC_RE = re.compile(r"^([A-Z0-9]{1,6})(\d{2})(\d{2})(\d{2})([CP])(\d{8})$")


def _occ_parse(occ: str) -> Optional[dict]:
    m = _OCC_RE.match(occ or "")
    if not m:
        return None
    root, yy, mm, dd, cp, strike = m.groups()
    return {
        "ticker": root,
        "expiration": f"20{yy}-{mm}-{dd}",
        "type": "C" if cp == "C" else "P",
        "strike": int(strike) / 1000.0,
    }


def _is_occ(sym: str) -> bool:
    return bool(_OCC_RE.match(sym or ""))


# ─── Effective cost basis ─────────────────────────────────────────────────────

def _banked_premium(rec: dict) -> float:
    """Dollar premium an order record actually BANKED — 0.0 unless it filled.

    Only status == "filled" counts (reconcile_order_statuses keeps these live);
    never-filled / canceled / rejected / resting orders and error-marked records
    must not move the effective cost basis — counting them deflated the
    covered-call hard floor with premium that was never collected. Prefers the
    real fill price; limit_price is only a fallback for filled records missing it.
    """
    if rec.get("error"):
        return 0.0
    if (rec.get("status") or "").lower() != "filled":
        return 0.0
    px = rec.get("filled_avg_price") or rec.get("limit_price")
    qty = rec.get("qty")
    try:
        if px and qty:
            return float(px) * 100 * int(qty)
    except (TypeError, ValueError):
        pass
    return 0.0


def effective_cost_basis(ticker: str, raw_basis: float, shares: int, state: dict) -> float:
    """Raw assignment price minus net option premium collected on this ticker.

    The wheel's real break-even: every premium banked on a name (the CSP rounds
    that led to assignment + every covered call since) lowers the effective cost
    of the shares. Net credit = FILLED sell-to-open premiums − FILLED
    buy-to-close debits, spread across the current share count.
    """
    ticker = (ticker or "").upper()
    credit = 0.0
    for rec in (state.get("orders") or []) + (state.get("cc_orders") or []):
        if (rec.get("symbol") or "").upper() != ticker:
            continue
        credit += _banked_premium(rec)
    for rec in (state.get("closes") or []):
        if (rec.get("ticker") or "").upper() != ticker:
            continue
        credit -= _banked_premium(rec)
    per_share = credit / max(int(shares), 1)
    return max(0.01, float(raw_basis) - per_share)


# ─── Discover coverable share lots ────────────────────────────────────────────

def find_coverable_lots(trader, state: dict) -> list[dict]:
    """Live Alpaca STOCK positions with ≥100 uncovered shares.

    Shares already backing a short call (held position OR resting sell order)
    are subtracted so we never over-write more calls than we have shares.
    """
    positions = trader.list_positions()
    stock_lots: dict[str, dict] = {}
    covered: dict[str, int] = {}

    for p in positions:
        sym = (p.get("symbol") or "").upper()
        if _is_occ(sym):
            meta = _occ_parse(sym)
            try:
                qty = int(float(p.get("qty", 0)))
            except (TypeError, ValueError):
                qty = 0
            if meta and meta["type"] == "C" and qty < 0:
                covered[meta["ticker"]] = covered.get(meta["ticker"], 0) + abs(qty) * 100
            continue
        # plain stock position
        try:
            qty = int(float(p.get("qty", 0)))
            raw_basis = float(p.get("avg_entry_price", 0) or 0)
            current = float(p.get("current_price", 0) or 0)
        except (TypeError, ValueError):
            continue
        if qty < 100 or raw_basis <= 0 or current <= 0:
            continue
        stock_lots[sym] = {"shares": qty, "raw_basis": raw_basis, "current_price": current}

    # resting sell-to-open call orders also cover shares; resting PLAIN-STOCK
    # sell orders (share-exit stops from step 1.5, manual sells) REMOVE shares —
    # without subtracting them, a call written this run against shares that are
    # mid-sale becomes a naked call the moment the stock sell fills.
    try:
        for o in trader.list_open_orders():
            osym = (o.get("symbol") or "").upper()
            if o.get("side") != "sell":
                continue
            if _is_occ(osym):
                m = _occ_parse(osym)
                if m and m["type"] == "C":
                    try:
                        covered[m["ticker"]] = covered.get(m["ticker"], 0) + abs(int(float(o.get("qty", 0)))) * 100
                    except (TypeError, ValueError):
                        pass
            elif osym in stock_lots:
                try:
                    stock_lots[osym]["shares"] -= abs(int(float(o.get("qty", 0))))
                except (TypeError, ValueError):
                    pass
    except Exception as e:
        logger.warning(f"[CC] open-orders cover check failed: {e}")

    out: list[dict] = []
    for sym, info in stock_lots.items():
        cov = covered.get(sym, 0)
        lots = (info["shares"] - cov) // 100
        if lots < 1:
            continue
        eff = effective_cost_basis(sym, info["raw_basis"], info["shares"], state)
        out.append({
            "symbol": sym,
            "shares": info["shares"],
            "raw_basis": round(info["raw_basis"], 2),
            "effective_basis": round(eff, 2),
            "current_price": info["current_price"],
            "covered_shares": cov,
            "uncovered_lots": int(lots),
        })
    return out


# ─── Share-lot exits (how long assigned shares may be held) ───────────────────
# See config.settings.COVERED_CALLS["share_exits"]. Three bounded-downside rules
# on top of the CC postures: a hard stop below effective basis, a time+income
# posture escalation, and a direct sell when a broken name has no writable call.

_POSTURE_BUMP = {"intact": "weakening", "weakening": "broken", "broken": "broken"}


def _cc_realized_credit(ticker: str, state: dict) -> float:
    """Net covered-call premium BANKED on this ticker (filled STO − filled CC BTC)."""
    ticker = (ticker or "").upper()
    credit = 0.0
    for rec in state.get("cc_orders") or []:
        if (rec.get("symbol") or "").upper() == ticker:
            credit += _banked_premium(rec)
    for rec in state.get("closes") or []:
        if (rec.get("ticker") or "").upper() == ticker and rec.get("strategy") == "CC":
            credit -= _banked_premium(rec)
    return credit


def _covered_share_counts(trader) -> dict:
    """Shares per ticker pinned under a short call (held or resting sell order)."""
    covered: dict[str, int] = {}
    for p in trader.list_positions():
        sym = (p.get("symbol") or "").upper()
        m = _occ_parse(sym) if _is_occ(sym) else None
        try:
            qty = int(float(p.get("qty", 0)))
        except (TypeError, ValueError):
            qty = 0
        if m and m["type"] == "C" and qty < 0:
            covered[m["ticker"]] = covered.get(m["ticker"], 0) + abs(qty) * 100
    try:
        for o in trader.list_open_orders():
            osym = (o.get("symbol") or "").upper()
            if o.get("side") == "sell" and _is_occ(osym):
                m = _occ_parse(osym)
                if m and m["type"] == "C":
                    try:
                        covered[m["ticker"]] = covered.get(m["ticker"], 0) + abs(int(float(o.get("qty", 0)))) * 100
                    except (TypeError, ValueError):
                        pass
    except Exception as e:
        logger.warning(f"[CC] open-orders cover check failed: {e}")
    return covered


def _sell_shares(trader, state: dict, sym: str, qty: int, current: float,
                 reason: str, execute: bool) -> dict:
    """Market-sell qty shares of sym; record under state['share_sells']."""
    rec = {
        "submitted_at": datetime.datetime.utcnow().isoformat() + "Z",
        "symbol": sym, "qty": qty, "side": "sell", "type": "shares",
        "price_at_submit": round(current, 2), "reason": reason,
    }
    if not execute:
        rec["dry_run"] = True
        logger.info(f"[CC/exit] DRY: would sell {qty} sh {sym} @ ~${current:.2f} — {reason}")
        return rec
    try:
        order = trader.submit_equity_qty(sym, qty, "sell")
        rec["alpaca_order_id"] = order.get("id")
        rec["status"] = order.get("status")
        logger.info(f"[CC/exit] SOLD {qty} sh {sym} @ mkt (~${current:.2f}) — {reason}")
    except Exception as e:
        rec["error"] = str(e)
        logger.error(f"[CC/exit] share sell {sym} failed: {e}")
    state.setdefault("share_sells", []).append(rec)
    return rec


def manage_share_exits(trader, state: dict, *, execute: bool) -> tuple[list, dict]:
    """Apply the share_exits rules to every stock lot the account holds.

    Returns (sold_records, posture_overrides) where posture_overrides maps
    ticker → forced/escalated posture for THIS run's CC writes. Also maintains
    state['share_lots'][ticker]['first_seen'] so lot age survives restarts
    (age counts from first sighting — assignment events aren't timestamped).
    """
    cfg = COVERED_CALLS.get("share_exits") or {}
    sold: list[dict] = []
    overrides: dict[str, str] = {}
    if not cfg.get("enabled"):
        return sold, overrides

    now = datetime.datetime.utcnow()
    lots_meta = state.setdefault("share_lots", {})
    covered = _covered_share_counts(trader)
    stop_pct = float(cfg.get("stop_below_basis_pct", 0.20))
    esc_days = int(cfg.get("escalate_after_days", 45))
    esc_income = float(cfg.get("escalate_min_monthly_income", 0.01))

    live: set = set()
    for p in trader.list_positions():
        sym = (p.get("symbol") or "").upper()
        if _is_occ(sym):
            continue
        try:
            qty = int(float(p.get("qty", 0)))
            raw_basis = float(p.get("avg_entry_price", 0) or 0)
            current = float(p.get("current_price", 0) or 0)
        except (TypeError, ValueError):
            continue
        if qty <= 0 or raw_basis <= 0 or current <= 0:
            continue
        live.add(sym)
        meta = lots_meta.setdefault(sym, {"first_seen": now.isoformat() + "Z"})
        eff = effective_cost_basis(sym, raw_basis, qty, state)
        uncovered = max(0, qty - covered.get(sym, 0))

        # 1) hard stop below effective basis — bounded post-assignment downside
        stop_px = eff * (1 - stop_pct)
        if current < stop_px:
            reason = (f"share stop: ${current:.2f} < ${stop_px:.2f} "
                      f"(effective basis ${eff:.2f} − {stop_pct*100:.0f}%)")
            if uncovered > 0:
                sold.append(_sell_shares(trader, state, sym, uncovered, current, reason, execute))
            if uncovered < qty:
                # covered shares can't be sold without going naked on the call —
                # force the exit posture so assignment takes them out instead
                overrides[sym] = "broken"
                logger.warning(f"[CC/exit] {sym}: {qty - uncovered} sh pinned under short "
                               f"calls at stop level — posture forced broken (exit via assignment)")
            continue

        # 2) time + income escalation — a lot that isn't earning doesn't get to idle
        try:
            first_seen = datetime.datetime.fromisoformat(str(meta.get("first_seen")).replace("Z", ""))
            age_days = (now - first_seen).days
        except (ValueError, TypeError):
            age_days = 0
        if age_days > esc_days:
            income = _cc_realized_credit(sym, state)
            monthly_rate = (income / (qty * current)) / max(age_days / 30.0, 1.0)
            if monthly_rate < esc_income:
                overrides[sym] = "_bump"
                logger.info(f"[CC/exit] {sym}: held {age_days}d, CC income "
                            f"{monthly_rate*100:.2f}%/mo < {esc_income*100:.0f}%/mo — posture escalates")

    # prune tracking for lots no longer held
    for sym in [s for s in lots_meta if s not in live]:
        lots_meta.pop(sym, None)

    return sold, overrides


# ─── Thesis posture ───────────────────────────────────────────────────────────

def thesis_posture(ticker: str) -> tuple[str, str]:
    """Return (posture, reason). posture ∈ {intact, weakening, broken}.

    Prefers the thesis_store status/conviction; falls back to the scan's
    would_own / Ollama quality_tag; defaults to neutral "weakening".
    """
    ticker = (ticker or "").upper()
    try:
        from trading.thesis_store import thesis_summary
        s = thesis_summary(ticker)
        if s:
            status = (s.get("status") or "").lower()
            conv = s.get("conviction")
            if status in ("intact", "weakening", "broken"):
                return status, f"thesis status={status} (conv={conv})"
            if conv is not None:
                if conv <= 3:
                    return "broken", f"conviction={conv}"
                if conv <= 5:
                    return "weakening", f"conviction={conv}"
                return "intact", f"conviction={conv}"
    except Exception as e:
        logger.debug(f"[CC] thesis lookup failed for {ticker}: {e}")
    try:
        from trading.wheel_runner import _wheel_candidate_lookup, WHEEL_CANDIDATE_BAD_TAGS
        rec = _wheel_candidate_lookup().get(ticker, {})
        wo = rec.get("would_own")
        tag = (rec.get("quality_tag") or "").upper()
        if tag in WHEEL_CANDIDATE_BAD_TAGS:
            return "broken", f"quality_tag={tag}"
        if wo is not None:
            if wo <= 3:
                return "broken", f"would_own={wo}"
            if wo <= 5:
                return "weakening", f"would_own={wo}"
            return "intact", f"would_own={wo}"
    except Exception:
        pass
    return "weakening", "no thesis — neutral default"


# ─── Strike selection ─────────────────────────────────────────────────────────

def _pick_expiration(client, symbol: str) -> tuple[Optional[str], Optional[int]]:
    try:
        exps = client.get_options_expirations(symbol) or []
    except Exception as e:
        logger.warning(f"[CC] expirations failed for {symbol}: {e}")
        return None, None
    today = datetime.date.today()
    pool: list[tuple[str, int]] = []
    for e in exps:
        try:
            d = (datetime.date.fromisoformat(e) - today).days
        except Exception:
            continue
        if COVERED_CALLS["dte_min"] <= d <= COVERED_CALLS["dte_max"]:
            pool.append((e, d))
    if not pool:
        return None, None
    target = COVERED_CALLS["target_dte"]
    return min(pool, key=lambda c: abs(c[1] - target))


def _build_candidates(client, symbol: str, lot: dict, posture: str) -> tuple[list[dict], Optional[str], Optional[int]]:
    """OTM call candidates that pass the cost-basis floor + liquidity rules."""
    exp, dte = _pick_expiration(client, symbol)
    if not exp:
        return [], None, None
    try:
        chain = client.get_options_chain(symbol, exp)
    except Exception as e:
        logger.warning(f"[CC] chain failed for {symbol} {exp}: {e}")
        return [], exp, dte
    calls = chain.get("calls")
    if calls is None or getattr(calls, "empty", True):
        return [], exp, dte

    from strategies.covered_call import calculate_cc_metrics

    current = lot["current_price"]
    eff_basis = lot["effective_basis"]
    is_exit = (posture == "broken")
    just_otm = current * 1.001
    if is_exit or not COVERED_CALLS.get("require_above_cost_basis", True):
        floor = just_otm                       # exit / floor disabled → only OTM
    else:
        floor = max(eff_basis, just_otm)        # ≥ effective basis AND OTM
    max_strike = current * (1.0 + COVERED_CALLS["max_otm_pct"])

    cands: list[dict] = []
    for _, row in calls.iterrows():
        try:
            strike = float(row.get("strike", 0) or 0)
            bid = float(row.get("bid", 0) or 0)
            ask = float(row.get("ask", 0) or 0)
        except (TypeError, ValueError):
            continue
        if strike <= 0 or bid <= 0 or ask <= 0:
            continue
        if strike < floor or strike > max_strike:
            continue
        oi = int(row.get("openInterest", 0) or 0)
        vol = int(row.get("volume", 0) or 0) if pd.notna(row.get("volume")) else None
        delta = None
        try:
            if pd.notna(row.get("delta")):
                delta = float(row.get("delta"))
        except Exception:
            delta = None
        m = calculate_cc_metrics(
            stock_price=current, cost_basis=eff_basis, strike=strike,
            bid=bid, ask=ask, dte=dte, open_interest=oi, volume=vol, delta=delta,
        )
        if not m["is_valid"]:
            continue
        if m["premium"] < COVERED_CALLS["min_premium"]:
            continue
        mr = m["monthly_return_pct"] / 100.0
        if mr < COVERED_CALLS["min_monthly_return"]:
            continue
        cands.append({
            "symbol": symbol, "strike": strike, "expiration": exp, "dte": dte,
            "premium": m["premium"], "bid": bid, "ask": ask, "delta": delta,
            "monthly_return": mr, "otm_pct": m["otm_pct"] / 100.0,
            "called_away_return": m["called_away_return_pct"] / 100.0,
            "open_interest": oi, "occ": build_occ(symbol, exp, "call", strike),
        })
    return cands, exp, dte


def _rank_baseline(cands: list[dict], posture: str) -> dict:
    """Pick the baseline strike by posture intent.

      broken    → we want OUT: maximize premium (near-ATM, invite assignment).
      intact    → keep the shares + upside: hug the LOW target delta; do NOT
                  force 2% (income is only a tiebreak).
      weakening → neutral income: hit ~2%/mo if reachable above the floor, then
                  the strike closest to the target delta; else best income.
    """
    target_delta = COVERED_CALLS["delta_targets"][posture]
    target_otm = COVERED_CALLS["otm_fallback"][posture]
    target_mr = COVERED_CALLS["target_monthly_return"]

    def dist(c: dict) -> float:
        if c.get("delta") is not None:
            return abs(c["delta"] - target_delta)
        return abs(c["otm_pct"] - target_otm)

    if posture == "broken":
        return max(cands, key=lambda c: c["premium"])
    if posture == "intact":
        # closest to the conservative target delta; higher income breaks ties.
        return min(cands, key=lambda c: (round(dist(c), 4), -c["monthly_return"]))
    # weakening
    meeting = [c for c in cands if c["monthly_return"] >= target_mr]
    if meeting:
        return min(meeting, key=lambda c: (round(dist(c), 4), -c["monthly_return"]))
    return max(cands, key=lambda c: c["monthly_return"])


def _extract_text(response) -> str:
    parts: list[str] = []
    for block in getattr(response, "content", []) or []:
        if getattr(block, "type", None) == "text":
            parts.append(block.text)
        elif isinstance(block, dict) and block.get("type") == "text":
            parts.append(block.get("text", ""))
    return "\n".join(p for p in parts if p).strip()


def _agent_prompt(symbol: str, lot: dict, posture: str, cands: list[dict],
                  baseline: dict, thesis_text: Optional[str], cfg: dict) -> str:
    """Build the strike RE-PICK prompt. Provider-agnostic: BOTH the Gemini and
    the Claude paths send this EXACT text (only the text generator differs)."""
    table = "\n".join(
        f"  ${c['strike']:.2f} | {c['dte']}d | prem ${c['premium']:.2f} | "
        f"delta {c['delta'] if c['delta'] is not None else '?'} | "
        f"{c['monthly_return']*100:.2f}%/mo | {c['otm_pct']*100:+.1f}% OTM | "
        f"called-away {c['called_away_return']*100:+.1f}%"
        for c in sorted(cands, key=lambda x: x["strike"])
    )
    posture_note = {
        "intact": "We are BULLISH and want to KEEP these shares — prefer farther OTM to preserve upside; accept <2%/mo.",
        "weakening": "Neutral — standard income, push toward ~2%/mo without giving up too much upside.",
        "broken": "We WANT OUT of this stock — prefer near-the-money for max premium and welcome assignment (a clean exit).",
    }[posture]
    return (
        f"You are selecting ONE covered-call strike to sell against {lot['shares']} shares of {symbol} "
        f"(effective cost basis ${lot['effective_basis']:.2f}, current price ${lot['current_price']:.2f}).\n"
        f"Posture: {posture.upper()}. {posture_note}\n\n"
        f"Thesis memory:\n{thesis_text or '(none)'}\n\n"
        f"Choose ONE strike from this pre-vetted list (all already satisfy our cost-basis and liquidity rules):\n"
        f"{table}\n\n"
        f"The rule-based baseline picked ${baseline['strike']:.2f}. Do up to {cfg['web_search_max_uses']} web "
        f"searches on {symbol} ONLY if a near-term catalyst (earnings date, known event) would change the choice.\n"
        f"Then output EXACTLY two lines and nothing else:\n"
        f"STRIKE: <one strike value from the list>\n"
        f"CONFIDENCE: <0.0-1.0>\n"
        f"Keep the baseline strike unless you have a concrete, news-grounded reason to deviate."
    )


def _gemini_pick_text(prompt: str, cfg: dict, symbol: str) -> Optional[str]:
    """Gemini path: same prompt, web-search grounded (matches the Claude call's
    web_search tool). Returns text, or None on missing key / any failure — the
    helper never raises, so the caller falls back to Claude cleanly."""
    try:
        from agents.llm_gemini import gemini_generate
    except ImportError:
        return None
    return gemini_generate(
        prompt,
        model=cfg.get("gemini_model"),   # None → helper's default (GEMINI_MODEL)
        use_search=True,                 # matches the Claude call's web_search tool
        temperature=0.4,
        max_tokens=400,
    )


def _claude_pick_text(prompt: str, cfg: dict, symbol: str) -> Optional[str]:
    """Claude path (fully intact): same prompt + web_search tool as before.
    Returns text, or None on missing key / SDK-absent / any failure."""
    if not ANTHROPIC_API_KEY:
        return None
    try:
        import anthropic
    except ImportError:
        return None
    try:
        client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
        msg = client.messages.create(
            model=cfg["model"], max_tokens=400,
            tools=[{"type": "web_search_20250305", "name": "web_search",
                    "max_uses": cfg["web_search_max_uses"]}],
            messages=[{"role": "user", "content": prompt}],
        )
        return _extract_text(msg)
    except Exception as e:
        logger.warning(f"[CC] agent pick (claude) failed for {symbol}: {e}")
        return None


# Sentinel: the reply parsed cleanly but the model chose to KEEP the baseline
# (confidence below the floor). That's a valid decision, NOT a provider failure,
# so it must NOT trigger a fallback to the other provider.
_KEEP_BASELINE = object()


def _parse_agent_pick(text: Optional[str], cands: list[dict], cfg: dict, symbol: str):
    """Run the SAME downstream STRIKE/CONFIDENCE parser + guardrails on any
    provider's text. Returns:
      * a pick dict            — a valid, in-candidate-set override (with confidence)
      * _KEEP_BASELINE         — parsed OK but keep the baseline (low confidence)
      * None                   — parse/validation FAILED (caller may try next provider)

    The in-candidate-set check is the hard guardrail: an out-of-set or unparseable
    strike is REJECTED, so a bad reply can never pick a floor-breaching strike (the
    candidate list is already floor-filtered upstream)."""
    if not text:
        return None
    # markdown-tolerant + line-anchored (re.M) so Gemini's "**STRIKE:** $52"
    # parses; the strike is still validated against the vetted candidate set
    # below, so an out-of-set/prose value can never be selected.
    sm = re.search(r"^\s*\**\s*STRIKE\s*\**\s*:\s*\**\s*\$?([0-9]+(?:\.[0-9]+)?)", text, re.I | re.M)
    cm = re.search(r"^\s*\**\s*CONFIDENCE\s*\**\s*:\s*\**\s*([01](?:\.[0-9]+)?)", text, re.I | re.M)
    if not sm:
        return None
    conf = float(cm.group(1)) if cm else 0.0
    if conf < cfg["min_confidence"]:
        logger.info(f"[CC] {symbol} agent confidence {conf:.2f} < {cfg['min_confidence']} — keep baseline")
        return _KEEP_BASELINE
    strike = round(float(sm.group(1)), 2)
    by_strike = {round(c["strike"], 2): c for c in cands}
    pick = by_strike.get(strike)
    if not pick:
        nearest = min(cands, key=lambda c: abs(c["strike"] - strike))
        if abs(nearest["strike"] - strike) <= 0.51:
            pick = nearest
    if not pick:
        logger.info(f"[CC] {symbol} agent strike ${strike} not in candidate set — reject (keep/fallback)")
        return None
    pick = dict(pick)
    pick["agent_confidence"] = conf
    return pick


def _agent_pick(symbol: str, lot: dict, posture: str, cands: list[dict],
                baseline: dict, thesis_text: Optional[str]) -> Optional[dict]:
    """Let the agent RE-PICK among already-safe candidates. Returns an override
    pick or None to keep the baseline. Cannot breach the floor — the candidate
    list is pre-filtered and the in-set guardrail below rejects any out-of-set
    reply.

    Provider order (COVERED_CALLS_LLM_PROVIDER, default 'gemini'): the primary
    provider generates the STRIKE/CONFIDENCE text; if it returns nothing OR its
    text fails to parse into a vetted candidate, we FALL BACK to Claude; if that
    also fails, None → the deterministic baseline stands."""
    cfg = COVERED_CALLS["agent"]
    if not cfg.get("enabled"):
        return None

    provider = _cc_provider()
    # gemini-first by default; 'anthropic' pins straight to Claude. The runner
    # only ever swaps the TEXT GENERATOR — parsing + guardrails are shared.
    if provider == "anthropic":
        order = [("anthropic", _claude_pick_text)]
    else:
        order = [("gemini", _gemini_pick_text), ("anthropic", _claude_pick_text)]

    prompt = _agent_prompt(symbol, lot, posture, cands, baseline, thesis_text, cfg)

    for name, gen in order:
        text = gen(prompt, cfg, symbol)
        result = _parse_agent_pick(text, cands, cfg, symbol)
        if result is _KEEP_BASELINE:
            logger.info(f"[CC] {symbol} strike re-pick via {name} — keep baseline")
            return None
        if result is not None:
            result["agent_provider"] = name
            logger.info(f"[CC] {symbol} strike re-pick via {name} → ${result['strike']:.2f} "
                        f"(conf {result['agent_confidence']:.2f})")
            return result
        # parse/validation failed for this provider — try the next one (if any)
        logger.info(f"[CC] {symbol} strike re-pick via {name} produced no usable pick — "
                    f"falling back")
    return None


def choose_cc(client, symbol: str, lot: dict, posture: str, *,
              use_agent: bool = True, thesis_text: Optional[str] = None) -> tuple[Optional[dict], bool]:
    """Return (pick, used_agent). pick is the chosen strike dict or None."""
    cands, exp, dte = _build_candidates(client, symbol, lot, posture)
    if not cands:
        return None, False
    baseline = _rank_baseline(cands, posture)
    pick = dict(baseline)
    pick["engine"] = "rules"
    used = False
    if use_agent and len(cands) > 1:
        used = True
        ov = _agent_pick(symbol, lot, posture, cands, baseline, thesis_text)
        if ov:
            ov["engine"] = "agent"
            pick = ov
    return pick, used


# ─── Management: buy-to-close winners ─────────────────────────────────────────

def manage_short_calls(trader, state: dict, *, execute: bool, pending=None) -> list[dict]:
    """Buy-to-close short calls once profit_take_pct of max premium is captured.

    Losers (stock rallied through the strike) are intentionally NOT cut — letting
    them assign sells the shares at a strike ≥ cost basis, which completes the
    wheel as designed.

    ``pending`` is the run's PendingLedger — a BTC is skipped when a matching
    buy order is already resting or was already submitted this run.
    """
    profit_pct = float(COVERED_CALLS["profit_take_pct"])
    closed: list[dict] = []
    for p in trader.list_positions():
        sym = (p.get("symbol") or "").upper()
        if not _is_occ(sym):
            continue
        meta = _occ_parse(sym)
        if not meta or meta["type"] != "C":
            continue
        try:
            qty = int(float(p.get("qty", 0)))
            avg = float(p.get("avg_entry_price", 0) or 0)
            cur = float(p.get("current_price", 0) or 0)
        except (TypeError, ValueError):
            continue
        if qty >= 0 or avg <= 0 or cur <= 0:
            continue  # only short calls
        captured = 1.0 - (cur / avg)
        if captured < profit_pct:
            continue
        if pending is not None and pending.has(sym, "buy"):
            pending.skip_duplicate(sym, "buy", context="CC profit-take")
            continue
        limit = round(cur * 1.05 + 0.01, 2)
        rec = {
            "submitted_at": datetime.datetime.utcnow().isoformat() + "Z",
            "symbol": sym, "ticker": meta["ticker"], "strategy": "CC",
            "strike": meta["strike"], "expiration": meta["expiration"],
            "qty": abs(qty), "side": "buy", "limit_price": limit,
            "entry_premium": avg, "exit_premium_est": cur,
            "reason": f"CC profit-take ({captured*100:.0f}% captured)",
            "captured_pct": round(captured * 100, 1),
        }
        if not execute:
            rec["dry_run"] = True
            closed.append(rec)
            logger.info(f"[CC] DRY: would BTC {sym} @ ${limit} — {rec['reason']}")
            continue
        try:
            order = trader.submit_option_limit(occ_symbol=sym, qty=abs(qty), side="buy", limit_price=limit)
            rec["alpaca_order_id"] = order.get("id")
            rec["status"] = order.get("status")
            closed.append(rec)
            state.setdefault("closes", []).append(rec)
            if pending is not None:
                pending.record(sym, "buy", abs(qty))
            logger.info(f"[CC] BTC {sym} qty {abs(qty)} @ ${limit} — {rec['reason']}")
        except Exception as e:
            logger.error(f"[CC] BTC {sym} failed: {e}")
    return closed


# ─── Orchestrator ─────────────────────────────────────────────────────────────

def run_covered_calls(trader, state: dict, *, regime: Optional[dict] = None,
                      dry_run: bool = False, pending=None) -> dict:
    """Manage existing short calls, then write new covered calls on uncovered lots."""
    result: dict = {"opened": [], "closed": [], "skipped": []}
    if not COVERED_CALLS.get("enabled", True):
        return result
    mode = COVERED_CALLS.get("mode", "off")
    if mode == "off":
        return result
    execute = (mode == "full_execute") and not dry_run

    if pending is None:
        # Direct callers get their own resting-order snapshot; the wheel run
        # passes its shared ledger so within-run dedup spans both legs.
        from trading.wheel_runner import PendingLedger  # lazy: avoid import cycle
        pending = PendingLedger(trader)

    # 1) manage existing short calls (profit-take) — always runs
    result["closed"] = manage_short_calls(trader, state, execute=execute, pending=pending)

    # 1.5) share-lot exits (stop / time+income escalation) BEFORE writing new
    # calls, so a stopped-out lot never gets a fresh call sold against it.
    result["share_sells"] = []
    posture_overrides: dict = {}
    try:
        share_sells, posture_overrides = manage_share_exits(trader, state, execute=execute)
        result["share_sells"] = share_sells
    except Exception as e:
        logger.error(f"[CC] share-exit step failed (non-fatal): {e}")

    # 2) open new covered calls on uncovered lots
    regime_name = (regime or {}).get("regime")
    if regime_name == "extreme" and COVERED_CALLS.get("skip_open_in_extreme", True):
        logger.info("[CC] skipping CC opens — VIX extreme")
        return result

    try:
        from data.alpaca_options_client import AlpacaOptionsClient
        client = AlpacaOptionsClient()
    except Exception as e:
        logger.error(f"[CC] options client init failed: {e}")
        return result

    lots = find_coverable_lots(trader, state)
    # Belt and braces vs the same-run race: never write a call on a ticker whose
    # shares were just market-sold in step 1.5 (the sell may not be resting or
    # reflected in list_positions yet).
    _just_sold = {(s.get("symbol") or "").upper() for s in result["share_sells"]}
    if _just_sold:
        lots = [l for l in lots if l["symbol"] not in _just_sold]
    if not lots:
        logger.info("[CC] no uncovered share lots to write calls on")
        return result

    agent_cfg = COVERED_CALLS.get("agent", {})
    agent_calls_left = int(agent_cfg.get("max_calls_per_run", 0)) if agent_cfg.get("enabled") else 0

    for lot in lots:
        sym = lot["symbol"]
        if sym in ETF_BLOCKLIST:
            result["skipped"].append({"symbol": sym, "reason": "ETF blocklist"})
            continue
        posture, why = thesis_posture(sym)
        # share-exit overrides: "_bump" escalates one tier (idle lot must earn
        # or walk toward assignment); an explicit posture (e.g. "broken" from a
        # stop with pinned shares) replaces it outright.
        ov = posture_overrides.get(sym)
        if ov == "_bump":
            bumped = _POSTURE_BUMP.get(posture, posture)
            if bumped != posture:
                why = f"{why} · escalated {posture}→{bumped} (held long, low CC income)"
                posture = bumped
        elif ov:
            why = f"{why} · forced {ov} (share-exit rule)"
            posture = ov
        thesis_text = None
        try:
            from trading.thesis_store import thesis_summary, format_for_prompt
            thesis_text = format_for_prompt(thesis_summary(sym))
        except Exception:
            thesis_text = None

        pick, used_agent = choose_cc(
            client, sym, lot, posture,
            use_agent=(agent_calls_left > 0), thesis_text=thesis_text,
        )
        if used_agent:
            agent_calls_left -= 1

        if not pick:
            # A broken name with NO writable call can't exit via assignment —
            # sell the uncovered shares directly instead of idling on a chain
            # that won't pay for the exit (share_exits.sell_when_broken_unwritable).
            if (posture == "broken"
                    and (COVERED_CALLS.get("share_exits") or {}).get("sell_when_broken_unwritable")):
                n = lot["uncovered_lots"] * 100
                rec = _sell_shares(
                    trader, state, sym, n, lot["current_price"],
                    f"broken thesis + no writable call ≥ min_premium — direct exit ({why})",
                    execute,
                )
                result["share_sells"].append(rec)
                continue
            floor_desc = "effective basis" if posture != "broken" else "OTM floor"
            result["skipped"].append({
                "symbol": sym,
                "reason": f"no valid OTM call ≥ {floor_desc} (${lot['effective_basis']:.2f}), posture={posture}",
            })
            logger.info(f"[CC] {sym}: no valid strike (posture={posture}, {why})")
            continue

        qty = lot["uncovered_lots"]
        # Marketable sell limit: undercut the mid (mirrors the BTC side padding ×1.05
        # to fill) so the call actually fills instead of resting at mid and leaving the
        # lot silently uncovered. Never quote below the bid; floor at a penny. Wide
        # spreads are already filtered out upstream by is_valid.
        sell_limit = max(round(pick.get("bid") or 0.0, 2),
                         round(pick["premium"] - COVERED_CALLS.get("sell_undercut", 0.05), 2),
                         0.01)
        rec = {
            "submitted_at": datetime.datetime.utcnow().isoformat() + "Z",
            "symbol": sym, "occ": pick["occ"], "strategy": "CC", "side": "sell",
            "strike": pick["strike"], "expiration": pick["expiration"], "dte": pick["dte"],
            "qty": qty, "limit_price": sell_limit,
            "delta": pick.get("delta"),
            "monthly_return_pct": round(pick["monthly_return"] * 100, 2),
            "posture": posture, "posture_reason": why, "engine": pick.get("engine", "rules"),
            "effective_basis": lot["effective_basis"], "current_price": lot["current_price"],
            "agent_confidence": pick.get("agent_confidence"),
        }
        if not execute:
            rec["dry_run"] = True
            result["opened"].append(rec)
            logger.info(
                f"[CC] {'DRY' if dry_run else mode.upper()}: would sell {sym} "
                f"{qty}×${pick['strike']:.2f}C ({pick['expiration']}) @ ${rec['limit_price']:.2f} "
                f"[{posture}/{rec['engine']}, {rec['monthly_return_pct']:.1f}%/mo]"
            )
            continue
        if pending.has(pick["occ"], "sell"):
            pending.skip_duplicate(pick["occ"], "sell", context="CC open")
            result["skipped"].append({"symbol": sym,
                                      "reason": "duplicate — sell order already resting/submitted"})
            continue
        try:
            order = trader.submit_option_limit(
                occ_symbol=pick["occ"], qty=qty, side="sell", limit_price=rec["limit_price"],
            )
            rec["alpaca_order_id"] = order.get("id")
            rec["status"] = order.get("status")
            result["opened"].append(rec)
            state.setdefault("cc_orders", []).append(rec)
            pending.record(pick["occ"], "sell", qty)
            logger.info(
                f"[CC] sold {sym} {qty}×${pick['strike']:.2f}C ({pick['expiration']}) "
                f"@ ${rec['limit_price']:.2f} → {order.get('status')} "
                f"[{posture}/{rec['engine']}, {rec['monthly_return_pct']:.1f}%/mo]"
            )
        except Exception as e:
            result["skipped"].append({"symbol": sym, "reason": f"order failed: {e}"})
            logger.error(f"[CC] {sym} sell order failed: {e}")

    return result
