"""Build inputs.json — the model's ONLY data source for one symbol.

The model runs with no web access, so everything it may use is here: five fiscal
years of statements from SEC XBRL, last-twelve-month figures, the latest balance
sheet, price / beta / share count from the dashboard's fundamentals cache, and
the 10-year yield. Two things are deliberately withheld so the deep model stays
an independent second opinion: the deterministic engine's fair value, and the
analyst price target.

`build_inputs` also decides whether the data is good enough to spend money on.
A non-empty `fatal` list means "skip this name, cost nothing".
"""
from __future__ import annotations

from datetime import date
from typing import Optional

from deep_dcf.config import Settings

SCHEMA_VERSION = 2
_CORE = ("revenue", "operating_income", "operating_cash_flow")
_MAX_FY_AGE_DAYS = 600          # latest fiscal year older than this = stale filer
_PRICE_DISAGREE_PCT = 15.0      # research price vs fundamentals-cache price
_SHARE_CORROBORATION = 0.15     # a share count must be this close to an SEC count


def _num(v) -> Optional[float]:
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    v = float(v)
    return v if v == v and v not in (float("inf"), float("-inf")) else None


def _rel(a: float, b: float) -> float:
    return abs(a - b) / abs(b) if b else float("inf")


def complete_years(statements: dict) -> int:
    """How many of the MOST RECENT fiscal years have every core line item
    (counted back from the latest year; a gap stops the count). Capex is not
    core: some filers leave a small capex line untagged in one year, and that
    is a note, not a reason to skip the name."""
    annual = statements.get("annual") or {}
    n = len(statements.get("fiscal_year_ends") or [])
    count = 0
    for i in range(n - 1, -1, -1):
        if all((annual.get(k) or [None] * n)[i] is not None for k in _CORE):
            count += 1
        else:
            break
    return count


def fill_operating_income(statements: dict) -> list[str]:
    """Some filers (NKE) do not tag OperatingIncomeLoss. Where gross profit and
    SG&A are tagged, derive it — gross profit − SG&A − R&D — and say so. Fills
    `statements` in place; → the fiscal-year ends that were derived."""
    annual = statements.get("annual") or {}
    ends = statements.get("fiscal_year_ends") or []
    op = list(annual.get("operating_income") or [None] * len(ends))
    gp, sga = annual.get("gross_profit") or [], annual.get("sga") or []
    rnd = annual.get("research_development") or []
    derived = []
    for i, e in enumerate(ends):
        if i < len(op) and op[i] is None and i < len(gp) and i < len(sga) \
                and gp[i] is not None and sga[i] is not None:
            op[i] = gp[i] - sga[i] - ((rnd[i] if i < len(rnd) else None) or 0.0)
            derived.append(e)
    if derived:
        annual["operating_income"] = op
    return derived


def resolve_shares(fund: dict, statements: dict) -> tuple[Optional[float], str, list[str]]:
    """The share count that goes with the market price → (shares, source, notes).
    `shares` is None when no count can be trusted (the caller skips the name).

    Two cached numbers can each be wrong. `shares_outstanding` is ONE share
    class for a multi-class company (5.9B Class A of 12.3B in one case, so a
    model using the right count failed a check made against the wrong one). `market_cap` can
    be wrong too (WAT: doubled, while the cached shares equal the SEC cover page
    exactly). So when the two disagree by more than 10%, neither wins on its
    own: the one that agrees with an SEC count — the cover page, or the latest
    fiscal year's diluted average — is used. If neither does, there is no
    trustworthy count."""
    notes: list[str] = []
    listed = _num(fund.get("shares_outstanding"))
    mc, px = _num(fund.get("market_cap")), _num(fund.get("current_price"))
    implied = mc / px if mc and px and px > 0 else None
    listed = listed if listed and listed > 0 else None
    sh = statements.get("shares") or {}
    diluted = [v for v in (sh.get("diluted_shares") or []) if _num(v)]
    cover = _num((sh.get("cover_page_shares_outstanding") or {}).get("value"))
    sec = [v for v in ([cover] if cover else []) + diluted[-1:] if v and v > 0]

    if listed and implied and abs(listed / implied - 1.0) > 0.10:
        def gap(x):
            return min((abs(x / r - 1.0) for r in sec), default=float("inf"))
        best, other, label = ((implied, listed, "market cap / price (all share classes)")
                              if gap(implied) <= gap(listed)
                              else (listed, implied, "fundamentals cache shares outstanding"))
        if gap(best) > _SHARE_CORROBORATION:
            return None, "unavailable", [
                f"share count cannot be corroborated: cache says {listed/1e6:,.0f}M, market cap / price "
                f"says {implied/1e6:,.0f}M, and neither is within {_SHARE_CORROBORATION:.0%} of an SEC count"]
        notes.append(f"two cached share counts disagree ({listed/1e6:,.0f}M listed vs {implied/1e6:,.0f}M "
                     f"from market cap / price); {best/1e6:,.0f}M matches the SEC filings and is the one to "
                     f"use — {other/1e6:,.0f}M is one share class only, or a bad market cap")
        shares, source = best, label
    elif listed:
        shares, source = listed, "fundamentals cache shares outstanding"
    elif implied:
        shares, source = implied, "market cap / price"
    else:
        return None, "unavailable", notes
    if diluted and abs(diluted[-1] / shares - 1.0) > 0.25:
        notes.append(f"SEC diluted shares for the latest fiscal year ({diluted[-1]/1e6:,.0f}M) differ from the "
                     f"current count ({shares/1e6:,.0f}M) by more than 25% — a split, large buyback or "
                     "issuance since year end; use the current count with the current price")
    return shares, source, notes


