"""
strategies/alpha_portfolio.py

Target-portfolio construction for the long-only "beat the S&P 500" trader. PURE:
no I/O, no network, no Alpaca — given a research ranking, current prices, and total
capital, it returns target weights / dollars / shares. The same function is used by
the live runner (`trading/alpha_runner.py`) and the backtest (`jobs/alpha_backtest.py`),
so what we backtest is exactly what we deploy.

Posture — "active conviction" (see config.settings.ALPHA):
  • Core sleeve     — a SMALL broad-market anchor (VOO/QQQ, ~12% of the equity book).
                      Deliberately small: you can't beat SPY by mostly holding SPY.
  • Satellite       — a diversified, conviction-weighted book of the top research names
                      (undervalued/DCF, growth, quality — all from research.json),
                      weighted ∝ composite score TILTED UP by DCF upside, with a hard
                      per-name and per-sector cap so no idea or sector dominates.
  • Reserve sleeve  — a DYNAMIC T-bill (BIL) buffer: dry powder that grows when good
                      buys are scarce and shrinks (deploys into equities) when the
                      screen surfaces many "extremely good buys".
  • Cash            — a tiny uninvested slice for settlement/slippage slack.

No leverage, long-only, weights sum to ≤ 1. This module never decides to *trade* or
*sell* an existing holding; it only describes the desired end state. The runner diffs
that against live positions (drift band) and runs its own sell-discipline layer.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

try:
    from config.settings import ALPHA
except Exception:  # pragma: no cover - allow import in bare contexts
    ALPHA = {}


@dataclass
class RankedName:
    """One candidate from the research engine (or the backtest's price proxy).

    The fundamental fields (`dcf_upside`, factor sub-scores, `shortlisted`) default to
    0/False so the price-only backtest can construct RankedNames with just a score and
    fall back cleanly to score-weighting.
    """
    symbol: str
    score: float
    sector: str = "Unknown"
    tier: Optional[str] = None
    is_etf: bool = False
    dcf_upside: float = 0.0          # DCF upside % to intrinsic value (0 if unknown)
    dcf_trusted: bool = False        # True only for a usable gated DCF (not LOW/ARTIFACT/N/A); no DCF → nothing to act on
    value_score: float = 0.0         # factor sub-scores (for style tagging / display)
    growth_score: float = 0.0
    quality_score: float = 0.0
    shortlisted: bool = False        # in any research shortlist (value/dcf/growth/quality)


@dataclass
class TargetBook:
    weights: dict = field(default_factory=dict)     # symbol -> fraction of capital
    dollars: dict = field(default_factory=dict)     # symbol -> target $ exposure
    shares: dict = field(default_factory=dict)      # symbol -> target (fractional) shares
    sleeves: dict = field(default_factory=dict)     # {"core","satellite","reserve","cash"}
    selected: list = field(default_factory=list)    # satellite symbols chosen, in rank order
    styles: dict = field(default_factory=dict)      # symbol -> "value"|"growth"|"quality"|"balanced"
    reserve_frac: float = 0.0                        # BIL sleeve as a fraction of capital
    opportunity_count: int = 0                       # # of "extremely good buys" this cycle
    notes: list = field(default_factory=list)


# ─── capped, redistributing normalization ─────────────────────────────────────

def _cap_and_redistribute(weights: dict, cap: float, iters: int = 8) -> dict:
    """Scale `weights` (sum→1) so no element exceeds `cap`, pushing the excess onto
    the uncapped elements proportionally. Idempotent once feasible.

    Infeasible case (cap·n ≤ 1 — too FEW names to reach sum=1 under the cap): cap each
    element at the ceiling and return a sub-1 sum. The caller absorbs the shortfall
    (rolls it into the reserve / cash) rather than letting a handful of names blow past
    the cap — this fixes the old degeneracy where 4 names became ~11% each.
    """
    if not weights:
        return {}
    total = sum(weights.values())
    if total <= 0:
        return {k: 0.0 for k in weights}
    w = {k: v / total for k, v in weights.items()}          # normalize to 1
    if cap * len(w) <= 1.0:
        return {k: min(v, cap) for k, v in w.items()}       # infeasible → cap, underfill
    for _ in range(iters):
        over = {k: v for k, v in w.items() if v > cap + 1e-12}
        if not over:
            break
        excess = sum(v - cap for v in over.values())
        for k in over:
            w[k] = cap
        free = {k: v for k, v in w.items() if v < cap - 1e-12}
        free_total = sum(free.values())
        if free_total <= 0:
            break
        for k in free:
            w[k] += excess * (free[k] / free_total)
    return w


def _apply_sector_cap(weights: dict, sectors: dict, cap: float, iters: int = 8) -> dict:
    """Scale weights so no single sector exceeds `cap` of the (satellite) total,
    redistributing onto under-cap sectors proportionally."""
    if not weights:
        return {}
    w = dict(weights)
    for _ in range(iters):
        by_sec: dict = {}
        for sym, wt in w.items():
            s = sectors.get(sym, "Unknown")
            by_sec[s] = by_sec.get(s, 0.0) + wt
        over = {s: tot for s, tot in by_sec.items() if tot > cap + 1e-9}
        if not over:
            break
        freed = 0.0
        for s, tot in over.items():
            scale = cap / tot
            for sym in [k for k in w if sectors.get(k, "Unknown") == s]:
                freed += w[sym] * (1 - scale)
                w[sym] *= scale
        under_syms = [k for k in w if by_sec.get(sectors.get(k, "Unknown"), 0.0) < cap - 1e-9]
        base = sum(w[k] for k in under_syms)
        if base <= 0 or freed <= 0:
            break
        for k in under_syms:
            w[k] += freed * (w[k] / base)
    return w


# ─── conviction / reserve / style helpers ─────────────────────────────────────

def research_exit_reason(n: RankedName, cfg: dict) -> Optional[str]:
    """Why the runner's sell discipline would exit this name on research alone, else None.

    Mirrors the research rules in trading.alpha_runner.decide_sells (broken thesis and
    DCF exhausted; stop-loss and take-profit depend on the position, not the research).
    build_target never buys a name this rejects: it would be bought on one run and sold
    on the next — NXPI (2026-11-16) and CPAY (2026-11-18) were bought with a trusted
    negative DCF and sold three hours later as "thesis broken (DCF turned negative)".
    tests/test_alpha_incumbency.py holds the two in step."""
    if n.tier in tuple(cfg.get("thesis_exit_tiers", ("pass", "avoid"))):
        return f"tier → {n.tier}"
    min_score = float(cfg.get("thesis_exit_min_score", 45))
    if n.score < min_score:
        return f"score {n.score:.0f} < {min_score:.0f}"
    if n.dcf_trusted:
        if n.dcf_upside < 0:
            return "DCF turned negative"
        if n.dcf_upside <= float(cfg.get("dcf_exhausted_upside", 5.0)):
            return f"DCF exhausted ({n.dcf_upside:.0f}% upside left)"
    return None


def _conviction(n: RankedName, scheme: str, dcf_tilt: float) -> float:
    """Conviction weight for a name: composite score TILTED UP by DCF upside.

    A name that is both high-quality (high score) AND deeply undervalued (high DCF
    upside) gets the most capital. DCF contribution is clamped at +150% upside so a
    single extreme reverse-DCF can't dominate the book. `scheme="equal"` bypasses it.
    """
    if scheme == "equal":
        return 1.0
    base = max(float(n.score), 1e-6)
    up = n.dcf_upside if n.dcf_trusted else 0.0     # never tilt on an artifact DCF
    tilt = 1.0 + dcf_tilt * min(max(up, 0.0) / 100.0, 1.5)
    return base * tilt


def _reserve_fraction(opp_count: int, floor: float, ceiling: float,
                      lean: int, rich: int) -> float:
    """Dynamic reserve size: ceiling when good buys are scarce (≤ lean), floor when
    plentiful (≥ rich), linearly interpolated in between. More opportunities → less
    dry powder (deploy into equities); fewer → more BIL."""
    if ceiling < floor:
        ceiling = floor
    if opp_count <= lean:
        return ceiling
    if opp_count >= rich:
        return floor
    t = (opp_count - lean) / max(1, (rich - lean))
    return ceiling + t * (floor - ceiling)


def _style(n: RankedName) -> str:
    """Dominant style tag for display: value / growth / quality, else balanced.

    Purely cosmetic (helps the user SEE the diversification); never affects selection
    or weighting. Empty when no factor sub-scores are present (e.g. the backtest)."""
    trio = {"value": n.value_score, "growth": n.growth_score, "quality": n.quality_score}
    if max(trio.values()) <= 0:
        return ""
    top = max(trio, key=trio.get)
    others = [v for k, v in trio.items() if k != top]
    if trio[top] >= 60 and trio[top] - (sum(others) / 2) >= 8:
        return top
    return "balanced"


# ─── main entry point ──────────────────────────────────────────────────────────

def build_target(
    rankings: list,
    prices: dict,
    capital: float,
    cfg: Optional[dict] = None,
    held: Optional[set] = None,
    locked: Optional[set] = None,
) -> TargetBook:
    """Construct the target core-satellite-reserve book.

    Args:
        rankings : list[RankedName | dict] from the research engine, any order.
        prices   : {symbol: last_price} — names without a price are skipped. Must
                   include the core ETFs and the reserve ETF for those sleeves to fund.
        capital  : total account equity in $ (cash + positions).
        cfg      : effective ALPHA config (already policy-overlaid by the caller;
                   `load_alpha_policy(ALPHA)` upstream). Falls back to ALPHA defaults.
        held     : satellite names the book already owns. A held name keeps its slot
                   while it ranks within `hold_rank_ratio` × the satellite size; a new
                   name must rank inside the satellite size itself (see below).
        locked   : held names still inside the minimum holding period. They stay in
                   the book whatever their rank this run. Both default to empty, which
                   is the stateless construction (every slot re-picked from scratch).

    Returns a TargetBook (weights/dollars/shares are fractions/$ of `capital`).
    """
    c = {**ALPHA, **(cfg or {})}
    cash_buffer = float(c.get("cash_buffer", 0.01))
    core_weight = float(c.get("core_weight", 0.12))
    core_etfs = dict(c.get("core_etfs", {"VOO": 0.6, "QQQ": 0.4}))
    max_names = int(c.get("satellite_max_names", 22))
    min_score = float(c.get("satellite_min_score", 55))
    watch_min = float(c.get("watch_min_score", 68))
    eligible_tiers = tuple(c.get("eligible_tiers", ()) or ())
    scheme = c.get("weight_scheme", "conviction")
    dcf_tilt = float(c.get("dcf_tilt", 0.5))
    name_cap = float(c.get("max_position_weight", 0.08))
    sector_cap = float(c.get("max_sector_weight", 0.30))
    reserve_etf = str(c.get("reserve_etf", "BIL")).upper()
    reserve_floor = float(c.get("reserve_floor", 0.03))
    reserve_ceiling = float(c.get("reserve_ceiling", 0.18))
    opp_score = float(c.get("opportunity_score_min", 72))
    opp_dcf = float(c.get("opportunity_dcf_min", 35.0))
    opp_rich = int(c.get("opportunity_rich_count", 12))
    opp_lean = int(c.get("opportunity_lean_count", 3))

    book = TargetBook()

    # normalize incoming rankings to RankedName
    norm: list[RankedName] = []
    for r in rankings:
        if isinstance(r, RankedName):
            norm.append(r)
        else:
            norm.append(RankedName(
                symbol=str(r.get("symbol", "")).upper(),
                score=float(r.get("score") or 0),
                sector=r.get("sector") or "Unknown",
                tier=r.get("tier"),
                is_etf=bool(r.get("is_etf", False)),
                dcf_upside=float(r.get("dcf_upside") or 0),
                dcf_trusted=bool(r.get("dcf_trusted", False)),
                value_score=float(r.get("value_score") or 0),
                growth_score=float(r.get("growth_score") or 0),
                quality_score=float(r.get("quality_score") or 0),
                shortlisted=bool(r.get("shortlisted", False)),
            ))

    core_syms = {s.upper() for s in core_etfs}
    reserved = core_syms | {reserve_etf}

    # ── Dynamic reserve size (from the opportunity count over the whole screen) ──
    opp_count = sum(
        1 for n in norm
        if not n.is_etf and n.symbol not in reserved
        and n.score >= opp_score and n.dcf_trusted and n.dcf_upside >= opp_dcf
    )
    book.opportunity_count = opp_count
    reserve_frac = _reserve_fraction(opp_count, reserve_floor, reserve_ceiling, opp_lean, opp_rich)
    reserve_frac = max(0.0, min(reserve_frac, 1.0 - cash_buffer))
    reserve_present = bool(prices.get(reserve_etf) and prices[reserve_etf] > 0)
    if not reserve_present and reserve_frac > 0:
        book.notes.append(f"no {reserve_etf} price — reserve held as cash this cycle")

    # Split the non-reserve, non-cash capital into core + satellite.
    equity_frac = max(0.0, 1.0 - cash_buffer - reserve_frac)
    core_frac_total = equity_frac * core_weight
    sat_frac_total = equity_frac * (1.0 - core_weight)

    # ── Satellite selection ───────────────────────────────────────────────────
    def _eligible(n: RankedName) -> bool:
        if n.is_etf or n.symbol in reserved:
            return False
        if not (prices.get(n.symbol) and prices[n.symbol] > 0):
            return False
        if research_exit_reason(n, c):   # the sell layer would exit it next run — even a shortlist leader
            return False
        gate = (
            n.score >= min_score
            and (not eligible_tiers or n.tier is None or n.tier in eligible_tiers)
            and (n.tier != "watch" or n.score >= watch_min)
        )
        return gate or n.shortlisted   # shortlist leaders are always candidates

    def _conv(n: RankedName) -> tuple:
        return (-_conviction(n, scheme, dcf_tilt), n.symbol)

    elig = [n for n in norm if _eligible(n)]
    elig.sort(key=_conv)
    # Diversify at SELECTION time, not just by weight: cap names per sector so the book
    # can't become 14 tiny financials just because that sector screens cheap. Highest
    # conviction still wins within each sector; the overflow slots go to the next sector
    # down the ranking (real spread, small conviction cost — those names were sub-cap anyway).
    sector_name_cap = max(3, round(max_names * sector_cap))
    picked: list[RankedName] = []
    taken: set = set()
    sec_count: dict = {}

    def _take(n: RankedName) -> None:
        picked.append(n)
        taken.add(n.symbol)
        sec_count[n.sector] = sec_count.get(n.sector, 0) + 1

    # Incumbency. Re-picking every slot from scratch each run churned the book: ~140
    # names qualify for ~22 slots, conviction is dense near the cut, and research is
    # rebuilt twice a day, so names around slot 22 swapped in and out — 183 of 321
    # positions (Jul–Nov 2026) were closed within 3 days of opening; HAS was bought at
    # 11:45 and sold at 14:45 ET on 2026-11-16 while rated strong/buy throughout.
    #   1. locked — held inside the minimum holding period: kept whatever their rank
    #      (a priced research row the sell rules don't reject is enough). The runner's
    #      stop-loss and broken-thesis exits run after this and still sell them.
    #   2. held  — kept while ranked within hold_rank_ratio × the satellite size.
    #   3. fill  — remaining slots from the top of the ranking, as before.
    locked_set = {s.upper() for s in (locked or ())}
    held_set = {s.upper() for s in (held or ())} | locked_set
    for n in sorted((n for n in norm if n.symbol in locked_set), key=_conv):
        if n.symbol not in taken and not n.is_etf and n.symbol not in reserved \
                and prices.get(n.symbol) and prices[n.symbol] > 0 \
                and not research_exit_reason(n, c):    # the runner exits these anyway
            _take(n)
    keep_rank = max(max_names, round(max_names * float(c.get("hold_rank_ratio", 1.0))))
    for n in elig[:keep_rank]:
        if len(picked) >= max_names:
            break
        if n.symbol in held_set and n.symbol not in taken \
                and sec_count.get(n.sector, 0) < sector_name_cap:
            _take(n)
    for n in elig:
        if len(picked) >= max_names:
            break
        if n.symbol in taken or sec_count.get(n.sector, 0) >= sector_name_cap:
            continue
        _take(n)
    picked.sort(key=_conv)
    entry_cut = {n.symbol for n in elig[:max_names]}
    kept_low = [n.symbol for n in picked if n.symbol in held_set and n.symbol not in entry_cut]
    if kept_low:
        book.notes.append("held, kept below the entry cut (incumbency band / minimum hold): "
                          + ", ".join(kept_low))
    book.selected = [n.symbol for n in picked]
    book.styles = {n.symbol: _style(n) for n in picked}

    sat_weights: dict = {}
    if picked:
        raw = {n.symbol: _conviction(n, scheme, dcf_tilt) for n in picked}
        sectors = {n.symbol: n.sector for n in picked}
        name_cap_sleeve = min(1.0, name_cap / sat_frac_total) if sat_frac_total > 0 else 1.0
        sector_cap_sleeve = min(1.0, sector_cap / sat_frac_total) if sat_frac_total > 0 else 1.0
        capped = _cap_and_redistribute(raw, name_cap_sleeve)
        capped = _apply_sector_cap(capped, sectors, sector_cap_sleeve)
        capped = _cap_and_redistribute(capped, name_cap_sleeve)  # re-tighten names
        sat_weights = {sym: w * sat_frac_total for sym, w in capped.items()}
    else:
        book.notes.append("no eligible satellite names — anchor + reserve only this cycle")

    # Any satellite budget we couldn't deploy (too few names to fill under the cap)
    # becomes extra dry powder in the reserve (or cash if BIL is unpriced).
    sat_used = sum(sat_weights.values())
    shortfall = max(0.0, sat_frac_total - sat_used)
    if shortfall > 1e-9:
        if reserve_present:
            reserve_frac += shortfall
        book.notes.append(f"satellite underfilled by {shortfall*100:.1f}% → reserve/cash")

    # ── Core sleeve ─────────────────────────────────────────────────────────────
    core_weights: dict = {}
    core_present = {s.upper(): w for s, w in core_etfs.items()
                    if prices.get(s.upper()) and prices[s.upper()] > 0}
    core_norm = sum(core_present.values()) or 1.0
    for sym, w in core_present.items():
        core_weights[sym] = core_frac_total * (w / core_norm)
    if not core_present:
        book.notes.append("no core ETF prices available — core sleeve skipped")

    # ── Reserve sleeve ──────────────────────────────────────────────────────────
    reserve_weights: dict = {}
    if reserve_present and reserve_frac > 0:
        reserve_weights[reserve_etf] = reserve_frac
    book.reserve_frac = sum(reserve_weights.values())

    # ── Combine ───────────────────────────────────────────────────────────────
    weights: dict = {}
    for sleeve in (core_weights, sat_weights, reserve_weights):
        for sym, w in sleeve.items():
            weights[sym] = weights.get(sym, 0.0) + w

    invested = sum(weights.values())
    book.weights = weights
    book.dollars = {s: w * capital for s, w in weights.items()}
    book.shares = {s: (w * capital) / prices[s] for s, w in weights.items() if prices.get(s)}
    book.sleeves = {
        "core": sum(core_weights.values()),
        "satellite": sat_used,
        "reserve": book.reserve_frac,
        "cash": max(0.0, 1.0 - invested),
    }
    return book
