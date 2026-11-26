#!/usr/bin/env python3
"""Weekly AI read of every research name (and every held name).

    jobs/ai_universe_read.py              read whatever is due, inside the budget
    jobs/ai_universe_read.py --dry-run    what is due and what it could cost; spends nothing
    jobs/ai_universe_read.py --status     the store and this cycle's spend
    jobs/ai_universe_read.py --limit 20   at most 20 names (a sample)
    jobs/ai_universe_read.py --symbols AAPL,KO [--force]

Writes forge-dashboard/data/robinhood/ai_reads.json. The dashboard ranks the
reads across the universe and uses the rank as the composite's `ai` factor; the
Holdings verdict reads the same file. Policy: config/settings.py:AI_READS.

Exit codes: 0 done (or nothing due, or the budget said no), 2 a missing input
or key, 3 the spend ledger is unreadable (a human must look before more is spent).
"""
from __future__ import annotations

import argparse
import fcntl
import json
import os
import signal
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

SOAI_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SOAI_DIR))

from loguru import logger  # noqa: E402

from config.settings import AI_READS  # noqa: E402  (loads .env)
from ai_reads import reader, run as airun  # noqa: E402
from deep_dcf import budget  # noqa: E402

UTC = timezone.utc


def data_dir() -> Path:
    raw = os.environ.get("AI_READS_DATA_DIR", "").strip()
    return Path(raw) if raw else SOAI_DIR.parent / "forge-dashboard" / "data" / "robinhood"


