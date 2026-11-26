"""Gated, archetype-aware DCF — intrinsic value that refuses to be confidently wrong.

Implements the local-dcf-blueprint: the number is CODE, the confidence is CODE,
and both are coupled to a set of circuit-breaker gates. The pipeline is:

  route (archetype) → owner-FCF (archetype-aware) → WACC (floored) → project →
  discount+terminal → gates → coupled confidence → reverse-DCF reconciliation.

The failure modes it exists to prevent (all real, all from the regression set):
  * IBKR $628 / +554% HIGH-CONF   — a broker; DCF is hard-blocked (§1, G1/G9).
  * CPAY $900 / GEN $69           — acquired-intangible amort added back as free
                                    cash; for serial acquirers it's a real cost (§3).
  * GPN +271% at a 7.1% WACC      — beta/WACC floors + reverse-DCF gate (§4, G5).
Central rule: a RED gate can NEVER coexist with HIGH confidence, and a 3×+ value
gap is a bug report, not a price target.

Backward-compatible: still exposes available/fair_value/upside_pct/sensitivity/…
so the value factor and DCF tab keep working; adds method/archetype/gates/
confidence/implied_wacc.
"""
from __future__ import annotations

from adapters import rh_archetype, safe_num as _num

# ── model constants (the auditable knobs) ───────────────────────────────────
RISK_FREE = 0.045          # ~10y Treasury
EQUITY_RISK_PREMIUM = 0.055
TAX_RATE = 0.23            # blueprint statutory normalization target
CREDIT_SPREAD = 0.020      # pre-tax cost of debt ≈ Rf + spread (no rating data)
TERMINAL_GROWTH = 0.025    # GDP-ish perpetuity
YEARS = 10
STAGE1_CAP = 0.20
STAGE1_FLOOR = 0.020
ACQUIRER_GROWTH_CAP = 0.10  # serial acquirers: reported growth is M&A-bought; cap organic
BETA_FALLBACK = 1.10
BETA_CLAMP = (0.70, 2.5)   # blueprint §4: 0.70 floor kills implausibly-low WACC
WACC_CLAMP = (0.07, 0.16)
TAX_NORM_BAND = (0.15, 0.28)
SHARE_TOLERANCE = 0.10     # two share counts further apart than this are different numbers


def _clamp(x, lo, hi):
    return max(lo, min(hi, x))


def _round(x, nd=2):
    v = _num(x)
    return None if v is None else round(v, nd)


def _norm_tax(fund: dict) -> float:
    """Effective tax normalized into the blueprint band [15%, 28%] (G10)."""
    t = _num(fund.get("effective_tax_rate"))
    if t is None or not (0 < t < 0.6):
        return TAX_RATE
    return _clamp(t, *TAX_NORM_BAND)


# ── owner FCF (blueprint §3) — archetype-aware ───────────────────────────────
def _owner_fcf(fund: dict, exclude_intangible: bool) -> tuple[float | None, str, list]:
    """Owner free cash flow. Preferred build is from statement components
    (NOPAT + tangible D&A − capex; SBC is already an expense inside NOPAT and
    is not subtracted a second time); acquired-intangible amortization is
    EXCLUDED whenever the name bought growth via M&A — serial acquirers AND one-off
    big acquirers (it's real consumption of purchased growth, not cash). Falls back
    to reported FCF (acquirer-corrected) when components are missing."""
    flags: list[str] = []
    ebit = _num(fund.get("ebit"))
    da_total = _num(fund.get("da_total"))
    da_intang = _num(fund.get("da_intangible")) or 0.0
    capex = _num(fund.get("capex"))
    sbc = _num(fund.get("sbc")) or 0.0
    tax = _norm_tax(fund)
    is_acq = exclude_intangible

    if ebit is not None and da_total is not None and capex is not None:
        da_tangible = da_total - (da_intang if is_acq else 0.0)
        nopat = ebit * (1 - tax)
        # SBC is already expensed inside GAAP EBIT → it is counted ONCE via NOPAT.
        # We honor 'SBC is a real cost' by NOT adding it back — not by subtracting
        # it a second time (that double-count is what made every SBC-heavy tech
        # name look 40-85% overvalued).
        fcf = nopat + da_tangible - capex
        src = (f"NOPAT ${nopat/1e9:.2f}B + tangible D&A ${da_tangible/1e9:.2f}B "
               f"− capex ${capex/1e9:.2f}B"
               + (f" (excl. ${da_intang/1e9:.2f}B intangible amort — serial acquirer)"
                  if (is_acq and da_intang) else ""))
        return fcf, src, flags

    # Reported FCF (OCF − capex) ADDED SBC back as a non-cash item → subtract it
    # to count it once; for acquirers also strip the intangible-amort add-back.
    rep = _num(fund.get("free_cashflow"))
    if rep is None:
        return None, "no FCF inputs", ["data_incomplete"]
    flags.append("fcf_from_reported")
    adj = rep - sbc - (da_intang if is_acq else 0.0)
    src = (f"reported FCF ${rep/1e9:.2f}B − ${sbc/1e9:.2f}B SBC"
           + (f" − ${da_intang/1e9:.2f}B intangible amort" if (is_acq and da_intang) else ""))
    return adj, src, flags


