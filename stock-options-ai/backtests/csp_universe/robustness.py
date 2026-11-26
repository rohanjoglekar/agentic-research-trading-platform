"""How much of a portfolio result is luck of the draw?

A $100,000 account takes a few trades a day out of dozens, so removing one trade
changes every later one. Each trial drops a random 10% of the candidates and
replays both universes; the spread of outcomes is the noise a single run hides.

    python robustness.py [trials]
"""
from __future__ import annotations

import random
import statistics
import sys

import replay as R
from common import load, save


def main(trials: int = 200) -> None:
    C, M = R.candidates(), R.Market()
    first = min(c["e"] for c in C)
    out = {}
    for mode, kw in R.MODES.items():
        diffs, base, wide, nocap = [], [], [], []
        for seed in range(trials):
            rng = random.Random(seed)
            keep = [c for c in C if rng.random() < 0.9]
            b = R.portfolio(keep, M, arm="base", first=first, **kw)["cagr"]
            w = R.portfolio(keep, M, arm="wide", first=first, **kw)["cagr"]
            n = R.portfolio(keep, M, arm="base_nocap", first=first, **kw)["cagr"]
            base.append(b); wide.append(w); nocap.append(n); diffs.append(w - b)
        q = lambda xs: [round(x * 100, 1) for x in statistics.quantiles(xs, n=20)]      # noqa: E731
        row = {"trials": trials,
               "base_median": statistics.median(base), "base_p5_p95": (q(base)[0], q(base)[-1]),
               "wide_median": statistics.median(wide), "wide_p5_p95": (q(wide)[0], q(wide)[-1]),
               "nocap_median": statistics.median(nocap), "nocap_p5_p95": (q(nocap)[0], q(nocap)[-1]),
               "wide_beats_base": sum(1 for d in diffs if d > 0) / trials,
               "nocap_beats_base": sum(1 for a, b_ in zip(nocap, base) if a > b_) / trials}
        out[mode] = row
        print(f"losers {mode}: today's list median {row['base_median'] * 100:+.1f}% a year "
              f"(5th-95th {row['base_p5_p95'][0]:+.1f} to {row['base_p5_p95'][1]:+.1f}); "
              f"today's list without the $100 cap {row['nocap_median'] * 100:+.1f}% "
              f"({row['nocap_p5_p95'][0]:+.1f} to {row['nocap_p5_p95'][1]:+.1f}), ahead in "
              f"{row['nocap_beats_base'] * 100:.0f}% of trials; wider universe {row['wide_median'] * 100:+.1f}% "
              f"({row['wide_p5_p95'][0]:+.1f} to {row['wide_p5_p95'][1]:+.1f}), ahead in "
              f"{row['wide_beats_base'] * 100:.0f}% of trials")
    res = load("results.json")
    res["robustness"] = out
    save("results.json", res)


if __name__ == "__main__":
    main(int(sys.argv[1]) if len(sys.argv) > 1 else 200)
