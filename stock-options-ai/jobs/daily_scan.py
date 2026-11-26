"""
jobs/daily_scan.py

The morning put-selling scan.

    python jobs/daily_scan.py --live            # the real scan (cron runs this with --notify)
    python jobs/daily_scan.py                   # offline rehearsal on mock data

Pipeline:
  1. Load the universe
  2. StockScreener        → candidates that pass the hard filters
  3. OptionsAgent         → every put worth considering on each candidate
  4. RecommendationAgent  → scored, rated and ranked
  5. Save the ranked table (CSV)
  6. Group by stock, run the per-stock analyst on the best 20, and save the
     grouped file the options trader reads

A live scan that cannot reach its data source stops with nothing saved. It
never substitutes mock data, because the trader would act on it. A mock run
writes its own `mock_` files and never touches the ones the trader reads.
"""

import sys
import os
import datetime
import json

# Ensure project root is in path when run directly
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd
from loguru import logger

from config.settings import (
    DEFAULT_UNIVERSE,
    USE_MOCK_OPTIONS,
    LOG_LEVEL,
)
from data.mock_data import generate_mock_universe
from screener.stock_screener import StockScreener
from agents.options_agent import OptionsAgent
from agents.recommendation_agent import RecommendationAgent
from agents.per_stock_analyst import group_by_stock, analyze_top_stocks


def _configure_logging() -> None:
    """Console at LOG_LEVEL plus a daily log file beside this script. Called by
    the command line only, so importing this module changes no logging."""
    logger.remove()
    logger.add(sys.stderr, level=LOG_LEVEL, format="{time:HH:mm:ss} | {level} | {message}")
    log_dir = os.path.dirname(os.path.abspath(__file__))
    logger.add(os.path.join(log_dir, "scan_log_{time:YYYY-MM-DD}.log"), rotation="1 day", retention="7 days")


