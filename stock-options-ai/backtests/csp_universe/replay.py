"""Step 4: the replay. What the put-selling rules would have done on real
traded option prices, for the current scan universe and for a wider one.

Stage A (`build`) turns the cached chains into candidates: for every stock and
day, the put the live scan would have ranked first, under the scan's own hard
filters. Stage B (`outcome`, `portfolio`) plays the trader's rules over them.

What is real and what is modelled
- Real: every entry price, every daily mark with a trade behind it, every
  expiry settlement (stock close against strike), VIX, earnings dates.
- Modelled: a day with no trade in the contract is marked with Black-Scholes at
  the contract's last implied volatility. Fundamental and technical scores are
  held at the live scan's medians for every name, because no point-in-time
  fundamentals exist; ranking is therefore by the option-level terms only.
- Prices are each day's volume-weighted average (the option's and the
  stock's), not last trades: a single stale or off-market print would
  otherwise pick the "best" put and trip the stop.
- Signal on day t, order on day t+1, filled at that day's average price less
  costs. Two fill rules: `requote` (default) sells at the next day's price if
  the trade still clears the monthly-return floor there; `stale` is the live
  trader's behaviour today, a limit at the scanned price, which fills only if
  the market is within 5% of it.
- A stock split, spin-off or special distribution changes the contract's terms,
  so an expiry cycle containing one is left out for that stock.
- Costs: 4% of the option price each way (one tick, a cent, at least) plus 3
  cents a contract. An assigned put is closed out at the expiry close: the
  covered-call leg is not replayed, for either universe.
- Two ways of handling a loser, because the live trader does both. `cut` buys
  back at 2.5x the premium (4x at the latest). `ride` holds to expiry and takes
  the shares: what the trader does for a stock it is "prepared to own", which
  on 2026-11-20 was 16 of the 20 names in its scan.
"""
from __future__ import annotations

import bisect
import datetime as dt
import json
import math
import pickle
import sys
from array import array
from collections import defaultdict

from common import CACHE, DATA_HOST, STOCK_END, get, load, save
from config.settings import MARKET_REGIME, OPTIONS, RESERVE, RISK, SCREENER
from scoring.composite_score import composite_trade_score

R = 0.045
MIN_VOL = 10                       # contracts traded on the signal day (stand-in for OI >= 100)
SLIP, SLIP_MIN, FEE = 0.04, 0.01, 0.0003
MODES = {"ride": {"stop": None, "kill": None}, "cut": {"stop": 2.5, "kill": 4.0}}
FUND, TECH = 70.0, 40.0            # medians of the live scan's own scores on 2026-11-20
GATES = (1.0, 1.5, 2.0, 2.5, 3.0)  # monthly-return floors to compare (the live rule is 2.0)
_SQ2 = math.sqrt(2.0)


def ncdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / _SQ2))


def bs_put(S: float, K: float, T: float, sig: float) -> float:
    if T <= 0 or sig <= 0:
        return max(K - S, 0.0)
    v = sig * math.sqrt(T)
    d1 = (math.log(S / K) + (R + 0.5 * sig * sig) * T) / v
    return K * math.exp(-R * T) * ncdf(v - d1) - S * ncdf(-d1)


def implied_vol(price: float, S: float, K: float, T: float) -> float | None:
    if T <= 0 or price <= max(K * math.exp(-R * T) - S, 0.0) + 1e-6:
        return None
    lo, hi = 0.01, 5.0
    if bs_put(S, K, T, hi) < price:
        return None
    while hi - lo > 2e-4:
        mid = 0.5 * (lo + hi)
        if bs_put(S, K, T, mid) > price:
            hi = mid
        else:
            lo = mid
    return 0.5 * (lo + hi)


def regime_of(vix: float | None) -> str:
    if vix is None:
        return "caution"
    if vix >= MARKET_REGIME["vix_extreme_level"]:
        return "extreme"
    if vix >= MARKET_REGIME["vix_pause_level"]:
        return "paused"
    if vix >= MARKET_REGIME["vix_caution_level"]:
        return "caution"
    return "calm"


