"""Deep DCF adapter — read-only view of the accepted models.

The stock-options-ai/deep_dcf job builds a full investment-banking DCF (three
scenarios, sensitivity grids, live formulas) with Claude, verifies it
independently, and publishes only what passed:

    <DEEP_DCF_DIR>/latest.json     one entry per symbol: what the owner sees
    <DEEP_DCF_DIR>/public.json     the same shape: what the public site sees
    <DEEP_DCF_DIR>/status.json     this cycle's spend against the ceiling
    <DEEP_DCF_DIR>/models/<SYM>/<run>/<SYM>_DCF_<date>.xlsx

ADVISORY ONLY. Nothing here feeds the composite score, the Holdings action or a
trader — it sits beside the deterministic DCF so the two can be compared.

The public read-only gateway (`view_only=True`) reads a different index, not a
filtered copy of the owner's. A model carries the date and price it was built
at, and a model built because the account holds or wants to sell a name is
built on the account's schedule, so its date alone would say what is held.
The job writes public.json only for models built on the public schedule
(deep_dcf/store.py). A missing public index is an empty one. The public gets
the numbers but not the workbook, the run cost, the run ids, the model name,
the reason for the build or its timestamp. `sheet()` serves the model's
COMPUTED values (history, assumptions, projection, valuation, sensitivity) so
the public demo can show the model itself — values only, never the file or its
formulas.
"""
from __future__ import annotations

import re
import zipfile
from datetime import date
from pathlib import Path
from typing import Optional
from xml.etree import ElementTree as ET

import appconfig as config
from adapters import read_json

STALE_AFTER_DAYS = 150
_SYM_RE = re.compile(r"^[A-Z][A-Z0-9.\-]{0,9}$")
# `triggers` says WHY a model was built and `generated_at` in what order: both
# are about how the job ranks its queue, which is not the public's business.
_PRIVATE_FIELDS = ("file", "cost_usd", "run_id", "model", "triggers", "generated_at")


def _models(view_only: bool = False) -> dict:
    name = "public.json" if view_only else "latest.json"
    index = read_json(config.DEEP_DCF_DIR / name, default={}) or {}
    models = index.get("models") if isinstance(index, dict) else None
    return models if isinstance(models, dict) else {}


def _age_days(as_of) -> Optional[int]:
    try:
        return (date.today() - date.fromisoformat(str(as_of)[:10])).days
    except ValueError:
        return None


def _shape(entry: dict, view_only: bool) -> dict:
    out = dict(entry)
    age = _age_days(entry.get("as_of"))
    out["age_days"] = age
    out["stale"] = age is None or age > STALE_AFTER_DAYS
    out["has_file"] = bool(entry.get("file")) and not view_only
    if view_only:
        for k in _PRIVATE_FIELDS:
            out.pop(k, None)
    return out


def get_models(view_only: bool = False) -> dict:
    """Every accepted model, newest first, plus this cycle's budget status.
    The public gets no status: its run counts cover models it is not shown."""
    models = [_shape(e, view_only) for e in _models(view_only).values() if isinstance(e, dict)]
    models.sort(key=lambda e: (str(e.get("as_of") or ""), str(e.get("symbol") or "")), reverse=True)
    status = None if view_only else read_json(config.DEEP_DCF_DIR / "status.json", default=None)
    return {"available": bool(models), "count": len(models), "models": models,
            "status": status if isinstance(status, dict) else None}


def for_symbol(sym: str, view_only: bool = False) -> Optional[dict]:
    entry = _models(view_only).get((sym or "").upper())
    return _shape(entry, view_only) if isinstance(entry, dict) else None


def model_path(sym: str, view_only: bool = False) -> Optional[Path]:
    """The accepted workbook for a symbol, or None. The path comes from our own
    index, but it is still confined to the models directory and must be a real
    .xlsx file — an index entry can never point the download somewhere else."""
    sym = (sym or "").upper()
    if not _SYM_RE.match(sym):
        return None
    entry = _models(view_only).get(sym)
    rel = entry.get("file") if isinstance(entry, dict) else None
    if not isinstance(rel, str) or not rel.endswith(".xlsx"):
        return None
    root = (config.DEEP_DCF_DIR / "models").resolve()
    try:
        path = (config.DEEP_DCF_DIR / rel).resolve()
        path.relative_to(root)
    except (OSError, ValueError):
        return None
    return path if path.is_file() else None


# ─── The model's computed values, for display ────────────────────────────────
# Read with the standard library (an .xlsx is a zip of XML) so the dashboard
# needs no spreadsheet dependency. The published workbook is the LibreOffice-
# recalculated copy, so every formula cell carries its cached value.
_NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
_REL = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id"
_CELL_RE = re.compile(r"^([A-Z]{1,2})(\d{1,5})$")
MAX_SHEETS = 6
MAX_ROWS = 400
MAX_COL = 26                     # A..Z
MAX_XML_BYTES = 8 * 1024 * 1024  # per part, uncompressed
_sheet_cache: dict = {}


