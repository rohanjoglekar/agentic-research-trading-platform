"""Step 3: what the trader's rules need besides prices.

- VIX closes (FRED VIXCLS) for the regime gates.
- Earnings dates from SEC EDGAR: every 8-K that reports results (item 2.02).
  Known in advance in real life (companies announce the date), so using them to
  skip names about to report is not hindsight.
- A sector for the 30% concentration cap: the dashboard's cached sector when it
  has one, else a coarse map from the SEC's SIC code.
"""
from __future__ import annotations

import csv
import io
import json
import sys
import time

import requests

from common import SOAI, load, save

UA = {"User-Agent": "ForgeResearch/1.0 personal-research-tool", "Accept-Encoding": "gzip, deflate"}


def vix() -> dict:
    got = load("vix.json")
    if got:
        return got
    r = requests.get("https://fred.stlouisfed.org/graph/fredgraph.csv",
                     params={"id": "VIXCLS", "cosd": "2023-10-01"}, timeout=60)
    r.raise_for_status()
    out = {}
    for row in csv.reader(io.StringIO(r.text)):
        try:
            out[row[0]] = float(row[1])
        except (ValueError, IndexError):
            continue
    save("vix.json", out)
    return out


def _sec(url: str):
    for attempt in range(5):
        time.sleep(0.13)                       # SEC fair-access: stay under 10 requests a second
        try:
            r = requests.get(url, headers=UA, timeout=30)
        except requests.RequestException:
            time.sleep(2 * (attempt + 1))
            continue
        if r.status_code == 200:
            return r.json()
        if r.status_code == 404:
            return None
        time.sleep(3 * (attempt + 1))
    return None


def _sector_from_sic(sic: str | None) -> str:
    try:
        s = int(sic)
    except (TypeError, ValueError):
        return "Unknown"
    if s in (3674, 3672, 3577, 3571, 3572, 3576, 3559, 3663, 3669, 3661, 3670, 3678, 3679, 3690) \
            or 7370 <= s <= 7379 or s == 3825 or s == 3827:
        return "Technology"
    if 2830 <= s <= 2836 or s in (3841, 3842, 3845, 3826, 3844, 8000, 8011, 8050, 8060, 8062, 8071,
                                  8090, 8093, 6324, 5122, 5912, 8731, 8734):
        return "Healthcare"
    if 6000 <= s <= 6411 or 6500 <= s <= 6799:
        return "Financial Services" if not (6500 <= s <= 6553 or s == 6798) else "Real Estate"
    if 1300 <= s <= 1389 or 2900 <= s <= 2999 or s in (4922, 4923, 4610, 5171, 5172):
        return "Energy"
    if 4900 <= s <= 4991:
        return "Utilities"
    if 4800 <= s <= 4899 or s in (2711, 2721, 2731, 2741, 7810, 7812, 7819, 7822, 7829, 7841):
        return "Communication Services"
    if 1000 <= s <= 1299 or 1400 <= s <= 1499 or 2600 <= s <= 2699 or 2800 <= s <= 2829 \
            or 2845 <= s <= 2899 or 3300 <= s <= 3399:      # 2840-2844 (soap, cosmetics) are staples
        return "Basic Materials"
    if 2000 <= s <= 2199 or s in (5411, 5412, 5141, 5140, 5331, 5399, 2840, 2844):
        return "Consumer Defensive"
    if 5000 <= s <= 5999 or 7000 <= s <= 7299 or 7800 <= s <= 7999 or 2300 <= s <= 2399 \
            or 3100 <= s <= 3199 or s in (3711, 3714, 3716, 3751, 3944, 3949, 2500, 2510, 2511, 2520):
        return "Consumer Cyclical"
    return "Industrials"


