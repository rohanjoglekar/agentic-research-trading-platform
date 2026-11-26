"""Multi-factor equity scoring — Quality, Value, Growth, Analyst.

Encodes the screening methodology from Anthropic's `idea-generation` /
`comps-analysis` skills as transparent 0-100 factor sub-scores. Each factor is
built from named components (same shape as rh_research's momentum score) so the
UI can show exactly WHY a stock scored the way it did.

Two design choices that keep it honest with flaky free data:
- **Coverage-weighted.** Every factor returns a `coverage` in [0,1] = how much of
  its evidence was actually present. A factor scored on 1 of 5 inputs is noisy,
  so the composite multiplies each factor's weight by its coverage — partial data
  contributes partially instead of either lying or zeroing the score.
- **Renormalized.** A factor's score is the sum of earned points over the points
  that were *scorable*, not over the theoretical max — a missing field never
  silently pushes the score down.

Momentum (price/technical) stays in rh_research._score; this module supplies the
fundamental factors and the composite blender that fuses all of them.
"""
from __future__ import annotations

import re

from adapters import safe_num as _num

# Composite weights (before coverage scaling). Value + Quality + Growth carry the
# fundamental thesis; Momentum keeps the price tape honest; Analyst, News and the
# weekly AI read are soft. The points sum to 105 and are normalised.
WEIGHTS = {
    "value": 25, "quality": 20, "growth": 20,
    "momentum": 20, "analyst": 10, "news": 5, "ai": 5,
}

# A trusted DCF is a light *confirmation* tilt inside the value factor, never a
# driver — 12 of ~92 value points ≈ 3% of the composite (Damodaran/AQR: a single
# intrinsic-value point is too terminal-value-fragile to weight heavily).
DCF_VALUE_MAX_POINTS = 12


def _ramp(v, lo, hi, max_pts):
    """Linear score: v==lo -> 0, v==hi -> max_pts, clamped to [0,max_pts].
    Pass hi < lo for 'lower is better' (cheap P/E, low debt)."""
    v = _num(v)
    if v is None or hi == lo:
        return None
    frac = (v - lo) / (hi - lo)
    return max(0.0, min(1.0, frac)) * max_pts


def _assemble(comps: list[dict]) -> tuple[int | None, float, list[dict]]:
    """comps = [{name, points(None=no data), max_points, detail}]. Returns
    (score_0_100 over scorable points, coverage 0-1, display components)."""
    scorable = [c for c in comps if c["points"] is not None]
    got_max = sum(c["max_points"] for c in scorable)
    total_max = sum(c["max_points"] for c in comps) or 1
    if not scorable or got_max == 0:
        return None, 0.0, comps
    earned = sum(c["points"] for c in scorable)
    score = int(round(earned / got_max * 100))
    coverage = got_max / total_max
    # round points for display
    for c in comps:
        if c["points"] is not None:
            c["points"] = round(c["points"], 1)
    return score, round(coverage, 2), comps


def _pct(x):
    v = _num(x)
    return None if v is None else round(v, 1)


