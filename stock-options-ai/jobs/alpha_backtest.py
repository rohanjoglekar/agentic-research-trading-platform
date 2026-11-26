#!/usr/bin/env python3
"""
jobs/alpha_backtest.py

Backtest the long-only core-satellite "beat the S&P 500" portfolio on the
on-disk price caches the dashboard already maintains
(forge-dashboard/data/robinhood/prices/*.json). It reuses the SAME
strategies.alpha_portfolio.build_target() the live runner uses, so this tests the
deployed construction logic — only the *ranking source* differs:

  • LIVE runner  : the research composite (value/quality/growth/momentum/DCF).
  • BACKTEST     : a POINT-IN-TIME price-momentum proxy (trailing momentum +
                   trend + low-vol), computed using only data up to each rebalance
                   date — NO fundamental lookahead. This is deliberately honest:
                   we do not apply today's fundamental snapshot to the past.

Benchmark = SPY buy-and-hold over the same window (price return; both sides exclude
dividends, so the comparison is apples-to-apples).

Run (from stock-options-ai/, any env with numpy+pandas):
    python jobs/alpha_backtest.py
    python jobs/alpha_backtest.py --rebalance-days 21 --satellite 15 --core-weight 0.55

NOTE: the bundled caches are ~1 trading year, so this is an *illustrative*
~9-month, ~monthly-rebalanced window — enough to validate the machinery and the
tracking-error/active-return behavior, not a multi-cycle factor study. A longer
study needs deeper history (see the doc's "backtested factor validation" item).
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

HERE = Path(__file__).resolve()
SOAI_ROOT = HERE.parents[1]
if str(SOAI_ROOT) not in sys.path:
    sys.path.insert(0, str(SOAI_ROOT))

from config.settings import ALPHA                       # noqa: E402
from strategies.alpha_portfolio import RankedName, build_target  # noqa: E402

# The dashboard's data dir (sibling project). Override with --data.
DEFAULT_DATA = SOAI_ROOT.parent / "forge-dashboard" / "data" / "robinhood"


# ─── data loading ──────────────────────────────────────────────────────────────

def load_panel(data_dir: Path, benchmark: str) -> tuple[pd.DataFrame, dict]:
    """Return (closes DataFrame indexed by date, columns=symbols), and a
    {symbol: sector} map from research.json. Spine = benchmark's trading days."""
    pdir = data_dir / "prices"
    series: dict[str, pd.Series] = {}
    for f in sorted(pdir.glob("*.json")):
        try:
            d = json.loads(f.read_text())
            dates, closes = d.get("dates"), d.get("closes")
            if not dates or not closes or len(dates) != len(closes):
                continue
            series[f.stem.upper()] = pd.Series(
                [float(c) for c in closes], index=pd.to_datetime(dates)
            )
        except Exception:
            continue
    if benchmark not in series:
        raise SystemExit(f"benchmark {benchmark} has no price cache in {pdir}")

    spine = series[benchmark].index
    panel = pd.DataFrame({s: ser.reindex(spine) for s, ser in series.items()}).sort_index()
    panel = panel.ffill()

    sectors: dict[str, str] = {}
    rj = data_dir / "research.json"
    if rj.exists():
        try:
            for r in json.loads(rj.read_text()).get("stocks", []):
                sectors[str(r.get("symbol", "")).upper()] = r.get("sector") or "Unknown"
        except Exception:
            pass
    return panel, sectors


# ─── point-in-time price ranking (no fundamental lookahead) ─────────────────────

