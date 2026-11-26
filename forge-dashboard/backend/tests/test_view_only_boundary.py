"""The public gateway never gets the owner's account data (viewonly.py).

A page that hides the brokerage account is not a boundary, because the address
behind it still answers. These tests pin the boundary from three sides: the
path policy, the routes that actually exist, and what the shared payloads
contain once scrubbed.
"""
import json
import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import viewonly  # noqa: E402

PUBLIC = {"X-Forge-View-Only": "1"}

# The owner's account, by address.
ACCOUNT_PATHS = [
    "/api/robinhood/overview", "/api/robinhood/holdings", "/api/robinhood/orders",
    "/api/robinhood/watchlists", "/api/robinhood/risk", "/api/robinhood/risk/advanced",
    "/api/robinhood/risk/report", "/api/robinhood/options", "/api/robinhood/benchmark",
    "/api/robinhood/verdicts", "/api/robinhood/symbol/AAA/analyze", "/api/robinhood/ask",
    "/api/stock-options/owned", "/api/stock-options/my-options",
    "/api/stock-options/covered-calls", "/api/stock-options/owned-puts",
    "/api/ingest/robinhood", "/api/status",
]
# Research output: the only addresses under those namespaces the public may read.
RESEARCH_PATHS = [
    "/api/robinhood/research", "/api/robinhood/research/history",
    "/api/robinhood/factor-performance", "/api/robinhood/deep-dcf",
    "/api/robinhood/deep-dcf/BBB/sheet", "/api/robinhood/deep-dcf/BBB/model.xlsx",
    "/api/robinhood/chart/AAA", "/api/robinhood/symbol/AAA", "/api/robinhood/symbol/BRK.B",
    "/api/stock-options/scan",
]


# ─── the path policy ─────────────────────────────────────────────────────────

@pytest.mark.parametrize("path", ACCOUNT_PATHS)
def test_account_paths_are_closed(path):
    assert viewonly.allowed(path) is False


@pytest.mark.parametrize("path", RESEARCH_PATHS)
def test_research_paths_stay_open(path):
    assert viewonly.allowed(path) is True


@pytest.mark.parametrize("path", [
    "/api/trader", "/api/trader/analytics", "/api/alpha/portfolio", "/api/landing", "/api/system",
    "/api/btc-lab", "/api/kalshi", "/api/health", "/api/concord/overview",
])
def test_paper_trading_and_platform_paths_are_untouched(path):
    assert viewonly.allowed(path) is True


@pytest.mark.parametrize("path", [
    "/api/robinhood/something-added-later",         # closed by default
    "/api/robinhood", "/api/robinhood/",
    "/api/stock-options/anything-new",
    "/api/robinhood/holdings/",                     # trailing slash
    "/api//robinhood///holdings",                   # doubled slashes
    "/API/Robinhood/Holdings",                      # case
    "/api/robinhood/symbol/AAA/anything",          # deeper than the open path
    "/api/robinhood/research/../holdings",
    "/api/robinhood/symbol/TOOLONGSYM1",            # not a symbol
])
def test_nothing_gets_through_by_spelling_the_path_differently(path):
    assert viewonly.allowed(path) is False


def test_every_route_under_a_closed_namespace_has_been_decided(api_server):
    """A new route under these namespaces is private until it is added to the
    open list on purpose. This pins the open list to the routes that exist."""
    opened = set()
    for r in api_server.app.routes:
        path = getattr(r, "path", "")
        if not any(path.startswith(ns) for ns in viewonly._CLOSED):
            continue
        concrete = re.sub(r"\{[^}]+\}", "AAA", path)
        if viewonly.allowed(concrete):
            opened.add(path)
    assert opened == {
        "/api/robinhood/research", "/api/robinhood/research/history",
        "/api/robinhood/factor-performance", "/api/robinhood/deep-dcf",
        "/api/robinhood/deep-dcf/{sym}/sheet", "/api/robinhood/deep-dcf/{sym}/model.xlsx",
        "/api/robinhood/chart/{sym}", "/api/robinhood/symbol/{sym}", "/api/stock-options/scan",
    }


# ─── the gateway, end to end ─────────────────────────────────────────────────

@pytest.fixture
def client(api_server):
    from fastapi.testclient import TestClient

    return TestClient(api_server.app), api_server


