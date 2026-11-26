"""Forge dashboard backend — path configuration.

All data paths resolve relative to FORGE_ROOT (default: the repo root two
levels up from this file). Override any path with an env var for VPS deploys.

The adapters read the SAME files the Streamlit dashboard reads, so the new
dashboard shows identical data (trades, scans, backtests, loops, etc.).
"""
from __future__ import annotations

import os
from pathlib import Path

# Repo root: forge-dashboard/backend/config.py -> forge/
FORGE_ROOT = Path(os.environ.get(
    "FORGE_ROOT",
    str(Path(__file__).resolve().parents[2]),
))

SOAI_ROOT = Path(os.environ.get("SOAI_ROOT", str(FORGE_ROOT / "stock-options-ai")))
ALGO_ROOT = Path(os.environ.get("ALGO_ROOT", str(FORGE_ROOT / "algo-trading")))
KALSHI_ROOT = Path(os.environ.get("KALSHI_ROOT", str(FORGE_ROOT / "kalshi")))
LOOPS_ROOT = Path(os.environ.get("LOOPS_ROOT", str(FORGE_ROOT / "research_loops")))

# Stock Options AI data files
SCAN_PATH = SOAI_ROOT / "scan_results_grouped_latest.json"
OWNED_PATH = SOAI_ROOT / "owned_positions.json"
MY_OPTIONS_PATH = SOAI_ROOT / "my_options.json"
CC_ANALYSIS_PATH = SOAI_ROOT / "cc_analysis_latest.json"
OWNED_PUTS_PATH = SOAI_ROOT / "owned_puts_ai_latest.json"
TRADER_STATE_PATH = SOAI_ROOT / "trader_state.json"
TRADER_HISTORY_PATH = SOAI_ROOT / "trader_history.json"
RESEARCH_CACHE_DIR = SOAI_ROOT / "research_cache"

# Algo-trading leaderboard
ALGO_SAVED_DIR = ALGO_ROOT / "backtests" / "saved"

# KNN · Kalshi BTC perp backtest (jobs/kalshi_knn_backtest.py writes this JSON;
# the live-trading dashboard reads it). The recompute endpoint shells out to the
# algo-trading numba venv to regenerate it.
KALSHI_KNN_PATH = Path(os.environ.get(
    "KALSHI_KNN_PATH",
    str(FORGE_ROOT / "forge-dashboard" / "data" / "kalshi_knn" / "result.json"),
))
KALSHI_KNN_JOB = ALGO_ROOT / "jobs" / "kalshi_knn_backtest.py"
KALSHI_KNN_PYTHON = Path(os.environ.get(
    "KALSHI_KNN_PYTHON", str(ALGO_ROOT / ".venv-bt" / "bin" / "python")))

# Kalshi
KALSHI_STATE_PATH = KALSHI_ROOT / "logs" / "paper_state.jsonl"
# Strategy-optimizer artifacts (written by the Claude agent on the VPS;
# absent locally — adapters degrade to empty structures).
KALSHI_AUDIT_PATH = KALSHI_ROOT / "audit" / "parameter_changes.jsonl"
KALSHI_REPORTS_DIR = KALSHI_ROOT / "reports"
KALSHI_MODEL_PATH = KALSHI_ROOT / "models" / "btc15m_latest.pkl"

# Kalshi 15-min predictor RESEARCH artifacts — written on the VPS by the daily
# strategy lab / search + the shadow recorder (kalshi/research/*). Champion files
# hold a literal JSON `null` when nothing robust is found; read_json returns None.
KALSHI_RESEARCH_DIR = KALSHI_ROOT / "research"
KALSHI_LAB_CHAMPION_PATH = KALSHI_RESEARCH_DIR / "lab_champion.json"
KALSHI_LAB_LOG_PATH = KALSHI_RESEARCH_DIR / "strategy_lab_log.jsonl"
KALSHI_LAB_REPORT_PATH = KALSHI_RESEARCH_DIR / "lab_report.txt"
KALSHI_SEARCH_CHAMPION_PATH = KALSHI_RESEARCH_DIR / "champion.json"
KALSHI_SEARCH_LOG_PATH = KALSHI_RESEARCH_DIR / "strategy_search_log.jsonl"
KALSHI_SHADOW_LOG_PATH = KALSHI_RESEARCH_DIR / "shadow_reversion.jsonl"
KALSHI_SHADOW_REPORT_PATH = KALSHI_RESEARCH_DIR / "shadow_reversion_report.txt"
# Internet model-research lab (Phase 4) — leaderboard of different model families.
INTERNET_MODEL_LAB_DIR = LOOPS_ROOT / "internet_model_lab"
INTERNET_MODEL_LAB_LEADERBOARD = INTERNET_MODEL_LAB_DIR / "leaderboard.json"
INTERNET_MODEL_LAB_LOG = INTERNET_MODEL_LAB_DIR / "runs.jsonl"

