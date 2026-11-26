"""Step 5: the answers. Runs the replay for each universe and each rule variant
and prints the tables; writes results.json for the write-up.

    python report.py            # everything
"""
from __future__ import annotations

import math
import statistics
from collections import defaultdict

import replay as R
from common import save


def pct(x: float, d: int = 1) -> str:
    return f"{x * 100:+.{d}f}%"


def line(name: str, s: dict) -> str:
    return (f"{name:34} final ${s['final']:>9,.0f}  total {pct(s['ret']):>7}  a year {pct(s['cagr']):>7}  "
            f"worst drop {pct(s['mdd']):>7}  Sharpe {s['sharpe']:>5.2f}  trades {s['trades']:>4}  "
            f"won {s['win'] * 100:>4.0f}%  in use {s['deployed'] * 100:>3.0f}%  worst trade ${s['worst']:>7,.0f}")


def slim(s: dict) -> dict:
    return {k: (round(v, 4) if isinstance(v, float) else v) for k, v in s.items()
            if k not in ("curve", "trade_list")}


def segment(c: dict) -> str:
    who = "today's list" if c["base"] else "new names"
    return f"{who}, {'$100 or less' if c['S'] <= 100 else 'over $100'}"


def trade_table(C: list[dict], M: R.Market, *, gate: float = 2.0, stop=2.5, label: str = "") -> dict:
    """One trade per stock per expiry cycle (the first signal that filled), so a
    put that stayed attractive for three weeks is counted once, not fifteen times."""
    seen, rows = set(), defaultdict(list)
    for c in C:                                           # sorted by entry day
        if gate not in c["gates"] or not (c["base"] or c["wide"]):
            continue
        if c["de"] is not None and 0 <= c["de"] < 14:
            continue
        key = (c["sym"], c["x"])
        if key in seen or not R.fillable(c, gate):
            continue
        seen.add(key)
        d, pnl, why = R.outcome(c, stop=stop, kill=None if stop is None else 4.0)
        hold = max(M.ord[d] - M.ord[c["e"]], 1)
        rows[segment(c)].append((pnl / c["K"], hold, why, c["mo"], c["sym"], pnl * 100))
    out = {}
    print(f"\nPer-trade results by segment{label}: one trade per stock per expiry, after costs")
    print(f"{'segment':30} {'trades':>6} {'stocks':>6} {'entry %/mo':>10} {'made/trade':>10} {'per 30d held':>12} "
          f"{'won':>5} {'assigned':>8} {'stopped':>8} {'worst 5%':>9} {'days':>5}")
    for seg in ("today's list, $100 or less", "today's list, over $100", "new names, $100 or less",
                "new names, over $100"):
        rs = rows.get(seg, [])
        if not rs:
            continue
        ret = [r[0] for r in rs]
        cap_days = sum(r[1] for r in rs)
        per30 = sum(r[0] for r in rs) / cap_days * 30            # return on collateral per 30 days it was tied up
        tail = sorted(ret)[:max(1, len(ret) // 20)]
        row = {"trades": len(rs), "stocks": len({r[4] for r in rs}),
               "entry_monthly_pct": statistics.mean(r[3] for r in rs), "ret_per_trade": statistics.mean(ret),
               "ret_per_30d": per30, "win": sum(1 for x in ret if x > 0) / len(ret),
               "assigned": sum(1 for r in rs if r[2] == "assigned") / len(rs),
               "stopped": sum(1 for r in rs if r[2] in ("stop", "kill")) / len(rs),
               "tail5": statistics.mean(tail), "days": statistics.mean(r[1] for r in rs),
               "se_per_trade": statistics.stdev(ret) / math.sqrt(len(ret)) if len(ret) > 2 else None}
        out[seg] = row
        print(f"{seg:30} {row['trades']:>6} {row['stocks']:>6} {row['entry_monthly_pct']:>9.2f}% "
              f"{pct(row['ret_per_trade'], 2):>10} {pct(row['ret_per_30d'], 2):>12} {row['win'] * 100:>4.0f}% "
              f"{row['assigned'] * 100:>7.0f}% {row['stopped'] * 100:>7.0f}% {pct(row['tail5'], 1):>9} {row['days']:>5.0f}")
    return out


def bucket_table(C: list[dict], M: R.Market, *, gate: float = 2.0, stop=None, who: str = "all") -> dict:
    """The same one-trade-per-stock-per-expiry sample, cut by share price.
    who: all | base (today's list) | new (names the wider universe adds)."""
    edges = [(0, 25, "under $25"), (25, 50, "$25 to $50"), (50, 100, "$50 to $100"),
             (100, 200, "$100 to $200"), (200, 500, "$200 to $500"), (500, 1e9, "over $500")]
    seen, rows = set(), defaultdict(list)
    for c in C:
        if gate not in c["gates"] or not (c["base"] or c["wide"]):
            continue
        if (who == "base" and not c["base"]) or (who == "new" and c["base"]):
            continue
        if c["de"] is not None and 0 <= c["de"] < 14:
            continue
        key = (c["sym"], c["x"])
        if key in seen or not R.fillable(c, gate):
            continue
        seen.add(key)
        d, pnl, why = R.outcome(c, stop=stop, kill=None if stop is None else 4.0)
        name = next(n for lo, hi, n in edges if lo <= c["S"] < hi)
        rows[name].append((pnl / c["K"], max(M.ord[d] - M.ord[c["e"]], 1), why, c["mo"], c["iv"], c["sym"]))
    out = {}
    print(f"\nBy share price, {who} names ({'held to expiry' if stop is None else 'cut at 2.5x'}): "
          "one trade per stock per expiry")
    print(f"{'share price':14} {'trades':>6} {'stocks':>6} {'entry %/mo':>10} {'implied vol':>11} {'made/trade':>10} "
          f"{'per 30d held':>12} {'won':>5} {'assigned':>8} {'worst 5%':>9}")
    for _lo, _hi, name in edges:
        rs = rows.get(name, [])
        if len(rs) < 5:
            continue
        ret = [r[0] for r in rs]
        tail = sorted(ret)[:max(1, len(ret) // 20)]
        row = {"trades": len(rs), "stocks": len({r[5] for r in rs}),
               "entry_monthly_pct": statistics.mean(r[3] for r in rs), "iv": statistics.mean(r[4] for r in rs),
               "ret_per_trade": statistics.mean(ret), "ret_per_30d": sum(ret) / sum(r[1] for r in rs) * 30,
               "win": sum(1 for v in ret if v > 0) / len(ret),
               "assigned": sum(1 for r in rs if r[2] == "assigned") / len(rs), "tail5": statistics.mean(tail),
               "se_per_trade": statistics.stdev(ret) / math.sqrt(len(ret))}
        out[name] = row
        print(f"{name:14} {row['trades']:>6} {row['stocks']:>6} {row['entry_monthly_pct']:>9.2f}% "
              f"{row['iv'] * 100:>10.0f}% {pct(row['ret_per_trade'], 2):>10} {pct(row['ret_per_30d'], 2):>12} "
              f"{row['win'] * 100:>4.0f}% {row['assigned'] * 100:>7.0f}% {pct(row['tail5'], 1):>9}")
    return out


def diff_t(a: dict, b: dict) -> float:
    """t-statistic of the mean daily return difference (a minus b)."""
    ra = [a["curve"][i] / a["curve"][i - 1] - 1 for i in range(1, len(a["curve"]))]
    rb = [b["curve"][i] / b["curve"][i - 1] - 1 for i in range(1, len(b["curve"]))]
    d = [x - y for x, y in zip(ra, rb)]
    sd = statistics.stdev(d)
    return statistics.mean(d) / sd * math.sqrt(len(d)) if sd else 0.0


def yearly(s: dict, M: R.Market) -> dict:
    out, first = {}, s["first"]
    for i, v in enumerate(s["curve"]):
        y = M.dates[first + i][:4]
        out.setdefault(y, [v, v])[1] = v
    prev, res = None, {}
    for y, (a, b) in out.items():
        base = prev if prev is not None else s["curve"][0]
        res[y] = b / base - 1
        prev = b
    return res


def main() -> None:
    C = R.candidates()
    M = R.Market()
    first = min(c["e"] for c in C)
    last = len(M.dates) - 1
    print(f"Replay window {M.dates[first]} to {M.dates[last]}; {len(C):,} candidate contract-days; "
          f"{sum(1 for c in C if 2.0 in c['gates']):,} pass the 2% floor")
    spy = R.spy_total_return(M, first, last)
    yrs = (M.ord[last] - M.ord[first]) / 365.25
    print(f"S&P 500 with dividends over the window: {pct(spy)} ({pct((1 + spy) ** (1 / yrs) - 1)} a year)")
    res: dict = {"window": [M.dates[first], M.dates[last]], "years": yrs, "spy_total": spy,
                 "spy_cagr": (1 + spy) ** (1 / yrs) - 1, "candidates": len(C),
                 "pass_2pct": sum(1 for c in C if 2.0 in c["gates"])}

    res["segments_ride"] = trade_table(C, M, stop=None, label=" (held to expiry)")
    res["segments_cut"] = trade_table(C, M, stop=2.5, label=" (cut at 2.5x)")
    res["price_ride"] = bucket_table(C, M, stop=None)
    res["price_ride_base"] = bucket_table(C, M, stop=None, who="base")
    res["price_ride_new"] = bucket_table(C, M, stop=None, who="new")
    res["price_cut"] = bucket_table(C, M, stop=2.5)

    NAMES = (("base", "Today's list, $100 cap (now)"), ("base_nocap", "Today's list, no price cap"),
             ("wide_cap", "Top 300 by volume, $100 cap"), ("wide", "Top 300 by volume, no cap"))
    res["arms"], res["yearly"], res["t_vs_base"], res["curves"] = {}, {}, {}, {}
    for mode, kw in R.MODES.items():
        print(f"\nPortfolio: $100,000, the trader's rules, cash-secured; losers: {mode}")
        arms = {}
        for arm, name in NAMES:
            arms[arm] = R.portfolio(C, M, arm=arm, first=first, **kw)
            print(line(name, arms[arm]), arms[arm]["by_reason"])
        res["arms"][mode] = {k: slim(v) for k, v in arms.items()}
        res["yearly"][mode] = {k: yearly(v, M) for k, v in arms.items()}
        print(" by year:", {k: {y: pct(v) for y, v in yearly(a, M).items()} for k, a in arms.items()})
        res["t_vs_base"][mode] = {k: diff_t(arms[k], arms["base"]) for k in ("base_nocap", "wide_cap", "wide")}
        print(" daily-return t-statistic against today's list:",
              {k: round(v, 2) for k, v in res["t_vs_base"][mode].items()})
        res["curves"][mode] = {k: [round(v) for v in a["curve"]] for k, a in arms.items()}
    res["curve_dates"] = M.dates[first:last + 1]

    print("\nWider universe under concentration limits (ride)")
    res["wide_caps"] = {}
    for cap in (0.10, 0.15, 0.20, 0.30):
        s = R.portfolio(C, M, arm="wide", hard_pos_cap=cap, first=first, **R.MODES["ride"])
        res["wide_caps"][f"contract<={cap:.0%}"] = slim(s)
        print(line(f"one contract <= {cap:.0%} of account", s))
    for pc in (150, 200, 250, 500):
        s = R.portfolio(C, M, arm="wide", price_cap=pc, first=first, **R.MODES["ride"])
        res["wide_caps"][f"price<={pc}"] = slim(s)
        print(line(f"price cap ${pc}", s))
    for pc in (150, 200, 250):
        s = R.portfolio(C, M, arm="base_nocap", price_cap=pc, first=first, **R.MODES["ride"])
        res["wide_caps"][f"base price<={pc}"] = slim(s)
        print(line(f"today's list, price cap ${pc}", s))

    print("\nSensitivity: fills and costs (ride)")
    res["sens"] = {}
    for arm in ("base", "wide"):
        for nm, kw in (("limit at the scanned price (live today)", {"fill": "stale"}),
                       ("costs halved (2% a side)", {"slip": 0.02}),
                       ("costs doubled (8% a side)", {"slip": 0.08})):
            s = R.portfolio(C, M, arm=arm, first=first, **R.MODES["ride"], **kw)
            res["sens"][f"{arm}|{nm}"] = slim(s)
            print(line(f"{arm}: {nm}", s))

    print("\nRule tests: each row changes ONE rule from the live setting")
    res["rules"] = {}
    f_all = lambda v: {k: v for k in ("calm", "caution", "paused", "extreme")}   # noqa: E731
    tests = [("ride: hold losers to expiry", R.MODES["ride"]),
             ("cut: buy back at 2.5x, 4x at the latest", R.MODES["cut"]),
             ("cut at 2x", {"stop": 2.0, "kill": 4.0}), ("cut at 3x", {"stop": 3.0, "kill": 4.0}),
             ("4x exit only", {"stop": None, "kill": 4.0}),
             ("take profit at 50%", {**R.MODES["ride"], "tp": 0.50}),
             ("take profit at 65%", {**R.MODES["ride"], "tp": 0.65}),
             ("never take profit early", {**R.MODES["ride"], "tp": 1.0}),
             ("no volatility gate", {**R.MODES["ride"], "vix_gate": False}),
             ("no earnings skip", {**R.MODES["ride"], "earn_buffer": None}),
             ("cash floor 30% always", {**R.MODES["ride"], "floors": f_all(0.30)}),
             ("cash floor 5% always", {**R.MODES["ride"], "floors": f_all(0.05)}),
             ("monthly floor 1.0%", {**R.MODES["ride"], "gate": 1.0}),
             ("monthly floor 1.5%", {**R.MODES["ride"], "gate": 1.5}),
             ("monthly floor 2.5%", {**R.MODES["ride"], "gate": 2.5}),
             ("monthly floor 3.0%", {**R.MODES["ride"], "gate": 3.0})]
    for arm in ("base", "wide"):
        print(f" universe: {arm}   (t = daily-return t-statistic against the first row; beyond about 2 is unlikely to be luck)")
        ref = None
        for nm, kw in tests:
            s = R.portfolio(C, M, arm=arm, first=first, **kw)
            ref = ref or s
            t = diff_t(s, ref) if s is not ref else 0.0
            res["rules"][f"{arm}|{nm}"] = {**slim(s), "t_vs_ride": round(t, 2)}
            print(line("  " + nm, s) + f"  t {t:+.2f}")
    # the same comparison for how orders are filled
    for arm in ("base", "wide"):
        ref = R.portfolio(C, M, arm=arm, first=first, **R.MODES["ride"])
        st = R.portfolio(C, M, arm=arm, first=first, fill="stale", **R.MODES["ride"])
        res["sens"][f"{arm}|stale t"] = round(diff_t(st, ref), 2)
        print(f" {arm}: limit at the scanned price against re-quoting, t {diff_t(st, ref):+.2f}")
    save("results.json", res)


if __name__ == "__main__":
    main()