def _never(*_a, **_k):
    raise AssertionError("an account adapter ran for a public request")


def test_public_requests_never_reach_an_account_adapter(client, monkeypatch):
    c, main = client
    for mod, fn in ((main.robinhood, "get_overview"), (main.robinhood, "get_holdings"),
                    (main.robinhood, "get_orders"), (main.robinhood, "get_watchlists"),
                    (main.robinhood, "get_risk"), (main.robinhood, "get_options"),
                    (main.rh_portfolio, "get_risk_advanced"), (main.rh_portfolio, "benchmark_curve"),
                    (main.rh_ai, "held_verdicts"), (main.rh_ai, "cached_portfolio_report"),
                    (main.rh_ai, "cached_symbol_analysis"), (main.stock_options, "get_owned"),
                    (main.stock_options, "get_my_options"), (main.stock_options, "get_covered_calls"),
                    (main.stock_options, "get_owned_puts")):
        monkeypatch.setattr(mod, fn, _never)
    for path in ACCOUNT_PATHS:
        r = c.get(path, headers=PUBLIC)
        assert r.status_code == 404 and r.json() == {"detail": "Not Found"}, path


@pytest.mark.parametrize("path", [
    "/api/robinhood/%68oldings",                    # an encoded letter
    "/api/robinhood/holdings%2F", "/api/robinhood%2Fholdings",
    "/api/robinhood/symbol/A%23/analyze",           # "#" would cut a parsed URL short
    "/api/robinhood/symbol/A%3F/analyze",           # so would "?"
    "/api/robinhood/research/%2E%2E/holdings",
    "/api/stock-options/scan/..%2Fowned",
    "/api/robinhood/holdings?x=/api/landing",
    "/api/robinhood/holdings/",
])
def test_encoded_and_odd_paths_do_not_reach_the_account(client, monkeypatch, path):
    c, main = client
    for mod, fn in ((main.robinhood, "get_holdings"), (main.rh_ai, "cached_symbol_analysis"),
                    (main.stock_options, "get_owned"), (main.rh_analytics, "get_symbol")):
        monkeypatch.setattr(mod, fn, _never)
    r = c.get(path, headers=PUBLIC, follow_redirects=True)
    assert r.status_code in (400, 404), (path, r.status_code)
    assert "quantity" not in r.text and "holdings" not in r.text


def test_the_owner_still_gets_his_account(client, monkeypatch):
    c, main = client
    monkeypatch.setattr(main.robinhood, "get_holdings", lambda: {"holdings": [{"symbol": "AAA", "quantity": 12}]})
    monkeypatch.setattr(main.rh_ai, "held_verdicts", lambda: {"verdicts": {"AAA": {"position_action": "ADD"}}})
    monkeypatch.setattr(main.stock_options, "get_owned", lambda: [{"symbol": "OWNB"}])
    assert c.get("/api/robinhood/holdings").json()["holdings"][0]["quantity"] == 12
    assert c.get("/api/robinhood/verdicts").json()["verdicts"]["AAA"]["position_action"] == "ADD"
    assert c.get("/api/stock-options/owned").json() == {"owned": [{"symbol": "OWNB"}]}
    # a header with any other value is not the gateway
    assert c.get("/api/robinhood/holdings", headers={"X-Forge-View-Only": "0"}).status_code == 200


RESEARCH = {
    "generated_at": "2026-11-21T06:00:00Z", "count": 2,
    "stocks": [
        {"symbol": "AAA", "name": "Alpha Corp", "score": 73, "tier": "strong", "held": True, "pl_pct": 3.7,
         "factors": {"ai": {"score": 99}}},
        {"symbol": "AAPL", "name": "Apple", "score": 60, "tier": "buy", "held": False, "pl_pct": None},
    ],
    "held_extra": [{"symbol": "OFFX", "tier": "pass", "score": 34, "outside_screen": True}],
    "shortlists": {"ai_buys": ["AAA"]},
}


