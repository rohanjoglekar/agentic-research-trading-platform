"""Market data for the research screen — price history, fundamentals, per-symbol
stats, news, and one company's research page.

Everything computes on locally cached 1y daily adjusted closes
(data/robinhood/prices/{SYM}.json), lazily fetched via yfinance with a 24h TTL.
Fundamentals cache 24h via yf.Ticker().info (flaky — degrades to {}). News via
Google News RSS, 10-min in-memory cache, failures -> [].

No data -> available:false / {} — never fabricated numbers. The analytics of
the owner's own account (benchmark curve, portfolio risk) live in rh_portfolio.
"""
from __future__ import annotations

import bisect
import datetime as dt
import fcntl
import json
import math
import os
import re
import statistics
import tempfile
import time
import xml.etree.ElementTree as ET

import numpy as np
import pandas as pd

import appconfig as config
from adapters import brokerage, read_json, rh_dcf, rh_factors

SYMBOL_RE = re.compile(r"^[A-Z.]{1,8}$")
RISK_FREE = 0.045          # annual risk-free assumption for Sharpe
TRADING_DAYS = 252
MIN_ROWS = 60              # below this, per-symbol stats -> {}
PRICE_TTL_SEC = 24 * 3600
FUND_TTL_SEC = 24 * 3600
# Bump when fetch_fundamentals gains fields the DCF/router depend on: old-schema
# cache entries are then treated as stale so the warmer refetches them (a
# self-healing rollout, no manual cache wipe). v2 = statement split
# (intangible-amort / SBC / EBIT / goodwill) for the gated DCF.
FUND_SCHEMA_V = 3  # v3: EBIT extraction excludes "non operating" rows (the LLY bug)
NEWS_TTL_SEC = 600

_news_cache: dict[str, tuple[float, list[dict]]] = {}


# ─── small helpers ───────────────────────────────────────────────────────────

def _utcnow_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _f(x, nd: int = 2):
    """numpy/float -> rounded plain float, NaN/inf/None -> None."""
    if x is None:
        return None
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    if math.isnan(v) or math.isinf(v):
        return None
    return round(v, nd)


def _is_stale(fetched_at: str | None, ttl: int) -> bool:
    if not fetched_at:
        return True
    try:
        t = dt.datetime.fromisoformat(str(fetched_at).replace("Z", "+00:00"))
        return (dt.datetime.now(dt.timezone.utc) - t).total_seconds() > ttl
    except (ValueError, TypeError):
        return True


# ─── price-history cache ─────────────────────────────────────────────────────

def daily_closes(sym: str = "SPY") -> dict:
    """{YYYY-MM-DD: close} from the daily price cache — the authoritative
    same-day benchmark level.

    The alpha runner records its own benchmark price per row, and until
    2026-09-17 that read the cache BEFORE asking the broker, so it stored the
    PRIOR session's close stamped with today's date. A benchmark lagged one day
    decorrelates from the book, which collapsed measured beta to +0.06 (corr
    0.064, R2 0.004) when the correctly aligned series gives +0.49 — and a beta
    pinned near zero credits the whole return to alpha. Callers resolve the
    benchmark through here and fall back to the recorded value only for dates
    the cache has not reached (i.e. today)."""
    d = read_json(_prices_path(sym)) or {}
    dates, closes = d.get("dates") or [], d.get("closes") or []
    out = {}
    for dt_, c in zip(dates, closes):
        try:
            out[str(dt_)[:10]] = float(c)
        except (TypeError, ValueError):
            continue
    return out


def _prices_path(sym: str):
    return config.RH_PRICES_DIR / f"{sym}.json"


def write_price_cache(sym: str, dates: list[str], closes: list[float]) -> None:
    config.RH_PRICES_DIR.mkdir(parents=True, exist_ok=True)
    payload = {"fetched_at": _utcnow_iso(), "dates": dates,
               "closes": [round(float(c), 4) for c in closes]}
    tmp = _prices_path(sym).with_suffix(".tmp")
    tmp.write_text(json.dumps(payload))
    tmp.replace(_prices_path(sym))


def _closes_from_download(df: pd.DataFrame, sym: str) -> pd.Series | None:
    """Extract the Close series for `sym` from a yf.download frame (single- or
    multi-symbol column layouts)."""
    if df is None or df.empty:
        return None
    cols = df.columns
    try:
        if isinstance(cols, pd.MultiIndex):
            if "Close" in cols.get_level_values(0):
                closes = df["Close"]
            else:  # (Ticker, Price) layout
                closes = df.xs("Close", axis=1, level=1)
            s = closes[sym] if sym in closes.columns else closes.iloc[:, 0]
        else:
            s = df["Close"]
    except (KeyError, IndexError):
        return None
    s = s.dropna()
    return s if len(s) else None


