"""Dividend term in the short-put POP model.

A dividend lowers the forward price, so P(S_T > K) — the seller's win
probability — must be LOWER on a payer than on a non-payer, all else equal.
Also pins the defensive yield normalization (yfinance fraction vs percent)."""
from strategies.cash_secured_put import (_bs_pop_short_put, _norm_div_yield,
                                          calculate_csp_metrics)


def test_dividend_lowers_short_put_pop():
    base = dict(stock_price=100.0, strike=90.0, dte=35, iv=0.40)
    pop_no_div = _bs_pop_short_put(**base, q=0.0)
    pop_div = _bs_pop_short_put(**base, q=0.04)
    assert pop_div < pop_no_div
    assert pop_no_div - pop_div < 0.02   # effect is real but small


def test_yield_normalization():
    assert _norm_div_yield(None) == 0.0
    assert _norm_div_yield(float("nan")) == 0.0
    assert _norm_div_yield(-0.01) == 0.0
    assert _norm_div_yield(0.021) == 0.021          # fraction form kept
    assert _norm_div_yield(2.1) == 0.021            # percent form corrected
    assert _norm_div_yield(55.0) == 0.12            # absurd input clamped


def test_metrics_accepts_and_uses_dividend_yield():
    common = dict(stock_price=100.0, strike=90.0, bid=1.0, ask=1.1,
                  dte=35, open_interest=500, iv=0.40)
    m0 = calculate_csp_metrics(**common, dividend_yield=None)
    m4 = calculate_csp_metrics(**common, dividend_yield=0.04)
    assert m4["pop_estimate"] < m0["pop_estimate"]
    # EV must also see the lower forward (bigger expected assignment loss)
    ev0 = m0.get("expected_value") or m0.get("ev_per_contract")
    ev4 = m4.get("expected_value") or m4.get("ev_per_contract")
    if ev0 is not None and ev4 is not None:
        assert ev4 < ev0
