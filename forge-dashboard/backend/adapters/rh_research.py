"""Robinhood research screen — quantitative scan over a broad liquid-stock universe.

The universe is the S&P 500 ∪ Nasdaq-100 plus a curated list (about 600 names,
loaded from data/universe/stocks.json built by backend/build_universe.py; an
inline list of about 200 is the fallback so the screen renders even if that
file is missing). Each name is graded 0-100 from seven weighted factors
(rh_factors), on the SAME cached 1y daily closes and fundamentals the rest of
the dashboard uses (rh_analytics). Scoring is transparent: every row carries
each factor's component breakdown. Rebuilt lazily on a 12h TTL.

Clicking a row in the UI opens the symbol slide-over (rh_analytics), so the
per-stock "why" needs no separate endpoint.

Missing history for a symbol -> the symbol is skipped (never a fabricated
score). The first build batch-fetches any uncached names (slower at ~500 names);
after that everything serves from disk.
"""
from __future__ import annotations

import json
import statistics
from collections import defaultdict

import appconfig as config
from adapters import (brokerage, read_json, read_jsonl_tail, rh_ai_reads, rh_analytics, rh_dcf,
                      rh_factors)

RESEARCH_TTL_SEC = 12 * 3600
# Below this fraction of names with a computable DCF, the payload carries a
# data-health warning (the "fundamentals wiped → momentum-only scores" guard).
DCF_COVERAGE_FLOOR = 0.30