def fetch_prices(sym: str) -> dict | None:
    """Fetch 1y daily adjusted closes via yfinance and write the cache."""
    try:
        import yfinance as yf
        df = yf.download(sym, period="1y", interval="1d",
                         auto_adjust=True, progress=False)
        s = _closes_from_download(df, sym)
        if s is None:
            return None
        dates = [d.strftime("%Y-%m-%d") for d in s.index]
        write_price_cache(sym, dates, list(s.values))
        return read_json(_prices_path(sym))
    except Exception:
        return None


def get_prices(sym: str) -> dict | None:
    """Cached closes; lazy refetch when stale (>24h). Stale cache beats nothing."""
    cached = read_json(_prices_path(sym))
    if not (isinstance(cached, dict) and cached.get("dates") and cached.get("closes")):
        cached = None
    if cached and not _is_stale(cached.get("fetched_at"), PRICE_TTL_SEC):
        return cached
    return fetch_prices(sym) or cached


def _series(sym: str) -> pd.Series | None:
    d = get_prices(sym)
    if not d:
        return None
    s = pd.Series([float(c) for c in d["closes"]],
                  index=pd.to_datetime(d["dates"]))
    s = s[~s.index.duplicated(keep="last")].sort_index().dropna()
    return s if len(s) else None


# ─── chart series (1D intraday + up-to-5Y daily) ─────────────────────────────
# Separate caches from the 1y stats series above, so widening chart history can
# never skew the trailing-1y stats (max_dd_1y, sharpe_1y, sma200, ret_1y, ...).
CHART5Y_TTL_SEC = 24 * 3600
INTRADAY_TTL_SEC = 30 * 60
# CALENDAR days per range, applied as a date cutoff from the series' last bar.
# (These used to be sliced as dates[-n:] — BAR counts — so "3M" spanned 93
# trading days ≈ 4.4 months and "1Y" ≈ 1.5 years. The frontend fallback slice
# in PriceChart.tsx mirrors these values; keep the two in sync.)
_RANGE_CAL_DAYS = {"1M": 31, "3M": 92, "6M": 183, "1Y": 366, "5Y": 1830}


def _chart5y_path(sym: str):
    return config.RH_PRICES_DIR.parent / "chart5y" / f"{sym}.json"


def _intraday_path(sym: str):
    return config.RH_PRICES_DIR.parent / "intraday" / f"{sym}.json"


def _write_series_cache(path, dates: list[str], closes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"fetched_at": _utcnow_iso(), "dates": dates,
               "closes": [round(float(c), 4) for c in closes]}
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload))
    tmp.replace(path)


def _get_chart5y(sym: str) -> dict | None:
    cached = read_json(_chart5y_path(sym))
    if (isinstance(cached, dict) and cached.get("closes")
            and not _is_stale(cached.get("fetched_at"), CHART5Y_TTL_SEC)):
        return cached
    try:
        import yfinance as yf
        df = yf.download(sym, period="5y", interval="1d", auto_adjust=True, progress=False)
        s = _closes_from_download(df, sym)
        if s is not None and len(s) >= 2:
            _write_series_cache(_chart5y_path(sym),
                                [d.strftime("%Y-%m-%d") for d in s.index], list(s.values))
            return read_json(_chart5y_path(sym))
    except Exception:
        pass
    return cached if isinstance(cached, dict) and cached.get("closes") else None


def _get_intraday(sym: str) -> dict | None:
    cached = read_json(_intraday_path(sym))
    if (isinstance(cached, dict) and cached.get("closes")
            and not _is_stale(cached.get("fetched_at"), INTRADAY_TTL_SEC)):
        return cached
    try:
        import yfinance as yf
        df = yf.download(sym, period="5d", interval="15m", auto_adjust=True, progress=False)
        s = _closes_from_download(df, sym)
        if s is not None and len(s) >= 2:
            _write_series_cache(_intraday_path(sym),
                                [d.strftime("%Y-%m-%d %H:%M") for d in s.index], list(s.values))
            return read_json(_intraday_path(sym))
    except Exception:
        pass
    return cached if isinstance(cached, dict) and cached.get("closes") else None


def screened(sym: str) -> bool:
    """Is this one of the companies the last research build screened? The public
    pages answer for these and for nothing else (see get_symbol)."""
    from adapters import rh_ai_reads  # noqa: PLC0415  (it imports nothing from here)
    return str(sym or "").upper() in set(rh_ai_reads.screen_symbols())


