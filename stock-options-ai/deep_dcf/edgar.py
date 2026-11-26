"""SEC EDGAR: latest 10-K/10-Q filing date, and statement history from XBRL.

Two free endpoints (no key; SEC asks for a descriptive User-Agent and ≤10 req/s):
  data.sec.gov/submissions/CIK##########.json          filing index
  data.sec.gov/api/xbrl/companyfacts/CIK##########.json  every reported XBRL fact

The extraction functions are PURE (facts dict in, numbers out) so they are unit-
tested on synthetic facts; only `Edgar` touches the network.

Why XBRL and not the dashboard's fundamentals.json: that cache holds TTM figures
and 3-4 years of revenue only. An investment-banking DCF needs five fiscal years
of the income statement, cash flow and balance sheet, from the filings.
"""
from __future__ import annotations

import json
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from loguru import logger

ANNUAL_FORMS = ("10-K", "10-K/A", "10-KT")
QUARTER_FORMS = ("10-Q", "10-Q/A")
PERIODIC_FORMS = ("10-K", "10-Q")           # what "a new filing" means for the T1 trigger

# ── concept → candidate us-gaap tags, in priority order ──────────────────────
# The first tag is preferred when two tags cover the same fiscal year; a tag
# whose data stops years ago only back-fills periods the current tag lacks.
FLOW_TAGS: dict[str, tuple[str, ...]] = {
    "revenue": ("Revenues", "RevenueFromContractWithCustomerExcludingAssessedTax",
                "RevenueFromContractWithCustomerIncludingAssessedTax", "SalesRevenueNet",
                "SalesRevenueGoodsNet", "SalesRevenueServicesNet"),
    "cost_of_revenue": ("CostOfRevenue", "CostOfGoodsAndServicesSold", "CostOfGoodsSold",
                        "CostOfServices"),
    "gross_profit": ("GrossProfit",),
    "research_development": ("ResearchAndDevelopmentExpense",
                             "ResearchAndDevelopmentExpenseExcludingAcquiredInProcessCost"),
    "sales_marketing": ("SellingAndMarketingExpense",),
    "general_administrative": ("GeneralAndAdministrativeExpense",),
    "sga": ("SellingGeneralAndAdministrativeExpense",),
    "operating_expenses": ("OperatingExpenses",),
    "operating_income": ("OperatingIncomeLoss",),
    "interest_expense": ("InterestExpense", "InterestExpenseNonoperating", "InterestExpenseDebt"),
    "pretax_income": (
        "IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest",
        "IncomeLossFromContinuingOperationsBeforeIncomeTaxesMinorityInterestAndIncomeLossFromEquityMethodInvestments",
    ),
    "income_tax": ("IncomeTaxExpenseBenefit",),
    "net_income": ("NetIncomeLoss", "ProfitLoss"),
    "depreciation_amortization": ("DepreciationDepletionAndAmortization",
                                  "DepreciationAndAmortization",
                                  "DepreciationAmortizationAndAccretionNet"),
    "amortization_intangibles": ("AmortizationOfIntangibleAssets",),
    "stock_based_compensation": ("ShareBasedCompensation",
                                 "AllocatedShareBasedCompensationExpense"),
    "operating_cash_flow": ("NetCashProvidedByUsedInOperatingActivities",
                            "NetCashProvidedByUsedInOperatingActivitiesContinuingOperations"),
    "capex": ("PaymentsToAcquirePropertyPlantAndEquipment", "PaymentsToAcquireProductiveAssets",
              "PaymentsForCapitalImprovements"),
    "buybacks": ("PaymentsForRepurchaseOfCommonStock",),
    "dividends_paid": ("PaymentsOfDividends", "PaymentsOfDividendsCommonStock"),
}
SHARE_FLOW_TAGS: dict[str, tuple[str, ...]] = {
    "diluted_shares": ("WeightedAverageNumberOfDilutedSharesOutstanding",),
    "basic_shares": ("WeightedAverageNumberOfSharesOutstandingBasic",),
}
INSTANT_TAGS: dict[str, tuple[str, ...]] = {
    "cash": ("CashAndCashEquivalentsAtCarryingValue",
             "CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents"),
    "short_term_investments": ("ShortTermInvestments", "MarketableSecuritiesCurrent",
                               "AvailableForSaleSecuritiesDebtSecuritiesCurrent"),
    "receivables": ("AccountsReceivableNetCurrent",),
    "inventory": ("InventoryNet",),
    "payables": ("AccountsPayableCurrent",),
    "deferred_revenue_current": ("ContractWithCustomerLiabilityCurrent", "DeferredRevenueCurrent"),
    "current_assets": ("AssetsCurrent",),
    "current_liabilities": ("LiabilitiesCurrent",),
    "total_assets": ("Assets",),
    "total_liabilities": ("Liabilities",),
    "stockholders_equity": ("StockholdersEquity",),
    "goodwill": ("Goodwill",),
    "long_term_debt_noncurrent": ("LongTermDebtNoncurrent",),
    "long_term_debt_current": ("LongTermDebtCurrent",),
    "long_term_debt_total": ("LongTermDebt",),
}
# A fiscal year is 52/53 weeks or a calendar year; a YTD period is 1-3 quarters.
_FY_DAYS = (345, 380)
_YTD_DAYS = (80, 290)


