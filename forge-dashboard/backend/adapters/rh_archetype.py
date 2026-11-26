"""Deterministic archetype router — classify BEFORE valuing (blueprint §1).

The archetype decides whether a DCF is even legal and, if so, which FCF
definition + guardrails apply. This is PURE deterministic logic (GICS sector +
industry keywords + statement ratios) so it runs instantly on the whole ~516-name
universe and is fully reproducible — no LLM in the valuation hot path.

A background job (jobs/archetype_refine.py) may OVERRIDE a name's archetype via
the on-disk cache using the local LLM, but only within hard guardrails: the LLM
can NEVER move a deterministically-BLOCKED name (Financial / REIT / Resource /
Pre-profit) out of the blocked set, and the deterministic result is always the
fallback. classify() reads that cache when present and fresh.

The router is the immune system's first cell: getting IBKR classified FINANCIAL
(→ DCF blocked) is what stops the $628 / +554% artifact at the door.
"""
from __future__ import annotations

import os
import time

from adapters import read_json, safe_num as _num

# DCF is illegal for these — emit the correct alternative multiple instead.
# The LLM refinement may never move a name OUT of this set (see classify()).
BLOCKED = {"FINANCIAL", "REIT", "RESOURCE", "PRE_PROFIT"}

# Scale/margin guard: a currently-unprofitable name with revenue at/above this
# floor AND a gross margin at/above this bar is a real, DCF-normalizable business
# (cyclical/investment loss), NOT a pre-profit startup — even if the 3y net-income
# window shows no positive year. Genuine pre-profit names sit BELOW one of these.
PRE_PROFIT_REV_FLOOR = 100e6     # $100M revenue = past startup scale
PRE_PROFIT_MIN_GM = 0.35         # healthy product economics (not a broken model)


_FIN_SECTORS = {"financial services", "financials"}
_FIN_WORDS = ("bank", "broker", "insurance", "insurer", "capital markets",
              "asset management", "exchange", "financial data", "credit services",
              "mortgage", "reinsurance")
_REIT_WORDS = ("reit", "real estate investment")
# GICS files these under Financials / Real Estate, but they carry no loan book,
# no underwriting risk and no settlement float — the cash flow is an ordinary
# subscription or per-transaction toll. The blanket sector block was suppressing
# a perfectly good DCF on payment networks (V/MA/PYPL), index & ratings vendors
# (SPGI/MCO/FDS) and property brokerages (CBRE/CSGP) — 9 names in the live
# universe, all of which value cleanly at HIGH confidence once allowed through.
# The carve-out is gated on _is_fee_business(), so lenders that share the same
# industry string (COF/SYF/AXP/SOFI/UPST) stay blocked.
# NOT carved out: "financial data & stock exchanges". That one vendor string
# covers both index/ratings vendors (SPGI/MCO/FDS) and clearing venues that hold
# customer margin (CME/ICE/NDAQ/COIN/CBOE), and no available ratio separates
# them — CBOE clears both thresholds with 60%+ margin. SPGI/MCO/FDS therefore
# stay blocked: a known, accepted false negative. Releasing them needs a
# clearing-house signal the schema does not carry.
_FEE_BUSINESS_WORDS = ("credit services", "real estate services")
# A lender funds itself with debt; a toll road does not. A clearing house's
# balance sheet dwarfs its P&L; a data vendor's does not.
FEE_MAX_INTEREST_OF_REV = 0.03
FEE_MAX_ASSETS_OF_REV = 4.5
_RESOURCE_WORDS = ("oil & gas e&p", "oil & gas exploration", "oil & gas drilling",
                   "oil & gas integrated", "oil & gas refining",
                   "coal", "gold", "silver", "copper", "metals & mining", "mining",
                   "aluminum", "steel", "precious metals", "uranium")
# Genuine serial-acquirer / roll-up INDUSTRIES — heavy goodwill here is a repeated
# M&A flywheel (organic-growth cap is correct), unlike a one-off transformative deal.
_ROLLUP_WORDS = ("software", "diagnostics", "research", "medical instrument",
                 "medical device", "specialty industrial", "specialty business",
                 "information technology services", "scientific & technical",
                 "industrial distribution", "waste management", "staffing")
# Capital-intensive INDUSTRIES (autos/industrials/utilities/telecom/semi-equip) —
# caught by keyword so a name just under the capex/rev line (TSLA 8.7%) still routes
# to mid-cycle normalization instead of a growth-capex-crushed single-stage DCF.
_CAPINT_WORDS = ("auto manufacturer", "auto parts", "airline", "railroad", " rail",
                 "utilit", "integrated freight", "trucking", "marine shipping",
                 "aerospace", "building products", "semiconductor equipment", "steel",
                 "chemicals", "farm & heavy", "heavy construction", "telecom",
                 "oil & gas equipment", "oil & gas midstream")