def chart_series(sym: str, rng: str = "1Y", public: bool = False) -> dict:
    """Price series for the symbol chart at the requested range. 1D/1W = 15-min
    intraday (last sessions); 1M..5Y = daily from a dedicated 5y cache — kept
    separate from the stats series so it can span 5y without skewing 1y stats.
    `public=True` answers only for a screened company: every other symbol gets
    the same empty series, and nothing is fetched or cached for it."""
    sym = (sym or "").strip().upper()
    rng = (rng or "1Y").upper()
    if not sym or not all(c.isalnum() or c in ".-" for c in sym) or len(sym) > 8:
        return {"symbol": sym, "dates": [], "closes": [], "interval": "daily", "range": rng}
    if public and not screened(sym):
        return {"symbol": sym, "dates": [], "closes": [], "interval": "daily", "range": rng}
    if rng in ("1D", "1W"):
        d = _get_intraday(sym) or {}
        dates, closes = list(d.get("dates") or []), list(d.get("closes") or [])
        if rng == "1D" and dates:                        # keep only the most recent session
            last_day = dates[-1].split(" ")[0]
            cut = next((i for i, x in enumerate(dates) if x.split(" ")[0] == last_day), 0)
            dates, closes = dates[cut:], closes[cut:]
        return {"symbol": sym, "dates": dates, "closes": closes, "interval": "intraday", "range": rng}
    d = _get_chart5y(sym) or {}
    dates, closes = list(d.get("dates") or []), list(d.get("closes") or [])
    n = _RANGE_CAL_DAYS.get(rng)
    if n and dates:
        # Dates are ascending "YYYY-MM-DD" strings — a lexicographic cutoff is a
        # correct calendar-day window anchored on the most recent bar.
        try:
            last = dt.date.fromisoformat(dates[-1][:10])
            cutoff = (last - dt.timedelta(days=n)).isoformat()
            i = bisect.bisect_left(dates, cutoff)
        except (ValueError, TypeError):   # corrupted cache entry — serve unsliced
            i = 0
        if i > 0:
            dates, closes = dates[i:], closes[i:]
    return {"symbol": sym, "dates": dates, "closes": closes, "interval": "daily", "range": rng}


# ─── fundamentals cache ──────────────────────────────────────────────────────

def _fcf_from_cashflow(cf) -> tuple[float | None, list]:
    """Robust base free cash flow from the ANNUAL cash-flow statement (columns =
    fiscal years, newest first). `info.freeCashflow` is unreliable (frequently
    ~half the real figure), so we prefer the statement: its 'Free Cash Flow'
    line, else Operating Cash Flow − |CapEx|.

    Returns (base_fcf, up-to-3y list newest-first). The base normalizes out a
    single depressed year for an otherwise-healthy payer (max of latest vs the
    3y average) BUT preserves a genuinely-negative latest year (a current cash
    burn) so the DCF still flags it rather than valuing a normalized fiction."""
    try:
        if cf is None or getattr(cf, "empty", True):
            return None, []
        row = None
        if "Free Cash Flow" in cf.index:
            row = cf.loc["Free Cash Flow"]
        elif "Operating Cash Flow" in cf.index and "Capital Expenditure" in cf.index:
            row = cf.loc["Operating Cash Flow"] + cf.loc["Capital Expenditure"]  # capex is negative
        if row is None:
            return None, []
        vals = [float(v) for v in list(row.values)[:3]
                if v is not None and not (isinstance(v, float) and math.isnan(v))]
        if not vals:
            return None, []
        latest = vals[0]
        # Normalize out the commodity/cyclical swing: the 3y MEDIAN is robust to a
        # single peak (e.g. a gold miner at the top of the cycle, FCF 0.1→3→7B) or
        # a one-off trough, where the latest year alone would wildly over- or
        # under-value. A genuine current cash burn (negative latest) is preserved
        # so the DCF still flags it instead of valuing a normalized fiction.
        base = latest if latest < 0 else statistics.median(vals)
        return base, [round(v, 0) for v in vals]
    except Exception:
        return None, []


def _row_val(df, *needles, exclude=()):
    """First value (newest fiscal column) from the first statement row whose index
    contains ANY needle (case-insensitive) and NONE of `exclude`. None if absent.
    `exclude` prevents e.g. an 'EBIT' needle matching the 'EBITDA' row."""
    try:
        if df is None or getattr(df, "empty", True):
            return None
        for idx in df.index:
            s = str(idx).lower()
            if any(x in s for x in exclude):
                continue
            if any(n in s for n in needles):
                for v in list(df.loc[idx].values):
                    if v is not None and not (isinstance(v, float) and math.isnan(v)):
                        return float(v)
        return None
    except Exception:
        return None


def _row_series(df, *needles, exclude=(), n=5):
    """Up to n newest-first values from the first matching row ([] if none)."""
    try:
        if df is None or getattr(df, "empty", True):
            return []
        for idx in df.index:
            s = str(idx).lower()
            if any(x in s for x in exclude):
                continue
            if any(nd in s for nd in needles):
                out = []
                for v in list(df.loc[idx].values)[:n]:
                    if v is not None and not (isinstance(v, float) and math.isnan(v)):
                        out.append(float(v))
                return out
        return []
    except Exception:
        return []


