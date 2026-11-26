"""Deterministic equity-research overlay for the options scan.

The wheel sells cash-secured puts on names it is willing to OWN. The dashboard's
research screen (multi-factor composite + confidence + DCF, rebuilt every ~12h,
fully deterministic) is exactly that willingness judgment — so its verdict gets
a bounded say in scan scoring:

  blended = (1 - w) * scan_score + w * equity_score        (w = weight knob)
  low-confidence names (value traps / parabolic tapes) take a flat penalty on
  top, so juicy premium alone can't rank them first.

Honesty rules:
- Names OUTSIDE the research universe (scan trades non-S&P names) are NEUTRAL —
  untouched score, empty research fields. Absence of coverage is not a signal.
- A stale/missing research file disables the overlay for the run (logged) —
  the scan degrades to its own scoring, never to fabricated research.
- Pure rules end-to-end: no LLM anywhere in this path (house rule for opening).

All knobs live in config.settings.RESEARCH_OVERLAY.
"""
from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

from loguru import logger

from config.settings import RESEARCH_OVERLAY

# Columns added to the scan DataFrame / grouped rows.
FIELDS = ("equity_score", "equity_confidence", "equity_conf_label",
          "equity_tier", "dcf_upside_pct")


def load_research(path: str | None = None) -> dict:
    """symbol -> research fields, or {} when the overlay is disabled/stale/missing."""
    cfg = RESEARCH_OVERLAY
    if not cfg.get("enabled", True):
        return {}
    p = Path(path or cfg["path"])
    try:
        doc = json.loads(p.read_text())
        gen = dt.datetime.fromisoformat(
            str(doc.get("generated_at", "")).replace("Z", "+00:00"))
        age_h = (dt.datetime.now(dt.timezone.utc) - gen).total_seconds() / 3600
        if age_h > float(cfg["max_age_hours"]):
            logger.warning(f"[overlay] research.json is {age_h:.0f}h old "
                           f"(cap {cfg['max_age_hours']}h) — overlay disabled this run")
            return {}
        book: dict = {}
        for r in doc.get("stocks") or []:
            sym = r.get("symbol")
            if not sym:
                continue
            dcf = r.get("dcf") or {}
            book[sym] = {
                "equity_score": r.get("score"),
                "equity_confidence": r.get("confidence"),
                "equity_conf_label": r.get("confidence_label"),
                "equity_tier": r.get("tier"),
                "dcf_upside_pct": dcf.get("upside_pct") if dcf.get("available") else None,
            }
        logger.info(f"[overlay] research loaded: {len(book)} names, {age_h:.1f}h old")
        return book
    except Exception as e:  # missing file, bad JSON, bad timestamp — degrade, don't die
        logger.warning(f"[overlay] research unavailable ({e}) — overlay disabled this run")
        return {}


def apply_overlay(final_df, book: dict | None = None):
    """Blend equity research into the scan's 'Final Score' and attach research
    columns. Returns (df re-sorted by blended score, human note). Neutral no-op
    (df unchanged, note explains why) when research is unavailable."""
    cfg = RESEARCH_OVERLAY
    if final_df is None or final_df.empty:
        return final_df, None
    if book is None:
        book = load_research()
    if not book:
        return final_df, "research overlay inactive (no fresh research)"

    w = float(cfg["weight"])
    low_thr = float(cfg["low_conf_threshold"])
    penalty = float(cfg["low_conf_penalty"])

    covered = penalized = 0
    new_scores, cols = [], {f: [] for f in FIELDS}
    for _, row in final_df.iterrows():
        sym = str(row.get("Ticker", "")).upper()
        r = book.get(sym)
        score = float(row.get("Final Score", 0) or 0)
        if r and r.get("equity_score") is not None:
            covered += 1
            blended = (1 - w) * score + w * float(r["equity_score"])
            conf = r.get("equity_confidence")
            if conf is not None and float(conf) < low_thr:
                blended -= penalty
                penalized += 1
            new_scores.append(round(max(0.0, blended), 1))
        else:
            r = r or {}
            new_scores.append(round(score, 1))
        for f in FIELDS:
            cols[f].append(r.get(f))

    final_df = final_df.copy()
    final_df["Final Score"] = new_scores
    for f in FIELDS:
        final_df[f] = cols[f]
    final_df = final_df.sort_values("Final Score", ascending=False).reset_index(drop=True)
    # Keep the saved artifacts honest: re-number Rank to the blended order.
    if "Rank" in final_df.columns:
        final_df["Rank"] = range(1, len(final_df) + 1)
    note = (f"research overlay: {covered}/{len(final_df)} rows covered, "
            f"w={w:.2f}, {penalized} low-confidence penalized (−{penalty:g})")
    return final_df, note


def annotate_grouped(grouped: list, book: dict | None = None) -> None:
    """Attach research fields to each per-stock grouped row (in place) so the
    dashboard's scan/candidate views can show WHY a name ranks where it does."""
    if not grouped:
        return
    if book is None:
        book = load_research()
    for s in grouped:
        r = book.get(str(s.get("symbol", "")).upper()) or {}
        for f in FIELDS:
            s[f] = r.get(f)