# ─── Quality: margins, returns, balance-sheet strength ───────────────────────
def quality(fund: dict, archetype: str | None = None) -> dict:
    gm = _num(fund.get("gross_margin"))
    om = _num(fund.get("operating_margin"))
    roe = _num(fund.get("roe"))
    fcfm = _num(fund.get("fcf_margin_pct"))      # already a %
    de = _num(fund.get("debt_to_equity"))        # a % (124.9 == 1.25x)
    if archetype == "PRE_PROFIT":
        # Early-stage VIABILITY, not profitability they don't have yet. Operating
        # margin / ROE / FCF-margin are structurally negative for a cash-burning
        # pre-profit name and would score 0, unfairly crushing quality — score
        # instead on gross margin (can the unit economics ever work?), cash runway
        # (will they survive to get there?), and the balance sheet.
        cash = _num(fund.get("total_cash"))
        fcf = _num(fund.get("free_cashflow"))
        runway = (cash / -fcf) if (cash and fcf and fcf < 0) else None      # years of burn
        self_funding = fcf is not None and fcf >= 0
        comps = [
            {"name": "Gross margin", "points": _ramp(gm, 0.10, 0.70, 35), "max_points": 35,
             "detail": f"{gm*100:.0f}% (unit economics)" if gm is not None else "n/a"},
            {"name": "Cash runway", "points": (35.0 if self_funding else _ramp(runway, 0.75, 4.0, 35)),
             "max_points": 35,
             "detail": ("self-funding (FCF ≥ 0)" if self_funding
                        else f"{runway:.1f}y at current burn" if runway is not None else "n/a")},
            {"name": "Balance sheet", "points": _ramp(de, 200, 0, 30), "max_points": 30,
             "detail": f"debt/equity {de/100:.2f}x" if de is not None else "n/a"},
        ]
    else:
        comps = [
            {"name": "Gross margin", "points": _ramp(gm, 0.20, 0.65, 20), "max_points": 20,
             "detail": f"{gm*100:.0f}%" if gm is not None else "n/a"},
            {"name": "Operating margin", "points": _ramp(om, 0.05, 0.30, 25), "max_points": 25,
             "detail": f"{om*100:.0f}%" if om is not None else "n/a"},
            {"name": "Return on equity", "points": _ramp(roe, 0.05, 0.30, 20), "max_points": 20,
             "detail": f"{roe*100:.0f}%" if roe is not None else "n/a"},
            {"name": "FCF margin", "points": _ramp(fcfm, 2, 20, 20), "max_points": 20,
             "detail": f"{fcfm:.0f}% of revenue" if fcfm is not None else "n/a"},
            {"name": "Balance sheet", "points": _ramp(de, 200, 0, 15), "max_points": 15,
             "detail": f"debt/equity {de/100:.2f}x" if de is not None else "n/a"},
        ]
    score, cov, comps = _assemble(comps)
    return {"score": score, "coverage": cov, "components": comps}


# ─── Growth: revenue, earnings, acceleration ─────────────────────────────────
def _cyclical_haircut(v, suspect, extreme, floor=0.45):
    """Multiplier in [floor, 1.0]: 1.0 up to `suspect`, fading linearly to `floor`
    by `extreme`. Implausibly high YoY growth is almost always a cyclical trough
    rebound (base effect) — e.g. a memory/chip name printing +1000% EPS off a
    depressed prior year — NOT durable secular growth. We discount it instead of
    awarding full marks, so a cyclical at the bottom of its cycle can't top the
    growth factor on a number that won't repeat."""
    v = _num(v)
    if v is None or v <= suspect:
        return 1.0
    if v >= extreme:
        return floor
    frac = (v - suspect) / (extreme - suspect)
    return 1.0 - frac * (1.0 - floor)


def growth(fund: dict, archetype: str | None = None) -> dict:
    rev = _num(fund.get("revenue_growth"))
    eps = _num(fund.get("earnings_growth"))
    epsq = _num(fund.get("earnings_growth_q"))
    accel = (epsq - eps) if (epsq is not None and eps is not None) else None

    if archetype == "PRE_PROFIT":
        # Earnings growth / acceleration are meaningless without earnings — N/A
        # them (don't score a negative EPS-growth as 0). Score on revenue growth
        # PLUS revenue SCALE: a $1B-revenue pre-profit name (a real, scaling
        # business) is a different animal from a $5M pre-revenue concept, and
        # scale is the discipline that stops a +900%-off-nothing base from
        # crowning the growth factor.
        revlvl = _num(fund.get("total_revenue"))
        rev_mult_pp = _cyclical_haircut(rev, 0.75, 3.00)
        rev_pts_pp = _ramp(rev, 0.0, 0.40, 60)
        if rev_pts_pp is not None:
            rev_pts_pp *= rev_mult_pp
        comps = [
            {"name": "Revenue growth", "points": rev_pts_pp, "max_points": 60,
             "detail": (f"{rev*100:+.0f}% YoY" + (" (base-effect discounted)"
                        if (rev_pts_pp is not None and rev_mult_pp < 0.999) else ""))
                       if rev is not None else "n/a"},
            {"name": "Revenue scale", "points": _ramp(revlvl, 5e7, 2e9, 40), "max_points": 40,
             "detail": (f"${revlvl/1e9:.2f}B revenue" if (revlvl and revlvl >= 1e9)
                        else f"${revlvl/1e6:.0f}M revenue" if revlvl else "pre-revenue")},
        ]
        score, cov, comps = _assemble(comps)
        return {"score": score, "coverage": cov, "components": comps}

    # Cyclical / base-effect guard: discount implausibly high YoY growth.
    rev_pts = _ramp(rev, 0.0, 0.25, 45)
    eps_pts = _ramp(eps, 0.0, 0.30, 35)
    rev_mult = _cyclical_haircut(rev, 0.60, 2.00)   # +60% full → +200%+ floored
    eps_mult = _cyclical_haircut(eps, 1.00, 6.00)   # +100% full → +600%+ floored
    rev_cyc = rev_pts is not None and rev_mult < 0.999
    eps_cyc = eps_pts is not None and eps_mult < 0.999
    if rev_pts is not None:
        rev_pts *= rev_mult
    if eps_pts is not None:
        eps_pts *= eps_mult

    # Acceleration gets the SAME cyclical treatment: a trough-rebound quarter
    # (+1368% QoQ EPS off a depressed base) is a base effect, not acceleration —
    # discount it by how implausible the quarterly print is.
    acc_pts = _ramp(accel, -0.05, 0.10, 20)
    acc_mult = _cyclical_haircut(epsq, 1.00, 6.00)
    acc_cyc = acc_pts is not None and acc_mult < 0.999
    if acc_pts is not None:
        acc_pts *= acc_mult

    comps = [
        {"name": "Revenue growth", "points": rev_pts, "max_points": 45,
         "detail": (f"{rev*100:+.0f}% YoY" + (" (cyclical-discounted)" if rev_cyc else ""))
                   if rev is not None else "n/a"},
        {"name": "Earnings growth", "points": eps_pts, "max_points": 35,
         "detail": (f"{eps*100:+.0f}% YoY" + (" (cyclical-discounted)" if eps_cyc else ""))
                   if eps is not None else "n/a"},
        {"name": "Acceleration", "points": acc_pts, "max_points": 20,
         "detail": ("latest quarter "
                    + ("faster" if (accel or 0) > 0 else "slower") + " than trailing year"
                    + (" (cyclical-discounted)" if acc_cyc else ""))
                   if accel is not None else "n/a"},
    ]
    score, cov, comps = _assemble(comps)
    return {"score": score, "coverage": cov, "components": comps}