def _statement_items(t) -> dict:
    """Statement-level items the gated DCF needs (blueprint §2): the D&A/
    intangible-amortization split, SBC, capex, EBIT, goodwill, net income. Every
    field is best-effort — None when the row/statement is missing — so a data gap
    caps confidence downstream instead of crashing. Reporting-currency values."""
    try:
        cf = t.cashflow
    except Exception:
        cf = None
    try:
        inc = t.income_stmt
    except Exception:
        inc = None
    try:
        bs = t.balance_sheet
    except Exception:
        bs = None

    capex = _row_val(cf, "capital expenditure")
    da_total = _row_val(cf, "depreciation and amortization",
                        "depreciation amortization depletion", "depreciation & amortization")
    # Acquired-intangible amortization — the serial-acquirer trap (§3). None on
    # non-acquirers that don't break it out (treated as 0 in FCF only when goodwill
    # is immaterial; otherwise it caps confidence).
    da_intangible = _row_val(cf, "amortization of intangible")
    sbc = _row_val(cf, "stock based compensation", "stock-based compensation")
    # exclude= is load-bearing: "operating income" is a SUBSTRING of yfinance's
    # "Other Non Operating Income Expenses" row, which sorts first for ~1/5 of the
    # universe and silently replaced real EBIT with a small non-operating figure
    # (mis-labelling LLY-class names "never profitable").
    ebit = _row_val(inc, "operating income",
                    exclude=("non operating", "non-operating", "nonoperating"))
    if ebit is None:
        ebit = _row_val(inc, "ebit", exclude=("ebitda",))
    net_income = (_row_val(inc, "net income from continuing operation",
                           "net income continuous operations")
                  or _row_val(inc, "net income", exclude=("common", "to common",
                              "continuing", "discontin", "including")))
    net_income_3y = (_row_series(inc, "net income from continuing operation",
                                 "net income continuous operations", n=3)
                     or _row_series(inc, "net income",
                                    exclude=("common", "continuing", "discontin"), n=3))
    interest_expense = _row_val(inc, "interest expense", exclude=("net interest",))
    pretax = _row_val(inc, "pretax income", "income before tax")
    tax_prov = _row_val(inc, "tax provision", "income tax expense")
    revenue_series = _row_series(inc, "total revenue", "operating revenue", n=5)
    goodwill = _row_val(bs, "goodwill", exclude=("and other",))
    gw_and_intang = _row_val(bs, "goodwill and other intangible")
    total_assets = _row_val(bs, "total assets")
    eff_tax = ((tax_prov / pretax) if (pretax and tax_prov is not None and pretax > 0)
               else None)
    return {
        "capex": abs(capex) if capex is not None else None,
        "da_total": da_total,
        "da_intangible": da_intangible,
        "sbc": sbc,
        "ebit": ebit,
        "net_income_stmt": net_income,
        "net_income_3y": net_income_3y or None,
        "interest_expense": abs(interest_expense) if interest_expense is not None else None,
        "effective_tax_rate": _f(eff_tax, 3) if eff_tax is not None else None,
        "revenue_series": revenue_series or None,
        "goodwill": goodwill if goodwill is not None else gw_and_intang,
        "goodwill_and_intangibles": gw_and_intang,
        "total_assets": total_assets,
    }


