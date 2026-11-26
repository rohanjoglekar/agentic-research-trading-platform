"""Persistent store for the user's open option positions (sold puts, sold calls,
long calls, long puts).

Positions are entered by hand and kept in a local JSON file that is never
committed. They feed the status and exit/roll recommendations in the daily
digest.

Schema:
    {
      "options": [
        {
          "id": "uuid-hex",
          "symbol": "XYZ",
          "option_type": "SHORT_PUT" | "SHORT_CALL" | "LONG_CALL" | "LONG_PUT",
          "contracts": 1,
          "strike": 50.0,
          "premium": 1.25,         # per share (so total credit = 125)
          "expiration": "2026-01-16",
          "opened_at": "2025-12-01T..."
        }
      ],
      "updated_at": "..."
    }
"""
from __future__ import annotations

import datetime
import json
import math
import uuid
from pathlib import Path
from typing import Optional

POSITIONS_PATH = Path(__file__).resolve().parents[1] / "my_options.json"

OPTION_TYPES = ("SHORT_PUT", "SHORT_CALL", "LONG_CALL", "LONG_PUT")


def _now_iso() -> str:
    return datetime.datetime.utcnow().isoformat() + "Z"


def load_options() -> list[dict]:
    if not POSITIONS_PATH.exists():
        return []
    try:
        data = json.loads(POSITIONS_PATH.read_text())
        return data.get("options", []) or []
    except Exception:
        return []


def save_options(options: list[dict]) -> None:
    POSITIONS_PATH.write_text(json.dumps(
        {"options": options, "updated_at": _now_iso()},
        indent=2,
    ))


def add_option(symbol: str, option_type: str, contracts: int,
               strike: float, premium: float, expiration: str) -> dict:
    if option_type not in OPTION_TYPES:
        raise ValueError(f"option_type must be one of {OPTION_TYPES}")
    rec = {
        "id": uuid.uuid4().hex[:12],
        "symbol": symbol.strip().upper(),
        "option_type": option_type,
        "contracts": int(contracts),
        "strike": float(strike),
        "premium": float(premium),
        "expiration": expiration.strip(),  # YYYY-MM-DD
        "opened_at": _now_iso(),
    }
    options = load_options()
    options.append(rec)
    save_options(options)
    return rec


def remove_option(option_id: str) -> bool:
    options = load_options()
    new = [o for o in options if o.get("id") != option_id]
    if len(new) < len(options):
        save_options(new)
        return True
    return False


def get_symbols() -> list[str]:
    return sorted({o["symbol"] for o in load_options() if o.get("symbol")})


# ─── Status / recommendation engine ─────────────────────────────────────────

def _days_to(exp: str) -> Optional[int]:
    try:
        d = datetime.date.fromisoformat(exp)
        return (d - datetime.date.today()).days
    except Exception:
        return None


def _fetch_quote(symbol: str) -> tuple[Optional[float], Optional[float]]:
    """Returns (current_underlying_price, current_option_mid_price_estimate).

    Uses AlpacaOptionsClient if available; falls back gracefully on errors so
    the dashboard never crashes. The option-mid uses intrinsic + a small time
    value heuristic when a live snapshot isn't available.
    """
    underlying = None
    try:
        from data.alpaca_options_client import AlpacaOptionsClient
        cli = AlpacaOptionsClient()
        underlying = cli.get_current_price(symbol)
    except Exception:
        return None, None
    return underlying, None