def _days(a: str, b: str) -> int:
    return (date.fromisoformat(b) - date.fromisoformat(a)).days


def _units(facts: dict, tag: str, unit: str = "USD", taxonomy: str = "us-gaap") -> list:
    node = (((facts or {}).get("facts") or {}).get(taxonomy) or {}).get(tag) or {}
    return (node.get("units") or {}).get(unit) or []


def _num(v) -> Optional[float]:
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def _annual_points(items: list) -> dict[str, dict]:
    """fiscal-year end → {val, start, filed}. Latest-FILED value wins, so a year
    restated in a later 10-K is on the same basis as the years around it."""
    out: dict[str, dict] = {}
    for x in items:
        s, e, v = x.get("start"), x.get("end"), _num(x.get("val"))
        if x.get("form") not in ANNUAL_FORMS or not s or not e or v is None:
            continue
        try:
            if not _FY_DAYS[0] <= _days(s, e) <= _FY_DAYS[1]:
                continue
        except ValueError:
            continue
        filed = x.get("filed") or ""
        cur = out.get(e)
        if cur is None or filed >= cur["filed"]:
            out[e] = {"val": v, "start": s, "filed": filed}
    return out


def _instant_points(items: list, forms: tuple) -> dict[str, dict]:
    """balance-sheet date → {val, filed} (facts with no start date)."""
    out: dict[str, dict] = {}
    for x in items:
        e, v = x.get("end"), _num(x.get("val"))
        if x.get("form") not in forms or x.get("start") or not e or v is None:
            continue
        filed = x.get("filed") or ""
        cur = out.get(e)
        if cur is None or filed >= cur["filed"]:
            out[e] = {"val": v, "filed": filed}
    return out


def _ytd_points(items: list) -> dict[str, dict]:
    """quarter end → the LONGEST 10-Q duration ending there (the year-to-date
    figure; a 10-Q reports both the 3-month and the YTD period for one date)."""
    out: dict[str, dict] = {}
    for x in items:
        s, e, v = x.get("start"), x.get("end"), _num(x.get("val"))
        if x.get("form") not in QUARTER_FORMS or not s or not e or v is None:
            continue
        try:
            d = _days(s, e)
        except ValueError:
            continue
        if not _YTD_DAYS[0] <= d <= _YTD_DAYS[1]:
            continue
        filed = x.get("filed") or ""
        cur = out.get(e)
        if cur is None or d > cur["days"] or (d == cur["days"] and filed >= cur["filed"]):
            out[e] = {"val": v, "start": s, "days": d, "filed": filed}
    return out