def fill_price(v: float, slip: float = SLIP) -> float:
    """What a sale at reference price v nets, per share."""
    return v - max(v * slip, SLIP_MIN) - FEE


def cost_price(v: float, slip: float = SLIP) -> float:
    """What buying back at reference price v costs, per share."""
    return v + max(v * slip, SLIP_MIN) + FEE


# ─── market data ─────────────────────────────────────────────────────────────

class Stock:
    __slots__ = ("di", "pos", "c", "ca", "v", "vw", "earn", "_memo")

    def __init__(self, o: dict, cal: dict, earn: list[int], vwap: dict):
        self.di, self.c, self.ca, self.v, self.vw = [], [], [], [], []
        for d, c, ca, v in zip(o["d"], o["c"], o["ca"], o["v"]):
            i = cal.get(d)
            if i is not None and c and ca:
                self.di.append(i); self.c.append(c); self.ca.append(ca); self.v.append(v)
                self.vw.append(vwap.get(d) or c)
        self.pos = {d: j for j, d in enumerate(self.di)}
        self.earn = earn
        self._memo: dict = {}

    def close(self, di: int) -> float | None:
        """Raw close on day di, or the last one before it."""
        j = self.pos.get(di)
        if j is None:
            j = bisect.bisect_right(self.di, di) - 1
        return self.c[j] if j >= 0 else None

    def avg(self, di: int) -> float | None:
        """The day's average price, or the last one before it."""
        j = self.pos.get(di)
        if j is None:
            j = bisect.bisect_right(self.di, di) - 1
        return self.vw[j] if j >= 0 else None

    def stats(self, di: int):
        """(avg volume 30d, RSI 14, distance from 52-week high, 30-day return)."""
        j = self.pos.get(di)
        if j is None:
            return None
        got = self._memo.get(j)
        if got is None:
            ca, lo = self.ca, max(0, j - 29)
            av = sum(self.v[lo:j + 1]) / (j + 1 - lo)
            rsi = None
            if j >= 14:
                ups = dns = 0.0
                for k in range(j - 13, j + 1):
                    ch = ca[k] - ca[k - 1]
                    ups += max(ch, 0.0); dns += max(-ch, 0.0)
                rsi = 100.0 if dns == 0 else 100.0 - 100.0 / (1.0 + ups / dns)
            dd = ca[j] / max(ca[max(0, j - 251):j + 1]) - 1.0
            r30 = ca[j] / ca[j - 21] - 1.0 if j >= 21 else None
            got = self._memo[j] = (av, rsi, dd, r30)
        return got

    def days_to_earnings(self, ordinal: int) -> int | None:
        k = bisect.bisect_left(self.earn, ordinal)
        return self.earn[k] - ordinal if k < len(self.earn) else None


class Market:
    def __init__(self):
        D = load("daily.json")
        self.dates = D["SPY"]["d"]
        self.cal = {d: i for i, d in enumerate(self.dates)}
        self.ord = [dt.date.fromisoformat(d).toordinal() for d in self.dates]
        E = load("edgar.json", {})
        self.sector = {s: (r.get("sector") or "Unknown") for s, r in E.items()}
        VW = load("daily_vwap.json", {})
        self.stocks = {s: Stock(o, self.cal, [dt.date.fromisoformat(x).toordinal()
                                              for x in (E.get(s) or {}).get("earnings", [])],
                                VW.get(s, {}))
                       for s, o in D.items()}
        vx = load("vix.json", {})
        self.vix, last = [], None
        for d in self.dates:
            last = vx.get(d, last)
            self.vix.append(last)
        # spin-offs and special distributions (collect_aux.distributions)
        self.events = {s: sorted(self.cal[d] for d in ds if d in self.cal)
                       for s, ds in (load("distributions.json", {}) or {}).items()}
        U = load("universe.json")
        self.baseline = set(U["baseline"])
        self.month_set = {m: set(v) for m, v in U["months"].items()}

    def in_wide(self, sym: str, di: int) -> bool:
        return sym in self.baseline or sym in self.month_set.get(self.dates[di][:7], ())

    def exp_index(self, exp: str) -> int | None:
        i = self.cal.get(exp)
        if i is None:                       # expiry on a non-trading day: last session before it
            i = bisect.bisect_right(self.dates, exp) - 1
        return i if i >= 0 else None