def bounded_beta(fund: dict, risk_free: float, s: Settings) -> tuple[float, Optional[float], list[str]]:
    """→ (beta to use, published beta, notes).

    CAPM with a raw beta of 2.25 (NVDA) gives a 17.5% cost of equity, which the
    acceptance check rejects as outside the range where a DCF means anything —
    so the run would be paid for and thrown away. The beta is bounded here
    instead, to the range that keeps base and bear WACC inside the gate at
    today's risk-free rate, and the published figure is passed alongside."""
    lo, hi = float(s.beta_clamp[0]), float(s.beta_clamp[1])
    erp = s.equity_risk_premium
    if erp > 0 and s.accept.get("wacc_max"):
        # leave 1.5 points of room for a bear case that adds to the base WACC
        hi = min(hi, (float(s.accept["wacc_max"]) - 0.015 - risk_free) / erp)
    hi = max(hi, lo)
    raw = _num(fund.get("beta_published"))
    if raw is None:
        return min(max(1.0, lo), hi), None, ["no published beta; 1.0 (market) is used"]
    beta = min(max(raw, lo), hi)
    notes = []
    if abs(beta - raw) > 1e-9:
        notes.append(f"published beta {raw:.2f} is outside the {lo:.2f}-{hi:.2f} range a CAPM discount "
                     f"rate is usable in; market.beta is bounded to {beta:.2f} (raw figure: beta_published)")
    return round(beta, 4), raw, notes


