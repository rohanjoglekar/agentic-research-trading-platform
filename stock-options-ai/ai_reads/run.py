"""Who gets read, the run loop, and the store.

Everything here is deterministic given its inputs: the API client is passed in,
so tests drive it with a fake. The spend rules (reserve, ceiling, settle) live in
jobs/ai_universe_read.py on top of deep_dcf.budget; this module only promises
never to START a call that could take the run past its cap.
"""
from __future__ import annotations

import json
import os
import tempfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Optional

from ai_reads import reader

UTC = timezone.utc
STORE_VERSION = 1
SDK_ATTEMPTS = 3            # the client is built with max_retries=2
ORPHAN_KEEP_DAYS = 45       # a read for a name that left the universe is kept this long


# ─── store ───────────────────────────────────────────────────────────────────

def empty_store() -> dict:
    return {"version": STORE_VERSION, "updated_at": None, "reads": {}, "last_run": None}


def load_store(path: Path) -> tuple[dict, Optional[str]]:
    """(store, problem). A missing file is an empty store. An unreadable one is
    ALSO an empty store, with the problem returned so the caller can log it:
    reads are cheap to redo and the ledger (which does fail closed) is what
    protects the budget."""
    if not path.exists():
        return empty_store(), None
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(doc, dict) or not isinstance(doc.get("reads"), dict):
            raise ValueError("not a reads store")
    except (OSError, ValueError) as e:
        return empty_store(), f"{path.name} unreadable ({e}); starting from an empty store"
    doc.setdefault("version", STORE_VERSION)
    doc.setdefault("last_run", None)
    doc["reads"] = {str(k).upper(): v for k, v in doc["reads"].items() if isinstance(v, dict)}
    return doc, None


