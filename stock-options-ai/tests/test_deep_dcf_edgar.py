"""XBRL extraction on synthetic company facts (pure functions, no network)."""
from datetime import date

from deep_dcf import edgar
from deep_dcf import inputs as inputs_mod
from tests.deep_dcf_helpers import filing, make_settings, research_row


def _fact(val, end, start=None, form="10-K", filed="2026-05-21"):
    f = {"val": val, "end": end, "form": form, "filed": filed}
    if start:
        f["start"] = start
    return f


def _facts(tags: dict, unit="USD", dei=None) -> dict:
    out = {"facts": {"us-gaap": {t: {"units": {u: items}} for (t, u), items in
                                 {(k if isinstance(k, tuple) else (k, unit)): v
                                  for k, v in tags.items()}.items()}}}
    if dei:
        out["facts"]["dei"] = dei
    return out


FY = [("2021-04-03", "2022-04-01"), ("2022-04-02", "2023-03-31"), ("2023-04-01", "2024-03-29"),
      ("2024-03-30", "2025-03-28"), ("2025-03-29", "2026-04-03")]      # last one is a 53-week year


def _annual(vals, filed="2026-05-21"):
    return [_fact(v, e, s, filed=filed) for v, (s, e) in zip(vals, FY)]


def test_annual_points_keep_fiscal_years_only_and_latest_filed_wins():
    items = _annual([10, 20, 30, 40, 50]) + [
        _fact(7, "2025-06-27", "2025-03-29", form="10-Q"),               # a quarter in a 10-Q
        _fact(12, "2026-04-03", "2026-01-03"),                           # a quarter inside the 10-K
        _fact(41, "2025-03-28", "2024-03-30", filed="2026-05-22"),       # FY2025 restated later
    ]
    pts = edgar._annual_points(items)
    assert sorted(pts) == [e for _, e in FY]
    assert pts["2025-03-28"]["val"] == 41 and pts["2026-04-03"]["val"] == 50


def test_current_tag_is_primary_and_an_old_tag_only_backfills():
    facts = _facts({
        "Revenues": [_fact(999, "2014-03-28", "2013-03-30", filed="2014-05-20")],
        "RevenueFromContractWithCustomerExcludingAssessedTax": _annual([1, 2, 3, 4, 5]),
    })
    pts = edgar.annual_concept(facts, edgar.FLOW_TAGS["revenue"])
    assert pts["2026-04-03"]["tag"] == "RevenueFromContractWithCustomerExcludingAssessedTax"
    assert pts["2014-03-28"]["val"] == 999                               # history kept, not preferred
    assert edgar.fiscal_year_ends(facts, 5) == [e for _, e in FY]


def test_total_revenue_tag_wins_a_tie_over_the_contract_subset():
    facts = _facts({
        "Revenues": _annual([11, 22, 33, 44, 55]),
        "RevenueFromContractWithCustomerExcludingAssessedTax": _annual([10, 20, 30, 40, 50]),
    })
    assert edgar.annual_concept(facts, edgar.FLOW_TAGS["revenue"])["2026-04-03"]["val"] == 55


def test_ltm_is_fy_plus_ytd_minus_prior_ytd():
    tag = "RevenueFromContractWithCustomerExcludingAssessedTax"
    facts = _facts({tag: _annual([1, 2, 3, 3935, 5000]) + [
        _fact(1300, "2026-07-03", "2026-04-04", form="10-Q", filed="2026-08-07"),   # Q1 (3 months)
        _fact(1350, "2026-10-02", "2026-07-04", form="10-Q", filed="2026-10-29"),   # Q2 quarter only
        _fact(2650, "2026-10-02", "2026-04-04", form="10-Q", filed="2026-10-29"),   # Q2 YTD
        _fact(2400, "2025-09-26", "2025-03-29", form="10-Q", filed="2026-10-29"),   # prior-year YTD
        _fact(1200, "2025-09-26", "2025-06-28", form="10-Q", filed="2026-10-29"),
    ]})
    ltm = edgar.ltm_value(facts, edgar.FLOW_TAGS["revenue"], "2026-04-03")
    assert ltm["value"] == 5000 + 2650 - 2400 and ltm["through"] == "2026-10-02"


