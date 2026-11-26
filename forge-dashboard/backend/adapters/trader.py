"""Trader adapter — Alpaca paper account + positions + recent actions.

Live Alpaca data is fetched lazily via the SOAI AlpacaTrader (needs creds in
env). When unavailable (e.g. running locally without keys), falls back to the
cached trader_state.json so the dashboard still shows the last-known state.
"""
from __future__ import annotations

import datetime as dt
import json
import statistics
import sys

import appconfig as config
from adapters import read_json

# Wheel cron schedule (UTC, weekdays) — mirrors the crontab; display-only.
_WHEEL_RUNS_UTC = ((13, 35), (16, 30), (19, 30))
_SCAN_RUN_UTC = (11, 30)


def _last_run_summary() -> dict | None:
    """Parse the newest RUN_SUMMARY heartbeat line the wheel logs at the end of
    every run (including skip paths). Reads only the log tail — cheap."""
    log = config.SOAI_ROOT / "logs" / "trader.log"
    try:
        with open(log, "rb") as fh:
            fh.seek(0, 2)
            size = fh.tell()
            fh.seek(max(0, size - 131072))
            tail = fh.read().decode("utf-8", "replace")
        idx = tail.rfind("RUN_SUMMARY ")
        if idx < 0:
            return None
        line = tail[idx + len("RUN_SUMMARY "):].splitlines()[0]
        return json.loads(line)
    except Exception:
        return None


def _next_fires(times_utc, now: dt.datetime) -> dt.datetime:
    """Next weekday fire among the given (hour, minute) UTC times."""
    for day in range(0, 8):
        d = (now + dt.timedelta(days=day)).date()
        if d.weekday() >= 5:            # Sat/Sun — cron is 1-5
            continue
        for hh, mm in sorted(times_utc):
            t = dt.datetime(d.year, d.month, d.day, hh, mm, tzinfo=dt.timezone.utc)
            if t > now:
                return t
    return now  # unreachable


