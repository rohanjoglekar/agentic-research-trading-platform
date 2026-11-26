"""Step 2: prices. Daily stock bars for every universe name, then for each
(stock, monthly expiration) the real traded daily prices of its out-of-the-money
puts over the 60 days before expiry. Resumable: a finished pair is a file.

    python collect_data.py daily       # stock bars (about 2 minutes)
    python collect_data.py chains      # option prices (about an hour, throttled)
"""
from __future__ import annotations

import datetime as dt
import math
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

from common import CACHE, DATA_HOST, END, STOCK_END, TRADING_HOST, load, paged, save

OLD_ROOT = {"XYZ": "SQ", "GAP": "GPS", "PSKY": "PARA"}     # option roots before the rename
ENTRY_MAX_DTE, ENTRY_MIN_DTE = 60, 14
BAND_LOW = 0.74          # strikes down to 26% below the lowest close in the entry window
# Skip a cycle whose stock never showed 22% realised volatility in the entry
# window: a put on it cannot pay 2% a month inside the delta limits (that needs
# roughly 28% implied), so the scan's own filters would drop it anyway.
HV_MIN = 0.22
VOL_MIN = 1_000_000      # the screener's 30-day average volume floor (shares)


def daily() -> dict:
    got = load("daily.json")
    if got:
        return got
    uni = load("universe.json")
    syms = sorted(set(uni["baseline"]) | {s for v in uni["months"].values() for s in v} | {"SPY"})
    out: dict = {s: {"d": [], "c": [], "ca": [], "v": []} for s in syms}
    for adj, key in (("raw", "c"), ("split", "ca")):
        for i in range(0, len(syms), 100):
            chunk = syms[i:i + 100]
            for page in paged(DATA_HOST, "/v2/stocks/bars",
                              {"symbols": ",".join(chunk), "timeframe": "1Day", "start": "2023-10-01",
                               "end": STOCK_END, "limit": 10000, "adjustment": adj, "feed": "sip"}, "bars"):
                for sym, bars in (page or {}).items():
                    o = out[sym]
                    for b in bars:
                        if key == "c":
                            o["d"].append(b["t"][:10]); o["c"].append(b["c"]); o["v"].append(b["v"])
                        else:
                            o["ca"].append(b["c"])
            print(f"  daily {adj} {min(i + 100, len(syms))}/{len(syms)}", file=sys.stderr, flush=True)
    out = {s: o for s, o in out.items() if o["d"] and len(o["d"]) == len(o["ca"])}
    save("daily.json", out)
    return out


def daily_vwap() -> dict:
    """{symbol: {date: raw volume-weighted price}} — the day's average stock
    price, paired with the option's own day average when pricing a signal."""
    got = load("daily_vwap.json")
    if got:
        return got
    syms = sorted(load("daily.json"))
    out: dict = {s: {} for s in syms}
    for i in range(0, len(syms), 100):
        for page in paged(DATA_HOST, "/v2/stocks/bars",
                          {"symbols": ",".join(syms[i:i + 100]), "timeframe": "1Day", "start": "2023-10-01",
                           "end": STOCK_END, "limit": 10000, "adjustment": "raw", "feed": "sip"}, "bars"):
            for sym, bars in (page or {}).items():
                for b in bars:
                    out[sym][b["t"][:10]] = b.get("vw") or b["c"]
            time.sleep(1.5)                 # the chain pull is using the same host
    save("daily_vwap.json", out)
    return out


def third_fridays() -> list[dt.date]:
    out, y, m = [], 2024, 3
    while (y, m) <= (int(END[:4]), int(END[5:7])):
        d = dt.date(y, m, 15)
        d += dt.timedelta(days=(4 - d.weekday()) % 7)
        out.append(d)
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return out


def _hv(closes: list[float], n: int) -> float | None:
    if len(closes) < n + 1:
        return None
    r = [math.log(closes[i] / closes[i - 1]) for i in range(len(closes) - n, len(closes))
         if closes[i] > 0 and closes[i - 1] > 0]
    if len(r) < n - 2:
        return None
    mu = sum(r) / len(r)
    return math.sqrt(sum((x - mu) ** 2 for x in r) / (len(r) - 1)) * math.sqrt(252)


def pairs(D: dict) -> list[dict]:
    """Every (stock, monthly expiration) worth pulling, with its strike band."""
    uni = load("universe.json")
    base = set(uni["baseline"])
    out = []
    for fri in third_fridays():
        lo_d, hi_d = fri - dt.timedelta(days=ENTRY_MAX_DTE + 2), fri - dt.timedelta(days=ENTRY_MIN_DTE)
        mths = {(lo_d + dt.timedelta(days=k)).strftime("%Y-%m") for k in range(0, (hi_d - lo_d).days + 1, 7)}
        mths.add(hi_d.strftime("%Y-%m"))
        syms = set(base)
        for m in mths:
            syms.update(uni["months"].get(m, []))
        for s in sorted(syms):
            o = D.get(s)
            if not o:
                continue
            idx = [i for i, d in enumerate(o["d"]) if lo_d.isoformat() <= d <= hi_d.isoformat()]
            if len(idx) < 5:
                continue
            closes = [o["c"][i] for i in idx]
            # a split inside the cycle changes the contract terms: skip the cycle
            last = max(i for i, d in enumerate(o["d"]) if d <= fri.isoformat())
            ratios = {round(o["c"][i] / o["ca"][i], 3) for i in range(idx[0], last + 1) if o["ca"][i]}
            if len(ratios) > 1:
                continue
            hv = max((max(_hv(o["ca"][:i + 1], 20) or 0, _hv(o["ca"][:i + 1], 60) or 0) for i in idx[::5]),
                     default=0)
            vol30 = max((sum(o["v"][max(0, i - 29):i + 1]) / min(30, i + 1) for i in idx[::5]), default=0)
            if hv < HV_MIN or max(closes) < 5 or vol30 < VOL_MIN:
                continue
            out.append({"sym": s, "fri": fri.isoformat(), "lo": round(BAND_LOW * min(closes), 2),
                        "hi": round(max(closes) * 1.001, 2), "from": lo_d.isoformat()})
    return out


