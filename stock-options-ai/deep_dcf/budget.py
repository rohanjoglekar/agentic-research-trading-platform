"""Spend ledger, monthly ceiling and pacing — the "never exceed the ceiling" rule.

Three properties this module guarantees, independent of anything the model does:

1. A run is RESERVED at its full per-run cap before it starts and SETTLED at its
   real cost afterwards. A crash between the two leaves the reservation in
   place, so an interrupted run always counts at its worst case.
2. No run starts when `spent-this-cycle + per-run cap` would cross the monthly
   ceiling.
3. Spend is paced: by day d of the cycle at most `ceiling × (d + 7) / L`
   may have been spent (L = cycle length), so an early backlog cannot drain the
   month. Manual runs skip pacing but never the ceiling.

A ledger that cannot be parsed is an error, never "zero spent" (fail closed).
"""
from __future__ import annotations

import json
import math
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

UTC = timezone.utc
PACE_LOOKAHEAD = timedelta(days=7)


class LedgerError(RuntimeError):
    """The ledger is unreadable. Spending must stop until a human looks."""


def _parse_ts(raw: str) -> datetime:
    dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def cycle_bounds(now: datetime, cycle_day: int) -> tuple[datetime, datetime]:
    """[start, end) of the billing cycle containing `now` (cycle_day in 1..28)."""
    if not 1 <= cycle_day <= 28:
        raise ValueError("cycle_day must be 1..28")
    now = now.astimezone(UTC)
    start = datetime(now.year, now.month, cycle_day, tzinfo=UTC)
    if now < start:
        y, m = (now.year - 1, 12) if now.month == 1 else (now.year, now.month - 1)
        start = datetime(y, m, cycle_day, tzinfo=UTC)
    ny, nm = (start.year + 1, 1) if start.month == 12 else (start.year, start.month + 1)
    return start, datetime(ny, nm, cycle_day, tzinfo=UTC)


def load_ledger(path: Path) -> list[dict]:
    """Every entry, validated. Missing file = empty ledger; bad content raises."""
    if not path.exists():
        return []
    out: list[dict] = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as e:
        raise LedgerError(f"cannot read ledger {path}: {e}") from e
    for n, line in enumerate(lines, 1):
        if not line.strip():
            continue
        try:
            e = json.loads(line)
            if e.get("kind") not in ("reserve", "settle"):
                raise ValueError(f"unknown kind {e.get('kind')!r}")
            if not e.get("run_id"):
                raise ValueError("missing run_id")
            cost = float(e["cost_usd"])
            if not (cost >= 0.0) or cost != cost or cost == float("inf"):
                raise ValueError(f"bad cost_usd {e['cost_usd']!r}")
            _parse_ts(e["ts"])
        except (ValueError, KeyError, TypeError, AttributeError) as err:
            raise LedgerError(f"ledger {path} line {n} is invalid: {err}") from err
        out.append(e)
    return out


