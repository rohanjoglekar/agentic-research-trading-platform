"""Unit tests for the adaptive cash-reserve controller (trading/reserve.py).

Pure function, no I/O — exercises the three rules: regime-scaled target,
slow-release / fast-rebuild easing, and the absolute catastrophe floor.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config.settings import RESERVE
from trading.reserve import next_cash_floor, resolve_floor

CFG = RESERVE


def test_regime_targets_when_already_at_target():
    # If prev already equals the regime target, the floor holds steady.
    assert next_cash_floor(0.12, "calm", CFG) == 0.12
    assert next_cash_floor(0.16, "caution", CFG) == 0.16
    assert next_cash_floor(0.30, "extreme", CFG) == 0.30


def test_cold_start_adopts_target():
    # No prior floor → jump straight to the regime target.
    assert next_cash_floor(None, "calm", CFG) == 0.12
    assert next_cash_floor(None, "extreme", CFG) == 0.30


def test_slow_release_is_rate_limited():
    # calm target is 0.12, but from a 0.20 legacy floor we may only drop one
    # release step (0.02) per run — not snap to 0.12.
    assert next_cash_floor(0.20, "calm", CFG) == 0.18
    assert next_cash_floor(0.18, "calm", CFG) == 0.16


def test_fast_rebuild_outpaces_release():
    # Danger arrives (calm→extreme, target 0.30): the floor rises by the bigger
    # rebuild step (0.06), faster than it would ever release.
    nxt = next_cash_floor(0.12, "extreme", CFG)
    assert nxt == 0.18
    assert (nxt - 0.12) > CFG["max_release_step"]   # rebuild faster than release


def test_release_never_overshoots_target():
    # One step from 0.13 toward calm's 0.12 lands exactly on target, not below.
    assert next_cash_floor(0.13, "calm", CFG) == 0.12


def test_catastrophe_floor_is_absolute():
    # Even a pathological config can't push the enforced floor below catastrophe.
    crazy = {**CFG, "base_floor_by_regime": {"calm": 0.0}, "catastrophe_floor": 0.05}
    assert next_cash_floor(0.05, "calm", crazy) == 0.05
    assert next_cash_floor(None, "calm", crazy) == 0.05


def test_unknown_regime_uses_default():
    assert next_cash_floor(None, "nonsense", CFG) == CFG["default_floor"]


def test_resolve_floor_seeds_from_legacy_static():
    # First run after deploy: state has no cash_floor_pct → seed from the old
    # 20% static floor and ease ONE step toward calm's 0.12 (= 0.18), not snap.
    out = resolve_floor({}, "calm", CFG, legacy_static=0.20)
    assert out["adaptive"] is True
    assert out["floor_pct"] == 0.18
    assert out["target_pct"] == 0.12
    assert out["easing"] == "release"


def test_resolve_floor_disabled_falls_back_to_static():
    out = resolve_floor({"cash_floor_pct": 0.11}, "calm",
                        {**CFG, "enabled": False}, legacy_static=0.20)
    assert out["adaptive"] is False
    assert out["floor_pct"] == 0.20


def test_full_trajectory_release_then_shock():
    # Simulate the floor gliding down over several calm runs, then a vol shock
    # that rebuilds it quickly — proves "slow release, fast rebuild" end to end.
    state = {}
    seq = []
    for _ in range(6):                       # 6 calm runs from the 20% legacy floor
        out = resolve_floor(state, "calm", CFG, legacy_static=0.20)
        state["cash_floor_pct"] = out["floor_pct"]
        seq.append(out["floor_pct"])
    assert seq == [0.18, 0.16, 0.14, 0.12, 0.12, 0.12]   # eases 20→12 over 4 runs, then holds

    # Now VIX goes extreme: floor rebuilds toward 0.30 at the faster step.
    shock = []
    for _ in range(4):
        out = resolve_floor(state, "extreme", CFG, legacy_static=0.20)
        state["cash_floor_pct"] = out["floor_pct"]
        shock.append(out["floor_pct"])
    assert shock == [0.18, 0.24, 0.30, 0.30]             # 12→30 in 3 runs (6%/run)


if __name__ == "__main__":
    import traceback
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    passed = 0
    for fn in fns:
        try:
            fn()
            print(f"  ok  {fn.__name__}")
            passed += 1
        except Exception:
            print(f"  FAIL {fn.__name__}")
            traceback.print_exc()
    print(f"\n{passed}/{len(fns)} passed")
    sys.exit(0 if passed == len(fns) else 1)