# (symbol, name, sector) — sectors follow yfinance naming so they line up with
# the risk page's sector math. This inline list is the FALLBACK; the live universe
# is loaded from data/universe/stocks.json (S&P 500 ∪ Nasdaq-100) by _load_universe()
# below, so the screen always renders even if that file is missing.
_FALLBACK_UNIVERSE: list[tuple[str, str, str]] = [
    ("AAPL", "Apple", "Technology"), ("MSFT", "Microsoft", "Technology"),
    ("GOOGL", "Alphabet", "Communication Services"), ("AMZN", "Amazon", "Consumer Cyclical"),
    ("META", "Meta Platforms", "Communication Services"), ("NVDA", "NVIDIA", "Technology"),
    ("TSLA", "Tesla", "Consumer Cyclical"), ("AVGO", "Broadcom", "Technology"),
    ("AMD", "Advanced Micro Devices", "Technology"), ("TSM", "Taiwan Semiconductor", "Technology"),
    ("ASML", "ASML Holding", "Technology"), ("QCOM", "Qualcomm", "Technology"),
    ("MU", "Micron Technology", "Technology"), ("AMAT", "Applied Materials", "Technology"),
    ("LRCX", "Lam Research", "Technology"), ("KLAC", "KLA Corp", "Technology"),
    ("MRVL", "Marvell Technology", "Technology"), ("ANET", "Arista Networks", "Technology"),
    ("ARM", "Arm Holdings", "Technology"), ("DELL", "Dell Technologies", "Technology"),
    ("CRM", "Salesforce", "Technology"), ("ORCL", "Oracle", "Technology"),
    ("ADBE", "Adobe", "Technology"), ("NOW", "ServiceNow", "Technology"),
    ("INTU", "Intuit", "Technology"), ("SNOW", "Snowflake", "Technology"),
    ("PLTR", "Palantir", "Technology"), ("PANW", "Palo Alto Networks", "Technology"),
    ("CRWD", "CrowdStrike", "Technology"), ("ZS", "Zscaler", "Technology"),
    ("DDOG", "Datadog", "Technology"), ("NET", "Cloudflare", "Technology"),
    ("MDB", "MongoDB", "Technology"), ("SHOP", "Shopify", "Technology"),
    ("CDNS", "Cadence Design Systems", "Technology"), ("SNPS", "Synopsys", "Technology"),
    ("NFLX", "Netflix", "Communication Services"), ("DIS", "Walt Disney", "Communication Services"),
    ("UBER", "Uber Technologies", "Technology"), ("ABNB", "Airbnb", "Consumer Cyclical"),
    ("SPOT", "Spotify", "Communication Services"), ("BKNG", "Booking Holdings", "Consumer Cyclical"),
    ("TMUS", "T-Mobile US", "Communication Services"),
    ("V", "Visa", "Financial Services"), ("MA", "Mastercard", "Financial Services"),
    ("AXP", "American Express", "Financial Services"), ("PYPL", "PayPal", "Financial Services"),
    ("COIN", "Coinbase", "Financial Services"), ("HOOD", "Robinhood Markets", "Financial Services"),
    ("SOFI", "SoFi Technologies", "Financial Services"),
    ("JPM", "JPMorgan Chase", "Financial Services"), ("BAC", "Bank of America", "Financial Services"),
    ("GS", "Goldman Sachs", "Financial Services"), ("MS", "Morgan Stanley", "Financial Services"),
    ("BLK", "BlackRock", "Financial Services"), ("SCHW", "Charles Schwab", "Financial Services"),
    ("UNH", "UnitedHealth Group", "Healthcare"), ("LLY", "Eli Lilly", "Healthcare"),
    ("JNJ", "Johnson & Johnson", "Healthcare"), ("ABBV", "AbbVie", "Healthcare"),
    ("MRK", "Merck", "Healthcare"), ("PFE", "Pfizer", "Healthcare"),
    ("TMO", "Thermo Fisher Scientific", "Healthcare"), ("ISRG", "Intuitive Surgical", "Healthcare"),
    ("AMGN", "Amgen", "Healthcare"), ("VRTX", "Vertex Pharmaceuticals", "Healthcare"),
    ("WMT", "Walmart", "Consumer Defensive"), ("COST", "Costco", "Consumer Defensive"),
    ("PG", "Procter & Gamble", "Consumer Defensive"), ("KO", "Coca-Cola", "Consumer Defensive"),
    ("PEP", "PepsiCo", "Consumer Defensive"), ("HD", "Home Depot", "Consumer Cyclical"),
    ("MCD", "McDonald's", "Consumer Cyclical"), ("NKE", "Nike", "Consumer Cyclical"),
    ("SBUX", "Starbucks", "Consumer Cyclical"), ("LULU", "Lululemon", "Consumer Cyclical"),
    ("CMG", "Chipotle", "Consumer Cyclical"),
    ("XOM", "Exxon Mobil", "Energy"), ("CVX", "Chevron", "Energy"),
    ("COP", "ConocoPhillips", "Energy"), ("CAT", "Caterpillar", "Industrials"),
    ("DE", "Deere & Co", "Industrials"), ("GE", "GE Aerospace", "Industrials"),
    ("HON", "Honeywell", "Industrials"), ("LMT", "Lockheed Martin", "Industrials"),
    ("RTX", "RTX Corp", "Industrials"), ("UNP", "Union Pacific", "Industrials"),
    ("LIN", "Linde", "Basic Materials"), ("FCX", "Freeport-McMoRan", "Basic Materials"),
    # ── expansion (data fills in via batched/scheduled refresh) ──────────────
    ("INTC", "Intel", "Technology"), ("IBM", "IBM", "Technology"),
    ("CSCO", "Cisco", "Technology"), ("TXN", "Texas Instruments", "Technology"),
    ("ADI", "Analog Devices", "Technology"), ("NXPI", "NXP Semiconductors", "Technology"),
    ("MCHP", "Microchip Technology", "Technology"), ("ON", "ON Semiconductor", "Technology"),
    ("SMCI", "Super Micro Computer", "Technology"), ("WDC", "Western Digital", "Technology"),
    ("STX", "Seagate", "Technology"), ("HPQ", "HP Inc", "Technology"),
    ("HPE", "Hewlett Packard Enterprise", "Technology"), ("NTAP", "NetApp", "Technology"),
    ("FTNT", "Fortinet", "Technology"), ("TEAM", "Atlassian", "Technology"),
    ("WDAY", "Workday", "Technology"), ("ADSK", "Autodesk", "Technology"),
    ("ROP", "Roper Technologies", "Technology"), ("FICO", "Fair Isaac", "Technology"),
    ("HUBS", "HubSpot", "Technology"), ("DOCU", "DocuSign", "Technology"),
    ("OKTA", "Okta", "Technology"), ("TWLO", "Twilio", "Technology"),
    ("U", "Unity Software", "Technology"), ("PATH", "UiPath", "Technology"),
    ("APH", "Amphenol", "Technology"), ("GLW", "Corning", "Technology"),
    ("T", "AT&T", "Communication Services"), ("VZ", "Verizon", "Communication Services"),
    ("CMCSA", "Comcast", "Communication Services"), ("CHTR", "Charter Communications", "Communication Services"),
    ("WBD", "Warner Bros Discovery", "Communication Services"), ("EA", "Electronic Arts", "Communication Services"),
    ("TTWO", "Take-Two Interactive", "Communication Services"), ("RBLX", "Roblox", "Communication Services"),
    ("PINS", "Pinterest", "Communication Services"), ("SNAP", "Snap", "Communication Services"),
    ("ROKU", "Roku", "Communication Services"),
    ("F", "Ford", "Consumer Cyclical"), ("GM", "General Motors", "Consumer Cyclical"),
    ("RIVN", "Rivian", "Consumer Cyclical"), ("MAR", "Marriott", "Consumer Cyclical"),
    ("RCL", "Royal Caribbean", "Consumer Cyclical"), ("DKNG", "DraftKings", "Consumer Cyclical"),
    ("ETSY", "Etsy", "Consumer Cyclical"), ("EBAY", "eBay", "Consumer Cyclical"),
    ("CHWY", "Chewy", "Consumer Cyclical"), ("YUM", "Yum Brands", "Consumer Cyclical"),
    ("ORLY", "O'Reilly Automotive", "Consumer Cyclical"), ("AZO", "AutoZone", "Consumer Cyclical"),
    ("ROST", "Ross Stores", "Consumer Cyclical"), ("TJX", "TJX Companies", "Consumer Cyclical"),
    ("MDLZ", "Mondelez", "Consumer Defensive"), ("MO", "Altria", "Consumer Defensive"),
    ("PM", "Philip Morris", "Consumer Defensive"), ("CL", "Colgate-Palmolive", "Consumer Defensive"),
    ("STZ", "Constellation Brands", "Consumer Defensive"), ("TGT", "Target", "Consumer Defensive"),
    ("DG", "Dollar General", "Consumer Defensive"), ("KR", "Kroger", "Consumer Defensive"),
    ("WFC", "Wells Fargo", "Financial Services"), ("C", "Citigroup", "Financial Services"),
    ("USB", "U.S. Bancorp", "Financial Services"), ("PNC", "PNC Financial", "Financial Services"),
    ("COF", "Capital One", "Financial Services"), ("ICE", "Intercontinental Exchange", "Financial Services"),
    ("CME", "CME Group", "Financial Services"), ("SPGI", "S&P Global", "Financial Services"),
    ("PGR", "Progressive", "Financial Services"), ("AFRM", "Affirm", "Financial Services"),
    ("NU", "Nu Holdings", "Financial Services"), ("MSTR", "MicroStrategy", "Financial Services"),
    ("ABT", "Abbott", "Healthcare"), ("DHR", "Danaher", "Healthcare"),
    ("BMY", "Bristol-Myers Squibb", "Healthcare"), ("GILD", "Gilead Sciences", "Healthcare"),
    ("CVS", "CVS Health", "Healthcare"), ("MDT", "Medtronic", "Healthcare"),
    ("SYK", "Stryker", "Healthcare"), ("BSX", "Boston Scientific", "Healthcare"),
    ("ZTS", "Zoetis", "Healthcare"), ("REGN", "Regeneron", "Healthcare"),
    ("MRNA", "Moderna", "Healthcare"), ("DXCM", "DexCom", "Healthcare"),
    ("BA", "Boeing", "Industrials"), ("GD", "General Dynamics", "Industrials"),
    ("NOC", "Northrop Grumman", "Industrials"), ("ETN", "Eaton", "Industrials"),
    ("ITW", "Illinois Tool Works", "Industrials"), ("CMI", "Cummins", "Industrials"),
    ("FDX", "FedEx", "Industrials"), ("UPS", "United Parcel Service", "Industrials"),
    ("CSX", "CSX", "Industrials"), ("NSC", "Norfolk Southern", "Industrials"),
    ("MMM", "3M", "Industrials"), ("PCAR", "PACCAR", "Industrials"),
    ("SLB", "Schlumberger", "Energy"), ("EOG", "EOG Resources", "Energy"),
    ("MPC", "Marathon Petroleum", "Energy"), ("PSX", "Phillips 66", "Energy"),
    ("VLO", "Valero Energy", "Energy"), ("OXY", "Occidental Petroleum", "Energy"),
    ("WMB", "Williams Companies", "Energy"), ("KMI", "Kinder Morgan", "Energy"),
    ("DVN", "Devon Energy", "Energy"), ("APD", "Air Products", "Basic Materials"),
    ("SHW", "Sherwin-Williams", "Basic Materials"), ("NEM", "Newmont", "Basic Materials"),
    ("NUE", "Nucor", "Basic Materials"), ("PLD", "Prologis", "Real Estate"),
    ("AMT", "American Tower", "Real Estate"), ("EQIX", "Equinix", "Real Estate"),
    ("O", "Realty Income", "Real Estate"), ("NEE", "NextEra Energy", "Utilities"),
    ("DUK", "Duke Energy", "Utilities"), ("SO", "Southern Company", "Utilities"),
]