# ─── Value: cheapness vs sector + absolute + DCF ─────────────────────────────
def value(fund: dict, last: float | None, dcf: dict | None,
          sector_pe_med: float | None = None,
          sector_ev_med: float | None = None) -> dict:
    pe = _num(fund.get("forward_pe")) or _num(fund.get("pe"))
    ev = _num(fund.get("ev_ebitda"))
    fcfy = _num(fund.get("fcf_yield_pct"))
    peg = _num(fund.get("peg"))
    # Pre-profit / never-earned-money names (IONQ-type): traditional value metrics
    # (P/E, EV/EBITDA, FCF-yield, PEG, DCF) are MEANINGLESS, not "worst" — return the
    # whole factor N/A so it drops out of the coverage-weighted blend instead of
    # scoring a punitive 0 (which was crushing IONQ to ~38). The harder-to-value
    # nature lands on the CONFIDENCE axis (value absent → lower fundamental coverage),
    # which keeps it out of the top without misrepresenting it as a value stock.
    if (dcf or {}).get("archetype") == "PRE_PROFIT":
        return {"score": None, "coverage": 0.0,
                "components": [{"name": "Value", "points": None, "max_points": 100,
                                "detail": "n/a — pre-profit (no earnings/FCF to value on)"}]}
    # Only a TRUSTWORTHY DCF feeds the value factor: a blocked (wrong-tool) or
    # gate-flagged artifact upside is meaningless (that's exactly the IBKR +554%
    # / GPN +271% case), so it contributes n/a, not points.
    _d = dcf or {}
    _dcf_ok = _d.get("available") and _d.get("confidence") not in ("LOW/ARTIFACT", "N/A", None)
    dcf_up = _num(_d.get("upside_pct")) if _dcf_ok else None
    _dcf_detail = (f"{dcf_up:+.0f}% to intrinsic" if dcf_up is not None
                   else (f"{_d.get('method')} — {_d.get('confidence')}"
                         if _d.get("method") else str(_d.get("reason") or "n/a")[:32]))

    # P/E and EV/EBITDA: relative to sector median when we have it (cheaper than
    # peers == high points), else absolute fallbacks.
    if sector_pe_med and pe and pe > 0:
        pe_pts = _ramp(pe, 1.6 * sector_pe_med, 0.5 * sector_pe_med, 25)
        pe_det = f"{pe:.1f}× vs sector {sector_pe_med:.1f}×"
    else:
        pe_pts = _ramp(pe, 40, 8, 25) if (pe and pe > 0) else None
        pe_det = f"{pe:.1f}× fwd" if (pe and pe > 0) else "n/a"
    if sector_ev_med and ev and ev > 0:
        ev_pts = _ramp(ev, 1.6 * sector_ev_med, 0.5 * sector_ev_med, 20)
        ev_det = f"{ev:.1f}× vs sector {sector_ev_med:.1f}×"
    else:
        ev_pts = _ramp(ev, 25, 6, 20) if (ev and ev > 0) else None
        ev_det = f"{ev:.1f}× EV/EBITDA" if (ev and ev > 0) else "n/a"

    comps = [
        {"name": "P/E", "points": pe_pts, "max_points": 25, "detail": pe_det},
        {"name": "EV/EBITDA", "points": ev_pts, "max_points": 20, "detail": ev_det},
        {"name": "FCF yield", "points": _ramp(fcfy, 0, 7, 20), "max_points": 20,
         "detail": f"{fcfy:.1f}%" if fcfy is not None else "n/a"},
        {"name": "PEG", "points": (_ramp(peg, 3.0, 0.5, 15) if (peg and peg > 0) else None),
         "max_points": 15, "detail": f"{peg:.2f}" if (peg and peg > 0) else "n/a"},
        {"name": "DCF fair value", "points": _ramp(dcf_up, -25, 40, DCF_VALUE_MAX_POINTS),
         "max_points": DCF_VALUE_MAX_POINTS, "detail": _dcf_detail},
    ]
    score, cov, comps = _assemble(comps)
    # Low-confidence down-weight: when the DCF couldn't be computed (negative or
    # depressed-trough FCF, unit mismatch), the surviving value signal leans on
    # P/E-type ratios that are unreliable for a cyclical at a trough — exactly the
    # value-trap case. Cap coverage so the composite leans LESS on value here,
    # rather than treating a possible trap as a clean "cheap" buy.
    # When the DCF couldn't be trusted (blocked wrong-tool, or a gate-flagged
    # artifact), the surviving value read leans on P/E-type ratios that mislead
    # for the value-trap / can't-value-this case — cap coverage so the composite
    # leans LESS on value here rather than treating it as a clean signal.
    if dcf is not None and not _dcf_ok and cov:
        cov = round(min(cov, 0.6), 2)
    return {"score": score, "coverage": cov, "components": comps}