def test_ltm_is_none_without_a_matching_prior_year_or_a_current_year_ytd():
    tag = "Revenues"
    only_cur = _facts({tag: _annual([1, 2, 3, 4, 5000]) + [
        _fact(2650, "2026-10-02", "2026-04-04", form="10-Q")]})
    assert edgar.ltm_value(only_cur, (tag,), "2026-04-03") is None
    stale_q = _facts({tag: _annual([1, 2, 3, 4, 5000]) + [
        _fact(2400, "2025-09-26", "2025-03-29", form="10-Q")]})          # no 10-Q after the 10-K
    assert edgar.ltm_value(stale_q, (tag,), "2026-04-03") is None
    wrong_start = _facts({tag: _annual([1, 2, 3, 4, 5000]) + [
        _fact(900, "2026-10-02", "2026-07-04", form="10-Q"),             # a quarter, not YTD
        _fact(800, "2025-09-26", "2025-06-28", form="10-Q")]})
    assert edgar.ltm_value(wrong_start, (tag,), "2026-04-03") is None


def test_extract_statements_shapes_and_missing_values_are_none():
    facts = _facts({
        "Revenues": _annual([100, 110, 120, 130, 140]),
        "OperatingIncomeLoss": _annual([10, 11, 12, 13, 14]),
        "NetCashProvidedByUsedInOperatingActivities": _annual([20, 21, 22, 23, 24]),
        "PaymentsToAcquirePropertyPlantAndEquipment": _annual([2, 2, 2, 2, 2])[2:],   # 3 years only
        "Assets": [_fact(900, "2026-04-03"), _fact(950, "2026-10-02", form="10-Q", filed="2026-10-29")],
        "CashAndCashEquivalentsAtCarryingValue": [_fact(50, "2026-04-03"),
                                                  _fact(60, "2026-10-02", form="10-Q", filed="2026-10-29")],
        "Goodwill": [_fact(5, "2026-11-01", form="10-Q", filed="2026-11-05")],        # stray later date
        ("WeightedAverageNumberOfDilutedSharesOutstanding", "shares"): _annual([9, 9, 9, 9, 8]),
    }, dei={"EntityCommonStockSharesOutstanding": {"units": {"shares": [
        _fact(7, "2026-10-20", form="10-Q", filed="2026-10-29")]}}})
    st = edgar.extract_statements(facts)
    assert st["fiscal_year_ends"] == [e for _, e in FY]
    assert st["annual"]["revenue"] == [100, 110, 120, 130, 140]
    assert st["annual"]["capex"] == [None, None, 2, 2, 2]
    assert st["annual"]["gross_profit"] == [None] * 5                     # never guessed
    assert st["shares"]["diluted_shares"][-1] == 8
    assert st["shares"]["cover_page_shares_outstanding"] == {"as_of": "2026-10-20", "value": 7}
    # the snapshot is anchored on total assets, not on Goodwill's stray date
    assert st["latest_balance_sheet"]["as_of"] == "2026-10-02"
    assert st["latest_balance_sheet"]["cash"] == 60 and st["latest_balance_sheet"]["goodwill"] is None
    assert inputs_mod.complete_years(st) == 5                             # capex is a note, not core


def test_empty_facts_do_not_crash():
    st = edgar.extract_statements({})
    assert st["fiscal_year_ends"] == [] and inputs_mod.complete_years(st) == 0