# Stale/renamed-ticker remaps → their live symbol, for names whose old listing
# returns degraded data. Empty for now: FISV→FI was reverted (yfinance, this
# system's source, has data for FISV but NOT FI, so remapping made Fiserv vanish).
# The single conviction Grade already demotes a thin-data name like FISV on its own.
_TICKER_REMAP: dict[str, str] = {}

# Curated additions beyond S&P 500 ∪ Nasdaq-100 index membership — high-interest
# thematic names (quantum, space, nuclear, fintech, growth) the owner wants
# screened. Sector is best-effort; the archetype router refines it from the
# fetched fundamentals. Most are pre-profit → low confidence → they rank low
# honestly rather than being hidden.
_EXTRA_UNIVERSE: list[tuple[str, str, str]] = [
    # ── Quantum computing ──
    ("IONQ", "IonQ", "Technology"), ("RGTI", "Rigetti Computing", "Technology"),
    ("QBTS", "D-Wave Quantum", "Technology"), ("QUBT", "Quantum Computing Inc", "Technology"),
    ("ARQQ", "Arqit Quantum", "Technology"), ("LAES", "SEALSQ", "Technology"),
    ("QMCO", "Quantum Corp", "Technology"),
    # ── Photonics / optical / advanced semis ──
    ("LITE", "Lumentum", "Technology"), ("COHR", "Coherent", "Technology"),
    ("AAOI", "Applied Optoelectronics", "Technology"), ("POET", "POET Technologies", "Technology"),
    ("LASR", "nLight", "Technology"), ("VIAV", "Viavi Solutions", "Technology"),
    ("MTSI", "MACOM Technology", "Technology"), ("CRDO", "Credo Technology", "Technology"),
    ("ALAB", "Astera Labs", "Technology"), ("INDI", "indie Semiconductor", "Technology"),
    ("AMBA", "Ambarella", "Technology"), ("LSCC", "Lattice Semiconductor", "Technology"),
    ("SITM", "SiTime", "Technology"), ("NVTS", "Navitas Semiconductor", "Technology"),
    # ── AI software / data ──
    ("PLTR", "Palantir", "Technology"), ("AI", "C3.ai", "Technology"),
    ("SOUN", "SoundHound AI", "Technology"), ("BBAI", "BigBear.ai", "Technology"),
    ("PATH", "UiPath", "Technology"), ("GTLB", "GitLab", "Technology"),
    ("S", "SentinelOne", "Technology"), ("TEM", "Tempus AI", "Healthcare"),
    ("RXRX", "Recursion Pharmaceuticals", "Healthcare"), ("SDGR", "Schrodinger", "Healthcare"),
    # ── Space / satellite / defense-tech ──
    # SPCX listed on 2026-06-12 and is in neither index list yet. It carries
    # RECENT_IPO (<1y of history), which caps its confidence and keeps the DCF
    # honest rather than hiding the name.
    ("SPCX", "SpaceX", "Industrials"),
    ("RKLB", "Rocket Lab", "Industrials"), ("ASTS", "AST SpaceMobile", "Communication Services"),
    ("LUNR", "Intuitive Machines", "Industrials"), ("RDW", "Redwire", "Industrials"),
    ("PL", "Planet Labs", "Industrials"), ("BKSY", "BlackSky", "Industrials"),
    ("SPIR", "Spire Global", "Industrials"), ("KTOS", "Kratos Defense", "Industrials"),
    ("AVAV", "AeroVironment", "Industrials"),
    # ── eVTOL / advanced mobility ──
    ("ACHR", "Archer Aviation", "Industrials"), ("JOBY", "Joby Aviation", "Industrials"),
    ("EVTL", "Vertical Aerospace", "Industrials"), ("EH", "EHang", "Industrials"),
    # ── Nuclear / next-gen & uranium ──
    ("OKLO", "Oklo", "Utilities"), ("SMR", "NuScale Power", "Industrials"),
    ("NNE", "Nano Nuclear Energy", "Industrials"), ("LEU", "Centrus Energy", "Energy"),
    ("CCJ", "Cameco", "Energy"), ("UEC", "Uranium Energy", "Energy"),
    ("UUUU", "Energy Fuels", "Energy"), ("BWXT", "BWX Technologies", "Industrials"),
    # ── EV / battery / energy storage ──
    ("QS", "QuantumScape", "Consumer Cyclical"), ("ENVX", "Enovix", "Technology"),
    ("AMPX", "Amprius Technologies", "Industrials"), ("SLDP", "Solid Power", "Consumer Cyclical"),
    ("CHPT", "ChargePoint", "Technology"), ("FLNC", "Fluence Energy", "Industrials"),
    ("STEM", "Stem Inc", "Technology"), ("NVX", "NOVONIX", "Industrials"),
    # ── Genomics / gene editing / diagnostics ──
    ("CRSP", "CRISPR Therapeutics", "Healthcare"), ("NTLA", "Intellia Therapeutics", "Healthcare"),
    ("BEAM", "Beam Therapeutics", "Healthcare"), ("EDIT", "Editas Medicine", "Healthcare"),
    ("VERV", "Verve Therapeutics", "Healthcare"), ("PACB", "Pacific Biosciences", "Healthcare"),
    ("GH", "Guardant Health", "Healthcare"), ("EXAS", "Exact Sciences", "Healthcare"),
    ("NTRA", "Natera", "Healthcare"),
    # ── Robotics / automation / sensing ──
    ("SERV", "Serve Robotics", "Technology"), ("OUST", "Ouster", "Technology"),
    ("NNDM", "Nano Dimension", "Technology"), ("RR", "Richtech Robotics", "Technology"),
    ("SYM", "Symbotic", "Industrials"),
    # ── Fintech / crypto-adjacent / miners ──
    ("HOOD", "Robinhood Markets", "Financial Services"), ("COIN", "Coinbase", "Financial Services"),
    ("SOFI", "SoFi Technologies", "Financial Services"), ("MSTR", "MicroStrategy", "Technology"),
    ("RIOT", "Riot Platforms", "Financial Services"), ("MARA", "MARA Holdings", "Financial Services"),
    ("CLSK", "CleanSpark", "Financial Services"), ("CIFR", "Cipher Mining", "Financial Services"),
    ("WULF", "TeraWulf", "Financial Services"), ("IREN", "IREN Ltd", "Financial Services"),
    ("APLD", "Applied Digital", "Technology"), ("CORZ", "Core Scientific", "Technology"),
    ("AFRM", "Affirm", "Financial Services"), ("UPST", "Upstart", "Financial Services"),
    # ── High-interest growth / consumer / platforms ──
    ("RDDT", "Reddit", "Communication Services"), ("HIMS", "Hims & Hers Health", "Healthcare"),
    ("CELH", "Celsius Holdings", "Consumer Staples"), ("RBLX", "Roblox", "Communication Services"),
    ("DKNG", "DraftKings", "Consumer Cyclical"), ("U", "Unity Software", "Technology"),
    ("RKLB", "Rocket Lab", "Industrials"), ("DUOL", "Duolingo", "Technology"),
    ("TTD", "The Trade Desk", "Technology"), ("APP", "AppLovin", "Technology"),
    ("ASAN", "Asana", "Technology"), ("PGY", "Pagaya Technologies", "Financial Services"),
]


