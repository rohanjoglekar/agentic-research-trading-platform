"""Alpha portfolio adapter — the long-only "beat the S&P 500" paper trader.

Reads the state + history written by stock-options-ai/trading/alpha_runner.py and
returns a frontend-friendly payload: current target book (core/satellite sleeves),
holdings, pending orders, and an equity-vs-SPY curve rebased to inception so the UI
can show active return. Degrades gracefully when the runner hasn't produced state
yet (returns available=False with a reason) — like every other adapter.
"""
from __future__ import annotations

import appconfig as config
from adapters import read_json, read_jsonl_tail


def _perf_curve(start_capital: float | None = None) -> tuple[list[dict], dict]:
    """Rebase alpha equity and SPY to 100 at INCEPTION (the first day the book was
    actually invested) → active return. The advisory/dry-run days before arming all
    sit flat at exactly start_capital; we trim that leading run but KEEP its last
    point — the first-trade day, flat because notional buys convert cash→positions
    at par — so the curve starts at the first real trade, not weeks earlier."""
    rows = read_jsonl_tail(config.ALPHA_HISTORY_PATH, n=2000)
    rows = [r for r in rows if r.get("equity") and r.get("spy")]
    if start_capital and len(rows) > 2:
        first_moved = next((i for i, r in enumerate(rows)
                            if abs(float(r["equity"]) - start_capital) >= 0.01), None)
        if first_moved and first_moved > 1:          # keep the last flat = inception
            rows = rows[first_moved - 1:]
    if len(rows) < 2:
        return [], {}
    # Same correction as the analytics: resolve the benchmark from the daily
    # closes cache so the chart line and beta agree, falling back to the recorded
    # value for dates the cache has not reached (today).
    from adapters import rh_analytics
    _closes = rh_analytics.daily_closes("SPY")

    def _spy(r):
        return _closes.get(str(r["as_of"])[:10]) or float(r["spy"])

    eq0, spy0 = float(rows[0]["equity"]), _spy(rows[0])
    curve = [
        {
            "t": r["as_of"],
            "strat": round(float(r["equity"]) / eq0 * 100, 3),
            "spy": round(_spy(r) / spy0 * 100, 3),
        }
        for r in rows
    ]
    last = curve[-1]
    return curve, {
        "strat_return_pct": round(last["strat"] - 100, 2),
        "spy_return_pct": round(last["spy"] - 100, 2),
        "active_return_pct": round(last["strat"] - last["spy"], 2),
        "points": len(curve),
        "since": curve[0]["t"],
    }


# Investment style per holding. Known ETFs are mapped directly; individual
# satellites take the label of their DOMINANT research factor (value/quality/
# growth/momentum) so the STYLE column reads like a real factor tilt.
_ETF_STYLE = {
    "BIL": "T-bills · cash", "SGOV": "T-bills · cash", "SHV": "T-bills · cash",
    "VOO": "US large blend", "SPY": "US large blend", "IVV": "US large blend",
    "VTI": "US total market", "QQQ": "US large growth", "QQQM": "US large growth",
    "QQQI": "Nasdaq call-income", "JEPQ": "Nasdaq call-income", "JEPI": "US call-income",
    "SCHD": "US dividend value", "VUG": "US large growth", "VTV": "US large value",
    "DIA": "US large value", "IWM": "US small blend",
}
_FACTOR_LABEL = {"value": "Value", "quality": "Quality", "growth": "Growth", "momentum": "Momentum"}


def _research_factors() -> dict:
    """symbol -> {value,quality,growth,momentum} sub-scores from the research cache."""
    data = read_json(config.RH_RESEARCH_CACHE_PATH, default={}) or {}
    out: dict = {}
    for s in (data.get("stocks") or []):
        f = s.get("factors") or {}
        out[s.get("symbol")] = {k: (f.get(k) or {}).get("score") for k in _FACTOR_LABEL}
    return out


