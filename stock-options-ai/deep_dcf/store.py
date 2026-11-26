"""Runner state (per-symbol history) and the two indexes of accepted models.

state.json     what the queue needs to remember between runs
latest.json    one entry per symbol with an ACCEPTED model — what the dashboard reads
public.json    the same shape, for the public site: only models built on the
               public schedule (see on_public_schedule)
models/<SYM>/<run_id>/   the accepted workbook, summary, inputs and verdict

All writes are atomic (temp file + rename) so a reader never sees half a file.
"""
from __future__ import annotations

import json
import os
import re
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Optional

from loguru import logger

from deep_dcf import sandbox
from deep_dcf.config import Settings

MAX_XLSX_BYTES = 25 * 1024 * 1024
MAX_JSON_BYTES = 2 * 1024 * 1024
_CTRL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def _atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "wb") as fh:
        fh.write(data)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


class StateError(RuntimeError):
    """state.json or latest.json exists but cannot be read. Like an unreadable
    ledger, that stops spending: reading it as empty would re-model every name
    as "never modelled", forget every failure strike, or rewrite the published
    index with a single entry."""


def _load(path: Path, default, *, strict: bool = False):
    if not path.exists():
        return default
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, type(default)):
            raise ValueError(f"expected a JSON {type(default).__name__}")
        return data
    except (OSError, ValueError) as e:
        if strict:
            raise StateError(f"{path} is unreadable: {e}") from e
        logger.warning(f"[deep-dcf] {path.name} unreadable ({e}) — treating as empty for this read-only view")
        return default


def load_state(s: Settings, *, strict: bool = False) -> dict:
    st = _load(s.state_path, {}, strict=strict)
    st.setdefault("symbols", {})
    st.setdefault("seen", {})
    return st


def integrity_problems(s: Settings) -> list[str]:
    """Why the on-disk records cannot be trusted enough to spend against."""
    problems: list[str] = []
    for path in (s.state_path, s.latest_path, s.public_path):
        try:
            _load(path, {}, strict=True)
        except StateError as e:
            problems.append(str(e))
    has_history = s.state_path.exists() or s.latest_path.exists() or (
        s.models_dir.is_dir() and any(s.models_dir.iterdir()))
    if has_history and not s.ledger_path.exists():
        problems.append(f"{s.ledger_path} is missing although earlier runs are on record — "
                        "a deleted ledger must not read as zero spent")
    return problems


def save_state(s: Settings, state: dict) -> None:
    state["updated"] = datetime.now(timezone.utc).isoformat()
    _atomic_write(s.state_path, json.dumps(state, indent=1, sort_keys=True).encode())


def record_attempt(state: dict, symbol: str, *, status: str, today: date,
                   detail: Optional[str] = None, accepted: Optional[dict] = None) -> None:
    """Update one symbol after a run. `accepted` carries the fields the triggers
    compare against next time (filing date, price, deterministic upside)."""
    rec = state.setdefault("symbols", {}).setdefault(symbol, {})
    rec["last_attempt"] = today.isoformat()
    rec["last_status"] = status
    if detail:
        rec["last_detail"] = str(detail)[:400]
    else:
        rec.pop("last_detail", None)
    if status == "accepted":
        rec["consecutive_failures"] = 0
        rec["last_model_date"] = today.isoformat()
        rec.update(accepted or {})
    else:
        rec["consecutive_failures"] = int(rec.get("consecutive_failures") or 0) + 1


def _clean(text, limit: int) -> str:
    """Model-written text is untrusted: strip control characters, cap length."""
    return _CTRL.sub(" ", str(text if text is not None else "")).strip()[:limit]


def _clean_list(items, n: int, limit: int) -> list[str]:
    if not isinstance(items, list):
        return []
    return [c for c in (_clean(x, limit) for x in items[:n]) if c]


def on_public_schedule(s: Settings, meta: dict) -> bool:
    """May this model go in the public index?

    Only when the brokerage account had no part in choosing the company or the
    day. A model carries the date and the price it was built at, so one built
    because a name is held, because the account wants to sell it, or by hand,
    would say so to anyone who lined the dates up. Those stay in latest.json.
    The paper books are public, so their names build on the public schedule.

    Anything unproven is treated as the account's: a request written before
    the flag existed is public only if it used the ordinary model, and only
    while the two models differ."""
    triggers = [str(t) for t in (meta.get("triggers") or [])]
    if any(t.startswith("T6") or "Robinhood" in t for t in triggers):
        return False
    flag = meta.get("real_money")
    if flag is not None:
        return flag is False
    return bool(meta.get("model")) and meta.get("model") == s.model_other != s.model_held


