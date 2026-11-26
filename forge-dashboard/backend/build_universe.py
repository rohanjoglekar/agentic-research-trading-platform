#!/usr/bin/env python3
"""Generate the research universe data files:

  • data/universe/stocks.json — S&P 500 ∪ Nasdaq-100, [{symbol, name, sector, in_sp500, in_ndx}]
  • data/universe/etfs.json   — curated liquid ETFs, [{symbol, name, category, sector}]

The stock list is fetched fresh from the public S&P 500 constituents CSV and merged
with the Nasdaq-100 (most of which is already in the S&P 500; the union is what we
scan). GICS sectors are mapped to the dashboard's existing Yahoo-style labels so the
factor engine's sector-relative medians and the UI stay consistent.

Run when constituents change (membership drifts a few names/quarter):
    cd forge-dashboard/backend && python build_universe.py
Network egress with relaxed TLS is used ONLY to fetch this public list; the result
is committed so the runtime never needs the network.
"""
from __future__ import annotations

import csv
import io
import json
import warnings
from pathlib import Path

import requests

warnings.filterwarnings("ignore")

OUT = Path(__file__).resolve().parents[1] / "data" / "universe"
SP500_CSV = "https://raw.githubusercontent.com/datasets/s-and-p-500-companies/main/data/constituents.csv"

# GICS (Wikipedia/datahub) → the dashboard's existing sector labels.
GICS_MAP = {
    "Information Technology": "Technology",
    "Health Care": "Healthcare",
    "Financials": "Financials",
    "Consumer Discretionary": "Consumer Cyclical",
    "Consumer Staples": "Consumer Defensive",
    "Communication Services": "Communication Services",
    "Industrials": "Industrials",
    "Energy": "Energy",
    "Materials": "Materials",
    "Real Estate": "Real Estate",
    "Utilities": "Utilities",
}

# Nasdaq-100 (reconstituted ~annually). Overlap with the S&P 500 is deduped.
NASDAQ_100 = [
    "AAPL", "ABNB", "ADBE", "ADI", "ADP", "ADSK", "AEP", "AMAT", "AMD", "AMGN",
    "AMZN", "ANSS", "APP", "ARM", "ASML", "AVGO", "AXON", "AZN", "BIIB", "BKNG",
    "BKR", "CCEP", "CDNS", "CDW", "CEG", "CHTR", "CMCSA", "COST", "CPRT", "CRWD",
    "CSCO", "CSGP", "CSX", "CTAS", "CTSH", "DASH", "DDOG", "DXCM", "EA", "EXC",
    "FANG", "FAST", "FTNT", "GEHC", "GFS", "GILD", "GOOG", "GOOGL", "HON", "IDXX",
    "INTC", "INTU", "ISRG", "KDP", "KHC", "KLAC", "LIN", "LRCX", "LULU", "MAR",
    "MCHP", "MDB", "MDLZ", "MELI", "META", "MNST", "MRVL", "MSFT", "MSTR", "MU",
    "NFLX", "NVDA", "NXPI", "ODFL", "ON", "ORLY", "PANW", "PAYX", "PCAR", "PDD",
    "PEP", "PLTR", "PYPL", "QCOM", "REGN", "ROP", "ROST", "SBUX", "SNPS", "TEAM",
    "TMUS", "TSLA", "TTD", "TTWO", "TXN", "VRSK", "VRTX", "WBD", "WDAY", "XEL", "ZS",
]
# Sectors for Nasdaq-100 names not in the S&P 500 (foreign ADRs / recent listers).
NDX_EXTRA = {
    "ASML": ("ASML Holding", "Technology"),
    "AZN": ("AstraZeneca", "Healthcare"),
    "ARM": ("Arm Holdings", "Technology"),
    "PDD": ("PDD Holdings", "Consumer Cyclical"),
    "MELI": ("MercadoLibre", "Consumer Cyclical"),
    "GFS": ("GlobalFoundries", "Technology"),
    "CCEP": ("Coca-Cola Europacific", "Consumer Defensive"),
    "MSTR": ("MicroStrategy", "Technology"),
    "ANSS": ("Ansys", "Technology"),
    "MDB": ("MongoDB", "Technology"),
    "MRVL": ("Marvell Technology", "Technology"),
    "TEAM": ("Atlassian", "Technology"),
    "ZS": ("Zscaler", "Technology"),
}

