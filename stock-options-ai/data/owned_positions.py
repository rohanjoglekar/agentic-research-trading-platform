"""Persistent store for share lots tracked by hand (for covered-call analysis).

A single JSON file at the project root that is never committed. Lots listed
here join the background researcher's rotation, so they get a news-grounded
memo even when they are outside the scan universe.

Schema:
    {
      "positions": [
        {"symbol": "XYZ", "shares": 100, "cost_basis": 50.00,
         "added_at": "2026-01-02T..."},
        ...
      ],
      "updated_at": "..."
    }
"""
from __future__ import annotations

import datetime
import json
from pathlib import Path
from typing import Optional

POSITIONS_PATH = Path(__file__).resolve().parents[1] / "owned_positions.json"


def _now_iso() -> str:
    return datetime.datetime.utcnow().isoformat() + "Z"


def load_positions() -> list[dict]:
    """Return list of owned-position dicts. Empty list if no file yet."""
    if not POSITIONS_PATH.exists():
        return []
    try:
        data = json.loads(POSITIONS_PATH.read_text())
        positions = data.get("positions", [])
        # Normalize: ensure symbol is uppercase, types are correct
        out = []
        for p in positions:
            sym = (p.get("symbol") or "").strip().upper()
            if not sym:
                continue
            out.append({
                "symbol": sym,
                "shares": int(p["shares"]) if p.get("shares") else 0,
                "cost_basis": float(p["cost_basis"]) if p.get("cost_basis") else None,
                "added_at": p.get("added_at"),
            })
        return out
    except Exception:
        return []


def save_positions(positions: list[dict]) -> None:
    """Overwrite the positions file."""
    POSITIONS_PATH.write_text(json.dumps({
        "positions": positions,
        "updated_at": _now_iso(),
    }, indent=2))


def add_position(symbol: str, shares: int = 100, cost_basis: Optional[float] = None) -> dict:
    """Add (or upsert) a position. If symbol already exists, replaces it."""
    sym = symbol.strip().upper()
    positions = [p for p in load_positions() if p["symbol"] != sym]
    record = {
        "symbol": sym,
        "shares": int(shares) if shares else 0,
        "cost_basis": float(cost_basis) if cost_basis else None,
        "added_at": _now_iso(),
    }
    positions.append(record)
    save_positions(positions)
    return record


def remove_position(symbol: str) -> bool:
    """Remove by symbol. Returns True if something was removed."""
    sym = symbol.strip().upper()
    positions = load_positions()
    filtered = [p for p in positions if p["symbol"] != sym]
    if len(filtered) < len(positions):
        save_positions(filtered)
        return True
    return False


def get_symbols() -> list[str]:
    """Just the tickers, uppercase. Used by the background researcher's rotation."""
    return [p["symbol"] for p in load_positions()]
