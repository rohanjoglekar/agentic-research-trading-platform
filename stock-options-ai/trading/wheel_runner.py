"""Wheel runner — manages existing CSPs and opens new ones from the daily scan.

Run order each cron tick:
  1. MANAGE existing positions
     - Buy-to-close winners at ≥ close_at_profit_pct of max profit (capture
       most of the premium, free the collateral)
     - Losers: the rule-based baseline buys back at close_at_loss_multiplier ×
       the entry premium unless the stock is one the book is prepared to own,
       which it rides toward assignment. With the position agent on
       (AGENT["mode"]), positions in its gray zone are its call, inside the
       kill switch and the roll guardrails (trading/agentic_manager.py)
  1.5 COVERED CALLS on assigned shares (trading/covered_call_runner.py)
  2. OPEN new CSPs from the grouped scan in rank order, sized by conviction
     (RISK.qty_by_rating), until free cash reaches the reserve floor.

The reserve floor is the gate on new opens (trading/reserve.py): free cash is
cash minus the collateral of open short puts, and the floor moves with the
volatility regime. There is no cap on the number of positions.
Multiple contracts on the same strike are supported (qty by rating).
"""
from __future__ import annotations

import datetime
import json
import math
import os
import re
import sys
import time
from pathlib import Path

from loguru import logger

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from config.settings import RISK, AGENT, COVERED_CALLS, RESERVE
from trading.alpaca_executor import AlpacaTrader, build_occ
from trading.reserve import resolve_floor
from data.market_regime import classify_regime


STATE_PATH = PROJECT_ROOT / "trader_state.json"
GROUPED_PATH = PROJECT_ROOT / "scan_results_grouped_latest.json"
RESEARCH_DIR = PROJECT_ROOT / "research_cache"

# Wheel-candidate thresholds — the VALUES live in config.settings.RISK (house
# rule: no risk knobs hardcoded in scripts). Module-level names kept for
# existing importers (covered_call_runner, tests).
WHEEL_CANDIDATE_MIN_WOULD_OWN = int(RISK["wheel_candidate_min_would_own"])
WHEEL_CANDIDATE_BAD_TAGS = set(RISK["wheel_candidate_bad_tags"])


# ─── State helpers ──────────────────────────────────────────────────────────

def load_state() -> dict:
    if STATE_PATH.exists():
        with open(STATE_PATH) as f:
            return json.load(f)
    return {"orders": [], "closes": [], "last_run": None}


def save_state(state: dict) -> None:
    state["last_run"] = datetime.datetime.utcnow().isoformat() + "Z"
    state.setdefault("closes", [])
    with open(STATE_PATH, "w") as f:
        json.dump(state, f, indent=2, default=str)


# ─── Discord notify ─────────────────────────────────────────────────────────

def notify_discord(msg: str) -> None:
    bot_token = os.environ.get("DISCORD_BOT_TOKEN", "").strip()
    channel_id = os.environ.get("DISCORD_CHANNEL_ID", "").strip()
    if not bot_token or bot_token.startswith("your_") or not channel_id:
        return
    try:
        import requests
        requests.post(
            f"https://discord.com/api/v10/channels/{channel_id}/messages",
            headers={"Authorization": f"Bot {bot_token}", "Content-Type": "application/json"},
            json={"content": msg[:1950]}, timeout=10,
        )
    except Exception as e:
        logger.warning(f"Discord notify failed: {e}")


# ─── OCC symbol parsing ─────────────────────────────────────────────────────

def occ_to_ticker(occ: str) -> str:
    """Extract underlying ticker from an OCC option symbol."""
    # OCC = TICKER + YYMMDD + C/P + 8-digit strike. Strip last 15 chars to get ticker.
    if len(occ) < 16:
        return occ
    return occ[:-15]


def occ_parse(occ: str) -> dict | None:
    """Parse OCC like HOOD260618P00070000 → {ticker, expiration, type, strike}."""
    try:
        if len(occ) < 16:
            return None
        ticker = occ[:-15]
        yymmdd = occ[-15:-9]
        cp = occ[-9]
        strike = int(occ[-8:]) / 1000
        exp = f"20{yymmdd[:2]}-{yymmdd[2:4]}-{yymmdd[4:6]}"
        return {"ticker": ticker, "expiration": exp, "type": "P" if cp == "P" else "C", "strike": strike}
    except Exception:
        return None


# ─── Safe parsing ───────────────────────────────────────────────────────────

def _safe_qty(v, context: str = "") -> int | None:
    """Parse an Alpaca qty string defensively (they can be fractional or garbage).

    Returns None — after a logger.warning — instead of letting one bad record
    abort the whole run with an unhandled ValueError.
    """
    try:
        return int(float(v))
    except (TypeError, ValueError):
        logger.warning(f"[Trader] unparseable qty {v!r}"
                       f"{f' ({context})' if context else ''} — skipping record")
        return None


# ─── Pending-intent ledger (duplicate-order guard) ─────────────────────────

class PendingLedger:
    """Dedup ledger for order submissions: {(OCC-or-symbol, side): qty}.

    Snapshot the broker's resting open orders ONCE at the top of a run, then
    record every order submitted during the run. Every closing submit path
    (manage_open_positions, the VIX-extreme sweep, short-call management, CC
    close paths, agentic close/roll) asks ``has(symbol, side)`` first — if a
    matching order is already resting at the broker OR was already submitted
    this run, the caller skips instead of stacking a duplicate. This is the
    fix for the closed-market bug where the same BTC stacked 3× and flipped
    short positions long at the next open.
    """

    def __init__(self, trader=None):
        self.pending: dict[tuple[str, str], int] = {}
        self.deduped: int = 0
        self.degraded: bool = False   # snapshot failed → dedup guard fails CLOSED
        if trader is not None:
            try:
                for o in trader.list_open_orders():
                    sym = (o.get("symbol") or "").upper()
                    side = (o.get("side") or "").lower()
                    qty = _safe_qty(o.get("qty", 0), context=f"open order {sym}")
                    if not sym or not side or not qty:
                        continue
                    self.record(sym, side, abs(qty))
            except Exception as e:
                # One retry, then FAIL CLOSED: with no view of resting orders the
                # dedup guard can't tell a fresh close from a duplicate — has()
                # answers True for everything this run (skip closes, log loudly)
                # rather than risk stacking BTCs that flip a short position long.
                try:
                    for o in trader.list_open_orders():
                        sym = (o.get("symbol") or "").upper()
                        side = (o.get("side") or "").lower()
                        qty = _safe_qty(o.get("qty", 0), context=f"open order {sym}")
                        if sym and side and qty:
                            self.record(sym, side, abs(qty))
                except Exception:
                    self.degraded = True
                    logger.error(f"[Ledger] open-orders snapshot failed twice ({e}) — "
                                 f"dedup guard DEGRADED: all closing submits skipped this run")

    def has(self, symbol: str, side: str) -> bool:
        if self.degraded:
            return True   # fail closed: treat every close as already-pending
        return self.pending.get(((symbol or "").upper(), (side or "").lower()), 0) > 0

    def record(self, symbol: str, side: str, qty: int) -> None:
        key = ((symbol or "").upper(), (side or "").lower())
        self.pending[key] = self.pending.get(key, 0) + int(qty)

    def skip_duplicate(self, symbol: str, side: str, context: str = "") -> None:
        self.deduped += 1
        logger.info(f"[Ledger] skipping duplicate {side} {symbol} — matching order already "
                    f"resting or submitted this run{f' ({context})' if context else ''}")