def run_full_scan(
    universe: list = None,
    use_mock: bool = None,
    save_csv: bool = True,
    output_dir: str = ".",
) -> pd.DataFrame:
    """
    Runs the complete stock + options research pipeline.

    Args:
        universe: list of ticker symbols (defaults to config DEFAULT_UNIVERSE)
        use_mock: use mock options data (defaults to USE_MOCK_OPTIONS env var)
        save_csv: whether to save the output files
        output_dir: directory to save them in

    Returns:
        (ranked DataFrame of trade recommendations, grouped per-stock list) —
        EVERY return path yields this 2-tuple; both callers unpack two values.
    """
    start_time = datetime.datetime.now()
    if use_mock is None:
        use_mock = USE_MOCK_OPTIONS

    logger.info("=" * 60)
    logger.info(f"DAILY SCAN STARTED — {start_time.strftime('%Y-%m-%d %H:%M:%S')}")
    logger.info(f"Mode: {'MOCK' if use_mock else 'LIVE'}")
    logger.info("=" * 60)

    # ─── Step 1: Stock Universe ───────────────────────────────────────────
    if universe is None:
        universe = DEFAULT_UNIVERSE

    logger.info(f"[Step 1] Universe: {len(universe)} tickers")

    # ─── Step 2: Stock Screener ───────────────────────────────────────────
    logger.info("[Step 2] Running stock screener...")

    data_client = None
    if use_mock:
        # Generate mock universe data (no API calls)
        raw_df = generate_mock_universe(tickers=universe)
        screener = StockScreener()  # no clients needed for mock
        candidates = screener.run(mock_df=raw_df)
    else:
        # Live data: Alpaca for everything (price, bars, technicals, options).
        try:
            from data.alpaca_options_client import AlpacaOptionsClient
            data_client = AlpacaOptionsClient()
        except Exception as e:
            # No data source means no scan. Mock prices must never stand in for a
            # live run: the trader reads this scan's output and would act on it.
            logger.error(f"AlpacaOptionsClient init failed: {e} — live scan aborted, nothing saved")
            return pd.DataFrame(), []
        # AlpacaOptionsClient is duck-compatible with the methods StockScreener
        # calls on yf_client (get_stock_info, get_technical_data, days_to_earnings).
        screener = StockScreener(yf_client=data_client)
        candidates = screener.run(universe=universe)

    logger.info(f"[Step 2] Screener passed: {len(candidates)} candidates")

    if candidates.empty:
        logger.warning("No candidates passed screening. Check filters or universe.")
        # Mirror the normal (df, grouped) return shape — both callers unpack two values.
        return pd.DataFrame(), []

    # ─── Step 3: Options Scan ─────────────────────────────────────────────
    logger.info("[Step 3] Scanning options chains...")

    options_agent = OptionsAgent(options_client=data_client, use_mock=use_mock, csp_only=True)
    opportunities = options_agent.run(candidates)

    logger.info(f"[Step 3] Raw opportunities found: {len(opportunities)}")

    if not opportunities:
        logger.warning("No options opportunities found.")
        # Mirror the normal (df, grouped) return shape — both callers unpack two values.
        return pd.DataFrame(), []

    # Only trades that clear every hard gate (minimum POP, downside buffer, ...).
    # A thin day means fewer ideas, or none: the gates are never relaxed to fill
    # the list.
    valid_opps = options_agent.filter_valid_only(opportunities)
    logger.info(f"[Step 3] Valid opportunities (after flag filter): {len(valid_opps)}")

    # Hard 2% threshold — strategy policy: don't even consider sub-2% trades.
    from config.settings import TARGETS
    target_pct = TARGETS["monthly_return_target"] * 100
    before = len(valid_opps)
    valid_opps = [o for o in valid_opps if (o.get("monthly_return_pct") or 0) >= target_pct]
    logger.info(f"[Step 3] After 2%/mo gate: {before} → {len(valid_opps)}")

    # ─── Step 4: Score & Rank ─────────────────────────────────────────────
    logger.info("[Step 4] Scoring and ranking opportunities...")

    # Regime-aware scoring + POP self-calibration
    if use_mock:
        regime, pop_bias = "calm", 1.0        # a rehearsal reads no market data
    else:
        try:
            from data.market_regime import classify_regime
            from data.pop_calibration import get_current_bias
            regime = classify_regime().get("regime") or "caution"
            pop_bias = get_current_bias()
            logger.info(f"[Step 4] regime={regime}, pop_bias={pop_bias:.3f}")
        except Exception as e:
            logger.warning(f"[Step 4] regime/bias lookup failed: {e}")
            regime, pop_bias = "caution", 1.0     # blind is not calm
    rec_agent = RecommendationAgent(regime=regime, pop_bias=pop_bias)
    if use_mock:
        rec_agent.use_llm = False             # no model is asked to explain random prices
    final_df = rec_agent.run(valid_opps)

    logger.info(f"[Step 4] Final recommendations: {len(final_df)} trades")

    # ─── Step 4b: equity-research overlay (deterministic) ────────────────
    # Blend the dashboard research screen's composite/confidence/DCF into the
    # scan score — "would I own this stock?" now shapes "should I sell this
    # put?". Degrades to a no-op when research is stale/missing.
    research_book: dict = {}
    try:
        from data.research_overlay import apply_overlay, load_research
        research_book = load_research()
        final_df, overlay_note = apply_overlay(final_df, research_book)
        if overlay_note:
            logger.info(f"[Step 4b] {overlay_note}")
    except Exception as e:
        logger.warning(f"[Step 4b] research overlay skipped: {e}")

    # ─── Step 5: Save Output ──────────────────────────────────────────────
    # A mock run keeps to its own files: the trader and the dashboard read the
    # unprefixed ones.
    prefix = "mock_" if use_mock else ""
    if save_csv and not final_df.empty:
        timestamp = start_time.strftime("%Y%m%d_%H%M")
        csv_path = os.path.join(output_dir, f"{prefix}scan_results_{timestamp}.csv")
        latest_path = os.path.join(output_dir, f"{prefix}scan_results_latest.csv")
        final_df.to_csv(csv_path, index=False)
        final_df.to_csv(latest_path, index=False)
        logger.info(f"[Step 5] Saved to {csv_path}")

    # ─── Step 6: Group by stock + per-stock AI analysis with web search ────
    grouped: list[dict] = []
    if not final_df.empty:
        grouped = group_by_stock(final_df, num_stocks=20, strikes_per_stock=5)
        logger.info(f"[Step 6] Grouped into {len(grouped)} stocks")
        # Carry the equity-research context onto each per-stock row so the
        # dashboard candidate views can show WHY a name ranks where it does.
        try:
            from data.research_overlay import annotate_grouped
            annotate_grouped(grouped, research_book)
        except Exception as e:
            logger.warning(f"[Step 6] grouped research annotation skipped: {e}")
        if not use_mock:
            # The analyst researches real companies at real prices; on mock data
            # it would spend model calls and write theses about random numbers.
            analyze_top_stocks(grouped)
        if save_csv:
            grouped_path = os.path.join(output_dir, f"{prefix}scan_results_grouped_latest.json")
            # Strip non-serializable numpy / pandas types before writing
            safe = json.loads(pd.DataFrame(grouped).to_json(orient="records"))
            # Atomic write: the wheel reads this file every run — a torn
            # truncate-then-dump handed it partial JSON (crash) or an empty file.
            tmp_path = grouped_path + ".tmp"
            with open(tmp_path, "w") as f:
                json.dump(safe, f, indent=2, default=str)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_path, grouped_path)
            logger.info(f"[Step 6] Saved grouped JSON to {grouped_path}")

    # ─── Summary ──────────────────────────────────────────────────────────
    elapsed = (datetime.datetime.now() - start_time).seconds
    logger.info("=" * 60)
    logger.info(f"SCAN COMPLETE in {elapsed}s")

    if not final_df.empty and "Rating" in final_df.columns:
        rating_counts = final_df["Rating"].value_counts().to_dict()
        logger.info(f"Ratings: {rating_counts}")

        if "Final Score" in final_df.columns:
            top = final_df.iloc[0]
            ticker_col = "Ticker" if "Ticker" in final_df.columns else final_df.columns[1]
            logger.info(f"Top trade: {top.get(ticker_col, '?')} — "
                       f"{top.get('Strategy', '?')} @ strike {top.get('Strike', '?')} — "
                       f"Score {top.get('Final Score', '?')}/100")

    logger.info("=" * 60)

    return final_df, grouped