def _append(path: Path, entry: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(entry, separators=(",", ":"), sort_keys=True) + "\n"
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(line)
        fh.flush()
        os.fsync(fh.fileno())


def reserve(path: Path, *, run_id: str, symbol: str, model: str, trigger: str,
            cap_usd: float, now: datetime, lane: str = "manual") -> None:
    """`lane` is which entry point started the run (window | fast | manual). The
    window and fast-lane caps count by lane, not by trigger: a sell-decision
    (T3) name is always first in the WEEKLY queue too, and counting by trigger
    let those runs escape the weekly cap."""
    _append(path, {"kind": "reserve", "run_id": run_id, "symbol": symbol, "model": model,
                   "trigger": trigger, "lane": lane, "cost_usd": round(float(cap_usd), 4),
                   "ts": now.astimezone(UTC).isoformat()})


def count_reservations(entries: list[dict], *, since: datetime, lane: str) -> int:
    """Runs started through `lane` at or after `since`. An entry written before
    lanes existed counts as manual."""
    return sum(1 for e in entries
               if e.get("kind") == "reserve" and (e.get("lane") or "manual") == lane
               and _parse_ts(e["ts"]) >= since)


def settle(path: Path, *, run_id: str, symbol: str, cost_usd: float, cost_source: str,
           status: str, now: datetime, duration_s: float | None = None,
           detail: str | None = None) -> None:
    """Record the real cost of a reserved run. `cost_source` is "reported" when
    the CLI told us, "assumed_cap" when it could not (timeout/crash)."""
    entry = {"kind": "settle", "run_id": run_id, "symbol": symbol,
             "cost_usd": round(max(float(cost_usd), 0.0), 4), "cost_source": cost_source,
             "status": status, "ts": now.astimezone(UTC).isoformat()}
    if duration_s is not None:
        entry["duration_s"] = round(float(duration_s), 1)
    if detail:
        entry["detail"] = str(detail)[:300]
    _append(path, entry)


def _runs(entries: list[dict]) -> dict[str, dict]:
    runs: dict[str, dict] = {}
    for e in entries:
        runs.setdefault(e["run_id"], {})[e["kind"]] = e   # a later settle overwrites an earlier one
    return runs


def spent_between(entries: list[dict], start: datetime, end: datetime) -> float:
    """Dollars attributable to [start, end). A run belongs to the cycle it was
    RESERVED in and counts at its settled cost, or its full cap if unsettled."""
    total = 0.0
    for r in _runs(entries).values():
        anchor = r.get("reserve") or r.get("settle")
        if not (start <= _parse_ts(anchor["ts"]) < end):
            continue
        total += float((r.get("settle") or r["reserve"])["cost_usd"])
    return round(total, 4)


def pace_allowance(now: datetime, start: datetime, end: datetime, ceiling: float) -> float:
    """Cumulative dollars that may have been spent by `now` (pace line + 7 days)."""
    span = (end - start).total_seconds()
    frac = ((now.astimezone(UTC) - start) + PACE_LOOKAHEAD).total_seconds() / span
    return round(ceiling * min(max(frac, 0.0), 1.0), 4)


def can_start(entries: list[dict], *, now: datetime, ceiling: float, cycle_day: int,
              run_cap: float, pace: bool = True) -> tuple[bool, str, dict]:
    """May a run with this per-run cap start now? → (ok, reason, numbers)."""
    start, end = cycle_bounds(now, cycle_day)
    spent = spent_between(entries, start, end)
    allowance = pace_allowance(now, start, end, ceiling)
    info = {"cycle_start": start.date().isoformat(), "cycle_end": end.date().isoformat(),
            "spent_usd": spent, "ceiling_usd": ceiling, "pace_allowance_usd": allowance,
            "run_cap_usd": run_cap}
    # Every test is written as "refuse unless provably inside the limit", so a
    # nan or inf anywhere (which makes `a > b` False) refuses instead of passing.
    if not all(math.isfinite(v) for v in (spent, ceiling, allowance, run_cap)):
        return False, "a budget number is not finite — refusing to spend", info
    if not run_cap > 0:
        return False, "per-run cap is not positive", info
    if not spent + run_cap <= ceiling + 1e-9:
        return False, (f"monthly ceiling: ${spent:.2f} spent + ${run_cap:.2f} cap "
                       f"> ${ceiling:.2f}"), info
    if pace and not spent + run_cap <= allowance + 1e-9:
        return False, (f"pacing: ${spent:.2f} spent + ${run_cap:.2f} cap "
                       f"> ${allowance:.2f} allowed by this point in the cycle"), info
    return True, "ok", info


def cycle_summary(entries: list[dict], *, now: datetime, ceiling: float, cycle_day: int) -> dict:
    start, end = cycle_bounds(now, cycle_day)
    runs = _runs(entries)
    in_cycle = [r for r in runs.values()
                if start <= _parse_ts((r.get("reserve") or r.get("settle"))["ts"]) < end]
    settled = [r["settle"] for r in in_cycle if "settle" in r]
    return {
        "cycle_start": start.date().isoformat(), "cycle_end": end.date().isoformat(),
        "spent_usd": spent_between(entries, start, end), "ceiling_usd": ceiling,
        "pace_allowance_usd": pace_allowance(now, start, end, ceiling),
        "runs": len(in_cycle),
        "accepted": sum(1 for s in settled if s.get("status") == "accepted"),
        "unsettled": sum(1 for r in in_cycle if "settle" not in r),
        "avg_cost_usd": round(sum(float(s["cost_usd"]) for s in settled) / len(settled), 2)
                        if settled else None,
    }