def _style_for(sym: str, sleeve: str, factors: dict | None) -> str:
    if sym in _ETF_STYLE:
        return _ETF_STYLE[sym]
    if sleeve == "reserve":
        return "T-bills · cash"
    if factors:
        scored = [(k, factors.get(k)) for k in _FACTOR_LABEL
                  if isinstance(factors.get(k), (int, float))]
        if scored:
            return _FACTOR_LABEL[max(scored, key=lambda x: x[1])[0]]
    return "—"


def _pick_detail() -> dict:
    """symbol -> research-screen rationale (composite, tier, confidence, DCF
    upside, sector) so the book can show WHY each satellite is held."""
    data = read_json(config.RH_RESEARCH_CACHE_PATH, default={}) or {}
    out: dict = {}
    for s in (data.get("stocks") or []):
        dcf = s.get("dcf") or {}
        out[s.get("symbol")] = {
            "name": s.get("name"),
            "sector": s.get("sector"),
            "score": s.get("score"),
            "tier": s.get("tier"),
            "confidence_label": s.get("confidence_label"),
            "dcf_upside_pct": dcf.get("upside_pct") if dcf.get("available") else None,
            "dcf_confidence": dcf.get("confidence"),
            "dcf_trusted": bool(dcf.get("available")
                                and dcf.get("confidence") not in ("LOW/ARTIFACT", "N/A", None)),
        }
    return out


def _proximity(pos: dict, policy: dict) -> dict:
    """How close THIS holding is to a buy-more or a sell trigger, 0..1 (1 = at the
    threshold), faithfully mirroring the runner's decide_sells: TAKE-PROFIT is
    min-hold-gated (rule 4), but STOP-LOSS is UNCONDITIONAL on holding time
    (rule 1, risk-first) — a fresh lot past its stop is NOT 'protected'. The
    TP/SL/thesis discipline applies ONLY to non-reserved (satellite) names;
    reserved core/reserve ETFs are never sold that way, so they show only the
    drift-based add signal."""
    plpc = (pos.get("unrealized_plpc") or 0.0) / 100.0            # fraction
    tp = policy.get("take_profit_pct") or 0.30
    sl = policy.get("stop_loss_pct") or 0.15
    band = policy.get("drift_band") or 0.02
    min_hold = policy.get("sell_min_hold_days") or 5
    thesis_min = policy.get("thesis_exit_min_score") or 45
    dcf_exhausted = policy.get("dcf_exhausted_upside")
    dcf_exhausted = 5.0 if dcf_exhausted is None else dcf_exhausted
    exit_tiers = set(policy.get("thesis_exit_tiers") or ("pass", "avoid"))
    hd = pos.get("holding_days")
    hold_ok = (hd is None) or (hd >= min_hold)                   # None → not gated (mirrors runner)
    disc = not pos.get("reserved")                               # discipline = satellites only
    score = pos.get("score")
    du = pos.get("dcf_upside_pct")
    dcf_trusted = bool(pos.get("dcf_trusted"))
    drift = pos.get("drift_pct") or 0.0                           # current − target weight

    cands = []                                                    # (proximity, rule, label)
    if disc and hold_ok and plpc > 0:                            # take-profit IS hold-gated
        cands.append((min(plpc / tp, 1.0) if tp else 0.0, "take-profit",
                      f"+{plpc*100:.0f}% toward +{tp*100:.0f}% take-profit"))
    if disc and plpc < 0:                                        # stop-loss is NOT hold-gated
        cands.append((min(-plpc / sl, 1.0) if sl else 0.0, "stop-loss",
                      f"{plpc*100:.0f}% toward −{sl*100:.0f}% stop"))
    if disc and isinstance(score, (int, float)):                # thesis decay (15-pt approach window)
        th = max(0.0, min((thesis_min + 15 - score) / 15.0, 1.0))
        if th > 0:
            cands.append((th, "thesis", f"score {score:.0f} vs exit <{thesis_min}"))
    # decide_sells' remaining exits (binary — fire now or not): tier flip, and,
    # ONLY on a trusted DCF, DCF-negative / DCF-exhausted. Gating on dcf_trusted
    # keeps the meter consistent with the runner (which now ignores artifact DCFs).
    if disc and pos.get("tier") in exit_tiers:
        cands.append((1.0, "thesis", f"tier → {pos.get('tier')}"))
    if disc and dcf_trusted and du is not None:
        if du < 0:
            cands.append((1.0, "thesis", "DCF turned negative"))
        elif du <= dcf_exhausted:
            cands.append((1.0, "take-profit", f"DCF exhausted ({du:.0f}% upside left)"))
    sell = max(cands, key=lambda x: x[0]) if cands else (0.0, None, None)

    buy_pct, buy_label = 0.0, None
    if drift < 0:                                                 # under target weight → eligible add
        buy_pct = min(-drift / band, 1.0) if band else 0.0
        buy_label = f"{-drift*100:.1f}% under target (add at {band*100:.0f}% drift)"
    return {
        "hold_ok": hold_ok,
        "sell_proximity": round(sell[0], 3), "sell_rule": sell[1], "sell_label": sell[2],
        "buy_proximity": round(buy_pct, 3), "buy_label": buy_label,
    }