def _load_universe() -> list[tuple[str, str, str]]:
    """Load the S&P 500 ∪ Nasdaq-100 universe from data/universe/stocks.json
    (built by backend/build_universe.py), remap stale tickers, and append the
    curated thematic extras. Falls back to the inline list when the file is
    missing/unreadable so the screen never goes dark."""
    doc = read_json(config.UNIVERSE_STOCKS_PATH, default=None)
    rows = doc.get("stocks") if isinstance(doc, dict) else None
    base = _FALLBACK_UNIVERSE if not rows else [
        (str(r.get("symbol", "")).strip().upper(), r.get("name") or "", r.get("sector") or "Unknown")
        for r in rows]
    out: list[tuple[str, str, str]] = []
    seen: set[str] = set()
    for sym, name, sector in list(base) + _EXTRA_UNIVERSE:
        sym = _TICKER_REMAP.get(sym, sym)
        if sym and sym not in seen:
            seen.add(sym)
            out.append((sym, name or sym, sector))
    return out or _FALLBACK_UNIVERSE


# Live universe (~516 names: S&P 500 ∪ Nasdaq-100), or the inline fallback.
UNIVERSE: list[tuple[str, str, str]] = _load_universe()


def _clamp01(x: float) -> float:
    return max(0.0, min(1.0, x))


# Parabolic-tape thresholds: a 3m move or a 1y Sharpe beyond these is almost
# never a durable signal on daily data — it's a blow-off or a data artifact
# (e.g. Sharpe 9 on a one-way squeeze). The momentum score gets a transparent
# discount (mirrors the growth factor's cyclical haircut) and the flag feeds the
# composite confidence, so a euphoric tape can't crown the research table.
_PARABOLIC_3M_PCT = 75.0
_PARABOLIC_SHARPE = 4.0
_PARABOLIC_DISCOUNT = 0.75


def _score(stats: dict) -> tuple[int, list[dict], bool, float] | None:
    """(score 0-100, components, parabolic?, coverage 0-1) from symbol_stats output.
    None when stats are empty."""
    if not stats:
        return None
    comps: list[dict] = []

    r3 = stats.get("ret_3m_pct")
    pts = round(_clamp01(((r3 or 0) + 15) / 40) * 20) if r3 is not None else 0
    comps.append({"name": "3-month momentum", "points": pts, "max_points": 20,
                  "detail": f"3m return {r3:+.1f}%" if r3 is not None else "no 3m history"})

    r1 = stats.get("ret_1m_pct")
    pts = round(_clamp01(((r1 or 0) + 10) / 25) * 10) if r1 is not None else 0
    comps.append({"name": "1-month momentum", "points": pts, "max_points": 10,
                  "detail": f"1m return {r1:+.1f}%" if r1 is not None else "no 1m history"})

    trend = stats.get("trend") or ""
    pts = (20 if "above 50d & 200d" in trend else 12 if "above 200d" in trend
           else 8 if "above 50d" in trend else 0)
    comps.append({"name": "Trend", "points": pts, "max_points": 20, "detail": trend or "unknown"})

    sh = stats.get("sharpe_1y")
    # Credit ramps to full at Sharpe 2; anything beyond ~4 on 1y daily data is a
    # blow-off/artifact regime, handled by the parabolic discount below.
    pts = round(_clamp01((sh or 0) / 2.0) * 20) if sh is not None else 0
    comps.append({"name": "Risk-adjusted return", "points": pts, "max_points": 20,
                  "detail": f"1y Sharpe {sh:.2f}" if sh is not None else "no Sharpe"})

    rsi = stats.get("rsi_14")
    if rsi is None:
        pts, detail = 0, "no RSI"
    elif 45 <= rsi <= 65:
        pts, detail = 15, f"RSI {rsi:.0f} — constructive, not overheated"
    elif 30 <= rsi < 45 or 65 < rsi <= 70:
        pts, detail = 9, f"RSI {rsi:.0f} — workable but watch it"
    elif rsi < 30:
        pts, detail = 5, f"RSI {rsi:.0f} — oversold, could be a falling knife"
    else:
        pts, detail = 3, f"RSI {rsi:.0f} — overbought"
    comps.append({"name": "RSI placement", "points": pts, "max_points": 15, "detail": detail})

    dd = stats.get("max_dd_1y_pct")
    pts = round(_clamp01((60 - abs(dd or 60)) / 50) * 15) if dd is not None else 0
    comps.append({"name": "Drawdown resilience", "points": pts, "max_points": 15,
                  "detail": f"worst 1y drawdown {dd:.0f}%" if dd is not None else "no drawdown history"})

    # Honest coverage: components whose input was missing scored 0 above (points
    # kept conservative) but must not claim their max as "evidence seen" — the
    # composite uses this coverage to down-weight a thin momentum read instead of
    # pretending a 2-signal tape is as trustworthy as a 6-signal one.
    present_max = sum(c["max_points"] for c, has in zip(comps, (
        r3 is not None, r1 is not None, bool(trend), sh is not None,
        rsi is not None, dd is not None)) if has)
    coverage = round(present_max / (sum(c["max_points"] for c in comps) or 1), 2)

    raw = int(min(100, sum(c["points"] for c in comps)))
    parabolic = (r3 is not None and r3 >= _PARABOLIC_3M_PCT) or \
                (sh is not None and sh >= _PARABOLIC_SHARPE)
    if parabolic:
        score = int(round(raw * _PARABOLIC_DISCOUNT))
        comps.append({
            "name": "Parabolic-move discount", "points": None, "max_points": 0,
            "detail": (f"3m {r3:+.0f}%" if r3 is not None else "")
                      + (f" · Sharpe {sh:.1f}" if sh is not None else "")
                      + f" — blow-off tape, momentum ×{_PARABOLIC_DISCOUNT:.2f}"
                      + f" ({raw}→{score})"})
        return score, comps, True, coverage
    return raw, comps, False, coverage