_CONSENSUS_PTS = {
    "strong_buy": 50, "buy": 40, "outperform": 40,
    "hold": 22, "neutral": 22, "underperform": 12,
    "sell": 3, "strong_sell": 3,
}


# ─── Analyst: Street consensus, target upside, coverage ──────────────────────
def analyst(fund: dict, last: float | None) -> dict:
    rec = (fund.get("recommendation") or "").strip().lower()
    n = _num(fund.get("num_analysts"))
    tgt = _num(fund.get("target_mean"))
    last = _num(last)
    upside = ((tgt - last) / last * 100) if (tgt and last) else None
    comps = [
        {"name": "Consensus", "points": (_CONSENSUS_PTS.get(rec) if rec in _CONSENSUS_PTS else None),
         "max_points": 50, "detail": rec.replace("_", " ") or "n/a"},
        {"name": "Target upside", "points": _ramp(upside, -10, 30, 35), "max_points": 35,
         "detail": f"{upside:+.0f}% to ${tgt:.0f} target" if upside is not None else "n/a"},
        {"name": "Coverage", "points": _ramp(n, 3, 25, 15), "max_points": 15,
         "detail": f"{int(n)} analysts" if n is not None else "n/a"},
    ]
    score, cov, comps = _assemble(comps)
    return {"score": score, "coverage": cov, "components": comps}


# ─── News / catalyst: recency-weighted headline tone (lexicon baseline) ──────
# Deliberately simple and low-weight: a free, instant read on the tape of
# recent headlines. The real qualitative read is the Ollama deep-dive (which
# already ingests these titles); this is just a quick directional tone.
_POS = {
    "beat", "beats", "surge", "surges", "soar", "soars", "jump", "jumps", "rally",
    "rallies", "upgrade", "upgraded", "raise", "raises", "raised", "record", "strong",
    "growth", "outperform", "bullish", "tops", "gain", "gains", "wins", "win",
    "approval", "approved", "expand", "expands", "boost", "boosts", "higher", "profit",
    "profits", "upbeat", "optimistic", "milestone", "breakthrough", "partnership",
    "dividend", "buyback", "rebound", "accelerate", "accelerates",
}
_NEG = {
    "miss", "misses", "missed", "plunge", "plunges", "drop", "drops", "fall", "falls",
    "slump", "slumps", "downgrade", "downgraded", "cut", "cuts", "lawsuit", "probe",
    "investigation", "fraud", "recall", "warning", "warns", "weak", "loss", "losses",
    "decline", "declines", "bearish", "slash", "slashes", "layoff", "layoffs",
    "bankruptcy", "default", "halt", "halts", "plummet", "plummets", "scandal", "fine",
    "fined", "sue", "sues", "sued", "concern", "concerns", "slowdown", "disappoint",
    "disappoints", "subpoena", "delay", "delays", "tumble", "tumbles", "sinks", "sink",
}


