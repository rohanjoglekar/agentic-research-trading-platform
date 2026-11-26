"""The weekly AI read, ranked across the research universe.

stock-options-ai/jobs/ai_universe_read.py asks Claude for a short structured
read of every research name once a week (five business dimensions scored 0-10,
a thesis line and a risk line) and writes data/robinhood/ai_reads.json. This
module turns those reads into the composite's `ai` factor and gives the
Holdings verdict its second opinion.

Calibration: a read's raw score is replaced by its PERCENTILE among this week's
reads of the research universe. Two properties follow, which a raw LLM score
does not have:
- the factor is mean-neutral: across the universe it averages exactly 50, so it
  cannot drag every score up or down (the old local-model verdict said SELL on
  77% of the names it read);
- it is the same yardstick for every name: the factor switches on only when at
  least COVERAGE_MIN of the universe has a fresh read, so no name is nudged by
  a read its neighbours did not get.

Never calls a model. A missing or unreadable file means no AI factor anywhere.
"""
from __future__ import annotations

import datetime as dt
import threading

import appconfig as config
from adapters import read_json

# A read older than this is not ranked. The weekly job re-reads anything 3+
# days old (AI_READS["refresh_days"]), so the oldest read normally seen is just
# under 10 days; this leaves a day of slack for a late run.
FRESH_DAYS = 11
COVERAGE_MIN = 0.90      # share of the universe that must have a fresh read
TOP_PCT = 80             # top quintile
BOTTOM_PCT = 20          # bottom quintile
_CONVICTION_COVERAGE = {"high": 1.0, "medium": 1.0, "low": 0.5}
DIMENSION_LABELS = {
    "business_quality": "Business quality",
    "growth_durability": "Growth durability",
    "financial_resilience": "Financial resilience",
    "risk_outlook": "Risk outlook",
    "valuation_vs_quality": "Price vs quality",
}

_lock = threading.Lock()
_cache: dict = {"key": None, "store": None}


def _parse_ts(raw) -> dt.datetime | None:
    try:
        d = dt.datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return d if d.tzinfo else d.replace(tzinfo=dt.timezone.utc)


def load_store() -> dict:
    """ai_reads.json, cached on mtime (the research build reads it ~600 times)."""
    p = config.RH_AI_READS_PATH
    try:
        st = p.stat()
        key = (st.st_mtime_ns, st.st_size)
    except OSError:
        return {}
    with _lock:
        if _cache["key"] == key:
            return _cache["store"] or {}
        doc = read_json(p, default=None)
        store = doc if isinstance(doc, dict) and isinstance(doc.get("reads"), dict) else {}
        _cache.update(key=key, store=store)
        return store


def _fresh(read: dict, now: dt.datetime) -> bool:
    ts = _parse_ts(read.get("read_at"))
    return ts is not None and now - ts <= dt.timedelta(days=FRESH_DAYS)


def _percentile(x: float, pool: list[float]) -> float:
    """Mid-rank percentile of x in pool: ties share the middle, so the pool's
    own percentiles average exactly 50."""
    below = sum(1 for v in pool if v < x)
    equal = sum(1 for v in pool if v == x)
    return 100.0 * (below + 0.5 * equal) / len(pool)


def calibrate(store: dict, universe: list[str] | set[str],
              now: dt.datetime | None = None) -> dict:
    """{"coverage_ok", "pool", "universe", "reads": {SYM: entry}}.

    The ranking pool is every fresh, usable read of a name in `universe`. Each
    entry carries the read for display, plus `pct` when the read is usable and
    fresh: its percentile in the pool. A held company outside the universe is
    ranked against the pool without joining it."""
    now = now or dt.datetime.now(dt.timezone.utc)
    uni = {str(s).upper() for s in universe}
    reads = {str(k).upper(): v for k, v in ((store or {}).get("reads") or {}).items()
             if isinstance(v, dict)}

    def rankable(r: dict) -> bool:
        return bool(r.get("usable")) and isinstance(r.get("raw"), (int, float)) and _fresh(r, now)

    pool = [float(r["raw"]) for s, r in reads.items() if s in uni and rankable(r)]
    coverage_ok = bool(uni) and len(pool) >= COVERAGE_MIN * len(uni)
    out: dict = {}
    for sym, r in reads.items():
        e = {k: r.get(k) for k in ("raw", "sub", "conviction", "thesis", "key_risk",
                                   "read_at", "model", "kind", "recognized", "usable")}
        e["fresh"] = _fresh(r, now)
        e["pct"] = (round(_percentile(float(r["raw"]), pool), 1)
                    if pool and rankable(r) else None)
        out[sym] = e
    return {"coverage_ok": coverage_ok, "pool": len(pool), "universe": len(uni), "reads": out}