# Analyst consensus → human label (yfinance recommendationKey).
_CONSENSUS_LABEL = {
    "strong_buy": "Strong Buy", "buy": "Buy", "outperform": "Buy",
    "hold": "Hold", "neutral": "Hold",
    "underperform": "Underperform", "sell": "Sell", "strong_sell": "Strong Sell",
}


def _value_signals(last, closes: list, stats: dict, fund: dict) -> dict:
    """Undervalued / dip-buy overlay — 'pulled back from its high but still a buy'
    (the Oracle case). Dip depth + oversold are PRICE-only (from cached closes,
    so they scale to the whole universe). The analyst overlay (consensus, target
    upside, valuation) comes from cache-only fundamentals: present where the cache
    is warmed, gracefully absent otherwise — never blocks the screen."""
    hi = max(closes) if closes else None
    pct_below_high = round((last - hi) / hi * 100, 1) if (hi and last) else None

    rec = (fund.get("recommendation") or "").strip().lower()
    n_an = int(fund.get("num_analysts") or 0)
    target = fund.get("target_mean")
    upside = round((target - last) / last * 100, 1) if (target and last) else None
    pe = fund.get("pe")
    fwd_pe = fund.get("forward_pe")
    rsi = stats.get("rsi_14")
    ret_1y = stats.get("ret_1y_pct")

    bearish_consensus = rec in ("sell", "strong_sell", "underperform") and n_an >= 5
    # A dip-buy = pulled back meaningfully off its 1y high and NOT in freefall.
    # The analyst view is an OVERLAY, not a GATE: a name with cached fundamentals
    # is excluded only if the Street is outright bearish; a name with NO cached
    # fundamentals still qualifies on price alone — so "recently went down"
    # candidates surface across the WHOLE universe, not just analyst-covered ones.
    dipped = pct_below_high is not None and pct_below_high <= -12
    not_freefall = (rsi is None or rsi >= 25) and (ret_1y is None or ret_1y >= -35)
    is_dip_buy = bool(dipped and not_freefall and not bearish_consensus)

    # 0-100 value score; each term contributes only when its data exists.
    vs = 0.0
    if pct_below_high is not None:
        vs += _clamp01(min(abs(pct_below_high), 40) / 40) * 35   # deeper pullback → more upside room
    if upside is not None:
        vs += _clamp01(upside / 40) * 30
    vs += {"strong_buy": 20, "buy": 16, "outperform": 16, "hold": 6, "neutral": 6}.get(rec, 0)
    eff_pe = fwd_pe or pe
    if eff_pe is not None and eff_pe > 0:
        vs += _clamp01((35 - min(eff_pe, 60)) / 35) * 15         # cheaper → more

    return {
        "value_score": int(min(100, round(vs))),
        "is_dip_buy": is_dip_buy,
        "pct_below_52w_high": pct_below_high,
        "consensus": _CONSENSUS_LABEL.get(rec) or (rec or None),
        "num_analysts": n_an or None,
        "target_mean": target,
        "upside_to_target_pct": upside,
        "pe": pe, "forward_pe": fwd_pe,
    }


def _prefetch_missing(symbols: list[str]) -> None:
    """One batched yf.download for every universe symbol whose daily cache is
    missing/stale — makes the first build ~20s instead of 80 serial fetches."""
    need = []
    for sym in symbols:
        cached = read_json(rh_analytics._prices_path(sym), default=None)
        ok = (isinstance(cached, dict) and cached.get("dates") and cached.get("closes")
              and not rh_analytics._is_stale(cached.get("fetched_at"), rh_analytics.PRICE_TTL_SEC))
        if not ok:
            need.append(sym)
    if not need:
        return
    try:
        import yfinance as yf
        df = yf.download(need, period="1y", interval="1d",
                         auto_adjust=True, progress=False, group_by="column")
        for sym in need:
            s = rh_analytics._closes_from_download(df, sym)
            if s is None or len(s) < 2:
                continue
            rh_analytics.write_price_cache(
                sym, [d.strftime("%Y-%m-%d") for d in s.index], list(s.values))
    except Exception:
        pass


_SIGNAL_KEYS = ("ret_1m_pct", "ret_3m_pct", "ret_ytd_pct", "ret_1y_pct",
                "vol_ann_pct", "sharpe_1y", "rsi_14", "max_dd_1y_pct",
                "beta_1y", "trend")

# DCF fields kept on every bulk row (full assumptions live in the symbol detail).
_DCF_BULK_KEYS = ("fair_value", "current_price", "upside_pct", "wacc_pct",
                  "stage1_growth_pct", "terminal_growth_pct", "implied_growth_pct",
                  "implied_wacc_pct", "terminal_value_fraction_pct", "base_fcf",
                  "assumptions", "sensitivity", "caveats",
                  # blueprint gated-DCF fields (archetype routing + gates + coupling)
                  "method", "archetype", "structural_flags", "why_archetype",
                  "owner_fcf_source", "gates", "confidence")

# Diagnostics carried through for gate-suppressed / DCF-blocked names so the DCF
# tab can show WHY a fair value was withheld (archetype, gates) + the correct
# alternative method, instead of a bare "n/a".
_DCF_SUPPRESSED_KEYS = ("suppressed", "raw_fair_value", "raw_upside_pct",
                        "base_fcf", "wacc_pct", "stage1_growth_pct",
                        "method", "archetype", "blocked", "structural_flags",
                        "why_archetype", "alt_fair_value", "alt_upside_pct",
                        "alt_detail", "gates", "confidence")


def _compact_dcf(d: dict) -> dict:
    if not d or not d.get("available"):
        out = {"available": False, "reason": (d or {}).get("reason")}
        out.update({k: d[k] for k in _DCF_SUPPRESSED_KEYS if d and k in d})
        return out
    out = {"available": True}
    out.update({k: d.get(k) for k in _DCF_BULK_KEYS})
    return out


def _sector_medians(raw: list[dict]) -> dict:
    """Median forward-P/E and EV/EBITDA per sector (positive values, ≥3 names)
    so valuation can be judged against peers, not an absolute yardstick."""
    pe_by, ev_by = defaultdict(list), defaultdict(list)
    for r in raw:
        f = r["fund"] or {}
        pe = f.get("forward_pe") or f.get("pe")
        ev = f.get("ev_ebitda")
        if pe and pe > 0:
            pe_by[r["sector"]].append(pe)
        if ev and ev > 0:
            ev_by[r["sector"]].append(ev)
    out: dict = {}
    for sec in set(pe_by) | set(ev_by):
        out[sec] = {
            "pe": statistics.median(pe_by[sec]) if len(pe_by[sec]) >= 3 else None,
            "ev": statistics.median(ev_by[sec]) if len(ev_by[sec]) >= 3 else None,
        }
    return out