def _col_index(col: str) -> int:
    n = 0
    for ch in col:
        n = n * 26 + (ord(ch) - 64)
    return n


def _part(z: zipfile.ZipFile, name: str) -> bytes:
    info = z.getinfo(name)
    if info.file_size > MAX_XML_BYTES:
        raise ValueError(f"{name} is too large")
    return z.read(name)


def read_workbook_values(path: Path) -> list[dict]:
    """[{name, rows: [{r, c: {"A": value, ...}}]}] — cached values only.
    Numbers come back as floats, text as str; empty cells are omitted."""
    with zipfile.ZipFile(path) as z:
        names = set(z.namelist())
        shared: list[str] = []
        if "xl/sharedStrings.xml" in names:
            for si in ET.fromstring(_part(z, "xl/sharedStrings.xml")).iter(f"{_NS}si"):
                shared.append("".join(t.text or "" for t in si.iter(f"{_NS}t")))
        rels = {r.get("Id"): r.get("Target") for r in ET.fromstring(_part(z, "xl/_rels/workbook.xml.rels"))}
        sheets_el = ET.fromstring(_part(z, "xl/workbook.xml")).find(f"{_NS}sheets")
        out: list[dict] = []
        for sh in list(sheets_el if sheets_el is not None else [])[:MAX_SHEETS]:
            target = rels.get(sh.get(_REL)) or ""
            target = target.lstrip("/")
            part = target if target.startswith("xl/") else f"xl/{target}"
            if part not in names:
                continue
            rows: dict[int, dict] = {}
            for c in ET.fromstring(_part(z, part)).iter(f"{_NS}c"):
                m = _CELL_RE.match(c.get("r") or "")
                if not m or _col_index(m.group(1)) > MAX_COL or int(m.group(2)) > MAX_ROWS:
                    continue
                t = c.get("t")
                v = c.find(f"{_NS}v")
                if t == "s" and v is not None and v.text is not None:
                    i = int(v.text)
                    val = shared[i] if 0 <= i < len(shared) else None
                elif t == "inlineStr":
                    isv = c.find(f"{_NS}is")
                    val = "".join(x.text or "" for x in isv.iter(f"{_NS}t")) if isv is not None else None
                elif t in ("str", "e") and v is not None:
                    val = v.text
                elif t == "b" and v is not None:
                    val = v.text == "1"
                elif v is not None and v.text is not None:
                    try:
                        val = float(v.text)
                    except ValueError:
                        val = v.text
                else:
                    val = None
                if val is None or val == "":
                    continue
                rows.setdefault(int(m.group(2)), {})[m.group(1)] = val
            out.append({"name": sh.get("name") or "Sheet",
                        "rows": [{"r": r, "c": rows[r]} for r in sorted(rows)]})
        return out


# Which AI model wrote a workbook's assumptions is private: Opus is used only for
# stocks held with real money, so the model name would reveal holdings. The
# view-only API already drops the `model` field; the sheet must not leak it.
_MODEL_NAME = re.compile(r"\b(?:claude|anthropic)[\w.-]*|\b(?:opus|sonnet|haiku|fable)(?:[\s-]*\d[\w.-]*)?", re.I)
_PRIVATE_ROW = re.compile(r"^\s*(assumptions by|ai model|model used|engine|run id|run cost|cost \(usd\))\b", re.I)


def _redact(sheets: list) -> list:
    """Drop rows that name the model or the run's cost; scrub model names
    anywhere else (e.g. the 'assumptions by claude-…' subtitle)."""
    out = []
    for sh in sheets:
        rows = []
        for row in sh["rows"]:
            a = row["c"].get("A")
            if isinstance(a, str) and _PRIVATE_ROW.match(a):
                continue
            cells = {k: (_MODEL_NAME.sub("an AI model", v) if isinstance(v, str) else v) for k, v in row["c"].items()}
            rows.append({"r": row["r"], "c": cells})
        out.append({"name": sh["name"], "rows": rows})
    return out


def sheet(sym: str, view_only: bool = False) -> Optional[dict]:
    """The accepted model's computed values for display, or None. Cached per
    file and modification time; a model that cannot be read is simply absent.
    The public is shown the model in its own index, which may be an older one."""
    sym = (sym or "").upper()
    path = model_path(sym, view_only)
    if path is None:
        return None
    try:
        key = (str(path), path.stat().st_mtime_ns)
    except OSError:
        return None
    hit = _sheet_cache.get((sym, view_only))
    if hit and hit[0] == key:
        return hit[1]
    try:
        sheets = read_workbook_values(path)
    except (zipfile.BadZipFile, KeyError, ValueError, ET.ParseError, OSError):
        return None
    entry = _models(view_only).get(sym) or {}
    data = {"symbol": sym, "as_of": entry.get("as_of"), "sheets": _redact(sheets)}
    _sheet_cache[(sym, view_only)] = (key, data)
    return data