def _listing(sym: str, fri: dt.date, lo: float, hi: float) -> tuple[str | None, list[dict]]:
    """The monthly put contracts in the strike band: (expiration date, contracts)."""
    for root in (sym, OLD_ROOT.get(sym)):
        if not root:
            continue
        rows = []
        for status in ("inactive", "active"):
            if status == "active" and fri < dt.date.today() - dt.timedelta(days=3):
                break                       # an old expiry has no active contracts left
            try:
                pages = list(paged(TRADING_HOST, "/v2/options/contracts",
                              {"underlying_symbols": root, "status": status, "type": "put",
                               "expiration_date_gte": (fri - dt.timedelta(days=4)).isoformat(),
                               "expiration_date_lte": (fri + dt.timedelta(days=1)).isoformat(),
                               "strike_price_gte": lo, "strike_price_lte": hi, "limit": 10000},
                              "option_contracts"))
            except RuntimeError as e:
                if "invalid underlying" in str(e):      # a root the vendor no longer knows
                    break
                raise
            for page in pages:
                rows.extend(page or [])
        if rows:
            by: dict = {}
            for r in {r["symbol"]: r for r in rows}.values():
                by.setdefault(r["expiration_date"], []).append(r)
            # the monthly is the Friday (Thursday when Friday is a holiday): the
            # date in the window carrying the most strikes
            exp = max(by, key=lambda e: (len(by[e]), e))
            # standard contracts only: a symbol with a leading digit is a
            # contract adjusted for a corporate action (odd deliverable)
            return exp, [r for r in by[exp] if r.get("style", "american") == "american"
                         and str(r.get("size", "100")) == "100" and r["symbol"][:1].isalpha()]
    return None, []


def _bars_end(exp: str) -> str:
    """The day after expiry, or 20 minutes ago when that is not yet in the past:
    the free plan refuses option data from the last 15 minutes."""
    end = dt.datetime.combine(dt.date.fromisoformat(exp) + dt.timedelta(days=1), dt.time(0), dt.timezone.utc)
    safe = dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=20)
    return min(end, safe).strftime("%Y-%m-%dT%H:%M:%SZ")


def pull(p: dict, cal: dict) -> str:
    name = f"chains/{p['sym']}_{p['fri'].replace('-', '')}.json"
    if (CACHE / name).exists():
        return "cached"
    fri = dt.date.fromisoformat(p["fri"])
    exp, contracts = _listing(p["sym"], fri, p["lo"], p["hi"])
    if not contracts:
        save(name, {"sym": p["sym"], "exp": None, "k": [], "b": []})
        return "none"
    occ = {c["symbol"]: float(c["strike_price"]) for c in contracts}
    bars: dict = {}
    syms = sorted(occ)
    for i in range(0, len(syms), 100):
        for page in paged(DATA_HOST, "/v1beta1/options/bars",
                          {"symbols": ",".join(syms[i:i + 100]), "timeframe": "1Day",
                           "start": p["from"], "end": _bars_end(exp), "limit": 10000}, "bars"):
            for s, rows in (page or {}).items():
                bars.setdefault(s, []).extend(rows)
    keep = [s for s in syms if bars.get(s)]
    save(name, {"sym": p["sym"], "exp": exp, "k": [occ[s] for s in keep],
                # per contract: [day index, close, vwap, volume]
                "b": [[[cal[r["t"][:10]], r["c"], r.get("vw") or r["c"], r["v"]]
                       for r in bars[s] if r["t"][:10] in cal] for s in keep]})
    return "ok"


def chains() -> None:
    D = daily()
    cal = {d: i for i, d in enumerate(D["SPY"]["d"])}
    todo = pairs(D)
    save("pairs.json", todo)
    print(f"{len(todo)} (stock, expiration) pairs", flush=True)
    t0, done, counts = time.time(), 0, {}
    with ThreadPoolExecutor(max_workers=5) as pool:
        futs = {pool.submit(pull, p, cal): p for p in todo}
        for f in as_completed(futs):
            p = futs[f]
            try:
                r = f.result()
            except Exception as e:  # noqa: BLE001
                r = "error"
                print(f"  {p['sym']} {p['fri']}: {type(e).__name__}: {str(e)[:150]}", flush=True)
            counts[r] = counts.get(r, 0) + 1
            done += 1
            if done % 200 == 0 or done == len(todo):
                print(f"  {done}/{len(todo)} {counts} {time.time() - t0:.0f}s", flush=True)


if __name__ == "__main__":
    {"daily": daily, "chains": chains, "vwap": daily_vwap}[sys.argv[1]]()