def news(titles: list[str]) -> dict:
    """Headline-tone sentiment over recent titles (newest first). Returns
    {score 0-100 (50=neutral), coverage, components, summary, label}."""
    titles = [t for t in (titles or []) if t]
    if not titles:
        return {"score": None, "coverage": 0.0, "components": [],
                "summary": "no recent headlines", "label": "n/a"}
    pos = neg = 0.0
    for i, t in enumerate(titles[:12]):
        w = 1.0 / (1 + 0.3 * i)                  # recency decay
        words = re.findall(r"[a-z']+", t.lower())
        pos += sum(1 for x in words if x in _POS) * w
        neg += sum(1 for x in words if x in _NEG) * w
    total = pos + neg
    if total == 0:
        score, label = 50, "neutral"
    else:
        ratio = (pos - neg) / total              # -1..1
        score = int(round(50 + ratio * 40))
        label = "positive" if ratio > 0.2 else "negative" if ratio < -0.2 else "mixed"
    coverage = min(1.0, len(titles) / 5)
    summary = f"{len(titles)} recent headlines read {label}"
    # points = the actual tone score: the WHY row used to hardcode None, which
    # rendered as a blank "−/100" next to a real-looking summary.
    return {"score": score, "coverage": round(coverage, 2),
            "components": [{"name": "Headline tone", "points": score, "max_points": 100,
                            "detail": summary}],
            "summary": summary, "label": label}


# ─── Composite: coverage-weighted blend over available factors ───────────────
# Tape-side factors (price action + Street sentiment). Their say in the composite
# is CAPPED at their nominal share: when fundamental evidence is missing, the
# freed weight must NOT flow to the tape — that's precisely the setup where a
# parabolic, analyst-euphoric cyclical (no DCF, depressed FCF) gets crowned by
# momentum. Missing-fundamentals weight redistributes only among the fundamental
# factors that ARE present; if none are, the tape alone scores it and the
# confidence gate (below) keeps it out of Buy/Strong and the top of the list.
_TAPE_FACTORS = ("momentum", "analyst", "news", "ai")


# ─── AI read ──────────────────────────────────────────────────────────────────
# The `ai` factor is built by adapters/rh_ai_reads.factor(): a weekly Claude read
# of every name, as its percentile across the universe. It is a CAPPED tape
# factor at weight 5, so it can move a composite by about two and a half points
# either way and never drive a name on its own. Kept out of
# fundamental_coverage() so a missing read never lowers confidence.


def composite(factors: dict) -> tuple[int | None, dict]:
    """factors = {name: {score, coverage}} (name in WEIGHTS). Returns
    (composite_0_100, weight_breakdown). Each factor's weight is scaled by its
    coverage, renormalized over whatever was available — but tape factors
    (momentum/analyst/news) never exceed their nominal weight share."""
    eff: dict = {}
    for name, w in WEIGHTS.items():
        fac = factors.get(name) or {}
        score = fac.get("score")
        cov = fac.get("coverage", 1.0 if score is not None else 0.0)
        if score is None or cov <= 0:
            continue
        eff[name] = w * cov
    if not eff:
        return None, {}
    wsum = sum(eff.values())
    pct = {n: e / wsum * 100.0 for n, e in eff.items()}

    # Cap each tape factor at its nominal share; hand the trimmed excess back to
    # the fundamental factors (pro-rata). Tape-only names just renormalize below.
    nominal_total = sum(WEIGHTS.values()) or 1
    excess = 0.0
    for n in _TAPE_FACTORS:
        if n in pct:
            cap = WEIGHTS[n] / nominal_total * 100.0
            if pct[n] > cap:
                excess += pct[n] - cap
                pct[n] = cap
    fund_present = [n for n in pct if n not in _TAPE_FACTORS]
    if excess > 0 and fund_present:
        fsum = sum(pct[n] for n in fund_present)
        for n in fund_present:
            pct[n] += excess * ((pct[n] / fsum) if fsum > 0 else 1.0 / len(fund_present))
    total = sum(pct.values())
    if total <= 0:
        return None, {}
    score = sum(pct[n] * ((factors[n] or {}).get("score") or 0) for n in pct) / total
    parts = {n: {"weight_pct": round(pct[n] / total * 100, 1),
                 "score": (factors[n] or {}).get("score")} for n in pct}
    return int(round(score)), parts


