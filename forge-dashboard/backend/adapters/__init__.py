"""Forge dashboard data adapters.

Each adapter reads a data source the dashboard uses and returns plain
JSON-serializable dicts/lists. All degrade gracefully: a missing file returns
an empty/placeholder result rather than raising, so the dashboard works both
locally (partial data) and on the server (full data).

JSONL readers are SEEK-BASED. Reading a whole multi-megabyte log to return its
last hundred rows, on every poll, used to be most of one endpoint's cost.
`iter_lines_reverse` walks the file backwards in chunks so a tail costs
O(rows returned), and `scan_lines_since` / `read_jsonl_since` let a caller keep
an incremental index over an append-only log by re-reading only the bytes that
were added.
"""
import json
import math
import os
from pathlib import Path
from typing import Any, Iterator

# Reverse reads start at this chunk size and double (capped) as the caller asks
# for more lines. 64 KiB is ~30 shadow_log rows / ~15 designer rows, so the
# common small tails are ONE read; a 4,000-row tail is a handful.
_TAIL_CHUNK_BYTES = 64 * 1024
_TAIL_CHUNK_MAX_BYTES = 4 * 1024 * 1024
# Forward scans stream in chunks of this size. It is a MEMORY bound, not a
# speed knob: variants.jsonl is 36 MB and a first-pass count over it must not
# materialize 36 MB of bytes plus ~100 MB of decoded str objects on a box with
# 8 GB and a starving research daemon.
_SCAN_CHUNK_BYTES = 1024 * 1024


def safe_num(x: Any) -> float | None:
    """Coerce to float; None for non-numeric, NaN, or ±inf.

    The one canonical version of the `_num` helper rh_factors / rh_archetype /
    rh_dcf each used to define locally (identical semantics)."""
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return None if (math.isnan(v) or math.isinf(v)) else v


def read_json(path: Path, default: Any = None) -> Any:
    """Read a JSON file, return `default` (or None) if missing/unreadable."""
    try:
        if not path.exists():
            return default
        return json.loads(path.read_text())
    except Exception:
        return default


def iter_lines_reverse(path: Path,
                       chunk_bytes: int = _TAIL_CHUNK_BYTES) -> Iterator[str]:
    """Yield the file's lines NEWEST-FIRST without loading the whole file.

    Reads fixed chunks from the end and splits on b"\\n", carrying the
    (possibly incomplete) first fragment of each chunk into the next read so no
    line is ever truncated. Decoding is per-line with errors="replace" — one
    corrupt byte can't abort a tail. A file that ends in a newline yields one
    empty string first; callers skip blanks. Never raises: an unreadable path
    yields nothing."""
    try:
        with open(path, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            pos = fh.tell()
            carry = b""
            step = max(1024, int(chunk_bytes))
            while pos > 0:
                take = min(step, pos)
                pos -= take
                fh.seek(pos)
                buf = fh.read(take) + carry
                parts = buf.split(b"\n")
                carry = parts[0]            # may be a partial line — keep it
                for part in reversed(parts[1:]):
                    yield part.decode("utf-8", "replace")
                step = min(step * 2, _TAIL_CHUNK_MAX_BYTES)
            if carry:
                yield carry.decode("utf-8", "replace")
    except OSError:
        return


def read_jsonl_tail(path: Path, n: int = 50) -> list[dict]:
    """The last n JSON objects of a JSONL file, in FILE order (oldest→newest).

    Seek-based (see module docstring): cost is proportional to n, not to file
    size. Blank and unparseable lines are skipped without consuming quota — a
    half-written trailing line (the writer mid-append) is ignored, exactly as
    the old slurping implementation ignored it. Non-dict JSON lines are skipped
    too: every caller does row.get(...)."""
    if n <= 0 or not path.exists():
        return []
    out: list[dict] = []
    for ln in iter_lines_reverse(path):
        ln = ln.strip()
        if not ln:
            continue
        try:
            row = json.loads(ln)
        except json.JSONDecodeError:
            continue
        if not isinstance(row, dict):
            continue
        out.append(row)
        if len(out) >= n:
            break
    return out[::-1]


def scan_lines_since(path: Path, offset: int, on_line,
                     chunk_bytes: int = _SCAN_CHUNK_BYTES) -> tuple[int, bool]:
    """Stream COMPLETE lines appended after byte `offset` to on_line(str);
    return (new_offset, reset).

    The building block for incremental indexes over append-only logs: read only
    the new bytes, never the whole file, and hold only one chunk at a time so a
    cold first pass over a 36 MB ledger costs a megabyte of RAM instead of a
    hundred. A trailing partial line (writer mid-append) is left unconsumed, so
    `new_offset` always lands on a line boundary and the line is delivered whole
    on the next call.

    reset=True means the file was TRUNCATED OR ROTATED under us (size < offset,
    or offset < 0): the scan restarts at 0 and the caller must discard whatever
    it accumulated, or it would double-count. Never raises."""
    try:
        size = path.stat().st_size
    except OSError:
        return 0, False
    start = int(offset or 0)
    reset = start < 0 or start > size
    if reset:
        start = 0
    if start >= size:
        return size, reset
    step = max(4096, int(chunk_bytes))
    pos = start
    carry = b""
    try:
        with open(path, "rb") as fh:
            fh.seek(start)
            while pos < size:
                buf = fh.read(min(step, size - pos))
                if not buf:
                    break
                pos += len(buf)
                data = carry + buf
                cut = data.rfind(b"\n")
                if cut < 0:                  # no line boundary in this chunk
                    carry = data
                    continue
                for ln in data[:cut].split(b"\n"):
                    if ln:
                        on_line(ln.decode("utf-8", "replace"))
                carry = data[cut + 1:]
    except OSError:
        return start, reset
    return pos - len(carry), reset


def read_jsonl_since(path: Path, offset: int) -> tuple[list[dict], int, bool]:
    """scan_lines_since + json parsing → (rows, new_offset, reset).

    Collects the parsed rows, so use it for an incremental slice (a tick's
    worth of appended lines), not for a cold pass over a huge file — that is
    what the streaming scan_lines_since is for.

    Blank / unparseable / non-dict lines are skipped; the offset still advances
    past them so a single bad line can't wedge an incremental reader."""
    rows: list[dict] = []

    def _parse(ln: str) -> None:
        ln = ln.strip()
        if not ln:
            return
        try:
            row = json.loads(ln)
        except json.JSONDecodeError:
            return
        if isinstance(row, dict):
            rows.append(row)

    new_offset, reset = scan_lines_since(path, offset, _parse)
    return rows, new_offset, reset