def edgar() -> dict:
    got = load("edgar.json", {})
    uni = load("universe.json")
    syms = sorted(set(uni["baseline"]) | {s for v in uni["months"].values() for s in v})
    # www.sec.gov can refuse a client that data.sec.gov accepts, so a cached
    # copy of the ticker map (the valuation job keeps one) is tried first.
    tick = load("company_tickers.json") or _sec("https://www.sec.gov/files/company_tickers.json") or {}
    tick = tick.get("data", tick) if isinstance(tick, dict) else {}
    cik = {str(v["ticker"]).upper(): int(v["cik_str"]) for v in tick.values() if isinstance(v, dict)}
    if not cik:
        raise SystemExit("no ticker-to-CIK map: copy company_tickers.json into the cache first")
    fund = {}
    try:
        fund = json.loads((SOAI.parent / "forge-dashboard" / "data" / "robinhood" / "fundamentals.json").read_text())
    except (OSError, ValueError):
        pass
    for n, s in enumerate(syms):
        if s in got:
            continue
        rec = {"earnings": [], "sector": (fund.get(s) or {}).get("sector"), "sic": None}
        c = cik.get(s)
        if c:
            sub = _sec(f"https://data.sec.gov/submissions/CIK{c:010d}.json")
            if sub:
                rec["sic"] = sub.get("sic")
                blocks = [sub.get("filings", {}).get("recent") or {}]
                oldest = min(blocks[0].get("filingDate") or ["9999"])
                for f in sub.get("filings", {}).get("files") or []:
                    if oldest <= "2023-12-31":
                        break
                    more = _sec("https://data.sec.gov/submissions/" + f["name"])
                    if more:
                        blocks.append(more)
                        oldest = min(oldest, min(more.get("filingDate") or ["9999"]))
                for b in blocks:
                    for form, date, items in zip(b.get("form") or [], b.get("filingDate") or [],
                                                 b.get("items") or []):
                        if form in ("8-K", "8-K/A") and "2.02" in (items or "") and date >= "2023-12-01":
                            rec["earnings"].append(date)
                rec["earnings"] = sorted(set(rec["earnings"]))
        if not rec["sector"]:
            rec["sector"] = _sector_from_sic(rec["sic"])
        got[s] = rec
        if n % 50 == 0:
            save("edgar.json", got)
            print(f"  edgar {n}/{len(syms)}", file=sys.stderr, flush=True)
    save("edgar.json", got)
    return got


def distributions() -> dict:
    """{symbol: [dates]} of one-off distributions worth over 3% of the share
    price: spin-offs and special dividends. The listed option is adjusted on
    that date (its deliverable changes) while the raw share price simply drops,
    so a put spanning it would look like a loss that never happened. Found as a
    jump in the ratio of the split-adjusted close to the fully adjusted close."""
    got = load("distributions.json")
    if got is not None:
        return got
    from common import DATA_HOST, STOCK_END, paged
    D = load("daily.json")
    syms = sorted(D)
    adj: dict = {s: {} for s in syms}
    for i in range(0, len(syms), 100):
        for page in paged(DATA_HOST, "/v2/stocks/bars",
                          {"symbols": ",".join(syms[i:i + 100]), "timeframe": "1Day", "start": "2023-10-01",
                           "end": STOCK_END, "limit": 10000, "adjustment": "all", "feed": "sip"}, "bars"):
            for s, bars in (page or {}).items():
                for bar in bars:
                    adj[s][bar["t"][:10]] = bar["c"]
    out: dict = {}
    for s, o in D.items():
        prev = None
        for d, ca in zip(o["d"], o["ca"]):
            a_close = adj[s].get(d)
            if not a_close:
                continue
            r = ca / a_close
            if prev and abs(r / prev - 1) > 0.03:
                out.setdefault(s, []).append(d)
            prev = r
    save("distributions.json", out)
    return out


if __name__ == "__main__":
    print("stocks with a spin-off or special distribution:", len(distributions()))
    v = vix()
    print("vix days", len(v), "max", max(v.values()), "last", sorted(v.items())[-1])
    e = edgar()
    have = sum(1 for r in e.values() if r["earnings"])
    print("names", len(e), "with earnings dates", have, "sample NVDA", e.get("NVDA", {}).get("earnings", [])[-6:],
          e.get("NVDA", {}).get("sector"))
    import collections
    print(collections.Counter(r["sector"] for r in e.values()).most_common())