def test_the_public_research_feed_does_not_say_what_he_holds(client, monkeypatch):
    c, main = client
    monkeypatch.setattr(main.rh_research, "get_research", lambda: json.loads(json.dumps(RESEARCH)))
    pub = c.get("/api/robinhood/research", headers=PUBLIC)
    body = pub.json()
    assert "held_extra" not in body and "OFFX" not in pub.text
    assert all("held" not in r and "pl_pct" not in r for r in body["stocks"])
    assert [r["symbol"] for r in body["stocks"]] == ["AAA", "AAPL"]          # the research itself is intact
    assert body["stocks"][0]["factors"] == {"ai": {"score": 99}} and body["shortlists"] == RESEARCH["shortlists"]
    own = c.get("/api/robinhood/research").json()
    assert own["stocks"][0]["held"] is True and own["stocks"][0]["pl_pct"] == 3.7 and own["held_extra"]


SYMBOL = {
    "symbol": "AAA", "as_of": "2026-11-20", "held": True,
    "position": {"quantity": 7.25, "avg_buy_price": 91.13, "market_value": 632.99, "total_usd": 46.88},
    "orders": [{"side": "buy", "quantity": 1, "price": 90.4}], "watchlists": ["Core names"],
    "quote": {"last": 87.31}, "fundamentals": {"sector": "Technology"}, "score": 73, "tier": "strong",
    "factors": {"ai": {"score": 99}}, "dcf": {"available": True},
}
VERDICT = {
    "verdict": "HOLD", "reason": "You hold 12 shares at $100, up 4%.", "generated_at": "2026-11-20T06:10:00Z",
    "portfolio_pct": 8.4, "pl_pct": 3.7, "position_action": "ADD", "position_note": "research strong (73) · 8% of book",
    "research_tier": "strong", "research_score": 73, "dcf_upside_pct": None,
    "ai_pct": 99.0, "ai_label": "AI read top 1%", "ai_thesis": "Entrenched enterprise distribution.",
    "ai_risk": "Capex may not earn its return.", "ai_sub": {"business_quality": 9}, "ai_kind": "company",
}


def test_the_public_symbol_page_carries_the_research_and_none_of_his_position(client, monkeypatch):
    """Belt and braces: the public path asks for an account-free answer, and if
    an adapter handed account fields back anyway the scrub still drops them."""
    c, main = client
    asked = []

    def get_symbol(sym, account=True):
        asked.append(account)
        return json.loads(json.dumps(SYMBOL))

    monkeypatch.setattr(main.rh_analytics, "get_symbol", get_symbol)
    monkeypatch.setattr(main.rh_ai_reads, "public_read", lambda sym: dict(VERDICT))
    monkeypatch.setattr(main.rh_ai, "cached_verdict", _never)      # the deep-dive verdict saw the position
    monkeypatch.setattr(main.deep_dcf, "for_symbol", lambda sym, view_only=False: None)
    pub = c.get("/api/robinhood/symbol/AAA", headers=PUBLIC)
    body = pub.json()
    assert asked == [False]
    for key in ("held", "position", "orders", "watchlists"):
        assert key not in body
    # no figure from the account survives anywhere in the response
    for secret in ("7.25", "91.13", "632.99", "46.88", "Core names", "You hold", "8% of book", "8.4"):
        assert secret not in pub.text, secret
    assert body["score"] == 73 and body["quote"] == {"last": 87.31} and body["factors"] == {"ai": {"score": 99}}
    # the weekly AI read is research, not account data: it stays
    assert body["ai_verdict"]["ai_thesis"] == "Entrenched enterprise distribution."
    assert set(body["ai_verdict"]) == {"ai_pct", "ai_label", "ai_thesis", "ai_risk", "ai_sub", "ai_kind"}
    monkeypatch.setattr(main.rh_ai, "cached_verdict", lambda sym, with_action=False: dict(VERDICT))
    own = c.get("/api/robinhood/symbol/AAA").json()
    assert asked == [False, True]
    assert own["position"]["quantity"] == 7.25 and own["ai_verdict"]["position_action"] == "ADD"


# ─── the symbol page, with the real adapter ──────────────────────────────────
# Removing fields is not enough if what remains still depends on the account.
# The account quote exists only for held or watched names, and the weekly AI
# read covers held companies the screen does not carry. So the public answer is
# built without opening the account at all, and these tests compare whole
# responses: same market data, different account, identical answer.

CLOSES = [100.0 + 0.5 * i for i in range(260)]
PRICES = {"dates": [f"d{i:03d}" for i in range(260)], "closes": CLOSES,
          "fetched_at": "2026-11-20T21:30:00+00:00"}
