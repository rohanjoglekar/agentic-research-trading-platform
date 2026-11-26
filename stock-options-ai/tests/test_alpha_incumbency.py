"""The alpha book must not churn names that are still good enough to hold.

183 of 321 positions (Jul–Nov 2026) closed within 3 days of opening, by two routes:
  * ranking — every slot was re-picked from scratch each run; ~140 names qualify
    for 22 slots and conviction is dense at the cut, so names there swapped in and
    out on every research rebuild (HAS: bought 11:45 ET, sold 14:45 ET 2026-11-16,
    rated strong/buy throughout);
  * entry/exit mismatch — a name the sell rules reject could still be bought, and
    was sold on the next run (NXPI 2026-11-16 and CPAY 2026-11-18, both bought with
    a trusted negative DCF and sold three hours later as "DCF turned negative").
Three rules fix it:
  * band   — a held name keeps its slot while it ranks within hold_rank_ratio ×
             the satellite size; a new name still has to make the satellite size;
  * lock   — a name held under sell_min_hold_days is kept whatever its rank, but
             stop-loss and broken-thesis exits still sell it;
  * entry  — never buy a name research_exit_reason rejects (kept in step with
             decide_sells by test_the_entry_rule_agrees_with_the_sell_rules).
"""
import datetime as dt

from strategies.alpha_portfolio import RankedName, build_target
from trading import alpha_positions
from trading.alpha_runner import apply_sells, diff_orders, locked_names

CAPITAL = 1_000_000.0
SECTORS = ["Tech", "Health", "Energy", "Retail", "Media", "Industrials", "Utilities", "Materials"]
CFG = {
    "satellite_max_names": 22, "satellite_min_score": 55, "watch_min_score": 68,
    "eligible_tiers": ("strong", "buy", "watch"), "weight_scheme": "conviction",
    "dcf_tilt": 0.5, "max_position_weight": 0.08, "max_sector_weight": 0.30,
    "core_weight": 0.12, "core_etfs": {"VOO": 0.6, "QQQ": 0.4}, "reserve_etf": "BIL",
    "reserve_floor": 0.03, "reserve_ceiling": 0.18, "cash_buffer": 0.01,
    "hold_rank_ratio": 1.5, "drift_band": 0.02, "min_trade_usd": 50.0,
}


def _names(n=40, **over):
    """N00 … N39, strictly descending score, spread across sectors so the
    per-sector name cap never decides who is picked."""
    out = []
    for i in range(n):
        sym = f"N{i:02d}"
        kw = dict(symbol=sym, score=90.0 - i * 0.5, sector=SECTORS[i % len(SECTORS)], tier="buy")
        kw.update(over.get(sym, {}))
        out.append(RankedName(**kw))
    return out


def _prices(names, missing=()):
    px = {n.symbol: 100.0 for n in names if n.symbol not in missing}
    px.update({"VOO": 500.0, "QQQ": 450.0, "BIL": 91.0})
    return px


TOP22 = [f"N{i:02d}" for i in range(22)]


def test_without_state_the_book_is_the_top_of_the_ranking():
    names = _names()
    stateless = build_target(names, _prices(names), CAPITAL, CFG)
    assert stateless.selected == TOP22
    empty = build_target(names, _prices(names), CAPITAL, CFG, held=set(), locked=set())
    assert empty.selected == TOP22 and empty.dollars == stateless.dollars


def test_a_held_name_inside_the_band_keeps_its_slot():
    names = _names()
    book = build_target(names, _prices(names), CAPITAL, CFG, held={"N25"})
    assert "N25" in book.selected and len(book.selected) == 22
    assert "N21" not in book.selected            # the marginal newcomer waits instead
    assert any("N25" in note for note in book.notes)


def test_a_held_name_below_the_band_is_replaced():
    names = _names()
    book = build_target(names, _prices(names), CAPITAL, CFG, held={"N33"})   # rank 34 of 33
    assert "N33" not in book.selected and book.selected == TOP22


def test_the_band_scales_with_the_book_size():
    names = _names()
    cfg = dict(CFG, satellite_max_names=12)      # band = 18
    assert "N17" in build_target(names, _prices(names), CAPITAL, cfg, held={"N17"}).selected
    assert "N18" not in build_target(names, _prices(names), CAPITAL, cfg, held={"N18"}).selected


def test_a_ranking_reshuffle_no_longer_sells_held_names():
    """The 2026-11-16 case: the book owns the top 22; a research rebuild lifts three
    outsiders above it. Stateless, three holdings are sold the same afternoon."""
    day1 = _names()
    held_dollars = build_target(day1, _prices(day1), CAPITAL, CFG).dollars
    holdings = {s: d for s, d in held_dollars.items() if s.startswith("N")}
    reshuffled = _names(**{s: {"score": 95.0} for s in ("N30", "N31", "N32")})

    stateless = build_target(reshuffled, _prices(reshuffled), CAPITAL, CFG)
    sold = [o["symbol"] for o in diff_orders(stateless, holdings, CAPITAL, CFG)
            if o["side"] == "sell" and o["target_usd"] == 0]
    assert len(sold) == 3                        # the churn this guards against

    banded = build_target(reshuffled, _prices(reshuffled), CAPITAL, CFG, held=set(holdings))
    assert set(banded.selected) == set(holdings)
    assert not [o for o in diff_orders(banded, holdings, CAPITAL, CFG) if o["target_usd"] == 0]


