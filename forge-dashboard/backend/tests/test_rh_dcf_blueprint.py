"""Golden regression for the gated DCF (local-dcf-blueprint §12).

These lock BEHAVIORS, not prices — the engine must reproduce the archetype
routing, the intangible-amort correction, the circuit-breaker gates, and the
coupled confidence. A build that lets a broker print a DCF, a serial acquirer
add back intangible amort, or a RED gate coexist with HIGH confidence FAILS here.

Deterministic: fixtures are hand-built fundamentals dicts, no live yfinance.
Run: backend/.venv/bin/python -m pytest tests/test_rh_dcf_blueprint.py -q
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from adapters import rh_archetype, rh_dcf  # noqa: E402


def _base(**kw):
    """A clean, ownable STABLE_COMPOUNDER; override fields per test."""
    f = dict(
        ticker="TEST", sector="Technology", industry="Software - Application",
        current_price=100.0, market_cap=50e9, shares_outstanding=500e6,
        total_debt=2e9, total_cash=5e9, beta_published=1.1,
        ebit=6e9, da_total=1e9, da_intangible=0.0, capex=0.6e9, sbc=0.4e9,
        free_cashflow=5e9, net_income_stmt=4.5e9, effective_tax_rate=0.21,
        revenue_growth=0.08, earnings_growth=0.09, gross_margin=0.70,
        goodwill_assets_ratio=0.10, book_value=20.0, roe=0.20,
    )
    f.update(kw)
    return f


# ── §1 / G1: financials are hard-blocked from DCF ────────────────────────────
def test_financial_blocks_dcf():
    f = _base(ticker="IBKR", sector="Financial Services", industry="Capital Markets")
    assert rh_archetype.classify(f)["archetype"] == "FINANCIAL"
    r = rh_dcf.dcf_value(f, 96.0)
    assert r["available"] is False and r.get("blocked")
    assert r["method"] == "PB_ROE"
    assert any(g["id"] == "G1" and g["level"] == "RED" for g in r["gates"])
    assert r["confidence"] == "N/A"


def test_reit_and_resource_blocked():
    reit = _base(sector="Real Estate", industry="REIT - Retail")
    assert rh_archetype.classify(reit)["archetype"] == "REIT"
    assert rh_dcf.dcf_value(reit, 100.0)["available"] is False
    xom = _base(sector="Energy", industry="Oil & Gas Integrated")
    assert rh_archetype.classify(xom)["archetype"] == "RESOURCE"
    assert rh_dcf.dcf_value(xom, 100.0)["available"] is False


# ── §3a / G9: FCF ≫ NI and noisy → balance-sheet float → block ───────────────
def test_float_breaker_blocks_even_non_financial():
    f = _base(ticker="FLOATY", sector="Technology", industry="Software",
              fcf_3y=[15.7e9, 8.7e9, 4.5e9], net_income_3y=[4.4e9, 3.4e9, 2.8e9])
    assert rh_archetype.cashflow_is_balance_sheet(f) is True
    r = rh_dcf.dcf_value(f, 96.0)
    assert r["available"] is False
    assert "CASHFLOW_IS_BALANCE_SHEET" in r.get("structural_flags", [])


# ── §3: serial acquirer — intangible amort is a COST, not cash ───────────────
def test_serial_acquirer_excludes_intangible_amort():
    f = _base(ticker="CPAY", goodwill_assets_ratio=0.41,
              da_total=393e6, da_intangible=262e6, ebit=1.99e9, capex=0.2e9,
              sbc=0.1e9, free_cashflow=1.765e9, shares_outstanding=72e6,
              market_cap=25.6e9, current_price=356.0,
              revenue_growth=0.09, earnings_growth=0.12)
    assert rh_archetype.classify(f)["archetype"] == "SERIAL_ACQUIRER"
    r = rh_dcf.dcf_value(f, 356.0)
    assert r["available"] and r["method"] == "DCF"
    assert "intangible amort" in r["owner_fcf_source"]
    # owner FCF must be well below the reported $1.765B (the add-back removed)
    assert r["base_fcf"] < 1.765e9
    # and the fair value nowhere near the ~$900 add-back artifact
    assert r["fair_value"] < 600
    # acquirer DCFs are never HIGH confidence (organic-growth estimate)
    assert r["confidence"] != "HIGH"


# ── §4: WACC floors (beta ≥ 0.70, WACC ≥ pretax Kd) ──────────────────────────
def test_wacc_floors():
    f = _base(beta_published=0.20)                 # implausibly low → floored
    r = rh_dcf.dcf_value(f, 100.0)
    wd = r["wacc_detail"]
    assert wd["beta"] >= 0.70
    assert wd["wacc_pct"] >= wd["pretax_cost_of_debt_pct"] - 1e-6


# ── §6 hypergrowth: growth beyond the cap is a known artifact ────────────────
def test_hypergrowth_flagged_artifact():
    f = _base(ticker="NVDA", revenue_growth=0.45, earnings_growth=0.55,
              gross_margin=0.72, capex=0.4e9)      # ASSET_LIGHT hyper-grower
    r = rh_dcf.dcf_value(f, 100.0)
    assert any(g["id"] == "G7" and g["level"] == "RED" for g in r["gates"])
    assert r["confidence"] == "LOW/ARTIFACT"


# ── §6 G6: a 3×+ value gap is an artifact, never HIGH conf ───────────────────
def test_big_gap_is_artifact_not_high_conf():
    # tiny FCF vs a big market cap → wildly "overvalued" → G6 RED
    f = _base(ebit=0.2e9, da_total=0.1e9, capex=0.05e9, free_cashflow=0.15e9,
              market_cap=50e9, shares_outstanding=500e6)
    r = rh_dcf.dcf_value(f, 100.0)
    if r["available"]:
        reds = [g for g in r["gates"] if g["level"] == "RED"]
        assert not (reds and r["confidence"] == "HIGH")


# ── §8: the coupling invariant — RED gate ⇒ never HIGH confidence ────────────
def test_red_gate_never_high_confidence():
    for f in (_base(sector="Financial Services"),
              _base(revenue_growth=0.60, earnings_growth=0.60, gross_margin=0.72, capex=0.3e9),
              _base(fcf_3y=[15.7e9, 8.7e9, 4.5e9], net_income_3y=[4.4e9, 3.4e9, 2.8e9])):
        r = rh_dcf.dcf_value(f, 100.0)
        if any(g["level"] == "RED" for g in r.get("gates", [])):
            assert r["confidence"] in ("LOW/ARTIFACT", "N/A")


# ── negative owner FCF → no fabricated value ─────────────────────────────────
def test_negative_owner_fcf_unavailable():
    f = _base(ebit=-1e9, net_income_stmt=-0.5e9, free_cashflow=-2e9)
    r = rh_dcf.dcf_value(f, 100.0)
    assert r["available"] is False


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-q"]))


# ── §6 / G8: the prudence FLOOR is gated, like the ceiling ───────────────────
# G7 guarded hyper-growers; nothing guarded a shrinking business whose decline
# was replaced by STAGE1_FLOOR. CPB (2026-09-15): revenue -7.9% modelled as
# +2.0%, +169% "upside", HIGH confidence, zero gates.

def test_decline_replaced_by_the_floor_is_an_artifact():
    f = _base(ticker="CPB", revenue_growth=-0.079, earnings_growth=None)
    r = rh_dcf.dcf_value(f, 21.26)
    g8 = [g for g in r["gates"] if g["id"] == "G8"]
    assert g8 and g8[0]["level"] == "RED", r["gates"]
    assert "-7.9% decline" in g8[0]["reason"]
    assert r["confidence"] == "LOW/ARTIFACT"      # a RED gate can never be HIGH


def test_a_small_decline_is_a_caution_not_an_artifact():
    f = _base(revenue_growth=-0.01, earnings_growth=None)
    r = rh_dcf.dcf_value(f, 100.0)
    g8 = [g for g in r["gates"] if g["id"] == "G8"]
    assert g8 and g8[0]["level"] == "YELLOW"
    assert r["confidence"] != "LOW/ARTIFACT"


def test_a_growing_business_is_untouched_by_the_floor_gate():
    r = rh_dcf.dcf_value(_base(), 100.0)
    assert not [g for g in r["gates"] if g["id"] == "G8"]


# ── §6 / G14: an earnings spike is a base effect, not a run-rate ─────────────

def test_earnings_spike_that_lifts_growth_is_flagged():
    f = _base(ticker="WYNN", revenue_growth=0.069, earnings_growth=1.06)
    r = rh_dcf.dcf_value(f, 100.0)
    g14 = [g for g in r["gates"] if g["id"] == "G14"]
    assert g14 and g14[0]["level"] == "YELLOW", r["gates"]
    assert "106%" in g14[0]["reason"]
    assert r["confidence"] != "HIGH"


def test_normal_earnings_growth_is_not_flagged():
    r = rh_dcf.dcf_value(_base(revenue_growth=0.08, earnings_growth=0.09), 100.0)
    assert not [g for g in r["gates"] if g["id"] == "G14"]


def test_serial_acquirers_keep_their_own_organic_treatment():
    f = _base(sector="Technology", industry="Software - Infrastructure",
              revenue_growth=0.09, earnings_growth=1.20,
              goodwill_assets_ratio=0.55, da_intangible=0.8e9)
    r = rh_dcf.dcf_value(f, 100.0)
    if r.get("archetype") == "SERIAL_ACQUIRER":
        assert not [g for g in r["gates"] if g["id"] == "G14"]



# ── fee businesses inside GICS Financials / Real Estate ──────────────────────
# GICS files payment networks and property brokerages under Financials/Real
# Estate, but neither rents out a balance sheet. The blanket sector block was
# suppressing a clean DCF on them. The carve-out is gated so lenders sharing the
# same vendor industry string stay blocked, and every threshold below is
# BRACKETED — a test on each side — so the constants cannot drift unnoticed.

def _fee(**kw):
    """A payment-network shape: fee revenue, no loan book, no clearing float."""
    f = dict(sector="Financial Services", industry="Credit Services",
             total_revenue=44.5e9, interest_expense=0.6e9, total_assets=99.6e9)
    f.update(kw)
    return _base(**f)


def test_payment_network_is_not_blocked_as_financial():
    d = rh_archetype._deterministic(_fee())
    assert d["archetype"] != "FINANCIAL", d
    assert d["dcf_allowed"] is True


def test_a_lender_sharing_the_industry_string_stays_blocked():
    """COF/SYF/AXP are 'Credit Services' too — interest expense gives them away."""
    d = rh_archetype._deterministic(_fee(interest_expense=15.8e9))   # 35% of revenue
    assert d["archetype"] == "FINANCIAL"
    assert d["dcf_allowed"] is False


def test_interest_leg_is_bracketed():
    """Pin FEE_MAX_INTEREST_OF_REV from both sides."""
    rev = 100e9
    assert rh_archetype._deterministic(
        _fee(total_revenue=rev, interest_expense=2.9e9))["archetype"] != "FINANCIAL"
    assert rh_archetype._deterministic(
        _fee(total_revenue=rev, interest_expense=3.1e9))["archetype"] == "FINANCIAL"


def test_assets_leg_is_bracketed():
    """Pin FEE_MAX_ASSETS_OF_REV from both sides — this is the clearing-float guard."""
    rev = 10e9
    assert rh_archetype._deterministic(
        _fee(total_revenue=rev, interest_expense=0.1e9, total_assets=44e9))["archetype"] != "FINANCIAL"
    assert rh_archetype._deterministic(
        _fee(total_revenue=rev, interest_expense=0.1e9, total_assets=46e9))["archetype"] == "FINANCIAL"


def test_zero_is_the_vendors_missing_sentinel_not_evidence():
    """This vendor writes a literal 0.0 for rows it lacks — the same sentinel that
    corrupts profitMargins. Reading it as 'no debt' would open the lender gate."""
    for missing in ({"interest_expense": 0.0}, {"total_assets": 0.0},
                    {"interest_expense": None}, {"total_assets": None}):
        d = rh_archetype._deterministic(_fee(**missing))
        assert d["archetype"] == "FINANCIAL", missing
        assert d["dcf_allowed"] is False, missing


def test_exchanges_and_data_vendors_stay_blocked():
    """'Financial Data & Stock Exchanges' covers both index vendors (SPGI/MCO/FDS)
    and clearing venues (CME/ICE/NDAQ/CBOE/COIN), and no available ratio separates
    them — a CBOE-shaped balance sheet clears both thresholds comfortably. The
    whole string stays blocked; SPGI/MCO/FDS are an accepted false negative."""
    d = rh_archetype._deterministic(
        _fee(industry="Financial Data & Stock Exchanges",
             total_revenue=5.1e9, interest_expense=0.05e9, total_assets=9.3e9))
    assert d["archetype"] == "FINANCIAL", d
    assert d["dcf_allowed"] is False


def test_real_banks_and_insurers_are_untouched():
    for ind in ("Banks - Diversified", "Insurance - Property & Casualty",
                "Capital Markets", "Insurance Brokers", "Asset Management"):
        d = rh_archetype._deterministic(
            _base(sector="Financial Services", industry=ind,
                  total_revenue=50e9, interest_expense=0.1e9, total_assets=60e9))
        assert d["archetype"] == "FINANCIAL", ind
        assert d["dcf_allowed"] is False, ind


def test_property_brokerage_is_released_from_the_reit_block():
    """CBRE/CSGP are fee/brokerage businesses sitting in sector 'Real Estate'."""
    d = rh_archetype._deterministic(
        _base(sector="Real Estate", industry="Real Estate Services",
              total_revenue=43.7e9, interest_expense=0.05e9, total_assets=30.9e9))
    assert d["archetype"] != "REIT", d
    assert d["dcf_allowed"] is True


def test_a_landlord_stays_in_the_reit_block():
    """The carve-out must not become a hole in the REIT gate: a property owner in
    the same sector carries assets far above the threshold."""
    d = rh_archetype._deterministic(
        _base(sector="Real Estate", industry="Real Estate Services",
              total_revenue=2.0e9, interest_expense=0.01e9, total_assets=40e9))
    assert d["archetype"] == "REIT", d
    assert d["dcf_allowed"] is False


def test_midstream_is_a_toll_road_not_a_price_taker():
    """KMI/OKE/WMB are fee-based contracted throughput, not E&P price-takers.
    Capex here is 8% of revenue — under the generic 10% line — so this exercises
    the _CAPINT_WORDS keyword branch rather than the ratio fallback."""
    d = rh_archetype._deterministic(
        _base(sector="Energy", industry="Oil & Gas Midstream",
              total_revenue=39.4e9, capex=3.15e9, da_total=2.0e9))
    assert d["archetype"] == "CAPITAL_INTENSIVE", d
    assert d["dcf_allowed"] is True


def test_upstream_is_still_blocked():
    for ind in ("Oil & Gas E&P", "Gold", "Copper", "Uranium"):
        d = rh_archetype._deterministic(_base(sector="Energy", industry=ind))
        assert d["archetype"] == "RESOURCE", ind


def test_goodwill_still_earns_the_organic_growth_cap():
    """Regression guard on a REVERTED change: an attempt to re-base
    SERIAL_ACQUIRER on amort/revenue demoted genuine roll-ups (TDG at 2.0% of
    revenue) and moved 15 live fair values up and 0 down. Until the schema
    carries a real flow signal, a mature name with heavy goodwill keeps the cap."""
    f = _base(industry="Aerospace & Defense", goodwill_assets_ratio=0.61,
              da_total=0.36e9, da_intangible=0.20e9, total_revenue=10.0e9,
              revenue_growth=0.225)
    assert rh_archetype._deterministic(f)["archetype"] == "SERIAL_ACQUIRER"


# ── G15: the share count has to be the one that goes with the price ──────────
# The cache's shares_outstanding is ONE share class; market_cap covers them all.
# Dividing the whole company's equity by one class overstated fair value by up to
# 5× (BF-B read +47% at HIGH where the answer was −7%). Rows are the live
# 2026-11-15 cache values.

def _g15(r):
    return next((g for g in r["gates"] if g["id"] == "G15"), None)


def test_multi_class_row_is_valued_on_every_share_class():
    """GOOGL: 5,888M Class A cached, 12,294M implied by market cap ÷ price."""
    f = _base(ticker="GOOGL", sector="Communication Services",
              industry="Internet Content & Information",
              current_price=295.77, market_cap=3636.196e9, shares_outstanding=5.888e9,
              enterprise_value=3539.452e9, total_debt=150.331e9, total_cash=265.098e9,
              debt_to_equity=22.2, book_value=53.65, total_revenue=471.098e9,
              ebit=129.039e9, da_total=21.136e9, da_intangible=0.641e9, capex=91.447e9,
              sbc=24.953e9, effective_tax_rate=0.168, revenue_growth=0.247,
              earnings_growth=-0.253, beta_published=1.23, goodwill_assets_ratio=0.056)
    all_classes = f["market_cap"] / f["current_price"]
    r = rh_dcf.dcf_value(f, 295.77)
    assert r["available"]
    assert abs(r["fair_value"] - r["equity_value"] / all_classes) < 0.01
    assert abs(r["fair_value"] - r["equity_value"] / f["shares_outstanding"]) > 50

    # every per-share output moves together: the same answer as a cache that had
    # listed every class in the first place
    whole = rh_dcf.dcf_value(dict(f, shares_outstanding=all_classes), 295.77)
    for k in ("fair_value", "upside_pct", "implied_growth_pct", "implied_wacc_pct", "sensitivity"):
        assert r[k] == whole[k], k
    assert not _g15(whole)

    g = _g15(r)
    assert g and g["level"] == "YELLOW", r["gates"]
    assert "5,888M" in g["reason"] and "12,294M" in g["reason"]
    assert any("12,294M" in c for c in r["caveats"])      # visible wherever caveats show


def test_single_class_row_is_unchanged():
    r = rh_dcf.dcf_value(_base(), 100.0)
    assert not _g15(r)
    assert abs(r["fair_value"] - r["equity_value"] / 500e6) < 0.01
    # ordinary drift between the two counts (a buyback since the last filing) is
    # inside the tolerance: the cached count still wins, silently
    drift = rh_dcf.dcf_value(_base(shares_outstanding=460e6), 100.0)
    assert not _g15(drift)
    assert abs(drift["fair_value"] - drift["equity_value"] / 460e6) < 0.01


def test_a_bad_market_cap_does_not_override_a_good_share_count():
    """WAT: the cached market cap is doubled; the cached count matches the filings.
    Book value sides with the cached count, so it stays."""
    f = _base(ticker="WAT", current_price=460.25, market_cap=90.455e9,
              shares_outstanding=98.286e6, enterprise_value=50.350e9,
              total_debt=5.515e9, total_cash=0.593e9, debt_to_equity=36.1, book_value=155.6)
    shares, gate = rh_dcf._resolve_shares(f)
    assert shares == 98.286e6
    assert gate["level"] == "YELLOW" and "market cap is the figure that is off" in gate["reason"]


def test_enterprise_value_backs_the_all_class_count_when_book_value_cannot():
    """DELL: negative book equity leaves no book-value witness."""
    f = _base(ticker="DELL", current_price=344.53, market_cap=219.057e9,
              shares_outstanding=315.433e6, enterprise_value=259.709e9,
              total_debt=35.284e9, total_cash=11.570e9, debt_to_equity=None, book_value=-2.23)
    shares, gate = rh_dcf._resolve_shares(f)
    assert shares == f["market_cap"] / f["current_price"] and gate["level"] == "YELLOW"


def test_a_share_count_nothing_backs_is_red():
    """CVNA (Up-C): 723M cached, 1,492M implied, 1,103M actual. Enterprise value
    sits on the cached count only because the vendor builds it from the listed
    class, so it must not be allowed to vouch for that count."""
    f = _base(ticker="CVNA", current_price=72.37, market_cap=107.969e9,
              shares_outstanding=722.714e6, enterprise_value=54.589e9,
              total_debt=5.697e9, total_cash=3.606e9, debt_to_equity=100.8, book_value=6.03)
    shares, gate = rh_dcf._resolve_shares(f)
    assert gate["level"] == "RED"
    assert shares == f["market_cap"] / f["current_price"]     # the larger count: errs low
    r = rh_dcf.dcf_value(f, 72.37)
    assert r["available"] and _g15(r)["level"] == "RED"
    assert r["confidence"] == "LOW/ARTIFACT"


def test_witnesses_that_disagree_settle_nothing():
    f = _base(shares_outstanding=300e6,                       # market cap ÷ price says 500M
              debt_to_equity=33.3, book_value=20.0,           # $6.0B book equity ÷ $20 = 300M
              enterprise_value=47e9)                          # ($47B − $2B + $5B) ÷ $100 = 500M
    shares, gate = rh_dcf._resolve_shares(f)
    assert gate["level"] == "RED" and shares == 500e6


# ── G5: an implied WACC the solver cannot reach is a failure, not a blank ────
# The reverse-DCF searches 3%–40%. Past either end it used to return nothing,
# and a missing number passed the gate: PGY (2026-11-15) printed +175% at MEDIUM
# because its price implied a WACC above 40%.

def _g5(r):
    return next((g for g in r["gates"] if g["id"] == "G5"), None)


def _cash_shell(price):
    """$100B of cash and a trickle of cash flow: worth ~$203 a share at the model's
    WACC, and no discount rate moves that much."""
    return _base(ebit=0.12e9, da_total=0.02e9, capex=0.02e9, sbc=0.0, free_cashflow=0.1e9,
                 net_income_stmt=0.1e9, total_cash=100e9, total_debt=0.0,
                 current_price=price, market_cap=price * 500e6)


def test_a_price_no_discount_rate_reaches_from_above_is_red():
    """Cash-rich and priced far under the model: the DCF is still above the price
    at a 40% WACC, and at 2.4× the 3× gap gate does not catch it either."""
    f = _base(total_cash=60e9, total_debt=0.0, current_price=120.0, market_cap=60e9)
    r = rh_dcf.dcf_value(f, 120.0)
    assert r["available"] and 100 < r["upside_pct"] < 200
    assert not [g for g in r["gates"] if g["id"] == "G6"]
    assert r["implied_wacc_pct"] is None
    g = _g5(r)
    assert g and g["level"] == "RED" and "above 40%" in g["reason"], r["gates"]
    assert r["confidence"] == "LOW/ARTIFACT"


def test_a_price_no_discount_rate_reaches_from_below_is_red():
    """Priced above its cash plus anything the cash flow is worth even at 3%."""
    r = rh_dcf.dcf_value(_cash_shell(300.0), 300.0)
    assert r["available"] and not [g for g in r["gates"] if g["id"] == "G6"]
    assert r["implied_wacc_pct"] is None
    g = _g5(r)
    assert g and g["level"] == "RED" and "below 3%" in g["reason"], r["gates"]
    assert r["confidence"] == "LOW/ARTIFACT"


def test_an_implied_wacc_inside_the_range_is_judged_on_its_value():
    r = rh_dcf.dcf_value(_cash_shell(205.0), 205.0)      # implies 7.7%, above its cost of debt
    assert r["implied_wacc_pct"] is not None and not _g5(r)
    assert r["confidence"] == "HIGH"


def test_no_price_is_not_a_gate_failure():
    r = rh_dcf.dcf_value(_base(current_price=None), None)
    assert r["available"] and r["implied_wacc_pct"] is None and not _g5(r)