def _stage1_growth(fund: dict, archetype: str) -> tuple[float, str, float]:
    # returns (capped_growth, source_label, raw_blended) — the 3rd (uncapped) value
    # is load-bearing: the G7 hyper-growth RED gate keys on it.
    rev = _num(fund.get("revenue_growth"))
    eps = _num(fund.get("earnings_growth"))
    is_acq = archetype == "SERIAL_ACQUIRER"
    if is_acq and rev is not None and eps is not None:
        # reported growth is acquisition-inflated; the LOWER of revenue and
        # (capped) earnings growth leans toward the ORGANIC rate a DCF should use
        # — this is what separates CPAY (~9% organic) from GEN (~3% organic).
        blended = min(rev, _clamp(eps, -0.10, 0.30))
        src = f"organic proxy min(rev {rev*100:.0f}%, eps {eps*100:.0f}%)"
    elif rev is not None and eps is not None:
        blended = 0.70 * rev + 0.30 * _clamp(eps, -0.10, 0.30)
        src = f"0.7×rev {rev*100:.0f}% + 0.3×eps {eps*100:.0f}%"
    elif rev is not None:
        blended, src = rev, f"revenue growth {rev*100:.0f}%"
    elif eps is not None:
        blended, src = _clamp(eps, -0.10, 0.30), f"earnings growth {eps*100:.0f}% (capped)"
    else:
        blended, src = 0.06, "no growth data — default 6%"
    cap = ACQUIRER_GROWTH_CAP if is_acq else STAGE1_CAP
    capped = _clamp(blended, STAGE1_FLOOR, cap)
    if is_acq:
        src += f" → organic cap {cap*100:.0f}%"
    elif abs(capped - blended) > 1e-9:
        src += f" → capped {capped*100:.1f}%"
    return capped, src, blended


def _wacc(fund: dict, beta_fallback: float | None) -> tuple[float, dict]:
    """CAPM cost of equity + after-tax cost of debt, market-value weighted, with
    the blueprint floors: beta ≥ 0.70 and WACC ≥ the firm's own pre-tax Kd."""
    beta = _num(fund.get("beta_published"))
    if beta is None:
        beta = _num(beta_fallback)
    beta = _clamp(beta if beta is not None else BETA_FALLBACK, *BETA_CLAMP)
    coe = RISK_FREE + beta * EQUITY_RISK_PREMIUM
    pretax_cod = RISK_FREE + CREDIT_SPREAD
    tax = _norm_tax(fund)
    after_tax_cod = pretax_cod * (1 - tax)

    mc = _num(fund.get("market_cap")) or 0.0
    debt = _num(fund.get("total_debt")) or 0.0
    cash = _num(fund.get("total_cash")) or 0.0
    net_debt = debt - cash
    d_for_w = max(net_debt, 0.0)
    ev_w = mc + d_for_w
    w_e = (mc / ev_w) if ev_w > 0 else 1.0
    w_d = 1.0 - w_e
    raw = coe * w_e + after_tax_cod * w_d
    wacc = _clamp(raw, *WACC_CLAMP)
    wacc = max(wacc, pretax_cod)          # §4: equity can't be cheaper than its own debt
    detail = {
        "beta": _round(beta, 2),
        "cost_of_equity_pct": _round(coe * 100, 1),
        "after_tax_cost_of_debt_pct": _round(after_tax_cod * 100, 1),
        "pretax_cost_of_debt_pct": _round(pretax_cod * 100, 1),
        "equity_weight_pct": _round(w_e * 100, 0),
        "debt_weight_pct": _round(w_d * 100, 0),
        "wacc_raw_pct": _round(raw * 100, 1),
        "wacc_pct": _round(wacc * 100, 1),
        "net_debt": _round(net_debt, 0),
    }
    return wacc, detail


