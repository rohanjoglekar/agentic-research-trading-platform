"""
screener/stock_screener.py

Two-pass stock screener:
  Pass 1 (hard filters): price, volume, market cap, earnings buffer — fast elimination
  Pass 2 (soft scoring): fundamentals, technicals, IV quality — scores remaining candidates

Returns a ranked DataFrame of candidates ready for options scanning.
"""

from typing import Optional
import pandas as pd
from loguru import logger

from config.settings import SCREENER, DEFAULT_UNIVERSE, ETF_BLOCKLIST


class StockScreener:
    """
    Screens a universe of tickers against configurable filters.
    Designed to work with real yfinance data or mock data interchangeably.
    """

    def __init__(self, yf_client=None, alpaca_client=None):
        """
        Args:
            yf_client: YFinanceClient instance
            alpaca_client: AlpacaClient instance (optional, supplements yfinance)
        """
        self.yf = yf_client
        self.alpaca = alpaca_client

    # ──────────────────────────────────────────────────────────────────────────
    # Main Entry Point
    # ──────────────────────────────────────────────────────────────────────────

    def run(
        self,
        universe: Optional[list] = None,
        mock_df: Optional[pd.DataFrame] = None,
    ) -> pd.DataFrame:
        """
        Run full screening pipeline.

        Args:
            universe: list of ticker symbols. Defaults to DEFAULT_UNIVERSE.
            mock_df: pre-built DataFrame to use instead of live API calls (for MVP/testing)

        Returns:
            DataFrame of screened candidates with all relevant fields + scores
        """
        if mock_df is not None:
            logger.info(f"Running screener on mock data: {len(mock_df)} tickers")
            df = mock_df.copy()
        else:
            if universe is None:
                universe = DEFAULT_UNIVERSE
            # Hard-drop leveraged/inverse/volatility ETFs (see ETF_BLOCKLIST).
            # They sneak in via custom user lists; this is the central gate.
            before_block = len(universe)
            universe = [t for t in universe if t.upper() not in ETF_BLOCKLIST]
            if len(universe) < before_block:
                blocked = before_block - len(universe)
                logger.info(f"ETF blocklist removed {blocked} ticker(s)")
            logger.info(f"Running screener on {len(universe)} tickers via live API")
            df = self._fetch_universe(universe)

        # Pass 1: Hard filters (eliminate)
        before = len(df)
        df = self._apply_hard_filters(df)
        logger.info(f"Hard filters: {before} → {len(df)} candidates")

        if df.empty:
            logger.warning("No candidates passed hard filters.")
            return df

        # Pass 2: Soft scoring
        df = self._apply_soft_scores(df)

        # Sort by composite screener score
        df = df.sort_values("screener_score", ascending=False).reset_index(drop=True)

        # GATE: screener_score now actually FILTERS, not just sorts. Previously every
        # hard-filter survivor was options-scanned identically regardless of score; a
        # 12/100 underlying cost the same chain fetch + AI grade as a 95/100 one. Drop
        # sub-threshold names and cap the count — better picks, less downstream cost.
        # Mock runs skip the gate so tests still see the full set.
        if mock_df is None:
            before_gate = len(df)
            min_score = SCREENER.get("min_screener_score")
            max_n = SCREENER.get("max_candidates")
            if min_score is not None:
                df = df[df["screener_score"] >= min_score]
            if max_n:
                df = df.head(int(max_n))
            df = df.reset_index(drop=True)
            if len(df) < before_gate:
                logger.info(f"Screener gate: {before_gate} → {len(df)} candidates "
                            f"(score ≥ {min_score}, cap {max_n})")

        if df.empty:
            logger.warning("No candidates passed the screener gate.")
            return df
        logger.info(f"Screening complete. Top candidate: {df.iloc[0]['symbol']}")
        return df

    # ──────────────────────────────────────────────────────────────────────────
    # Data Fetching
    # ──────────────────────────────────────────────────────────────────────────

    def _fetch_universe(self, universe: list) -> pd.DataFrame:
        """
        Fetches key data for all symbols in universe.
        Falls back gracefully per symbol if yfinance errors out.
        """
        rows = []
        for sym in universe:
            try:
                info = self.yf.get_stock_info(sym)
                tech = self.yf.get_technical_data(sym)
                dte_earnings = self.yf.days_to_earnings(sym)

                row = {
                    "symbol": sym,
                    "price": info.get("price"),
                    "sector": info.get("sector") or "Unknown",
                    "industry": info.get("industry") or "Unknown",
                    "market_cap": info.get("market_cap"),
                    "avg_volume_30d": info.get("avg_volume_30d"),
                    "beta": info.get("beta"),
                    "revenue_growth": info.get("revenue_growth"),
                    "profit_margin": info.get("profit_margin"),
                    "debt_to_equity": info.get("debt_to_equity"),
                    "dividend_yield": info.get("dividend_yield"),
                    "current_ratio": info.get("current_ratio"),
                    "short_float": info.get("short_float"),
                    "recommendation": info.get("recommendation"),
                    "analyst_target": info.get("analyst_target"),
                    "days_to_earnings": dte_earnings,
                    "above_ma50": tech.get("above_ma50"),
                    "above_ma200": tech.get("above_ma200"),
                    "rsi_14": tech.get("rsi_14"),
                    "hv_20": tech.get("hv_20"),
                    "hv_60": tech.get("hv_60"),
                    "drawdown_from_52w_high": tech.get("drawdown_from_52w_high"),
                    "recent_30d_return_pct": tech.get("recent_30d_return_pct"),
                    "52w_high": info.get("52w_high"),
                    "52w_low": info.get("52w_low"),
                    "50d_avg": info.get("50d_avg"),
                    "200d_avg": info.get("200d_avg"),
                    "free_cash_flow": info.get("free_cash_flow"),
                    "total_cash": info.get("total_cash"),
                    "total_debt": info.get("total_debt"),
                }
                rows.append(row)
            except Exception as e:
                logger.warning(f"[Screener] Error fetching {sym}: {e}")

        return pd.DataFrame(rows)

    # ──────────────────────────────────────────────────────────────────────────
    # Pass 1: Hard Filters
    # ──────────────────────────────────────────────────────────────────────────

    def _apply_hard_filters(self, df: pd.DataFrame) -> pd.DataFrame:
        """Eliminates stocks that fail any hard filter."""
        if df.empty:
            return df                    # an empty universe has no columns to test
        mask = pd.Series([True] * len(df), index=df.index)
        reasons = pd.Series([""] * len(df), index=df.index)

        def apply_filter(condition_mask, reason: str):
            nonlocal mask, reasons
            failed = ~condition_mask & mask
            reasons[failed] = reasons[failed] + reason + "; "
            mask &= condition_mask

        # Price range
        apply_filter(
            df["price"].notna() & (df["price"] >= SCREENER["min_price"]) & (df["price"] <= SCREENER["max_price"]),
            f"Price not in ${SCREENER['min_price']}-${SCREENER['max_price']}"
        )

        # Market cap
        apply_filter(
            df["market_cap"].isna() | (df["market_cap"] >= SCREENER["min_market_cap"]),
            f"Market cap < ${SCREENER['min_market_cap']/1e6:.0f}M"
        )

        # Volume
        apply_filter(
            df["avg_volume_30d"].isna() | (df["avg_volume_30d"] >= SCREENER["min_avg_volume_30d"]),
            f"Avg volume < {SCREENER['min_avg_volume_30d']/1e6:.0f}M"
        )

        # Short float (avoid extreme short interest)
        apply_filter(
            df["short_float"].isna() | (df["short_float"] <= SCREENER["max_short_float"]),
            f"Short float > {SCREENER['max_short_float']:.0%}"
        )

        # Earnings buffer
        if "days_to_earnings" in df.columns:
            apply_filter(
                df["days_to_earnings"].isna() | (df["days_to_earnings"] >= SCREENER["earnings_buffer_days"]),
                f"Earnings within {SCREENER['earnings_buffer_days']} days"
            )

        # IV floor (need premium to sell)
        if "hv_20" in df.columns:
            apply_filter(
                df["hv_20"].isna() | (df["hv_20"] >= SCREENER["min_iv"]),
                f"Historical vol < {SCREENER['min_iv']:.0%} (not enough premium)"
            )

        df = df[mask].copy()
        return df

    # ──────────────────────────────────────────────────────────────────────────
    # Pass 2: Soft Scoring
    # ──────────────────────────────────────────────────────────────────────────

    def _apply_soft_scores(self, df: pd.DataFrame) -> pd.DataFrame:
        """
        Adds screener_score (0-100) and component scores.
        No eliminations — just rankings.
        """
        df = df.copy()

        # Fundamental score
        df["fundamental_score"] = df.apply(self._score_fundamentals, axis=1)

        # Technical score
        df["technical_score"] = df.apply(self._score_technicals, axis=1)

        # Volatility quality score (for premium selling)
        df["vol_score"] = df.apply(self._score_volatility, axis=1)

        # Risk flags
        df["risk_flags"] = df.apply(self._get_risk_flags, axis=1)
        df["num_risk_flags"] = df["risk_flags"].apply(len)

        # Penalty for risk flags
        flag_penalty = df["num_risk_flags"] * 3  # -3 points per flag

        # Composite screener score
        df["screener_score"] = (
            df["fundamental_score"] * 0.40
            + df["technical_score"] * 0.30
            + df["vol_score"] * 0.30
            - flag_penalty
        ).clip(0, 100).round(1)

        return df

    def _score_fundamentals(self, row: pd.Series) -> float:
        """Returns 0-100 fundamental quality score."""
        score = 50.0  # neutral start

        # Profitability (±20 points)
        pm = row.get("profit_margin")
        if pm is not None:
            if pm > 0.10:    score += 20
            elif pm > 0:     score += 10
            elif pm > -0.10: score += 0
            else:            score -= 15

        # Revenue growth (±15 points)
        rg = row.get("revenue_growth")
        if rg is not None:
            if rg > 0.20:    score += 15
            elif rg > 0.05:  score += 8
            elif rg > -0.05: score += 0
            else:            score -= 12

        # Debt safety (±10 points)
        de = row.get("debt_to_equity")
        if de is not None:
            if de < 0.5:     score += 10
            elif de < 1.5:   score += 5
            elif de < 3.0:   score += 0
            else:            score -= 10

        # Current ratio (±8 points)
        cr = row.get("current_ratio")
        if cr is not None:
            if cr > 2.0:     score += 8
            elif cr > 1.0:   score += 3
            else:            score -= 8

        # Analyst recommendation (±5 points)
        rec = row.get("recommendation", "")
        if rec in ("buy", "strong_buy"):    score += 5
        elif rec in ("sell", "strong_sell"): score -= 5

        return float(max(0, min(100, score)))

    def _score_technicals(self, row: pd.Series) -> float:
        """Returns 0-100 technical trend score."""
        score = 50.0

        # Trend vs MAs (±25 points)
        if row.get("above_ma200") is True:   score += 15
        elif row.get("above_ma200") is False: score -= 10
        if row.get("above_ma50") is True:    score += 10
        elif row.get("above_ma50") is False:  score -= 5

        # RSI (±15 points) — prefer 40-65 range for wheel trades
        rsi = row.get("rsi_14")
        if rsi is not None:
            if 40 <= rsi <= 65:  score += 15   # ideal: not overbought/oversold
            elif 35 <= rsi < 40: score += 5
            elif 65 < rsi <= 75: score += 0
            elif rsi > 75:       score -= 10   # overbought — avoid CC entries
            elif rsi < 30:       score -= 5    # oversold — caution

        # Drawdown (±10 points)
        dd = row.get("drawdown_from_52w_high")
        if dd is not None:
            if dd > -0.15:       score += 10   # near 52w high
            elif dd > -0.30:     score += 3
            elif dd > -0.50:     score -= 5
            else:                score -= 12   # big drawdown

        return float(max(0, min(100, score)))

    def _score_volatility(self, row: pd.Series) -> float:
        """Returns 0-100 score for volatility quality (premium selling context)."""
        score = 50.0

        # IV level — want elevated but not insane
        hv20 = row.get("hv_20") or 0
        hv60 = row.get("hv_60") or 0
        hv = max(hv20, hv60)

        if 0.35 < hv <= 0.80:    score += 20   # sweet spot for premium
        elif 0.80 < hv <= 1.20:  score += 5    # high, caution
        elif hv > 1.20:          score -= 15   # too wild
        elif hv < 0.25:          score -= 20   # too low, not worth selling

        # NOTE: the old `row.get("iv_rank")` branch was DEAD in production — the live
        # `_fetch_universe` never populates iv_rank (get_technical_data only gives point
        # HV20/HV60, not an options-IV history to rank against), so it only ever fired in
        # mock mode: a silent prod-vs-test scoring divergence. Removed. Vol is scored from
        # the HV level (above) + the HV20/HV60 expansion ratio (below) — both real live
        # inputs. Re-add a ranked branch here once a true IV-history source is wired in.

        # HV20/HV60 ratio (IV expanding vs contracting)
        if hv20 and hv60 and hv60 > 0:
            ratio = hv20 / hv60
            if ratio > 1.2:      score += 5    # recent vol expansion — sell premium
            elif ratio < 0.8:    score -= 5    # vol contracting — less premium

        return float(max(0, min(100, score)))

    def _get_risk_flags(self, row: pd.Series) -> list:
        """Returns list of risk warning strings for this stock."""
        flags = []

        if row.get("short_float", 0) and row["short_float"] > 0.20:
            flags.append(f"High short interest ({row['short_float']:.0%})")

        if row.get("revenue_growth") is not None and row["revenue_growth"] < -0.15:
            flags.append("Revenue declining >15%")

        if row.get("debt_to_equity") is not None and row["debt_to_equity"] > 3.0:
            flags.append(f"High D/E ratio ({row['debt_to_equity']:.1f})")

        if row.get("current_ratio") is not None and row["current_ratio"] < 1.0:
            flags.append(f"Current ratio below 1 ({row['current_ratio']:.2f})")

        if row.get("days_to_earnings") is not None and 0 <= row["days_to_earnings"] <= 30:
            flags.append(f"Earnings in {row['days_to_earnings']} days")

        if row.get("above_ma200") is False:
            flags.append("Below 200-day MA (downtrend)")

        if row.get("rsi_14") is not None:
            if row["rsi_14"] > 75:
                flags.append(f"Overbought (RSI {row['rsi_14']:.0f})")
            elif row["rsi_14"] < 30:
                flags.append(f"Oversold (RSI {row['rsi_14']:.0f})")

        if row.get("profit_margin") is not None and row["profit_margin"] < -0.20:
            flags.append("Deeply unprofitable")

        return flags

    # ──────────────────────────────────────────────────────────────────────────
    # Utility
    # ──────────────────────────────────────────────────────────────────────────

    def get_optionable_check(self, symbol: str) -> bool:
        """
        Checks if a symbol has options available in yfinance.
        Returns True if at least one expiration exists.
        """
        if not self.yf:
            return True  # assume optionable in mock mode
        expirations = self.yf.get_options_expirations(symbol)
        return len(expirations) > 0

    def filter_by_earnings_safe(self, df: pd.DataFrame, min_days: int = 14) -> pd.DataFrame:
        """Convenience filter: remove stocks with earnings within min_days."""
        if "days_to_earnings" not in df.columns:
            return df
        return df[
            df["days_to_earnings"].isna() | (df["days_to_earnings"] >= min_days)
        ]