# ─── stage A: candidates ─────────────────────────────────────────────────────

def _liquidity(vol: float) -> float:
    """The live liquidity score (OI 50%, spread 35%, volume 15%) from volume
    alone: open interest taken as 15 days of this volume, spread as 10%."""
    return 50.0 * min(1.0, 15.0 * vol / 10_000) + 17.5 + 15.0 * min(1.0, vol / 1_000)


def build(limit: int | None = None) -> list[dict]:
    """Every (stock, day, gate) winner: the put the scan would rank first."""
    M = Market()
    files = sorted((CACHE / "chains").glob("*.json"))[:limit]
    best: dict = {}                               # (sym, t, gate) -> (score, record)
    lo_otm, hi_otm = OPTIONS["otm_pct_min"], OPTIONS["otm_pct_max"]
    for n, fp in enumerate(files):
        f = json.loads(fp.read_text())
        if not f.get("exp") or not f["k"]:
            continue
        sym, st = f["sym"], M.stocks.get(f["sym"])
        x = M.exp_index(f["exp"])
        if st is None or x is None:
            continue
        # the contract's terms change at a spin-off or special distribution
        if any(x - 46 <= ev <= x for ev in M.events.get(sym, ())):
            continue
        exp_ord = dt.date.fromisoformat(f["exp"]).toordinal()
        for j, K in enumerate(f["k"]):
            for t, _last, prem, vol in f["b"][j]:
                e = t + 1
                if e > x or e >= len(M.ord):
                    continue
                dte = exp_ord - M.ord[e]
                if dte < OPTIONS["dte_min"] or dte > OPTIONS["dte_max"] or vol < MIN_VOL \
                        or prem < OPTIONS["min_premium"]:
                    continue
                j_s = st.pos.get(t)
                if j_s is None:
                    continue
                S = st.vw[j_s]
                otm = (S - K) / S
                if otm < lo_otm or otm > hi_otm or S < SCREENER["min_price"]:
                    continue
                monthly = prem / K * 30.0 / dte * 100.0
                buf = (S - (K - prem)) / S
                if monthly < GATES[0] or buf < RISK["min_downside_buffer_pct"]:
                    continue
                T = dte / 365.0
                iv = implied_vol(prem, S, K, T)
                if iv is None or iv < SCREENER["min_iv"] or iv > SCREENER["max_iv"]:
                    continue
                v = iv * math.sqrt(T)
                d1 = (math.log(S / K) + (R + 0.5 * iv * iv) * T) / v
                pop = max(0.05, min(0.95, ncdf(d1 - v)))
                delta = ncdf(d1) - 1.0
                if pop < OPTIONS["min_pop"] or abs(delta) > OPTIONS["delta_target_high"] + 0.05:
                    continue
                av, rsi, dd, r30 = st.stats(t)
                if av < SCREENER["min_avg_volume_30d"]:
                    continue
                de = st.days_to_earnings(M.ord[t])
                flags = []
                if pop < OPTIONS["preferred_pop_min"]:
                    flags.append(f"Below preferred POP ({pop:.0%})")
                if abs(delta) < OPTIONS["delta_target_low"] - 0.03:
                    flags.append(f"Delta {abs(delta):.2f} below sweet-spot")
                if de is not None and 0 <= de <= 30:
                    flags.append(f"Earnings in {de} days")
                reg = regime_of(M.vix[t])
                sc = composite_trade_score(
                    monthly_return_pct=monthly, pop_estimate=pop, liquidity_score=_liquidity(vol),
                    fundamental_score=FUND, technical_score=TECH, iv_rank=50.0, days_to_earnings=de,
                    risk_flags=flags, strategy="CSP", rsi_14=rsi, drawdown_from_52w_high=dd,
                    recent_30d_return_pct=r30, regime=reg, downside_buffer_pct=buf * 100.0)
                score = sc["final_score"]
                rec = None
                for g in GATES:
                    if monthly < g:
                        break
                    cur = best.get((sym, t, g))
                    if cur is None or score > cur[0]:
                        if rec is None:
                            rec = {"sym": sym, "t": t, "e": e, "x": x, "K": K, "ps": prem, "S": S,
                                   "iv": iv, "pop": pop, "delta": delta, "mo": monthly, "buf": buf,
                                   "otm": otm, "dte": dte, "score": score, "rating": sc["rating"],
                                   "vol": vol, "de": de, "file": fp.name, "j": j}
                        best[(sym, t, g)] = (score, rec)
        if n % 500 == 0:
            print(f"  scanned {n}/{len(files)} chains, {len(best)} winners", file=sys.stderr, flush=True)

    # one record per distinct contract-day, tagged with the gates it wins
    recs: dict = {}
    for (sym, t, g), (_s, rec) in best.items():
        r = recs.setdefault((sym, t, rec["file"], rec["j"]), rec)
        r.setdefault("gates", []).append(g)
    by_file = defaultdict(list)
    for r in recs.values():
        by_file[r["file"]].append(r)

    # paths: the contract's daily value from the entry day to expiry
    for n, (name, rs) in enumerate(sorted(by_file.items())):
        f = json.loads((CACHE / "chains" / name).read_text())
        st = M.stocks[f["sym"]]
        exp_ord = dt.date.fromisoformat(f["exp"]).toordinal()
        bars_of: dict = {}
        for r in rs:
            bars = bars_of.get(r["j"])
            if bars is None:
                bars = bars_of[r["j"]] = {b[0]: b for b in f["b"][r["j"]]}
            e, x, K = r["e"], r["x"], r["K"]
            eb = bars.get(e)
            r["ev"] = eb[2] if eb and eb[3] >= 1 else None        # entry-day VWAP, None = no trade
            path, sig, real = array("f"), r["iv"], 0
            for d in range(e, x + 1):
                S_d = st.close(d) if d == x else st.avg(d)
                T_d = (exp_ord - M.ord[d]) / 365.0
                b = bars.get(d)
                if d == x:
                    m = max(K - S_d, 0.0)                         # settlement: close against strike
                elif b:
                    m, real = b[2], real + 1
                    got = implied_vol(m, S_d, K, T_d)
                    sig = got if got else sig
                else:
                    m = bs_put(S_d, K, T_d, sig)
                path.append(m)
            r["path"], r["real"] = path, real
            r["sector"] = M.sector.get(r["sym"], "Unknown")
            r["base"] = r["sym"] in M.baseline
            r["wide"] = M.in_wide(r["sym"], r["t"])
            r["regime"] = regime_of(M.vix[r["t"]])
            del r["file"], r["j"]
        if n % 500 == 0:
            print(f"  paths {n}/{len(by_file)}", file=sys.stderr, flush=True)
    out = sorted(recs.values(), key=lambda r: (r["e"], -r["score"]))
    with open(CACHE / "candidates.pkl", "wb") as fh:
        pickle.dump(out, fh, protocol=pickle.HIGHEST_PROTOCOL)
    return out