def build_inputs(*, symbol: str, research_row: dict, fund: dict, statements: dict,
                 filing: Optional[dict], risk_free: float, risk_free_source: str,
                 settings: Settings, today: date) -> tuple[dict, list[str]]:
    """→ (inputs dict, fatal problems). Pure: no I/O."""
    fatal: list[str] = []
    notes: list[str] = []
    fund = fund or {}
    dcf = (research_row or {}).get("dcf") or {}
    ends = statements.get("fiscal_year_ends") or []
    annual = statements.get("annual") or {}

    # ── can we afford to trust this data? ───────────────────────────────────
    derived = fill_operating_income(statements)
    if derived:
        notes.append("operating income is not tagged in XBRL for fiscal years ending "
                     f"{', '.join(derived)}; it was derived as gross profit − SG&A − R&D")
    annual = statements.get("annual") or {}
    years = complete_years(statements)
    if years < settings.min_fiscal_years:
        fatal.append(f"only {years} complete fiscal year(s) of revenue/operating income/"
                     f"operating cash flow in SEC XBRL (need {settings.min_fiscal_years})")
    if ends:
        age = (today - date.fromisoformat(ends[-1])).days
        if age > _MAX_FY_AGE_DAYS:
            fatal.append(f"latest fiscal year in XBRL ended {ends[-1]} ({age} days ago) — stale filer")

    price = _num((research_row or {}).get("last")) or _num(fund.get("current_price"))
    if not price or price <= 0:
        fatal.append("no current price")
    cache_px = _num(fund.get("current_price"))
    if price and cache_px and cache_px > 0 and _rel(price, cache_px) * 100.0 > _PRICE_DISAGREE_PCT:
        # the share count and market cap travel with the cache price; a split
        # between the two snapshots would put a 2-3x error into the per-share value
        fatal.append(f"research price ${price:,.2f} and fundamentals-cache price ${cache_px:,.2f} differ by "
                     f"{_rel(price, cache_px) * 100:.0f}% — possible split or stale cache")
    shares, shares_source, share_notes = resolve_shares(fund, statements)
    notes.extend(share_notes)
    if not shares or shares <= 0:
        fatal.append("no shares outstanding")

    fy_rev = (annual.get("revenue") or [None])[-1] if ends else None
    series = [x for x in (fund.get("revenue_series") or []) if _num(x)]
    if fy_rev and series:
        # fundamentals.json may be one fiscal year ahead of or behind XBRL
        gap = min(_rel(fy_rev, _num(x)) for x in series[:2]) * 100.0
        if gap > settings.revenue_mismatch_pct:
            fatal.append(f"latest-FY revenue disagrees between SEC XBRL (${fy_rev/1e6:,.0f}M) and "
                         f"the fundamentals cache by {gap:.0f}% — wrong tag or wrong entity")
    elif fy_rev:
        notes.append("fundamentals cache has no revenue history; XBRL revenue could not be cross-checked")

    derived_oi = (statements.get("derived") or {}).get("operating_income") or []
    if derived_oi:
        notes.append("this company tags no operating-income line for fiscal years "
                     + ", ".join(derived_oi) + "; operating_income there is DERIVED as pre-tax "
                     "income + interest expense, so it still includes non-operating items and "
                     "one-off charges (e.g. acquired in-process R&D) — read margins with that in mind")
    ltm = statements.get("ltm") or {}
    ttm_rev = _num(fund.get("total_revenue"))
    if ltm.get("revenue") and ttm_rev:
        gap = _rel(ltm["revenue"]["value"], ttm_rev) * 100.0
        if gap > 3.0:
            notes.append(f"LTM revenue from XBRL (${ltm['revenue']['value']/1e6:,.0f}M) differs from the "
                         f"fundamentals-cache TTM (${ttm_rev/1e6:,.0f}M) by {gap:.1f}%; prefer the XBRL figure")
    if not ltm.get("revenue"):
        notes.append("no clean LTM could be derived from the latest 10-Q; base the model on the latest fiscal year")
    da = annual.get("depreciation_amortization") or []
    if ends and (not da or da[-1] is None):
        notes.append("D&A not tagged in XBRL for the latest year; use ttm.depreciation_amortization")
    capex = annual.get("capex") or []
    missing_capex = [e for i, e in enumerate(ends) if i >= len(capex) or capex[i] is None]
    if missing_capex:
        notes.append(f"capex not tagged in XBRL for fiscal years ending {', '.join(missing_capex)}; "
                     "use ttm.capex and the tagged years")
    for name, c in (statements.get("conflicts") or {}).items():
        vals = ", ".join(f"{t} ${v/1e6:,.0f}M" for t, v in c["values"].items())
        notes.append(f"{name}: two XBRL tags report different latest-year values ({vals}); "
                     f"{c.get('used')} is used — check it is the statement total")
    basis = statements.get("basis") or {}
    if basis.get("years_from_earlier_filings"):
        notes.append("fiscal years ending " + ", ".join(basis["years_from_earlier_filings"])
                     + " are as last reported in EARLIER 10-Ks (a 10-K restates only three years); a "
                       "divestiture or reclassification since then makes them non-comparable — weight "
                       "the recent three years when setting growth and margins")
    if basis.get("share_count_breaks"):
        notes.append("share count jumps at fiscal years ending " + ", ".join(basis["share_count_breaks"])
                     + " (stock split); per-share history before that is on a different footing")
    beta, beta_raw, beta_notes = bounded_beta(fund, risk_free, settings)
    notes.extend(beta_notes)

    debt, cash = _num(fund.get("total_debt")), _num(fund.get("total_cash"))
    interest = _num(fund.get("interest_expense"))
    if debt is None or cash is None:
        notes.append("total debt / cash missing from the fundamentals cache; derive net debt from latest_balance_sheet")

    inputs = {
        "schema": SCHEMA_VERSION,
        "symbol": symbol.upper(),
        "company": fund.get("name") or (research_row or {}).get("name"),
        "sector": fund.get("sector") or (research_row or {}).get("sector"),
        "industry": fund.get("industry"),
        "as_of": today.isoformat(),
        "units_note": ("Every monetary value in this file is in raw US dollars (not thousands, not "
                       "millions). Share counts are raw shares. Rates and margins are decimals "
                       "(0.055 = 5.5%)."),
        "market": {
            "price": price,
            "price_source": "Forge research cache (previous close)",
            "market_cap": _num(fund.get("market_cap")),
            "shares_outstanding": shares,
            "shares_source": shares_source,
            "beta": beta,
            "beta_published": beta_raw,
            "beta_source": "published 5y monthly beta (fundamentals cache), bounded — see data_quality_notes",
            "week52_low": _num(fund.get("wk52_low")),
            "week52_high": _num(fund.get("wk52_high")),
            "dividend_yield_pct": _num(fund.get("dividend_yield_pct")),
        },
        "rates": {
            "risk_free_10y": risk_free,
            "risk_free_source": risk_free_source,
            "equity_risk_premium": settings.equity_risk_premium,
            "equity_risk_premium_source": "Forge standard assumption (market range 5.0-6.0%)",
        },
        "capital_structure": {
            "total_debt": debt,
            "total_cash": cash,
            "net_debt": (debt - cash) if debt is not None and cash is not None else None,
            "interest_expense_ttm": interest,
            "implied_pretax_cost_of_debt": round(interest / debt, 4)
                                           if interest and debt and debt > 0 else None,
            "effective_tax_rate": _num(fund.get("effective_tax_rate")),
            "source": "fundamentals cache (Yahoo Finance)",
        },
        "ttm": {
            "source": "fundamentals cache (Yahoo Finance), fetched " + str(fund.get("fetched_at")),
            "revenue": ttm_rev,
            "ebit": _num(fund.get("ebit")),
            "ebitda": _num(fund.get("ebitda")),
            "net_income": _num(fund.get("net_income_stmt")),
            "operating_cash_flow": _num(fund.get("operating_cashflow")),
            "capex": _num(fund.get("capex")),
            "free_cash_flow": _num(fund.get("free_cashflow")),
            "depreciation_amortization": _num(fund.get("da_total")),
            "amortization_intangibles": _num(fund.get("da_intangible")),
            "stock_based_compensation": _num(fund.get("sbc")),
            "gross_margin": _num(fund.get("gross_margin")),
            "operating_margin": _num(fund.get("operating_margin")),
            "revenue_growth_yoy": _num(fund.get("revenue_growth")),
            "goodwill": _num(fund.get("goodwill")),
            "total_assets": _num(fund.get("total_assets")),
        },
        "annual": {
            "source": "SEC XBRL company facts (10-K), latest-filed value per fiscal year",
            "fiscal_year_ends": ends,
            "income_and_cash_flow": annual,
            "balance_sheet": statements.get("balance_sheet") or {},
            "shares": {k: v for k, v in (statements.get("shares") or {}).items()
                       if k != "cover_page_shares_outstanding"},
            "xbrl_tags_used": statements.get("tags") or {},
        },
        "ltm": {
            "source": "SEC XBRL: latest fiscal year + current YTD − prior-year YTD (latest 10-Q)",
            **{k: {"value": v["value"], "through": v["through"]} for k, v in ltm.items()},
        } if ltm else None,
        "latest_balance_sheet": statements.get("latest_balance_sheet") or None,
        "cover_page_shares": (statements.get("shares") or {}).get("cover_page_shares_outstanding"),
        "latest_filing": {k: (filing or {}).get(k) for k in ("form", "filing_date", "period_end",
                                                             "accession")} if filing else None,
        "consensus": {
            "forward_eps": _num(fund.get("forward_eps")),
            "trailing_eps": _num(fund.get("trailing_eps")),
            "forward_pe": _num(fund.get("forward_pe")),
            "analyst_count": _num(fund.get("num_analysts")),
            "rating": fund.get("recommendation"),
            "earnings_growth_yoy": _num(fund.get("earnings_growth")),
        },
        "business_flags": {
            "archetype": dcf.get("archetype"),
            "why": dcf.get("why_archetype"),
            "structural_flags": dcf.get("structural_flags") or [],
            "research_notes": (research_row or {}).get("confidence_notes") or [],
        },
        "data_quality_notes": notes,
    }
    return inputs, fatal