def test_a_locked_name_is_kept_whatever_its_rank():
    names = _names(**{"N39": {"score": 50.0}})   # below the 55 entry floor, rank last
    book = build_target(names, _prices(names), CAPITAL, CFG, held={"N39"}, locked={"N39"})
    assert "N39" in book.selected and len(book.selected) == 22
    assert book.dollars["N39"] > 0


def test_a_locked_name_without_a_price_cannot_be_sized():
    names = _names()
    book = build_target(names, _prices(names, missing={"N39"}), CAPITAL, CFG,
                        held={"N39"}, locked={"N39"})
    assert "N39" not in book.selected


def test_a_stop_loss_still_sells_a_locked_name():
    names = _names()
    book = build_target(names, _prices(names), CAPITAL, CFG, held={"N39"}, locked={"N39"})
    exited = apply_sells(book, {"N39": {"action": "exit", "reason": "stop-loss -16%",
                                        "target_usd": 0.0, "plpc": -0.16}})
    assert exited == {"N39"} and book.dollars["N39"] == 0.0
    orders = diff_orders(book, {"N39": 30_000.0}, CAPITAL, CFG, force={"N39"})
    assert [(o["symbol"], o["side"]) for o in orders if o["symbol"] == "N39"] == [("N39", "sell")]


def _store_with(ages: dict):
    now = dt.datetime.now(dt.timezone.utc)
    return {sym: ({"first_seen": (now - dt.timedelta(days=d)).isoformat()} if d is not None else {})
            for sym, d in ages.items()}


def test_locked_names_are_the_ones_inside_the_minimum_hold():
    store = _store_with({"NEW": 0, "FOUR": 4, "FIVE": 5, "OLD": 30, "UNKNOWN": None})
    assert locked_names(store, set(store), 5) == {"NEW", "FOUR"}
    assert locked_names(store, set(store), 0) == set()
    assert alpha_positions.holding_days(store, "UNKNOWN") is None   # unknown age → not locked


# ── never buy what the sell rules would exit on the next run ─────────────────

from strategies.alpha_portfolio import research_exit_reason  # noqa: E402
from trading.alpha_runner import decide_sells  # noqa: E402

SELL_CFG = {"thesis_exit_tiers": ("pass", "avoid"), "thesis_exit_min_score": 45,
            "dcf_exhausted_upside": 5.0, "stop_loss_pct": 0.15, "take_profit_pct": 0.30,
            "take_profit_trim": 0.5, "sell_min_hold_days": 5}


def test_a_name_with_a_trusted_negative_dcf_is_not_bought():
    """NXPI 2026-11-16: strong 72, trusted DCF −63% → bought 11:45 ET, sold 14:45 ET."""
    names = _names(**{"N00": {"dcf_trusted": True, "dcf_upside": -63.0}})
    book = build_target(names, _prices(names), CAPITAL, dict(CFG, **SELL_CFG))
    assert "N00" not in book.selected and len(book.selected) == 22


def test_not_even_a_shortlist_leader_or_a_locked_name():
    over = {"N00": {"dcf_trusted": True, "dcf_upside": 2.0, "shortlisted": True},
            "N01": {"dcf_trusted": True, "dcf_upside": -10.0}}
    names = _names(**over)
    book = build_target(names, _prices(names), CAPITAL, dict(CFG, **SELL_CFG),
                        held={"N01"}, locked={"N01"})
    assert "N00" not in book.selected and "N01" not in book.selected


def test_an_untrusted_or_missing_dcf_does_not_block_a_buy():
    names = _names(**{"N00": {"dcf_trusted": False, "dcf_upside": -80.0}})
    assert "N00" in build_target(names, _prices(names), CAPITAL, dict(CFG, **SELL_CFG)).selected
    assert RankedName(symbol="X", score=70).dcf_trusted is False      # no DCF → nothing to act on


def test_the_entry_rule_agrees_with_the_sell_rules():
    """research_exit_reason must reject exactly what decide_sells exits on research,
    for every tier/score/DCF combination, at every dcf_exhausted setting the governor
    may choose. A stop-loss/take-profit never fires here (flat P/L, fresh position)."""
    cases = 0
    for exhausted in (-5.0, 0.0, 5.0, 15.0):
        cfg = dict(SELL_CFG, dcf_exhausted_upside=exhausted)
        for tier in ("strong", "buy", "watch", "pass", "avoid", None):
            for score in (30.0, 44.9, 45.0, 60.0):
                for trusted in (True, False):
                    for up in (-40.0, -0.1, 0.0, 3.0, 5.0, 10.0, 40.0):
                        n = RankedName(symbol="X", score=score, tier=tier,
                                       dcf_trusted=trusted, dcf_upside=up)
                        pos = [{"symbol": "X", "market_value": 25_000.0, "avg_entry_price": 100.0,
                                "current_price": 100.0, "unrealized_plpc": 0.0, "qty": 250.0}]
                        sold = "X" in decide_sells(pos, {"X": n}, cfg, {}, set())
                        assert sold == bool(research_exit_reason(n, cfg)), (exhausted, tier, score, trusted, up)
                        cases += 1
    assert cases == 4 * 6 * 4 * 2 * 7