def test_latest_periodic_ignores_other_forms_and_foreign_filers():
    subs = {"filings": {"recent": {
        "form": ["8-K", "10-Q", "4", "10-K", "10-Q"],
        "filingDate": ["2026-11-01", "2026-10-29", "2026-09-01", "2026-05-21", "2026-08-07"],
        "accessionNumber": ["a", "b", "c", "d", "e"], "reportDate": ["", "2026-10-02", "", "2026-04-03", ""]}}}
    hit = edgar.latest_periodic(subs)
    assert (hit["form"], hit["filing_date"], hit["accession"]) == ("10-Q", "2026-10-29", "b")
    assert edgar.latest_periodic({"filings": {"recent": {"form": ["20-F", "6-K"],
                                                         "filingDate": ["2026-04-01", "2026-05-01"]}}}) is None
    assert edgar.latest_periodic({}) is None


class _Resp:
    def __init__(self, text="", status=200):
        self.text, self.status_code = text, status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class _Sess:
    def __init__(self, resp):
        self.resp = resp

    def get(self, *a, **k):
        if isinstance(self.resp, Exception):
            raise self.resp
        return self.resp


def test_risk_free_takes_the_last_real_observation_and_falls_back():
    csv = "observation_date,DGS10\n2026-11-10,4.98\n2026-11-11,\n2026-11-12,5.10\n2026-11-13,.\n"
    assert edgar.risk_free_10y(0.045, session=_Sess(_Resp(csv))) == (0.051, "FRED DGS10 2026-11-12")
    assert edgar.risk_free_10y(0.045, session=_Sess(RuntimeError("down")))[0] == 0.045
    assert edgar.risk_free_10y(0.045, session=_Sess(_Resp("observation_date,DGS10\n2026-11-12,99\n")))[0] == 0.045


# ── inputs.json ─────────────────────────────────────────────────────────────

def _statements(years=5, rev_last=5_000e6):
    ends = [e for _, e in FY][-years:]
    return {"fiscal_year_ends": ends,
            "annual": {"revenue": [1e9] * (years - 1) + [rev_last], "operating_income": [1e8] * years,
                       "operating_cash_flow": [2e8] * years, "capex": [1e7] * years,
                       "depreciation_amortization": [5e7] * years},
            "balance_sheet": {}, "shares": {}, "tags": {}, "coverage": {},
            "ltm": {"revenue": {"value": 5.25e9, "through": "2026-10-02"}}, "latest_balance_sheet": {}}


FUND = {"name": "Acme Software Inc.", "current_price": 30.4, "shares_outstanding": 593e6,
        "total_debt": 8.1e9, "total_cash": 0.58e9, "interest_expense": 0.569e9,
        "revenue_series": [5.0e9, 3.935e9], "total_revenue": 5.228e9, "beta_published": 1.24,
        "target_mean": 32.27, "effective_tax_rate": 0.356, "fetched_at": "2026-11-14T06:22:55Z"}


def _build(tmp_path, statements=None, fund=None, row=None, today=date(2026, 11, 15)):
    return inputs_mod.build_inputs(
        symbol="acme", research_row=row or research_row("ACME", last=30.01), fund=FUND if fund is None else fund,
        statements=statements or _statements(), filing=filing(), risk_free=0.051,
        risk_free_source="FRED", settings=make_settings(tmp_path), today=today)


def test_good_data_builds_inputs_with_no_fatal_problems(tmp_path):
    inp, fatal = _build(tmp_path)
    assert fatal == []
    assert inp["symbol"] == "ACME" and inp["market"]["price"] == 30.01
    assert inp["capital_structure"]["net_debt"] == 8.1e9 - 0.58e9
    assert inp["capital_structure"]["implied_pretax_cost_of_debt"] == round(0.569e9 / 8.1e9, 4)
    assert inp["ltm"]["revenue"] == {"value": 5.25e9, "through": "2026-10-02"}
    assert inp["rates"] == {"risk_free_10y": 0.051, "risk_free_source": "FRED",
                            "equity_risk_premium": 0.055,
                            "equity_risk_premium_source": "Forge standard assumption (market range 5.0-6.0%)"}