def _load(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        logger.error(f"[ai-reads] cannot read {path}: {e}")
        return None


def _api_key() -> str:
    return (os.environ.get("AI_READS_ANTHROPIC_API_KEY", "").strip()
            or os.environ.get("ANTHROPIC_API_KEY", "").strip())


def _client():
    import anthropic
    return anthropic.Anthropic(api_key=_api_key(), max_retries=airun.SDK_ATTEMPTS - 1,
                               timeout=120.0)


def _summary(store: dict, names: list[dict], now: datetime) -> dict:
    reads = store.get("reads") or {}
    screen = [n["symbol"] for n in names if n["in_screen"]]
    fresh = [s for s in screen if s in reads and reads[s].get("usable")]
    return {"names": len(names), "screen_names": len(screen), "screen_with_usable_read": len(fresh),
            "funds_and_other_held": len(names) - len(screen), "stored_reads": len(reads),
            "updated_at": store.get("updated_at"), "last_run": store.get("last_run")}


def main(argv: list[str] | None = None, *, client=None, now: datetime | None = None) -> int:
    ap = argparse.ArgumentParser(description="Weekly AI read of the research universe")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--symbols", default="")
    ap.add_argument("--force", action="store_true", help="re-read even if the read is fresh")
    a = ap.parse_args(argv)
    cfg = AI_READS
    d = data_dir()
    store_path, ledger_path = d / "ai_reads.json", d / "ai_reads_ledger.jsonl"
    now = now or datetime.now(UTC)

    research = _load(d / "research.json")
    if not isinstance(research, dict) or not research.get("stocks"):
        logger.error("[ai-reads] research.json has no stocks — nothing to read")
        return 2
    snapshot = _load(d / "snapshot.json") or {}
    fundamentals = _load(d / "fundamentals.json") or {}
    names = airun.names_to_read(research, snapshot, fundamentals)
    store, problem = airun.load_store(store_path)
    if problem:
        logger.warning(f"[ai-reads] {problem}")

    try:
        entries = budget.load_ledger(ledger_path)
    except budget.LedgerError as e:
        logger.error(f"[ai-reads] {e} — refusing to spend")
        return 3
    cycle = budget.cycle_summary(entries, now=now, ceiling=cfg["monthly_ceiling_usd"],
                                 cycle_day=cfg["cycle_day"])

    if a.status:
        logger.info("[ai-reads] " + json.dumps({"store": _summary(store, names, now),
                                                "cycle": cycle}, default=str))
        return 0

    pool = names
    if a.symbols:
        want = {s.strip().upper() for s in a.symbols.split(",") if s.strip()}
        pool = [n for n in names if n["symbol"] in want]
        missing = want - {n["symbol"] for n in pool}
        if missing:
            logger.warning(f"[ai-reads] not in the screen or the book, skipped: {sorted(missing)}")
    todo = airun.due(pool, store, now=now, refresh_days=cfg["refresh_days"],
                     model=cfg["model"], force=a.force)
    if a.limit > 0:
        todo = todo[:a.limit]
    reserve = airun.reservation_usd(len(todo), cfg)
    ok, why, info = budget.can_start(entries, now=now, ceiling=cfg["monthly_ceiling_usd"],
                                     cycle_day=cfg["cycle_day"], run_cap=max(reserve, 0.0001),
                                     pace=False)
    logger.info(f"[ai-reads] {len(names)} names, {len(todo)} due; worst case ${reserve:.2f}; "
                f"cycle ${info['spent_usd']:.2f} of ${info['ceiling_usd']:.2f} spent")
    if a.dry_run:
        logger.info(f"[ai-reads] dry run: budget says {'go' if ok else 'no — ' + why}; "
                    f"first due: {[n['symbol'] for n in todo[:15]]}")
        return 0
    if not cfg["enabled"]:
        logger.info("[ai-reads] AI_READS_ENABLED is off — nothing read")
        return 0
    if not todo:
        logger.info("[ai-reads] every read is fresh — nothing to do")
        return 0
    if not ok:
        logger.warning(f"[ai-reads] not started: {why}")
        return 0
    if client is None:
        if not _api_key():
            logger.error("[ai-reads] no ANTHROPIC_API_KEY in the environment")
            return 2
        client = _client()

    d.mkdir(parents=True, exist_ok=True)
    with open(d / "ai_reads.lock", "w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            logger.warning("[ai-reads] another run holds the lock — exiting")
            return 0
        # Re-read the ledger under the lock: a run that finished while this one
        # waited has spent money the first read did not see.
        try:
            entries = budget.load_ledger(ledger_path)
        except budget.LedgerError as e:
            logger.error(f"[ai-reads] {e} — refusing to spend")
            return 3
        ok, why, _ = budget.can_start(entries, now=now, ceiling=cfg["monthly_ceiling_usd"],
                                      cycle_day=cfg["cycle_day"], run_cap=reserve, pace=False)
        if not ok:
            logger.warning(f"[ai-reads] not started: {why}")
            return 0
        return _spend(client, todo, names, store, store_path, ledger_path, d,
                      fundamentals, cfg, reserve, now)


def _spend(client, todo, names, store, store_path, ledger_path, d, fundamentals, cfg,
           reserve, now) -> int:
    run_id = f"aireads-{now.strftime('%Y%m%dT%H%M%S')}-{os.getpid()}"
    budget.reserve(ledger_path, run_id=run_id, symbol="*", model=cfg["model"],
                   trigger=f"{len(todo)} names", cap_usd=reserve, now=now, lane="weekly")
    tally: dict = {"spent": 0.0, "in_flight": 0}
    t0 = time.time()
    result, status = None, "interrupted"
    try:
        result = airun.run(client, todo, store, fundamentals=fundamentals, cfg=cfg,
                           limit_usd=reserve, tally=tally,
                           save=lambda s: airun.save_store(store_path, s),
                           today=now.date().isoformat(),
                           log=lambda m: logger.info(f"[ai-reads] {m}"))
        status = "stopped" if result["stopped"] else "done"
    finally:
        # Settle at what the API reported, plus a full worst case for anything
        # that was mid-flight when the run was interrupted. The run loop never
        # starts a chunk whose worst case does not fit under the reservation, so
        # this cannot come out above it.
        cost = tally["spent"] + tally.get("in_flight", 0) * airun.name_bound_usd(cfg)
        budget.settle(ledger_path, run_id=run_id, symbol="*", cost_usd=cost,
                      cost_source=("reported" if not tally.get("assumed") and status != "interrupted"
                                   else "reported+assumed"),
                      status=status, now=datetime.now(UTC), duration_s=time.time() - t0,
                      detail=(result or {}).get("stopped"))

    airun.prune(store, names, now=now)
    store["last_run"] = {"run_id": run_id, "started_at": now.isoformat(),
                         "finished_at": datetime.now(UTC).isoformat(), "model": cfg["model"],
                         "effort": cfg["effort"], "prompt_v": reader.PROMPT_VERSION,
                         "due": len(todo), **result}
    store["updated_at"] = datetime.now(UTC).isoformat()
    airun.save_store(store_path, store)
    if result["ok"]:
        with open(d / "ai_reads_history.jsonl", "a", encoding="utf-8") as fh:
            fh.write(json.dumps(airun.history_line(store, now=now), separators=(",", ":")) + "\n")
    logger.info(f"[ai-reads] {result['ok']} read, {result['failed']} unusable answers, "
                f"{result['api_errors']} API errors, {result['remaining']} left; "
                f"${result['cost_usd']:.4f} in {time.time() - t0:.0f}s"
                + (f"; stopped: {result['stopped']}" if result["stopped"] else ""))
    if result["errors"]:
        logger.warning(f"[ai-reads] problems: {json.dumps(result['errors'])[:1500]}")
    return 0


if __name__ == "__main__":
    # systemd stops a unit with SIGTERM: turn it into SystemExit so the
    # `finally` in _spend settles the ledger before the process goes.
    for _sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(_sig, lambda signum, _frame: sys.exit(128 + signum))
    sys.exit(main())