def rank_at(panel: pd.DataFrame, i: int, candidates: list[str], sectors: dict, cfg: dict) -> list:
    """Cross-sectional price-momentum/trend/low-vol blend → RankedName list,
    using only closes at or before row i."""
    windows = cfg.get("bt_mom_windows", [63, 21])
    sma_n = int(cfg.get("bt_trend_sma", 50))
    vol_n = int(cfg.get("bt_vol_window", 21))
    need = max(max(windows), sma_n, vol_n) + 1

    rows: dict[str, dict] = {}
    for sym in candidates:
        if sym not in panel.columns:
            continue
        col = panel[sym].iloc[: i + 1]
        if col.notna().sum() < need or pd.isna(col.iloc[-1]) or col.iloc[-1] <= 0:
            continue
        px = col.values.astype(float)
        last = px[-1]
        feats = {}
        ok = True
        for w in windows:
            base = px[-1 - w]
            if base <= 0 or np.isnan(base):
                ok = False
                break
            feats[f"mom{w}"] = last / base - 1.0
        if not ok:
            continue
        sma = np.nanmean(px[-sma_n:])
        feats["trend"] = (last / sma - 1.0) if sma > 0 else 0.0
        rets = np.diff(px[-(vol_n + 1):]) / px[-(vol_n + 1):-1]
        feats["nvol"] = -float(np.nanstd(rets))      # low vol is good
        rows[sym] = feats
    if not rows:
        return []

    df = pd.DataFrame(rows).T
    # cross-sectional percentile rank per feature → weighted blend → 0..100
    wmap = {f"mom{windows[0]}": 0.45}
    if len(windows) > 1:
        wmap[f"mom{windows[1]}"] = 0.25
    wmap["trend"] = 0.20
    wmap["nvol"] = 0.10
    blend = pd.Series(0.0, index=df.index)
    for feat, wt in wmap.items():
        if feat in df:
            blend += wt * df[feat].rank(pct=True)
    score = (blend / sum(wmap.values()) * 100).round(1)
    return [RankedName(symbol=s, score=float(score[s]), sector=sectors.get(s, "Unknown"))
            for s in score.index]


# ─── simulation ─────────────────────────────────────────────────────────────────

def simulate(panel: pd.DataFrame, sectors: dict, cfg: dict) -> dict:
    bench = cfg["benchmark"]
    core_syms = [s.upper() for s in cfg.get("core_etfs", {})]
    reserve_etf = str(cfg.get("reserve_etf", "BIL")).upper()
    candidates = [s for s in panel.columns
                  if s != bench and s not in core_syms and s != reserve_etf]

    warmup = int(cfg.get("bt_warmup", 63))
    rb_days = int(cfg.get("bt_rebalance_days", 21))
    # Honest friction: modeled commission + slippage, charged per side on traded notional.
    cost_bps = float(cfg.get("transaction_cost_bps", 1.0)) + float(cfg.get("slippage_bps", 0.0))
    cap0 = float(cfg.get("start_capital", 100_000.0))

    n = len(panel)
    start = max(warmup, max(cfg.get("bt_mom_windows", [63])) + 1)
    if start >= n - 5:
        raise SystemExit("not enough history in the caches for the chosen windows")
    rebal = set(range(start, n, rb_days))

    cash = cap0
    shares: dict[str, float] = {}
    first_held: dict = {}                     # symbol -> date it entered the book
    min_hold = int(cfg.get("sell_min_hold_days", 0))
    anchors = set(core_syms) | {reserve_etf}
    eq_curve, dates = [], []
    turnovers, rebal_dates = [], []

    for i in range(start, n):
        px = panel.iloc[i]

        def mv():
            return sum(sh * px[s] for s, sh in shares.items()
                       if s in px.index and not pd.isna(px[s]))

        if i in rebal:
            equity = cash + mv()
            ranked = rank_at(panel, i, candidates, sectors, cfg)
            prices_t = {}
            for s in [r.symbol for r in ranked] + core_syms:
                v = px.get(s)
                if v is not None and not pd.isna(v) and v > 0:
                    prices_t[s] = float(v)
            today = panel.index[i]
            held = {s for s, sh in shares.items() if sh > 0 and s not in anchors}
            locked = {s for s in held if (today - first_held[s]).days < min_hold}
            book = build_target(ranked, prices_t, equity, cfg, held=held, locked=locked)

            tgt_dollars = book.dollars
            traded = 0.0
            for s in set(list(shares.keys()) + list(tgt_dollars.keys())):
                cur = shares.get(s, 0.0) * (prices_t.get(s, px.get(s, 0.0) or 0.0))
                tgt = tgt_dollars.get(s, 0.0)
                traded += abs(tgt - cur)
            cost = traded * cost_bps / 10_000.0

            shares = {s: d / prices_t[s] for s, d in tgt_dollars.items() if prices_t.get(s)}
            first_held = {s: first_held.get(s, today) for s in shares if s not in anchors}
            cash = equity - sum(tgt_dollars.values()) - cost
            turnovers.append(traded / equity if equity else 0.0)
            rebal_dates.append(panel.index[i])

        eq_curve.append(cash + mv())
        dates.append(panel.index[i])

    strat = pd.Series(eq_curve, index=pd.DatetimeIndex(dates))
    spy = panel[bench].iloc[start:n]
    spy = spy / spy.iloc[0] * cap0
    spy.index = strat.index
    return {
        "strat": strat, "spy": spy,
        "turnovers": turnovers, "rebal_dates": rebal_dates,
        "cap0": cap0, "n_rebal": len(rebal_dates),
    }