def test_the_model_never_sees_the_price_target_or_the_deterministic_fair_value(tmp_path):
    import json

    row = research_row("ACME", last=30.01)
    row["dcf"].update({"fair_value": 41.91, "upside_pct": 39.6, "caveats": ["fair value 41.91"]})
    inp, _ = _build(tmp_path, row=row)
    blob = json.dumps(inp)
    assert "41.91" not in blob and "32.27" not in blob and "39.6" not in blob
    assert "target" not in blob.lower()


def test_fatal_data_problems_cost_nothing(tmp_path):
    assert any("complete fiscal year" in f for f in _build(tmp_path, statements=_statements(years=2))[1])
    assert any("stale filer" in f for f in _build(tmp_path, today=date(2028, 6, 1))[1])
    assert any("no current price" in f
               for f in _build(tmp_path, fund={**FUND, "current_price": None},
                               row={**research_row("ACME"), "last": None})[1])
    assert any("no shares" in f for f in _build(tmp_path, fund={**FUND, "shares_outstanding": 0})[1])
    assert any("disagrees" in f for f in _build(tmp_path, statements=_statements(rev_last=9e9))[1])


def test_multi_class_share_count_comes_from_market_cap_not_one_class(tmp_path):
    """A dual-class company: the cache says 5,888M (one class); market cap /
    price says 12,416M, and the SEC's diluted count agrees with that."""
    dual = {**FUND, "shares_outstanding": 5.888e9, "market_cap": 3.6723e12, "current_price": 295.77}
    st = _statements()
    st["shares"] = {"diluted_shares": [13.2e9, 12.7e9, 12.4e9, 12.3e9, 12.23e9]}
    shares, source, notes = inputs_mod.resolve_shares(dual, st)
    assert round(shares / 1e6) == 12416 and "all share classes" in source
    assert len(notes) == 1 and "5,888M" in notes[0] and "matches the SEC filings" in notes[0]
    inp, fatal = _build(tmp_path, statements=st, fund=dual, row=research_row("DUAL", last=295.77))
    assert fatal == [] and round(inp["market"]["shares_outstanding"] / 1e6) == 12416
    assert any("one share class" in n for n in inp["data_quality_notes"])


def test_single_class_share_count_is_left_alone_and_a_split_is_flagged():
    st = _statements()
    st["shares"] = {"diluted_shares": [6.0e8, 6.0e8, 5.95e8]}
    ok = {**FUND, "market_cap": 593e6 * 30.4}
    assert inputs_mod.resolve_shares(ok, st) == (593e6, "fundamentals cache shares outstanding", [])
    assert inputs_mod.resolve_shares({"market_cap": 1e9, "current_price": 10.0}, {})[:2] == (1e8, "market cap / price")
    assert inputs_mod.resolve_shares({}, {})[0] is None
    st["shares"] = {"diluted_shares": [1.48e8]}                      # pre 4-for-1 split
    _, _, notes = inputs_mod.resolve_shares(ok, st)
    assert len(notes) == 1 and "split" in notes[0]


def test_a_bad_market_cap_does_not_override_a_share_count_the_sec_confirms():
    """WAT: cached shares equal the SEC cover page; the cached
    market cap is doubled. Market cap / price must NOT win just for disagreeing."""
    wat = {"shares_outstanding": 98_285_634, "market_cap": 90.5e9, "current_price": 460.4}
    st = {"shares": {"diluted_shares": [59.8e6, 59.6e6], "cover_page_shares_outstanding":
                     {"as_of": "2026-10-30", "value": 98_285_634}}}
    shares, source, notes = inputs_mod.resolve_shares(wat, st)
    assert shares == 98_285_634 and source == "fundamentals cache shares outstanding"
    assert "bad market cap" in notes[0]


def test_share_counts_that_nothing_corroborates_skip_the_name_for_free(tmp_path):
    odd = {**FUND, "shares_outstanding": 100e6, "market_cap": 300e6 * 30.4}
    st = _statements()
    st["shares"] = {"diluted_shares": [200e6]}                       # agrees with neither
    assert inputs_mod.resolve_shares(odd, st)[0] is None
    assert inputs_mod.resolve_shares(odd, {})[0] is None            # no SEC count at all
    assert any("no shares" in f for f in _build(tmp_path, statements=st, fund=odd)[1])