def band(pct: float | None) -> str | None:
    if pct is None:
        return None
    return "top" if pct >= TOP_PCT else "bottom" if pct < BOTTOM_PCT else "middle"


def describe(e: dict | None) -> str | None:
    """'AI read top 12%' / 'AI read bottom 9%' / 'AI read 55th percentile'."""
    pct = (e or {}).get("pct")
    if pct is None:
        return None
    b = band(pct)
    if b == "top":
        return f"AI read top {max(1, round(100 - pct))}%"
    if b == "bottom":
        return f"AI read bottom {max(1, round(pct))}%"
    return f"AI read {_ordinal(round(pct))} percentile"


def _ordinal(n: int) -> str:
    """21 → '21st', 22 → '22nd', 53 → '53rd', 44 → '44th', 11 → '11th'."""
    if 10 <= n % 100 <= 20:
        return f"{n}th"
    return f"{n}{ {1: 'st', 2: 'nd', 3: 'rd'}.get(n % 10, 'th') }"


def factor(e: dict | None, coverage_ok: bool, pool: int = 0, universe: int = 0) -> dict:
    """The composite's `ai` factor for one name: score = percentile, coverage 0
    when the name has no ranked read or the universe is not covered yet."""
    pct = (e or {}).get("pct")
    if pct is None or not coverage_ok:
        why = ("no AI read this week" if pct is None
               else f"AI reads cover {pool} of {universe} names — factor off for every "
                    "name until 90% are read")
        return {"score": None, "coverage": 0.0,
                "components": [{"name": "AI read", "points": None, "max_points": 100,
                                "detail": why}]}
    comps = [{"name": "Rank vs universe", "points": round(pct), "max_points": 100,
              "detail": f"{describe(e)} · raw {e.get('raw')}/100 · "
                        f"{e.get('conviction') or '?'} conviction"}]
    for k, label in DIMENSION_LABELS.items():
        v = (e.get("sub") or {}).get(k)
        comps.append({"name": label, "points": v, "max_points": 10, "detail": ""})
    if e.get("thesis"):
        comps.append({"name": "Thesis", "points": None, "max_points": 0, "detail": e["thesis"]})
    if e.get("key_risk"):
        comps.append({"name": "Key risk", "points": None, "max_points": 0, "detail": e["key_risk"]})
    return {"score": int(round(pct)),
            "coverage": _CONVICTION_COVERAGE.get(e.get("conviction"), 1.0),
            "components": comps}


def for_universe(universe: list[str] | set[str]) -> dict:
    """calibrate() over the stored reads, for callers that do not hold a store."""
    return calibrate(load_store(), universe)


_screen: dict = {"key": None, "syms": []}


def screen_symbols() -> list[str]:
    """The names the last research build actually screened (research.json
    `stocks`). The ranking pool and the coverage test are measured against
    these, not the raw ticker list: a ticker with no price data never reaches
    the screen, so it is never read either."""
    p = config.RH_RESEARCH_CACHE_PATH
    try:
        st = p.stat()
        key = (st.st_mtime_ns, st.st_size)
    except OSError:
        return []
    with _lock:
        if _screen["key"] != key:
            doc = read_json(p, default=None)
            syms = [str(r.get("symbol")).upper() for r in ((doc or {}).get("stocks") or [])
                    if isinstance(r, dict) and r.get("symbol")] if isinstance(doc, dict) else []
            _screen.update(key=key, syms=syms)
        return list(_screen["syms"])


def for_screen() -> dict:
    """calibrate() against the names the last research build screened."""
    return calibrate(load_store(), screen_symbols())


def fields(e: dict | None) -> dict:
    """One read as the `ai_*` fields the research pages show."""
    e = e or {}
    return {"ai_pct": e.get("pct"), "ai_band": band(e.get("pct")),
            "ai_label": describe(e), "ai_raw": e.get("raw"),
            "ai_kind": e.get("kind"), "ai_conviction": e.get("conviction"),
            "ai_thesis": e.get("thesis"), "ai_risk": e.get("key_risk"),
            "ai_sub": e.get("sub"), "ai_read_at": e.get("read_at"),
            "ai_fresh": e.get("fresh")}


def public_read(sym: str) -> dict | None:
    """The weekly AI read of one screened company, for the public gateway: the
    `ai_*` fields and nothing else. It opens neither the account snapshot nor
    the local deep-dive cache. A name outside the screen answers None even when
    a read exists, because the only such reads are of companies the owner
    holds, and showing one would say so."""
    sym = str(sym).upper()
    if sym not in set(screen_symbols()):
        return None
    e = for_screen()["reads"].get(sym)
    if not e:
        return None
    out = fields(e)
    # The day of the read, never the time. The job reads one name after another,
    # so the times would give away the order it works in.
    if out.get("ai_read_at"):
        out["ai_read_at"] = str(out["ai_read_at"])[:10]
    return out