def _resolve_shares(fund: dict) -> tuple[float | None, dict | None]:
    """The share count that belongs with the price → (shares, gate or None).

    The cached `shares_outstanding` is ONE share class for a multi-class company,
    while `market_cap` covers every class (GOOGL: 5,888M Class A cached
    vs 12,294M from market cap ÷ price; 51 of 625 names were more than 10% apart).
    Dividing the whole company's equity by one class overstated fair value by up
    to 5×, some of it at HIGH confidence (BF-B +47% where the answer was −7%).

    Market cap ÷ price is not always the better number: WAT's cached market cap
    was doubled and its cached count was the right one. So when the two disagree,
    the count is the one a second figure in the row backs up — book equity ÷ book
    value per share, or the market cap inside enterprise value:
      * the market-cap count is backed → use it, YELLOW so the swap is visible;
      * book value backs the cached count → keep it, YELLOW (the market cap is the
        bad number). Enterprise value cannot vouch for the cached count: for an
        Up-C company (SYM, CVNA) the vendor builds it from the listed class alone;
      * nothing settles it, or the two witnesses disagree → RED. The larger count
        is used, so the number that is shown errs low (CVNA: 723M cached, 1,492M
        implied, 1,103M actual).
    stock-options-ai/deep_dcf/inputs.py:resolve_shares applies the same rule with
    SEC filings as the witness; this engine only has the fundamentals row."""
    listed = _num(fund.get("shares_outstanding"))
    listed = listed if listed and listed > 0 else None
    mc, px = _num(fund.get("market_cap")), _num(fund.get("current_price"))
    implied = mc / px if (mc and px and mc > 0 and px > 0) else None
    if not (listed and implied) or abs(listed / implied - 1.0) <= SHARE_TOLERANCE:
        return listed, None

    def backed(witness):
        """Whichever of the two candidates `witness` lands within tolerance of."""
        if not witness or witness <= 0:
            return None
        near = min((listed, implied), key=lambda c: abs(witness / c - 1.0))
        return near if abs(witness / near - 1.0) <= SHARE_TOLERANCE else None

    debt, cash = _num(fund.get("total_debt")), _num(fund.get("total_cash"))
    d_to_e, bvps = _num(fund.get("debt_to_equity")), _num(fund.get("book_value"))
    ev = _num(fund.get("enterprise_value"))
    # debt_to_equity is a PERCENT, so book equity = debt ÷ (D/E ÷ 100).
    book_says = (backed(debt / (d_to_e / 100.0) / bvps)
                 if (debt and d_to_e and bvps and min(debt, d_to_e, bvps) > 0) else None)
    ev_says = (backed((ev - debt + cash) / px)
               if (ev and debt is not None and cash is not None) else None)

    counts = f"the cache lists {listed/1e6:,.0f}M shares and market cap ÷ price implies {implied/1e6:,.0f}M"
    if book_says != listed and implied in (book_says, ev_says):
        return implied, _gate("G15", "YELLOW", f"share count — {counts}; the listed figure is one "
                              "share class, so the larger count is used")
    if book_says == listed and ev_says != implied:
        return listed, _gate("G15", "YELLOW", f"share count — {counts}, but book value agrees with "
                             "the listed count, so the market cap is the figure that is off")
    return max(listed, implied), _gate("G15", "RED", f"share count unverified — {counts}, and nothing "
                                       "else in the data settles which; fair value uses the larger "
                                       "count and is not reliable")