def build_research() -> dict:
    symbols = [u[0] for u in UNIVERSE]
    _prefetch_missing(symbols)

    snap = brokerage.snapshot()
    held = {p.get("symbol") for p in snap.get("positions") or []}
    # avg cost per held name → a P/L chip on held research rows (as-of the screen
    # build; the live figure is in Holdings / the click-through detail).
    pos_avg = {p.get("symbol"): float(p.get("avg_buy_price") or 0)
               for p in (snap.get("positions") or [])}
    spy = rh_analytics._series("SPY")

    # Held COMPANIES the universe does not carry are graded by exactly the
    # same screen, so the Holdings verdict has a research grade for them. They
    # are kept OUT of
    # `stocks` — the alpha trader, the deep DCF and every shortlist read only
    # that list, so grading a held name never makes it a buy candidate. Funds
    # (no revenue line) are left out: this screen grades operating businesses.
    universe_syms = {u[0] for u in UNIVERSE}
    extra: list[tuple[str, str, str]] = []
    for sym in sorted(s for s in held if s and s not in universe_syms):
        f = rh_analytics.get_fundamentals_cached(sym)
        if f and rh_analytics._f(f.get("total_revenue")):
            extra.append((sym, f.get("name") or sym, f.get("sector") or "Unknown"))
    extra_syms = {e[0] for e in extra}

    # ── pass 1: momentum + fundamentals + DCF per name ──────────────────────
    raw: list[dict] = []
    skipped: list[str] = []
    for sym, name, sector in list(UNIVERSE) + extra:
        stats = rh_analytics.symbol_stats(sym, spy)
        scored = _score(stats)
        if scored is None:                 # no usable price history -> skip entirely
            if sym not in extra_syms:
                skipped.append(sym)
            continue
        mom_score, mom_comps, parabolic, mom_cov = scored
        d = rh_analytics.get_prices(sym)
        closes = (d or {}).get("closes") or []
        last = float(closes[-1]) if closes else None
        fund = rh_analytics.get_fundamentals_cached(sym)   # cache-only → no 429 storm
        # Don't run the gated DCF on schema-stale fundamentals during a post-bump
        # heal window (e.g. the EBIT-fix FUND_SCHEMA_V 2→3): the old fields can be
        # corrupt in exactly the way the bump fixes. Suppress until the warmer
        # heals it — dcf_llm_review already gates on schema_v; the screen now too.
        if fund and fund.get("schema_v") not in (None, rh_analytics.FUND_SCHEMA_V):
            dcf = {"available": False, "reason": "fundamentals awaiting schema refresh"}
        else:
            dcf = (rh_dcf.dcf_value(fund, last, stats.get("beta_1y")) if fund
                   else {"available": False, "reason": "fundamentals not cached yet"})
        raw.append({"symbol": sym, "name": name, "sector": sector, "last": last,
                    "closes": closes, "stats": stats, "fund": fund, "dcf": dcf,
                    "mom_score": mom_score, "mom_comps": mom_comps,
                    "parabolic": parabolic, "mom_cov": mom_cov})

    # sector medians describe the universe; a held extra must not move them
    sec_med = _sector_medians([r for r in raw if r["symbol"] not in extra_syms])
    # this week's AI reads, ranked across the names this build screens
    ai_cal = rh_ai_reads.for_universe({r["symbol"] for r in raw if r["symbol"] not in extra_syms})

    # ── pass 2: factor scores + capped coverage-weighted composite ──────────
    rows: list[dict] = []
    for r in raw:
        fund, last, stats, dcf = r["fund"], r["last"], r["stats"], r["dcf"]
        sm = sec_med.get(r["sector"]) or {}
        _arch = (dcf or {}).get("archetype")
        factors = {
            "value": rh_factors.value(fund, last, dcf, sm.get("pe"), sm.get("ev")),
            "quality": rh_factors.quality(fund, _arch),
            "growth": rh_factors.growth(fund, _arch),
            "momentum": {"score": r["mom_score"], "coverage": r.get("mom_cov", 1.0),
                         "components": r["mom_comps"]},
            "analyst": rh_factors.analyst(fund, last),
            "ai": rh_ai_reads.factor(ai_cal["reads"].get(r["symbol"]), ai_cal["coverage_ok"],
                                     ai_cal["pool"], ai_cal["universe"]),
        }
        _revlvl = rh_analytics._f((fund or {}).get("total_revenue"))
        # "pre-revenue" = essentially NO product revenue (< $1M). A pre-profit name
        # that IS generating revenue (RGTI ~$10M, QBTS ~$12M, QUBT ~$4M) is just
        # "pre-profit" — only genuine $0-revenue concepts (OKLO, QS) are pre-revenue.
        stage = ("pre-revenue" if (_arch == "PRE_PROFIT" and (not _revlvl or _revlvl < 1e6))
                 else "pre-profit" if _arch == "PRE_PROFIT" else None)
        comp, parts = rh_factors.composite(factors)
        fund_cov = rh_factors.fundamental_coverage(factors)
        conf, conf_label, conf_why = rh_factors.confidence(
            factors, dcf, parabolic=r["parabolic"])
        factors["composite"] = {"score": comp, "weights": parts,
                                "fundamental_coverage": round(fund_cov, 2),
                                "confidence": conf, "confidence_label": conf_label,
                                "confidence_notes": conf_why}
        headline = comp if comp is not None else r["mom_score"]
        rows.append({
            "symbol": r["symbol"], "name": r["name"], "sector": r["sector"],
            "last": rh_analytics._f(last), "held": r["symbol"] in held,
            "pl_pct": (round((last / pos_avg[r["symbol"]] - 1) * 100, 1)
                       if (r["symbol"] in held and last and pos_avg.get(r["symbol"], 0) > 0)
                       else None),
            "score": headline, "tier": rh_factors.tier(headline, conf),
            "stage": stage,
            "confidence": conf, "confidence_label": conf_label,
            "confidence_notes": conf_why,
            "rank_score": rh_factors.rank_score(headline, conf),
            "factors": factors,
            "dcf": _compact_dcf(dcf),
            "value": _value_signals(last, r["closes"], stats, fund),
            "signals": {k: stats.get(k) for k in _SIGNAL_KEYS},
        })
    # ONE headline metric: the conviction-adjusted grade = composite × confidence
    # (rank_score), the single number the UI now shows. Folding confidence in is
    # what keeps a high-composite / low-trust name (e.g. FISV: grade 79 on tape-
    # only data, no quality/value) from crowning the list — it correctly lands
    # mid-pack. The raw composite (score) stays in the payload for the breakdown.
    # DISPLAY order only — the alpha trader re-sorts by its own conviction
    # weighting (alpha_portfolio.build_target), so this never drives what it buys.
    held_extra = [dict(r, outside_screen=True) for r in rows if r["symbol"] in extra_syms]
    rows = [r for r in rows if r["symbol"] not in extra_syms]
    rows.sort(key=lambda r: (-(r.get("rank_score") or 0), -(r.get("score") or 0)))
    for i, r in enumerate(rows):
        r["rank"] = i + 1

    # ── thematic shortlists for the UI filters ──────────────────────────────
    # Factor shortlists require medium+ confidence: "cheapest" must not surface
    # value traps whose cheapness is exactly the low-confidence signal.
    def _rank_by(scorer, n=25, min_conf=0.5):
        cand = [(scorer(r), r["symbol"]) for r in rows
                if (r.get("confidence") or 0) >= min_conf]
        cand = [(s, sym) for s, sym in cand if s is not None]
        cand.sort(key=lambda x: -x[0])
        return [sym for _, sym in cand[:n]]

    def _fac(r, name):
        return (r["factors"].get(name) or {}).get("score")

    dip_buys = [r["symbol"] for r in sorted(
        (r for r in rows if (r.get("value") or {}).get("is_dip_buy")),
        key=lambda r: -((r.get("value") or {}).get("value_score") or 0))][:25]
    shortlists = {
        "dip_buys": dip_buys,
        "cheapest": _rank_by(lambda r: _fac(r, "value")),
        "fastest_growth": _rank_by(lambda r: _fac(r, "growth")),
        "top_quality": _rank_by(lambda r: _fac(r, "quality")),
        "dcf_upside": _rank_by(lambda r: (r["dcf"].get("upside_pct")
                              if (r["dcf"].get("available") and r["dcf"].get("confidence")
                                  not in ("LOW/ARTIFACT", "N/A", None)) else None)),
        # Momentum + Analyst: price/Street lenses — a lower confidence floor so a
        # genuine momentum leader isn't filtered out for thin fundamentals, but
        # still not pure junk.
        "momentum": _rank_by(lambda r: _fac(r, "momentum"), min_conf=0.3),
        "analyst": _rank_by(lambda r: _fac(r, "analyst"), min_conf=0.3),
        # Emerging: the tagged pre-profit / pre-revenue names, by conviction Grade —
        # scored on the early-stage methodology, so their own view (no conf floor).
        "emerging": [r["symbol"] for r in sorted(
            (r for r in rows if r.get("stage")),
            key=lambda r: -(r.get("rank_score") or 0))][:30],
        # AI picks: the top fifth of this week's AI reads, by conviction grade.
        "ai_buys": [r["symbol"] for r in sorted(
            (r for r in rows
             if (((r.get("factors") or {}).get("ai") or {}).get("score") or -1) >= rh_ai_reads.TOP_PCT),
            key=lambda r: -(r.get("rank_score") or 0))][:25],
    }

    # Data-health signals: when the fundamentals cache is thin (e.g. after a
    # wipe), most composites lean on momentum/quality — say so IN the payload so
    # both the Research and DCF tabs can render an unmissable banner instead of
    # silently serving corrupted rankings.
    dcf_n = sum(1 for r in rows if (r.get("dcf") or {}).get("available"))
    fund_n = sum(1 for r in rows if (r.get("confidence") or 0) >= 0.5)
    warnings: list[str] = []
    if rows and dcf_n < DCF_COVERAGE_FLOOR * len(rows):
        warnings.append(
            f"fundamentals cache incomplete — DCF computed for only {dcf_n}/{len(rows)} "
            "names, so most scores lean on momentum/quality; treat rankings as "
            "provisional until the warmers refill the cache")

    payload = {
        "generated_at": rh_analytics._utcnow_iso(),
        "count": len(rows), "universe_size": len(UNIVERSE), "skipped": skipped,
        "dcf_coverage": {"available": dcf_n, "count": len(rows),
                         "floor_pct": int(DCF_COVERAGE_FLOOR * 100)},
        "usable_confidence_count": fund_n,
        "warnings": warnings,
        "stocks": rows,
        # graded like `stocks`, but held names outside the universe: read by
        # the Holdings verdict only, never by the traders or the shortlists
        "held_extra": held_extra,
        "ai_reads": {"coverage_ok": ai_cal["coverage_ok"], "read": ai_cal["pool"],
                     "universe": ai_cal["universe"],
                     "updated_at": (rh_ai_reads.load_store() or {}).get("updated_at")},
        "dip_buys": dip_buys,                  # back-compat (existing UI card)
        "shortlists": shortlists,
        "factor_weights": rh_factors.WEIGHTS,
        "method": ("Multi-factor analyst screen: Value (P/E vs sector, EV/EBITDA, "
                   "FCF yield, PEG, a light DCF confirmation), Quality (margins, "
                   "ROE, FCF conversion, leverage), Growth (revenue/earnings/"
                   "acceleration), Momentum (price/trend/Sharpe, parabolic-move "
                   "discounted), Analyst consensus, and a weekly Claude read of "
                   "every name ranked against the rest of the universe (so it "
                   "averages neutral) — coverage-weighted into a 0-100 composite "
                   "with the tape factors (momentum/analyst/news/AI) capped at "
                   "their nominal weight. Rows are ORDERED by conviction "
                   "(composite × confidence), the bold number shown: names with "
                   "no DCF, base-effect growth or a blow-off tape rank lower and "
                   "carry a confidence badge. Click a row for the breakdown + AI "
                   "deep dive."),
        "disclaimer": "Quantitative + DCF screen — decision support, not investment advice.",
    }
    # Unique tmp per writer: the refresh timer and a stale-GET rebuild can run
    # concurrently — a FIXED tmp name raced (torn reads, FileNotFoundError on the
    # loser's replace, duplicate history lines blanking the movers panel).
    config.RH_RESEARCH_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    import os as _os
    import tempfile as _tempfile
    fd, tmp_name = _tempfile.mkstemp(dir=str(config.RH_RESEARCH_CACHE_PATH.parent),
                                     suffix=".tmp")
    with _os.fdopen(fd, "w") as fh:
        fh.write(json.dumps(payload))
    _os.replace(tmp_name, config.RH_RESEARCH_CACHE_PATH)
    _append_history(payload)
    return payload


