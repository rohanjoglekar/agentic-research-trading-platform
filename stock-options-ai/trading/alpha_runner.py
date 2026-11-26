#!/usr/bin/env python3
"""
trading/alpha_runner.py

Live (paper) runner for the long-only, conviction-weighted "beat the S&P 500"
portfolio. Separate Alpaca *paper* account from the wheel, sized by
ALPHA["start_capital"] ($1M).

Pipeline each run (idempotent toward the target book):
  1. Overlay the governor's clamped policy onto the ALPHA defaults (load_alpha_policy).
  2. Load the research ranking (research.json composite scores + factors + DCF).
  3. Read account equity + current positions (Alpaca paper) — or fall back to
     ALPHA["start_capital"] in advisory/dry-run.
  4. Run the SELL-DISCIPLINE layer (take-profit / stop-loss / thesis-invalidation)
     against live positions and force those names out of the target.
  5. Build the target book via strategies.alpha_portfolio.build_target() (the SAME
     construction the backtest validates): small VOO/QQQ anchor + conviction
     satellite + dynamic BIL reserve.
  6. Diff target vs current, apply the drift band + min-trade filter → orders.
  7. Write a state JSON for the dashboard; submit orders only in full_execute.

Mode (config.settings.ALPHA["mode"], mirrors the wheel's AGENT["mode"]):
  • off          — do nothing.
  • advisory     — compute, log, write state. NO orders. (default; --dry-run forces this)
  • full_execute — submit Alpaca *paper* notional orders.

This module never trades options and is long-only. Orders go to a paper
account: full_execute refuses any other host unless ALPHA_ALLOW_LIVE is set by
hand. Run:
    python -m trading.alpha_runner --dry-run
    python -m trading.alpha_runner --research /path/to/research.json
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
import time
from pathlib import Path

from loguru import logger

HERE = Path(__file__).resolve()
SOAI_ROOT = HERE.parents[1]
if str(SOAI_ROOT) not in sys.path:
    sys.path.insert(0, str(SOAI_ROOT))

from config.settings import ALPHA, ALPHA_CLAMPS, ALPHA_GOVERNOR, load_alpha_policy  # noqa: E402
from strategies.alpha_portfolio import RankedName, build_target                     # noqa: E402
from trading import alpha_positions                                                 # noqa: E402

DASH_DATA = SOAI_ROOT.parent / "forge-dashboard" / "data" / "robinhood"

# Style shortlists whose members are always eligible satellite candidates.
_STYLE_SHORTLISTS = ("cheapest", "dcf_upside", "fastest_growth", "top_quality")


# ─── inputs ─────────────────────────────────────────────────────────────────

def _resolve_soai(p: str) -> Path:
    q = Path(p)
    return q if q.is_absolute() else (SOAI_ROOT / q)


def _resolve_research_path(cfg: dict, override: str | None) -> Path | None:
    for cand in [override, cfg.get("research_path"), str(DASH_DATA / "research.json")]:
        if cand and Path(cand).exists():
            return Path(cand)
    return None


def _price_cache_last(sym: str) -> float | None:
    f = DASH_DATA / "prices" / f"{sym.upper()}.json"
    if not f.exists():
        return None
    try:
        closes = json.loads(f.read_text()).get("closes") or []
        return float(closes[-1]) if closes else None
    except Exception:
        return None


def _dcf_trusted(dcf: dict | None) -> bool:
    """True only when the gated DCF is available AND not flagged LOW/ARTIFACT or
    N/A — mirrors rh_factors._dcf_ok so the trader never EXITS a name, up-weights a
    buy, or counts an 'opportunity' on a DCF upside the engine itself says not to
    trust (the artifact G5/G6/G7 gates: implied-WACC, 3× gap, hyper-grower cap)."""
    d = dcf or {}
    return bool(d.get("available") and d.get("confidence") not in ("LOW/ARTIFACT", "N/A", None))


def load_rankings(path: Path, cfg: dict) -> tuple[list, dict]:
    """Return (rankings, prices) from research.json.

    Each RankedName carries the composite score, sector, tier, DCF upside, the
    value/growth/quality factor sub-scores (for conviction weighting + style tags),
    and whether the name sits in any style shortlist (always a candidate). Prices
    come from each name's `last`; the anchor/reserve ETFs are priced separately.
    """
    doc = json.loads(path.read_text())

    shortlisted: set[str] = set()
    for key, names in (doc.get("shortlists") or {}).items():
        if key not in _STYLE_SHORTLISTS:
            continue
        for x in (names or []):
            sym = x if isinstance(x, str) else (x.get("symbol") if isinstance(x, dict) else None)
            if sym:
                shortlisted.add(str(sym).upper())

    rankings, prices = [], {}
    for r in doc.get("stocks", []):
        sym = str(r.get("symbol", "")).upper()
        if not sym:
            continue
        last = r.get("last")
        if last:
            prices[sym] = float(last)
        fac = r.get("factors") or {}
        rankings.append(RankedName(
            symbol=sym,
            score=float(r.get("score") or 0),
            sector=r.get("sector") or "Unknown",
            tier=r.get("tier"),
            dcf_upside=float((r.get("dcf") or {}).get("upside_pct") or 0),
            dcf_trusted=_dcf_trusted(r.get("dcf")),
            value_score=float((fac.get("value") or {}).get("score") or 0),
            growth_score=float((fac.get("growth") or {}).get("score") or 0),
            quality_score=float((fac.get("quality") or {}).get("score") or 0),
            shortlisted=sym in shortlisted,
        ))
    return rankings, prices


def check_research_quality(doc: dict, cfg: dict) -> str | None:
    """Quality gate on research.json before any order is trusted to it.

    A corrupted upstream fundamentals cache degrades the composite to momentum-only
    scores — this catches that. Returns a short detail string when the doc is
    degraded (stale, or too few confident rows), None when it is trustworthy.
    "Usable" row = per-row `confidence` (0-1) >= research_min_confidence; older
    payloads without the field fall back to factors.composite.fundamental_coverage.
    """
    max_age_h = float(cfg.get("research_max_age_hours", 36))
    min_conf = float(cfg.get("research_min_confidence", 0.5))
    min_usable = int(cfg.get("research_min_usable", 15))

    gen = doc.get("generated_at")
    if not gen:
        return "missing generated_at"
    try:
        ts = dt.datetime.fromisoformat(str(gen).replace("Z", "+00:00"))
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=dt.timezone.utc)
    except ValueError:
        return f"unparseable generated_at: {gen}"
    age_h = (dt.datetime.now(dt.timezone.utc) - ts).total_seconds() / 3600.0
    if age_h > max_age_h:
        return f"stale: {age_h:.0f}h old > {max_age_h:.0f}h max"

    usable = 0
    for r in doc.get("stocks", []):
        conf = r.get("confidence")
        if conf is None:   # older payloads: proxy = fundamental coverage of the composite
            conf = ((r.get("factors") or {}).get("composite") or {}).get("fundamental_coverage")
        try:
            if conf is not None and float(conf) >= min_conf:
                usable += 1
        except (TypeError, ValueError):
            continue
    if usable < min_usable:
        return f"{usable} usable rows (confidence >= {min_conf:g}) < {min_usable} required"
    return None


def _ensure_etf_prices(cfg: dict, prices: dict, trader, detail: list | None = None) -> None:
    """Fill in the anchor (VOO/QQQ), reserve (BIL) and benchmark (SPY) prices that
    research.json doesn't carry: price-cache first, then Alpaca's latest trade (free
    IEX feed on paper), then — for a HELD ETF — the position's own current_price.

    That last fallback is load-bearing. 2026-09-11 16:45 UTC: Alpaca market data
    504'd, BIL went unpriced, the book dropped the reserve, and the diff computed
    `sell BIL $179,870.60` — the whole T-bill reserve — for want of one quote, while
    /v2/positions had answered with BIL at $91.50 a call earlier. (Blocked only
    because the clock check failed in the same outage.) Unheld ETFs with no price
    still degrade to cash; held ones are additionally guarded in run()."""
    held_px = {str(p.get("symbol", "")).upper(): p.get("current_price")
               for p in (detail or []) if p.get("current_price")}
    bench = str(cfg.get("benchmark", "SPY")).upper()
    syms = {s.upper() for s in cfg.get("core_etfs", {})}
    syms.add(str(cfg.get("reserve_etf", "BIL")).upper())
    syms.add(str(cfg.get("benchmark", "SPY")).upper())
    for s in syms:
        if prices.get(s):
            continue
        px = None
        if s == bench and trader is not None:
            # The benchmark is never traded (it sits in `reserved` and never
            # enters the target book) — it only draws the comparison line, so it
            # must be TODAY's price. The closes cache refreshes ~06:56 UTC and
            # therefore holds yesterday's close, so cache-first made the equity
            # chart's SPY and the headline "active vs SPY" exactly one session
            # stale: alpha_history recorded 754.05 on 09-17, which is the 09-16
            # close, and the same off-by-one held on every prior day.
            px = trader.latest_price(s)
        if not px:
            px = _price_cache_last(s)
        if not px and trader is not None:
            px = trader.latest_price(s)
        if not px and held_px.get(s):
            px = held_px[s]
            logger.warning(f"{s}: no quote this cycle — priced from the held position (${float(px):.2f})")
        if px:
            prices[s] = float(px)


# ─── account ────────────────────────────────────────────────────────────────

def get_account(cfg: dict):
    """Return (equity, {symbol: market_value}, detail, trader_or_None, client_error).

    `detail` is the per-position list (symbol, market_value, avg_entry_price,
    current_price, unrealized_plpc, qty) that the sell-discipline layer needs.
    `client_error` is the exception CLASS NAME when the dedicated client failed to
    construct/auth (so full_execute surfaces a loud downgrade), None otherwise —
    keys were present but broken is NOT the same as keys absent.

    Uses ONLY the dedicated alpha Alpaca keys (ALPHA_ALPACA_API_KEY/SECRET) so this
    book never collides with the wheel's account. If those keys are absent we run
    virtual: equity = start_capital, no positions, trader=None (full_execute is then
    refused upstream)."""
    key = os.getenv(cfg.get("alpaca_key_env", "ALPHA_ALPACA_API_KEY"))
    sec = os.getenv(cfg.get("alpaca_secret_env", "ALPHA_ALPACA_SECRET_KEY"))
    if not (key and sec):
        logger.warning("No dedicated ALPHA Alpaca keys — virtual book on start_capital "
                       f"${cfg['start_capital']:,.0f} (kept separate from the wheel account).")
        return float(cfg["start_capital"]), {}, [], None, None
    try:
        from trading.alpaca_executor import AlpacaTrader
        base = os.getenv(cfg.get("alpaca_base_env", "ALPHA_ALPACA_BASE_URL")) or "https://paper-api.alpaca.markets"
        base = base.rstrip("/")
        if base.endswith("/v2"):          # executor appends /v2/... itself
            base = base[:-3]
        # Dedicated credentials only — NEVER fall back to the wheel's env keys.
        # 30s timeout (up from 15s) + a bounded retry on the account check: Alpaca's
        # /v2/account occasionally reads-timeout in the first minutes after the
        # 09:30-ET open, which used to DOWNGRADE the whole 09:45-ET execute window on
        # a single slow response (the 13:45-UTC runs failed 2026-07-14/15 while the
        # 16:45/19:45 runs were fine). Retrying an idempotent GET rides out the
        # open-latency spike; the fail-safe still trips (→ advisory) only if every
        # attempt fails, i.e. a genuine outage — not open-latency noise.
        t = AlpacaTrader(api_key=key, api_secret=sec, base_url=base,
                         allow_env_fallback=False, timeout=30)
        acct = None
        for attempt in range(3):
            try:
                acct = t.account()
                if attempt:
                    logger.info(f"ALPHA account check recovered on attempt {attempt + 1}/3.")
                break
            except Exception as e:  # noqa: BLE001 — retry transient latency/timeout
                if attempt == 2:
                    raise
                wait = 2 * (attempt + 1)
                logger.warning(f"ALPHA account check attempt {attempt + 1}/3 failed "
                               f"({type(e).__name__}) — retrying in {wait}s…")
                time.sleep(wait)
        equity = float(acct.get("equity") or acct.get("portfolio_value") or cfg["start_capital"])
        positions, detail = {}, []
        for p in t.list_positions():
            if (p.get("asset_class") or "us_equity") != "us_equity":
                continue
            sym = p["symbol"].upper()
            mv = float(p.get("market_value") or 0)
            positions[sym] = mv
            plpc = p.get("unrealized_plpc")
            detail.append({
                "symbol": sym,
                "market_value": mv,
                "avg_entry_price": float(p.get("avg_entry_price") or 0) or None,
                "current_price": float(p.get("current_price") or 0) or None,
                "unrealized_plpc": (float(plpc) if plpc not in (None, "") else None),
                "qty": float(p.get("qty") or 0),
            })
        return equity, positions, detail, t, None
    except Exception as e:
        logger.error(f"ALPHA Alpaca client unavailable ({type(e).__name__}) — dedicated keys "
                     "are set but the client failed to construct/auth; falling back to the "
                     "virtual book on start_capital. full_execute will be DOWNGRADED.")
        return float(cfg["start_capital"]), {}, [], None, type(e).__name__


def _position_rows(detail: list, book, store: dict, equity: float,
                   reserved: set | None = None) -> list:
    """Per-HELD-position rows for the dashboard: live P/L + the raw inputs a
    'how close to buy-more / sell' proximity indicator needs. Pure projection of
    data already in scope at write time (no I/O). The adapter joins research +
    derives the proximity ratios against state.policy thresholds. `reserved` (core
    + reserve + benchmark ETFs) are flagged so the adapter can skip the TP/SL/
    thesis discipline that decide_sells never applies to them."""
    reserved = {s.upper() for s in (reserved or set())}
    eq = equity or 1.0
    rows = []
    for p in detail:
        sym = p["symbol"].upper()
        mv = float(p.get("market_value") or 0.0)
        avg = p.get("avg_entry_price")
        cur = p.get("current_price")
        qty = float(p.get("qty") or 0.0)
        plpc = p.get("unrealized_plpc")
        if plpc is None and avg and cur:
            plpc = (cur - avg) / avg
        cur_w = mv / eq
        tgt_w = book.weights.get(sym, 0.0)
        rows.append({
            "symbol": sym,
            "market_value": round(mv, 2),
            "qty": round(qty, 4),
            "avg_entry_price": (round(avg, 2) if avg else None),
            "current_price": (round(cur, 2) if cur else None),
            "unrealized_plpc": (round(plpc * 100, 2) if plpc is not None else None),
            "unrealized_pl_usd": (round(mv - qty * avg, 2) if (avg and qty) else None),
            "holding_days": alpha_positions.holding_days(store, sym),
            "target_usd": round(book.dollars.get(sym, 0.0), 2),
            "current_weight": round(cur_w, 4),
            "target_weight": round(tgt_w, 4),
            "drift_pct": round(cur_w - tgt_w, 4),
            "reserved": sym in reserved,       # ETF anchor/reserve — no TP/SL/thesis
        })
    return sorted(rows, key=lambda x: -x["market_value"])


# ─── sell discipline ──────────────────────────────────────────────────────────

def decide_sells(detail: list, research_by_sym: dict, cfg: dict,
                 store: dict, reserved: set) -> dict:
    """Decide take-profit / stop-loss / thesis-invalidation exits for live holdings.

    Returns {symbol: {action, reason, target_usd, plpc}} where action is:
      • "exit" — force target to $0 (stop-loss, broken thesis, or DCF exhausted).
      • "trim" — reduce target to `target_usd` (take-profit on a winner still cheap).
    The anchor/reserve ETFs are never sold here (they are only rebalanced). Empty
    when there are no positions (advisory/virtual) — the layer is a no-op until the
    book actually holds names.
    """
    tp = float(cfg.get("take_profit_pct", 0.30))
    trim = float(cfg.get("take_profit_trim", 0.5))
    sl = float(cfg.get("stop_loss_pct", 0.15))
    dcf_exhausted = float(cfg.get("dcf_exhausted_upside", 5.0))
    exit_tiers = tuple(cfg.get("thesis_exit_tiers", ("pass", "avoid")))
    min_score = float(cfg.get("thesis_exit_min_score", 45))
    min_hold = int(cfg.get("sell_min_hold_days", 5))

    out: dict = {}
    for p in detail:
        sym = p["symbol"].upper()
        if sym in reserved:
            continue
        mv = float(p.get("market_value") or 0)
        if mv <= 0:
            continue
        plpc = p.get("unrealized_plpc")
        if plpc is None and p.get("avg_entry_price") and p.get("current_price"):
            plpc = (p["current_price"] - p["avg_entry_price"]) / p["avg_entry_price"]
        held = alpha_positions.holding_days(store, sym)
        r = research_by_sym.get(sym)

        # 1) Stop-loss — risk first, ignores holding time.
        if plpc is not None and plpc <= -sl:
            out[sym] = {"action": "exit", "reason": f"stop-loss {plpc*100:.0f}%",
                        "target_usd": 0.0, "plpc": plpc}
            continue
        # 2) Thesis invalidation — only when we still have a research read.
        if r is not None:
            why = None
            if r.tier in exit_tiers:
                why = f"tier → {r.tier}"
            elif r.score < min_score:
                why = f"score {r.score:.0f} < {min_score:.0f}"
            elif r.dcf_trusted and r.dcf_upside < 0:     # ignore artifact DCFs
                why = "DCF turned negative"
            if why:
                out[sym] = {"action": "exit", "reason": f"thesis broken ({why})",
                            "target_usd": 0.0, "plpc": plpc}
                continue
        # 3) DCF exhausted — price at/above intrinsic value: take the profit.
        if r is not None and r.dcf_trusted and 0 <= r.dcf_upside <= dcf_exhausted:
            out[sym] = {"action": "exit",
                        "reason": f"DCF exhausted ({r.dcf_upside:.0f}% upside left)",
                        "target_usd": 0.0, "plpc": plpc}
            continue
        # 4) Take-profit trim on a winner that is still below fair value.
        # WATERMARKED: without memory this re-fired every run, halving the
        # position repeatedly — a trim re-arms only after the gain advances
        # another full take_profit_pct beyond the last EXECUTED trim. The
        # watermark is carried on the record and committed by run() ONLY when the
        # order actually places, so an advisory/closed/failed run doesn't burn it.
        if plpc is not None and plpc >= tp and (held is None or held >= min_hold):
            wm = (store.get(sym) or {}).get("trim_watermark")
            if wm is None or plpc >= float(wm) + tp:
                out[sym] = {"action": "trim",
                            "reason": f"take-profit +{plpc*100:.0f}% (trim {int(trim*100)}%)",
                            "target_usd": round(mv * (1.0 - trim), 2), "plpc": plpc,
                            "set_trim_watermark": round(float(plpc), 4),
                            "set_trim_cap_usd": round(mv * (1.0 - trim), 2)}
    return out


# ─── orders ─────────────────────────────────────────────────────────────────

def locked_names(store: dict, held: set, min_hold_days: int) -> set:
    """Held names still inside the minimum holding period.

    A position whose age is unknown is not locked (the old behaviour), so a store
    that lost its entry records can never freeze the book."""
    out = set()
    for sym in held:
        days = alpha_positions.holding_days(store, sym)
        if days is not None and days < int(min_hold_days):
            out.add(sym)
    return out


def apply_sells(book, sells: dict) -> set:
    """Write the sell-discipline decisions into the target book → the exited symbols.

    Runs AFTER build_target, so a stop-loss or broken-thesis exit overrides the
    incumbency band and the minimum-hold lock."""
    exit_syms: set = set()
    for sym, s in sells.items():
        book.dollars[sym] = float(s.get("target_usd") or 0.0)
        if s["action"] == "exit":
            exit_syms.add(sym)
            book.weights.pop(sym, None)
            book.shares.pop(sym, None)
    return exit_syms


def apply_trim_caps(book, store: dict, cfg: dict, equity: float) -> list[str]:
    """Clamp freshly-rebuilt targets to any dollar ceiling left by a recent trim.

    Returns the symbols it capped (for logging/tests). Only ever LOWERS a target,
    so a stop-loss / thesis / DCF-exhausted exit (which sets target 0 afterwards)
    is never blocked by it.
    """
    capped: list[str] = []
    days = int(cfg.get("trim_cap_days", cfg.get("reentry_cooldown_days", 5)))
    for sym in list(book.dollars):
        cap = alpha_positions.active_trim_cap(store, sym, days)
        if cap is not None and book.dollars.get(sym, 0.0) > cap:
            book.dollars[sym] = cap
            if equity:
                book.weights[sym] = cap / equity
            capped.append(sym)
            logger.info(f"trim cap holds {sym} at ${cap:,.0f} "
                        f"({days}d after its take-profit)")
    return capped


def diff_orders(book, current: dict, equity: float, cfg: dict,
                force: set | frozenset = frozenset(),
                hold: set | frozenset = frozenset()) -> list[dict]:
    """Target vs current → notional orders, gated by the drift band + min trade.

    Symbols in `force` (explicit sell-discipline triggers: stop-loss / thesis /
    DCF-exhausted / trim) BYPASS the drift band — a small position's stop-loss
    was silently swallowed by the 2% band and never executed.

    Symbols in `hold` get NO order in either direction unless also in `force`:
    held anchor/reserve ETFs the run could not price, whose zero target is a
    pricing artifact rather than a decision."""
    drift = float(cfg.get("drift_band", 0.02))
    min_usd = float(cfg.get("min_trade_usd", 50.0))
    orders = []
    syms = set(book.dollars) | set(current)
    for s in sorted(syms):
        if s in hold and s not in force:
            continue
        tgt = book.dollars.get(s, 0.0)
        cur = current.get(s, 0.0)
        delta = tgt - cur
        drift_w = abs(delta) / equity if equity else 0.0
        if s not in force and (abs(delta) < min_usd or drift_w < drift):
            continue
        if s in force and abs(delta) < 1.0:
            continue                      # discipline sell already satisfied
        orders.append({
            "symbol": s, "side": "buy" if delta > 0 else "sell",
            "notional": round(abs(delta), 2),
            "target_usd": round(tgt, 2), "current_usd": round(cur, 2),
            "drift_pct": round(drift_w * 100, 2),
        })
    return orders


# ─── governor surfacing ────────────────────────────────────────────────────────

def _governor_summary(cfg: dict) -> dict:
    """Compact view of the governor's current policy + recent decisions for the
    dashboard. Empty-ish when the governor has never run."""
    summary: dict = {"mode": ALPHA_GOVERNOR.get("mode", "advisory")}
    pol = _resolve_soai(cfg.get("policy_path", "data/alpha_policy.json"))
    if pol.exists():
        try:
            raw = json.loads(pol.read_text())
            summary["policy_as_of"] = raw.get("as_of")
            summary["rationale"] = raw.get("rationale")
        except Exception:
            pass
    logp = _resolve_soai(ALPHA_GOVERNOR.get("log_path", "data/alpha_governor_log.jsonl"))
    if logp.exists():
        try:
            lines = [ln for ln in logp.read_text().strip().splitlines() if ln.strip()]
            summary["recent"] = [json.loads(ln) for ln in lines[-6:]]
        except Exception:
            pass
    return summary


def _sleeve_of(sym: str, cfg: dict) -> str:
    if sym.upper() in {s.upper() for s in cfg.get("core_etfs", {})}:
        return "core"
    if sym.upper() == str(cfg.get("reserve_etf", "BIL")).upper():
        return "reserve"
    return "satellite"


# ─── main ─────────────────────────────────────────────────────────────────────

def _clock_gate(trader) -> str | None:
    """None when the market is open; otherwise why orders are skipped. Fails closed
    either way but names the cause: 'market_closed' is the exchange's own answer,
    'clock_unavailable' means Alpaca's /v2/clock call failed. The 2026-09-11 16:45
    run reported 'market_closed' on a Friday afternoon during an Alpaca 500."""
    try:
        return None if (trader.clock() or {}).get("is_open") else "market_closed"
    except Exception as e:
        logger.warning(f"clock preflight failed ({type(e).__name__}) — treating market as closed")
        return "clock_unavailable"


def run(cfg: dict, research_override: str | None = None, force_advisory: bool = False) -> dict:
    cfg = load_alpha_policy(dict(cfg or ALPHA))      # governor policy (clamped) over defaults
    mode = "advisory" if force_advisory else cfg.get("mode", "advisory")
    if mode == "off":
        logger.info("ALPHA mode=off — skipping")
        return {"mode": "off"}

    rp = _resolve_research_path(cfg, research_override)
    if not rp:
        logger.error("no research.json found — cannot build the alpha book")
        return {"mode": mode, "error": "research.json not found"}
    research_doc = json.loads(rp.read_text())
    rankings, prices = load_rankings(rp, cfg)
    research_by_sym = {n.symbol: n for n in rankings}

    # Research quality gate: a degraded doc still produces a book + state for the
    # dashboard, but is never allowed to place orders.
    skipped_reason = None
    degraded = check_research_quality(research_doc, cfg)
    if degraded:
        logger.error(f"research quality gate FAILED: {degraded} — skipping order placement "
                     "(target book still computed, state still written)")
        skipped_reason = f"research_degraded ({degraded})"

    equity, current, detail, trader, client_error = get_account(cfg)
    _ensure_etf_prices(cfg, prices, trader, detail)

    # Safety: never place live (paper) orders without a dedicated alpha account.
    mode_note, downgraded_from = None, None
    if mode == "full_execute" and trader is None:
        if client_error:
            logger.error("full_execute requested but the dedicated ALPHA Alpaca client is "
                         f"unavailable ({client_error}) — DOWNGRADING to advisory. This is "
                         "broken credentials/endpoint, NOT an un-armed run: check the "
                         "ALPHA_ALPACA_* env keys.")
            mode_note = f"DOWNGRADED from full_execute: alpaca client unavailable ({client_error})"
        else:
            logger.warning("full_execute requested but no dedicated ALPHA Alpaca account — "
                           "downgrading to advisory so the wheel account is never touched.")
            mode_note = "DOWNGRADED from full_execute: no dedicated ALPHA Alpaca keys"
        downgraded_from = "full_execute"
        mode = "advisory"
    # Safety: full_execute must point at the Alpaca PAPER host unless explicitly allowed.
    elif mode == "full_execute" and "paper-api" not in trader.base \
            and not cfg.get("allow_live", False):
        logger.error(f"REFUSING to arm full_execute: base URL '{trader.base}' is not the "
                     "Alpaca paper host and ALPHA_ALLOW_LIVE is not set — downgrading to advisory.")
        mode_note = "refused: non-paper base URL without ALPHA_ALLOW_LIVE"
        downgraded_from = "full_execute"
        mode = "advisory"

    # ── Sell discipline (before building the target) ──
    core_syms = {s.upper() for s in cfg.get("core_etfs", {})}
    reserved = core_syms | {str(cfg.get("reserve_etf", "BIL")).upper(),
                            str(cfg.get("benchmark", "SPY")).upper()}
    store = alpha_positions.load(cfg)
    held_syms = {p["symbol"].upper() for p in detail}
    entry_prices = {p["symbol"].upper(): p.get("avg_entry_price") for p in detail}
    store = alpha_positions.sync(store, held_syms, research_by_sym, entry_prices, reserved)
    sells = decide_sells(detail, research_by_sym, cfg, store, reserved)

    # Re-entry cooldown: a name we discipline-exited in the last N days is out of
    # the candidate set — without this, a stop-loss/DCF-exhausted exit was rebuilt
    # into the target on the very next run (a buy-sell loop paying spread).
    cooldown_days = int(cfg.get("reentry_cooldown_days", 5))
    cooled = [n for n in rankings
              if not (n.symbol.upper() not in held_syms
                      and alpha_positions.recently_exited(store, n.symbol, cooldown_days))]
    if len(cooled) < len(rankings):
        skipped_cd = sorted({n.symbol for n in rankings} - {n.symbol for n in cooled})
        logger.info(f"re-entry cooldown ({cooldown_days}d) holding out: {skipped_cd}")

    # Incumbency: held satellite names get the ranking band, and the ones still inside
    # the minimum holding period are kept whatever their rank this run.
    sat_held = held_syms - reserved
    locked = locked_names(store, sat_held, int(cfg.get("sell_min_hold_days", 5)))
    book = build_target(cooled, prices, equity, cfg, held=sat_held, locked=locked)

    # Trim cap: hold a trimmed name at its reduced size for a few days. The trim
    # target itself is a one-run instruction, so without this the very next run
    # rebuilds the full conviction target and buys the trim straight back (paying
    # the spread twice and undoing the take-profit). Only ever LOWERS a target, so
    # it can never block a stop-loss/thesis exit — those set target 0 below.
    apply_trim_caps(book, store, cfg, equity)

    # Force sold names out of the target so we exit them (and don't rebuy).
    exit_syms = apply_sells(book, sells)

    # A held anchor/reserve ETF with no price this cycle left the book only because
    # it couldn't be sized — never trade it on that basis. Satellite names are NOT
    # held here: for them a zero target is a real rotate-out decision.
    unpriced = sorted(s for s in reserved if current.get(s) and not prices.get(s))
    if unpriced:
        book.notes.append(f"unpriced this cycle, held unchanged: {', '.join(unpriced)}")
        logger.warning(f"no price for held {unpriced} — no orders for them this run")
    orders = diff_orders(book, current, equity, cfg, force=set(sells), hold=set(unpriced))

    logger.info(
        f"ALPHA [{mode}] equity ${equity:,.0f} · core {book.sleeves['core']*100:.0f}% "
        f"satellite {book.sleeves['satellite']*100:.0f}% reserve {book.sleeves['reserve']*100:.0f}% "
        f"cash {book.sleeves['cash']*100:.0f}% · {len(book.selected)} names · "
        f"{book.opportunity_count} good buys · {len(sells)} sells · {len(orders)} orders"
    )

    executed, order_errors = [], []
    place_orders = mode == "full_execute" and trader is not None and skipped_reason is None
    # Market-open preflight: don't trade into holidays/closed sessions (fail closed —
    # an unreadable clock is treated as a closed market).
    if place_orders and cfg.get("require_market_open", True):
        gate = _clock_gate(trader)
        if gate:
            logger.info(f"{gate} — skipping order placement "
                        "(target book still computed and persisted)")
            skipped_reason = gate
            place_orders = False
    if place_orders:
        for o in orders:
            try:
                if o["side"] == "sell" and (o["symbol"] in exit_syms or o["target_usd"] <= 0.01):
                    res = trader.close_position(o["symbol"])     # full liquidation
                    alpha_positions.record_exit(store, o["symbol"],
                                                (sells.get(o["symbol"]) or {}).get("reason", "exit"))
                else:
                    res = trader.submit_equity_notional(o["symbol"], o["notional"], o["side"])
                # Commit the trim watermark ONLY now that the order placed, so an
                # advisory/closed/failed run never silently burns the take-profit.
                _tw = (sells.get(o["symbol"]) or {}).get("set_trim_watermark")
                if _tw is not None and o["symbol"] in store:
                    store[o["symbol"]]["trim_watermark"] = _tw
                    _cap = (sells.get(o["symbol"]) or {}).get("set_trim_cap_usd")
                    if _cap is not None:
                        store[o["symbol"]]["trim_cap_usd"] = float(_cap)
                        store[o["symbol"]]["trim_cap_at"] = alpha_positions._now()
                executed.append({**o, "order_id": res.get("id"), "status": res.get("status")})
                logger.info(f"  {o['side']} {o['symbol']} ${o['notional']:,.0f} → {res.get('status')}")
            except Exception as e:
                logger.error(f"  order failed {o['symbol']}: {e}")
                executed.append({**o, "error": str(e)})
                order_errors.append({"symbol": o["symbol"],
                                     "error": f"{type(e).__name__}: {str(e)[:200]}"})

    alpha_positions.save(cfg, store)

    as_of = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    spy_last = prices.get(str(cfg.get("benchmark", "SPY")).upper()) or _price_cache_last(cfg.get("benchmark", "SPY"))
    sell_records = [
        {"symbol": s, "action": v["action"], "reason": v["reason"],
         "plpc": (round(v["plpc"] * 100, 2) if v.get("plpc") is not None else None),
         "target_usd": round(float(v.get("target_usd") or 0), 2)}
        for s, v in sorted(sells.items())
    ]
    state = {
        "as_of": as_of,
        "mode": mode,
        "mode_note": mode_note,
        "downgraded_from": downgraded_from,
        "skipped_reason": skipped_reason,
        "benchmark": cfg.get("benchmark", "SPY"),
        "spy_last": spy_last,
        "equity": round(equity, 2),
        "start_capital": round(float(cfg["start_capital"]), 2),
        "sleeves": {k: round(v, 4) for k, v in book.sleeves.items()},
        "reserve": {"symbol": str(cfg.get("reserve_etf", "BIL")).upper(),
                    "weight": round(book.reserve_frac, 4),
                    "floor": cfg.get("reserve_floor"), "ceiling": cfg.get("reserve_ceiling")},
        "opportunity_count": book.opportunity_count,
        "selected": book.selected,
        "target": [
            {"symbol": s, "weight": round(book.weights.get(s, 0.0), 4),
             "dollars": round(book.dollars[s], 2),
             "shares": round(book.shares.get(s, 0.0), 4),
             "sleeve": _sleeve_of(s, cfg),
             "style": book.styles.get(s, "")}
            for s in sorted(book.dollars, key=lambda x: -book.weights.get(x, 0.0))
            if book.dollars.get(s, 0.0) > 0.01
        ],
        "current": [{"symbol": s, "market_value": round(v, 2)} for s, v in sorted(current.items())],
        # Rich per-held-position rows (P/L + proximity inputs) for the dashboard.
        "positions": _position_rows(detail, book, store, equity, reserved),
        "sells": sell_records,
        "orders": orders,
        "executed": executed,
        "order_errors": order_errors,
        "notes": book.notes,
        "policy": {k: cfg.get(k) for k in ALPHA_CLAMPS},
        "governor": _governor_summary(cfg),
        "research_as_of": research_doc.get("generated_at"),
    }
    out = _resolve_soai(cfg.get("state_path", "data/alpha_state.json"))
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(state, indent=2))

    # Append a perf point so the dashboard can plot equity vs SPY (active return).
    hist = out.with_name("alpha_history.jsonl")
    try:
        with hist.open("a") as fh:
            fh.write(json.dumps({"as_of": as_of, "equity": round(equity, 2),
                                 "spy": spy_last, "mode": mode}) + "\n")
    except Exception as e:
        logger.warning(f"could not append alpha history: {e}")
    logger.info(f"wrote {out}")
    return state


def main():
    ap = argparse.ArgumentParser(description="Alpha portfolio runner (paper)")
    ap.add_argument("--dry-run", action="store_true", help="force advisory (no orders)")
    ap.add_argument("--research", help="path to research.json")
    args = ap.parse_args()
    run(dict(ALPHA), research_override=args.research, force_advisory=args.dry_run)


if __name__ == "__main__":
    main()