# Curated, liquid ETFs — a separate universe for the equity trader's core/ETF picks
# and an ETF research view. `sector` is a coarse tag the factor engine can group on.
ETFS = [
    # Broad market
    ("SPY", "SPDR S&P 500", "broad", "US Large Cap"),
    ("VOO", "Vanguard S&P 500", "broad", "US Large Cap"),
    ("IVV", "iShares Core S&P 500", "broad", "US Large Cap"),
    ("VTI", "Vanguard Total Market", "broad", "US Total Market"),
    ("QQQ", "Invesco Nasdaq-100", "broad", "US Large Growth"),
    ("QQQM", "Invesco Nasdaq-100 (M)", "broad", "US Large Growth"),
    ("DIA", "SPDR Dow Jones", "broad", "US Large Cap"),
    ("IWM", "iShares Russell 2000", "broad", "US Small Cap"),
    ("IWB", "iShares Russell 1000", "broad", "US Large Cap"),
    ("RSP", "Invesco S&P 500 Equal Weight", "broad", "US Large Cap"),
    ("MDY", "SPDR S&P MidCap 400", "broad", "US Mid Cap"),
    # Sectors (SPDR Select)
    ("XLK", "Technology Select", "sector", "Technology"),
    ("XLF", "Financials Select", "sector", "Financials"),
    ("XLE", "Energy Select", "sector", "Energy"),
    ("XLV", "Health Care Select", "sector", "Healthcare"),
    ("XLI", "Industrials Select", "sector", "Industrials"),
    ("XLY", "Cons. Discretionary Select", "sector", "Consumer Cyclical"),
    ("XLP", "Cons. Staples Select", "sector", "Consumer Defensive"),
    ("XLU", "Utilities Select", "sector", "Utilities"),
    ("XLB", "Materials Select", "sector", "Materials"),
    ("XLRE", "Real Estate Select", "sector", "Real Estate"),
    ("XLC", "Communication Svcs Select", "sector", "Communication Services"),
    # Style / factor
    ("VUG", "Vanguard Growth", "factor", "US Large Growth"),
    ("VTV", "Vanguard Value", "factor", "US Large Value"),
    ("MTUM", "iShares Momentum", "factor", "US Momentum"),
    ("QUAL", "iShares Quality", "factor", "US Quality"),
    ("USMV", "iShares Min Vol", "factor", "US Low Vol"),
    ("VYM", "Vanguard High Dividend", "factor", "US Dividend"),
    ("SCHD", "Schwab US Dividend", "factor", "US Dividend"),
    # International
    ("VEA", "Vanguard Developed Mkts", "intl", "Dev ex-US"),
    ("VWO", "Vanguard Emerging Mkts", "intl", "Emerging"),
    ("EFA", "iShares MSCI EAFE", "intl", "Dev ex-US"),
    ("EEM", "iShares Emerging Mkts", "intl", "Emerging"),
    ("VXUS", "Vanguard Total Intl", "intl", "Intl"),
    # Bonds
    ("AGG", "iShares Core US Agg Bond", "bond", "US Aggregate"),
    ("BND", "Vanguard Total Bond", "bond", "US Aggregate"),
    ("TLT", "iShares 20+ Yr Treasury", "bond", "Long Treasury"),
    ("IEF", "iShares 7-10 Yr Treasury", "bond", "Treasury"),
    ("LQD", "iShares IG Corporate", "bond", "IG Corp"),
    ("HYG", "iShares High Yield", "bond", "High Yield"),
    ("TIP", "iShares TIPS", "bond", "TIPS"),
    # Thematic / industry
    ("SMH", "VanEck Semiconductors", "thematic", "Semiconductors"),
    ("SOXX", "iShares Semiconductors", "thematic", "Semiconductors"),
    ("IBB", "iShares Biotech", "thematic", "Biotech"),
    ("XBI", "SPDR Biotech", "thematic", "Biotech"),
    ("ITA", "iShares Aerospace & Defense", "thematic", "Defense"),
    ("KRE", "SPDR Regional Banks", "thematic", "Regional Banks"),
    ("XOP", "SPDR Oil & Gas E&P", "thematic", "Energy"),
    ("TAN", "Invesco Solar", "thematic", "Clean Energy"),
    ("ICLN", "iShares Clean Energy", "thematic", "Clean Energy"),
    ("ARKK", "ARK Innovation", "thematic", "Innovation"),
    ("IGV", "iShares Software", "thematic", "Software"),
    # Commodity / metals
    ("GLD", "SPDR Gold", "commodity", "Gold"),
    ("SLV", "iShares Silver", "commodity", "Silver"),
    ("GDX", "VanEck Gold Miners", "commodity", "Gold Miners"),
]


def fetch_sp500() -> dict:
    r = requests.get(SP500_CSV, verify=False, timeout=30)
    r.raise_for_status()
    out = {}
    for row in csv.DictReader(io.StringIO(r.text)):
        sym = (row.get("Symbol") or "").strip().upper().replace(".", "-")
        if not sym:
            continue
        out[sym] = {
            "symbol": sym,
            "name": (row.get("Security") or sym).strip(),
            "sector": GICS_MAP.get((row.get("GICS Sector") or "").strip(), "Unknown"),
            "in_sp500": True,
            "in_ndx": False,
        }
    return out


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    stocks = fetch_sp500()
    print(f"S&P 500: {len(stocks)} names")

    added = 0
    for sym in NASDAQ_100:
        s = sym.upper()
        if s in stocks:
            stocks[s]["in_ndx"] = True
        else:
            name, sector = NDX_EXTRA.get(s, (s, "Unknown"))
            stocks[s] = {"symbol": s, "name": name, "sector": sector,
                         "in_sp500": False, "in_ndx": True}
            added += 1
    print(f"Nasdaq-100: +{added} names not already in S&P 500")

    stock_list = sorted(stocks.values(), key=lambda x: x["symbol"])
    (OUT / "stocks.json").write_text(json.dumps({
        "as_of": None, "count": len(stock_list),
        "sources": ["S&P 500", "Nasdaq-100"], "stocks": stock_list,
    }, indent=2))

    etf_list = [{"symbol": s, "name": n, "category": c, "sector": sec} for s, n, c, sec in ETFS]
    (OUT / "etfs.json").write_text(json.dumps({
        "count": len(etf_list), "etfs": etf_list,
    }, indent=2))

    sectors: dict = {}
    for s in stock_list:
        sectors[s["sector"]] = sectors.get(s["sector"], 0) + 1
    print(f"\nWrote {OUT/'stocks.json'} ({len(stock_list)} stocks) and "
          f"{OUT/'etfs.json'} ({len(etf_list)} ETFs)")
    print("Sectors:", dict(sorted(sectors.items(), key=lambda x: -x[1])))


if __name__ == "__main__":
    main()