# Research loops
KALSHI_LOOP_DIR = LOOPS_ROOT / "kalshi_improver"
ALGO_LOOP_DIR = LOOPS_ROOT / "algo_improver"
# Kalshi BTC-perp strategy lab (24/7 multi-strategy backtest loop) — runner is
# algo-trading/jobs/perp_lab_loop.py; it writes state here (gitignored runtime).

# BTC Lab — the unified 15m-binary + BTC-perp research engine. The loop
# (algo-trading/btc_lab/loop.py) writes its tracked ledger here on the VPS:
# status.json / leaderboard.json / champions.json / active.json /
# switches.jsonl / variants.jsonl / search_state.json (gitignored runtime).
BTC_LAB_DIR = Path(os.environ.get("BTC_LAB_DIR", str(LOOPS_ROOT / "btc_lab")))

# Live Kalshi BTC-perp runner state (kalshi/jobs/knn_perp_loop.py writes this
# every tick; the live dashboard reads it).
KNN_PERP_STATE_PATH = Path(os.environ.get(
    "KNN_PERP_STATE", str(KALSHI_ROOT / "logs" / "knn_perp_state.json")))
# Dashboard arm/disarm control the runner reads each tick (written by the UI's
# Arm button via kalshi_perp_live.set_control — paper-only, never real money).
KNN_PERP_CONTROL_PATH = Path(os.environ.get(
    "KNN_PERP_CONTROL", str(KALSHI_ROOT / "logs" / "knn_perp_control.json")))

# Read-only aggregate status endpoint (/api/status). Token-gated + exposed outside
# the dashboard Basic Auth for programmatic monitoring. Unset -> endpoint disabled.
STATUS_TOKEN = os.environ.get("FORGE_STATUS_TOKEN", "").strip()

# Ad-hoc research history (~/.forge_research_history.jsonl)
RESEARCH_HISTORY_PATH = Path(os.environ.get(
    "RESEARCH_HISTORY_PATH",
    str(Path.home() / ".forge_research_history.jsonl"),
))

# Alpha portfolio — the long-only "beat the S&P 500" paper trader. State + history
# are written by stock-options-ai/trading/alpha_runner.py.
ALPHA_STATE_PATH = Path(os.environ.get(
    "ALPHA_STATE_PATH", str(SOAI_ROOT / "data" / "alpha_state.json")))
ALPHA_HISTORY_PATH = Path(os.environ.get(
    "ALPHA_HISTORY_PATH", str(SOAI_ROOT / "data" / "alpha_history.jsonl")))

# Research universe (S&P 500 ∪ Nasdaq-100 stocks + a curated ETF list), built by
# backend/build_universe.py. rh_research loads stocks.json (falls back to its inline
# list); the alpha trader / an ETF research view read etfs.json.
UNIVERSE_STOCKS_PATH = Path(os.environ.get(
    "UNIVERSE_STOCKS_PATH",
    str(FORGE_ROOT / "forge-dashboard" / "data" / "universe" / "stocks.json")))
UNIVERSE_ETFS_PATH = Path(os.environ.get(
    "UNIVERSE_ETFS_PATH",
    str(FORGE_ROOT / "forge-dashboard" / "data" / "universe" / "etfs.json")))
# One compact line per research build — composite + tier per symbol — so the screen
# can show score history / "what changed" (movers, new Buys) across rebuilds.
RH_SCREEN_HISTORY_PATH = Path(os.environ.get(
    "RH_SCREEN_HISTORY_PATH",
    str(FORGE_ROOT / "forge-dashboard" / "data" / "robinhood" / "research_history.jsonl")))