def _fair_for_growth(g1, wacc, tg, fcf0, shares, net_debt):
    pv, fcf = 0.0, fcf0
    for t in range(1, YEARS + 1):
        g_t = g1 + (tg - g1) * (t - 1) / (YEARS - 1)
        fcf *= (1 + g_t)
        pv += fcf / (1 + wacc) ** (t - 0.5)
    tv = fcf * (1 + tg) / (wacc - tg)
    pv += tv / (1 + wacc) ** (YEARS - 0.5)
    return (pv - net_debt) / shares


def _implied_growth(last, wacc, tg, shares, net_debt, fcf0):
    last = _num(last)
    if not last or last <= 0:
        return None
    lo, hi = -0.10, 0.60
    if (_fair_for_growth(lo, wacc, tg, fcf0, shares, net_debt) - last) > 0:
        return None
    if (_fair_for_growth(hi, wacc, tg, fcf0, shares, net_debt) - last) < 0:
        return None
    for _ in range(40):
        mid = (lo + hi) / 2
        if _fair_for_growth(mid, wacc, tg, fcf0, shares, net_debt) - last < 0:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2 * 100


def _implied_wacc(last, g1, tg, shares, net_debt, fcf0):
    """Reverse-DCF: the discount rate that sets DCF == price (holding growth)
    → (rate %, None). The honest headline for a big gap — 'the market is pricing
    a X% cost of capital'.

    When no rate in the search range does it, → (None, "above 40%" | "below 3%"):
    the answer lies past that end. That is a finding, not a missing number — the
    caller must not read it as 'nothing to report'. (None, None) only when there
    is no price to solve against."""
    last = _num(last)
    if not last or last <= 0:
        return None, None
    lo, hi = max(tg + 0.005, 0.03), 0.40    # higher WACC → lower value
    if _fair_for_growth(g1, hi, tg, fcf0, shares, net_debt) > last:
        return None, f"above {hi*100:.0f}%"     # worth more than the price even at the top rate
    if _fair_for_growth(g1, lo, tg, fcf0, shares, net_debt) < last:
        return None, f"below {lo*100:.0f}%"     # worth less than the price even at the bottom rate
    for _ in range(48):
        mid = (lo + hi) / 2
        if _fair_for_growth(g1, mid, tg, fcf0, shares, net_debt) - last > 0:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2 * 100, None


_ARCH_HUMAN = {
    "FINANCIAL": "Financial", "REIT": "REIT", "RESOURCE": "Resource",
    "PRE_PROFIT": "Pre-profit", "SERIAL_ACQUIRER": "Serial acquirer",
    "ASSET_LIGHT": "Asset-light", "CAPITAL_INTENSIVE": "Capital-intensive",
    "STABLE_COMPOUNDER": "Stable compounder", "HOLDCO": "Holdco",
}


def _arch_human(a: str) -> str:
    """Human label for an archetype enum ('REIT' not 'Reit', 'Pre-profit' not
    'Pre_Profit') so gate-reason strings read cleanly in the UI."""
    return _ARCH_HUMAN.get(a, (a or "").replace("_", " ").title())


# ── alternative valuation for DCF-blocked archetypes (blueprint §1) ──────────
def _alt_value(fund: dict, last: float | None, archetype: str) -> dict:
    """A defensible non-DCF estimate for blocked archetypes. Financials get a
    justified P/B-vs-ROE (excess-return) fair value; others report the correct
    method label without fabricating a point estimate from data we lack (FFO/NAV)."""
    method = {"FINANCIAL": "PB_ROE", "REIT": "FFO", "RESOURCE": "NAV",
              "PRE_PROFIT": "REV_MULTIPLE"}.get(archetype, "NONE")
    if archetype == "FINANCIAL":
        bvps = _num(fund.get("book_value"))
        roe = _num(fund.get("roe"))
        beta = _clamp(_num(fund.get("beta_published")) or BETA_FALLBACK, *BETA_CLAMP)
        ke = RISK_FREE + beta * EQUITY_RISK_PREMIUM
        g = min(TERMINAL_GROWTH, ke - 0.02)
        if bvps and bvps > 0 and roe is not None and ke > g:   # negative book → no P/B-ROE (was printing −$0.92 fair values)
            just_pb = max(0.3, (roe - g) / (ke - g))     # Gordon excess-return P/B
            fair = bvps * just_pb
            up = ((fair / last - 1) * 100) if last else None
            return {"method": method, "fair_value": _round(fair, 2),
                    "upside_pct": _round(up, 1), "justified_pb": _round(just_pb, 2),
                    "detail": f"justified P/B {just_pb:.2f}× (ROE {roe*100:.0f}%, Ke "
                              f"{ke*100:.1f}%, g {g*100:.1f}%) × book ${bvps:.2f}"}
    return {"method": method, "fair_value": None, "upside_pct": None,
            "detail": f"{_arch_human(archetype)} — DCF not applicable; value on {method}"}