def evaluate(opt: dict) -> dict:
    """Return a status dict with current pricing + a recommendation.

    Keys: underlying_price, dte, intrinsic, max_profit, captured_pct_est,
    moneyness, action ('HOLD'|'CLOSE'|'ROLL'|'CONSIDER_CLOSE'|'AT_RISK'),
    note (one-line plain-english).
    """
    sym = opt["symbol"]
    strike = float(opt["strike"])
    premium = float(opt["premium"])
    contracts = int(opt["contracts"])
    typ = opt["option_type"]
    dte = _days_to(opt["expiration"])
    max_profit = premium * 100 * contracts  # total credit (for shorts)

    underlying, _ = _fetch_quote(sym)
    out = {
        "underlying_price": underlying,
        "dte": dte,
        "intrinsic": None,
        "max_profit": max_profit,
        "captured_pct_est": None,
        "moneyness": None,
        "action": "HOLD",
        "note": "",
    }
    if underlying is None or dte is None:
        out["note"] = "Couldn't fetch quote — re-check later."
        return out

    # Intrinsic value per share
    if typ in ("SHORT_PUT", "LONG_PUT"):
        intrinsic = max(0.0, strike - underlying)
        otm = underlying > strike
    else:  # CALLS
        intrinsic = max(0.0, underlying - strike)
        otm = underlying < strike
    out["intrinsic"] = intrinsic
    out["moneyness"] = "OTM" if otm else "ITM"

    # Time-value heuristic: linear decay of original premium scaled by sqrt(time
    # remaining / total). Crude but enough to flag winners.
    # We assume avg DTE on open ~30 days. Use that for the ratio cap.
    decay_floor = max(0.0, premium * math.sqrt(max(dte, 0) / 30.0))
    est_mid = max(intrinsic, decay_floor) if typ.startswith("SHORT") else max(intrinsic, decay_floor)

    if typ.startswith("SHORT"):
        # Profit captured (estimated) = 1 - (est_mid / premium)
        if premium > 0:
            captured = max(-1.0, min(1.0, 1.0 - est_mid / premium))
            out["captured_pct_est"] = round(captured * 100, 1)

        # Recommendations
        if captured >= 0.80:
            out["action"] = "CLOSE"
            out["note"] = f"≥80% of max profit captured (est). BTC and free the collateral."
        elif intrinsic > premium * 1.5 and dte > 7:
            out["action"] = "ROLL" if dte > 14 else "AT_RISK"
            if out["action"] == "ROLL":
                out["note"] = (f"ITM and premium ~{intrinsic/premium:.1f}× original. "
                               f"Roll out + further OTM to collect more credit.")
            else:
                out["note"] = (f"Deep ITM with {dte}d to expiry. Likely assignment — "
                               f"either let it assign or BTC at a loss.")
        elif dte <= 3:
            if otm:
                out["action"] = "CONSIDER_CLOSE"
                out["note"] = f"{dte}d left, still OTM. Let it expire or BTC for pennies."
            else:
                out["action"] = "AT_RISK"
                out["note"] = f"{dte}d left and ITM. Assignment imminent unless you BTC."
        elif dte <= 21 and captured >= 0.50:
            out["action"] = "CONSIDER_CLOSE"
            out["note"] = f"~{int(captured*100)}% captured with {dte}d left. Decent exit."
        else:
            out["note"] = f"{out['moneyness']}, {dte}d to expiry. Holding."
    else:
        # LONG options: profit when intrinsic > premium
        if premium > 0:
            captured = (intrinsic - premium) / premium
            out["captured_pct_est"] = round(captured * 100, 1)
        if intrinsic > premium * 2 and dte > 7:
            out["action"] = "CLOSE"
            out["note"] = f"~{captured*100:.0f}% gain. Lock in profit."
        elif dte <= 5 and intrinsic < premium * 0.5:
            out["action"] = "AT_RISK"
            out["note"] = f"{dte}d left, deep OTM. Likely loss — consider exit."
        else:
            out["note"] = f"{out['moneyness']}, {dte}d to expiry."
    return out


def evaluate_all() -> list[dict]:
    """Convenience: load all options + attach .status to each."""
    out = []
    for opt in load_options():
        s = dict(opt)
        try:
            s["status"] = evaluate(opt)
        except Exception as e:
            s["status"] = {"action": "HOLD", "note": f"eval error: {e}"}
        out.append(s)
    return out
