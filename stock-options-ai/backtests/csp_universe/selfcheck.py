"""Hand-built trades through the replay: account arithmetic, sizing, gates, fills.

    python selfcheck.py
"""
import types
from array import array

import replay as R

TOL = 1e-3          # paths are stored in single precision


def cand(sym, e, x, K, path, rating="B", S=None, sector="Tech", ps=None):
    return {"sym": sym, "t": e - 1, "e": e, "x": x, "K": K, "ps": ps or path[0], "S": S or K * 1.1, "iv": 0.5,
            "pop": 0.7, "delta": -0.25, "mo": 2.5, "buf": 0.1, "otm": 0.08, "dte": 30, "score": 65.0,
            "rating": rating, "vol": 100, "de": 60, "gates": [1.0, 1.5, 2.0], "ev": path[0], "real": len(path),
            "path": array("f", path), "sector": sector, "base": True, "wide": True, "regime": "calm"}


def main() -> None:
    M = types.SimpleNamespace(dates=[f"2025-01-{d:02d}" for d in range(1, 31)],
                              ord=list(range(1000, 1030)), vix=[15.0] * 30)
    run = lambda cs, arm="base", mode="ride", **kw: R.portfolio(cs, M, arm=arm, first=4, last=25,   # noqa: E731
                                                                **{**R.MODES[mode], **kw})
    entry = R.fill_price(1.0)

    # a winner: sold at 1.00, decays to 0.15 on day 3, bought back; B rating = 2 contracts
    c1 = cand("AAA", 5, 20, 50.0, [1.0, 0.8, 0.5, 0.15] + [0.1] * 12)
    s = run([c1], mode="cut")
    assert s["trades"] == 1 and abs(s["final"] - (100_000 + (entry - R.cost_price(0.15)) * 200)) < TOL
    assert s["trade_list"][0]["reason"] == "profit" and s["trade_list"][0]["q"] == 2

    # a loser: ridden to expiry it is assigned 4 points in the money; cut, it is bought back at 3.5
    c2 = cand("BBB", 5, 9, 50.0, [1.0, 2.0, 3.5, 5.0, 4.0])
    s = run([c2])
    assert abs(s["final"] - (100_000 + (entry - 4.0) * 200)) < TOL and s["trade_list"][0]["reason"] == "assigned"
    assert abs(min(s["curve"]) - (100_000 + (entry - 5.0) * 200)) < TOL      # marked to market on the way
    s = run([c2], mode="cut")
    assert s["trade_list"][0]["reason"] == "stop" and s["trade_list"][0]["out"] == 7
    assert abs(s["final"] - (100_000 + (entry - R.cost_price(3.5)) * 200)) < TOL

    # cash-secured and concentration: a $400 strike is $40,000 a contract. The 30% sector cap
    # ($30,000) refuses it; with a wider sector cap one is taken; a per-contract cap refuses it again
    c3 = cand("CCC", 5, 20, 400.0, [8.0] * 16, rating="A")
    assert run([c3], arm="wide")["trades"] == 0
    s = run([c3], arm="wide", sector_cap=0.5)
    assert s["trades"] == 1 and s["trade_list"][0]["q"] == 1
    assert run([c3], arm="wide", sector_cap=0.5, hard_pos_cap=0.2)["trades"] == 0

    # the $100 cap binds today's list only; one position per stock; D ratings are skipped
    c4 = cand("DDD", 5, 20, 120.0, [3.0] * 16, S=130.0)
    assert run([c4])["trades"] == 0 and run([c4], arm="wide")["trades"] == 1
    assert run([cand("EEE", 5, 20, 20.0, [0.5] * 16), cand("EEE", 6, 20, 19.0, [0.4] * 15)])["trades"] == 1
    assert run([cand("FFF", 5, 20, 20.0, [0.5] * 16, rating="D")])["trades"] == 0

    # volatility gate (caution halves B from 2 to 1, paused opens nothing) and the earnings skip
    M.vix = [28.0] * 30
    assert run([c1])["trade_list"][0]["q"] == 1
    M.vix = [38.0] * 30
    assert run([c1])["trades"] == 0 and run([c1], vix_gate=False)["trades"] == 1
    M.vix = [15.0] * 30
    soon = dict(c1, de=5)
    assert run([soon])["trades"] == 0 and run([soon], earn_buffer=None)["trades"] == 1

    # fills: requote needs the next-day price to still pay the floor; stale needs it within 5% of the scan
    thin = cand("GGG", 5, 20, 50.0, [0.5] * 16, ps=1.0)       # 0.5 / 50 for 30 days = 1% a month
    assert not R.fillable(thin, 2.0) and R.fillable(thin, 1.0) and not R.fillable(thin, 2.0, "stale")
    assert R.fillable(c1, 2.0) and R.fillable(c1, 2.0, "stale")

    # pricing: implied volatility round-trips; expiry value is intrinsic
    p = R.bs_put(100, 95, 30 / 365, 0.40)
    assert abs(R.implied_vol(p, 100, 95, 30 / 365) - 0.40) < 1e-3
    assert R.bs_put(100, 95, 0, 0.4) == 0 and R.bs_put(90, 95, 0, 0.4) == 5
    print("replay self-check passed: arithmetic, sizing, gates, fills, pricing")


if __name__ == "__main__":
    main()