# ─── Pricing ────────────────────────────────────────────────────────────────

def _limit_from_strike(strike_row: dict) -> float | None:
    p = strike_row.get("Premium")
    try:
        p = float(p)
    except (TypeError, ValueError):
        return None
    if p <= 0:
        return None
    return round(p, 2)


def _qty_for_rating(rating: str) -> int:
    """Conviction-based contract sizing. A → 3, B → 2, C → 1, D → 0.
    UNKNOWN ratings size to 0 (fail closed): a '?' from a scan-schema drift must
    not quietly open positions at 1 contract."""
    qty = RISK.get("qty_by_rating", {}).get(rating)
    if qty is None:
        logger.warning(f"[Trader] unknown rating {rating!r} — sizing 0 (fail-closed)")
        return 0
    return int(qty)


_OCC_RE = re.compile(r"^([A-Z.]{1,6})(\d{6})([CP])(\d{8})$")


def committed_put_collateral(positions: list, open_orders: list) -> float:
    """Cash already promised to short puts → Σ strike × 100 × contracts.

    Counts open short-put positions plus the unfilled part of resting sell-to-open
    put orders. Broker `cash` still includes this money (it only leaves when a put
    is assigned), so a reserve measured against raw cash counts it as free. That is
    what let the wheel write $68,850 of puts against $50,702 of cash (2026-11-20, a
    4x margin account) while its own log said $36,000 was "deployable" — and why the
    options-BP preflight blocked 4-14 orders every day.

    Assigned shares are not counted: their cash is already spent and out of `cash`.
    Short calls are collateralized by shares, long options by nothing. Raises on a
    record it cannot read, so the caller fails safe (no opens) instead of under-
    counting."""
    total = 0.0
    for p in positions or []:
        m = _OCC_RE.match((p.get("symbol") or "").upper())
        if not m or m.group(3) != "P":
            continue
        qty = float(p.get("qty"))
        if qty < 0:
            total += int(m.group(4)) / 1000 * 100 * -qty
    for o in open_orders or []:
        m = _OCC_RE.match((o.get("symbol") or "").upper())
        if not m or m.group(3) != "P" or o.get("side") != "sell":
            continue
        if (o.get("position_intent") or "sell_to_open") != "sell_to_open":
            continue
        left = float(o.get("qty") or 0) - float(o.get("filled_qty") or 0)
        if left > 0:
            total += int(m.group(4)) / 1000 * 100 * left
    return total