def _append_history(payload: dict) -> None:
    """One compact line per build (composite + tier per symbol) for score history /
    'what changed'. Best-effort — never break the build."""
    try:
        rec = {
            "generated_at": payload["generated_at"],
            "composite": {r["symbol"]: r["score"] for r in payload["stocks"] if r.get("score") is not None},
            "tier": {r["symbol"]: r["tier"] for r in payload["stocks"]},
        }
        p = config.RH_SCREEN_HISTORY_PATH
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a") as fh:
            fh.write(json.dumps(rec, separators=(",", ":")) + "\n")
    except Exception:
        pass


def get_research_history(sym: str | None = None, limit: int = 90) -> dict:
    """Per-symbol composite series (?sym=) or, with no symbol, the latest 'what
    changed' — biggest composite movers + names newly upgraded to Buy/Strong."""
    rows = read_jsonl_tail(config.RH_SCREEN_HISTORY_PATH, n=limit)
    if not rows:
        return {"available": False, "builds": 0}
    if sym:
        s = sym.upper()
        series = [{"t": (r.get("generated_at") or "")[:10],
                   "composite": (r.get("composite") or {}).get(s),
                   "tier": (r.get("tier") or {}).get(s)} for r in rows]
        return {"available": True, "symbol": s,
                "series": [x for x in series if x["composite"] is not None]}
    if len(rows) < 2:
        return {"available": True, "builds": len(rows), "movers": [], "new_buys": []}
    prev, cur = rows[-2], rows[-1]
    pc, cc = prev.get("composite") or {}, cur.get("composite") or {}
    pt, ct = prev.get("tier") or {}, cur.get("tier") or {}
    movers = sorted(
        ({"symbol": s, "delta": cc[s] - pc[s], "from": pc[s], "to": cc[s]}
         for s in cc if s in pc and cc[s] is not None and pc[s] is not None and cc[s] != pc[s]),
        key=lambda x: -abs(x["delta"]))[:10]
    new_buys = sorted(s for s in ct
                      if ct[s] in ("strong", "buy") and pt.get(s) not in ("strong", "buy"))
    return {"available": True, "builds": len(rows), "since": prev.get("generated_at"),
            "movers": movers, "new_buys": new_buys}


