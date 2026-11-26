"""
agents/options_agent.py

Agent 3: Options Chain Analyzer

Input:  List of screened stock candidates (DataFrame from StockScreener)
Output: List of all valid options trade opportunities with full metrics

This agent:
1. Pulls options chains for each candidate (real or mock)
2. Identifies valid expirations within the DTE window
3. Scans puts (CSP) and calls (CC) for valid opportunities
4. Returns raw trade opportunities before scoring
"""

import datetime
from typing import Optional
import pandas as pd
from loguru import logger

from config.settings import OPTIONS, USE_MOCK_OPTIONS
from data.mock_data import generate_mock_options_chain
from strategies.cash_secured_put import scan_csps_for_stock
from strategies.covered_call import scan_ccs_for_stock


class OptionsAgent:
    """
    Pulls options chains and extracts tradeable opportunities
    for each stock candidate.
    """

    def __init__(self, options_client=None, use_mock: Optional[bool] = None,
                 csp_only: bool = False, cc_only: bool = False, yf_client=None):
        """
        Args:
            options_client: object with ``get_options_expirations(symbol)`` and
                ``get_options_chain(symbol, expiration)`` (e.g. AlpacaOptionsClient
                or YFinanceClient). Used for live data.
            use_mock: override USE_MOCK_OPTIONS env setting
            csp_only: skip covered-call scanning (daily report uses this)
            cc_only:  skip cash-secured-put scanning (CC page uses this)
            yf_client: deprecated alias for ``options_client`` (kept for callers
                that haven't been updated yet).
        """
        self.options_client = options_client if options_client is not None else yf_client
        self.use_mock = use_mock if use_mock is not None else USE_MOCK_OPTIONS
        self.csp_only = csp_only
        self.cc_only = cc_only

    def run(self, candidates: pd.DataFrame) -> list:
        """
        Main entry point. Processes all candidates and returns opportunities.

        Args:
            candidates: DataFrame from StockScreener with columns:
                        symbol, price, screener_score, hv_20, etc.

        Returns:
            List of trade opportunity dicts (pre-scoring)
        """
        all_opportunities = []

        for _, row in candidates.iterrows():
            symbol = row["symbol"]
            price = row.get("price")

            if not price or price <= 0:
                logger.warning(f"[OptionsAgent] Skipping {symbol}: no valid price")
                continue

            logger.info(f"[OptionsAgent] Processing {symbol} @ ${price:.2f}")

            try:
                opps = self._process_ticker(row)
                all_opportunities.extend(opps)
                logger.info(f"[OptionsAgent] {symbol}: {len(opps)} opportunities found")
            except Exception as e:
                logger.error(f"[OptionsAgent] Error processing {symbol}: {e}")

        logger.info(f"[OptionsAgent] Total opportunities: {len(all_opportunities)}")
        return all_opportunities

    def _process_ticker(self, stock_row: pd.Series) -> list:
        """Processes a single ticker and returns its options opportunities."""
        symbol = stock_row["symbol"]
        price = float(stock_row["price"])
        screener_score = float(stock_row.get("screener_score", 50))
        iv_estimate = float(stock_row.get("hv_20", 0.55) or 0.55)

        if self.use_mock:
            return self._process_mock(symbol, price, iv_estimate, screener_score, stock_row)
        else:
            return self._process_live(symbol, price, screener_score, stock_row)

    def _process_mock(
        self,
        symbol: str,
        price: float,
        iv: float,
        screener_score: float,
        stock_row: pd.Series,
    ) -> list:
        """Generates mock options chain and finds opportunities."""
        opportunities = []

        for dte in [21, 30, 45]:
            chain = generate_mock_options_chain(
                symbol=symbol,
                stock_price=price,
                iv=iv,
                dte=dte,
                num_strikes=12,
            )
            expiration = chain["expiration"]
            opps = self._extract_opportunities(
                symbol, price, chain, expiration, dte, screener_score, stock_row
            )
            opportunities.extend(opps)

        return opportunities

    def _process_live(
        self,
        symbol: str,
        price: float,
        screener_score: float,
        stock_row: pd.Series,
    ) -> list:
        """Pulls real options chain via the configured client (Alpaca or yfinance)."""
        if not self.options_client:
            logger.warning(f"[OptionsAgent] No options client, skipping live data for {symbol}")
            return []

        expirations = self.options_client.get_options_expirations(symbol)
        if not expirations:
            logger.warning(f"[OptionsAgent] No options found for {symbol}")
            return []

        opportunities = []
        today = datetime.date.today()

        for exp_str in expirations:
            try:
                exp_date = datetime.datetime.strptime(exp_str, "%Y-%m-%d").date()
                dte = (exp_date - today).days

                if dte < OPTIONS["dte_min"] or dte > OPTIONS["dte_max"]:
                    continue

                chain = self.options_client.get_options_chain(symbol, exp_str)
                opps = self._extract_opportunities(
                    symbol, price, chain, exp_str, dte, screener_score, stock_row
                )
                opportunities.extend(opps)

            except Exception as e:
                logger.warning(f"[OptionsAgent] Error on {symbol} {exp_str}: {e}")

        return opportunities

    def _extract_opportunities(
        self,
        symbol: str,
        price: float,
        chain: dict,
        expiration: str,
        dte: int,
        screener_score: float,
        stock_row: pd.Series,
    ) -> list:
        """Extracts CSP and CC opportunities from a single chain."""
        opportunities = []

        # Cash-Secured Puts
        csp_results = [] if self.cc_only else scan_csps_for_stock(
            symbol=symbol,
            stock_price=price,
            options_chain=chain,
            expiration=expiration,
            dte=dte,
            screener_score=screener_score,
            dividend_yield=stock_row.get("dividend_yield"),
        )

        # Covered Calls
        cc_results = [] if self.csp_only else scan_ccs_for_stock(
            symbol=symbol,
            stock_price=price,
            options_chain=chain,
            expiration=expiration,
            dte=dte,
            cost_basis=price,
            screener_score=screener_score,
        )

        # Enrich with stock-level data for scoring
        for opp in csp_results + cc_results:
            opp["fundamental_score"] = float(stock_row.get("fundamental_score", 50))
            opp["technical_score"] = float(stock_row.get("technical_score", 50))
            opp["iv_rank"] = float(stock_row.get("iv_rank", 50) or 50)
            opp["days_to_earnings"] = stock_row.get("days_to_earnings")
            opp["hv_20"] = stock_row.get("hv_20")
            opp["rsi_14"] = stock_row.get("rsi_14")
            opp["drawdown_from_52w_high"] = stock_row.get("drawdown_from_52w_high")
            opp["recent_30d_return_pct"] = stock_row.get("recent_30d_return_pct")
            opp["sector"] = stock_row.get("sector", "Unknown")
            opp["market_cap"] = stock_row.get("market_cap")
            opportunities.append(opp)

        return opportunities

    def filter_valid_only(self, opportunities: list) -> list:
        """Removes trades with hard-fail flags."""
        return [opp for opp in opportunities if opp.get("is_valid", False)]