def test_beta_is_bounded_so_capm_stays_inside_the_wacc_gate(tmp_path):
    """NVDA: beta 2.25 → 17.5% cost of equity → every scenario
    fails the 16% WACC gate, after the run has been paid for."""
    from tests.deep_dcf_helpers import ACCEPT
    s = make_settings(tmp_path, accept=dict(ACCEPT))
    beta, raw, notes = inputs_mod.bounded_beta({"beta_published": 2.25}, 0.051, s)
    assert raw == 2.25 and beta == 1.70 and "2.25" in notes[0]
    assert 0.051 + beta * 0.055 + 0.015 <= 0.16                      # base + a bear point still passes
    assert inputs_mod.bounded_beta({"beta_published": 0.30}, 0.051, s)[0] == 0.70
    assert inputs_mod.bounded_beta({"beta_published": 1.24}, 0.051, s) == (1.24, 1.24, [])
    assert inputs_mod.bounded_beta({}, 0.051, s)[0] == 1.0           # missing: market beta, noted
    # a higher risk-free rate lowers the ceiling so the gate still holds
    hi = inputs_mod.bounded_beta({"beta_published": 3.0}, 0.07, s)[0]
    assert hi < 1.70 and 0.07 + hi * 0.055 + 0.015 <= 0.16 + 1e-9
    inp, _ = _build(tmp_path, fund={**FUND, "beta_published": 2.25})
    assert inp["market"]["beta"] == 1.70 and inp["market"]["beta_published"] == 2.25


def test_price_sources_that_disagree_skip_the_name(tmp_path):
    # research has the post-split price, the cache (and its share count) the pre-split one
    fatal = _build(tmp_path, row=research_row("ACME", last=15.2))[1]
    assert any("possible split or stale cache" in f for f in fatal)
    assert _build(tmp_path, row=research_row("ACME", last=31.5))[1] == []     # ordinary daily move


def test_statement_total_beats_a_footnote_tag_and_ltm_uses_one_tag():
    """CRM: DepreciationDepletionAndAmortization is a PP&E
    footnote (1,200); the cash-flow line DepreciationAndAmortization is 3,631."""
    facts = _facts({
        "Revenues": _annual([1, 2, 3, 4, 5]),
        "DepreciationDepletionAndAmortization": _annual([678, 903, 1100, 1000, 1200]) + [
            _fact(700, "2026-10-02", "2026-04-04", form="10-Q"),
            _fact(600, "2025-09-26", "2025-03-29", form="10-Q")],
        "DepreciationAndAmortization": _annual([3000, 3200, 3400, 3500, 3631]) + [
            _fact(1900, "2026-10-02", "2026-04-04", form="10-Q"),
            _fact(1800, "2025-09-26", "2025-03-29", form="10-Q")],
    })
    st = edgar.extract_statements(facts)
    assert st["annual"]["depreciation_amortization"] == [3000, 3200, 3400, 3500, 3631]
    assert st["ltm"]["depreciation_amortization"]["value"] == 3631 + 1900 - 1800     # one tag, all legs
    assert st["conflicts"]["depreciation_amortization"]["used"] == "DepreciationAndAmortization"
    # a concept without the prefer-larger rule keeps priority order, and still reports the clash
    two = _facts({"Revenues": _annual([1, 2, 3, 4, 100]),
                  "RevenueFromContractWithCustomerExcludingAssessedTax": _annual([1, 2, 3, 4, 60])})
    assert edgar.extract_statements(two)["annual"]["revenue"][-1] == 100
    assert "revenue" in edgar.extract_statements(two)["conflicts"]


