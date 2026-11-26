"""The put-selling replay (backtests/csp_universe) on hand-built trades.

The replay decides whether the options scan should change, so its account
arithmetic, sizing, gates and fill rules are checked like trading code.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backtests" / "csp_universe"))


def test_replay_arithmetic_sizing_gates_and_fills():
    import selfcheck
    selfcheck.main()


def test_replay_reads_the_live_rule_settings():
    """What the replay calls the live rules must be the trader's own numbers."""
    import replay as R
    from config.settings import AGENT, OPTIONS, RISK, TARGETS
    assert R.MODES["cut"] == {"stop": RISK["close_at_loss_multiplier"], "kill": AGENT["kill_switch_loss_mult"]}
    assert R.outcome.__defaults__[0] == RISK["close_at_profit_pct"]
    assert TARGETS["monthly_return_target"] * 100 in R.GATES
    assert R._QTY == RISK["qty_by_rating"] and OPTIONS["min_pop"] == 0.50
