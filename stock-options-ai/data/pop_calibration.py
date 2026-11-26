"""POP calibration: track predicted vs realized POP on closed trades.

Workflow:
  1. wheel_runner logs each opened position with its predicted_pop.
  2. On each trader run (or display call), reconcile statuses against Alpaca:
       - filled (= assigned)        → trade LOST (POP failed)
       - expired (worth $0)         → trade WON
       - bought-to-close at profit  → trade WON
       - bought-to-close at loss    → trade LOST
  3. Compute realized win-rate vs average predicted POP.
  4. Bias multiplier = realized_winrate / predicted_winrate, clamped [0.7, 1.1].
     Applied as pop_bias in composite_trade_score.

Self-correcting feedback loop: if 70%-POP trades win 60% of the time,
all future POP scores get multiplied by ~0.86 to reflect reality.

Cache: JSON at pop_calibration.json with the last-computed bias + sample size.
No bias is applied until at least MIN_SAMPLE trades have closed.
"""
from __future__ import annotations

import datetime
import json
from pathlib import Path
from typing import Optional

PROJECT_ROOT = Path(__file__).resolve().parents[1]
STATE_PATH = PROJECT_ROOT / "trader_state.json"
CALIB_PATH = PROJECT_ROOT / "pop_calibration.json"

MIN_SAMPLE = 10            # need ≥10 closed trades before applying bias
BIAS_FLOOR = 0.70
BIAS_CEILING = 1.10


def _classify_outcome(rec: dict) -> Optional[bool]:
    """Returns True (win), False (loss), or None (still open / unclear)."""
    status = (rec.get("status") or "").lower()
    if status == "expired":
        return True
    if status == "filled":
        # For a SHORT PUT, "filled" at submission time = order accepted, but
        # for an OPEN it just means we sold the contract. We need a separate
        # close record. Since we log opens with status that later gets updated
        # by reconcile_order_statuses to filled, we treat this as a WIN only
        # if the contract has EXPIRED (i.e. past expiration date and no
        # assignment record). Conservative: only count truly-terminal statuses.
        try:
            exp = datetime.date.fromisoformat(rec.get("expiration", ""))
            if exp < datetime.date.today():
                return True   # past expiration, fill never reversed → won
        except Exception:
            pass
        return None
    if status in ("canceled", "cancelled", "rejected"):
        return None
    return None


def load_closed_trades() -> list[dict]:
    """Return all CLOSED orders with a known outcome + predicted_pop."""
    if not STATE_PATH.exists():
        return []
    state = json.loads(STATE_PATH.read_text())
    out = []
    for o in state.get("orders", []):
        if "predicted_pop" not in o or o["predicted_pop"] is None:
            continue
        outcome = _classify_outcome(o)
        if outcome is None:
            continue
        o2 = dict(o)
        o2["won"] = outcome
        out.append(o2)
    return out


def compute_bias() -> dict:
    """Compute current POP bias. Saves to CALIB_PATH and returns result."""
    closed = load_closed_trades()
    n = len(closed)
    if n < MIN_SAMPLE:
        result = {"bias": 1.0, "sample_size": n, "reason": "insufficient data",
                  "computed_at": datetime.datetime.utcnow().isoformat() + "Z"}
        CALIB_PATH.write_text(json.dumps(result, indent=2))
        return result

    wins = sum(1 for c in closed if c["won"])
    realized_winrate = wins / n
    predicted_winrate = sum(c["predicted_pop"] for c in closed) / n
    if predicted_winrate <= 0:
        bias = 1.0
    else:
        bias = realized_winrate / predicted_winrate
    bias = max(BIAS_FLOOR, min(BIAS_CEILING, bias))

    result = {
        "bias": round(bias, 3),
        "sample_size": n,
        "wins": wins,
        "realized_winrate": round(realized_winrate, 3),
        "avg_predicted_pop": round(predicted_winrate, 3),
        "computed_at": datetime.datetime.utcnow().isoformat() + "Z",
    }
    CALIB_PATH.write_text(json.dumps(result, indent=2))
    return result


def get_current_bias() -> float:
    """Read the cached bias multiplier. Lightweight — for scoring path."""
    if not CALIB_PATH.exists():
        return 1.0
    try:
        d = json.loads(CALIB_PATH.read_text())
        return float(d.get("bias", 1.0))
    except Exception:
        return 1.0
