#!/usr/bin/env python3
"""Backtested factor performance — does the research screen's signal actually predict?

For each rebalance date, ranks the universe by each PRICE-ONLY factor using only
data up to that date (no fundamental lookahead), sorts into quintiles, and measures
the forward-period return of the top quintile minus the bottom quintile (a classic
long/short factor spread) + hit rate + cumulative curve.

Writes data/robinhood/factor_performance.json (served at GET /api/robinhood/factor-performance)
and prints a report. Run on the dashboard's on-disk price caches:
    cd forge-dashboard/backend && python factor_backtest.py

NOTE: the caches are ~1 trading year, so this is an illustrative ~9-period study,
not a multi-cycle factor validation. Fundamental factors need score-history first
(see "persist research score history"); these are the price factors available today.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

import appconfig as config

OUT = config.RH_SNAPSHOT_PATH.parent / "factor_performance.json"

# Point-in-time price factors (window in trading days). Short windows so the 1y
# caches leave a usable backtest. "low_vol" negates vol so higher rank = lower vol.
FACTORS = {
    "momentum_3m": lambda px, i: px[i] / px[i - 63] - 1 if i >= 63 and px[i - 63] > 0 else None,
    "momentum_1m": lambda px, i: px[i] / px[i - 21] - 1 if i >= 21 and px[i - 21] > 0 else None,
    "trend_50d":   lambda px, i: px[i] / np.nanmean(px[i - 50:i]) - 1 if i >= 50 else None,
    "low_vol":     lambda px, i: -float(np.nanstd(np.diff(px[i - 21:i + 1]) / px[i - 21:i])) if i >= 22 else None,
}
WARMUP = 63
REBAL = 21          # ~monthly
FWD = 21            # forward holding period


def load_panel(prices_dir: Path, benchmark: str = "SPY") -> pd.DataFrame:
    series: dict[str, pd.Series] = {}
    for f in sorted(prices_dir.glob("*.json")):
        try:
            d = json.loads(f.read_text())
            dates, closes = d.get("dates"), d.get("closes")
            if dates and closes and len(dates) == len(closes):
                series[f.stem.upper()] = pd.Series([float(c) for c in closes], index=pd.to_datetime(dates))
        except Exception:
            continue
    if benchmark not in series:
        raise SystemExit(f"no {benchmark} cache in {prices_dir}")
    spine = series[benchmark].index
    return pd.DataFrame({s: v.reindex(spine) for s, v in series.items()}).sort_index().ffill()


def quintile_spread(panel: pd.DataFrame, fname: str, fn) -> dict:
    cols = [c for c in panel.columns if c != "SPY"]
    n = len(panel)
    rebals = list(range(WARMUP, n - FWD, REBAL))
    spreads, q5s, q1s = [], [], []
    for i in rebals:
        scores = {}
        for s in cols:
            px = panel[s].values.astype(float)
            if np.isnan(px[i]) or px[i] <= 0:
                continue
            try:
                val = fn(px, i)
            except Exception:
                val = None
            if val is not None and np.isfinite(val):
                scores[s] = val
        if len(scores) < 20:
            continue
        ser = pd.Series(scores).sort_values(ascending=False)
        k = max(1, len(ser) // 5)
        top, bot = ser.index[:k], ser.index[-k:]
        def fwd(syms):
            r = []
            for s in syms:
                p0, p1 = panel[s].iloc[i], panel[s].iloc[i + FWD]
                if p0 and p0 > 0 and np.isfinite(p1):
                    r.append(p1 / p0 - 1)
            return float(np.mean(r)) if r else 0.0
        q5, q1 = fwd(top), fwd(bot)
        q5s.append(q5); q1s.append(q1); spreads.append(q5 - q1)
    if not spreads:
        return {"name": fname, "n_periods": 0}
    arr = np.array(spreads)
    cum = float(np.prod([1 + x for x in arr]) - 1)
    return {
        "name": fname,
        "n_periods": len(arr),
        "mean_spread_pct": round(float(arr.mean()) * 100, 2),
        "ann_spread_pct": round((float(np.prod([1 + x for x in arr])) ** (252 / FWD / len(arr)) - 1) * 100, 1),
        "hit_rate": round(float((arr > 0).mean()) * 100, 0),
        "cum_spread_pct": round(cum * 100, 1),
        "q5_avg_pct": round(float(np.mean(q5s)) * 100, 2),
        "q1_avg_pct": round(float(np.mean(q1s)) * 100, 2),
        "curve": [round(float(x) * 100, 2) for x in np.cumsum(arr)],
    }


def main():
    panel = load_panel(config.RH_PRICES_DIR)
    out = [quintile_spread(panel, name, fn) for name, fn in FACTORS.items()]
    out.sort(key=lambda r: -(r.get("ann_spread_pct") or -1e9))
    doc = {
        "window": f"{panel.index[0].date()} → {panel.index[-1].date()}",
        "n_names": panel.shape[1] - 1,
        "rebalance_days": REBAL, "forward_days": FWD,
        "factors": out,
        "method": ("Top-quintile minus bottom-quintile forward-return spread per price-only "
                   "factor, point-in-time (no fundamental lookahead), monthly rebalance."),
        "caveat": "~1y of cached daily closes — illustrative, not a multi-cycle factor study.",
    }
    OUT.write_text(json.dumps(doc, indent=2))
    print(f"\nFACTOR PERFORMANCE  ·  {doc['window']}  ·  {doc['n_names']} names\n" + "─" * 60)
    print(f"  {'factor':<14}{'ann spread':>12}{'hit':>7}{'Q5 avg':>9}{'Q1 avg':>9}{'periods':>9}")
    for r in out:
        if r.get("n_periods"):
            print(f"  {r['name']:<14}{r['ann_spread_pct']:>11.1f}%{r['hit_rate']:>6.0f}%"
                  f"{r['q5_avg_pct']:>8.2f}%{r['q1_avg_pct']:>8.2f}%{r['n_periods']:>9}")
    print("─" * 60)
    print(f"Wrote {OUT}\n{doc['caveat']}\n")


if __name__ == "__main__":
    if str(Path(__file__).resolve().parent) not in sys.path:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
    main()