def fundamental_coverage(factors: dict) -> float:
    """How much of the FUNDAMENTAL evidence (value/quality/growth/analyst) is
    actually present, 0-1. Low == the name is being scored mostly on momentum."""
    num = den = 0.0
    for name in ("value", "quality", "growth", "analyst"):
        w = WEIGHTS[name]
        den += w
        fac = factors.get(name) or {}
        if fac.get("score") is not None:
            num += w * (fac.get("coverage") or 0.0)
    return (num / den) if den else 0.0


def confidence(factors: dict, dcf: dict | None = None,
               parabolic: bool = False) -> tuple[float, str, list[str]]:
    """How much to TRUST the composite (0-1) — separate from how GOOD the company
    scores. Starts from fundamental coverage and subtracts explicit, explainable
    penalties: no DCF (can't value it), cyclical-discounted growth (base-effect
    numbers that won't repeat), and a parabolic tape (momentum that tends to mean-
    revert). Returns (confidence, label, reasons) — reasons render in the UI so a
    downgrade is never a black box."""
    conf = fundamental_coverage(factors)
    why: list[str] = []
    if dcf is not None:
        _reason = str(dcf.get("reason") or "")
        if "awaiting schema refresh" in _reason:
            # Transient: the fundamentals cache is mid re-warm after a schema bump.
            # Don't penalize confidence for a temporary state — it heals in a cycle.
            pass
        elif not dcf.get("available"):
            conf -= 0.15
            why.append("DCF blocked/unavailable — "
                       + (_reason or "insufficient FCF")[:60])
        elif dcf.get("confidence") == "LOW/ARTIFACT":
            # a RED valuation gate fired — can't print high confidence (blueprint §8)
            conf -= 0.20
            _red = next((g.get("reason") for g in (dcf.get("gates") or [])
                         if g.get("level") == "RED"), "a valuation gate fired")
            why.append("DCF is a screen artifact — " + str(_red)[:60])
        elif dcf.get("confidence") not in ("N/A", None):
            # Signal disagreement: a TRUSTED DCF that says the name is materially
            # OVERVALUED contradicts a bullish composite (the NTAP case — multi-
            # factor 66/BUY while intrinsic value sat 28% below price). Conflicting
            # trusted signals = lower conviction, same as an analyst would treat it.
            _up = _num(dcf.get("upside_pct"))
            if _up is not None and _up < -15:
                conf -= 0.10
                why.append(f"trusted DCF says overvalued ({_up:.0f}% to intrinsic)")
    g_comps = (factors.get("growth") or {}).get("components") or []
    if any("cyclical-discounted" in (c.get("detail") or "") for c in g_comps):
        conf -= 0.10
        why.append("growth is base-effect / cyclical-discounted")
    if parabolic:
        conf -= 0.20
        why.append("parabolic tape — 3m return and/or Sharpe at unsustainable extremes")
    conf = max(0.0, min(1.0, conf))
    label = "high" if conf >= 0.75 else "medium" if conf >= 0.5 else "low"
    return round(conf, 2), label, why


def rank_score(score: int | None, conf: float) -> float:
    """Ordering key for the research table: conviction-adjusted score. A full-
    confidence name keeps 100% of its composite; a low-confidence one is ranked
    as if it scored ~70% of it. The displayed score is NOT changed — only where
    the name sits in the list (and whether it can crown the top)."""
    if score is None:
        return -1.0
    return round(score * (0.70 + 0.30 * max(0.0, min(1.0, conf))), 1)


def tier(score: int | None, conf: float = 1.0) -> str:
    """Tier from composite + CONFIDENCE (0-1; pass rh_factors.confidence()[0],
    or fundamental coverage as a weaker proxy). Low confidence cannot claim a
    Buy/Strong badge — that's the value-trap / momentum-crown case."""
    if score is None:
        return "pass"
    base = ("strong" if score >= 70 else "buy" if score >= 58 else
            "watch" if score >= 45 else "pass")
    if conf < 0.5 and base in ("strong", "buy"):
        return "watch"
    return base
