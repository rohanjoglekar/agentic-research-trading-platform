#!/usr/bin/env python3
"""Recalculate an .xlsx with LibreOffice and report formula errors as JSON.

    python3 recalc.py model.xlsx [timeout_s]
        [--cells "DCF!B6,DCF!C95"]     also print the recalculated value (and the
                                       formula text) of these cells
        [--set "DCF!B6=1"]             recalculate a TEMPORARY copy with this
                                       input overridden (repeatable)
        [--out path.xlsx]              keep the recalculated copy here

The input file is never modified. Without --set the recalculated copy is kept
next to the input as <stem>.recalc.xlsx (formulas AND computed values); with
--set nothing is kept unless --out is given.

This is a command-line tool: its stdout IS its interface (one JSON object), so
it prints rather than logs. Exit status: 0 = recalculated (check "status" for
formula errors), 2 = could not recalculate.

openpyxl writes formulas without computed values. LibreOffice loads the file,
computes every formula (a registry override forces a full recalculation on
load, so stale cached values can never survive) and saves a copy that carries
both — which is what the values below are read from.
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
from pathlib import Path

EXCEL_ERRORS = ("#VALUE!", "#DIV/0!", "#REF!", "#NAME?", "#NULL!", "#NUM!", "#N/A")
MAX_LOCATIONS = 20
MAX_BYTES = 25 * 1024 * 1024
_CELL_RE = re.compile(r"^'?([^'!]+)'?!\$?([A-Za-z]{1,3})\$?([0-9]{1,7})$")

# Force "recalculate always" when loading OOXML / ODF (0 = always, 1 = never, 2 = prompt).
_XCU = """<?xml version="1.0" encoding="UTF-8"?>
<oor:items xmlns:oor="http://openoffice.org/2001/registry" xmlns:xs="http://www.w3.org/2001/XMLSchema" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">
<item oor:path="/org.openoffice.Office.Calc/Formula/Load"><prop oor:name="OOXMLRecalcMode" oor:op="fuse"><value>0</value></prop></item>
<item oor:path="/org.openoffice.Office.Calc/Formula/Load"><prop oor:name="ODFRecalcMode" oor:op="fuse"><value>0</value></prop></item>
</oor:items>
"""


class RecalcError(Exception):
    pass


def parse_cell(ref: str) -> tuple[str, str]:
    """'DCF!$C$95' → ('DCF', 'C95'). Raises ValueError on anything else."""
    m = _CELL_RE.match(ref.strip())
    if not m:
        raise ValueError(f"bad cell reference {ref!r} (want Sheet!A1)")
    return m.group(1), f"{m.group(2).upper()}{int(m.group(3))}"


def parse_set(spec: str) -> tuple[str, str, object]:
    """'DCF!B6=1' → ('DCF', 'B6', 1). Numbers become numbers; the rest stay text."""
    if "=" not in spec:
        raise ValueError(f"bad --set {spec!r} (want Sheet!A1=value)")
    ref, raw = spec.split("=", 1)
    sheet, cell = parse_cell(ref)
    raw = raw.strip()
    try:
        val: object = int(raw)
    except ValueError:
        try:
            val = float(raw)
        except ValueError:
            val = raw
    return sheet, cell, val


def _jsonable(v):
    if v is None or isinstance(v, (bool, int, str)):
        return v
    if isinstance(v, float):
        return v if v == v and v not in (float("inf"), float("-inf")) else None
    if isinstance(v, (_dt.datetime, _dt.date, _dt.time)):
        return v.isoformat()
    return str(v)


def find_soffice() -> str:
    for name in ("soffice", "libreoffice"):
        hit = shutil.which(name)
        if hit:
            return hit
    for cand in ("/usr/bin/soffice", "/usr/lib/libreoffice/program/soffice",
                 "/Applications/LibreOffice.app/Contents/MacOS/soffice"):
        if os.path.exists(cand):
            return cand
    raise RecalcError("LibreOffice (soffice) is not installed")


def soffice_recalc(src: Path, dest: Path, timeout: int) -> None:
    """Load `src` in headless LibreOffice, recalculate, save as xlsx to `dest`."""
    soffice = find_soffice()
    work = Path(tempfile.mkdtemp(prefix="recalc_"))
    proc = None
    try:
        profile = work / "profile"
        (profile / "user").mkdir(parents=True)
        (profile / "user" / "registrymodifications.xcu").write_text(_XCU, encoding="utf-8")
        outdir = work / "out"
        outdir.mkdir()
        staged = work / "in.xlsx"               # fixed name: the output name derives from it
        shutil.copyfile(src, staged)
        cmd = [soffice, f"-env:UserInstallation={profile.as_uri()}", "--headless", "--norestore",
               "--nologo", "--nodefault", "--nolockcheck", "--convert-to",
               "xlsx:Calc MS Excel 2007 XML", "--outdir", str(outdir), str(staged)]
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                stdin=subprocess.DEVNULL, start_new_session=True)
        try:
            _, err = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            # soffice is a launcher script; kill the whole process group
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            proc.communicate()
            raise RecalcError(f"LibreOffice timed out after {timeout}s")
        produced = outdir / "in.xlsx"
        if proc.returncode != 0 or not produced.exists() or produced.stat().st_size == 0:
            tail = (err or b"").decode("utf-8", "replace").strip()[-400:]
            raise RecalcError(f"LibreOffice failed (exit {proc.returncode}): {tail or 'no output file'}")
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_name(dest.name + ".tmp")
        shutil.copyfile(produced, tmp)
        os.replace(tmp, dest)
    finally:
        shutil.rmtree(work, ignore_errors=True)


def scan(values_path: Path, formulas_path: Path, cells: list[tuple[str, str]]) -> dict:
    """Error scan + requested cells. `values_path` is the recalculated copy
    (computed values); `formulas_path` holds the same cells as formula text."""
    import openpyxl

    wb_v = openpyxl.load_workbook(values_path, data_only=True)
    wb_f = openpyxl.load_workbook(formulas_path, data_only=False)
    errors: dict[str, dict] = {}
    total_errors = 0
    total_formulas = 0
    per_sheet: dict[str, int] = {}
    uncomputed = 0
    for name in wb_f.sheetnames:
        ws_f = wb_f[name]
        ws_v = wb_v[name] if name in wb_v.sheetnames else None
        n = 0
        for row in ws_f.iter_rows():
            for c in row:
                is_formula = isinstance(c.value, str) and c.value.startswith("=")
                if is_formula:
                    n += 1
                v = ws_v[c.coordinate].value if ws_v is not None else None
                if is_formula and v is None:
                    uncomputed += 1
                if isinstance(v, str):
                    for err in EXCEL_ERRORS:
                        if err in v:
                            slot = errors.setdefault(err, {"count": 0, "locations": []})
                            slot["count"] += 1
                            if len(slot["locations"]) < MAX_LOCATIONS:
                                slot["locations"].append(f"{name}!{c.coordinate}")
                            total_errors += 1
                            break
        per_sheet[name] = n
        total_formulas += n

    out_cells: dict[str, dict] = {}
    for sheet, coord in cells:
        key = f"{sheet}!{coord}"
        if sheet not in wb_f.sheetnames:
            out_cells[key] = {"error": f"no sheet named {sheet!r}"}
            continue
        f = wb_f[sheet][coord].value
        v = wb_v[sheet][coord].value if sheet in wb_v.sheetnames else None
        out_cells[key] = {"value": _jsonable(v),
                          "formula": f if isinstance(f, str) and f.startswith("=") else None}
    return {
        "status": "success" if total_errors == 0 else "errors_found",
        "total_formulas": total_formulas,
        "formulas_by_sheet": per_sheet,
        "total_errors": total_errors,
        "error_summary": errors,
        # a formula LibreOffice left blank usually returns an empty string — worth a look
        "formulas_without_value": uncomputed,
        "sheets": wb_f.sheetnames,
        "cells": out_cells,
    }


def run(path: Path, *, timeout: int = 120, cells: list[str] | None = None,
        sets: list[str] | None = None, out: Path | None = None) -> dict:
    if not path.is_file():
        raise RecalcError(f"no such file: {path}")
    if path.stat().st_size > MAX_BYTES:
        raise RecalcError(f"{path.name} is larger than {MAX_BYTES // (1024 * 1024)} MB")
    cell_refs = [parse_cell(c) for c in (cells or []) if c.strip()]
    overrides = [parse_set(x) for x in (sets or [])]

    tmpdir = Path(tempfile.mkdtemp(prefix="recalc_src_"))
    try:
        source = path
        if overrides:
            import openpyxl

            wb = openpyxl.load_workbook(path)           # formulas preserved, no cached values
            for sheet, coord, val in overrides:
                if sheet not in wb.sheetnames:
                    raise RecalcError(f"--set: no sheet named {sheet!r}")
                wb[sheet][coord].value = val
            source = tmpdir / "override.xlsx"
            wb.save(source)
        if out is not None:
            dest = out
        elif overrides:
            dest = tmpdir / "recalculated.xlsx"
        else:
            dest = path.with_name(path.stem + ".recalc.xlsx")
        soffice_recalc(source, dest, timeout)
        result = scan(dest, source, cell_refs)
        result["recalculated_file"] = None if (overrides and out is None) else str(dest)
        if overrides:
            result["overrides"] = [f"{s}!{c}={v}" for s, c, v in overrides]
        return result
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("file")
    ap.add_argument("timeout", nargs="?", type=int, default=120)
    ap.add_argument("--cells", default="")
    ap.add_argument("--set", dest="sets", action="append", default=[])
    ap.add_argument("--out", default=None)
    a = ap.parse_args(argv)
    try:
        result = run(Path(a.file), timeout=max(10, min(a.timeout, 600)),
                     cells=[c for c in a.cells.split(",") if c.strip()],
                     sets=a.sets, out=Path(a.out) if a.out else None)
    except (RecalcError, ValueError) as e:
        sys.stdout.write(json.dumps({"status": "failed", "message": str(e)}) + "\n")
        return 2
    except Exception as e:   # a tool must always answer in JSON
        sys.stdout.write(json.dumps({"status": "failed", "message": f"{type(e).__name__}: {e}"}) + "\n")
        return 2
    sys.stdout.write(json.dumps(result, indent=1) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