def candidates() -> list[dict]:
    with open(CACHE / "candidates.pkl", "rb") as fh:
        return pickle.load(fh)


# ─── stage B: the rules ──────────────────────────────────────────────────────

def outcome(c: dict, tp: float = 0.80, stop: float | None = 2.5, kill: float | None = 4.0,
            slip: float = SLIP):
    """(exit day, profit per share, reason) for one contract sold at its entry
    VWAP. tp = share of the premium to capture before buying back (1.0 = never);
    stop/kill = buy back when the option trades at this multiple of the entry."""
    entry, path, e, x = fill_price(c["ev"], slip), c["path"], c["e"], c["x"]
    for d in range(e + 1, x):
        ratio = path[d - e] / entry
        if kill and ratio >= kill:
            return d, entry - cost_price(path[d - e], slip), "kill"
        if tp < 1.0 and 1.0 - ratio >= tp:
            return d, entry - cost_price(path[d - e], slip), "profit"
        if stop and ratio >= stop:
            return d, entry - cost_price(path[d - e], slip), "stop"
    settle = path[x - e]
    return x, entry - settle, ("assigned" if settle > 0 else "expired")


def fillable(c: dict, gate: float = 2.0, fill: str = "requote") -> bool:
    """Did the order placed the day after the signal trade?"""
    if c["ev"] is None:
        return False
    if fill == "stale":                       # a limit at the scanned price
        return c["ev"] >= 0.95 * c["ps"]
    return c["ev"] / c["K"] * 30.0 / max(c["dte"], 1) * 100.0 >= gate   # still clears the floor


