"""Persistent investment-thesis store for the wheel trader.

Why this exists
---------------
The trader used to re-derive ``would_own_score`` from scratch on every scan and
the agentic close/hold/roll manager decided each position *cold*, with no memory
of how the thesis had evolved. A thesis is only useful if it's **falsifiable and
tracked over time** — you want to watch conviction *drift* and catch the moment a
pillar breaks, not re-litigate the whole bull case from zero each run.

This module keeps one durable thesis per ticker:
  * the bull-case narrative + the stated risks (latest snapshot),
  * a conviction score (1-10) with a bounded history → a TREND (rising/falling),
  * a status (intact / weakening / broken),
  * an append-only event log of confirming/disconfirming evidence the agent saw.

Two writers feed it:
  * ``per_stock_analyst`` (daily scan) → ``upsert_from_scan`` records the fresh
    bull/risk narrative and conviction = would_own_score.
  * ``agentic_manager`` (3×/day, per gray-zone position) → ``record_agent_observation``
    appends what the agent found and updates conviction/status.

Everything degrades gracefully: no thesis on disk ⇒ readers get ``None`` and the
trader behaves exactly as it did before.

Storage: one atomic JSON file per ticker under ``thesis_store/`` (per-ticker files
avoid the write races that a single shared JSON would hit under the scan's
ThreadPoolExecutor).
"""
from __future__ import annotations

import datetime
import json
import os
import re
import tempfile
from pathlib import Path
from typing import Optional

from loguru import logger

PROJECT_ROOT = Path(__file__).resolve().parents[1]
THESIS_DIR = PROJECT_ROOT / "thesis_store"

# Bounds so the files never grow without limit.
_MAX_CONVICTION_HISTORY = 40
_MAX_LOG = 50
# How far back the trend looks (number of conviction points).
_TREND_WINDOW = 5

_VALID_STATUS = ("intact", "weakening", "broken")
_VALID_IMPACT = ("strengthen", "neutral", "weaken", "break")


# ─── low-level io ────────────────────────────────────────────────────────────

def _now() -> str:
    return datetime.datetime.utcnow().isoformat() + "Z"


def _path(ticker: str) -> Path:
    return THESIS_DIR / f"{ticker.upper()}.json"


def load_thesis(ticker: str) -> Optional[dict]:
    """Return the stored thesis dict for ``ticker`` or None if none exists."""
    p = _path(ticker)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text())
    except Exception as e:
        logger.warning(f"[thesis] failed to read {p.name}: {e}")
        return None


def _save(thesis: dict) -> None:
    """Atomically write a thesis dict (tmp file + os.replace)."""
    THESIS_DIR.mkdir(parents=True, exist_ok=True)
    thesis["updated"] = _now()
    p = _path(thesis["ticker"])
    try:
        fd, tmp = tempfile.mkstemp(dir=str(THESIS_DIR), suffix=".tmp")
        with os.fdopen(fd, "w") as f:
            json.dump(thesis, f, indent=2, default=str)
        os.replace(tmp, p)
    except Exception as e:
        logger.warning(f"[thesis] failed to write {p.name}: {e}")


# ─── helpers ─────────────────────────────────────────────────────────────────

def _clamp_conviction(v) -> Optional[int]:
    try:
        return max(1, min(10, int(round(float(v)))))
    except (TypeError, ValueError):
        return None


def conviction_trend(history: list) -> str:
    """'rising' / 'falling' / 'stable' from the last ``_TREND_WINDOW`` points."""
    pts = [h.get("conviction") for h in (history or []) if h.get("conviction") is not None]
    if len(pts) < 2:
        return "stable"
    window = pts[-_TREND_WINDOW:]
    delta = window[-1] - window[0]
    if delta >= 1:
        return "rising"
    if delta <= -1:
        return "falling"
    return "stable"


def _derive_status(conviction: Optional[int], history: list,
                   explicit: Optional[str] = None) -> str:
    """Status from an explicit agent call, else inferred from conviction level
    and its drift from the recent peak."""
    if explicit in _VALID_STATUS:
        return explicit
    if conviction is None:
        return "intact"
    if conviction <= 3:
        return "broken"
    pts = [h.get("conviction") for h in (history or []) if h.get("conviction") is not None]
    recent_peak = max(pts[-_TREND_WINDOW:], default=conviction)
    if conviction <= 5 or (recent_peak - conviction) >= 2:
        return "weakening"
    return "intact"


