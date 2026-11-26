"""Stock Options AI adapter — scan, owned positions, my options, CC, owned puts."""
from __future__ import annotations

import appconfig as config
from adapters import read_json


def get_scan() -> list[dict]:
    """Today's CSP scan with AI memos."""
    data = read_json(config.SCAN_PATH, default=[]) or []
    out = []
    for s in data:
        best = (s.get("strikes") or [{}])[0]
        out.append({
            "symbol": s.get("symbol"),
            "price": s.get("current_price"),
            "rating": s.get("best_rating"),
            "score": s.get("best_score"),
            "would_own": s.get("would_own_score"),
            # The ORDERING key — best contract score x (would-own/10) x sqrt(POP)
            # x analyst-consensus. The table sorts by this, so it must be visible:
            # a table ordered by a number it does not show reads as arbitrary.
            "rank_score": s.get("composite_rank_score"),
            "strike": best.get("Strike"),
            "expiration": best.get("Expiration"),
            "premium": best.get("Premium"),
            "monthly_pct": best.get("Monthly %"),
            "pop": best.get("POP"),
            "analysis": s.get("analysis"),
            "strikes": s.get("strikes") or [],
        })
    return out


def get_owned() -> list[dict]:
    """Owned underlying positions (for covered calls).

    The file is `{"positions": [...], "updated_at": ...}` — unwrap to the list.
    Also tolerate a bare list or a symbol-keyed dict.
    """
    raw = read_json(config.OWNED_PATH, default=[])
    if isinstance(raw, list):
        return raw
    if isinstance(raw, dict):
        if isinstance(raw.get("positions"), list):
            return raw["positions"]
        # symbol-keyed dict -> list of values
        vals = [v for v in raw.values() if isinstance(v, dict)]
        if vals:
            return vals
    return []


def get_my_options() -> list[dict]:
    """User-tracked options. Tries the evaluated version (with HOLD/ROLL/CLOSE
    recommendations) via the SOAI module; falls back to the raw JSON file."""
    # Try evaluated version first (richer)
    try:
        import sys
        if str(config.SOAI_ROOT) not in sys.path:
            sys.path.insert(0, str(config.SOAI_ROOT))
        from data.my_options import evaluate_all  # type: ignore
        records = evaluate_all()
        if records:
            return records
    except Exception:
        pass
    # Fallback: raw file
    raw = read_json(config.MY_OPTIONS_PATH, default=[])
    if isinstance(raw, dict):
        return raw.get("positions", [])
    return raw or []


def get_covered_calls() -> dict:
    """Covered-call analysis for owned stocks."""
    return read_json(config.CC_ANALYSIS_PATH, default={}) or {}


def get_owned_puts() -> dict:
    """Owned puts AI analysis."""
    return read_json(config.OWNED_PUTS_PATH, default={}) or {}