def eligible(c: dict, *, arm: str, gate: float = 2.0, earn_buffer: int | None = 14,
             price_cap: float | None = None) -> bool:
    """arm: base (today's list, $100 cap) | base_nocap | wide_cap | wide."""
    if gate not in c["gates"]:
        return False
    if arm.startswith("base") and not c["base"]:
        return False
    if arm.startswith("wide") and not c["wide"]:
        return False
    cap = price_cap if price_cap is not None else (SCREENER["max_price"] if arm in ("base", "wide_cap") else None)
    if cap is not None and c["S"] > cap:
        return False
    if earn_buffer and c["de"] is not None and 0 <= c["de"] < earn_buffer:
        return False
    return True


_QTY = RISK["qty_by_rating"]
_MULT = {"calm": 1.0, "caution": 0.5, "paused": 0.0, "extreme": 0.0}


def portfolio(cands: list[dict], M: Market, *, arm: str, gate: float = 2.0, tp: float = 0.80,
              stop: float | None = 2.5, kill: float | None = 4.0, earn_buffer: int | None = 14,
              vix_gate: bool = True, floors: dict | None = None, slip: float = SLIP,
              start: float = 100_000.0, pos_pct: float | None = None, hard_pos_cap: float | None = None,
              sector_cap: float | None = None, price_cap: float | None = None,
              fill: str = "requote", first: int | None = None, last: int | None = None) -> dict:
    """Run one account through the trader's rules, cash-secured: collateral plus
    the regime's cash floor never exceeds equity."""
    floors = floors or RESERVE["base_floor_by_regime"]
    pos_pct = RISK["max_position_pct"] if pos_pct is None else pos_pct
    sector_cap = RISK["max_sector_concentration"] if sector_cap is None else sector_cap
    by_e = defaultdict(list)
    for c in cands:
        if eligible(c, arm=arm, gate=gate, earn_buffer=earn_buffer, price_cap=price_cap):
            by_e[c["e"]].append(c)
    first = min(by_e) if first is None else first
    last = (len(M.dates) - 1) if last is None else last
    cash, open_pos, curve, trades = start, [], [], []
    dep_sum = npos_sum = 0.0
    for di in range(first, last + 1):
        # 1. manage what is open, on today's prices
        keep = []
        for p in open_pos:
            c, q = p["c"], p["q"]
            m = c["path"][di - c["e"]] if di <= c["x"] else 0.0
            reason = None
            if di >= c["x"]:
                reason, pay = ("assigned" if m > 0 else "expired"), m
            elif di > c["e"]:
                ratio = m / p["entry"]
                if kill and ratio >= kill:
                    reason = "kill"
                elif tp < 1.0 and 1.0 - ratio >= tp:
                    reason = "profit"
                elif stop and ratio >= stop:
                    reason = "stop"
                pay = cost_price(m, slip) if reason else 0.0
            if reason:
                cash -= pay * 100 * q
                trades.append({"sym": c["sym"], "e": c["e"], "out": di, "K": c["K"], "q": q,
                               "pnl": (p["entry"] - pay) * 100 * q, "reason": reason, "S": c["S"]})
            else:
                p["mark"] = m
                keep.append(p)
        open_pos = keep
        collateral = sum(p["c"]["K"] * 100 * p["q"] for p in open_pos)
        equity = cash - sum(p["mark"] * 100 * p["q"] for p in open_pos)
        curve.append(equity)
        dep_sum += collateral / equity if equity > 0 else 0.0
        npos_sum += len(open_pos)
        # 2. open new ones from yesterday's scan, best first
        todays = by_e.get(di)
        if not todays or equity <= 0:
            continue
        reg = regime_of(M.vix[di - 1])
        mult = _MULT[reg] if vix_gate else 1.0
        if mult == 0.0:
            continue
        deployable = equity - collateral - floors[reg] * equity
        held = {p["c"]["sym"] for p in open_pos}
        sec_used = defaultdict(float)
        for p in open_pos:
            sec_used[_bucket(p["c"])] += p["c"]["K"] * 100 * p["q"]
        for c in todays:                        # already sorted by score, best first
            if deployable <= 0:
                break
            if c["sym"] in held:
                continue
            want = int(round(_QTY.get(c["rating"], 0) * mult))
            coll = c["K"] * 100
            if want == 0 or (hard_pos_cap and coll > hard_pos_cap * equity):
                continue
            q = min(want, max(1, int(pos_pct * equity // coll)), int(deployable // coll),
                    int((sector_cap * equity - sec_used[_bucket(c)]) // coll))
            if q <= 0 or not fillable(c, gate, fill):
                continue
            entry = fill_price(c["ev"], slip)
            cash += entry * 100 * q
            open_pos.append({"c": c, "q": q, "entry": entry, "mark": c["path"][0]})
            held.add(c["sym"])
            deployable -= coll * q
            sec_used[_bucket(c)] += coll * q
    return _stats(curve, trades, M, first, last, start, dep_sum, npos_sum)


def _bucket(c: dict) -> str:
    return c["sector"] if c["sector"] != "Unknown" else "?" + c["sym"]


def _stats(curve, trades, M, first, last, start, dep_sum, npos_sum) -> dict:
    n = len(curve)
    years = (M.ord[last] - M.ord[first]) / 365.25
    rets = [curve[i] / curve[i - 1] - 1.0 for i in range(1, n) if curve[i - 1] > 0]
    mu = sum(rets) / len(rets)
    sd = math.sqrt(sum((r - mu) ** 2 for r in rets) / (len(rets) - 1))
    dn = math.sqrt(sum(min(r, 0.0) ** 2 for r in rets) / len(rets))
    peak, mdd = curve[0], 0.0
    for v in curve:
        peak = max(peak, v)
        mdd = min(mdd, v / peak - 1.0)
    wins = [t for t in trades if t["pnl"] > 0]
    by = defaultdict(lambda: [0, 0.0])
    for t in trades:
        by[t["reason"]][0] += 1
        by[t["reason"]][1] += t["pnl"]
    return {"final": curve[-1], "ret": curve[-1] / start - 1.0,
            "cagr": (curve[-1] / start) ** (1 / years) - 1.0 if curve[-1] > 0 else -1.0,
            "mdd": mdd, "sharpe": mu / sd * math.sqrt(252) if sd else 0.0,
            "sortino": mu / dn * math.sqrt(252) if dn else 0.0, "years": years,
            "trades": len(trades), "win": len(wins) / len(trades) if trades else 0.0,
            "avg_pnl": sum(t["pnl"] for t in trades) / len(trades) if trades else 0.0,
            "worst": min((t["pnl"] for t in trades), default=0.0),
            "deployed": dep_sum / n, "positions": npos_sum / n,
            "by_reason": {k: (v[0], round(v[1])) for k, v in by.items()},
            "curve": curve, "first": first, "last": last, "trade_list": trades}


def spy_total_return(M: Market, first: int, last: int) -> float:
    got = load("spy_tr.json")
    if not got:
        got = {b["t"][:10]: b["c"] for page in [get(DATA_HOST, "/v2/stocks/SPY/bars", {
            "timeframe": "1Day", "start": "2023-10-01", "end": STOCK_END, "limit": 10000,
            "adjustment": "all", "feed": "sip"})] for b in page.get("bars", [])}
        save("spy_tr.json", got)
    return got[M.dates[last]] / got[M.dates[first]] - 1.0


if __name__ == "__main__":
    lim = int(sys.argv[2]) if len(sys.argv) > 2 else None
    if sys.argv[1] == "build":
        c = build(lim)
        print(len(c), "candidate contract-days;", sum(1 for r in c if 2.0 in r["gates"]), "win the 2% gate")