# ─── metrics + report ────────────────────────────────────────────────────────

def _stats(curve: pd.Series) -> dict:
    rets = curve.pct_change().dropna()
    days = (curve.index[-1] - curve.index[0]).days or 1
    yrs = days / 365.25
    total = curve.iloc[-1] / curve.iloc[0] - 1
    cagr = (curve.iloc[-1] / curve.iloc[0]) ** (1 / yrs) - 1 if yrs > 0 else float("nan")
    vol = rets.std() * np.sqrt(252)
    sharpe = (rets.mean() * 252) / vol if vol > 0 else float("nan")
    dd = (curve / curve.cummax() - 1).min()
    return {"total": total, "cagr": cagr, "vol": vol, "sharpe": sharpe, "maxdd": dd,
            "final": curve.iloc[-1], "rets": rets}


def report(res: dict, res_legacy: dict | None = None):
    s, b = _stats(res["strat"]), _stats(res["spy"])
    sr, br = s["rets"].align(b["rets"], join="inner")
    active = sr - br
    te = active.std() * np.sqrt(252)
    days = (res["strat"].index[-1] - res["strat"].index[0]).days or 1
    yrs = days / 365.25
    ann_active = (1 + s["total"]) ** (1 / yrs) - (1 + b["total"]) ** (1 / yrs) if yrs > 0 else float("nan")
    ir = ann_active / te if te > 0 else float("nan")
    beta = np.cov(sr, br)[0, 1] / np.var(br) if np.var(br) > 0 else float("nan")
    win = float((active > 0).mean())
    avg_turn = float(np.mean(res["turnovers"])) if res["turnovers"] else 0.0

    def pct(x): return f"{x*100:+.1f}%"

    print("\n" + "═" * 68)
    print("  ALPHA PORTFOLIO BACKTEST — core-satellite vs S&P 500 (SPY)")
    print("═" * 68)
    print(f"  Window      {res['strat'].index[0].date()} → {res['strat'].index[-1].date()}"
          f"  ({days} days, {yrs:.2f} yr)   ·   {res['n_rebal']} rebalances")
    print(f"  Start       ${res['cap0']:,.0f}   ·   avg turnover/rebalance {avg_turn*100:.0f}%")
    print("-" * 68)
    print(f"  {'':<14}{'STRATEGY':>16}{'SPY (S&P 500)':>18}")
    print(f"  {'Final value':<14}{'$'+format(s['final'],',.0f'):>16}{'$'+format(b['final'],',.0f'):>18}")
    print(f"  {'Total return':<14}{pct(s['total']):>16}{pct(b['total']):>18}")
    print(f"  {'CAGR':<14}{pct(s['cagr']):>16}{pct(b['cagr']):>18}")
    print(f"  {'Ann. vol':<14}{pct(s['vol']):>16}{pct(b['vol']):>18}")
    print(f"  {'Sharpe':<14}{s['sharpe']:>16.2f}{b['sharpe']:>18.2f}")
    print(f"  {'Max drawdown':<14}{pct(s['maxdd']):>16}{pct(b['maxdd']):>18}")
    print("-" * 68)
    print("  VS BENCHMARK")
    print(f"    Excess return (total) {pct(s['total']-b['total'])}   ·   ann. active {pct(ann_active)}")
    print(f"    Tracking error {te*100:.1f}%   ·   information ratio {ir:.2f}   ·   beta {beta:.2f}")
    print(f"    Days beating SPY {win*100:.0f}%")
    verdict = "BEAT" if s["total"] > b["total"] else "TRAILED"
    print(f"\n  → Strategy {verdict} the S&P 500 by {pct(s['total']-b['total'])} over the window.")

    if res_legacy is not None:
        lg = _stats(res_legacy["strat"])
        print("-" * 68)
        print("  POSTURE COMPARISON (same ranking + friction, different sleeve mix)")
        print(f"    Active-conviction (core ~12% + reserve)  total {pct(s['total'])}  "
              f"vs SPY {pct(s['total']-b['total'])}")
        print(f"    Legacy closet-index (core 55%)           total {pct(lg['total'])}  "
              f"vs SPY {pct(lg['total']-b['total'])}")
        edge = s["total"] - lg["total"]
        print(f"    → the active-conviction posture {'adds' if edge>=0 else 'subtracts'} "
              f"{pct(edge)} vs the old VOO-heavy book.")
    print("═" * 68)
    print("  NOTE: ranking here is a point-in-time PRICE proxy (no fundamental")
    print("  lookahead); the live runner uses the full research composite (so the")
    print("  conviction/DCF tilt and the DYNAMIC reserve aren't exercised here — the")
    print("  reserve is held flat). Window is short (1y caches). Illustrative.\n")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default=str(DEFAULT_DATA), help="dashboard data dir")
    ap.add_argument("--rebalance-days", type=int)
    ap.add_argument("--satellite", type=int, help="satellite_max_names override")
    ap.add_argument("--core-weight", type=float)
    ap.add_argument("--min-score", type=float)
    args = ap.parse_args()

    cfg = dict(ALPHA)
    # The price-only backtest can't compute the DCF "opportunity count" that sizes the
    # live reserve, so pin the reserve flat here (a small, honest cash buffer) rather
    # than let it sit at the ceiling every rebalance. Construction/caps/weighting are
    # still exactly what deploys.
    cfg["reserve_floor"] = cfg["reserve_ceiling"] = 0.02
    if args.rebalance_days:
        cfg["bt_rebalance_days"] = args.rebalance_days
    if args.satellite:
        cfg["satellite_max_names"] = args.satellite
    if args.core_weight is not None:
        cfg["core_weight"] = args.core_weight
    if args.min_score is not None:
        cfg["satellite_min_score"] = args.min_score

    # Legacy "closet-index" posture, to quantify what the new mix changes vs the old
    # VOO-heavy book (same ranking + friction; only the sleeve mix differs).
    legacy = dict(cfg)
    legacy.update({"core_weight": 0.55, "satellite_max_names": 20, "weight_scheme": "score",
                   "dcf_tilt": 0.0, "max_position_weight": 0.06, "max_sector_weight": 0.35})

    panel, sectors = load_panel(Path(args.data), cfg["benchmark"])
    print(f"Loaded {panel.shape[1]} symbols × {panel.shape[0]} days "
          f"({panel.index[0].date()} → {panel.index[-1].date()}); "
          f"{len(sectors)} sector tags from research.json")
    res = simulate(panel, sectors, cfg)
    res_legacy = simulate(panel, sectors, legacy)
    report(res, res_legacy)


if __name__ == "__main__":
    main()
