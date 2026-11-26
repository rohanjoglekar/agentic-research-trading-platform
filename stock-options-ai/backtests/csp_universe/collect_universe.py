"""Step 1: which stocks. Point-in-time, survivorship-free.

Every month the candidate universe is the N US-listed common stocks with the
highest dollar volume in the PREVIOUS calendar month, drawn from every asset
Alpaca knows, delisted ones included. So a name enters when it was actually
heavily traded and a name that later collapsed or was acquired stays in for
the months it was there. The current scan universe (DEFAULT_UNIVERSE) is
always added so the baseline can be replayed.
"""
from __future__ import annotations

import re
import sys

from common import DATA_HOST, END, START, STOCK_END, TRADING_HOST, get, load, paged, save
from config.settings import DEFAULT_UNIVERSE, ETF_BLOCKLIST

TOP_N = 300
# Renamed since the list was written; bars live under the new symbol.
RENAMED = {"SQ": "XYZ", "GPS": "GAP", "PARA": "PSKY"}
_FUND = re.compile(
    r"\b(ETF|ETN|ETP|Fund|Trust,? Series|ProShares|iShares|SPDR|Invesco QQQ|Direxion|Vanguard|"
    r"GraniteShares|YieldMax|T-Rex|Leverage Shares|Defiance|Roundhill|WisdomTree|VanEck|"
    r"Global X|ARK |Grayscale|Bitcoin Trust|Ethereum Trust|Ether Trust|Index|2X|3X|Daily|"
    r"Tradr|Simplify|Schwab U\.?S\.?|Select Sector|Warrant|Rights?|Units?|Preferred|"
    r"Depositary Shares|Notes due|Acquisition Corp)\b", re.I)


def assets() -> list[dict]:
    got = load("assets.json")
    if got:
        return got
    out = []
    for status in ("active", "inactive"):
        for a in get(TRADING_HOST, "/v2/assets", {"status": status, "asset_class": "us_equity"}):
            sym = a.get("symbol") or ""
            if a.get("exchange") not in ("NASDAQ", "NYSE", "AMEX"):
                continue
            if not re.fullmatch(r"[A-Z]{1,5}", sym) or sym in ETF_BLOCKLIST:
                continue
            if _FUND.search(a.get("name") or ""):
                continue
            out.append({"symbol": sym, "name": a.get("name"), "exchange": a.get("exchange"),
                        "status": status})
    seen, uniq = set(), []
    for a in out:                      # an active listing wins over a dead namesake
        if a["symbol"] not in seen:
            seen.add(a["symbol"])
            uniq.append(a)
    save("assets.json", uniq)
    return uniq


def monthly_dollar_volume(symbols: list[str]) -> dict:
    """{symbol: {YYYY-MM: dollar volume}} from raw monthly bars."""
    got = load("monthly_dv.json")
    if got:
        return got
    out: dict = {}
    for i in range(0, len(symbols), 150):
        chunk = symbols[i:i + 150]
        for page in paged(DATA_HOST, "/v2/stocks/bars",
                          {"symbols": ",".join(chunk), "timeframe": "1Month", "start": "2023-12-01",
                           "end": STOCK_END, "limit": 10000, "adjustment": "raw", "feed": "sip"}, "bars"):
            for sym, bars in (page or {}).items():
                d = out.setdefault(sym, {})
                for b in bars:
                    d[b["t"][:7]] = float(b["v"]) * float(b.get("vw") or b["c"])
        print(f"  monthly bars {min(i + 150, len(symbols))}/{len(symbols)}", file=sys.stderr, flush=True)
    save("monthly_dv.json", out)
    return out


def months(start: str, end: str) -> list[str]:
    y, m = int(start[:4]), int(start[5:7])
    out = []
    while f"{y:04d}-{m:02d}" <= end[:7]:
        out.append(f"{y:04d}-{m:02d}")
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return out


def build() -> dict:
    al = assets()
    names = {a["symbol"]: a["name"] for a in al}
    dv = monthly_dollar_volume([a["symbol"] for a in al])
    base = [RENAMED.get(s, s) for s in DEFAULT_UNIVERSE]
    uni: dict = {}
    for mth in months(START, END):
        y, m = int(mth[:4]), int(mth[5:7])
        prev = f"{y - 1:04d}-12" if m == 1 else f"{y:04d}-{m - 1:02d}"
        ranked = sorted(((d.get(prev, 0.0), s) for s, d in dv.items() if d.get(prev)), reverse=True)
        uni[mth] = [s for _, s in ranked[:TOP_N]]
    out = {"top_n": TOP_N, "baseline": base, "renamed": RENAMED, "months": uni, "names": names}
    save("universe.json", out)
    return out


if __name__ == "__main__":
    u = build()
    allsyms = set(u["baseline"])
    for v in u["months"].values():
        allsyms.update(v)
    print("months", len(u["months"]), "symbols ever in universe", len(allsyms))
    last = list(u["months"])[-1]
    print(last, "top 60:", u["months"][last][:60])
    first = list(u["months"])[1]
    print(first, "top 40:", u["months"][first][:40])
    left = [s for s in allsyms if s not in u["months"][last] and s not in u["baseline"]]
    print("in at some point, out of the latest month:", len(left), sorted(left)[:80])
