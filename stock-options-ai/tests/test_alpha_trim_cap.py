"""Take-profit trim must actually bank profit — not get bought straight back.

A trim's reduced target is a ONE-RUN instruction. Without a persisted cap the
next run rebuilds the full conviction-weighted target, which is larger than the
freshly-halved position, so diff_orders issues a BUY the following day: the
spread is paid twice and the take-profit is undone. It fires whenever
target_weight - position/2 clears the drift band — i.e. any name at a >=4%
target, which the 8% per-name cap permits.
"""
import datetime as dt

from strategies.alpha_portfolio import TargetBook
from trading import alpha_positions
from trading.alpha_runner import apply_trim_caps, diff_orders

CFG = {"drift_band": 0.02, "min_trade_usd": 50.0, "trim_cap_days": 5}
EQUITY = 1_000_000.0


def _store(cap=None, age_days=0):
    """Store for CRM, optionally carrying a trim cap set `age_days` ago."""
    rec = {"first_seen": alpha_positions._now()}
    if cap is not None:
        ts = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=age_days)
        rec["trim_cap_usd"] = cap
        rec["trim_cap_at"] = ts.isoformat()
    return {"CRM": rec}


def _book(target_usd):
    return TargetBook(weights={"CRM": target_usd / EQUITY},
                      dollars={"CRM": target_usd})


def test_without_a_cap_the_next_run_buys_the_trim_back():
    """The bug this guards: 5% target, trimmed to 2.5% -> 2.5pp gap > 2% band."""
    book = _book(50_000.0)                       # conviction target 5%
    apply_trim_caps(book, _store(cap=None), CFG, EQUITY)
    orders = diff_orders(book, {"CRM": 25_000.0}, EQUITY, CFG)
    assert [o["side"] for o in orders] == ["buy"]
    assert orders[0]["notional"] == 25_000.0     # the whole trim, undone


def test_active_cap_blocks_the_rebuy():
    book = _book(50_000.0)
    capped = apply_trim_caps(book, _store(cap=25_000.0), CFG, EQUITY)
    assert capped == ["CRM"]
    assert book.dollars["CRM"] == 25_000.0
    assert book.weights["CRM"] == 0.025          # display weight follows
    assert diff_orders(book, {"CRM": 25_000.0}, EQUITY, CFG) == []


def test_cap_expires_and_the_book_may_size_up_again():
    book = _book(50_000.0)
    apply_trim_caps(book, _store(cap=25_000.0, age_days=6), CFG, EQUITY)
    assert book.dollars["CRM"] == 50_000.0       # past trim_cap_days -> released


def test_cap_never_raises_a_target_or_blocks_a_sell():
    # a cap above the current target must not inflate it
    book = _book(10_000.0)
    apply_trim_caps(book, _store(cap=25_000.0), CFG, EQUITY)
    assert book.dollars["CRM"] == 10_000.0
    # and an exit (target 0, forced) still liquidates with a cap on file
    book.dollars["CRM"] = 0.0
    orders = diff_orders(book, {"CRM": 25_000.0}, EQUITY, CFG, force={"CRM"})
    assert [o["side"] for o in orders] == ["sell"]
    assert orders[0]["target_usd"] == 0.0