def test_older_years_and_split_boundaries_are_flagged(tmp_path):
    """APP / NVDA: a 10-K restates three years; the two before
    them keep an old basis, and a 10:1 split sits in the share history."""
    rev = _annual([2793, 2817], filed="2024-02-20") + _annual([0, 0, 1842, 3224, 5481])[2:]
    facts = _facts({"Revenues": rev,
                    ("WeightedAverageNumberOfDilutedSharesOutstanding", "shares"):
                        _annual([2535, 2507, 25070, 24940, 24800])})
    st = edgar.extract_statements(facts)
    assert st["basis"]["years_from_earlier_filings"] == [FY[0][1], FY[1][1]]
    assert st["basis"]["share_count_breaks"] == [FY[2][1]]
    full = _statements()
    full["basis"] = st["basis"]
    notes = " | ".join(_build(tmp_path, statements=full)[0]["data_quality_notes"])
    assert "EARLIER 10-Ks" in notes and "stock split" in notes


def test_untagged_operating_income_is_derived_and_untagged_capex_is_a_note(tmp_path):
    st = _statements()
    st["annual"].update(operating_income=[None] * 5, gross_profit=[5e8] * 5, sga=[3e8] * 5,
                        research_development=[5e7, 5e7, 5e7, None, None], capex=[1e7, 1e7, 1e7, 1e7, None])
    inp, fatal = _build(tmp_path, statements=st)
    assert fatal == []
    assert inp["annual"]["income_and_cash_flow"]["operating_income"] == [1.5e8, 1.5e8, 1.5e8, 2e8, 2e8]
    notes = " | ".join(inp["data_quality_notes"])
    assert "derived as gross profit" in notes and "capex not tagged" in notes
    bare = _statements()
    bare["annual"]["operating_income"] = [None] * 5                  # nothing to derive it from
    assert any("complete fiscal year" in f for f in _build(tmp_path, statements=bare)[1])


def test_revenue_check_tolerates_the_cache_being_one_fiscal_year_off(tmp_path):
    # XBRL latest FY = 3.935B matches the SECOND entry of the cache's series
    assert _build(tmp_path, statements=_statements(rev_last=3.935e9))[1] == []


def test_missing_ltm_is_a_note_not_a_failure(tmp_path):
    st = _statements()
    st["ltm"] = {}
    inp, fatal = _build(tmp_path, statements=st)
    assert fatal == [] and inp["ltm"] is None
    assert any("latest fiscal year" in n for n in inp["data_quality_notes"])


# ── filers with no operating-income line (BMY, BIIB) ────────────────────────

def _pharma_facts(with_oi=False):
    pre_tag = "IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest"
    tags = {
        "Revenues": _annual([100, 110, 120, 130, 140]),
        pre_tag: _annual([20, 22, -5, 26, 28]),
        "InterestExpense": _annual([2, 2, 3, 3, 3]),
        "NetCashProvidedByUsedInOperatingActivities": _annual([30, 31, 32, 33, 34]),
        "PaymentsToAcquirePropertyPlantAndEquipment": _annual([5, 5, 5, 5, 5]),
    }
    if with_oi:
        tags["OperatingIncomeLoss"] = _annual([25, 26, 27, 28, 29])
    return _facts(tags)


def test_missing_operating_income_is_derived_from_pretax_plus_interest():
    st = edgar.extract_statements(_pharma_facts())
    assert st["annual"]["operating_income"] == [22, 24, -2, 29, 31]
    assert st["derived"]["operating_income"] == [e for _, e in FY]
    assert "derived: pretax_income + interest_expense" in st["tags"]["operating_income"]
    assert inputs_mod.complete_years(st) == 5


def test_a_tagged_operating_income_is_never_replaced():
    st = edgar.extract_statements(_pharma_facts(with_oi=True))
    assert st["annual"]["operating_income"] == [25, 26, 27, 28, 29]
    assert "derived" not in st


def test_the_model_is_told_which_years_are_derived(tmp_path):
    st = edgar.extract_statements(_pharma_facts())
    st["ltm"] = {"revenue": {"value": 5.25e9, "through": "2026-10-02"}}
    inp, _ = _build(tmp_path, statements=st)
    assert any("DERIVED as pre-tax income + interest expense" in n for n in inp["data_quality_notes"])