_SENT_SPLIT = re.compile(r"(?<=[.!?])\s+")


def _sentences(text: str, limit: int) -> list:
    """Best-effort split of a prose paragraph into up to ``limit`` short points.

    Discrete pillars/risks are a convenience for display; the narrative remains
    the source of truth, so this stays deliberately simple.
    """
    if not text:
        return []
    text = re.sub(r"\*\*.*?\*\*", "", text)  # drop **headers**
    out = []
    for s in _SENT_SPLIT.split(text.strip()):
        s = s.strip(" -•\t")
        if len(s) >= 12:
            out.append(s[:200])
        if len(out) >= limit:
            break
    return out


def split_scan_analysis(text: str) -> tuple[str, str]:
    """Split a per_stock_analyst memo into (risk_narrative, bull_narrative).

    The memo format is: **Risk assessment** <para> **Why it is a wheel candidate**
    <para> WOULD_OWN_SCORE: n. Falls back to ("", whole-text) if headers absent.
    """
    if not text:
        return "", ""
    body = re.sub(r"WOULD_OWN_SCORE\s*:.*$", "", text, flags=re.IGNORECASE | re.DOTALL)
    m_bull = re.search(r"\*\*\s*Why it is a wheel candidate.*?\*\*", body, re.IGNORECASE)
    if m_bull:
        risk_part = body[:m_bull.start()]
        bull_part = body[m_bull.end():]
    else:
        risk_part, bull_part = "", body
    risk_part = re.sub(r"\*\*\s*Risk assessment.*?\*\*", "", risk_part, flags=re.IGNORECASE)
    return risk_part.strip(), bull_part.strip()


# ─── writers ─────────────────────────────────────────────────────────────────

def upsert_from_scan(ticker: str, *, would_own: Optional[int],
                     analysis_text: str = "", price: Optional[float] = None,
                     score: Optional[float] = None,
                     sector: Optional[str] = None) -> Optional[dict]:
    """Record (or refresh) the thesis from a daily-scan analysis.

    conviction = would_own_score. Appends a conviction point, refreshes the
    bull/risk narrative, recomputes trend + status. Returns the stored thesis.
    """
    ticker = (ticker or "").upper()
    if not ticker:
        return None
    conviction = _clamp_conviction(would_own)
    risk_text, bull_text = split_scan_analysis(analysis_text or "")

    thesis = load_thesis(ticker) or {
        "ticker": ticker,
        "created": _now(),
        "conviction_history": [],
        "log": [],
    }

    if conviction is not None:
        thesis.setdefault("conviction_history", []).append(
            {"ts": _now(), "conviction": conviction, "source": "scan"}
        )
        thesis["conviction_history"] = thesis["conviction_history"][-_MAX_CONVICTION_HISTORY:]
        thesis["conviction"] = conviction

    if bull_text:
        thesis["thesis"] = bull_text[:1200]
        thesis["pillars"] = _sentences(bull_text, 4)
    if risk_text:
        thesis["risks_narrative"] = risk_text[:1000]
        thesis["risks"] = _sentences(risk_text, 4)
    if sector:
        thesis["sector"] = sector
    if price is not None:
        thesis["last_price"] = price
    if score is not None:
        thesis["last_scan_score"] = score

    thesis["status"] = _derive_status(thesis.get("conviction"),
                                      thesis.get("conviction_history", []))
    _save(thesis)
    return thesis