def fetch_fundamentals(sym: str) -> dict:
    """yf.Ticker().info (+ cash-flow statement) -> normalized fundamentals ({} on failure)."""
    try:
        import yfinance as yf
        t = yf.Ticker(sym)
        info = t.info or {}
    except Exception:
        return {}
    if not isinstance(info, dict) or not info:
        return {}
    try:
        cf_stmt = t.cashflow
    except Exception:
        cf_stmt = None
    fcf_base, fcf_3y = _fcf_from_cashflow(cf_stmt)
    stmt = _statement_items(t)

    # EV/EBITDA data-integrity: yfinance sometimes returns a garbage multiple
    # (e.g. ASML 3075× — a currency/units mismatch on an ADR). Recompute from
    # EV÷EBITDA when the scalar is implausible; null it if still bad, so one bad
    # datum can't zero the value factor's EV/EBITDA component.
    ev_ebitda = _f(info.get("enterpriseToEbitda"))
    if ev_ebitda is None or not (0 < ev_ebitda <= 100):
        _ev, _eb = _f(info.get("enterpriseValue")), _f(info.get("ebitda"))
        _re = (_ev / _eb) if (_ev and _eb and _eb > 0) else None
        ev_ebitda = _f(_re) if (_re is not None and 0 < _re <= 100) else None
    # Serial-acquirer signal (blueprint router): goodwill+intangibles / total assets.
    _gw = stmt.get("goodwill_and_intangibles") or stmt.get("goodwill")
    _ta = stmt.get("total_assets")
    goodwill_assets = (_gw / _ta) if (_gw and _ta and _ta > 0) else None

    div = info.get("dividendYield")
    # yfinance reports dividendYield already in percent for most versions >=0.2.5x
    if div is not None and div < 1.0 and (info.get("trailingAnnualDividendYield") or 0) == div:
        div = div * 100.0
    mc = info.get("marketCap")
    # Prefer the statement-derived FCF; fall back to the (unreliable) info scalar.
    fcf = fcf_base if fcf_base is not None else info.get("freeCashflow")
    rev = info.get("totalRevenue")
    fcf_yield = (fcf / mc * 100) if (fcf and mc) else None
    fcf_margin = (fcf / rev * 100) if (fcf and rev) else None
    # yfinance hands back profitMargins == 0.0 for names it has no margin for —
    # 25 of the 621-name universe (IONQ, ACHR, OKLO, MSTR …), every one of them
    # deeply loss-making. Stored raw that renders as a flat "0.0% margin" on a
    # company burning nine figures a year. Same house rule as free_cashflow
    # above: prefer the statement, keep the vendor scalar for audit.
    _pm_info = _f(info.get("profitMargins"), 4)
    _ni_stmt = _f((stmt or {}).get("net_income_stmt"), 2)
    _pm_stmt = (_ni_stmt / rev) if (_ni_stmt is not None and rev) else None
    # Only override toward a LOSS. A positive statement net income can come from
    # non-cash fair-value gains on a company that is still burning cash (MSTR's
    # bitcoin remeasurement is the live example), so substituting it would trade
    # a misleading 0.0 for a misleading PROFIT. The stated goal — stop printing
    # 0.0% for a name losing nine figures — is fully served by the negative case.
    _profit_margin = (_pm_stmt if (_pm_info == 0.0 and _pm_stmt is not None and _pm_stmt < 0)
                      else _pm_info)
    return {
        # ── identity ──
        "ticker": (sym or "").upper(),
        "schema_v": FUND_SCHEMA_V,          # gates self-healing cache refresh
        "name": info.get("longName") or info.get("shortName"),
        "sector": info.get("sector"),
        "industry": info.get("industry"),
        # First public trade (epoch seconds) → the router flags RECENT_IPO when a
        # name has < ~1y of public history, so a fresh IPO (SPCX) with no track
        # record and negative FCF can't masquerade as a confident valuation.
        # yfinance exposes this as firstTradeDateMilliseconds in .info (the older
        # firstTradeDateEpochUtc key is gone). Not schema-gated: populates as each
        # name's 24h TTL naturally refreshes.
        "first_trade_epoch": ((_ftm / 1000.0)
                              if (_ftm := _f(info.get("firstTradeDateMilliseconds"))) else None),
        "market_cap": mc,
        "enterprise_value": info.get("enterpriseValue"),
        "current_price": _f(info.get("currentPrice")),
        # ── valuation ──
        "pe": _f(info.get("trailingPE")),
        "forward_pe": _f(info.get("forwardPE")),
        "peg": _f(info.get("trailingPegRatio") or info.get("pegRatio"), 2),
        "price_to_book": _f(info.get("priceToBook")),
        "ev_ebitda": ev_ebitda,             # sanitized (see EV/EBITDA integrity above)
        "ev_rev": _f(info.get("enterpriseToRevenue")),
        "fcf_yield_pct": _f(fcf_yield),
        "dividend_yield_pct": _f(div),
        # ── growth (fractions: 0.21 == +21% YoY) ──
        "revenue_growth": _f(info.get("revenueGrowth"), 4),
        "earnings_growth": _f(info.get("earningsGrowth"), 4),
        "earnings_growth_q": _f(info.get("earningsQuarterlyGrowth"), 4),
        # ── quality / profitability (fractions) ──
        "gross_margin": _f(info.get("grossMargins"), 4),
        "operating_margin": _f(info.get("operatingMargins"), 4),
        "profit_margin": _f(_profit_margin, 4),
        "profit_margin_info": _pm_info,                    # vendor scalar (kept for audit)
        "roe": _f(info.get("returnOnEquity"), 4),
        "roa": _f(info.get("returnOnAssets"), 4),
        "fcf_margin_pct": _f(fcf_margin),
        # ── balance sheet / cash (raw $; debt_to_equity is a PERCENT: 124.9 == 1.25x) ──
        "free_cashflow": fcf,                              # statement-derived (robust)
        "free_cashflow_info": info.get("freeCashflow"),    # yfinance scalar (kept for audit)
        "fcf_3y": fcf_3y,                                  # last 3 annual FCFs, newest first
        "operating_cashflow": info.get("operatingCashflow"),
        "ebitda": info.get("ebitda"),
        "total_revenue": rev,
        "total_cash": info.get("totalCash"),
        "total_debt": info.get("totalDebt"),
        "debt_to_equity": _f(info.get("debtToEquity"), 1),
        "shares_outstanding": info.get("sharesOutstanding"),
        "book_value": _f(info.get("bookValue")),
        "trailing_eps": _f(info.get("trailingEps")),
        "forward_eps": _f(info.get("forwardEps")),
        # ── market / consensus (same info dict, no extra call) ──
        "beta_published": _f(info.get("beta")),
        "wk52_low": _f(info.get("fiftyTwoWeekLow")),
        "wk52_high": _f(info.get("fiftyTwoWeekHigh")),
        "recommendation": info.get("recommendationKey"),   # strong_buy/buy/hold/sell/...
        "target_mean": _f(info.get("targetMeanPrice")),
        "num_analysts": info.get("numberOfAnalystOpinions"),
        # ── statement-level items for the gated DCF (blueprint §2–§3) ──
        "capex": stmt.get("capex"),
        "da_total": stmt.get("da_total"),
        "da_intangible": stmt.get("da_intangible"),      # KEY: acquired-intangible amort
        "sbc": stmt.get("sbc"),
        "ebit": stmt.get("ebit"),
        "net_income_stmt": stmt.get("net_income_stmt"),
        "net_income_3y": stmt.get("net_income_3y"),
        "interest_expense": stmt.get("interest_expense"),
        "effective_tax_rate": stmt.get("effective_tax_rate"),
        "revenue_series": stmt.get("revenue_series"),
        "goodwill": stmt.get("goodwill"),
        "total_assets": stmt.get("total_assets"),
        "goodwill_assets_ratio": _f(goodwill_assets, 3),  # serial-acquirer signal
        "fetched_at": _utcnow_iso(),
    }


