"""Adaptive cash-reserve controller for the CSP wheel.

The wheel keeps a free-cash "floor" and stops opening new puts once cash hits
it. Historically that floor was a static 20% of equity — a permanent lock that
no code path ever deployed (closes/rolls ignore it, and there is no defensive
routine that spends it). So the 20% was inert capital, not a usable reserve.

This module makes the floor ADAPTIVE and GRADUAL instead — a small control law
with three rules:

  1. Regime-scaled target. The *target* floor is low in calm markets (deploy
     the reserve, harvest premium) and high in stress (hoard the cushion),
     driven by the same VIX regime the wheel already classifies.

  2. Slow release / fast rebuild. The *enforced* floor only eases toward its
     target a small step per run when LOWERING (so a single calm reading can't
     dump all the dry powder — "slowly release the lever"), but may rise faster
     when the regime worsens (rebuild the cushion quickly when danger arrives).

  3. Catastrophe floor. An absolute hard line (default 5%) the floor never
     crosses, regardless of regime or smoothing — the margin-survival minimum.

There is NO manual override: the floor is a pure function of the regime and the
prior floor, so the reserve releases and rebuilds itself. `next_cash_floor` is
pure (no I/O) and unit-tested in tests/test_reserve_floor.py.
"""
from __future__ import annotations


def next_cash_floor(prev_floor: float | None, regime: str, cfg: dict) -> float:
    """Compute this run's enforced free-cash floor (fraction of equity).

    Args:
        prev_floor: last run's enforced floor (fraction). None on a cold start
            with no persisted state — then we jump straight to the target
            (the caller seeds this with the legacy static floor so easing
            actually starts from the old behavior rather than snapping).
        regime: classify_regime()["regime"] — "calm"|"caution"|"paused"|"extreme".
        cfg: settings.RESERVE.

    Returns:
        The floor to enforce now, clamped to the catastrophe floor, having moved
        at most one release/rebuild step from prev_floor toward the regime target.
    """
    catastrophe = float(cfg["catastrophe_floor"])
    base = float(cfg["base_floor_by_regime"].get(regime, cfg["default_floor"]))
    target = max(base, catastrophe)

    if prev_floor is None:
        return round(target, 4)  # nothing to ease from — adopt the target directly

    prev = max(catastrophe, float(prev_floor))
    if target < prev:                                  # releasing capital — go SLOW
        nxt = max(target, prev - float(cfg["max_release_step"]))
    elif target > prev:                                # rebuilding reserve — quicker
        nxt = min(target, prev + float(cfg["max_rebuild_step"]))
    else:
        nxt = prev
    # Round to 0.01% — keeps the persisted smoothing state free of float dust.
    return round(max(catastrophe, nxt), 4)


def resolve_floor(state: dict, regime: str, cfg: dict, legacy_static: float) -> dict:
    """Advance the smoothed floor and return a status dict for state + dashboard.

    `state["cash_floor_pct"]` carries the smoothing across runs; on the very
    first run after deploy it's absent, so we seed from `legacy_static` (the old
    RISK["min_cash_buffer"]) and ease from there — the floor glides 20%→target
    over several runs instead of snapping.
    """
    if not cfg.get("enabled", True):
        floor = float(legacy_static)
        return {"floor_pct": floor, "target_pct": floor, "regime": regime,
                "catastrophe_pct": float(cfg["catastrophe_floor"]),
                "easing": "static", "adaptive": False}

    prev = state.get("cash_floor_pct")
    if prev is None:
        prev = float(legacy_static)
    floor = next_cash_floor(prev, regime, cfg)

    base = float(cfg["base_floor_by_regime"].get(regime, cfg["default_floor"]))
    target = max(base, float(cfg["catastrophe_floor"]))
    easing = "release" if floor < prev else "rebuild" if floor > prev else "steady"
    return {"floor_pct": round(floor, 4), "target_pct": round(target, 4),
            "regime": regime, "catastrophe_pct": float(cfg["catastrophe_floor"]),
            "easing": easing, "adaptive": True, "prev_pct": round(float(prev), 4)}
