"""Market-regime detection — pulls VIX and classifies the environment.

Selling puts works great in calm markets and blows up in panicky ones. This
module gives the trader and scorer a single signal — `regime` — that they
can branch on.

VIX source order (first that works):
  1. CBOE's public delayed quote — the official index, free, no key (~15-min lag)
  2. yfinance ^VIX — under a hard wall-clock bound: 0.2.40 can hang indefinitely
  3. FRED VIXCLS — the official CLOSE, no key, one business day behind
  4. SPY 20-day realized vol x 1.15 from Alpaca bars — a PROXY, last resort

FRED was added 2026-11-20: CBOE had started answering this server with a 403 and
yfinance was failing, so every run was gating on the proxy. A day-old official
close is a much better gate than a stand-in that reads about half the index.

Why this order (measured 2026-09-11). yfinance used to lead and had been failing
silently, so the regime ran on the proxy — which reads ~0.55x the real index
(logged 9.44 vs CBOE close 17.84 on 09-10; three days below the all-time closing
low). With caution at 25 and pause at 35, a real VIX of 35 would have read ~19:
the gates would stay silent exactly when they matter. Then on 09-11 an Alpaca
504 knocked out the proxy too and the book dropped to half sizing.

Finnhub is deliberately NOT in the chain: the free plan answers ^VIX with
"Market data subscription required for CFD indices" and VIX with c=0, so it can
never supply a value — listing it was false redundancy. Every read records its
source (vix_source()), and a proxy read says so in the regime reason.
"""
from __future__ import annotations

import time
from typing import Optional

from loguru import logger

from config.settings import MARKET_REGIME

_cache: dict = {"value": None, "ts": 0, "source": None}

_CBOE_VIX_URL = "https://cdn.cboe.com/api/global/delayed_quotes/quotes/_VIX.json"
_YF_TIMEOUT_SEC = 10.0
_FRED_VIX_URL = "https://fred.stlouisfed.org/graph/fredgraph.csv"
_FRED_MAX_AGE_DAYS = 5      # a close older than a long weekend is not "current"


def _fetch_vix_cboe() -> Optional[float]:
    """The official VIX from CBOE's public delayed-quote feed (no key needed)."""
    try:
        import requests
        r = requests.get(_CBOE_VIX_URL, timeout=8,
                         headers={"User-Agent": "Mozilla/5.0 (forge market-regime)"})
        if r.status_code != 200:
            return None
        v = float(((r.json() or {}).get("data") or {}).get("current_price") or 0)
        return v if 1.0 < v < 150.0 else None
    except Exception as e:
        logger.debug(f"[regime] CBOE VIX failed: {e}")
        return None


def _fetch_vix_yfinance() -> Optional[float]:
    try:
        import yfinance as yf
        ticker = yf.Ticker("^VIX")
        hist = ticker.history(period="2d")
        if hist is None or hist.empty:
            return None
        return float(hist["Close"].iloc[-1])
    except Exception as e:
        logger.debug(f"[regime] yfinance VIX failed: {e}")
        return None


def _parse_fred_vix(text: str, today) -> Optional[float]:
    """The newest close in a FRED VIXCLS csv, or None when there is none, it is
    out of range, or it is more than _FRED_MAX_AGE_DAYS old. FRED writes "." for
    a day with no observation."""
    import datetime
    last = None
    for line in (text or "").splitlines()[1:]:
        day, _, val = line.partition(",")
        try:
            last = (datetime.date.fromisoformat(day.strip()), float(val))
        except ValueError:
            continue
    if last is None or (today - last[0]).days > _FRED_MAX_AGE_DAYS:
        return None
    return last[1] if 1.0 < last[1] < 150.0 else None


def _fetch_vix_fred() -> Optional[float]:
    """The official VIX close from FRED: keyless, but one business day behind."""
    try:
        import datetime
        import requests
        today = datetime.date.today()
        r = requests.get(_FRED_VIX_URL, timeout=8,
                         params={"id": "VIXCLS", "cosd": (today - datetime.timedelta(days=14)).isoformat()})
        return _parse_fred_vix(r.text, today) if r.status_code == 200 else None
    except Exception as e:
        logger.debug(f"[regime] FRED VIX failed: {e}")
        return None


def _fetch_vix_spy_proxy() -> Optional[float]:
    """
    Last-resort VIX proxy: SPY's 20-day annualized realized volatility from
    Alpaca bars, scaled up by 1.15 (implied vol usually sits above realized).

    It is a stand-in, not a measurement. Against the real index it has read
    about 0.55x in calm conditions (see the module docstring), so a regime
    built on it understates stress. That is why it comes last, and why a read
    from it is named in the regime reason.
    """
    try:
        from data.alpaca_options_client import AlpacaOptionsClient
        c = AlpacaOptionsClient()
        bars = c.get_year_bars("SPY")
        if not bars or len(bars) < 22:
            return None
        closes = [float(b["c"]) for b in bars[-22:]]
        returns = [(closes[i] - closes[i-1]) / closes[i-1] for i in range(1, len(closes))]
        if not returns:
            return None
        mean = sum(returns) / len(returns)
        var = sum((r - mean) ** 2 for r in returns) / len(returns)
        hv20_annualized = (var ** 0.5) * (252 ** 0.5) * 100  # % annualized
        # VIX typically ~115% of SPY HV20 (vol premium)
        return hv20_annualized * 1.15
    except Exception as e:
        logger.debug(f"[regime] SPY-HV proxy failed: {e}")
        return None