def _send_discord(text: str) -> None:
    """Internal: post a single message (auto-chunked at 1900 chars)."""
    import requests
    bot_token = os.environ.get("DISCORD_BOT_TOKEN", "").strip()
    channel_id = os.environ.get("DISCORD_CHANNEL_ID", "").strip()
    if not bot_token or bot_token.startswith("your_") or not channel_id:
        return
    for chunk in [text[i:i+1900] for i in range(0, len(text), 1900)]:
        try:
            r = requests.post(
                f"https://discord.com/api/v10/channels/{channel_id}/messages",
                headers={"Authorization": f"Bot {bot_token}", "Content-Type": "application/json"},
                json={"content": chunk}, timeout=10,
            )
            r.raise_for_status()
        except Exception as e:
            logger.warning(f"Discord chunk post failed: {e}")


def _digest_top10(grouped: list) -> str:
    """Top 10 CSPs with full per-stock detail."""
    today = datetime.datetime.now().strftime("%b %d, %Y")
    if not grouped:
        return f"**Daily CSP Scan — {today}**\n\nNo candidates passed today's filters."
    lines = [f"**Daily CSP Scan — {today}**",
             f"_{len(grouped)} stocks ranked — top 10 below_", ""]
    for i, s in enumerate(grouped[:10], 1):
        sym = s.get("symbol", "?")
        rating = s.get("best_rating", "?")
        score = float(s.get("best_score", 0))
        wo = s.get("would_own_score", "?")
        price = s.get("current_price")
        best = (s.get("strikes") or [{}])[0]
        strike = best.get("Strike", "?")
        exp = best.get("Expiration", "?")
        prem = best.get("Premium", "?")
        mo = best.get("Monthly %", "?")
        pop = best.get("POP", "?")
        price_str = f"${price:.2f}" if isinstance(price, (int, float)) else "?"
        lines.append(
            f"`#{i:>2}` **{sym}** @ {price_str} · ${strike}P {exp} · "
            f"${prem}/sh ({mo}%/mo) · POP {pop} · "
            f"{rating} ({score:.0f}) · would-own {wo}/10"
        )
        blurb = (s.get("analysis") or "").strip().split("\n", 1)[0]
        if blurb:
            lines.append(f"  └ {blurb[:240]}")
    return "\n".join(lines)