def _fund_needs_refresh(entry, ttl: int = FUND_TTL_SEC) -> bool:
    """True if a cached fundamentals entry is missing, past its TTL, or on an old
    schema (missing fields the current DCF/router need) — drives self-healing."""
    if not isinstance(entry, dict) or not entry:
        return True
    if entry.get("schema_v") != FUND_SCHEMA_V:
        return True
    return _is_stale(entry.get("fetched_at"), ttl)


def get_fundamentals(sym: str, force: bool = False) -> dict:
    book = read_json(config.RH_FUNDAMENTALS_PATH, default={}) or {}
    entry = book.get(sym)
    if not force and not _fund_needs_refresh(entry):
        return entry
    fresh = fetch_fundamentals(sym)
    if fresh:
        _cache_fundamental(sym, fresh)
        return fresh
    return entry if isinstance(entry, dict) else {}


def _cache_fundamental(sym: str, fresh: dict) -> None:
    """Persist one symbol into the shared fundamentals book under an EXCLUSIVE
    file lock, so concurrent writers can't lose each other's updates.

    The book is a single JSON file read-modify-written by several processes (the
    background warmer, the daily preload, ad-hoc lookups). Two bugs lived here:
      1. `book = read_json(...) or {}` then writing `book` back — `read_json` swallows
         any error into `{}`, so one transient unreadable read let a writer clobber
         all ~500 entries down to one (DCF coverage → ~0 until the slow warmer
         refilled).
      2. After fixing (1) with a strict re-read, a SECOND race remained: two writers
         both read a valid book, each add their own symbol, and last-writer-wins drops
         the other's addition — so the book churned and never filled (observed: 512
         symbols fetched, only ~160 persisted).
    Wrapping the whole read-merge-write in `flock(LOCK_EX)` serializes writers, so the
    book grows monotonically to full coverage even with several warmers running. The
    write itself stays an atomic tmp+rename (unique tmp via mkstemp). A read failure
    *under the lock* means a genuinely corrupt/missing file, so we skip rather than
    clobber."""
    path = config.RH_FUNDAMENTALS_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_suffix(".lock")
    with open(lock_path, "w") as lock_fh:
        fcntl.flock(lock_fh, fcntl.LOCK_EX)
        try:
            txt = path.read_text() if path.exists() else ""
            current = json.loads(txt) if txt.strip() else {}
            if not isinstance(current, dict):
                current = {}
        except Exception:
            # Unreadable/corrupt even under the lock — skip rather than risk a clobber.
            return
        current[sym] = fresh
        fd, tmpname = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as fh:
                json.dump(current, fh)
            os.replace(tmpname, path)
        except Exception:
            try:
                os.unlink(tmpname)
            except OSError:
                pass
            raise
    # exclusive lock released when lock_fh closes


def get_fundamentals_cached(sym: str) -> dict:
    """Cache-only read — NEVER triggers a (rate-limited) yfinance fetch.

    The bulk research screen uses this so a 300-name build can't fan out into
    300 `.info` calls and trip Yahoo's 429s; the fundamentals cache is warmed
    separately in scheduled batches (and lazily by single-symbol ad-hoc lookups,
    which may fetch live)."""
    book = read_json(config.RH_FUNDAMENTALS_PATH, default={}) or {}
    entry = book.get(sym)
    return entry if isinstance(entry, dict) else {}


# ─── news (Google News RSS) ──────────────────────────────────────────────────

def get_news(sym: str, limit: int = 12) -> list[dict]:
    now = time.monotonic()
    hit = _news_cache.get(sym)
    if hit and now - hit[0] < NEWS_TTL_SEC:
        return hit[1]
    url = (f"https://news.google.com/rss/search?q={sym}+stock"
           "&hl=en-US&gl=US&ceid=US:en")
    items: list[dict] = []
    try:
        # requests (not urllib): the framework Python build has no root certs
        # on its default SSL context; requests bundles certifi.
        import requests
        resp = requests.get(url, headers={"User-Agent": "forge-dashboard/1.0"},
                            timeout=8)
        resp.raise_for_status()
        root = ET.fromstring(resp.content)
        for it in root.iter("item"):
            src = it.find("source")
            items.append({
                "title": (it.findtext("title") or "").strip(),
                "source": (src.text or "").strip() if src is not None else "",
                "published": (it.findtext("pubDate") or "").strip(),
                "url": (it.findtext("link") or "").strip(),
            })
            if len(items) >= limit:
                break
    except Exception:
        items = []
    _news_cache[sym] = (now, items)
    return items


