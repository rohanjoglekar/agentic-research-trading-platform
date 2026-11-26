"""Deep statistics for the two autonomous paper books (equity trader + wheel).

One shared engine: both books reduce to the same primitive — a daily series of
(date, equity_dollars, benchmark_dollars) — so every metric is computed once,
here, and served to both Analytics sub-pages. The UI renders; it never computes.

Conventions (stated on the page, not hidden):
  - Daily simple returns from the equity series; annualization factor 252.
  - Risk-free rate 4.5%/yr (matches the DCF engine's Rf), converted to daily.
  - Sortino uses target-0 semideviation (LPM2), the house convention from the
    RiskTab audit — upside is not penalized, and rf is NOT subtracted from the
    downside target (only from the numerator).
  - Alpha/beta are CAPM (Jensen): daily OLS of book excess return on benchmark
    excess return; alpha is annualized by x252. R^2 is squared correlation.
  - CAGR uses trading-day count (252/n), not calendar days — the series only
    contains days the runner recorded.
  - SMALL SAMPLE: with n < ~60 daily points every annualized figure is noisy;
    the payload carries n_days so the UI can say so instead of implying four
    decimal places of truth.
"""
from __future__ import annotations

import datetime as dt
import math
from typing import Optional

TRADING_DAYS = 252
RF_ANNUAL = 0.045
RF_DAILY = RF_ANNUAL / TRADING_DAYS


def _mean(xs: list[float]) -> float:
    return sum(xs) / len(xs) if xs else 0.0


def _std(xs: list[float]) -> float:
    """Sample std (n-1) — matches pandas .std() so verification lines up."""
    n = len(xs)
    if n < 2:
        return 0.0
    m = _mean(xs)
    return math.sqrt(sum((x - m) ** 2 for x in xs) / (n - 1))


def _cov(xs: list[float], ys: list[float]) -> float:
    n = min(len(xs), len(ys))
    if n < 2:
        return 0.0
    mx, my = _mean(xs[:n]), _mean(ys[:n])
    return sum((xs[i] - mx) * (ys[i] - my) for i in range(n)) / (n - 1)


def _returns(series: list[float]) -> list[float]:
    out = []
    for a, b in zip(series, series[1:]):
        if a and a > 0 and b is not None:
            out.append(b / a - 1.0)
    return out


def _drawdown_series(equity: list[float]) -> tuple[list[float], float, int]:
    """Underwater series (pct from running peak, <= 0), max depth, and the
    longest peak-to-recovery stretch in days."""
    dd, peak = [], -math.inf
    max_dd = 0.0
    longest, cur = 0, 0
    for v in equity:
        peak = max(peak, v)
        d = (v / peak - 1.0) if peak > 0 else 0.0
        dd.append(round(d * 100, 3))
        max_dd = min(max_dd, d)
        cur = cur + 1 if d < 0 else 0
        longest = max(longest, cur)
    return dd, max_dd, longest


def _round(x: Optional[float], nd: int = 3) -> Optional[float]:
    if x is None or not math.isfinite(x):
        return None
    return round(x, nd)


def _to_daily(dates: list[str], equity: list[float],
              bench: Optional[list[float]]
              ) -> tuple[list[str], list[float], Optional[list[float]]]:
    """Collapse to ONE observation per date — the day's last mark.

    Every statistic below annualizes by sqrt(252) and derives elapsed time from
    the point COUNT, so both assume one observation per trading day. The alpha
    runner fires three times a weekday (13:45/16:45/19:45 UTC), so its curve
    arrived with 131 points across 43 dates — up to 5 in one day. Left raw that
    understated volatility by ~sqrt(3), inflated Sharpe by the same factor,
    stretched 43 trading days into "131 days" for CAGR, and — worst — paired
    three intraday book returns against one daily benchmark move, so two of
    every three were scored against a zero SPY return. That crushed beta and
    R-squared toward zero and inflated CAPM alpha (which subtracts beta x
    benchmark). Measured on the live book: beta 0.15 -> 0.42, R2 0.03 -> 0.30.

    Order is preserved and the last mark per date wins, matching how a daily
    close is taken. A series that is already daily passes through untouched.
    """
    last: dict[str, int] = {}
    for i, d in enumerate(dates):
        last[str(d)] = i
    keep = sorted(last.values())
    if len(keep) == len(dates):
        return dates, equity, bench
    return ([dates[i] for i in keep],
            [equity[i] for i in keep],
            [bench[i] for i in keep] if bench is not None else None)


