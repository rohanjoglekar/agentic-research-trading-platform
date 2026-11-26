"""
agents/recommendation_agent.py

The scan's last scoring step.

Input:  trade opportunities from OptionsAgent
Output: a ranked DataFrame with composite scores, ratings and explanations

It:
1. Applies the composite score to every trade
2. Ranks by quality (not just yield)
3. Optionally writes a one-line explanation for the top-N trades
4. Produces the final output DataFrame
"""

import os

import pandas as pd
from loguru import logger

from scoring.composite_score import composite_trade_score, rank_trades
from config.settings import LLM

# Who writes the cosmetic per-trade "why rated X" one-liner. RECO_LLM_PROVIDER
# defaults to "ollama" (a local model); on any ollama error, timeout or empty
# reply the explanation falls back to Claude. Any other value skips ollama and
# uses LLM["provider"] from settings. The explanation never affects a score.
_RECO_PROVIDER = os.environ.get(
    "RECO_LLM_PROVIDER", "ollama").strip().lower()
_OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://127.0.0.1:11434")
_OLLAMA_PROSE_MODEL = os.environ.get("OLLAMA_PROSE_MODEL", "llama3.2:3b")


def _ollama_explanation(prompt: str, *, timeout: int = 60) -> str:
    """Local Ollama one-liner (mirrors agents/ollama_researcher.py's
    `/api/generate` helper). Returns "" on any failure so the caller can fall
    back to Claude. Never raises."""
    try:
        import requests
        r = requests.post(
            f"{_OLLAMA_URL}/api/generate",
            json={"model": _OLLAMA_PROSE_MODEL, "prompt": prompt,
                  "stream": False,
                  "options": {"temperature": 0.3,
                              "num_predict": LLM.get("max_tokens", 200)}},
            timeout=timeout,
        )
        if r.status_code >= 400:
            logger.warning(f"[RecommendationAgent] ollama HTTP {r.status_code}")
            return ""
        return (r.json().get("response") or "").strip()
    except Exception as e:
        logger.warning(f"[RecommendationAgent] ollama error: {e}")
        return ""