ACCOUNT = {
    "as_of": "2026-11-21T14:00:00Z",
    "positions": [{"symbol": "AAA", "quantity": "7.25", "avg_buy_price": "91.13"},
                  {"symbol": "OFFX", "quantity": "40", "avg_buy_price": "31.75"}],
    "quotes": {"AAA": {"last": 87.31, "prev_close": 86.02, "last_ext": 87.64},
               "OFFX": {"last": 33.4, "prev_close": 33.0, "last_ext": 33.62}},
    "orders": [{"symbol": "AAA", "side": "buy", "quantity": 1, "price": 90.4}],
    "watchlists": [{"name": "Core names", "items": ["AAA", "OFFX"]}],
}


@pytest.fixture
def market(client, monkeypatch):
    """AAA is in the screen, OFFX and ZZZZ are not. The account holds AAA and OFFX.
    All three have the same prices, so only the account can tell them apart."""
    import datetime as dt

    c, main = client
    from adapters import rh_ai_reads
    an = main.rh_analytics
    monkeypatch.setattr(an, "get_prices", lambda sym: json.loads(json.dumps(PRICES)))
    monkeypatch.setattr(an, "get_fundamentals", lambda sym, force=False: {"name": "Co", "sector": "Technology"})
    monkeypatch.setattr(an, "symbol_stats", lambda sym, spy=None: {})
    monkeypatch.setattr(an, "get_news", lambda sym, limit=12: [])
    now = dt.datetime.now(dt.timezone.utc).isoformat()
    read = {"usable": True, "raw": 70.0, "conviction": "high", "thesis": "A durable franchise.",
            "key_risk": "Customers cut spending.", "sub": {"business_quality": 8}, "read_at": now, "kind": "company"}
    monkeypatch.setattr(rh_ai_reads, "load_store", lambda: {"reads": {"AAA": dict(read), "OFFX": dict(read)}})
    monkeypatch.setattr(rh_ai_reads, "screen_symbols", lambda: ["AAA"])
    monkeypatch.setattr(main.deep_dcf, "for_symbol", lambda sym, view_only=False: None)
    monkeypatch.setattr(main.rh_ai, "cached_verdict", _never)

    def public(sym):
        r = c.get(f"/api/robinhood/symbol/{sym}", headers=PUBLIC)
        assert r.status_code == 200
        return r

    return main, public


def test_a_public_symbol_request_never_opens_the_account(market, monkeypatch):
    main, public = market
    monkeypatch.setattr(main.robinhood, "_snap", _never)
    body = public("AAA").json()
    # the price is the cached close for every name, never the account quote
    assert body["quote"] == {"last": CLOSES[-1], "prev_close": CLOSES[-2], "last_ext": None,
                             "day_pct": round((CLOSES[-1] / CLOSES[-2] - 1) * 100, 2)}
    # the public is told the day of the last close and the day of the read, and
    # never when a cache was filled or in what order the names were read
    assert body["as_of"] == PRICES["dates"][-1]
    assert PRICES["fetched_at"] not in json.dumps(body)
    assert len(body["ai_verdict"]["ai_read_at"]) == 10
    assert body["ai_verdict"]["ai_thesis"] == "A durable franchise."
    assert body["factors"]["ai"]["score"] is not None


def test_the_public_answer_is_the_same_whether_or_not_he_holds_the_stock(market, monkeypatch):
    main, public = market
    monkeypatch.setattr(main.robinhood, "_snap", lambda: json.loads(json.dumps(ACCOUNT)))
    holding = public("AAA")
    monkeypatch.setattr(main.robinhood, "_snap", lambda: {})
    not_holding = public("AAA")
    assert holding.json() == not_holding.json()
    for secret in ("87.31", "87.64", "7.25", "91.13", "Core names", "2026-11-21T14:00:00Z"):
        assert secret not in holding.text, secret
    # the comparison can see a leak: the owner's own answer does change with the account
    monkeypatch.setattr(main.robinhood, "_snap", lambda: json.loads(json.dumps(ACCOUNT)))
    own = main.rh_analytics.get_symbol("AAA")
    assert own["held"] is True and own["quote"]["last"] == 87.31 and own["quote"]["last_ext"] == 87.64
    assert own["orders"] and own["watchlists"] == ["Core names"] and own["position"]["quantity"] == 7.25
    assert own["as_of"] == "2026-11-21T14:00:00Z"