def _schedule() -> dict:
    now = dt.datetime.now(dt.timezone.utc)
    nxt = _next_fires(_WHEEL_RUNS_UTC, now)
    scan = _next_fires((_SCAN_RUN_UTC,), now)
    return {
        "next_run": nxt.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "next_run_in_min": int((nxt - now).total_seconds() // 60),
        "next_scan": scan.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "runs_utc": [f"{h:02d}:{m:02d}" for h, m in _WHEEL_RUNS_UTC],
    }


def _guardrails() -> dict:
    """Key risk knobs, read from the SOAI settings module (same source the
    runner imports). Values reflect config defaults + any env overrides visible
    to this process."""
    try:
        if str(config.SOAI_ROOT) not in sys.path:
            sys.path.insert(0, str(config.SOAI_ROOT))
        from config.settings import AGENT, RISK  # type: ignore
        return {
            "kill_switch_loss_mult": AGENT.get("kill_switch_loss_mult"),
            "close_at_profit_pct": RISK.get("close_at_profit_pct"),
            "close_at_loss_multiplier": RISK.get("close_at_loss_multiplier"),
            "require_market_open": RISK.get("require_market_open"),
            "max_scan_age_hours": RISK.get("max_scan_age_hours"),
        }
    except Exception:
        return {}


def _scan_candidates(n: int = 6) -> list[dict]:
    """Top of today's grouped scan — what the wheel would open next — with the
    equity-research overlay fields (composite/confidence/DCF) the scan now
    carries, so 'why this name' is visible at a glance."""
    rows = read_json(config.SOAI_ROOT / "scan_results_grouped_latest.json", default=None)
    if not isinstance(rows, list):
        return []
    out = []
    for s in rows[:n]:
        best = (s.get("strikes") or [{}])[0]
        out.append({
            "symbol": s.get("symbol"),
            "best_score": s.get("best_score"),
            "best_rating": s.get("best_rating"),
            "would_own": s.get("would_own_score"),
            "last": s.get("current_price"),
            "strike": best.get("Strike"),
            "expiration": best.get("Expiration"),
            "monthly_pct": best.get("Monthly %"),
            "pop": best.get("POP"),
            # research overlay (None when the scan predates the overlay or the
            # name is outside the research universe)
            "equity_score": s.get("equity_score"),
            "equity_conf_label": s.get("equity_conf_label"),
            "equity_tier": s.get("equity_tier"),
            "dcf_upside_pct": s.get("dcf_upside_pct"),
        })
    return out


def _parse_occ(symbol: str) -> tuple[str, str, float] | None:
    """Parse an OCC option symbol → (ticker, 'C'|'P', strike). None if not an option.

    OCC format: <ticker><YYMMDD><C|P><8-digit strike×1000>, e.g.
    AFRM260702P00059000 → ("AFRM", "P", 59.0). A bare stock ticker ("UBER")
    has no 15-char option suffix and returns None.
    """
    s = (symbol or "").strip().upper()
    if len(s) < 16:  # ticker(≥1) + 6 date + 1 type + 8 strike
        return None
    body = s[-15:]
    opt_type, date_part, strike_part = body[6], body[:6], body[7:]
    if opt_type not in ("C", "P") or not (date_part.isdigit() and strike_part.isdigit()):
        return None
    return s[:-15], opt_type, int(strike_part) / 1000.0


def _liabilities(positions: list) -> dict:
    """Assignment obligation if every SHORT option were exercised against us.

    For a short put, assignment means BUYING 100×qty shares at the strike — real
    cash out the door (this is the collateral a cash-secured put reserves, and
    why the wheel holds so much cash). For a short call, "called away" means
    SELLING shares at the strike, so it's tracked separately (not a cash debit).
    """
    put_assign = call_assign = 0.0
    n_puts = n_calls = 0
    for p in positions or []:
        parsed = _parse_occ(p.get("symbol", ""))
        if not parsed:
            continue
        _, opt_type, strike = parsed
        try:
            qty = int(float(p.get("qty", 0) or 0))
        except (TypeError, ValueError):
            qty = 0
        if qty >= 0:  # only short options create an obligation
            continue
        notional = strike * 100 * abs(qty)
        if opt_type == "P":
            put_assign += notional
            n_puts += abs(qty)
        else:
            call_assign += notional
            n_calls += abs(qty)
    return {
        "put_assignment": round(put_assign, 2),    # cash you'd pay if all puts assigned
        "call_assignment": round(call_assign, 2),  # proceeds if all calls assigned (info)
        "short_put_contracts": n_puts,
        "short_call_contracts": n_calls,
    }


def _alpaca_live() -> dict | None:
    """Try a live Alpaca pull. Returns None if creds/module unavailable."""
    try:
        if str(config.SOAI_ROOT) not in sys.path:
            sys.path.insert(0, str(config.SOAI_ROOT))
        from trading.alpaca_executor import AlpacaTrader  # type: ignore
        t = AlpacaTrader()
        return {
            "account": t.account(),
            "positions": t.list_positions(),
            "open_orders": t.list_open_orders(),
            "spy_last": _spy_last(t),
        }
    except Exception:
        return None


def _spy_last(trader) -> float | None:
    """SPY's latest trade for the benchmark line's right edge — one extra
    market-data call per poll (~0.25s measured). None on any failure, and the
    grey line then simply ends at the last cached close."""
    try:
        px = trader.latest_price("SPY")
        return float(px) if px and 1.0 < float(px) < 100000.0 else None
    except Exception:
        return None


def _history_series(history: dict, *, live: dict | None = None,
                    spy_today: float | None = None) -> list[dict]:
    """Per-day chart series from trader_history: equity, cash, activity, premium
    (with a running cumulative), and the day's VIX/regime tag."""
    out: list[dict] = []
    cum = 0.0
    for d in (history.get("days") or []):
        prem = float(d.get("net_premium_today", 0) or 0)
        cum += prem
        reg = d.get("regime") or {}
        out.append({
            "date": d.get("date"),
            "equity": d.get("equity"),
            "cash": d.get("cash"),
            "opens": d.get("opens_count", 0),
            "closes": d.get("closes_count", 0),
            "net_premium": round(prem, 2),
            "cum_premium": round(cum, 2),
            "vix": round(float(reg["vix"]), 1) if isinstance(reg.get("vix"), (int, float)) else None,
            "regime": reg.get("regime"),
            "open_positions": d.get("open_positions_count"),
        })
    # The live anchor goes on BEFORE the SPY overlay so the overlay can price it
    # (and today's nightly row, once it lands) from SPY's live trade.
    if live:
        out = _with_live_anchor(out, **live)
    _spy_overlay(out, spy_today)
    return out


def _with_live_anchor(series: list[dict], equity: float, cash: float,
                      source: str) -> list[dict]:
    """Pin the equity chart's right edge to the live account value.

    Per-day rows are written by daily_trader_summary at 21:00 UTC, so for the
    whole trading day the newest row is YESTERDAY while the headline card reads
    Alpaca's live equity. Measured 2026-09-11: the chart ended at $101,869
    (09-10) under a $103,069 headline — a $1,200 gap that read as a broken chart.
    Until the nightly row lands, append today's live mark, flagged `live`.

    Only for a live Alpaca read (the cached fallback has nothing newer than its
    last row) and never duplicating a date — dates are UTC, matching the writer.
    spy_equity starts None; _spy_overlay fills it from SPY's LIVE trade, never
    from yesterday's close — pairing an intraday mark with a stale close would
    draw a fake gap between the lines. Without a quote it stays None and the
    grey line ends at the last close. Consumers that need daily closes (wheel_analytics, the
    activity chart) must skip `live` rows — an intraday mark is not a close.
    """
    if source != "alpaca_live" or not equity or not series:
        return series
    today = dt.datetime.now(dt.timezone.utc).date().isoformat()
    if str(series[-1].get("date") or "")[:10] >= today:
        return series
    return series + [{
        "date": today,
        "equity": round(float(equity), 2),
        "cash": round(float(cash), 2) if cash else None,
        "spy_equity": None,
        "live": True,
    }]


def _spy_overlay(out: list[dict], spy_today: float | None = None) -> None:
    """Add spy_equity per day: what the SAME starting capital would be worth in
    SPY — the benchmark line on the wheel equity chart (mirrors the alpha tab's
    strategy-vs-SPY curve). Best-effort: any failure leaves the series absent and
    the chart renders exactly as before.

    `spy_today` (SPY's latest trade) prices any day NEWER than the cache's last
    close, within a long weekend of today. The closes cache refreshes once a
    day (~06:56 UTC), so on 2026-09-11 the options chart's equity line reached
    today while the S&P line stopped a day behind — and after the 21:00 UTC
    history row lands, that row would otherwise copy yesterday's S&P value
    forward. Days the cache already covers (market holidays included) still
    carry the prior close forward, which is correct for a closed market."""
    try:
        from adapters import rh_analytics, read_json  # lazy — keep /api/trader cheap
        # CACHE-ONLY read: /api/trader is polled every 15s; a stale SPY cache must
        # never trigger a blocking yfinance download here (the scheduled refresh +
        # research builds keep the cache warm). Stale closes beat a blocked poll.
        d = read_json(rh_analytics._prices_path("SPY"))
        if not (isinstance(d, dict) and d.get("dates") and d.get("closes")) or not out:
            return
        closes = {str(k)[:10]: float(c) for k, c in zip(d["dates"], d["closes"])}
        last_cached = max(closes) if closes else ""
        today = dt.datetime.now(dt.timezone.utc).date()

        def _uncached_recent(date_s: str) -> bool:
            try:
                return date_s > last_cached and \
                    (today - dt.date.fromisoformat(date_s)).days <= 4
            except ValueError:
                return False

        base_close = base_equity = last_close = None
        for row in out:
            date_s = str(row.get("date") or "")[:10]
            c = closes.get(date_s)
            if c is None and spy_today and _uncached_recent(date_s):
                c = float(spy_today)
            if c is None and row.get("live"):
                row["spy_equity"] = None      # never pair a live mark with a stale close
                continue
            if c is not None:
                last_close = c
            if base_close is None and last_close is not None and row.get("equity"):
                base_close, base_equity = last_close, float(row["equity"])
            row["spy_equity"] = (round(base_equity * (last_close / base_close), 2)
                                 if (base_close and last_close and base_equity) else None)
    except Exception:
        for d in out:
            d.setdefault("spy_equity", None)


def _close_stats(state: dict) -> dict:
    """Realized-close scorecard. captured_pct comes from the runner's estimated
    exit premium (exit_premium_est) — honest label: estimates, not marks."""
    closes = state.get("closes") or []
    scored = [c for c in closes if isinstance(c.get("captured_pct"), (int, float))]
    if not scored:
        return {"n_closes": len(closes)}
    caps = [float(c["captured_pct"]) for c in scored]
    wins = [c for c in caps if c > 0]
    best = max(scored, key=lambda c: c["captured_pct"])
    worst = min(scored, key=lambda c: c["captured_pct"])
    # Entry quality over opens that actually became (or could become) positions
    # — expired/cancelled submissions never filled and would flatter the stat.
    opens = [o for o in (state.get("orders") or [])
             if o.get("status") not in ("expired", "canceled", "cancelled", "rejected")
             and not o.get("dry_run")]
    pops = [float(o["predicted_pop"]) for o in opens if isinstance(o.get("predicted_pop"), (int, float))]
    scores = [float(o["score"]) for o in opens if isinstance(o.get("score"), (int, float))]
    return {
        "n_closes": len(closes),
        "n_scored": len(scored),
        "win_rate_pct": round(len(wins) / len(caps) * 100, 1),
        "avg_captured_pct": round(sum(caps) / len(caps), 1),
        "median_captured_pct": round(statistics.median(caps), 1),
        "best": {"ticker": best.get("ticker"), "captured_pct": best["captured_pct"]},
        "worst": {"ticker": worst.get("ticker"), "captured_pct": worst["captured_pct"]},
        "n_entries": len(opens),
        "avg_entry_pop": round(sum(pops) / len(pops), 3) if pops else None,
        "avg_entry_score": round(sum(scores) / len(scores), 1) if scores else None,
        "caveat": "captured % uses the runner's estimated exit premium",
    }


def get_decisions(n: int = 30) -> dict:
    """Tail of the wheel agent's per-position decision log (newest first) —
    the model's actual reasoning: baseline rule vs agent verdict, gray-zone
    triggers, confidence, and the (capped) research text behind it."""
    state = read_json(config.TRADER_STATE_PATH, default={}) or {}
    rows = state.get("agent_decisions") or []
    out = []
    for d in rows[-n:][::-1]:
        agent = d.get("agent") or {}
        raw = agent.get("raw") or ""
        snap = d.get("snapshot") or {}
        out.append({
            "ts": d.get("ts"),
            "ticker": d.get("ticker"),
            "occ": d.get("occ"),
            "mode": d.get("mode"),
            "gray_zone": bool(d.get("gray_zone")),
            "gray_reasons": d.get("gray_reasons") or [],
            "baseline": d.get("baseline") or {},
            "agent": {
                "action": agent.get("action"),
                "confidence": agent.get("confidence"),
                "rationale": agent.get("rationale"),
                "thesis_status": agent.get("thesis_status"),
                "research": raw[:1500] + ("…" if len(raw) > 1500 else ""),
            },
            "final": d.get("final") or {},
            "executed": d.get("executed"),
            "snapshot": {k: snap.get(k) for k in
                         ("strike", "dte", "captured_pct", "underlying_price",
                          "quality_tag", "days_to_earnings", "regime")},
        })
    return {"decisions": out, "total": len(rows)}


def get_trader() -> dict:
    state = read_json(config.TRADER_STATE_PATH, default={}) or {}
    history = read_json(config.TRADER_HISTORY_PATH, default={}) or {}
    live = _alpaca_live()
    spy_now = (live or {}).get("spy_last")

    # Account summary — prefer live, else derive from state
    if live and live.get("account"):
        acc = live["account"]
        equity = float(acc.get("equity", 0) or 0)
        cash = float(acc.get("cash", 0) or 0)
        positions = live.get("positions", [])
        open_orders = live.get("open_orders", [])
        source = "alpaca_live"
    else:
        # The runner never writes top-level equity/cash — they live under
        # state['reserve'] (and each history day). Fall back through those so an
        # Alpaca outage shows last-known figures instead of a false $0 account.
        reserve = state.get("reserve") or {}
        last_day = ((history.get("days") or [{}])[-1]) if isinstance(history, dict) else {}
        equity = float(state.get("equity") or reserve.get("equity")
                       or last_day.get("equity") or 0)
        cash = float(state.get("cash") or reserve.get("cash")
                     or last_day.get("cash") or 0)
        positions = state.get("positions", []) or []
        open_orders = state.get("open_orders", []) or []
        source = "cached_state"

    recent_opens = (state.get("orders") or [])[-10:]
    recent_closes = (state.get("closes") or [])[-10:]
    regime = state.get("last_regime") or {}

    # Lifetime history summary
    days = (history.get("days") or []) if isinstance(history, dict) else []
    lifetime = {
        "days_tracked": len(days),
        "total_opens": sum(d.get("opens_count", 0) for d in days),
        "total_closes": sum(d.get("closes_count", 0) for d in days),
        "net_premium": sum(d.get("credit_today", 0) - d.get("debit_today", 0) for d in days),
    }

    liabilities = _liabilities(positions)

    return {
        "source": source,
        "equity": equity,
        "cash": cash,
        "pl_vs_start": equity - 100000.0 if equity else 0,
        "liabilities": liabilities,
        # Cash left after honoring every short-put assignment (the true free cash).
        "cash_after_assignment": round(cash - liabilities["put_assignment"], 2),
        # Adaptive reserve state (floor %, regime target, easing) — persisted by
        # the wheel each run; absent until the new wheel_runner has run once.
        "reserve": state.get("reserve") or {},
        "open_positions": len(positions),
        "positions": positions,
        "open_orders": open_orders,
        "recent_opens": recent_opens,
        "recent_closes": recent_closes,
        "regime": regime,
        "lifetime": lifetime,
        "history": _history_series(
            history, live={"equity": equity, "cash": cash, "source": source},
            spy_today=spy_now),
        "stats": _close_stats(state),
        # Run health + operating context (added with the trader-tab upgrade):
        # the wheel's RUN_SUMMARY heartbeat, the cron schedule, the effective
        # guardrails, and the research-annotated next candidates.
        "last_run": _last_run_summary(),
        "schedule": _schedule(),
        "guardrails": _guardrails(),
        "candidates": _scan_candidates(),
    }