class RecommendationAgent:
    """
    Applies final scoring and generates the ranked recommendation table.
    """

    def __init__(self, llm_client=None, regime="calm", pop_bias=1.0):
        """
        Args:
            llm_client: optional Anthropic/OpenAI client for explanations.
                If omitted but ``LLM.use_llm_explanations`` is True and an
                ``ANTHROPIC_API_KEY`` is configured, this auto-constructs one.
        """
        self.llm = llm_client
        self.provider = _RECO_PROVIDER
        if self.llm is None and LLM.get("use_llm_explanations"):
            try:
                from config.settings import ANTHROPIC_API_KEY
                if ANTHROPIC_API_KEY:
                    import anthropic
                    self.llm = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
            except Exception as e:
                logger.warning(f"[RecommendationAgent] could not auto-init LLM client: {e}")
        # With the local-ollama provider, explanations run WITHOUT an SDK client
        # (ollama is an HTTP endpoint); Claude is only the fallback. So enable
        # explanations whenever the toggle is on and either a client exists OR
        # the provider is ollama.
        self.use_llm = LLM.get("use_llm_explanations", False) and (
            self.llm is not None or self.provider == "ollama")
        # The market regime picks the weight set and pop_bias scales POP in _score_trade.
        self.regime = regime
        self.pop_bias = pop_bias

    def run(self, opportunities: list) -> pd.DataFrame:
        """
        Main entry point.

        Args:
            opportunities: list of trade dicts from OptionsAgent

        Returns:
            Ranked DataFrame with all columns for dashboard display
        """
        if not opportunities:
            logger.warning("[RecommendationAgent] No opportunities to score.")
            return pd.DataFrame()

        logger.info(f"[RecommendationAgent] Scoring {len(opportunities)} opportunities")

        # Score every trade
        scored = []
        for opp in opportunities:
            scored_opp = self._score_trade(opp)
            scored.append(scored_opp)

        # Rank by final score
        ranked = rank_trades(scored)

        # Optionally enrich top-N with LLM explanations
        if self.use_llm:
            top_n = LLM.get("top_n_for_llm", 10)
            for trade in ranked[:top_n]:
                trade["explanation"] = self._llm_explanation(trade)

        # Build output DataFrame
        df = self._build_output_df(ranked)
        logger.info(f"[RecommendationAgent] Top trade: {df.iloc[0]['Ticker']} — {df.iloc[0]['Rating']} ({df.iloc[0]['Final Score']:.0f}/100)")

        return df

    def _score_trade(self, opp: dict) -> dict:
        """Applies composite scoring to a single trade opportunity."""
        result = composite_trade_score(
            monthly_return_pct=opp.get("monthly_return_pct", 0),
            pop_estimate=opp.get("pop_estimate", 0.5),
            liquidity_score=opp.get("liquidity_score", 50),
            fundamental_score=opp.get("fundamental_score", 50),
            technical_score=opp.get("technical_score", 50),
            iv_rank=opp.get("iv_rank"),
            days_to_earnings=opp.get("days_to_earnings"),
            risk_flags=opp.get("flags", []),
            strategy=opp.get("strategy", "CSP"),
            rsi_14=opp.get("rsi_14"),
            drawdown_from_52w_high=opp.get("drawdown_from_52w_high"),
            recent_30d_return_pct=opp.get("recent_30d_return_pct"),
            regime=getattr(self, "regime", "calm"),
            pop_bias=getattr(self, "pop_bias", 1.0),
            # wheel opportunities carry the buffer under `csp_downside_buffer_pct`
            downside_buffer_pct=opp.get("downside_buffer_pct") or opp.get("csp_downside_buffer_pct"),
        )

        # Merge composite result into the opportunity dict
        opp_copy = opp.copy()
        opp_copy.update(result)
        return opp_copy

    def _build_output_df(self, ranked: list) -> pd.DataFrame:
        """
        Builds the final display DataFrame with all dashboard columns.
        """
        df = pd.DataFrame(ranked)

        # Select and rename columns for clean display
        column_map = {
            "rank": "Rank",
            "symbol": "Ticker",
            "stock_price": "Price",
            "sector": "Sector",
            "strategy": "Strategy",
            "expiration": "Expiration",
            "strike": "Strike",
            "premium": "Premium",
            "monthly_return_pct": "Monthly %",
            "annualized_return_pct": "Ann. %",
            "breakeven": "Breakeven",
            "downside_buffer_pct": "Buffer %",
            "iv_rank": "IV Rank",
            "open_interest": "OI",
            "spread_pct": "Spread %",
            "days_to_earnings": "Earnings Days",
            "fundamental_score": "Fund Score",
            "technical_score": "Tech Score",
            "liquidity_score": "Liq Score",
            "pop_estimate": "POP",
            "final_score": "Final Score",
            "rating": "Rating",
            "at_2pct_target": "Meets 2%",
            "flags_str": "Flags",
            "explanation": "Explanation",
        }

        # Only include columns that actually exist
        available = {k: v for k, v in column_map.items() if k in df.columns}
        df = df.rename(columns=available)
        output_cols = [v for v in column_map.values() if v in df.columns]
        df = df[output_cols]

        # Format
        if "POP" in df.columns:
            df["POP"] = (df["POP"] * 100).round(1).astype(str) + "%"
        if "Monthly %" in df.columns:
            df["Monthly %"] = df["Monthly %"].round(2)
        if "Ann. %" in df.columns:
            df["Ann. %"] = df["Ann. %"].round(1)
        if "Meets 2%" in df.columns:
            df["Meets 2%"] = df["Meets 2%"].map({True: "✅", False: "❌"})

        return df

    def _llm_explanation(self, trade: dict) -> str:
        """
        Generates a 1-2 sentence plain-English explanation using LLM.
        Only called for top-N trades.
        """
        # With ollama we have no SDK client; every other provider needs one.
        if self.provider != "ollama" and not self.llm:
            return trade.get("explanation", "")

        prompt = (
            f"In 1-2 sentences, explain this options trade for a retail investor:\n"
            f"Stock: {trade['symbol']} @ ${trade.get('stock_price', '?')}\n"
            f"Strategy: {trade['strategy']}, Strike: ${trade.get('strike', '?')}, "
            f"Expiration: {trade.get('expiration', '?')}, "
            f"Premium: ${trade.get('premium', '?')}\n"
            f"Monthly return: {trade.get('monthly_return_pct', '?'):.1f}%, "
            f"Score: {trade.get('final_score', '?')}/100, Rating: {trade.get('rating', '?')}\n"
            f"Flags: {', '.join(trade.get('flags', []) or ['none'])}\n"
            f"Be direct and practical. Note the key reason it's rated {trade.get('rating', '?')}."
        )

        # Preferred: LOCAL ollama. On empty/error fall through to Claude below.
        if self.provider == "ollama":
            text = _ollama_explanation(prompt)
            if text:
                logger.info("[RecommendationAgent] explanation provider=ollama")
                return text
            logger.info("[RecommendationAgent] ollama empty — falling back to anthropic")

        try:
            # Fallback (or explicitly-selected) provider. When ollama was tried
            # and came back empty we route to anthropic; otherwise honor the
            # config-declared provider.
            provider = ("anthropic" if self.provider == "ollama"
                        else LLM["provider"])
            if provider == "anthropic":
                # Reuse an already-constructed client (imported at __init__)
                # so we don't hard-require the SDK when a client was injected.
                client = self.llm
                if client is None:
                    import anthropic
                    from config.settings import ANTHROPIC_API_KEY
                    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
                msg = client.messages.create(
                    model=LLM["model"],
                    max_tokens=LLM["max_tokens"],
                    messages=[{"role": "user", "content": prompt}]
                )
                logger.info("[RecommendationAgent] explanation provider=anthropic")
                return msg.content[0].text

            elif provider == "openai":
                import openai
                from config.settings import OPENAI_API_KEY
                client = openai.OpenAI(api_key=OPENAI_API_KEY)
                resp = client.chat.completions.create(
                    model="gpt-4o-mini",
                    max_tokens=LLM["max_tokens"],
                    messages=[{"role": "user", "content": prompt}]
                )
                logger.info("[RecommendationAgent] explanation provider=openai")
                return resp.choices[0].message.content

        except Exception as e:
            logger.warning(f"[RecommendationAgent] LLM error: {e}")

        return trade.get("explanation", "No explanation available.")