# Concepts where two tags can both be "right" and the LARGER one is the
# statement total. CRM tags PP&E depreciation (a footnote, $1.2B) as
# DepreciationDepletionAndAmortization and the cash-flow line ($3.6B) as
# DepreciationAndAmortization; taking the first by priority understated D&A 3x.
PREFER_LARGER = frozenset({"depreciation_amortization"})
CONFLICT_PCT = 10.0


def _merge(per_tag: list[tuple[str, dict]], prefer_larger: bool = False) -> dict[str, dict]:
    """Combine several tags for one concept. The tag reporting the most recent
    period is primary (ties → priority order); the others only fill gaps. With
    `prefer_larger`, among the tags that report the newest period the one with
    the larger value there is primary."""
    live = [(t, p) for t, p in per_tag if p]
    if prefer_larger and live:
        newest = max(max(p) for _, p in live)
        live.sort(key=lambda tp: (newest in tp[1],
                                  tp[1][newest]["val"] if newest in tp[1] else float("-inf"),
                                  max(tp[1])), reverse=True)
    else:
        live.sort(key=lambda tp: max(tp[1]), reverse=True)     # stable: priority kept on ties
    merged: dict[str, dict] = {}
    for tag, pts in live:
        for end, v in pts.items():
            merged.setdefault(end, {**v, "tag": tag})
    return merged


def annual_concept(facts: dict, tags: tuple, unit: str = "USD",
                   prefer_larger: bool = False) -> dict[str, dict]:
    return _merge([(t, _annual_points(_units(facts, t, unit))) for t in tags], prefer_larger)


def tag_conflict(facts: dict, tags: tuple, end: str, unit: str = "USD") -> dict[str, float]:
    """{tag: value} for the fiscal year ending `end` when the candidate tags
    disagree by more than CONFLICT_PCT — the reader should know a choice was made."""
    vals = {}
    for t in tags:
        pts = _annual_points(_units(facts, t, unit))
        if end in pts:
            vals[t] = pts[end]["val"]
    if len(vals) < 2:
        return {}
    lo, hi = min(vals.values()), max(vals.values())
    if hi == lo or abs(hi - lo) <= CONFLICT_PCT / 100.0 * max(abs(hi), abs(lo)):
        return {}
    return vals


def instant_concept(facts: dict, tags: tuple, forms: tuple = ANNUAL_FORMS + QUARTER_FORMS,
                    unit: str = "USD") -> dict[str, dict]:
    return _merge([(t, _instant_points(_units(facts, t, unit), forms)) for t in tags])


def fiscal_year_ends(facts: dict, n: int = 5) -> list[str]:
    """The last `n` fiscal-year end dates (ascending), taken from revenue."""
    ends = sorted(annual_concept(facts, FLOW_TAGS["revenue"]))
    return ends[-n:]


def ltm_value(facts: dict, tags: tuple, fy_end: str, unit: str = "USD",
              prefer_larger: bool = False) -> Optional[dict]:
    """Last-twelve-months = latest FY + current YTD − same YTD a year earlier.

    All three legs come from the SAME tag — the one that supplied the fiscal
    year — because two tags for one concept can measure different things.
    Returned only when the legs exist and line up: the current YTD starts right
    after `fy_end`, and the prior-year YTD covers the same span. Otherwise None
    (the caller falls back to the fiscal year)."""
    fy = annual_concept(facts, tags, unit, prefer_larger).get(fy_end)
    if not fy:
        return None
    ytd = _ytd_points(_units(facts, fy["tag"], unit))
    later = sorted(e for e in ytd if e > fy_end)
    if not later:
        return None
    q_end = later[-1]
    cur = ytd[q_end]
    try:
        if not 0 <= _days(fy_end, cur["start"]) <= 7:        # YTD of the CURRENT fiscal year
            return None
        target = date.fromisoformat(q_end) - timedelta(days=364)
        prior = None
        for e, p in ytd.items():
            if abs((date.fromisoformat(e) - target).days) <= 10 and abs(p["days"] - cur["days"]) <= 10:
                if prior is None or p["filed"] >= prior[1]["filed"]:
                    prior = (e, p)
    except ValueError:
        return None
    if prior is None:
        return None
    return {"value": fy["val"] + cur["val"] - prior[1]["val"], "through": q_end,
            "fy": fy["val"], "ytd": cur["val"], "prior_ytd": prior[1]["val"],
            "ytd_days": cur["days"], "tag": fy["tag"]}