def _median(xs):
    xs = sorted(v for v in xs if v is not None)
    n = len(xs)
    if not n:
        return None
    return xs[n // 2] if n % 2 else (xs[n // 2 - 1] + xs[n // 2]) / 2


def cashflow_is_balance_sheet(fund: dict) -> bool:
    """Blueprint §3a breaker: reported FCF persistently ≫ net income AND noisy →
    the 'cash flow' is customer/settlement/segregated float, not owner cash (the
    IBKR detector). True → DCF must be vetoed regardless of sector."""
    fcf3 = [n for v in (fund.get("fcf_3y") or []) if (n := _num(v)) is not None]
    ni3 = [n for v in (fund.get("net_income_3y") or []) if (n := _num(v)) is not None]
    if len(fcf3) < 2 or len(ni3) < 2:
        return False
    mf, mn = _median(fcf3), _median(ni3)
    if not mf or not mn or mn <= 0 or mf <= 0:
        return False
    # SBC-aware: high-SBC software (PLTR) shows FCF ≫ NI because stock comp is
    # added back in OCF — that's a real (dilutive) economic cost, NOT settlement
    # float. Netting SBC out of the FCF median before the ratio test keeps the
    # breaker aimed at genuine balance-sheet float (IBKR: FCF/NI gap is customer
    # cash, SBC tiny — still fires; verified against the golden regression).
    sbc = _num(fund.get("sbc")) or 0.0
    if (mf - max(sbc, 0.0)) <= 1.5 * mn:
        return False
    if mf <= 1.5 * mn:
        return False
    mean_f = sum(fcf3) / len(fcf3)
    if mean_f == 0:
        return False
    var = sum((v - mean_f) ** 2 for v in fcf3) / len(fcf3)
    cov = (var ** 0.5) / abs(mean_f)
    return cov > 0.40


def _is_fee_business(fund: dict, industry: str) -> bool:
    """True when a Financials/Real-Estate-sector name earns a fee stream rather
    than renting out its balance sheet.

    Deliberately strict: a missing signal returns False (stay blocked). A wrong
    DCF on a lender is the IBKR +554% artifact class; a missing DCF is merely a
    gap on the screen. MSCI fails this test on interest expense (6.3% of revenue,
    from a leveraged buyback rather than a loan book) and stays blocked — a known
    false negative we accept rather than loosen the lender guard.
    """
    if not any(w in industry for w in _FEE_BUSINESS_WORDS):
        return False
    rev = _num(fund.get("total_revenue"))
    ie = _num(fund.get("interest_expense"))
    ta = _num(fund.get("total_assets"))
    # This vendor emits a literal 0.0 for rows it simply does not have (the same
    # sentinel that corrupts profitMargins), so 0.0 is MISSING, not "no debt".
    # Fail closed on it: a genuinely debt-free fee business stays blocked rather
    # than a lender being released on an absent interest-expense row.
    if not rev or rev <= 0 or not ie or not ta:
        return False
    return (abs(ie) / rev) < FEE_MAX_INTEREST_OF_REV and (ta / rev) < FEE_MAX_ASSETS_OF_REV


def _deterministic(fund: dict) -> dict:
    sector = (fund.get("sector") or "").strip().lower()
    industry = (fund.get("industry") or "").strip().lower()
    flags: list[str] = []

    ebit = _num(fund.get("ebit"))
    ni = _num(fund.get("net_income_stmt"))
    fcf = _num(fund.get("free_cashflow"))
    rev = _num(fund.get("total_revenue"))
    capex = _num(fund.get("capex"))
    gm = _num(fund.get("gross_margin"))
    gwr = _num(fund.get("goodwill_assets_ratio"))
    da_t = _num(fund.get("da_total"))
    da_i = _num(fund.get("da_intangible"))
    net_debt = (_num(fund.get("total_debt")) or 0.0) - (_num(fund.get("total_cash")) or 0.0)

    # structural flags (co-exist with any archetype)
    if net_debt < 0:
        flags.append("NET_CASH")
    if fcf is not None and fcf < 0:
        flags.append("NEGATIVE_FCF")
    # Recent IPO: < ~1y of public history → no track record, trailing multiples
    # and any DCF are unreliable (the SPCX case). Rides into any archetype.
    ipo_epoch = _num(fund.get("first_trade_epoch"))
    if ipo_epoch is not None and (time.time() - ipo_epoch) < 365 * 24 * 3600:
        flags.append("RECENT_IPO")
    float_breaker = cashflow_is_balance_sheet(fund)
    if float_breaker:
        flags.append("CASHFLOW_IS_BALANCE_SHEET")

    def out(arch, why):
        return {"archetype": arch, "structural_flags": flags, "why": why,
                "source": "deterministic", "dcf_allowed": arch not in BLOCKED
                and "CASHFLOW_IS_BALANCE_SHEET" not in flags}

    # ── derived signals ──
    rev_g = _num(fund.get("revenue_growth"))
    intang_share = (da_i / da_t) if (da_i and da_t and da_t > 0) else 0.0
    intang_amort_rev = (da_i / rev) if (da_i and rev and rev > 0) else 0.0   # acquired-intangible amort intensity
    capex_int = (capex / rev) if (capex is not None and rev and rev > 0) else None
    total_da_rev = (da_t / rev) if (da_t and rev and rev > 0) else None       # total-D&A (capital-intensity) proxy
    capint_kw = any(w in industry for w in _CAPINT_WORDS)
    rollup_sector = any(w in industry for w in _ROLLUP_WORDS)
    ni3 = [n for v in (fund.get("net_income_3y") or []) if (n := _num(v)) is not None]
    pos_hist = any(v > 0 for v in ni3)

    # ── ordered classification: most specific / most dangerous first ──
    fee_biz = _is_fee_business(fund, industry)
    if (sector in _FIN_SECTORS or any(w in industry for w in _FIN_WORDS)) and not fee_biz:
        return out("FINANCIAL", "GICS Financials / bank-broker-insurer industry")
    if (sector == "real estate" or any(w in industry for w in _REIT_WORDS)) and not fee_biz:
        return out("REIT", "real estate / REIT")
    if any(w in industry for w in _RESOURCE_WORDS):
        return out("RESOURCE", "commodity price-taker (E&P / miner)")
    if float_breaker:                                  # non-financial with float → block
        return out("FINANCIAL", "FCF≫NI & noisy → balance-sheet/settlement float")

    # Pre-profit ONLY when the firm has NEVER earned money AND isn't already a
    # cash machine. A GAAP loss driven by stock-comp on a name with strongly
    # positive owner FCF (e.g. TEAM/Atlassian: EBIT −$130M but FCF +$1.4B) is a
    # real, DCF-able business — NOT a pre-profit startup. A cyclical-trough loss
    # (profitable before) is NORMALIZED, not DCF-blocked (INTC/F ≠ MRNA).
    fcf_positive = fcf is not None and fcf > 0
    neg_now = (not fcf_positive) and (
        (ebit is not None and ebit < 0) or (ni is not None and ni < 0))
    if neg_now:
        # SCALE/MARGIN GUARD: a name with MATERIAL revenue AND a healthy gross
        # margin (real product economics) is a cyclical/investment loss, NOT a
        # pre-profit startup — even when the short 3y net-income window shows no
        # positive year. A long down-cycle can bury a prior profit peak just
        # outside that window: e.g. SITM earned +$32M/+$23M in 2021-22, then 3
        # straight loss years at $200-330M revenue and ~56% GM — a mature,
        # DCF-normalizable business, not a startup. A genuine pre-profit name has
        # tiny revenue AND/OR broken unit economics (thin/negative GM).
        scaled = (rev is not None and rev >= PRE_PROFIT_REV_FLOOR
                  and gm is not None and gm >= PRE_PROFIT_MIN_GM)
        if not pos_hist and not scaled:
            return out("PRE_PROFIT", "never profitable — no earnings to discount")
        flags.append("CYCLICAL_LOSS")
        base = ("cyclical trough loss" if pos_hist
                else f"scaled loss (~${rev / 1e6:.0f}M rev, {gm:.0%} GM) — real business")
        if capint_kw or (capex_int is not None and capex_int > 0.07):
            return out("CAPITAL_INTENSIVE", base + " — normalize mid-cycle")
        return out("STABLE_COMPOUNDER", base + " — normalize")

    # Serial acquirer = a genuine ROLL-UP (repeated M&A flywheel), NOT one big deal.
    # A one-off transformative acquisition inside a hyper-grower (AMD/Xilinx, AVGO)
    # is flagged ONE_OFF_ACQUIRER instead: its purchased-intangible amort is still
    # stripped (rh_dcf), but its organic growth is NOT capped at the acquirer rate.
    acq_evidence = (gwr is not None and gwr > 0.35) and (intang_share > 0.30 or intang_amort_rev > 0.03)
    if acq_evidence:
        # NOTE (2026-09-23): goodwill is a STOCK, so a one-off acquirer keeps this
        # label — and its organic-growth cap — long after the deal that earned it
        # (DIS/Fox, HON, CARR, NXPI all sit here). Correcting that safely needs a
        # FLOW signal the fundamentals schema does not carry: year-over-year change
        # in goodwill, or cash paid for acquisitions. An attempt to proxy it with
        # purchased-intangible amort intensity was reverted — amort/revenue is
        # scale-dependent, so it demoted genuine roll-ups (TDG 2.0% of revenue,
        # MSI 1.9%, HSIC 1.3%) and moved 15 fair values UP and 0 down, turning a
        # fail-closed guard into a loosening. Add the flow field first.
        if (rev_g is None or rev_g <= 0.30) or rollup_sector:
            return out("SERIAL_ACQUIRER", f"goodwill/assets {gwr:.0%}, intangible amort "
                       f"{intang_share:.0%} of D&A — repeated M&A")
        flags.append("ONE_OFF_ACQUIRER")
    # Even without the goodwill bar (a near-threshold roll-up, or a name routed to
    # CAPITAL_INTENSIVE below), heavy purchased-intangible amort means growth was
    # BOUGHT — strip that amort from owner FCF. The flag rides into any archetype;
    # only the organic-growth cap stays SERIAL_ACQUIRER-only.
    if "ONE_OFF_ACQUIRER" not in flags and intang_share > 0.30 and intang_amort_rev > 0.03:
        flags.append("ONE_OFF_ACQUIRER")

    # Capital-intensive: autos/industrials/utilities by industry (catches TSLA at
    # 8.7% capex, just under the ratio line), or high capex/D&A intensity outright.
    if capint_kw and ((capex_int is not None and capex_int > 0.07)
                      or (total_da_rev is not None and total_da_rev > 0.07)):
        return out("CAPITAL_INTENSIVE", f"{(industry or 'capital-intensive')[:22]} — capital-intensive")
    if (capex_int is not None and capex_int > 0.10) or (total_da_rev is not None and total_da_rev > 0.15):
        return out("CAPITAL_INTENSIVE", f"capex {(capex_int or 0):.0%} of revenue — normalize mid-cycle")

    if (capex_int is not None and capex_int < 0.06) and (gm is not None and gm > 0.55):
        return out("ASSET_LIGHT", f"capex {capex_int:.0%} of rev, gross margin {gm:.0%}")
    return out("STABLE_COMPOUNDER", "mature profitable — standard DCF")


def _cache_path() -> str:
    # co-located with the other research artifacts; overridable for tests
    default = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                           "data", "archetype_cache.json")
    return os.environ.get("RH_ARCHETYPE_CACHE", default)