def record_agent_observation(ticker: str, *, event: str, impact: str = "neutral",
                             conviction: Optional[int] = None,
                             status: Optional[str] = None,
                             action: Optional[str] = None,
                             source: str = "agent") -> Optional[dict]:
    """Append what the agentic manager observed to the thesis log and, if the
    agent supplied them, update conviction/status.

    A no-op (returns None) when there is no thesis yet AND nothing meaningful to
    record — but if the agent has a view we still create one, so the position is
    tracked going forward.
    """
    ticker = (ticker or "").upper()
    if not ticker:
        return None
    conviction = _clamp_conviction(conviction)
    impact = impact if impact in _VALID_IMPACT else "neutral"

    thesis = load_thesis(ticker)
    if thesis is None:
        if conviction is None and not event:
            return None
        thesis = {"ticker": ticker, "created": _now(),
                  "conviction_history": [], "log": []}

    thesis.setdefault("log", []).append({
        "ts": _now(),
        "event": (event or "")[:400],
        "impact": impact,
        "action": action,
        "conviction": conviction,
        "source": source,
    })
    thesis["log"] = thesis["log"][-_MAX_LOG:]

    if conviction is not None:
        thesis.setdefault("conviction_history", []).append(
            {"ts": _now(), "conviction": conviction, "source": source}
        )
        thesis["conviction_history"] = thesis["conviction_history"][-_MAX_CONVICTION_HISTORY:]
        thesis["conviction"] = conviction

    explicit = status if status in _VALID_STATUS else (
        "broken" if impact == "break" else None
    )
    thesis["status"] = _derive_status(thesis.get("conviction"),
                                      thesis.get("conviction_history", []),
                                      explicit=explicit)
    _save(thesis)
    return thesis


# ─── reader (for the agent prompt) ───────────────────────────────────────────

def _age_days(thesis: dict) -> Optional[int]:
    try:
        created = datetime.datetime.fromisoformat(
            thesis["created"].replace("Z", "")
        )
        return (datetime.datetime.utcnow() - created).days
    except Exception:
        return None


def thesis_summary(ticker: str) -> Optional[dict]:
    """Compact, prompt-ready view of the thesis. None if no thesis on disk.

    Gives the agent MEMORY: prior conviction + how it has trended, the age of the
    thesis, the current status, the prior bull case + stated risks, and the most
    recent logged evidence.
    """
    thesis = load_thesis(ticker)
    if not thesis:
        return None
    history = thesis.get("conviction_history", [])
    pts = [h.get("conviction") for h in history if h.get("conviction") is not None]
    prior = pts[-2] if len(pts) >= 2 else (pts[-1] if pts else None)
    recent_events = [
        {"ts": e.get("ts", "")[:10], "event": e.get("event", ""),
         "impact": e.get("impact"), "action": e.get("action")}
        for e in thesis.get("log", [])[-3:]
    ]
    return {
        "ticker": thesis["ticker"],
        "thesis": thesis.get("thesis", ""),
        "risks_narrative": thesis.get("risks_narrative", ""),
        "pillars": thesis.get("pillars", []),
        "risks": thesis.get("risks", []),
        "conviction": thesis.get("conviction"),
        "conviction_prev": prior,
        "trend": conviction_trend(history),
        "status": thesis.get("status", "intact"),
        "age_days": _age_days(thesis),
        "recent_events": recent_events,
        "scan_count": len([h for h in history if h.get("source") == "scan"]),
    }


def format_for_prompt(summary: dict) -> str:
    """Render a thesis_summary into a compact text block for the agent prompt."""
    if not summary:
        return ""
    conv = summary.get("conviction")
    prev = summary.get("conviction_prev")
    drift = ""
    if conv is not None and prev is not None and prev != conv:
        drift = f" (was {prev}/10 → {conv}/10)"
    elif conv is not None:
        drift = f" ({conv}/10)"
    lines = [
        "\n--- TRACKED THESIS (prior runs — has it broken since?) ---",
        f"status: {summary.get('status','intact').upper()}  | conviction {summary.get('trend','stable')}{drift}"
        f"  | age {summary.get('age_days','?')}d over {summary.get('scan_count',0)} scans",
    ]
    if summary.get("thesis"):
        lines.append(f"bull case: {summary['thesis']}")
    if summary.get("risks_narrative"):
        lines.append(f"stated risks: {summary['risks_narrative']}")
    ev = summary.get("recent_events") or []
    if ev:
        lines.append("recent logged evidence:")
        for e in ev:
            lines.append(f"  • [{e.get('ts','')}] ({e.get('impact')}) {e.get('event','')}")
    lines.append("--- end tracked thesis ---")
    lines.append(
        "Judge whether the thesis is still INTACT, WEAKENING, or BROKEN given today's "
        "fresh news. Cutting is for BROKEN theses; ordinary drawdowns are not breakage.\n"
    )
    return "\n".join(lines)