def test_a_company_outside_the_screen_gets_one_answer_held_or_not(market, client, monkeypatch):
    """OFFX is held: it has warm caches and a weekly AI read. ZZZZ is a stranger
    with neither. The public is answered for neither of them, with the same
    words, and nothing is read or fetched to find that out."""
    main, _ = market
    c, _ = client
    monkeypatch.setattr(main.robinhood, "_snap", lambda: json.loads(json.dumps(ACCOUNT)))
    # the owner's dashboard still gets the page and the read
    own = main.rh_analytics.get_symbol("OFFX")
    assert own["held"] is True and own["factors"]["ai"]["score"] is not None

    for fn in ("get_prices", "get_fundamentals", "symbol_stats", "get_news", "_get_chart5y", "_get_intraday"):
        monkeypatch.setattr(main.rh_analytics, fn, _never)
    held = c.get("/api/robinhood/symbol/OFFX", headers=PUBLIC)
    stranger = c.get("/api/robinhood/symbol/ZZZZ", headers=PUBLIC)
    assert held.status_code == stranger.status_code == 404
    assert held.json() == stranger.json() == {"error": "not in the research screen"}
    assert main.rh_ai_reads.public_read("OFFX") is None and main.rh_ai_reads.public_read("ZZZZ") is None
    for rng in ("1Y", "1D"):
        a = c.get(f"/api/robinhood/chart/OFFX?range={rng}", headers=PUBLIC).json()
        b = c.get(f"/api/robinhood/chart/ZZZZ?range={rng}", headers=PUBLIC).json()
        assert a.pop("symbol") == "OFFX" and b.pop("symbol") == "ZZZZ"
        assert a == b and a["closes"] == []


def test_scrubbing_copies_and_tolerates_odd_payloads():
    before = json.dumps(RESEARCH, sort_keys=True)
    viewonly.scrub_research(RESEARCH)
    viewonly.scrub_symbol(SYMBOL)
    assert json.dumps(RESEARCH, sort_keys=True) == before and SYMBOL["held"] is True      # originals untouched
    assert viewonly.scrub_research(None) is None and viewonly.scrub_research({"stocks": None}) == {"stocks": None}
    assert viewonly.scrub_symbol({"error": "invalid symbol"}) == {"error": "invalid symbol"}
    assert viewonly.scrub_symbol({"symbol": "X", "ai_verdict": {"verdict": "SELL", "pl_pct": 3}})["ai_verdict"] is None
    assert viewonly.scrub_symbol({"symbol": "X", "ai_verdict": None})["ai_verdict"] is None


def test_public_valuation_models_say_neither_why_nor_in_what_order_they_were_built(tmp_path, monkeypatch):
    """BBB was built on the public schedule and OWNA for the account. The public
    index holds BBB alone, and of BBB the public gets no reason and no timestamp."""
    import appconfig as config
    from adapters import deep_dcf
    monkeypatch.setattr(config, "DEEP_DCF_DIR", tmp_path)
    bbb = {"symbol": "BBB", "as_of": "2026-11-20", "price": 30.0, "scenarios": {}, "file": "models/BBB/x.xlsx",
           "generated_at": "2026-11-20T22:04:11+00:00", "triggers": ["T1: 10-Q filed 2026-10-29"], "cost_usd": 0.05}
    owna = {**bbb, "symbol": "OWNA", "generated_at": "2026-11-20T22:01:02+00:00",
            "triggers": ["T3: sell decision pending on the account"]}
    (tmp_path / "latest.json").write_text(json.dumps({"models": {"BBB": bbb, "OWNA": owna}}))
    (tmp_path / "public.json").write_text(json.dumps({"models": {"BBB": bbb}}))
    models = deep_dcf.get_models(view_only=True)["models"]
    assert [m["symbol"] for m in models] == ["BBB"]
    for private in ("triggers", "cost_usd", "generated_at"):
        assert private not in models[0], private
    assert deep_dcf.for_symbol("BBB", view_only=True).get("triggers") is None
    assert deep_dcf.for_symbol("OWNA", view_only=True) is None
    assert {m["symbol"] for m in deep_dcf.get_models()["models"]} == {"BBB", "OWNA"}   # the owner sees both
    assert deep_dcf.for_symbol("OWNA")["triggers"]