# ── gates (blueprint §6) + coupled confidence (§8) ───────────────────────────
def _gate(gid, level, reason):
    return {"id": gid, "level": level, "reason": reason}


def _confidence(gates: list, hard_cap_medium: bool) -> str:
    reds = [g for g in gates if g["level"] == "RED"]
    yellows = [g for g in gates if g["level"] == "YELLOW"]
    if reds:
        return "LOW/ARTIFACT"
    if len(yellows) >= 2:
        conf = "MEDIUM"
    elif len(yellows) == 1:
        conf = "MEDIUM-HIGH"
    else:
        conf = "HIGH"
    if hard_cap_medium and conf in ("HIGH", "MEDIUM-HIGH"):
        conf = "MEDIUM"
    return conf


def _is_fund(fund: dict) -> bool:
    """ETF / ETN / closed-end fund — priced by its holdings (NAV), so a single-
    company DCF is meaningless. An income ETF otherwise fell through to the
    company path and got a nonsensical 'owner FCF ≤ 0' rejection."""
    name = (fund.get("name") or "").upper()
    if " ETF" in name or name.endswith("ETF") or " ETN" in name or " FUND" in name:
        return True
    # No operating financials (no sector, no revenue, no FCF) but a real cap → fund.
    return (not fund.get("sector")
            and _num(fund.get("total_revenue")) is None
            and _num(fund.get("free_cashflow")) is None
            and bool(_num(fund.get("market_cap"))))