def _enrich_positions(state: dict, picks: dict) -> list:
    """Join the runner's rich per-position rows with research (score/tier/DCF) and
    attach the derived buy/sell proximity. Empty until the runner writes `positions`."""
    policy = state.get("policy") or {}
    out = []
    for p in (state.get("positions") or []):
        r = picks.get(p.get("symbol")) or {}
        pos = {**p, "name": r.get("name"), "sector": r.get("sector"),
               "score": r.get("score"), "tier": r.get("tier"),
               "confidence_label": r.get("confidence_label"),
               "dcf_upside_pct": r.get("dcf_upside_pct"),
               "dcf_confidence": r.get("dcf_confidence"),
               "dcf_trusted": r.get("dcf_trusted", True)}
        pos["proximity"] = _proximity(pos, policy)
        out.append(pos)
    return out


def get_portfolio() -> dict:
    """Current alpha book + holdings + orders + perf-vs-SPY curve."""
    state = read_json(config.ALPHA_STATE_PATH, default=None)
    if not state:
        return {
            "available": False,
            "reason": "The alpha trader hasn't produced state yet. Run "
                      "`python -m trading.alpha_runner --dry-run` in stock-options-ai.",
        }
    curve, perf = _perf_curve(state.get("start_capital"))
    target = state.get("target", [])
    rfac = _research_factors()
    picks = _pick_detail()
    for t in target:                       # fill the STYLE column (ETF map / factor tilt)
        if not t.get("style"):
            t["style"] = _style_for(t.get("symbol"), t.get("sleeve"), rfac.get(t.get("symbol")))
        # research rationale (satellites only — ETFs aren't in the screen)
        p = picks.get(t.get("symbol"))
        if p and t.get("sleeve") == "satellite":
            t["research"] = p
    return {
        "available": True,
        "as_of": state.get("as_of"),
        "mode": state.get("mode"),
        "benchmark": state.get("benchmark", "SPY"),
        "equity": state.get("equity"),
        "start_capital": state.get("start_capital", 1_000_000),
        "sleeves": state.get("sleeves", {}),
        "reserve": state.get("reserve", {}),
        "opportunity_count": state.get("opportunity_count"),
        "n_satellite": len([t for t in target if t.get("sleeve") == "satellite"]),
        "target": target,
        "current": state.get("current", []),
        "positions": _enrich_positions(state, picks),   # rich holdings: P/L + buy/sell proximity
        "orders": state.get("orders", []),
        "executed": state.get("executed", []),
        "sells": state.get("sells", []),
        "notes": state.get("notes", []),
        "policy": state.get("policy", {}),
        "governor": state.get("governor", {}),
        "research_as_of": state.get("research_as_of"),
        # Run-health surface (added with the Monday-readiness patches): a silent
        # full_execute→advisory downgrade, a skipped run (market closed / degraded
        # research) or per-order rejections must be VISIBLE on the dashboard.
        "mode_note": state.get("mode_note"),
        "downgraded_from": state.get("downgraded_from"),
        "skipped_reason": state.get("skipped_reason"),
        "order_errors": state.get("order_errors", []),
        "perf": perf,
        "curve": curve,
    }