def save_store(path: Path, store: dict) -> None:
    """Atomic replace, readable by the dashboard's service user."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".ai_reads.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(store, fh, separators=(",", ":"), sort_keys=True)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp, 0o644)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _parse_ts(raw) -> Optional[datetime]:
    try:
        d = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return d if d.tzinfo else d.replace(tzinfo=UTC)


# ─── who gets read ───────────────────────────────────────────────────────────

def names_to_read(research: dict, snapshot: dict, fundamentals: dict) -> list[dict]:
    """Every name that gets a read: every row of the research screen, in screen
    order, then anything held that the screen does not carry.

    The order does not depend on the account. A run cut short by its budget
    leaves the top of the screen read and the rest stale, which says nothing
    about what is held. Whatever the screen cannot grade is almost always a
    fund, and is flagged so the prompt says so."""
    held = [str(p.get("symbol") or "").upper() for p in (snapshot or {}).get("positions") or []]
    held = [s for s in dict.fromkeys(held) if s]
    out: dict[str, dict] = {}
    for r in (research or {}).get("stocks") or []:
        sym = str(r.get("symbol") or "").upper()
        if not sym or sym in out:
            continue
        dcf = r.get("dcf") or {}
        out[sym] = {"symbol": sym, "name": r.get("name"), "sector": r.get("sector"),
                    "last": r.get("last"), "stage": r.get("stage"),
                    "dcf": {"archetype": dcf.get("archetype"),
                            "why_archetype": dcf.get("why_archetype")},
                    "in_screen": True, "is_fund": False, "held": sym in held}
    for sym in held:
        if sym in out:
            continue
        f = (fundamentals or {}).get(sym) or {}
        out[sym] = {"symbol": sym, "name": f.get("name") or sym, "sector": f.get("sector"),
                    "last": f.get("current_price"), "stage": None, "dcf": {},
                    "in_screen": False, "is_fund": not f.get("total_revenue"), "held": True}
    return list(out.values())


def due(names: list[dict], store: dict, *, now: datetime, refresh_days: float, model: str,
        force: bool = False) -> list[dict]:
    """The names whose read is missing, older than `refresh_days`, or was made
    by a different model or prompt (scores from two prompts are not comparable,
    so a change re-reads everyone)."""
    if force:
        return list(names)
    cutoff = now - timedelta(days=refresh_days)
    todo = []
    for n in names:
        r = (store.get("reads") or {}).get(n["symbol"])
        ts = _parse_ts((r or {}).get("read_at"))
        if (not r or ts is None or ts < cutoff or r.get("model") != model
                or r.get("prompt_v") != reader.PROMPT_VERSION):
            todo.append(n)
    return todo


def prune(store: dict, names: list[dict], *, now: datetime) -> int:
    """Drop reads for names that left the universe long ago."""
    keep = {n["symbol"] for n in names}
    cutoff = now - timedelta(days=ORPHAN_KEEP_DAYS)
    gone = [s for s, r in store["reads"].items()
            if s not in keep and (_parse_ts(r.get("read_at")) or cutoff) <= cutoff]
    for s in gone:
        del store["reads"][s]
    return len(gone)


# ─── cost bounds ─────────────────────────────────────────────────────────────

def name_bound_usd(cfg: dict) -> float:
    """The most one NAME can cost: a full output budget plus a generous input,
    for every attempt the SDK may make. A retried call is normally not billed
    (the first attempt failed), but a client-side timeout can be, so the bound
    assumes all of them were."""
    rates = cfg["rates_per_mtok"]
    one_call = (cfg["max_tokens"] * rates["output"]
                + cfg["input_tokens_worst_case"] * rates["input"]) / 1e6
    return one_call * SDK_ATTEMPTS


def reservation_usd(n: int, cfg: dict) -> float:
    """What to reserve for a run of `n` names: its worst case, at most the run
    cap. The run loop is handed the same number as its limit, so the settled
    cost can never be above the reservation."""
    return round(min(float(cfg["run_cap_usd"]), n * name_bound_usd(cfg)), 4)


# ─── the run ─────────────────────────────────────────────────────────────────

def _fatal(exc: BaseException) -> bool:
    """A rejected key or a permission error will not get better by trying the
    next name."""
    return getattr(exc, "status_code", None) in (401, 403)


def run(client, todo: list[dict], store: dict, *, fundamentals: dict, cfg: dict,
        limit_usd: float, tally: dict, save: Callable[[dict], None], today: str,
        log: Callable[[str], None] = lambda _m: None) -> dict:
    """Read `todo` in chunks of `workers`, saving the store after every chunk.

    `tally` is updated as results arrive ({"spent", "in_flight"}), so a caller
    that is interrupted can still settle the ledger at what was really spent
    plus a worst case for whatever was mid-flight.

    `limit_usd` is the amount reserved in the ledger for this run. No chunk is
    started unless its worst case still fits under it.

    Stops early, leaving the remaining names for the next run, when: the next
    chunk could cross the limit; the API rejects the key; or
    `max_consecutive_errors` calls in a row fail at the API level.
    """
    workers = max(1, int(cfg["workers"]))
    bound = name_bound_usd(cfg)
    cap = float(limit_usd)
    ok = failed = api_errors = consecutive = 0
    errors: dict[str, str] = {}
    stopped: Optional[str] = None
    tally.setdefault("spent", 0.0)
    tally.setdefault("in_flight", 0)

    def one(n: dict) -> dict:
        return reader.read_one(client, row=n, fund=(fundamentals or {}).get(n["symbol"]) or {},
                               model=cfg["model"], effort=cfg["effort"],
                               max_tokens=cfg["max_tokens"], rates=cfg["rates_per_mtok"],
                               today=today)

    with ThreadPoolExecutor(max_workers=workers) as pool:
        for i in range(0, len(todo), workers):
            chunk = todo[i:i + workers]
            # written as "go only if provably inside the cap": a nan anywhere stops the run
            if not tally["spent"] + len(chunk) * bound <= cap + 1e-9:
                stopped = f"budget for this run (${cap:.2f}) reached after ${tally['spent']:.4f}"
                break
            futures = {pool.submit(one, n): n for n in chunk}
            tally["in_flight"] = len(futures)
            for fut in as_completed(futures):
                n = futures[fut]
                # Money first, then the in-flight count, so an interruption
                # between the two can only over-count.
                try:
                    res = fut.result()
                except Exception as e:  # noqa: BLE001 — any SDK/transport failure
                    # Unknown cost: assume every attempt was billed in full.
                    tally["spent"] += bound
                    tally["in_flight"] -= 1
                    tally["assumed"] = True
                    api_errors += 1
                    consecutive += 1
                    errors[n["symbol"]] = f"api: {type(e).__name__}: {str(e)[:160]}"
                    if _fatal(e):
                        stopped = f"the API rejected the request ({type(e).__name__})"
                    continue
                tally["spent"] += float(res.get("cost_usd") or 0.0)
                tally["in_flight"] -= 1
                consecutive = 0
                if res.get("read"):
                    read = dict(res["read"])
                    read["in_screen"] = bool(n.get("in_screen"))
                    store["reads"][n["symbol"]] = read
                    ok += 1
                else:
                    failed += 1
                    errors[n["symbol"]] = str(res.get("error"))
            store["updated_at"] = datetime.now(UTC).isoformat()
            save(store)
            done = min(i + workers, len(todo))
            if done % (workers * 10) == 0 or done == len(todo):
                log(f"{done}/{len(todo)} read, ${tally['spent']:.4f} so far")
            if stopped:
                break
            if consecutive >= int(cfg["max_consecutive_errors"]):
                stopped = f"{consecutive} API errors in a row"
                break

    return {"attempted": ok + failed + api_errors, "ok": ok, "failed": failed,
            "api_errors": api_errors, "remaining": len(todo) - (ok + failed + api_errors),
            "cost_usd": round(tally["spent"], 4), "cost_assumed": bool(tally.get("assumed")),
            "stopped": stopped, "errors": dict(list(errors.items())[:40])}


def history_line(store: dict, *, now: datetime) -> dict:
    """One cross-section for the record: every usable read's raw score. This is
    what a later test of "does the AI read predict returns" will need."""
    return {"ts": now.astimezone(UTC).isoformat(),
            "raw": {s: r.get("raw") for s, r in sorted(store["reads"].items())
                    if r.get("usable") and r.get("raw") is not None}}