# Robinhood read-only snapshot (pulled via MCP; lives inside forge-dashboard/data)
# NOTE: FORGE_ROOT is the repo root, so the snapshot sits under forge-dashboard/.
RH_SNAPSHOT_PATH = Path(os.environ.get(
    "RH_SNAPSHOT_PATH",
    str(FORGE_ROOT / "forge-dashboard" / "data" / "robinhood" / "snapshot.json"),
))
RH_CC_ANALYSIS_PATH = Path(os.environ.get(
    "RH_CC_ANALYSIS_PATH",
    str(FORGE_ROOT / "forge-dashboard" / "data" / "robinhood" / "cc_analysis.json"),
))
# Appended on every snapshot ingest — accumulates the portfolio time-series.
RH_HISTORY_PATH = Path(os.environ.get(
    "RH_HISTORY_PATH",
    str(FORGE_ROOT / "forge-dashboard" / "data" / "robinhood" / "history.jsonl"),
))
# Robinhood analytics caches (price history, fundamentals, AI narratives) — used
# by rh_analytics (symbol detail / risk engine) and rh_ai (per-symbol AI).
RH_PRICES_DIR = Path(os.environ.get(
    "RH_PRICES_DIR", str(RH_SNAPSHOT_PATH.parent / "prices")))
RH_FUNDAMENTALS_PATH = Path(os.environ.get(
    "RH_FUNDAMENTALS_PATH", str(RH_SNAPSHOT_PATH.parent / "fundamentals.json")))
RH_AI_CACHE_DIR = Path(os.environ.get(
    "RH_AI_CACHE_DIR", str(RH_SNAPSHOT_PATH.parent / "ai_cache")))
# Quantitative research-screen output (rh_research; rebuilt lazily on a 12h TTL)
RH_RESEARCH_CACHE_PATH = Path(os.environ.get(
    "RH_RESEARCH_CACHE_PATH", str(RH_SNAPSHOT_PATH.parent / "research.json")))
# Weekly Claude read of every research name, written by
# stock-options-ai/jobs/ai_universe_read.py; ranked by adapters/rh_ai_reads.py.
RH_AI_READS_PATH = Path(os.environ.get(
    "RH_AI_READS_PATH", str(RH_SNAPSHOT_PATH.parent / "ai_reads.json")))

# Deep DCF — accepted investment-banking models written by the
# stock-options-ai/deep_dcf job (latest.json index, status.json, models/).
# The default must match deep_dcf.config.load()'s data_dir.
DEEP_DCF_DIR = Path(os.environ.get(
    "DEEP_DCF_DATA_DIR", str(RH_SNAPSHOT_PATH.parent.parent / "deep_dcf")))

# Local LLM for /api/robinhood/ask (no Anthropic API at runtime by design)
OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://127.0.0.1:11434")
RH_ASK_MODEL = os.environ.get("RH_ASK_MODEL", "llama3.2:3b")
# Per-symbol AI analysis model (rh_ai falls back to llama3.2:3b).
RH_ANALYSIS_MODEL = os.environ.get("RH_ANALYSIS_MODEL", "llama3.2:3b")

# OpenClaw gateway probe
OPENCLAW_PORT = int(os.environ.get("OPENCLAW_PORT", "18789"))

# CORS — allow the Next.js dev server + same-origin
CORS_ORIGINS = os.environ.get(
    "CORS_ORIGINS",
    "http://localhost:3000,http://127.0.0.1:3000",
).split(",")

# Concord (daily-vote trading club) — its isolated API runs as the `concord`
# user on :8780. The dashboard reads its admin overview via a static service
# token (CONCORD_ADMIN_TOKEN must match concord's .env). Empty token → adapter
# degrades to "unavailable".
CONCORD_API_URL = os.environ.get("CONCORD_API_URL", "http://127.0.0.1:8780").rstrip("/")
CONCORD_ADMIN_TOKEN = os.environ.get("CONCORD_ADMIN_TOKEN", "")

# ICT NQ forward test (a pre-registered shadow replay). Its timer writes
# status.json / ledger.csv / logs here, outside the repository.
FORWARD_TEST_DIR = Path(os.environ.get("FORWARD_TEST_DIR", str(Path.home() / "ict_forward")))
