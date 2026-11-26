#!/usr/bin/env python3
"""
trading/alpha_governor.py

Local-LLM POLICY GOVERNOR for the long-only "beat the S&P 500" alpha book.

This is the "have a local model review how the trader is doing and change how stocks
get picked and sold" piece — but built on the SAME safety pattern the wheel's agentic
manager uses (trading/agentic_manager.py):

    the deterministic policy is the BASELINE and the FALLBACK; the LLM only NUDGES
    numeric knobs, each WITHIN a hard clamp (config.settings.ALPHA_CLAMPS), and never
    more than `max_step_frac` of a knob's range per run. It never picks tickers, never
    removes a guardrail, and any malformed/oversized output collapses to "no change".

What it reads:  the current effective policy (ALPHA ← alpha_policy.json), the live book
                state (data/alpha_state.json), and the equity-vs-SPY history
                (data/alpha_history.jsonl) → active return, drawdown, sleeves, holdings,
                opportunity count.
What it writes: an updated data/alpha_policy.json (knobs the runner overlays next cycle)
                and an append-only data/alpha_governor_log.jsonl audit trail.

Mode (config.settings.ALPHA_GOVERNOR["mode"], mirrors AGENT["mode"]):
  • off       — do nothing.
  • advisory  — compute + log the PROPOSED policy, but DO NOT write alpha_policy.json
                (default; --dry-run forces this). The runner keeps the current policy.
  • apply     — write alpha_policy.json so the runner adopts it next cycle.

Run:
    python -m trading.alpha_governor --dry-run
    python -m trading.alpha_governor            # honours ALPHA_GOVERNOR["mode"]
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from pathlib import Path

import requests
from loguru import logger

HERE = Path(__file__).resolve()
SOAI_ROOT = HERE.parents[1]
if str(SOAI_ROOT) not in sys.path:
    sys.path.insert(0, str(SOAI_ROOT))

from config.settings import (  # noqa: E402
    ALPHA, ALPHA_CLAMPS, ALPHA_GOVERNOR, clamp_alpha_policy, load_alpha_policy,
)


def _resolve(p: str) -> Path:
    q = Path(p)
    return q if q.is_absolute() else (SOAI_ROOT / q)


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ─── local model ─────────────────────────────────────────────────────────────

def _ollama_chat(system: str, user: str, gcfg: dict) -> str:
    """One-shot JSON chat against local Ollama (format=json, keep_alive, retry
    once on a transient transport error)."""
    payload = {
        "model": gcfg["model"],
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "stream": False,
        "format": "json",
        "keep_alive": "5m",
        "options": {"temperature": gcfg.get("temperature", 0.3)},
    }
    last: Exception | None = None
    for _ in range(2):
        try:
            r = requests.post(f"{gcfg['ollama_url']}/api/chat", json=payload,
                              timeout=gcfg.get("timeout_s", 180))
            r.raise_for_status()
            return (r.json().get("message") or {}).get("content") or ""
        except requests.RequestException as e:
            last = e
    raise last  # type: ignore[misc]


def _extract_json(text: str) -> dict:
    text = (text or "").strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        a, b = text.find("{"), text.rfind("}")
        if a >= 0 and b > a:
            try:
                return json.loads(text[a:b + 1])
            except json.JSONDecodeError:
                pass
    return {}


# ─── performance / book read ──────────────────────────────────────────────────

def _load_history(cfg: dict, limit: int = 2000) -> list:
    hist = _resolve(cfg.get("state_path", "data/alpha_state.json")).with_name("alpha_history.jsonl")
    if not hist.exists():
        return []
    rows = []
    for ln in hist.read_text().strip().splitlines()[-limit:]:
        try:
            rows.append(json.loads(ln))
        except Exception:
            continue
    return rows


def _perf_summary(hist: list) -> dict:
    """Active return vs SPY + drawdown from the equity/SPY history (rebased to 100)."""
    pts = [r for r in hist if r.get("equity")]
    n = len(pts)
    if n < 2:
        return {"points": n, "strat_return_pct": 0.0, "spy_return_pct": 0.0,
                "active_return_pct": 0.0, "max_drawdown_pct": 0.0, "since": pts[0]["as_of"] if pts else None}
    e0 = float(pts[0]["equity"]) or 1.0
    strat = float(pts[-1]["equity"]) / e0 - 1.0
    spy = 0.0
    sp = [float(r["spy"]) for r in pts if r.get("spy")]
    if len(sp) >= 2 and sp[0]:
        spy = sp[-1] / sp[0] - 1.0
    peak, mdd = e0, 0.0
    for r in pts:
        eq = float(r["equity"])
        peak = max(peak, eq)
        mdd = min(mdd, eq / peak - 1.0)
    return {
        "points": n,
        "strat_return_pct": round(strat * 100, 2),
        "spy_return_pct": round(spy * 100, 2),
        "active_return_pct": round((strat - spy) * 100, 2),
        "max_drawdown_pct": round(mdd * 100, 2),
        "since": pts[0].get("as_of"),
    }


def _book_summary(cfg: dict) -> dict:
    """Current sleeves / holdings / sector spread / opportunity count from alpha_state.json."""
    st = _resolve(cfg.get("state_path", "data/alpha_state.json"))
    if not st.exists():
        return {}
    try:
        s = json.loads(st.read_text())
    except Exception:
        return {}
    sectors: dict = {}
    for t in s.get("target", []):
        if t.get("sleeve") == "satellite":
            sec = t.get("sector") or "?"
            sectors[sec] = sectors.get(sec, 0) + 1
    return {
        "sleeves": s.get("sleeves", {}),
        "n_holdings": len(s.get("selected", [])),
        "opportunity_count": s.get("opportunity_count"),
        "reserve_weight": (s.get("reserve") or {}).get("weight"),
        "n_recent_sells": len(s.get("sells", [])),
        "styles": _count_styles(s.get("target", [])),
    }


def _count_styles(target: list) -> dict:
    out: dict = {}
    for t in target:
        if t.get("sleeve") == "satellite":
            k = t.get("style") or "?"
            out[k] = out.get(k, 0) + 1
    return out


# ─── prompt ───────────────────────────────────────────────────────────────────

_SYSTEM = (
    "You are the POLICY GOVERNOR for a long-only paper equity portfolio whose sole "
    "objective is to BEAT the S&P 500 (SPY) over time at acceptable risk. You do NOT "
    "pick stocks — a separate research engine does that. Your only job is to tune a "
    "small set of NUMERIC KNOBS that control how the book is built and sold, each "
    "strictly within the [min,max] range you are given. Rules: (1) stay inside every "
    "range; (2) make SMALL, justified adjustments — keep a knob unchanged unless the "
    "data argues for a change; (3) the book must beat SPY, so a large passive anchor "
    "(core_weight) is counter-productive — favour the conviction names and the dynamic "
    "reserve; (4) if performance is weak, consider tighter sells (stop_loss_pct) or a "
    "larger reserve when opportunities are thin; if the book lags by holding too much "
    "cash while opportunities are plentiful, lean the reserve down. Respond ONLY with "
    'JSON: {"knobs": {<knob>: <number>, ...}, "rationale": "<=2 sentences"}. Include '
    "only knobs you are changing."
)


def _build_user_prompt(current: dict, perf: dict, book: dict) -> str:
    ranges = "\n".join(
        f"  {k}: current={current.get(k)}  allowed=[{lo}, {hi}]"
        for k, (lo, hi) in ALPHA_CLAMPS.items()
    )
    return (
        "CURRENT KNOBS (adjust within the allowed range only):\n" + ranges +
        "\n\nPERFORMANCE (equity vs SPY, since inception of the paper book):\n"
        f"  data points: {perf.get('points')}  since {perf.get('since')}\n"
        f"  strategy return: {perf.get('strat_return_pct')}%   SPY return: {perf.get('spy_return_pct')}%\n"
        f"  ACTIVE return vs SPY: {perf.get('active_return_pct')}%   max drawdown: {perf.get('max_drawdown_pct')}%\n"
        "\nCURRENT BOOK:\n"
        f"  sleeves: {book.get('sleeves')}\n"
        f"  holdings: {book.get('n_holdings')} names   styles: {book.get('styles')}\n"
        f"  reserve weight: {book.get('reserve_weight')}   'extremely good buys' available now: {book.get('opportunity_count')}\n"
        f"  sells last run: {book.get('n_recent_sells')}\n"
        "\nPropose knob changes (or none) to improve active return vs SPY at acceptable "
        "risk, then give a one- or two-sentence rationale. JSON only."
    )


# ─── bounding ────────────────────────────────────────────────────────────────

def _apply_step_limit(current: dict, proposed: dict, max_step_frac: float) -> dict:
    """Cap each knob's move to `max_step_frac` of its clamp range and re-coerce type,
    so the LLM can nudge but never lurch."""
    out: dict = {}
    for k, newv in proposed.items():
        lo, hi = ALPHA_CLAMPS[k]
        oldv = float(current.get(k, newv))
        max_step = max_step_frac * (hi - lo)
        v = float(newv)
        if abs(v - oldv) > max_step:
            v = oldv + max_step * (1.0 if v > oldv else -1.0)
        v = max(lo, min(hi, v))
        if isinstance(ALPHA.get(k), int):
            v = int(round(v))
        out[k] = v
    return out


# ─── main ─────────────────────────────────────────────────────────────────────

def run(gcfg: dict, dry_run: bool = False) -> dict:
    mode = "advisory" if dry_run else gcfg.get("mode", "advisory")
    if mode == "off":
        logger.info("ALPHA_GOVERNOR mode=off — skipping")
        return {"mode": "off"}

    cfg = load_alpha_policy(dict(ALPHA))                 # current EFFECTIVE policy
    current = {k: cfg[k] for k in ALPHA_CLAMPS}
    hist = _load_history(cfg)
    perf = _perf_summary(hist)
    book = _book_summary(cfg)

    # Not enough track record yet → do nothing (deterministic baseline holds).
    if perf["points"] < int(gcfg.get("min_history_points", 4)):
        rec = {"as_of": _now(), "mode": mode, "source": "insufficient_history",
               "rationale": f"only {perf['points']} history points (<{gcfg.get('min_history_points',4)}) — policy unchanged",
               "perf": perf, "changed": {}}
        _log(gcfg, rec)
        logger.info(f"ALPHA_GOVERNOR: {rec['rationale']}")
        return rec

    # Ask the local model for a bounded re-tune; any failure → keep current policy.
    proposed, source, rationale = {}, "llm", ""
    try:
        raw = _ollama_chat(_SYSTEM, _build_user_prompt(current, perf, book), gcfg)
        data = _extract_json(raw)
        proposed = clamp_alpha_policy(data.get("knobs") or data)     # HARD clamp
        proposed = _apply_step_limit(current, proposed, float(gcfg.get("max_step_frac", 0.34)))
        rationale = str(data.get("rationale") or "")[:500]
        if not proposed:
            source = "no_change"
            rationale = rationale or "model proposed no in-range changes"
    except Exception as e:
        source, proposed, rationale = "fallback", {}, f"LLM/parse failure ({e}) — policy unchanged"

    new_knobs = {**current, **proposed}
    changed = {k: {"from": current.get(k), "to": new_knobs[k]}
               for k in new_knobs if new_knobs[k] != current.get(k)}

    rec = {"as_of": _now(), "mode": mode, "source": source, "rationale": rationale,
           "perf": perf, "book": book, "changed": changed}

    # Apply only in apply-mode AND only when something actually changed.
    if mode == "apply" and changed:
        _write_policy(cfg, new_knobs, rationale, source)
        rec["applied"] = True
        logger.info(f"ALPHA_GOVERNOR [apply] wrote {len(changed)} knob change(s): "
                    f"{ {k: v['to'] for k, v in changed.items()} }")
    else:
        rec["applied"] = False
        logger.info(f"ALPHA_GOVERNOR [{mode}] proposed {len(changed)} change(s) "
                    f"(source={source}) — {'not written' if mode!='apply' else 'no change'}")

    _log(gcfg, rec)
    return rec


def _write_policy(cfg: dict, knobs: dict, rationale: str, source: str) -> None:
    out = _resolve(cfg.get("policy_path", "data/alpha_policy.json"))
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({
        "as_of": _now(), "source": source, "rationale": rationale,
        "knobs": knobs,
    }, indent=2))


def _log(gcfg: dict, rec: dict) -> None:
    logp = _resolve(gcfg.get("log_path", "data/alpha_governor_log.jsonl"))
    try:
        logp.parent.mkdir(parents=True, exist_ok=True)
        with logp.open("a") as fh:
            fh.write(json.dumps(rec) + "\n")
    except Exception as e:
        logger.warning(f"[alpha_governor] could not append log: {e}")


def main():
    ap = argparse.ArgumentParser(description="Alpha policy governor (local LLM, bounded)")
    ap.add_argument("--dry-run", action="store_true", help="force advisory (propose + log, no write)")
    args = ap.parse_args()
    run(dict(ALPHA_GOVERNOR), dry_run=args.dry_run)


if __name__ == "__main__":
    main()
