"""Lightweight per-position entry store for the alpha equity book.

Tracks, per held symbol: when we first saw it (holding age → gates take-profit vs
churn), the entry price, and the research context at entry (score, DCF upside). This
is the small amount of state the sell-discipline layer needs *beyond* what Alpaca's
live position object already gives us (average cost + unrealized P/L). Everything
degrades to an empty store on any error, so the runner behaves exactly as before if
the file is missing or corrupt.

Storage: one JSON file (``data/alpha_positions.json`` by default), written atomically.
"""
from __future__ import annotations

import datetime
import json
import os
import tempfile
from pathlib import Path
from typing import Optional

from loguru import logger

_PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def _resolve(p: str) -> Path:
    q = Path(p)
    return q if q.is_absolute() else (_PROJECT_ROOT / q)


def load(cfg: dict) -> dict:
    """Return the store dict ({symbol: {...}}) or {} if none/unreadable."""
    p = _resolve(cfg.get("positions_path", "data/alpha_positions.json"))
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text()) or {}
    except Exception as e:
        logger.warning(f"[alpha_positions] read failed: {e}")
        return {}


def save(cfg: dict, store: dict) -> None:
    """Atomically persist the store."""
    p = _resolve(cfg.get("positions_path", "data/alpha_positions.json"))
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(p.parent), suffix=".tmp")
        with os.fdopen(fd, "w") as f:
            json.dump(store, f, indent=2)
        os.replace(tmp, p)
    except Exception as e:
        logger.warning(f"[alpha_positions] write failed: {e}")


def sync(store: dict, held: set, research_by_sym: dict,
         entry_prices: dict, reserved: set) -> dict:
    """Reconcile the store with the live book.

    Adds any newly-held satellite name with its entry context (first_seen = now,
    entry price + research score/DCF at entry); forgets names no longer held. The
    anchor/reserve ETFs (`reserved`) are never tracked here — they aren't sold on
    take-profit/stop-loss, only rebalanced.
    """
    held = set(held)
    for sym in list(store):
        if sym.startswith("_"):
            continue                      # meta keys (e.g. _exits) are not positions
        if sym not in held:
            store.pop(sym, None)          # position closed → forget its entry context
    # prune exit records past any plausible cooldown so the file can't grow forever
    exits = store.get("_exits") or {}
    for sym in list(exits):
        try:
            ts = datetime.datetime.fromisoformat(str(exits[sym].get("ts")).replace("Z", "+00:00"))
            if (datetime.datetime.now(datetime.timezone.utc) - ts).days > 30:
                exits.pop(sym, None)
        except Exception:
            exits.pop(sym, None)
    for sym in held:
        if sym in reserved or sym in store:
            continue
        r = research_by_sym.get(sym)
        store[sym] = {
            "first_seen": _now(),
            "entry_price": (float(entry_prices.get(sym)) if entry_prices.get(sym) else None),
            "entry_score": (float(r.score) if r is not None else None),
            "entry_dcf_upside": (float(r.dcf_upside) if r is not None else None),
        }
    return store


def record_exit(store: dict, sym: str, reason: str) -> None:
    """Remember a discipline exit (stop-loss / thesis / DCF-exhausted) so the
    next run doesn't immediately rebuy the same name — the buy-sell loop fix."""
    store.setdefault("_exits", {})[sym.upper()] = {"ts": _now(), "reason": reason}


def recently_exited(store: dict, sym: str, days: int) -> bool:
    """True if `sym` was discipline-exited within the last `days` days."""
    rec = (store.get("_exits") or {}).get(sym.upper())
    if not rec:
        return False
    try:
        ts = datetime.datetime.fromisoformat(str(rec.get("ts")).replace("Z", "+00:00"))
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=datetime.timezone.utc)
        return (datetime.datetime.now(datetime.timezone.utc) - ts).days < int(days)
    except Exception:
        return False


def active_trim_cap(store: dict, sym: str, days: int) -> Optional[float]:
    """Dollar ceiling still in force from a recent take-profit trim, else None.

    A trim's reduced target is a ONE-RUN instruction: the next run rebuilds the
    conviction-weighted target, which is larger than the freshly-halved position,
    so `diff_orders` issues a BUY and undoes the profit-taking (it fires whenever
    target_weight - position/2 clears the drift band — i.e. any name at a >=4%
    target, which the 8% per-name cap allows). Persisting the cap for `days` is
    the trim analogue of the exit re-entry cooldown.
    """
    rec = (store.get(sym.upper()) or {})
    cap, ts = rec.get("trim_cap_usd"), rec.get("trim_cap_at")
    if cap is None or not ts:
        return None
    try:
        d = datetime.datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        if d.tzinfo is None:
            d = d.replace(tzinfo=datetime.timezone.utc)
        if (datetime.datetime.now(datetime.timezone.utc) - d).days >= int(days):
            return None                   # expired — the book may size up again
        return float(cap)
    except Exception:
        return None


def holding_days(store: dict, sym: str) -> Optional[int]:
    """Whole days since we first saw the position, or None if unknown."""
    ts = (store.get(sym) or {}).get("first_seen")
    if not ts:
        return None
    try:
        d = datetime.datetime.fromisoformat(ts.replace("Z", "+00:00"))
        if d.tzinfo is None:
            d = d.replace(tzinfo=datetime.timezone.utc)
        now = datetime.datetime.now(datetime.timezone.utc)
        return max(0, (now - d).days)
    except Exception:
        return None