def get_research(refresh: bool = False) -> dict:
    cached = read_json(config.RH_RESEARCH_CACHE_PATH, default=None)
    if (not refresh and isinstance(cached, dict) and cached.get("stocks")
            and not rh_analytics._is_stale(cached.get("generated_at"), RESEARCH_TTL_SEC)):
        return cached
    # A rebuild that throws must serve the stale-but-good cache, not 500 the
    # endpoint on every request until the next successful timer run.
    try:
        fresh = build_research()
    except Exception as e:
        import traceback
        print(f"[research] build_research failed: {e}\n{traceback.format_exc()}", flush=True)
        fresh = {}
    if fresh.get("stocks"):
        return fresh
    return (cached if isinstance(cached, dict) and cached.get("stocks")
            else (fresh or {"stocks": [], "error": "research build failed and no cache"}))


def get_factor_performance() -> dict:
    """Latest backtested factor spreads (written by backend/factor_backtest.py):
    does each price factor's top quintile actually beat its bottom quintile?"""
    return read_json(config.RH_SNAPSHOT_PATH.parent / "factor_performance.json",
                     default={"factors": [], "available": False}) or {"factors": []}


def research_symbol(sym: str) -> dict:
    """Run the full screen on ANY ticker (the ad-hoc 'research my own companies'
    box) — not limited to the universe. A single-symbol LIVE fundamentals fetch
    is fine (one call, no 429 storm). Same row shape as the universe, or an error."""
    sym = (sym or "").strip().upper()
    if not sym or not all(c.isalnum() or c in ".-" for c in sym) or len(sym) > 8:
        return {"error": "invalid symbol"}
    spy = rh_analytics._series("SPY")
    stats = rh_analytics.symbol_stats(sym, spy)
    scored = _score(stats)
    if scored is None:
        return {"symbol": sym, "error": f"no price history for {sym} (unknown ticker or no data)"}
    score, comps, parabolic, mom_cov = scored
    d = rh_analytics.get_prices(sym)
    closes = (d or {}).get("closes") or []
    last = float(closes[-1]) if closes else None
    fund = rh_analytics.get_fundamentals(sym)        # live fetch OK for one symbol
    dcf = (rh_dcf.dcf_value(fund, last, stats.get("beta_1y")) if fund
           else {"available": False, "reason": "no fundamentals"})
    _arch = (dcf or {}).get("archetype")
    _ai = rh_ai_reads.for_screen()
    factors = {
        "value": rh_factors.value(fund, last, dcf),  # no sector context (single name)
        "quality": rh_factors.quality(fund, _arch),
        "growth": rh_factors.growth(fund, _arch),
        "momentum": {"score": score, "coverage": mom_cov, "components": comps},
        "analyst": rh_factors.analyst(fund, last),
        "ai": rh_ai_reads.factor(_ai.get("reads", {}).get(sym), _ai.get("coverage_ok", False),
                                 _ai.get("pool", 0), _ai.get("universe", 0)),
    }
    fac_news = rh_factors.news([n.get("title") for n in rh_analytics.get_news(sym, 8)])
    if fac_news.get("score") is not None:
        factors["news"] = fac_news
    comp, parts = rh_factors.composite(factors)
    fund_cov = rh_factors.fundamental_coverage(factors)
    conf, conf_label, conf_why = rh_factors.confidence(factors, dcf, parabolic=parabolic)
    factors["composite"] = {"score": comp, "weights": parts,
                            "fundamental_coverage": round(fund_cov, 2),
                            "confidence": conf, "confidence_label": conf_label,
                            "confidence_notes": conf_why}
    headline = comp if comp is not None else score
    snap = brokerage.snapshot()
    held = {p.get("symbol") for p in snap.get("positions") or []}
    return {
        "symbol": sym, "name": fund.get("name") or sym, "sector": fund.get("sector"),
        "last": rh_analytics._f(last), "held": sym in held,
        "score": headline, "tier": rh_factors.tier(headline, conf),
        "stage": ("pre-revenue" if (_arch == "PRE_PROFIT"
                  and (not rh_analytics._f(fund.get("total_revenue")) or rh_analytics._f(fund.get("total_revenue")) < 1e6))
                  else "pre-profit" if _arch == "PRE_PROFIT" else None),
        "confidence": conf, "confidence_label": conf_label, "confidence_notes": conf_why,
        "rank_score": rh_factors.rank_score(headline, conf),
        "factors": factors,
        "dcf": dcf,                                  # full DCF for the single name
        "news_sentiment": fac_news,
        "value": _value_signals(last, closes, stats, fund),
        "signals": {k: stats.get(k) for k in _SIGNAL_KEYS},
        "generated_at": rh_analytics._utcnow_iso(),
        "disclaimer": "Multi-factor + DCF screen — not investment advice.",
    }