def _digest_my_options() -> str:
    """User's tracked options with action signals."""
    try:
        from data.my_options import evaluate_all
    except Exception as e:
        return f"**My Options** — load error: {e}"
    records = evaluate_all()
    if not records:
        return "**My Options** — nothing tracked. Add positions on the My Options page."
    lines = [f"**My Options — {len(records)} tracked**"]
    for r in records[:10]:
        st = r["status"]
        emoji = {"CLOSE":"🟢","ROLL":"🟠","AT_RISK":"🔴","CONSIDER_CLOSE":"🟡","HOLD":"⚪"}.get(st.get("action"), "⚪")
        line = (f"{emoji} **{r['symbol']}** {r['option_type'].replace('_',' ').title()} "
                f"${r['strike']:.0f} {r['expiration']} ({r['contracts']}x) · "
                f"{st.get('action','HOLD')}")
        if st.get("dte") is not None:
            line += f" · {st['dte']}d"
        if st.get("captured_pct_est") is not None:
            line += f" · {st['captured_pct_est']:+.0f}% captured"
        lines.append(line)
        if st.get("note"):
            lines.append(f"  └ {st['note'][:200]}")
    return "\n".join(lines)


def _digest_trader() -> str:
    """Today's trader actions: opens, closes, regime."""
    try:
        from jobs.daily_trader_summary import build_summary, render_text
    except ModuleNotFoundError:
        return ""                       # the summary job is not part of this checkout
    try:
        return render_text(build_summary())
    except Exception as e:
        return f"**Trader Summary** — error: {e}"


def post_grouped_digest_to_discord(grouped: list) -> None:
    """Composite daily digest: scan top-10 + tracked options + trader.

    Posted as multiple messages so each section is readable.
    """
    bot_token = os.environ.get("DISCORD_BOT_TOKEN", "").strip()
    channel_id = os.environ.get("DISCORD_CHANNEL_ID", "").strip()
    if not bot_token or bot_token.startswith("your_") or not channel_id or channel_id.startswith("your_"):
        logger.info("Discord not configured; skipping digest post")
        return
    parts = [
        _digest_top10(grouped),
        _digest_my_options(),
        _digest_trader(),
    ]
    for p in parts:
        if p and p.strip():
            _send_discord(p)
    logger.info(f"Discord composite digest posted ({len(parts)} sections)")



if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Run the options research scan")
    parser.add_argument("--mock", action="store_true", help="Use mock data (no API calls)")
    parser.add_argument("--live", action="store_true", help="Use live market data (Alpaca)")
    parser.add_argument("--no-save", action="store_true", help="Don't save any output files")
    parser.add_argument("--tickers", nargs="+", help="Specific tickers to scan")
    parser.add_argument("--notify", action="store_true", help="Post the digest to Discord (cron use)")
    args = parser.parse_args()
    _configure_logging()

    use_mock = not args.live            # a bare run is an offline rehearsal on mock data

    universe = args.tickers if args.tickers else None

    df, grouped = run_full_scan(
        universe=universe,
        use_mock=use_mock,
        save_csv=not args.no_save,
        output_dir=".",
    )

    if not df.empty:
        print("\n=== TOP 10 TRADES ===")
        display_cols = ["Ticker", "Strategy", "Strike", "Monthly %", "Rating", "Final Score"]
        available = [c for c in display_cols if c in df.columns]
        print(df[available].head(10).to_string(index=False))

    if args.notify:
        post_grouped_digest_to_discord(grouped)