def compute(dates: list[str], equity: list[float],
            bench: Optional[list[float]] = None,
            bench_label: str = "SPY") -> dict:
    """The full statistics block. `bench` may have leading Nones (the wheel's
    SPY overlay starts on day 2) — benchmark-relative stats use only the
    overlapping stretch; absolute stats use the whole equity series."""
    dates, equity, bench = _to_daily(dates, equity, bench)

    n = len(equity)
    if n < 3:
        return {"available": False, "reason": f"only {n} daily points"}

    r = _returns(equity)

    # ── absolute risk/return ────────────────────────────────────────────────
    total_ret = equity[-1] / equity[0] - 1.0
    years = len(r) / TRADING_DAYS
    cagr = (equity[-1] / equity[0]) ** (1 / years) - 1.0 if years > 0 else None
    vol_ann = _std(r) * math.sqrt(TRADING_DAYS)
    mean_exc = _mean(r) - RF_DAILY
    sharpe = (mean_exc / _std(r) * math.sqrt(TRADING_DAYS)) if _std(r) > 0 else None
    # Sortino: LPM2 with target 0 (house convention — see module docstring)
    downside = math.sqrt(_mean([min(x, 0.0) ** 2 for x in r])) if r else 0.0
    sortino = (mean_exc * TRADING_DAYS) / (downside * math.sqrt(TRADING_DAYS)) \
        if downside > 0 else None
    dd_series, max_dd, dd_days = _drawdown_series(equity)
    calmar = (cagr / abs(max_dd)) if (cagr is not None and max_dd < 0) else None
    var95 = sorted(r)[max(0, int(0.05 * len(r)) - 1)] if len(r) >= 20 else None
    tail = [x for x in sorted(r) if var95 is not None and x <= var95]
    cvar95 = _mean(tail) if tail else None

    pos = [x for x in r if x > 0]
    neg = [x for x in r if x < 0]

    # ── benchmark-relative (CAPM) ───────────────────────────────────────────
    rel = {}
    bench_curve = None
    if bench and any(b is not None for b in bench):
        pairs = [(e, b) for e, b in zip(equity, bench) if b is not None and b > 0]
        if len(pairs) >= 3:
            eq2 = [p[0] for p in pairs]
            bq2 = [p[1] for p in pairs]
            rr = _returns(eq2)
            rb = _returns(bq2)
            m = min(len(rr), len(rb))
            rr, rb = rr[:m], rb[:m]
            var_b = _std(rb) ** 2
            beta = _cov(rr, rb) / var_b if var_b > 0 else None
            alpha_d = (_mean(rr) - RF_DAILY) - beta * (_mean(rb) - RF_DAILY) \
                if beta is not None else None
            corr = (_cov(rr, rb) / (_std(rr) * _std(rb))) \
                if _std(rr) > 0 and _std(rb) > 0 else None
            act = [a - b for a, b in zip(rr, rb)]
            te = _std(act) * math.sqrt(TRADING_DAYS)
            rel = {
                "beta": _round(beta),
                "alpha_ann_pct": _round(alpha_d * TRADING_DAYS * 100, 2)
                                 if alpha_d is not None else None,
                "correlation": _round(corr),
                "r_squared": _round(corr ** 2) if corr is not None else None,
                "tracking_error_ann_pct": _round(te * 100, 2),
                "info_ratio": _round((_mean(act) * TRADING_DAYS) / te)
                              if te > 0 else None,
                "bench_total_return_pct": _round((bq2[-1] / bq2[0] - 1) * 100, 2),
                "active_return_pct": _round((total_ret - (bq2[-1] / bq2[0] - 1)) * 100, 2),
                "bench_label": bench_label,
                "n_overlap_days": m + 1,
            }
            b0 = bq2[0]
            bench_map = {d: b for (d, b) in zip(
                [dates[i] for i, (e, b) in enumerate(zip(equity, bench))
                 if b is not None and b > 0], bq2)}
            bench_curve = [
                _round(bench_map[d] / b0 * 100, 3) if d in bench_map else None
                for d in dates
            ]

    # ── rolling series (21-day window) ──────────────────────────────────────
    roll_sharpe, roll_beta = [], []
    W = 21
    rb_full = _returns([b for b in (bench or []) if b is not None and b > 0]) \
        if bench else []
    for i in range(len(r)):
        if i + 1 >= W:
            w = r[i + 1 - W:i + 1]
            s = _std(w)
            roll_sharpe.append(_round(((_mean(w) - RF_DAILY) / s) * math.sqrt(TRADING_DAYS))
                               if s > 0 else None)
            if len(rb_full) == len(r):
                wb = rb_full[i + 1 - W:i + 1]
                vb = _std(wb) ** 2
                roll_beta.append(_round(_cov(w, wb) / vb) if vb > 0 else None)
            else:
                roll_beta.append(None)
        else:
            roll_sharpe.append(None)
            roll_beta.append(None)
    # returns start at day 2 → pad to align with dates
    roll_sharpe = [None] + roll_sharpe
    roll_beta = [None] + roll_beta

    # ── monthly returns table ───────────────────────────────────────────────
    monthly: dict[str, dict] = {}
    for i in range(1, n):
        mkey = str(dates[i])[:7]
        row = monthly.setdefault(mkey, {"strat": 1.0, "bench": 1.0})
        if equity[i - 1] > 0:
            row["strat"] *= equity[i] / equity[i - 1]
        if bench and bench[i] is not None and bench[i - 1] is not None \
                and bench[i - 1] > 0:
            row["bench"] *= bench[i] / bench[i - 1]
    monthly_rows = [
        {"month": k,
         "strat_pct": _round((v["strat"] - 1) * 100, 2),
         "bench_pct": _round((v["bench"] - 1) * 100, 2) if v["bench"] != 1.0 else None}
        for k, v in sorted(monthly.items())
    ]

    # ── daily-return histogram (UI renders bars, we choose bins) ────────────
    hist = []
    if len(r) >= 10:
        lo, hi = min(r), max(r)
        span = (hi - lo) or 1e-9
        nb = min(21, max(7, len(r) // 4))
        counts = [0] * nb
        for x in r:
            counts[min(nb - 1, int((x - lo) / span * nb))] += 1
        hist = [{"mid_pct": _round((lo + (i + 0.5) * span / nb) * 100, 3),
                 "count": c} for i, c in enumerate(counts)]

    return {
        "available": True,
        "as_of": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "n_days": n,
        "since": str(dates[0]),
        "small_sample": len(r) < 60,
        "assumptions": {
            "rf_annual_pct": RF_ANNUAL * 100,
            "annualization_days": TRADING_DAYS,
            "sortino": "target-0 semideviation (LPM2)",
            "alpha_beta": "daily CAPM OLS vs benchmark, alpha annualized x252",
        },
        "headline": {
            "total_return_pct": _round(total_ret * 100, 2),
            "cagr_pct": _round(cagr * 100, 2) if cagr is not None else None,
            "vol_ann_pct": _round(vol_ann * 100, 2),
            "sharpe": _round(sharpe),
            "sortino": _round(sortino),
            "max_drawdown_pct": _round(max_dd * 100, 2),
            "longest_underwater_days": dd_days,
            "calmar": _round(calmar),
            "var95_daily_pct": _round(var95 * 100, 3) if var95 is not None else None,
            "cvar95_daily_pct": _round(cvar95 * 100, 3) if cvar95 is not None else None,
            "var95_daily_usd": _round(var95 * equity[-1], 0) if var95 is not None else None,
            "best_day_pct": _round(max(r) * 100, 3) if r else None,
            "worst_day_pct": _round(min(r) * 100, 3) if r else None,
            "positive_days_pct": _round(len(pos) / len(r) * 100, 1) if r else None,
            "avg_up_day_pct": _round(_mean(pos) * 100, 3) if pos else None,
            "avg_down_day_pct": _round(_mean(neg) * 100, 3) if neg else None,
        },
        "relative": rel,
        "charts": {
            "dates": [str(d) for d in dates],
            "equity_rebased": [_round(e / equity[0] * 100, 3) for e in equity],
            "bench_rebased": bench_curve,
            "drawdown_pct": dd_series,
            "rolling_sharpe_21d": roll_sharpe,
            "rolling_beta_21d": roll_beta,
            "daily_returns_pct": [_round(x * 100, 4) for x in r],
            "histogram": hist,
        },
        "monthly": monthly_rows,
    }


# ─── per-book entrypoints ────────────────────────────────────────────────────

def alpha_analytics() -> dict:
    """Equity trader: daily rows from the alpha runner's history (dollars)."""
    import appconfig as config
    from adapters import read_json, read_jsonl_tail
    rows = read_jsonl_tail(config.ALPHA_HISTORY_PATH, n=2000)
    rows = [r for r in rows if r.get("equity") and r.get("spy")]
    # same inception-trim as the Overview curve, so the two pages agree
    state = read_json(config.ALPHA_STATE_PATH, default=None) or {}
    start = state.get("start_capital")
    if start and len(rows) > 2:
        first_moved = next((i for i, r in enumerate(rows)
                            if abs(float(r["equity"]) - start) >= 0.01), None)
        if first_moved and first_moved > 1:
            rows = rows[first_moved - 1:]
    if len(rows) < 3:
        return {"available": False, "reason": "not enough history yet"}
    dates = [str(r["as_of"])[:10] for r in rows]
    eq = [float(r["equity"]) for r in rows]
    # Benchmark from the daily closes cache, not the value the runner recorded:
    # that was one session stale until 2026-09-17 and a lagged benchmark drives
    # beta to zero (+0.06 measured vs +0.49 correctly aligned). The recorded
    # value is the fallback for dates the cache has not reached yet.
    from adapters import rh_analytics
    closes = rh_analytics.daily_closes("SPY")
    spy = [closes.get(d) or float(r["spy"]) for d, r in zip(dates, rows)]
    spy0 = spy[0]
    bench = [s / spy0 * eq[0] for s in spy]  # SPY, same start $
    out = compute(dates, eq, bench, "SPY")
    out["book"] = "equity_trader"
    return out


def wheel_analytics() -> dict:
    """Options trader: per-day rows from trader_history + the SPY overlay the
    Overview chart already uses (spy_equity = same starting capital in SPY)."""
    from adapters import trader as trader_adapter
    payload = trader_adapter.get_trader()
    hist = payload.get("history") or []
    # `live` = the intraday anchor get_trader appends for the chart; statistics
    # need daily closes only, so it is excluded here.
    hist = [h for h in hist if h.get("equity") and not h.get("live")]
    if len(hist) < 3:
        return {"available": False, "reason": "not enough history yet"}
    dates = [str(h["date"])[:10] for h in hist]
    eq = [float(h["equity"]) for h in hist]
    bench = [float(h["spy_equity"]) if h.get("spy_equity") else None for h in hist]
    out = compute(dates, eq, bench, "SPY")
    out["book"] = "options_trader"
    # options-specific texture the stats block already carries
    st = payload.get("stats") or {}
    lt = payload.get("lifetime") or {}
    out["wheel"] = {
        "win_rate_pct": st.get("win_rate_pct"),
        "n_scored_closes": st.get("n_scored"),
        "median_captured_pct": st.get("median_captured_pct"),
        "avg_captured_pct": st.get("avg_captured_pct"),
        "avg_entry_pop_pct": _round((st.get("avg_entry_pop") or 0) * 100, 1)
                             if st.get("avg_entry_pop") is not None else None,
        "net_premium_usd": lt.get("net_premium"),
        "days_tracked": lt.get("days_tracked"),
        "total_opens": lt.get("total_opens"),
        "total_closes": lt.get("total_closes"),
        "best": st.get("best"),
        "worst": st.get("worst"),
    }
    return out