def dcf_value(fund: dict, last: float | None,
              beta_fallback: float | None = None) -> dict:
    """Archetype-routed, gated intrinsic value. Returns a dict that is backward
    compatible with the old shape (available/fair_value/upside_pct/…) plus
    method/archetype/gates/confidence/implied_wacc."""
    ticker = fund.get("ticker")
    last = _num(last) or _num(fund.get("current_price"))

    # Funds first — an ETF/CEF has no company financials to discount. Honest
    # "NAV-priced" note beats routing it through the company DCF for a garbage reason.
    if _is_fund(fund):
        return {"available": False, "method": "FUND", "archetype": "FUND",
                "structural_flags": [], "confidence": "N/A",
                "reason": "ETF / fund — priced by its holdings (NAV), not a single-company DCF",
                "current_price": _round(last, 2)}

    arch = rh_archetype.classify(fund, ticker)
    archetype = arch["archetype"]
    flags = arch["structural_flags"]

    # ── G1: wrong-tool block (financials/REIT/resource/pre-profit or float) ──
    if not arch["dcf_allowed"]:
        alt = _alt_value(fund, last, archetype)
        reason = ("customer/settlement float — that 'FCF' is not owner cash; DCF vetoed"
                  if "CASHFLOW_IS_BALANCE_SHEET" in flags
                  else f"{_arch_human(archetype)} — DCF is the wrong tool")
        return {
            "available": False, "blocked": True, "method": alt["method"],
            "archetype": archetype, "structural_flags": flags,
            "reason": reason, "why_archetype": arch["why"],
            "alt_fair_value": alt.get("fair_value"), "alt_upside_pct": alt.get("upside_pct"),
            "alt_detail": alt.get("detail"),
            "gates": [_gate("G1", "RED", reason)],
            "confidence": "N/A", "current_price": _round(last, 2),
        }

    # Strip purchased-intangible amort for BOTH genuine serial acquirers and one-off
    # big acquirers (AMD/AVGO); the organic-growth cap (_stage1_growth) stays keyed
    # strictly on SERIAL_ACQUIRER so a one-off acquirer keeps its real growth.
    exclude_intang = archetype == "SERIAL_ACQUIRER" or "ONE_OFF_ACQUIRER" in flags
    fcf0, fcf_src, fcf_flags = _owner_fcf(fund, exclude_intang)
    shares, share_gate = _resolve_shares(fund)
    data_incomplete = "data_incomplete" in fcf_flags

    if fcf0 is None or fcf0 <= 0:
        return {"available": False, "method": "DCF", "archetype": archetype,
                "structural_flags": flags,
                "reason": ("owner free cash flow ≤ 0 (heavy-capex build-out, "
                           "intangible-amort drag, or unprofitable) — a perpetuity "
                           "DCF would be misleading"),
                "owner_fcf_source": fcf_src,
                "gates": [_gate("G9", "RED", "non-positive owner FCF")],
                "confidence": "LOW/ARTIFACT"}
    if not shares or shares <= 0:
        return {"available": False, "method": "DCF", "archetype": archetype,
                "reason": "shares outstanding unavailable", "confidence": "N/A"}

    g1, g_src, g_raw = _stage1_growth(fund, archetype)
    wacc, wd = _wacc(fund, beta_fallback)
    tg = TERMINAL_GROWTH
    if wacc <= tg:
        wacc = tg + 0.02

    pv_explicit, fcf = 0.0, fcf0
    for t in range(1, YEARS + 1):
        g_t = g1 + (tg - g1) * (t - 1) / (YEARS - 1)
        fcf *= (1 + g_t)
        pv_explicit += fcf / (1 + wacc) ** (t - 0.5)
    tv = fcf * (1 + tg) / (wacc - tg)
    pv_terminal = tv / (1 + wacc) ** (YEARS - 0.5)
    ev = pv_explicit + pv_terminal
    net_debt = _num(wd["net_debt"]) or 0.0
    equity = ev - net_debt
    mc = _num(fund.get("market_cap"))

    # Units guard (foreign-ADR local-currency FCF vs USD price) → suppress. Still
    # compute the raw per-share value so the suppressed-view diagnostic drawer can
    # show the untrusted number that motivated the gate (not a blank '—').
    if mc and mc > 0 and equity > 8.0 * mc:
        _raw = equity / shares
        return {"available": False, "suppressed": True, "method": "DCF",
                "archetype": archetype,
                "reason": "FCF vs market cap imply inconsistent units (likely a "
                          "foreign ADR reporting in local currency) — DCF suppressed",
                "owner_fcf_source": fcf_src, "base_fcf": _round(fcf0, 0),
                "raw_fair_value": _round(_raw, 2),
                "raw_upside_pct": (_round((_raw / last - 1) * 100, 1)
                                   if (last and _raw > 0) else None),
                "gates": [_gate("G11", "RED", "unit mismatch")], "confidence": "N/A"}

    fair = equity / shares
    upside = ((fair / last - 1) * 100) if (last and fair > 0) else None
    tv_frac = (pv_terminal / ev * 100) if ev else None
    ratio = (fair / last) if (last and last > 0) else None

    implied_g = _implied_growth(last, wacc, tg, shares, net_debt, fcf0)
    implied_w, implied_w_beyond = _implied_wacc(last, g1, tg, shares, net_debt, fcf0)

    # ── gates ──
    # G2/G3 are inert under the current constants (the WACC floor keeps wacc ≥ 7%
    # and TERMINAL_GROWTH is a fixed 2.5%), so neither fires today — they are kept
    # as assertions that re-activate if terminal growth ever becomes per-name.
    gates: list = [share_gate] if share_gate else []
    if tg >= wacc:
        gates.append(_gate("G2", "RED", "terminal growth ≥ WACC (invalid perpetuity)"))
    if tg > min(RISK_FREE, 0.03):
        gates.append(_gate("G3", "YELLOW", f"terminal g {tg*100:.1f}% > min(Rf, 3%)"))
    if tv_frac is not None and tv_frac > 90:
        gates.append(_gate("G4", "RED", f"terminal value {tv_frac:.0f}% of EV (>90%)"))
    elif tv_frac is not None and tv_frac > 80:
        gates.append(_gate("G4", "YELLOW", f"terminal value {tv_frac:.0f}% of EV (>80%)"))
    # G5 fires on an implausible implied WACC (absolute), OR — the case a levered,
    # short-history name like SOLV slips under the 15% ceiling — when the market's
    # implied discount rate exceeds the model's by >450bps: the upside is then a
    # discount-rate artifact (the model is more optimistic on risk than the market),
    # so suppress it and let the reverse-DCF speak.
    # An implied WACC the solver cannot reach fails the absolute test by more than
    # it can measure: past 40% it is above the 15% ceiling, under 3% it is below any
    # cost of debt. It used to come back as None and skip the gate, and one
    # name printed +175% at MEDIUM that way.
    model_wacc_pct = wacc * 100
    if implied_w_beyond:
        gates.append(_gate("G5", "RED", f"price implies a WACC {implied_w_beyond} — the "
                     "assumption, not the business, is doing the work"))
    elif implied_w is not None and (implied_w < wd["pretax_cost_of_debt_pct"] or implied_w > 15
                                    or implied_w > model_wacc_pct + 4.5):
        why = (f"price implies a {implied_w:.1f}% WACC vs the model's {model_wacc_pct:.1f}% — "
               "the discount-rate assumption, not the business, is doing the work"
               if implied_w > model_wacc_pct + 4.5 and implied_w <= 15
               else f"price implies a {implied_w:.1f}% WACC — the assumption, not the "
                    "business, is doing the work")
        gates.append(_gate("G5", "RED", why))
    if ratio is not None and (ratio > 3.0 or ratio < 0.34):
        gates.append(_gate("G6", "RED", f"fair/price = {ratio:.1f}× — a 3×+ gap is a "
                     "screen artifact to investigate, not a price target"))
    if archetype != "SERIAL_ACQUIRER" and g_raw is not None and g_raw > 0.25:
        gates.append(_gate("G7", "RED", f"pre-cap growth ~{g_raw*100:.0f}% is beyond the "
                     "DCF's reliable range — the prudence cap makes hyper-growers look "
                     "overvalued; read the reverse-DCF implied growth, not this gap"))
    elif g1 >= STAGE1_CAP - 1e-9:
        gates.append(_gate("G7", "YELLOW", "stage-1 growth pinned at the prudence cap"))
    # G8 is G7's missing mirror. G7 guards the prudence CEILING (hyper-growers look
    # overvalued once capped); nothing guarded the FLOOR, so a SHRINKING business had
    # its decline replaced by STAGE1_FLOOR and was then valued as a grower. CPB
    # (2026-09-15): revenue -7.9% became +2.0% — a 9.9pp substitution carrying a
    # +169% "upside" at HIGH confidence with an empty gate list.
    if g_raw is not None and g_raw < 0 < g1:
        _lift_pp = (g1 - g_raw) * 100
        if _lift_pp >= 5.0:
            gates.append(_gate("G8", "RED", f"modelled +{g1*100:.1f}% growth replaces an "
                         f"observed {g_raw*100:.1f}% decline ({_lift_pp:.0f}pp lift from the "
                         "prudence floor) — the fair value rests on that substitution, not on "
                         "the business; read the reverse-DCF implied growth instead"))
        else:
            gates.append(_gate("G8", "YELLOW", f"prudence floor lifted growth from "
                         f"{g_raw*100:.1f}% to {g1*100:.1f}% — a flat-to-shrinking top line"))
    # G14: an earnings-growth spike is a base effect, not a run-rate. The blend already
    # clamps EPS growth to 30%, but a clamped 30% still adds ~9pp to a single-digit
    # revenue trend — WYNN (2026-09-15) reached 13.8% stage-1 off eps +106% against
    # revenue +7%, clearing every gate at HIGH.
    _eps_g = _num(fund.get("earnings_growth"))
    _rev_g = _num(fund.get("revenue_growth"))
    if (archetype != "SERIAL_ACQUIRER" and _eps_g is not None and _eps_g > 0.50
            and _rev_g is not None and g1 > _rev_g + 0.02):
        gates.append(_gate("G14", "YELLOW", f"earnings growth {_eps_g*100:.0f}% is a "
                     "recovery/base effect, not a run-rate — it pushed stage-1 growth to "
                     f"{g1*100:.1f}% against a {_rev_g*100:.0f}% revenue trend"))
    _raw_tax = _num(fund.get("effective_tax_rate"))
    if _raw_tax is None or not (0.0 < _raw_tax < 0.6):
        gates.append(_gate("G10", "YELLOW", "effective tax unavailable — statutory default assumed"))
    elif abs(_norm_tax(fund) - _raw_tax) > 1e-9:
        gates.append(_gate("G10", "YELLOW", "effective tax normalized into 15–28% band"))
    if data_incomplete or "fcf_from_reported" in fcf_flags:
        gates.append(_gate("G12", "YELLOW", "owner FCF from reported cash flow "
                     "(statement split unavailable) — treat as screen-grade"))
    recent_ipo = "RECENT_IPO" in flags
    if recent_ipo:
        gates.append(_gate("G13", "YELLOW", "recent IPO (<1y public) — no track "
                     "record; trailing FCF/growth unreliable, read the reverse-DCF"))

    # Serial-acquirer DCFs rest on an organic-growth estimate → widen the band
    # (never HIGH); data gaps do the same.
    confidence = _confidence(gates, hard_cap_medium=(data_incomplete
                             or archetype == "SERIAL_ACQUIRER" or recent_ipo))

    caveats = [
        f"Owner FCF ${fcf0/1e9:.2f}B ({fcf_src}), grown {g1*100:.1f}%→{tg*100:.1f}% over "
        f"{YEARS}y, discounted at WACC {wacc*100:.1f}% (mid-year), Gordon terminal.",
    ]
    if implied_w is not None:
        caveats.append(f"Reverse-DCF: today's price implies a {implied_w:.1f}% cost of "
                       f"capital (vs our {wacc*100:.1f}%) at {g1*100:.0f}% growth.")
    for g in gates:
        # every RED, plus the share-count gate at any level: a swapped share count
        # changes the per-share value itself, so it is stated wherever caveats show
        if g["level"] == "RED" or g is share_gate:
            caveats.append("⚠ " + g["reason"])

    sens_waccs = [wacc - 0.01, wacc, wacc + 0.01]
    sens_gs = [g1 - 0.02, g1, g1 + 0.02]
    sensitivity = {
        "wacc_pcts": [_round(w * 100, 1) for w in sens_waccs],
        "growth_pcts": [_round(g * 100, 1) for g in sens_gs],
        "fair_values": [[_round(_fair_for_growth(g, max(w, tg + 0.015), tg, fcf0, shares, net_debt), 2)
                         for w in sens_waccs] for g in sens_gs],
    }

    return {
        "available": True, "method": "DCF",
        "archetype": archetype, "structural_flags": flags, "why_archetype": arch["why"],
        "fair_value": _round(fair, 2), "current_price": _round(last, 2),
        "upside_pct": _round(upside, 1),
        "wacc_pct": wd["wacc_pct"], "stage1_growth_pct": _round(g1 * 100, 1),
        "terminal_growth_pct": _round(tg * 100, 1), "years": YEARS,
        "base_fcf": _round(fcf0, 0), "owner_fcf_source": fcf_src,
        "enterprise_value": _round(ev, 0), "equity_value": _round(equity, 0),
        "pv_explicit": _round(pv_explicit, 0), "pv_terminal": _round(pv_terminal, 0),
        "terminal_value_fraction_pct": _round(tv_frac, 0),
        "wacc_detail": wd, "growth_source": g_src,
        "implied_growth_pct": _round(implied_g, 1), "implied_wacc_pct": _round(implied_w, 1),
        "gates": gates, "confidence": confidence,
        "sensitivity": sensitivity, "caveats": caveats,
        "assumptions": (f"WACC {wd['wacc_pct']}% (β {wd['beta']}, ERP "
                        f"{EQUITY_RISK_PREMIUM*100:.1f}%, Rf {RISK_FREE*100:.1f}%), "
                        f"growth {g1*100:.1f}%→{tg*100:.1f}% over {YEARS}y."),
        "disclaimer": "Screen-grade gated DCF — assumptions surfaced; not a price target.",
    }