# ─── per-symbol stats ────────────────────────────────────────────────────────

def _ret_pct(s: pd.Series, n: int):
    """Return over the last n trading days, %."""
    if len(s) <= n:
        return None
    return _f((s.iloc[-1] / s.iloc[-(n + 1)] - 1) * 100)


def _rsi14(s: pd.Series):
    delta = s.diff()
    gain = delta.clip(lower=0.0)
    loss = (-delta).clip(lower=0.0)
    ag = gain.ewm(alpha=1 / 14, min_periods=14, adjust=False).mean().iloc[-1]
    al = loss.ewm(alpha=1 / 14, min_periods=14, adjust=False).mean().iloc[-1]
    if pd.isna(ag) or pd.isna(al):
        return None
    if al == 0:
        return 100.0
    return _f(100 - 100 / (1 + ag / al))


def _beta(r: pd.Series, bench_r: pd.Series):
    j = pd.concat([r, bench_r], axis=1, join="inner").dropna()
    if len(j) < MIN_ROWS:
        return None
    cov = np.cov(j.iloc[:, 0], j.iloc[:, 1])
    return None if cov[1, 1] == 0 else cov[0, 1] / cov[1, 1]


def symbol_stats(sym: str, spy: pd.Series | None = None) -> dict:
    s = _series(sym)
    if s is None or len(s) < MIN_ROWS:
        return {}
    r = s.pct_change().dropna()
    if spy is None:
        spy = _series("SPY")

    # YTD: base = last close of prior year if present, else first close this year
    year = int(s.index[-1].year)
    prior = s[s.index.year < year]
    this = s[s.index.year == year]
    ret_ytd = None
    if len(this):
        base = prior.iloc[-1] if len(prior) else this.iloc[0]
        ret_ytd = _f((s.iloc[-1] / base - 1) * 100)

    ann_vol = float(r.std() * math.sqrt(TRADING_DAYS))
    total = float(s.iloc[-1] / s.iloc[0] - 1)
    ann_ret = (1 + total) ** (TRADING_DAYS / len(r)) - 1 if len(r) else None
    sharpe = ((ann_ret - RISK_FREE) / ann_vol) if (ann_ret is not None and ann_vol > 0) else None
    max_dd = float(((s / s.cummax()) - 1).min() * 100)

    sma50 = s.rolling(50).mean().iloc[-1] if len(s) >= 50 else None
    sma200 = s.rolling(200).mean().iloc[-1] if len(s) >= 200 else None
    last = float(s.iloc[-1])
    if sma50 is not None and sma200 is not None:
        a50, a200 = last >= sma50, last >= sma200
        trend = ("above 50d & 200d (uptrend)" if a50 and a200 else
                 "above 200d, below 50d (pullback)" if a200 else
                 "above 50d, below 200d (recovering)" if a50 else
                 "below 50d & 200d (downtrend)")
    elif sma50 is not None:
        trend = ("above 50d (no 200d history)" if last >= sma50
                 else "below 50d (no 200d history)")
    else:
        trend = "insufficient history"

    beta = _beta(r, spy.pct_change().dropna()) if spy is not None else None
    return {
        "ret_1w_pct": _ret_pct(s, 5),
        "ret_1m_pct": _ret_pct(s, 21),
        "ret_3m_pct": _ret_pct(s, 63),
        "ret_ytd_pct": ret_ytd,
        "ret_1y_pct": _f(total * 100),
        "vol_ann_pct": _f(ann_vol * 100),
        "beta_1y": _f(beta),
        "sharpe_1y": _f(sharpe),
        "max_dd_1y_pct": _f(max_dd),
        "var95_day_pct": _f(float(np.percentile(r, 5)) * 100),
        "rsi_14": _rsi14(s),
        "sma50": _f(sma50),
        "sma200": _f(sma200),
        "trend": trend,
    }


# ─── symbol detail (GET /api/robinhood/symbol/{sym}) ─────────────────────────

UNSCREENED = "not in the research screen"