def _derive_operating_income(out: dict, ends: list) -> None:
    """Some filers (most large pharma: BMY, BIIB) never tag an operating-income
    line; their statements go from costs straight to income before tax. For a
    year with no OperatingIncomeLoss, EBIT is approximated as pre-tax income +
    interest expense. The proxy still carries non-operating items (investment
    income, one-off charges), so every derived year is listed in
    out["derived"] and inputs.py tells the model."""
    annual = out["annual"]
    oi = list(annual.get("operating_income") or [None] * len(ends))
    pre = annual.get("pretax_income") or [None] * len(ends)
    intr = annual.get("interest_expense") or [None] * len(ends)
    derived = []
    for i, e in enumerate(ends):
        if oi[i] is None and pre[i] is not None:
            oi[i] = pre[i] + (intr[i] or 0.0)
            derived.append(e)
    if derived:
        annual["operating_income"] = oi
        out["coverage"]["operating_income"] = sum(v is not None for v in oi)
        out.setdefault("derived", {})["operating_income"] = derived
        out["tags"]["operating_income"] = sorted(set(out["tags"].get("operating_income", []))
                                                 | {"derived: pretax_income + interest_expense"})


def extract_statements(facts: dict, n_years: int = 5) -> dict:
    """Everything inputs.json needs from XBRL. Missing values are None — never
    guessed. `coverage` says how many fiscal years each concept has."""
    ends = fiscal_year_ends(facts, n_years)
    out: dict = {"fiscal_year_ends": ends, "annual": {}, "balance_sheet": {}, "shares": {},
                 "ltm": {}, "latest_balance_sheet": {}, "tags": {}, "coverage": {},
                 "conflicts": {}, "basis": {}}
    if not ends:
        return out

    def series(points: dict) -> list:
        return [points[e]["val"] if e in points else None for e in ends]

    for name, tags in FLOW_TAGS.items():
        pts = annual_concept(facts, tags, prefer_larger=name in PREFER_LARGER)
        out["annual"][name] = series(pts)
        out["coverage"][name] = sum(v is not None for v in out["annual"][name])
        used = sorted({pts[e]["tag"] for e in ends if e in pts})
        if used:
            out["tags"][name] = used
        clash = tag_conflict(facts, tags, ends[-1])
        if clash:
            out["conflicts"][name] = {"values": clash, "used": pts[ends[-1]]["tag"]
                                      if ends[-1] in pts else None}
    _derive_operating_income(out, ends)
    # A 10-K restates only the three years it presents. Older years keep the
    # basis of the filing they last appeared in, so a later divestiture or
    # reclassification (APP's apps business) makes them non-comparable.
    rev = annual_concept(facts, FLOW_TAGS["revenue"])
    filed_latest = max((rev[e]["filed"] for e in ends if e in rev), default="")
    out["basis"]["latest_10k_filed"] = filed_latest
    out["basis"]["years_from_earlier_filings"] = [e for e in ends
                                                  if e in rev and rev[e]["filed"] < filed_latest]
    for name, tags in SHARE_FLOW_TAGS.items():
        pts = annual_concept(facts, tags, unit="shares")
        out["shares"][name] = series(pts)
    eps = annual_concept(facts, ("EarningsPerShareDiluted",), unit="USD/shares")
    out["shares"]["eps_diluted"] = series(eps)
    # A jump in the share count between two fiscal years is a split (a 10-for-1,
    # say): everything per-share before it is on a different footing.
    dil = out["shares"].get("diluted_shares") or []
    out["basis"]["share_count_breaks"] = [
        ends[i] for i in range(1, len(dil))
        if dil[i] and dil[i - 1] and not 0.55 <= dil[i] / dil[i - 1] <= 1.8]
    for name, tags in INSTANT_TAGS.items():
        pts = instant_concept(facts, tags, forms=ANNUAL_FORMS)
        out["balance_sheet"][name] = series(pts)

    last = ends[-1]
    for name in ("revenue", "gross_profit", "operating_income", "net_income",
                 "depreciation_amortization", "amortization_intangibles",
                 "stock_based_compensation", "operating_cash_flow", "capex",
                 "interest_expense", "income_tax", "pretax_income",
                 "research_development", "sales_marketing", "general_administrative", "sga",
                 "cost_of_revenue"):
        ltm = ltm_value(facts, FLOW_TAGS[name], last, prefer_larger=name in PREFER_LARGER)
        if ltm:
            out["ltm"][name] = ltm
    if "operating_income" not in out["ltm"] and "pretax_income" in out["ltm"]:
        pre, intr = out["ltm"]["pretax_income"], out["ltm"].get("interest_expense")
        if intr is None or intr.get("through") == pre.get("through"):
            out["ltm"]["operating_income"] = {
                **pre, "value": pre["value"] + (intr["value"] if intr else 0.0), "derived": True}

    # Most recent balance sheet of any periodic filing. Anchored on total assets
    # (every balance sheet has it) so one concept with a stray later-dated fact
    # cannot drag the snapshot to a date where everything else is missing.
    snap = {}
    for name, tags in INSTANT_TAGS.items():
        pts = instant_concept(facts, tags)
        if pts:
            snap[name] = pts
    anchor = snap.get("total_assets") or {}
    latest_end = max(anchor) if anchor else (max(max(p) for p in snap.values()) if snap else None)
    if latest_end:
        out["latest_balance_sheet"] = {"as_of": latest_end}
        for name, pts in snap.items():
            out["latest_balance_sheet"][name] = pts[latest_end]["val"] if latest_end in pts else None
    cover = _instant_points(_units(facts, "EntityCommonStockSharesOutstanding", "shares", "dei"),
                            ANNUAL_FORMS + QUARTER_FORMS)
    if cover:
        e = max(cover)
        out["shares"]["cover_page_shares_outstanding"] = {"as_of": e, "value": cover[e]["val"]}
    return out