def sync_public(s: Settings) -> dict:
    """Bring public.json up to date with latest.json → {latest, public, added}.

    Every latest entry that was built on the public schedule and is newer than
    the public one is copied across. Nothing is ever removed, so a model the
    public already has outlives a later one built for the account. It created
    the index for the models that predate it, and is safe to run again. An
    index entry carries no account flag, so the stored model name decides, as
    for any request written before the flag existed."""
    latest = _load(s.latest_path, {}, strict=True).get("models") or {}
    public = _load(s.public_path, {}, strict=True)
    models = public.setdefault("models", {})
    added = 0
    for sym, entry in latest.items():
        if not isinstance(entry, dict) or not on_public_schedule(s, entry):
            continue
        have = models.get(sym)
        if have is None or str(have.get("generated_at") or "") < str(entry.get("generated_at") or ""):
            models[sym] = entry
            added += 1
    if added or not s.public_path.exists():
        stamps = [str(e.get("generated_at") or "") for e in models.values()]
        public["updated"] = max(stamps) if stamps else None
        _atomic_write(s.public_path, json.dumps(public, indent=1, sort_keys=True).encode())
    return {"latest": len(latest), "public": len(models), "added": added}


def publish(s: Settings, *, symbol: str, run_id: str, run_dir: Path, verdict: dict,
            inputs: dict, meta: dict, today: date) -> dict:
    """Copy an ACCEPTED run out of the sandbox and index it. Numbers come from
    the acceptance check's recalculated values; only prose comes from the
    model's summary.json. → the latest.json entry."""
    latest = _load(s.latest_path, {}, strict=True)     # never rewrite an index we could not read
    to_public = on_public_schedule(s, meta)
    public = _load(s.public_path, {}, strict=True) if to_public else None
    dest = s.models_dir / symbol / run_id
    dest.mkdir(parents=True, exist_ok=True)
    xlsx_name = f"{symbol}_DCF_{today.isoformat()}.xlsx"
    # the LibreOffice-recalculated copy carries formulas AND values, so it opens
    # with numbers in any viewer; the as-built file is kept beside it
    _atomic_write(dest / xlsx_name,
                  sandbox.read_file(run_dir / "model.recalc.xlsx", MAX_XLSX_BYTES))
    _atomic_write(dest / "model_as_built.xlsx",
                  sandbox.read_file(run_dir / "model.xlsx", MAX_XLSX_BYTES))
    summary = json.loads(sandbox.read_file(run_dir / "summary.json", MAX_JSON_BYTES))
    _atomic_write(dest / "summary.json", json.dumps(summary, indent=1).encode())
    _atomic_write(dest / "inputs.json", json.dumps(inputs, indent=1).encode())
    _atomic_write(dest / "acceptance.json", json.dumps(verdict, indent=1).encode())

    vals = verdict.get("values") or {}
    entry = {
        "symbol": symbol,
        "run_id": run_id,
        "as_of": today.isoformat(),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "price": vals.get("price"),
        "scenarios": vals.get("scenarios"),
        "upside_pct": vals.get("upside_pct"),
        "tv_fraction": vals.get("tv_fraction"),
        "thesis": _clean(summary.get("thesis"), 700),
        "key_assumptions": _clean_list(summary.get("key_assumptions"), 8, 300),
        "caveats": _clean_list(summary.get("caveats"), 6, 300),
        "warnings": _clean_list(verdict.get("warnings"), 8, 300),
        "file": f"models/{symbol}/{run_id}/{xlsx_name}",
        "model": meta.get("model"),
        "triggers": meta.get("triggers") or [],
        "cost_usd": meta.get("cost_usd"),
        "filing": meta.get("filing"),
        "deterministic_upside_pct_at_run": meta.get("det_upside"),
    }
    models = latest.setdefault("models", {})
    models[symbol] = entry
    latest["updated"] = entry["generated_at"]
    _atomic_write(s.latest_path, json.dumps(latest, indent=1, sort_keys=True).encode())
    if public is not None:
        # A later model built for the account never replaces this entry: the
        # public keeps the last one built on its own schedule.
        public.setdefault("models", {})[symbol] = entry
        public["updated"] = entry["generated_at"]
        _atomic_write(s.public_path, json.dumps(public, indent=1, sort_keys=True).encode())
    return entry


def notify_discord(msg: str) -> None:
    """One plain line to the notification channel. Silent no-op when not configured."""
    token = os.environ.get("DISCORD_BOT_TOKEN", "").strip()
    channel = os.environ.get("DISCORD_CHANNEL_ID", "").strip()
    if not token or token.startswith("your_") or token == "dummy" or not channel or channel == "0":
        return
    try:
        import requests

        requests.post(f"https://discord.com/api/v10/channels/{channel}/messages",
                      headers={"Authorization": f"Bot {token}", "Content-Type": "application/json"},
                      json={"content": msg[:1900]}, timeout=10)
    except Exception as e:
        logger.warning(f"[deep-dcf] Discord notify failed: {e!r}")