def get_symbol(sym: str, account: bool = True) -> dict:
    """One company's research. `account=False` is the public gateway: the
    Robinhood snapshot is never opened, so nothing in the answer can depend on
    what the owner holds or watches. That means no position, orders or
    watchlists, and a price taken from the cached closes for every name (the
    snapshot quotes exist only for held or watched names, so a live quote
    would say which those are).

    The public is answered only for a company the research build screened.
    Every other symbol gets the same short answer and nothing is read, fetched
    or cached for it. A held company outside the screen has warm caches and an
    AI read, a stranger has neither, and a page built for both would differ."""
    if not account and not screened(sym):
        return {"symbol": str(sym or "").upper(), "error": UNSCREENED}
    snap = brokerage.snapshot() if account else {}
    as_of = snap.get("as_of")
    quotes = snap.get("quotes") or {}

    position = None
    for p in snap.get("positions") or []:
        if p.get("symbol") == sym:
            qty = float(p.get("quantity") or 0)
            avg = float(p.get("avg_buy_price") or 0)
            q = quotes.get(sym) or {}
            last = q.get("last")
            mv = qty * last if last is not None else None
            total_usd = qty * (last - avg) if last is not None else None
            total_pct = ((last - avg) / avg * 100) if (last is not None and avg) else None
            position = {
                "quantity": qty,
                "avg_buy_price": _f(avg),
                "market_value": _f(mv),
                "total_usd": _f(total_usd),
                "total_pct": _f(total_pct),
            }
            break

    quote = None
    q = quotes.get(sym)
    if isinstance(q, dict) and q.get("last") is not None:
        last, prev = q.get("last"), q.get("prev_close")
        day_pct = ((last - prev) / prev * 100) if (last is not None and prev) else None
        quote = {"last": _f(last), "prev_close": _f(prev),
                 "last_ext": _f(q.get("last_ext")), "day_pct": _f(day_pct)}

    prices = get_prices(sym)
    history = ({"dates": prices["dates"], "closes": prices["closes"]}
               if prices else {})
    if not account:
        # The public "as of" is the day of the last close shown. When a cache
        # was filled depends on who has been looking at what.
        as_of = (history.get("dates") or [None])[-1]

    # Snapshot quotes only cover held/watchlisted names. For the rest of the
    # research universe, synthesize the quote from the SAME cached closes the
    # stats/DCF below already use — otherwise the slide-over showed "—" for a
    # name whose every other number was computed off closes[-1].
    if quote is None and len(history.get("closes") or []) >= 2:
        closes = history["closes"]
        last, prev = closes[-1], closes[-2]
        quote = {"last": _f(last), "prev_close": _f(prev), "last_ext": None,
                 "day_pct": _f((last - prev) / prev * 100) if prev else None}

    orders = [o for o in (snap.get("orders") or []) if o.get("symbol") == sym][:20]
    watchlists = [w.get("name") for w in (snap.get("watchlists") or [])
                  if sym in (w.get("items") or [])]

    # Analyst layer: DCF + fundamental factor sub-scores + news tone, computed
    # once on the already-fetched fundamentals/stats/news (no extra network).
    fund = get_fundamentals(sym)
    stats = symbol_stats(sym)
    news = get_news(sym)
    last_px = (quote or {}).get("last")
    if last_px is None and history.get("closes"):
        last_px = history["closes"][-1]
    dcf = (rh_dcf.dcf_value(fund, last_px, stats.get("beta_1y")) if fund
           else {"available": False, "reason": "no fundamentals"})
    news_sent = rh_factors.news([n.get("title") for n in news])
    _arch = (dcf or {}).get("archetype")
    factors = {
        "value": rh_factors.value(fund, last_px, dcf),
        "quality": rh_factors.quality(fund, _arch),
        "growth": rh_factors.growth(fund, _arch),
        "analyst": rh_factors.analyst(fund, last_px),
    }
    # Momentum from the SAME price stats the research screen uses + the coverage-
    # weighted composite, so the drill-down's factors and headline match the
    # research table instead of contradicting it. Lazy import: rh_research imports
    # this module, so importing it at call time avoids a circular import.
    from adapters import rh_research  # noqa: PLC0415
    mom = rh_research._score(stats)
    parabolic = bool(mom[2]) if mom is not None else False
    if mom is not None:
        factors["momentum"] = {"score": mom[0], "coverage": mom[3], "components": mom[1]}
    if news_sent.get("score") is not None:
        factors["news"] = news_sent
    # AI read — the same weekly read, ranked the same way, as the research
    # table, so the slide-over's composite/tier match the table.
    from adapters import rh_ai_reads  # noqa: PLC0415
    _ai = rh_ai_reads.for_screen()
    _read = _ai["reads"].get(str(sym).upper())
    if not account and str(sym).upper() not in set(rh_ai_reads.screen_symbols()):
        _read = None
    factors["ai"] = rh_ai_reads.factor(_read, _ai["coverage_ok"], _ai["pool"], _ai["universe"])
    comp, parts = rh_factors.composite(factors)
    conf, conf_label, conf_why = rh_factors.confidence(factors, dcf, parabolic=parabolic)
    if comp is not None:
        factors["composite"] = {"score": comp, "weights": parts,
                                "confidence": conf, "confidence_label": conf_label,
                                "confidence_notes": conf_why}

    return {
        "symbol": sym,
        "as_of": as_of,
        "held": position is not None,
        "position": position,
        "quote": quote,
        "fundamentals": {k: v for k, v in fund.items() if k != "fetched_at"},
        "stats": stats,
        "score": comp,
        "tier": rh_factors.tier(comp, conf),
        "confidence": conf, "confidence_label": conf_label, "confidence_notes": conf_why,
        "factors": factors,
        "dcf": dcf,
        "news_sentiment": news_sent,
        "history": history,
        "orders": orders,
        "watchlists": watchlists,
        "news": news,
    }