def latest_periodic(submissions: dict) -> Optional[dict]:
    """Newest 10-K or 10-Q in a submissions document (None for non-filers, e.g.
    foreign issuers on 20-F/6-K)."""
    recent = ((submissions or {}).get("filings") or {}).get("recent") or {}
    forms, dates = recent.get("form") or [], recent.get("filingDate") or []
    accs, reports = recent.get("accessionNumber") or [], recent.get("reportDate") or []
    best = None
    for i, form in enumerate(forms):
        if form not in PERIODIC_FORMS or i >= len(dates) or not dates[i]:
            continue
        if best is None or dates[i] > best["filing_date"]:
            best = {"form": form, "filing_date": dates[i],
                    "accession": accs[i] if i < len(accs) else None,
                    "period_end": reports[i] if i < len(reports) else None}
    return best


class EdgarError(RuntimeError):
    pass


class Edgar:
    """Thin, rate-limited, disk-cached client. All failures raise EdgarError so
    callers can skip one symbol without spending anything."""

    BASE = "https://data.sec.gov"
    TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
    MIN_GAP_S = 0.2                         # ≤5 req/s, half the SEC limit

    def __init__(self, cache_dir: Path, user_agent: str, session=None):
        self.cache_dir = Path(cache_dir)
        self.user_agent = user_agent
        self._session = session
        self._last = 0.0
        self._ciks: Optional[dict] = None

    # ── http ────────────────────────────────────────────────────────────────
    def _get_json(self, url: str, timeout: int = 30):
        import requests

        sess = self._session or requests
        last_err = None
        for attempt in range(3):
            gap = self.MIN_GAP_S - (time.monotonic() - self._last)
            if gap > 0:
                time.sleep(gap)
            self._last = time.monotonic()
            try:
                r = sess.get(url, headers={"User-Agent": self.user_agent,
                                           "Accept-Encoding": "gzip, deflate"}, timeout=timeout)
                if r.status_code == 404:
                    raise EdgarError(f"404 {url}")
                if r.status_code in (403, 429) or r.status_code >= 500:
                    last_err = f"HTTP {r.status_code}"
                    time.sleep(2.0 * (attempt + 1))
                    continue
                r.raise_for_status()
                return r.json()
            except EdgarError:
                raise
            except Exception as e:           # network / JSON — retry then give up
                last_err = repr(e)
                time.sleep(1.5 * (attempt + 1))
        raise EdgarError(f"GET {url} failed: {last_err}")

    def _cached(self, name: str, max_age_h: float, fetch):
        path = self.cache_dir / name
        try:
            if path.exists() and (time.time() - path.stat().st_mtime) < max_age_h * 3600:
                return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            pass                              # unreadable cache → refetch
        data = fetch()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(path.suffix + ".tmp")
            tmp.write_text(json.dumps(data), encoding="utf-8")
            tmp.replace(path)
        except OSError as e:
            logger.warning(f"[deep-dcf] cache write failed for {name}: {e}")
        return data

    # ── lookups ─────────────────────────────────────────────────────────────
    def cik(self, symbol: str) -> Optional[int]:
        if self._ciks is None:
            raw = self._cached("company_tickers.json", 24 * 7,
                               lambda: self._get_json(self.TICKERS_URL))
            self._ciks = {str(v["ticker"]).upper(): int(v["cik_str"])
                          for v in (raw or {}).values() if isinstance(v, dict) and v.get("ticker")}
        sym = symbol.upper()
        return self._ciks.get(sym) or self._ciks.get(sym.replace(".", "-"))

    def submissions(self, cik: int) -> dict:
        return self._cached(f"submissions/CIK{cik:010d}.json", 20,
                            lambda: self._get_json(f"{self.BASE}/submissions/CIK{cik:010d}.json"))

    def latest_filing(self, symbol: str) -> Optional[dict]:
        """{form, filing_date, accession, period_end, cik} or None when the
        symbol is not an SEC 10-K/10-Q filer."""
        cik = self.cik(symbol)
        if cik is None:
            return None
        hit = latest_periodic(self.submissions(cik))
        if hit:
            hit["cik"] = cik
        return hit

    def company_facts(self, cik: int) -> dict:
        # ~4 MB per company and only needed for names about to be modelled:
        # fetched fresh, not cached.
        return self._get_json(f"{self.BASE}/api/xbrl/companyfacts/CIK{cik:010d}.json", timeout=60)


def risk_free_10y(fallback: float, session=None) -> tuple[float, str]:
    """Latest 10-year Treasury yield from FRED's public CSV (no key). Falls back
    to the configured constant rather than failing a run over a rate lookup."""
    import requests

    since = (datetime.now(timezone.utc) - timedelta(days=21)).date().isoformat()
    url = f"https://fred.stlouisfed.org/graph/fredgraph.csv?id=DGS10&cosd={since}"
    try:
        r = (session or requests).get(url, timeout=20,
                                      headers={"User-Agent": "ForgeResearch/1.0"})
        r.raise_for_status()
        best = None
        for line in r.text.strip().splitlines()[1:]:
            parts = line.split(",")
            if len(parts) != 2:
                continue
            try:
                val = float(parts[1])
            except ValueError:
                continue                       # holidays are blank / "."
            best = (parts[0], val)
        if best and 0.5 <= best[1] <= 15.0:
            return round(best[1] / 100.0, 5), f"FRED DGS10 {best[0]}"
    except Exception as e:
        logger.warning(f"[deep-dcf] FRED 10Y fetch failed ({e!r}) — using fallback")
    return fallback, "fallback constant (FRED unavailable)"