def _fetch_vix_yfinance_bounded() -> Optional[float]:
    """yfinance under a hard wall-clock bound. A DAEMON thread, not an executor:
    executor workers are joined at interpreter exit, so a fetch that never
    returns would hang the wheel run's exit instead of its regime check."""
    import threading
    box: dict = {}
    t = threading.Thread(target=lambda: box.__setitem__("v", _fetch_vix_yfinance()),
                         daemon=True)
    t.start()
    t.join(_YF_TIMEOUT_SEC)
    if t.is_alive():
        logger.warning(f"[regime] yfinance VIX hung >{_YF_TIMEOUT_SEC:.0f}s — skipped")
        return None
    return box.get("v")


def get_vix() -> Optional[float]:
    """Cached VIX read. None only if every source fails — classify_regime then
    halves sizing and holds the default reserve floor (deliberately not fail-open)."""
    ttl = MARKET_REGIME.get("cache_ttl_seconds", 600)
    if _cache["value"] is not None and (time.time() - _cache["ts"]) < ttl:
        return _cache["value"]

    # Resolved at call time (not a module-level tuple) so tests can patch sources.
    for source, fetcher in (("cboe", _fetch_vix_cboe),
                            ("yfinance", _fetch_vix_yfinance_bounded),
                            ("fred_prev_close", _fetch_vix_fred),
                            ("spy_hv_proxy", _fetch_vix_spy_proxy)):
        val = fetcher()
        if val and val > 0:
            _cache.update(value=val, ts=time.time(), source=source)
            return val
    _cache["source"] = None
    return None


def vix_source() -> Optional[str]:
    """Where the cached VIX came from: 'cboe', 'yfinance', 'fred_prev_close'
    or 'spy_hv_proxy'."""
    return _cache.get("source")


def classify_regime(vix: Optional[float] = None) -> dict:
    """The regime dict (see _classify) plus "vix_source". A proxy read is called
    out in the reason — it understates the real index, so the gates are weaker."""
    src = "provided" if vix is not None else None
    if vix is None:
        vix = get_vix()
        src = vix_source()
    out = _classify(vix)
    out["vix_source"] = src
    if src == "spy_hv_proxy":
        out["reason"] += " · VIX is a realized-vol PROXY (reads ~0.55x the real index)"
    elif src == "fred_prev_close":
        out["reason"] += " · VIX is the previous close (live sources unavailable)"
    return out


def _classify(vix: Optional[float]) -> dict:
    """
    Returns:
        {
            "vix": float | None,
            "regime": "calm" | "caution" | "paused" | "extreme",
            "qty_multiplier": 1.0 | 0.5 | 0.0,
            "allow_new_opens": bool,
            "force_close_speculative": bool,
            "reason": str,
        }
    """
    if vix is None:
        # NOT fail-open: a VIX outage is positively correlated with market stress,
        # so "blind" must not mean "calm". Half sizing, opens still allowed, and
        # the regime key "unknown" deliberately misses base_floor_by_regime so the
        # adaptive reserve holds default_floor (20%) instead of easing cash out
        # while the system can't see volatility.
        return {
            "vix": None, "regime": "unknown",
            "qty_multiplier": 0.5, "allow_new_opens": True,
            "force_close_speculative": False,
            "reason": "VIX unavailable (all sources failed) — half sizing, reserve at default floor",
        }

    caution = MARKET_REGIME["vix_caution_level"]
    pause   = MARKET_REGIME["vix_pause_level"]
    extreme = MARKET_REGIME["vix_extreme_level"]

    if vix >= extreme:
        return {
            "vix": vix, "regime": "extreme",
            "qty_multiplier": 0.0, "allow_new_opens": False,
            "force_close_speculative": True,
            "reason": f"VIX {vix:.1f} ≥ {extreme} extreme — close speculative, no new opens",
        }
    if vix >= pause:
        return {
            "vix": vix, "regime": "paused",
            "qty_multiplier": 0.0, "allow_new_opens": False,
            "force_close_speculative": False,
            "reason": f"VIX {vix:.1f} ≥ {pause} elevated — managing only, no new opens",
        }
    if vix >= caution:
        return {
            "vix": vix, "regime": "caution",
            "qty_multiplier": 0.5, "allow_new_opens": True,
            "force_close_speculative": False,
            "reason": f"VIX {vix:.1f} ≥ {caution} caution — sizing halved",
        }
    return {
        "vix": vix, "regime": "calm",
        "qty_multiplier": 1.0, "allow_new_opens": True,
        "force_close_speculative": False,
        "reason": f"VIX {vix:.1f} — calm market, normal sizing",
    }