_CACHE_TTL_SEC = 8 * 24 * 3600  # a refresh runs ~daily; 8d tolerates a few misses


def classify(fund: dict, ticker: str | None = None) -> dict:
    """Return {archetype, structural_flags, why, source, dcf_allowed}.

    Deterministic result is authoritative. If the LLM-refinement cache holds a
    fresh entry for this ticker, it may REFINE the archetype — but never un-block
    a hard-sector name (Financial/REIT stay blocked) and never enable a DCF the
    float breaker vetoed. A missing/stale cache changes nothing."""
    det = _deterministic(fund)
    if not ticker:
        return det
    try:
        cache = read_json(_cache_path(), default=None)
        entry = (cache or {}).get(ticker.upper()) if isinstance(cache, dict) else None
    except Exception:
        entry = None
    if not isinstance(entry, dict):
        return det
    ts = entry.get("as_of_epoch")
    if not isinstance(ts, (int, float)) or (time.time() - ts) > _CACHE_TTL_SEC:
        return det
    arch = str(entry.get("archetype") or "").upper()
    if arch not in {"FINANCIAL", "REIT", "RESOURCE", "PRE_PROFIT", "SERIAL_ACQUIRER",
                    "ASSET_LIGHT", "CAPITAL_INTENSIVE", "STABLE_COMPOUNDER", "HOLDCO"}:
        return det
    # Guardrails: the LLM may re-route among ALLOWED archetypes and may even tighten
    # a name INTO a blocked class, but it can NEVER move a deterministically-BLOCKED
    # name (Financial / REIT / Resource / Pre-profit) OUT of the blocked set, nor
    # un-block a float-breaker veto. The deterministic block is the artifact firewall.
    if det["archetype"] in BLOCKED and arch not in BLOCKED:
        return det
    if "CASHFLOW_IS_BALANCE_SHEET" in det["structural_flags"] and arch not in BLOCKED:
        return det
    flags = sorted(set(det["structural_flags"]) | set(entry.get("structural_flags") or []))
    return {"archetype": arch, "structural_flags": flags,
            "why": entry.get("why") or det["why"], "source": "llm_refined",
            "dcf_allowed": arch not in BLOCKED and "CASHFLOW_IS_BALANCE_SHEET" not in flags}