def _max_qty_under_buffer(strike: float, cash_available: float) -> int:
    """How many contracts of this strike can we sell while keeping cash buffer?

    cash_available is already net of the buffer floor.
    Each contract reserves strike*100 cash collateral.
    """
    if strike <= 0:
        return 0
    return max(0, int(cash_available // (strike * 100)))


# ─── BLOCK-ONLY safety gates (added 2026-07-07) ──────────────────────────────

def _is_broker_rejection(exc: Exception) -> bool:
    """True when `exc` is an order the BROKER rejected (POSTed, 4xx from Alpaca).

    AlpacaTrader._post raises RuntimeError("Alpaca <status> on POST ...: <body>")
    on any HTTP >= 400. A 4xx (400-orders like insufficient BP, invalid symbol,
    422 wash-trade, 403) is a genuine broker rejection of a submitted order; a
    5xx / network / other exception is an UNEXPECTED error. We classify on the
    message so the run-summary can distinguish broker_rejected from
    unexpected_errors. Conservative: anything we can't positively identify as a
    4xx broker rejection counts as an unexpected error.
    """
    msg = str(exc)
    if "on POST" not in msg or "Alpaca" not in msg:
        return False
    # Pull the numeric status after "Alpaca ".
    import re
    m = re.search(r"Alpaca\s+(\d{3})\b", msg)
    if not m:
        return False
    status = int(m.group(1))
    return 400 <= status < 500


def _available_options_bp(account: dict, equity: float) -> tuple[float, float]:
    """Return (available_options_bp, buffer_usd) for the CSP buying-power preflight.

    A cash-secured put is collateralized by the broker's OPTIONS buying power,
    which may be lower than raw cash. We read ``options_buying_power`` first,
    fall back to ``buying_power``, then to ``cash`` if neither is present. The
    buffer we hold back = max(options_bp_buffer_usd, options_bp_buffer_pct×equity).

    This is a READ-ONLY helper — it can only ever cause an order to be SKIPPED,
    never placed or enlarged. On any parse failure the caller's try/except treats
    it as a gate error and blocks (fail-safe).
    """
    def _f(key):
        v = account.get(key)
        if v is None or v == "":
            return None
        try:
            return float(v)
        except (TypeError, ValueError):
            return None

    avail = _f("options_buying_power")
    if avail is None:
        avail = _f("buying_power")
    if avail is None:
        avail = _f("cash")
    if avail is None or not math.isfinite(avail):
        # No usable/finite BP figure (missing, NaN, or a bogus "inf" string) →
        # treat as zero available so the preflight BLOCKS rather than fails open.
        avail = 0.0
    buffer_usd = max(
        float(RISK.get("options_bp_buffer_usd", 50)),
        float(RISK.get("options_bp_buffer_pct", 0.0)) * float(equity or 0.0),
    )
    return float(avail), float(buffer_usd)


def _check_required_feed_freshness() -> tuple[bool, str | None]:
    """Check the ages of the REQUIRED feeds the OPEN step consumes.

    run_wheel reads only two on-disk feeds for opening new CSPs:
      • the grouped options scan (GROUPED_PATH) — SLA = RISK.max_scan_age_hours
      • the Ollama research cache (RESEARCH_DIR)  — SLA = RISK.research_cache_max_age_h
        (the freshest file in the dir; only gated when the dir has files, since
        the wheel-candidate lookup treats missing research as neutral, not stale)

    Returns (is_stale, stale_feed_name). The scan file's absence/staleness is
    ALREADY handled upstream (no_scan_results / stale_scan); this consolidates
    the required-feed freshness SLA and, critically, adds the research-cache SLA.
    Fail-safe is the CALLER's responsibility — this only reports.
    """
    now = time.time()
    # 1) Grouped scan freshness (required — drives every strike/premium).
    try:
        max_scan_age_h = float(RISK.get("max_scan_age_hours", 20))
        if GROUPED_PATH.exists():
            scan_age_h = (now - GROUPED_PATH.stat().st_mtime) / 3600.0
            if scan_age_h > max_scan_age_h:
                return True, f"scan ({scan_age_h:.1f}h > {max_scan_age_h:.0f}h)"
    except OSError:
        # Can't stat → let the caller's fail-safe decide; report as stale.
        return True, "scan (stat failed)"

    # 2) Research cache freshness (required for the wheel-candidate quality tag).
    #    Only enforce when the cache directory actually has files — an empty/
    #    absent cache is "no coverage" (neutral), not a stale feed.
    try:
        max_res_age_h = float(RISK.get("research_cache_max_age_h", 48))
        if RESEARCH_DIR.exists():
            mtimes = [p.stat().st_mtime for p in RESEARCH_DIR.glob("*.json")]
            if mtimes:
                res_age_h = (now - max(mtimes)) / 3600.0
                if res_age_h > max_res_age_h:
                    return True, f"research_cache ({res_age_h:.1f}h > {max_res_age_h:.0f}h)"
    except OSError:
        return True, "research_cache (stat failed)"

    return False, None


def _sector_bucket(sector: str, ticker: str) -> str:
    """Concentration-cap bucket key for a position/candidate.

    Real sectors bucket together (so the 30% sector cap diversifies as intended).
    But an *unknown* sector must NOT lump every unrelated name into one giant
    "Unknown" bucket — that silently caps TOTAL deployment at the sector limit.
    Unknowns are bucketed per-ticker instead; the per-position cap
    (``max_position_pct``) still bounds each name.
    """
    s = (sector or "").strip()
    if not s or s.lower() in ("unknown", "n/a", "none", "nan"):
        return f"_ticker:{ticker}"
    return s


# ─── Wheel-candidate test ───────────────────────────────────────────────────

def _wheel_candidate_lookup() -> dict:
    """Build {TICKER: {'would_own': int|None, 'quality_tag': str|None}} from
    today's grouped scan + Ollama research cache."""
    info: dict[str, dict] = {}
    if GROUPED_PATH.exists():
        try:
            for s in json.loads(GROUPED_PATH.read_text()):
                t = (s.get("symbol") or "").upper()
                if t:
                    info[t] = {"would_own": s.get("would_own_score"),
                               "quality_tag": None}
        except Exception as e:
            logger.warning(f"[Manager] couldn't read grouped scan: {e}")
    if RESEARCH_DIR.exists():
        for p in RESEARCH_DIR.glob("*.json"):
            try:
                d = json.loads(p.read_text())
                t = (d.get("symbol") or p.stem).upper()
                info.setdefault(t, {"would_own": None, "quality_tag": None})
                info[t]["quality_tag"] = d.get("quality_tag")
            except Exception:
                pass
    return info


def _is_wheel_candidate(ticker: str, lookup: dict) -> tuple:
    """Returns (is_candidate, reason).

    A position is a wheel candidate (hold to assignment) when:
      - AI would_own_score ≥ WHEEL_CANDIDATE_MIN_WOULD_OWN, AND
      - Ollama quality_tag NOT in WHEEL_CANDIDATE_BAD_TAGS

    If either signal is missing, default to "yes" (be conservative — don't
    eject from a position just because we lost the recent scan data).
    """
    rec = lookup.get(ticker.upper(), {})
    wo = rec.get("would_own")
    tag = (rec.get("quality_tag") or "").upper()

    if tag in WHEEL_CANDIDATE_BAD_TAGS:
        return False, f"quality_tag={tag} (not a wheel candidate)"
    if wo is not None and wo < WHEEL_CANDIDATE_MIN_WOULD_OWN:
        return False, f"would_own={wo} below {WHEEL_CANDIDATE_MIN_WOULD_OWN} threshold"
    return True, f"would_own={wo or '?'}, tag={tag or '?'}"


# ─── Position management (close & roll) ─────────────────────────────────────

def manage_open_positions(trader: AlpacaTrader, state: dict, dry_run: bool = False,
                          pending: PendingLedger | None = None) -> list:
    """Buy-to-close winners (≥ profit threshold) and losers (≥ loss threshold)
    the book is not prepared to own. These rules never roll a position; rolling
    is only ever the position agent's decision.

    When AGENT["mode"] is "advisory" or "full_execute", this delegates to the
    agentic manager, which uses these same deterministic thresholds as its
    baseline/fallback for positions outside the gray zone.
    """
    if pending is None:
        pending = PendingLedger(trader)
    mode = AGENT.get("mode", "off")
    if mode in ("advisory", "full_execute"):
        from trading.agentic_manager import manage_positions_agentic
        return manage_positions_agentic(trader, state, dry_run=dry_run, mode=mode,
                                        pending=pending)

    closed: list[dict] = []
    profit_pct = float(RISK.get("close_at_profit_pct", 0.80))
    loss_mult = float(RISK.get("close_at_loss_multiplier", 2.5))
    kill_mult = float(AGENT["kill_switch_loss_mult"])
    candidates = _wheel_candidate_lookup()

    for p in trader.list_positions():
        sym = p.get("symbol", "")
        if len(sym) < 16:
            continue  # not an OCC
        try:
            qty = int(p.get("qty", 0))  # negative for short
            avg = float(p.get("avg_entry_price", 0))
            cur = float(p.get("current_price", 0))
        except (TypeError, ValueError):
            continue
        if qty >= 0 or avg <= 0 or cur <= 0:
            continue  # only manage short option positions

        # For a short put: we sold at avg, profit when current_price < avg
        # profit_captured_pct = 1 - (cur/avg) — at 1.0, premium worth $0
        captured = 1.0 - (cur / avg)
        meta = occ_parse(sym) or {}
        ticker = meta.get("ticker", "")
        if meta.get("type") == "C":
            continue  # short calls are handled by the covered-call leg, not here

        reason = None
        if cur >= avg * kill_mult:
            # Kill switch at the same threshold as the agentic path's
            # apply_guardrails. In this rule-only mode wheel candidacy cannot
            # override it; the agentic path may instead ride a fresh candidate
            # through to assignment (AGENT["kill_switch_ride"]).
            reason = (f"KILL-SWITCH force-close (premium {cur/avg:.1f}× entry "
                      f"≥ {kill_mult}×)")
        elif captured >= profit_pct:
            reason = f"profit-take ({captured*100:.0f}% of max profit captured)"
        elif cur >= avg * loss_mult:
            # Loss threshold tripped — but check wheel candidacy FIRST.
            # If this is a stock we'd happily own, ride it to assignment
            # instead of locking in a realized loss.
            keep, why = _is_wheel_candidate(ticker, candidates)
            if keep:
                logger.info(
                    f"[Manager] {sym} hit loss threshold (premium {cur/avg:.1f}×) "
                    f"but holding — wheel candidate ({why})"
                )
                continue
            reason = f"loss-cut (premium {cur/avg:.1f}× entry; not wheel-worthy — {why})"

        if not reason:
            continue

        if pending.has(sym, "buy"):
            pending.skip_duplicate(sym, "buy", context=reason)
            continue

        # Buy-to-close with a generous limit (just above current ask)
        limit = round(cur * 1.05 + 0.01, 2)
        close_record = {
            "submitted_at": datetime.datetime.utcnow().isoformat() + "Z",
            "symbol": sym,
            "ticker": meta.get("ticker"),
            "strike": meta.get("strike"),
            "expiration": meta.get("expiration"),
            "qty": abs(qty),
            "side": "buy",
            "limit_price": limit,
            "entry_premium": avg,
            "exit_premium_est": cur,
            "reason": reason,
            "captured_pct": round(captured * 100, 1),
            "dry_run": dry_run,
        }
        if dry_run:
            closed.append(close_record)
            logger.info(f"[Manager] DRY: would close {sym} @ ${limit} — {reason}")
            continue

        try:
            order = trader.submit_option_limit(occ_symbol=sym, qty=abs(qty),
                                                side="buy", limit_price=limit)
            close_record["alpaca_order_id"] = order.get("id")
            close_record["status"] = order.get("status")
            closed.append(close_record)
            state.setdefault("closes", []).append(close_record)
            pending.record(sym, "buy", abs(qty))
            logger.info(f"[Manager] closing {sym} qty {abs(qty)} @ ${limit} — {reason}")
        except Exception as e:
            logger.error(f"[Manager] close {sym} failed: {e}")

    return closed


# ─── Speculative close (VIX-extreme emergency) ─────────────────────────────

def _close_speculative_positions(trader: AlpacaTrader, state: dict,
                                  dry_run: bool = False,
                                  pending: PendingLedger | None = None) -> list:
    """Force-close positions on stocks tagged SPECULATIVE/LEVERAGED_PROXY/DISTRESSED.
    Triggered only when VIX is extreme (>=45)."""
    if pending is None:
        pending = PendingLedger(trader)
    candidates = _wheel_candidate_lookup()
    closed = []
    for p in trader.list_positions():
        sym = p.get("symbol", "")
        if len(sym) < 16:
            continue
        meta = occ_parse(sym) or {}
        ticker = meta.get("ticker", "")
        if meta.get("type") == "C":
            continue  # don't force-buy-back covered calls; the CC leg manages them
        keep, why = _is_wheel_candidate(ticker, candidates)
        if keep:
            continue  # let blue-chips ride
        try:
            qty = abs(int(p.get("qty", 0)))
            cur = float(p.get("current_price", 0))
        except (TypeError, ValueError):
            continue
        if qty == 0 or cur <= 0:
            continue
        # Dedup vs resting orders AND closes already submitted earlier this run
        # (manage_open_positions may have closed the same position this tick).
        if pending.has(sym, "buy"):
            pending.skip_duplicate(sym, "buy", context="VIX-extreme close")
            continue
        limit = round(cur * 1.10 + 0.02, 2)  # pay up to close in panic
        rec = {
            "submitted_at": datetime.datetime.utcnow().isoformat() + "Z",
            "symbol": sym, "ticker": ticker,
            "strike": meta.get("strike"), "expiration": meta.get("expiration"),
            "qty": qty, "side": "buy", "limit_price": limit,
            "reason": f"VIX-extreme close (non-wheel: {why})",
            "dry_run": dry_run,
        }
        if dry_run:
            closed.append(rec); continue
        try:
            order = trader.submit_option_limit(occ_symbol=sym, qty=qty,
                                                side="buy", limit_price=limit)
            rec["alpaca_order_id"] = order.get("id")
            rec["status"] = order.get("status")
            closed.append(rec)
            state.setdefault("closes", []).append(rec)
            pending.record(sym, "buy", qty)
        except Exception as e:
            logger.error(f"[VIX-close] {sym} failed: {e}")
    return closed


# ─── Open new positions ─────────────────────────────────────────────────────

def run_wheel(dry_run: bool = False) -> dict:
    """Manage existing, then open new CSPs until cash buffer hit. Conviction-sized."""
    trader = AlpacaTrader()
    state = load_state()

    # ─── Step -1: market-clock gate ─────────────────────────────────────────
    # A full run on a closed day (holiday/weekend/off-hours) queues stale-priced
    # orders. When the market is closed, do the one thing that IS useful —
    # reconcile local order statuses — and stop.
    market_open = None
    clock_error: str | None = None
    try:
        market_open = bool(trader.clock().get("is_open"))
    except Exception as e:
        # FAIL SAFE: an unknown market state must not authorize NEW opens — the
        # OPEN step is degraded below (management + reconcile still run) instead
        # of the old behavior of assuming open and trading at full size.
        clock_error = f"market_clock: {e}"
        logger.warning(f"[Trader] market clock check failed ({e}) — "
                       f"blocking OPEN step (fail-safe); management still runs")
    if market_open is False and RISK.get("require_market_open", True):
        reconcile_order_statuses(state, save=True)
        logger.info("[Trader] market closed — skipping run")
        _log_run_summary(market_open=False, skipped_reason="market_closed")
        return {"closed": 0, "placed": 0, "skipped": 0, "cc_opened": 0,
                "cc_closed": 0, "errors": [], "market_open": False,
                "skipped_reason": "market_closed",
                "risk_blocked": 0, "broker_rejected": 0, "freshness_blocked": None,
                "unexpected_errors": 0, "gate_error": None}

    # A missing or unreadable scan file degrades EXACTLY like a stale one: it
    # gates NEW opens only. Management, the VIX sweep, the CC leg and the
    # kill-switch run regardless — the old early-return here silently disabled
    # ALL position management (kill-switch included) whenever the file vanished
    # or was unreadable.
    grouped: list = []
    scan_gate: str | None = None
    if not GROUPED_PATH.exists():
        logger.error(f"No grouped scan output at {GROUPED_PATH}; run daily_scan "
                     f"first — opens blocked, management still runs.")
        scan_gate = "scan (missing)"
    else:
        try:
            with open(GROUPED_PATH) as f:
                grouped = json.load(f)
        except Exception as e:
            logger.error(f"[Trader] grouped scan unreadable ({e}) — "
                         f"opens blocked, management still runs.")
            scan_gate = "scan (unreadable)"

    # ─── BLOCK-ONLY freshness gate ──────────────────────────────────────────
    # An old/stale REQUIRED feed must not drive NEW opens. Management of
    # existing positions below still runs — closing/rolling does NOT depend on
    # these feeds and is NEVER freshness-blocked. FAIL SAFE: if the gate itself
    # errors, DEGRADE the OPEN step (treat as stale) and record gate_error —
    # never fall through to placing an order.
    #   stale_scan          → whether the OPEN step is degraded to dry_run
    #   freshness_blocked   → None | stale-feed name (run-summary contract)
    #   gate_error          → None | the first gate's own exception message
    stale_scan = False
    freshness_blocked: str | None = None
    gate_error: str | None = None
    try:
        stale_scan, freshness_blocked = _check_required_feed_freshness()
        if stale_scan:
            logger.warning(f"[Trader] required feed stale: {freshness_blocked} — "
                           f"degrading the OPEN step to compute-but-don't-place; "
                           f"management still runs")
    except Exception as e:
        # Gate itself failed → block opens (fail safe), surface the error.
        stale_scan = True
        freshness_blocked = "gate_error"
        gate_error = f"freshness_gate: {e}"
        logger.error(f"[Trader] freshness gate errored ({e}) — blocking OPEN step (fail-safe)")
    # Fold the scan-file and market-clock gates into the same block-opens path.
    if scan_gate and not stale_scan:
        stale_scan, freshness_blocked = True, scan_gate
    if clock_error and not stale_scan:
        stale_scan, freshness_blocked = True, "market_clock"
    if clock_error:
        gate_error = gate_error or clock_error

    # Reconcile stale local statuses (filled/expired/cancelled) so the
    # dashboard shows reality, not the status at submission time.
    reconcile_order_statuses(state, save=False)

    # Pending-intent ledger: snapshot resting open orders ONCE, then record
    # every submit this run. All closing paths consult it, so one tick can
    # never stack duplicate buy-to-closes on the same contract.
    pending = PendingLedger(trader)
    if pending.degraded:
        # The dedup guard failed CLOSED (correct — we don't stack orders blind),
        # which means EVERY close is skipped this run, INCLUDING the kill-switch.
        # That must be loud + machine-readable, not a silent healthy-looking run.
        gate_error = ((gate_error + " | pending_ledger_degraded")
                      if gate_error else "pending_ledger_degraded")
        logger.error("[Trader] PendingLedger DEGRADED — all closes (kill-switch "
                     "included) skipped this run; surfacing to run-summary + notify")

    # ─── Step 0: market regime check (VIX gate) ────────────────────────────
    regime = classify_regime()
    logger.info(f"[Trader] regime: {regime['regime']} — {regime['reason']}")
    state["last_regime"] = regime

    # ─── Step 1: manage existing positions ─────────────────────────────────
    closed = manage_open_positions(trader, state, dry_run=dry_run, pending=pending)
    if closed:
        logger.info(f"[Trader] closed {len(closed)} position(s) this pass")

    # If VIX is extreme, additionally force-close speculative positions
    if regime["force_close_speculative"]:
        spec_closed = _close_speculative_positions(trader, state, dry_run=dry_run,
                                                   pending=pending)
        closed.extend(spec_closed)
        if spec_closed:
            logger.warning(
                f"[Trader] VIX extreme — force-closed {len(spec_closed)} speculative shorts"
            )

    # ─── Step 1.5: covered-call leg on owned/assigned shares ───────────────
    # Closes the wheel: writes income calls on shares we hold (e.g. assigned from
    # a CSP) and buy-to-closes them at a profit. Thesis-tiered, never below the
    # effective cost basis unless the thesis is broken. Independent of the cash
    # buffer (CCs are share-collateralized, not cash-collateralized).
    cc_opened: list[dict] = []
    cc_closed: list[dict] = []
    share_sells: list[dict] = []
    try:
        if COVERED_CALLS.get("enabled", True) and COVERED_CALLS.get("mode", "off") != "off":
            from trading.covered_call_runner import run_covered_calls
            cc_res = run_covered_calls(trader, state, regime=regime, dry_run=dry_run,
                                       pending=pending)
            cc_opened = cc_res.get("opened", [])
            cc_closed = cc_res.get("closed", [])
            share_sells = cc_res.get("share_sells", [])
            if cc_opened or cc_closed or share_sells:
                logger.info(f"[Trader] covered calls — opened {len(cc_opened)}, "
                            f"closed {len(cc_closed)}, share exits {len(share_sells)}")
    except Exception as e:
        logger.error(f"[Trader] covered-call leg failed (non-fatal): {e}")

    # ─── Step 2: refresh account state after closes ────────────────────────
    account = trader.account()
    equity = float(account.get("equity", 0))
    cash = float(account.get("cash", 0))
    positions = trader.list_positions()
    open_orders = trader.list_open_orders()
    # Free cash = cash not already promised to short puts. FAIL SAFE: if the
    # collateral can't be counted, nothing is free (blocks opens, never places).
    try:
        committed = committed_put_collateral(positions, open_orders)
    except Exception as e:
        committed = float("inf")
        gate_error = (gate_error + f" | collateral: {e}") if gate_error else f"collateral: {e}"
        logger.error(f"[Trader] couldn't count committed put collateral ({e}) — "
                     "no opens this run (fail-safe)")
    free_cash = cash - committed
    # Adaptive reserve: a regime-scaled free-cash floor that eases slowly toward
    # its target (slow to release capital, fast to rebuild) and never crosses the
    # catastrophe floor. Replaces the old static RISK["min_cash_buffer"].
    res = resolve_floor(state, regime.get("regime", "calm"), RESERVE,
                        RISK.get("min_cash_buffer", 0.20))
    state["cash_floor_pct"] = res["floor_pct"]   # persist smoothing for next run
    buffer_floor = equity * res["floor_pct"]
    deployable_cash = max(0.0, free_cash - buffer_floor)
    state["reserve"] = {**res, "floor_usd": round(buffer_floor, 2),
                        "deployable_usd": round(deployable_cash, 2),
                        "equity": round(equity, 2), "cash": round(cash, 2),
                        "committed_collateral_usd": (round(committed, 2)
                                                     if math.isfinite(committed) else None),
                        "free_cash_usd": (round(free_cash, 2)
                                          if math.isfinite(free_cash) else None)}
    logger.info(f"[Trader] equity=${equity:,.0f}  cash=${cash:,.0f}  "
                f"put collateral=${committed:,.0f}  free cash=${free_cash:,.0f}  "
                f"floor={res['floor_pct']*100:.0f}% (target {res['target_pct']*100:.0f}% "
                f"{res['regime']} · {res['easing']})  buffer_floor=${buffer_floor:,.0f}  "
                f"deployable=${deployable_cash:,.0f}")

    # Tickers already with positions/open orders — don't double-up unless rolling
    have_positions = set()
    for p in positions:
        _q = _safe_qty(p.get("qty", 0), context=f"position {p.get('symbol', '?')}")
        if _q is not None and _q < 0:
            have_positions.add(occ_to_ticker((p.get("symbol") or "").upper()))
    have_open_underlyings = {occ_to_ticker(o.get("symbol", "").upper())
                              for o in open_orders if o.get("side") == "sell"}
    logger.info(f"[Trader] held: {sorted(have_positions)}")
    logger.info(f"[Trader] open sell orders on: {sorted(have_open_underlyings)}")

    placed: list[dict] = []
    skipped: list[dict] = []
    errors: list[str] = []
    # ─── Run-summary counters (read by the audit job) ───────────────────────────
    risk_blocked: list[dict] = []      # orders skipped pre-submit by the BP gate
    broker_rejected = 0                 # orders POSTed that the broker actually rejected
    unexpected_errors = 0              # submit errors that are NOT broker rejections
    max_per_position = equity * float(RISK.get("max_position_pct", 0.10))
    max_sector_pct = float(RISK.get("max_sector_concentration", 0.30))

    # ─── BLOCK-ONLY buying-power preflight tally ────────────────────────────
    # Read the broker's OPTIONS buying power (fallback: buying_power → cash) and
    # hold back a small buffer. We decrement this running tally as we place
    # orders in the loop so multiple orders in one run can't oversubscribe BP.
    # FAIL SAFE: if reading BP errors, treat available BP as 0 (blocks all opens)
    # and record gate_error — never fall through to placing unbudgeted orders.
    try:
        options_bp_avail, options_bp_buffer = _available_options_bp(account, equity)
        available_bp = max(0.0, options_bp_avail - options_bp_buffer)
        logger.info(f"[Trader] options BP=${options_bp_avail:,.0f} "
                    f"(buffer ${options_bp_buffer:,.0f}) → deployable BP=${available_bp:,.0f}")
    except Exception as e:
        available_bp = 0.0
        gate_error = (gate_error + f" | bp_gate: {e}") if gate_error else f"bp_gate: {e}"
        logger.error(f"[Trader] options-BP preflight errored ({e}) — "
                     f"available BP set to 0 (fail-safe, blocks opens)")

    # ─── If regime says pause, skip opens entirely ─────────────────────────
    if not regime["allow_new_opens"]:
        logger.warning(f"[Trader] new opens PAUSED — {regime['reason']}")
        save_state(state)
        _notify_summary(closed, placed, skipped, errors, equity, regime,
                        cc_opened=cc_opened, cc_closed=cc_closed, reserve=res,
                        share_sells=share_sells)
        _log_run_summary(market_open=market_open, skipped_reason="regime_paused",
                         closed=len(closed),
                         rolled=sum(1 for c in closed if c.get("action") == "ROLL"),
                         deduped=pending.deduped, cc_opened=len(cc_opened),
                         cc_closed=len(cc_closed), errors=len(errors), equity=equity,
                         risk_blocked=len(risk_blocked), broker_rejected=broker_rejected,
                         freshness_blocked=freshness_blocked,
                         unexpected_errors=unexpected_errors, gate_error=gate_error)
        return {"closed": len(closed), "placed": 0, "skipped": 0,
                "cc_opened": len(cc_opened), "cc_closed": len(cc_closed),
                "share_sells": len(share_sells),
                "errors": errors, "regime": regime,
                "market_open": market_open, "skipped_reason": "regime_paused",
                "risk_blocked": len(risk_blocked), "broker_rejected": broker_rejected,
                "freshness_blocked": freshness_blocked,
                "unexpected_errors": unexpected_errors, "gate_error": gate_error,
                "details": {"closed": closed, "placed": [], "skipped": [],
                            "cc_opened": cc_opened, "cc_closed": cc_closed,
                            "share_sells": share_sells}}

    qty_multiplier = float(regime["qty_multiplier"])  # 1.0 calm, 0.5 caution

    # ─── Sector concentration tracking ─────────────────────────────────────
    # Existing collateral by sector — pull sector from grouped scan
    sector_lookup = {s.get("symbol","").upper(): (s.get("sector") or "Unknown")
                     for s in grouped}
    sector_deployed: dict[str, float] = {}
    for p in positions:
        psym = p.get("symbol", "")
        ticker_p = occ_to_ticker(psym) if len(psym) >= 16 else psym
        sec = _sector_bucket(sector_lookup.get(ticker_p, "Unknown"), ticker_p)
        if len(psym) >= 16:
            # Count ONLY short PUTS at strike collateral. A short CALL is
            # collateralized by shares (already counted in the else-branch as
            # market value) — counting both double-counted a covered/assigned lot
            # and silently halved the sector cap; a long option isn't collateral.
            try:
                qty_raw = int(p.get("qty", 0))
                opt_type = psym[-9]           # OCC: …YYMMDD[C|P]strike(8)
                if opt_type == "P" and qty_raw < 0:
                    strike_p = int(psym[-8:]) / 1000
                    sector_deployed[sec] = sector_deployed.get(sec, 0.0) + strike_p * 100 * abs(qty_raw)
            except Exception:
                pass
        else:
            # Assigned/held SHARES are sector exposure too — count their market
            # value so the concentration cap sees the whole book, and pause fresh
            # CSPs on tickers we already hold ≥1 lot of (assignment used to
            # silently re-open the same name for another round of collateral).
            try:
                qty_s = float(p.get("qty", 0) or 0)
                mv = float(p.get("market_value") or 0) or qty_s * float(p.get("current_price") or 0)
                if qty_s > 0 and mv > 0:
                    sector_deployed[sec] = sector_deployed.get(sec, 0.0) + mv
                if qty_s >= 100:
                    have_positions.add(ticker_p.upper())
            except Exception:
                pass

    # ─── Step 3: open new CSPs in scan-rank order until buffer hit ─────────
    # A stale scan must not open positions off day-old strikes/premiums —
    # skip the OPEN step only (management above already ran).
    open_candidates = [] if stale_scan else grouped
    for stock in open_candidates:
        if deployable_cash <= 0:
            logger.info("[Trader] cash buffer reached — stopping deployment")
            break

        sym = stock.get("symbol", "?").upper()
        strikes = stock.get("strikes") or []
        rating = stock.get("best_rating", "?")
        sector = sector_lookup.get(sym, "Unknown")
        sector_bucket = _sector_bucket(sector, sym)

        if not strikes:
            skipped.append({"symbol": sym, "reason": "no strikes"}); continue
        if sym in have_positions or sym in have_open_underlyings:
            skipped.append({"symbol": sym, "reason": "already open"}); continue
        if rating == "D":
            skipped.append({"symbol": sym, "reason": f"rating {rating} too low"}); continue

        best = strikes[0]
        strike = best.get("Strike")
        expiration = best.get("Expiration")
        limit = _limit_from_strike(best)
        if not strike or not expiration or not limit:
            errors.append(f"{sym}: missing strike/expiration/premium"); continue

        # Sizing: rating × regime multiplier
        target_qty = max(0, int(round(_qty_for_rating(rating) * qty_multiplier)))
        if target_qty == 0:
            skipped.append({"symbol": sym, "reason": f"qty=0 for rating {rating} × regime {qty_multiplier}"})
            continue

        # ─── Sector concentration check ──────────────────────────────────
        # Unknown-sector names are bucketed per-ticker (see _sector_bucket) so
        # they don't all collapse into one "Unknown" bucket that would silently
        # throttle TOTAL deployment to the 30% sector cap.
        sector_now = sector_deployed.get(sector_bucket, 0.0)
        sector_max = equity * max_sector_pct
        sector_room = sector_max - sector_now
        if sector_room <= 0:
            skipped.append({"symbol": sym,
                            "reason": f"sector {sector} at concentration cap "
                                      f"(${sector_now:,.0f} / ${sector_max:,.0f})"})
            continue

        contract_collateral = float(strike) * 100
        max_qty_pos = max(1, int(max_per_position // contract_collateral))
        max_qty_cash = _max_qty_under_buffer(float(strike), deployable_cash)
        max_qty_sector = max(0, int(sector_room // contract_collateral))
        actual_qty = min(target_qty, max_qty_pos, max_qty_cash, max_qty_sector)

        if actual_qty <= 0:
            # Report the TRUE binding constraint rather than always blaming cash.
            binding = min(("cash", max_qty_cash), ("sector", max_qty_sector),
                          ("position", max_qty_pos), key=lambda kv: kv[1])[0]
            if binding == "cash":
                reason = (f"insufficient buffered cash (strike ${strike} × 100 = "
                          f"${contract_collateral:,.0f} > ${deployable_cash:,.0f} deployable)")
            elif binding == "sector":
                reason = (f"sector {sector} room exhausted (${sector_room:,.0f} left "
                          f"< ${contract_collateral:,.0f} per contract)")
            else:
                reason = (f"per-position cap (${max_per_position:,.0f} max "
                          f"< ${contract_collateral:,.0f} per contract)")
            skipped.append({"symbol": sym, "reason": f"can't fit even 1 contract — {reason}"})
            continue

        occ = build_occ(sym, expiration, "put", strike)

        if pending.has(occ, "sell"):
            pending.skip_duplicate(occ, "sell", context="new CSP open")
            skipped.append({"symbol": sym, "reason": "duplicate — sell order already resting/submitted"})
            continue

        # ─── BLOCK-ONLY buying-power preflight ───────────────────────────────
        # A CSP is collateralized by broker OPTIONS buying power, which may be
        # lower than cash. If the required collateral exceeds our remaining
        # (buffered) options BP, SKIP this order and record it as risk_blocked —
        # NOT an error, NOT a broker rejection. Decrement the running tally as we
        # place so multiple orders in one run can't oversubscribe. FAIL SAFE: any
        # exception in this gate blocks the order (records gate_error).
        try:
            required_collateral = float(strike) * 100 * actual_qty
            if required_collateral > available_bp:
                rb = {"symbol": sym, "occ": occ, "strike": float(strike),
                      "qty": actual_qty,
                      "required_collateral": round(required_collateral, 2),
                      "available_bp": round(available_bp, 2),
                      "reason": (f"options BP preflight — need "
                                 f"${required_collateral:,.0f} collateral > "
                                 f"${available_bp:,.0f} available options BP")}
                risk_blocked.append(rb)
                skipped.append({"symbol": sym, "reason": rb["reason"]})
                logger.warning(f"[Trader] BP-BLOCK {sym}: {rb['reason']}")
                continue
        except Exception as e:
            gate_error = (gate_error + f" | bp_gate_loop: {e}") if gate_error else f"bp_gate_loop: {e}"
            skipped.append({"symbol": sym, "reason": f"BP gate error (blocked, fail-safe): {e}"})
            logger.error(f"[Trader] BP gate errored on {sym} ({e}) — blocking order (fail-safe)")
            continue

        # Best strike's POP — needed for the calibration loop.
        _pop_raw = best.get("POP", best.get("pop_estimate"))
        try:
            if isinstance(_pop_raw, str):
                _pop_val = float(_pop_raw.rstrip("%")) / (100 if "%" in _pop_raw else 1)
            else:
                _pop_val = float(_pop_raw) / (100 if float(_pop_raw) > 1.5 else 1)
        except (TypeError, ValueError):
            _pop_val = None

        entry = {
            "submitted_at": datetime.datetime.utcnow().isoformat() + "Z",
            "symbol": sym, "occ": occ,
            "strike": float(strike), "expiration": expiration,
            "qty": actual_qty,
            "target_qty": target_qty,
            "limit_price": limit,
            "rating": rating,
            "score": stock.get("best_score"),
            "predicted_pop": _pop_val,
            "dry_run": dry_run,
        }

        if dry_run:
            placed.append(entry)
            collateral_used = float(strike) * 100 * actual_qty
            deployable_cash -= collateral_used
            available_bp -= collateral_used   # decrement BP tally in dry-run too
            # Mirror the live path so dry-run sector accounting is accurate —
            # otherwise each candidate independently sees full sector room and
            # dry-run over-reports placements.
            sector_deployed[sector_bucket] = sector_deployed.get(sector_bucket, 0.0) + collateral_used
            logger.info(f"[Trader] DRY: would place {sym} {actual_qty}×${strike}P @ ${limit}")
            continue

        try:
            order = trader.submit_option_limit(occ_symbol=occ, qty=actual_qty,
                                                side="sell", limit_price=limit)
            entry["alpaca_order_id"] = order.get("id")
            entry["status"] = order.get("status")
            entry["sector"] = sector
            placed.append(entry)
            state["orders"].append(entry)
            pending.record(occ, "sell", actual_qty)
            collateral_used = float(strike) * 100 * actual_qty
            deployable_cash -= collateral_used
            available_bp -= collateral_used   # decrement BP tally on placement
            sector_deployed[sector_bucket] = sector_deployed.get(sector_bucket, 0.0) + collateral_used
            logger.info(f"[Trader] placed {sym} {actual_qty}×${strike}P "
                        f"({expiration}) @ ${limit} → {order.get('status')}  [sector={sector}]")
        except Exception as e:
            errors.append(f"{sym}: {e}")
            # Distinguish a BROKER REJECTION (order POSTed, broker said no — an
            # Alpaca 4xx from the executor) from an UNEXPECTED error (timeout,
            # bug, connectivity). Both stay in `errors`; the counters split them
            # for the audit job. This block still ONLY records — it never retries
            # or places anything.
            if _is_broker_rejection(e):
                broker_rejected += 1
                logger.error(f"[Trader] {sym} order BROKER-REJECTED: {e}")
            else:
                unexpected_errors += 1
                logger.error(f"[Trader] {sym} order failed (unexpected): {e}")

    save_state(state)
    # Refresh POP calibration after any state change
    try:
        from data.pop_calibration import compute_bias
        bias_info = compute_bias()
        logger.info(f"[Calibration] {bias_info}")
    except Exception as e:
        logger.warning(f"[Calibration] failed: {e}")
    _notify_summary(closed, placed, skipped, errors, equity, regime,
                    cc_opened=cc_opened, cc_closed=cc_closed, reserve=res,
                    share_sells=share_sells)

    skipped_reason = "stale_scan" if stale_scan else None
    # freshness_blocked → the stale feed name (or None/False when fresh); per the
    # contract it is a bool-or-name. gate_error already captured any gate failure.
    _log_run_summary(market_open=market_open, skipped_reason=skipped_reason,
                     closed=len(closed), opened=len(placed),
                     rolled=sum(1 for c in closed if c.get("action") == "ROLL"),
                     deduped=pending.deduped, cc_opened=len(cc_opened),
                     cc_closed=len(cc_closed), errors=len(errors), equity=equity,
                     risk_blocked=len(risk_blocked), broker_rejected=broker_rejected,
                     freshness_blocked=freshness_blocked,
                     unexpected_errors=unexpected_errors, gate_error=gate_error)

    return {
        "closed": len(closed),
        "placed": len(placed),
        "skipped": len(skipped),
        "cc_opened": len(cc_opened),
        "cc_closed": len(cc_closed),
        "share_sells": len(share_sells),
        "errors": errors,
        "regime": regime,
        "market_open": market_open,
        "skipped_reason": skipped_reason,
        # ─── safety-gate run-summary fields (read by the audit job) ──────────────
        "risk_blocked": len(risk_blocked),
        "broker_rejected": broker_rejected,
        "freshness_blocked": freshness_blocked,
        "unexpected_errors": unexpected_errors,
        "gate_error": gate_error,
        "details": {"closed": closed, "placed": placed, "skipped": skipped,
                    "cc_opened": cc_opened, "cc_closed": cc_closed,
                    "share_sells": share_sells,
                    "risk_blocked": risk_blocked},
    }


def _log_run_summary(*, market_open, skipped_reason=None, closed=0, opened=0,
                     rolled=0, deduped=0, cc_opened=0, cc_closed=0, errors=0,
                     equity=None, risk_blocked=0, broker_rejected=0,
                     freshness_blocked=None, unexpected_errors=0,
                     gate_error=None) -> None:
    """Single-line machine-readable heartbeat — emitted on EVERY run, including
    skip paths, so monitoring can tell 'skipped on purpose' from 'crashed'.

    The safety-gate fields (read by the audit job):
      risk_blocked      — int count of orders skipped pre-submit by the BP gate
      broker_rejected   — int count of orders the broker actually rejected
      freshness_blocked — bool/str: False|null when fresh, else the stale feed name
      unexpected_errors — int count of submit errors that are NOT broker rejections
      gate_error        — str|null: a gate's own exception message (fail-safe blocked)
    """
    summary = {
        "ts": datetime.datetime.utcnow().isoformat() + "Z",
        "market_open": market_open,
        "skipped_reason": skipped_reason,
        "closed": closed,
        "opened": opened,
        "rolled": rolled,
        "deduped_skips": deduped,
        "cc_opened": cc_opened,
        "cc_closed": cc_closed,
        "errors": errors,
        "equity": equity,
        # ─── safety-gate fields (read by the audit job) ──────────────────────────
        "risk_blocked": risk_blocked,
        "broker_rejected": broker_rejected,
        "freshness_blocked": freshness_blocked,
        "unexpected_errors": unexpected_errors,
        "gate_error": gate_error,
    }
    logger.info("RUN_SUMMARY " + json.dumps(summary, default=str))
    # Also append a JSONL history the platform's audit job reads (it probes
    # data/wheel_run_summary.jsonl) so gate health + consecutive-block streaks
    # surface in the automated audit. Telemetry-only: NEVER let it break a run.
    try:
        # Env override lets tests redirect this off the real data dir so they
        # never pollute the audit job's gate-health feed.
        hist = Path(os.environ.get("WHEEL_RUN_SUMMARY_PATH")
                    or PROJECT_ROOT / "data" / "wheel_run_summary.jsonl")
        hist.parent.mkdir(parents=True, exist_ok=True)
        lines = []
        if hist.exists():
            lines = hist.read_text().splitlines()[-199:]     # keep newest ~200
        lines.append(json.dumps(summary, default=str))
        tmp = hist.with_suffix(".jsonl.tmp")
        tmp.write_text("\n".join(lines) + "\n")
        tmp.replace(hist)
    except Exception as e:                                    # noqa: BLE001
        logger.warning(f"[Trader] run-summary history write failed (non-fatal): {e}")


def _notify_summary(closed, placed, skipped, errors, equity, regime,
                    cc_opened=None, cc_closed=None, reserve=None, share_sells=None):
    now = datetime.datetime.now().strftime("%b %d %H:%M")
    summary = f"**Trader run — {now}**\n"
    if regime and regime.get("vix") is not None:
        emoji = {"calm":"🟢","caution":"🟡","paused":"🟠","extreme":"🔴"}.get(regime["regime"], "⚪")
        summary += f"{emoji} **{regime['regime'].upper()}** · VIX {regime['vix']:.1f} — {regime['reason']}\n"
    if closed:
        summary += f"🔒 Closed: **{len(closed)}**\n"
        for c in closed[:6]:
            summary += (f"• BTC {c.get('ticker','?')} ${c.get('strike','?')}P "
                        f"qty {c['qty']} @ ${c['limit_price']:.2f} "
                        f"— {c['reason']}\n")
    # Covered-call leg
    cc_opened = cc_opened or []
    cc_closed = cc_closed or []
    share_sells = share_sells or []
    if share_sells:
        summary += f"🛑 Share exits: **{len(share_sells)}**\n"
        for s in share_sells[:4]:
            summary += f"• SOLD {s.get('qty','?')} sh {s.get('symbol','?')} — {s.get('reason','')[:120]}\n"
    if cc_opened or cc_closed:
        summary += f"🟣 Covered calls — sold: **{len(cc_opened)}** · closed: {len(cc_closed)}\n"
        for c in cc_opened[:6]:
            summary += (f"• CC {c.get('symbol','?')} {c.get('qty','?')}×${c.get('strike',0):.2f}C "
                        f"({c.get('expiration','?')}) @ ${c.get('limit_price',0):.2f} "
                        f"[{c.get('posture','?')}/{c.get('engine','rules')}, "
                        f"{c.get('monthly_return_pct',0):.1f}%/mo]\n")
        for c in cc_closed[:4]:
            summary += (f"• BTC-CC {c.get('ticker','?')} ${c.get('strike','?')}C "
                        f"qty {c.get('qty','?')} @ ${c.get('limit_price',0):.2f} — {c.get('reason','')}\n")
    summary += f"📤 Placed: **{len(placed)}** · Skipped: {len(skipped)} · Errors: {len(errors)}\n"
    for p in placed[:10]:
        sec = p.get("sector", "?")
        summary += (f"• {p['symbol']} {p['qty']}×${p['strike']:.2f}P "
                    f"({p['expiration']}) @ ${p['limit_price']:.2f} [{p['rating']} · {sec}]\n")
    floor_pct = (reserve or {}).get("floor_pct", RISK["min_cash_buffer"])
    summary += f"\n💵 Cash reserve floor: ${equity * floor_pct:,.0f} ({floor_pct*100:.0f}% of equity)"
    if reserve and reserve.get("adaptive"):
        summary += (f" · {reserve.get('easing','steady')} → target "
                    f"{reserve.get('target_pct', floor_pct)*100:.0f}% ({reserve.get('regime','?')})")
    if errors:
        summary += "\n\nErrors:\n" + "\n".join(f"  {e}" for e in errors[:5])
    notify_discord(summary)




# ─── Status reconciliation (fetch live alpaca status into local state) ─────

TERMINAL_STATUSES = {"filled", "canceled", "cancelled", "expired",
                     "rejected", "done_for_day", "replaced"}


def reconcile_order_statuses(state: dict | None = None,
                              save: bool = True) -> dict:
    """Update local order records with their live Alpaca status.

    Without this, the dashboard would forever show whatever
    status the order had at submission time (typically ``accepted`` or
    ``pending_new``), even though Alpaca has long since filled/expired/
    cancelled them.

    Returns a dict {changed: int, total: int}.
    """
    if state is None:
        state = load_state()
    try:
        trader = AlpacaTrader()
    except Exception as e:
        logger.warning(f"[Reconcile] AlpacaTrader init failed: {e}")
        return {"changed": 0, "total": 0}

    # CSP opens live in state["orders"]; covered-call opens in state["cc_orders"];
    # buy-to-CLOSE records live in state["closes"] and MUST reconcile too — their
    # debits only count into _banked_premium at status "filled", so leaving them
    # at submit-time status silently overstated banked credit and deflated the
    # covered-call effective-cost-basis floor with every BTC.
    orders = ((state.get("orders", []) or []) + (state.get("cc_orders", []) or [])
              + (state.get("closes", []) or []))
    changed = 0
    for rec in orders:
        local = (rec.get("status") or "").lower()
        if local in TERMINAL_STATUSES:
            continue
        oid = rec.get("alpaca_order_id")
        if not oid:
            continue
        try:
            live = trader._get(f"/v2/orders/{oid}")
            live_status = (live.get("status") or "").lower()
            if live_status and live_status != local:
                rec["status"] = live_status
                rec["filled_at"] = live.get("filled_at") or rec.get("filled_at")
                rec["filled_avg_price"] = live.get("filled_avg_price") or rec.get("filled_avg_price")
                changed += 1
        except Exception as e:
            logger.debug(f"[Reconcile] {oid} fetch failed: {e}")

    if changed and save:
        save_state(state)
        logger.info(f"[Reconcile] updated {changed}/{len(orders)} order statuses")

    return {"changed": changed, "total": len(orders)}

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true",
                        help="Don't submit orders, just preview")
    args = parser.parse_args()
    result = run_wheel(dry_run=args.dry_run)
    print(json.dumps(result, indent=2, default=str))
