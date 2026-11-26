"""
config/settings.py
Central configuration for all thresholds, constants, and strategy parameters.
Edit this file to tune the screener and scoring without touching logic code.
"""

import json
import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()


# ─── Environment ──────────────────────────────────────────────────────────────
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")
USE_MOCK_OPTIONS = os.getenv("USE_MOCK_OPTIONS", "true").lower() == "true"

# ─── API Keys ─────────────────────────────────────────────────────────────────
ALPACA_API_KEY = os.getenv("ALPACA_API_KEY", "")
ALPACA_SECRET_KEY = os.getenv("ALPACA_SECRET_KEY", "")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
FINNHUB_API_KEY = os.getenv("FINNHUB_API_KEY", "")

# The equity research screen's output, which the scan and the equity book read.
# It is written beside this project, by the dashboard backend.
_RESEARCH_JSON = str(Path(__file__).resolve().parents[2]
                     / "forge-dashboard" / "data" / "robinhood" / "research.json")

# ─── Universe / Screener Hard Filters ─────────────────────────────────────────
SCREENER = {
    "max_price": 100.0,               # Stock price ceiling
    "min_price": 5.0,                 # Avoid penny stocks
    "min_market_cap": 500_000_000,    # $500M minimum
    "min_avg_volume_30d": 1_000_000,  # 1M average daily shares
    "min_iv": 0.25,                   # 25% IV floor — need premium
    "max_iv": 1.50,                   # 150% IV ceiling — avoid chaos
    "earnings_buffer_days": 14,       # Skip stocks with earnings within 14 days
    "max_short_float": 0.40,          # Short interest < 40%
    # GATE on the composite screener_score (0-100). Previously screener_score only
    # SORTED — every hard-filter survivor was options-scanned regardless of quality,
    # so a 12/100 underlying got the same chain fetch + AI grade as a 95/100 one.
    # These cap the set that flows downstream: better picks + far less chain/AI cost.
    "min_screener_score": 35,         # drop sub-threshold names before options scanning
    "max_candidates": 50,             # cap the top-ranked names passed to the options agent
}

# ─── Options Chain Filters ────────────────────────────────────────────────────
OPTIONS = {
    "dte_min": 14,                    # Minimum days to expiration
    "dte_max": 60,                    # Maximum days to expiration
    "preferred_dte_min": 21,          # Ideal range lower bound
    "preferred_dte_max": 45,          # Ideal range upper bound
    "otm_pct_min": 0.02,              # Minimum OTM % (2% out of the money)
    "otm_pct_max": 0.20,              # Maximum OTM % (20% out)
    "min_open_interest": 100,         # Per-strike minimum OI
    "max_spread_pct": 0.20,           # Per-strike max bid-ask spread
    "min_premium": 0.05,              # Minimum premium ($5 per contract)
    # ─── Risk gates ─────────────────────────────────────────────────────────
    "min_pop": 0.50,                  # HARD FLOOR — never sell a put with <50% POP
    "preferred_pop_min": 0.65,        # Below this, trade is flagged "low POP"
    "delta_target_low": 0.10,         # For CSPs: prefer strikes with |delta| between
    "delta_target_high": 0.30,        #  10% and 30% (= 70-90% POP, "sweet spot")
}

# ─── Market Regime ────────────────────────────────────────────────────────────
# When VIX spikes, premium looks juicy but tail-risk is much higher.
# Forge throttles or pauses new opens based on VIX level.
MARKET_REGIME = {
    "vix_caution_level": 25,         # Above this, halve qty by rating
    "vix_pause_level": 35,           # Above this, no new opens (manage only)
    "vix_extreme_level": 45,         # Above this, close speculative shorts
    "cache_ttl_seconds": 600,        # Refresh VIX every 10 min
}

# ─── Return Targets ───────────────────────────────────────────────────────────
TARGETS = {
    "monthly_return_target": 0.02,    # 2% monthly target
    "monthly_return_max_score": 0.04, # 4% monthly → perfect return score
    "min_monthly_return": 0.005,      # 0.5% floor — trades below this aren't worth it
}

# ─── Risk Rules ───────────────────────────────────────────────────────────────
RISK = {
    "max_position_pct": 0.10,         # Max 10% of equity per single position
    "min_cash_buffer": 0.20,          # Free-cash floor when RESERVE is disabled (static fallback)
    "max_sector_concentration": 0.30, # Max 30% in one sector
    "min_downside_buffer_pct": 0.03,  # Strike must be ≥3% below current price (CSP)
    # ─── Run gating ──────────────────────────────────────────────────────────
    # When the market is closed (holidays, weekends, off-hours) the wheel run
    # only reconciles order statuses — no submits against stale prices.
    "require_market_open": os.getenv("REQUIRE_MARKET_OPEN", "true").lower() == "true",
    # A grouped scan older than this many hours must not drive NEW opens
    # (management of existing positions still runs).
    "max_scan_age_hours": float(os.getenv("MAX_SCAN_AGE_HOURS", "20")),
    # ─── BLOCK-ONLY buying-power preflight (added 2026-07-07) ────────────────
    # A cash-secured put is collateralized by the broker's OPTIONS buying power,
    # which can be LOWER than raw cash. The wheel sizes against its own
    # deployable_cash (since 2026-11-20: cash MINUS the collateral of open short
    # puts and resting sell-put orders, minus the RESERVE floor), so this gate is
    # now the broker-side second check (audit case: PYPL needed $4,084 collateral,
    # only $558 options BP left).
    # Before each order we require: collateral ≤ available options BP − buffer.
    # This can ONLY skip an order (recorded risk_blocked) — never place/enlarge.
    # Buffer = max(options_bp_buffer_usd, options_bp_buffer_pct × equity).
    "options_bp_buffer_usd": float(os.getenv("OPTIONS_BP_BUFFER_USD", "50")),
    "options_bp_buffer_pct": float(os.getenv("OPTIONS_BP_BUFFER_PCT", "0.0")),
    # ─── BLOCK-ONLY required-feed freshness SLAs (added 2026-07-07) ──────────
    # Extends the scan-age idea to every feed the OPEN step consumes. If a
    # REQUIRED feed is older than its SLA, the OPEN step DEGRADES to compute-
    # but-don't-place (existing dry_run path) and records freshness_blocked with
    # the stale feed's name. Management of existing positions is NOT gated.
    # NOTE: run_wheel reads the grouped scan (scan, gated by max_scan_age_hours)
    # and the research_cache (research_cache_max_age_h). It does NOT read the RH
    # snapshot or a fundamentals feed directly, so no SLA is added for those —
    # only feeds the wheel genuinely uses are gated.
    "research_cache_max_age_h": float(os.getenv("RESEARCH_CACHE_MAX_AGE_H", "48")),
    # ─── Position management (close & roll) ─────────────────────────────────
    "close_at_profit_pct": 0.80,      # Buy-to-close winners at 80% max profit
    "close_at_loss_multiplier": 2.5,  # Buy-to-close losers if premium ≥ 2.5× entry
    "roll_otm_pct_bump": 0.05,        # A roll's new strike must be ≥5% further OTM than the closed one
    # ─── Wheel-candidate test (relocated from trading/wheel_runner.py) ──────
    # A position can be force-closed on a loss ONLY if the underlying fails the
    # wheel-candidate test (we wouldn't be OK owning the shares at assignment).
    "wheel_candidate_min_would_own": int(
        os.getenv("WHEEL_CANDIDATE_MIN_WOULD_OWN", "6")),  # AI would_own_score floor to keep
    "wheel_candidate_bad_tags": tuple(                      # Ollama quality_tag → always exit
        t.strip().upper() for t in os.getenv(
            "WHEEL_CANDIDATE_BAD_TAGS", "SPECULATIVE,LEVERAGED_PROXY,DISTRESSED"
        ).split(",") if t.strip()),
    # ─── Position sizing by conviction ──────────────────────────────────────
    "qty_by_rating": {"A": 3, "B": 2, "C": 1, "D": 0},
}

# ─── Adaptive cash reserve ──────────────────────────────────────────────────
# The wheel stops opening new puts once free cash hits the "floor". Historically
# this was the static RISK["min_cash_buffer"] (20%) — a permanent lock that no
# code path ever deployed. RESERVE makes the floor adaptive + gradual instead
# (see trading/reserve.py): the TARGET floor is regime-scaled (low in calm =
# deploy the reserve, high in stress = hoard), the ENFORCED floor only eases
# toward that target a small step per run when LOWERING (slow release, so one
# calm reading can't dump all the dry powder) but may rise faster when the
# regime worsens (rebuild the cushion quickly), and an absolute catastrophe
# floor is never crossed. No manual lever — it releases/rebuilds itself.
# Set "enabled": False to fall back to the static min_cash_buffer.
RESERVE = {
    "enabled": True,
    # Regime → target free-cash floor (% of equity). Keys match classify_regime.
    "base_floor_by_regime": {
        "calm":    0.12,   # deploy: thin premiums, low risk — free ~8% of equity
        "caution": 0.16,
        "paused":  0.24,   # VIX high (no new opens anyway) — hold a fat cushion
        "extreme": 0.30,
    },
    "default_floor":    0.20,   # unknown/missing regime → legacy behavior
    "catastrophe_floor": 0.05,  # ABSOLUTE hard line — never deploy below this
    "max_release_step": 0.02,   # ≤2% of equity LOWER per run (slow release)
    "max_rebuild_step": 0.06,   # ≤6% of equity HIGHER per run (fast rebuild)
}

# ─── Equity-research overlay on the options scan ─────────────────────────────
# The wheel sells puts on names it's willing to OWN — the dashboard research
# screen (composite + confidence + DCF, deterministic) is exactly that judgment.
# Its score gets a bounded say in scan ranking, and low-confidence names (value
# traps / parabolic tapes) take a flat penalty so juicy premium alone can't rank
# them first. Names outside the research universe stay NEUTRAL (no coverage ≠
# signal); stale/missing research disables the overlay for the run. Pure rules —
# no LLM in this path.
RESEARCH_OVERLAY = {
    "enabled": os.getenv("RESEARCH_OVERLAY_ENABLED", "true").lower() == "true",
    "path": os.getenv("RESEARCH_OVERLAY_PATH", _RESEARCH_JSON),
    "max_age_hours": float(os.getenv("RESEARCH_OVERLAY_MAX_AGE_HOURS", "36")),
    # blended = (1-weight)*scan + weight*equity_composite
    "weight": float(os.getenv("RESEARCH_OVERLAY_WEIGHT", "0.20")),
    "low_conf_threshold": float(os.getenv("RESEARCH_OVERLAY_LOW_CONF", "0.5")),
    "low_conf_penalty": float(os.getenv("RESEARCH_OVERLAY_LOW_CONF_PENALTY", "10")),
}

# ─── Agentic position manager ─────────────────────────────────────────────────
# When enabled, an LLM agent with web search (Gemini with Google Search
# grounding by default, Claude as the fallback) makes the per-position
# close/hold/roll decision for positions in a "gray zone", instead of the fixed
# deterministic thresholds above. The deterministic rules remain the BASELINE:
# boring positions follow them with no API spend, and they are the fallback
# whenever the agent fails, times out, or returns low confidence.
#
#   mode:
#     "off"          → pure deterministic (original behavior, no LLM in the loop)
#     "advisory"     → agent decides + logs + notifies, but deterministic rules
#                      still EXECUTE the orders (safe shadow mode)
#     "full_execute" → agent's guardrailed decision executes real (paper) orders
AGENT = {
    "mode": os.getenv("TRADER_AGENT_MODE", "full_execute"),
    "model": "claude-haiku-4-5",
    "web_search_max_uses": 2,
    "min_confidence": 0.55,        # below this, fall back to the deterministic baseline
    "max_agent_calls_per_run": 8,  # cap API spend; highest-stress positions go first
    "decision_timeout_s": 45,

    # ─── Gray zone: which positions wake the agent ──────────────────────────
    # Outside every band below, the position is "boring" → deterministic baseline,
    # no LLM call. Inside any band → the agent is consulted.
    "gray_zone": {
        "profit_capture_band": (0.55, 0.97),  # captured% in band → ask (rule auto-closes ≥0.97)
        "loss_mult_band_low": 1.6,            # premium ≥1.6× entry → stress, ask the agent
        "dte_review_max": 10,                 # ≤10 DTE → gamma/assignment zone, ask
        "earnings_review_days": 7,            # earnings within Nd → ask
        "review_regimes": ("paused", "extreme"),  # stressed market → ask on every position
    },

    # ─── Hard guardrails the agent CANNOT override ──────────────────────────
    "kill_switch_loss_mult": 4.0,  # premium ≥4× entry → force-close regardless of agent

    # ─── Kill-switch ride-through (assignment instead of surrender) ─────────
    # At the kill switch, a FRESH wheel candidate (today's would_own + clean
    # quality tag + thesis not broken) converts to RIDE — accept assignment —
    # instead of force-closing at max loss. Rationale: the collateral is
    # already reserved (cash-secured), so for a name we'd own anyway,
    # assignment turns a forced realized loss into shares at (strike − premium)
    # that the CC leg then earns on. Every gate below must pass or the position
    # force-closes exactly as before; the switch itself is NOT removed.
    # Evidence (Jun–Jul 2026): 5 kill-switch closes on wheel candidates
    # (OXY/CLF/DKNG/DVN/HAL) realized −$1,775 while the ride-to-assignment
    # path marked −$1,424 (4 of 5 better) — and each had been told RIDE
    # repeatedly on the way down, absorbing the drawdown then locking the loss.
    "kill_switch_ride": {
        "enabled": True,
        "max_itm_pct": 0.15,                # underlying < strike×(1−this) → too deep, close
        "max_assignment_pct_equity": 0.35,  # Σ riding collateral (strike×100×qty) ≤ this × equity
        "require_thesis_not_broken": True,  # a broken thesis always closes
    },
    "roll": {
        "min_otm_bump": RISK["roll_otm_pct_bump"],  # new strike ≥ this much further OTM (0.05)
        "min_credit_per_share": 0.0,                # roll must be net credit (≥ $0)
        "target_dte_min": OPTIONS["preferred_dte_min"],  # prefer 21–45 DTE roll target
        "target_dte_max": OPTIONS["preferred_dte_max"],
        "max_dte": 60,                              # never roll past 60 DTE
        # Two-phase roll: only sell the new leg after the buy-to-close leg FILLS.
        "fill_timeout_s": float(os.getenv("ROLL_FILL_TIMEOUT_S", "10")),  # wait this long for the close fill
        "fill_poll_s": 2.0,                         # close-leg fill poll interval
    },
}

# ─── Covered-call leg (sell-side income on assigned/owned shares) ──────────────
# Closes the wheel: when a CSP is assigned we hold 100 shares per contract; this
# leg auto-sells OTM covered calls against those shares to collect income, then
# buy-to-closes them at a profit. Strike selection is THESIS-TIERED: the per-
# ticker conviction in trading/thesis_store.py picks the posture, so a name we
# still love gets a conservative far-OTM call (keep the upside) while a name whose
# thesis is broken gets a near-ATM call (max premium + invite assignment = exit).
#
# The OPEN is rules-first (consistent with the CSP open staying pure-rules), but
# an optional agent may RE-PICK among the strikes that already pass the cost-basis
# + liquidity filters — it can be smart, but it can never violate the floor.
#
# HARD GUARDRAIL: never sell a call below the position's EFFECTIVE cost basis
# (= assignment price − all premium collected on the ticker), so being called away
# is always net-green — EXCEPT when the thesis is BROKEN and we want out, where
# the floor is lifted to let the shares get called away for max income.
#
#   mode:
#     "off"          → leg disabled entirely
#     "advisory"     → compute + log + Discord what it WOULD sell, place no orders
#     "full_execute" → place real (paper) covered-call orders
COVERED_CALLS = {
    "enabled": os.getenv("COVERED_CALLS_ENABLED", "true").lower() == "true",
    "mode": os.getenv("COVERED_CALLS_MODE", "full_execute"),  # off | advisory | full_execute

    # ─── Expiration window ──────────────────────────────────────────────────
    "dte_min": 14,
    "dte_max": 60,
    "target_dte": 35,                 # aim near 30–45 DTE peak-theta zone

    # ─── Income goal (soft target, not a hard floor) ────────────────────────
    "target_monthly_return": 0.02,    # 2%/mo premium yield TARGET on share value
    "min_monthly_return": 0.004,      # below this, not worth capping upside → skip
    "min_premium": 0.05,              # $5/contract hard floor
    "max_otm_pct": 0.25,              # don't sell deep-OTM no-premium lottery calls

    # ─── Thesis-tiered posture → target call delta ──────────────────────────
    # intact   = bullish, keep the shares → far OTM, low delta, accept <2%
    # weakening= neutral income           → standard ~0.30 delta, push for 2%
    # broken   = we want out              → near-ATM, high delta, max premium + exit
    "delta_targets": {"intact": 0.20, "weakening": 0.30, "broken": 0.45},
    # OTM fallback proxy (used only when the chain has no delta), keyed by posture
    "otm_fallback": {"intact": 0.08, "weakening": 0.05, "broken": 0.015},

    # ─── Cost-basis floor ───────────────────────────────────────────────────
    # Enforced for intact/weakening (strike ≥ effective basis AND OTM). For the
    # BROKEN posture the basis floor is lifted — strikes only need to be just-OTM
    # so we can sell near-ATM for max premium and welcome assignment (a clean exit).
    "require_above_cost_basis": True,

    # ─── Order placement ────────────────────────────────────────────────────
    "sell_undercut": 0.05,             # quote the STO this far below mid (floored at bid) so it fills

    # ─── Management (buy-to-close winners) ──────────────────────────────────
    "profit_take_pct": 0.65,           # BTC the call once 65% of max premium captured

    # ─── Regime gate ────────────────────────────────────────────────────────
    "skip_open_in_extreme": True,      # no new CC opens when VIX extreme (mgmt still runs)

    # ─── Share-lot exits (how long assigned shares may be held) ─────────────
    # The CC postures handle the happy paths; these bound everything else.
    #   stop       — price < effective_basis×(1−stop_below_basis_pct) → sell the
    #                UNCOVERED shares at market (post-assignment downside is
    #                bounded like any equity stop; shares pinned under a short
    #                call exit via assignment instead, posture forced broken)
    #   escalate   — lot held > escalate_after_days with realized CC income
    #                below escalate_min_monthly_income (of share value, per
    #                month) → posture bumps one tier (intact→weakening→broken)
    #                so strikes walk toward ATM and assignment-away gets likelier
    #   unwritable — posture broken AND the chain has no writable call (nothing
    #                ≥ min_premium) → sell the shares directly; don't wait on a
    #                chain that won't pay for the exit
    "share_exits": {
        "enabled": True,
        "stop_below_basis_pct": 0.20,
        "escalate_after_days": 45,
        "escalate_min_monthly_income": 0.01,
        "sell_when_broken_unwritable": True,
    },

    # ─── Agentic override (re-picks among pre-filtered safe candidates only) ─
    "agent": {
        "enabled": os.getenv("COVERED_CALLS_AGENT", "true").lower() == "true",
        # Text generator: COVERED_CALLS_LLM_PROVIDER ∈ {gemini, anthropic}, default
        # gemini (see covered_call_runner._cc_provider). Gemini falls back to Claude
        # on None/parse-fail, then to the deterministic baseline. "model" = Claude
        # fallback model; "gemini_model" = None → helper default (GEMINI_MODEL).
        "model": "claude-haiku-4-5",
        "gemini_model": None,
        "web_search_max_uses": 2,
        "min_confidence": 0.55,        # below this, keep the deterministic baseline pick
        "max_calls_per_run": 6,        # cap API spend per wheel tick
        "timeout_s": 45,
    },
}

# ─── Alpha portfolio (long-only equity/ETF "beat the S&P 500" trader) ─────────
# A separate $1M paper account from the wheel. Core-satellite: a low-cost ETF
# core keeps it market-like; a satellite sleeve of the top research-ranked names
# supplies the alpha. Rankings come from the forge-dashboard research engine
# (composite score + factors + DCF) live, or a point-in-time price-momentum
# proxy in the backtest. Benchmark = SPY total return. Paper only — `mode` mirrors
# the wheel's: off | advisory (compute + log + write state, no orders) | full_execute.
#
# Posture: "active conviction". The goal is to BEAT SPY, and you can't beat SPY
# by holding mostly SPY — so the passive VOO/QQQ anchor is deliberately SMALL and
# the book's return is driven by a diversified set of high-conviction single names
# (undervalued/DCF, growth, quality) sourced from the dashboard research engine,
# conviction-weighted (strongest ideas get the most capital), with a dynamic
# T-bill (BIL) reserve that grows as dry powder when good buys are scarce and
# deploys into equities when they are plentiful.
#
# Every knob in ALPHA_CLAMPS is tunable by the local-LLM governor (alpha_governor.py)
# WITHIN the hard [min,max] clamp — the deterministic policy here is the baseline and
# fallback; the LLM only nudges, never invents tickers and never removes a guardrail.
# ─────────────────────────────────────────────────────────────────────────────
ALPHA = {
    "mode": os.getenv("ALPHA_MODE", "advisory"),   # off | advisory | full_execute
    "start_capital": 1_000_000.0,
    "benchmark": "SPY",
    # DEDICATED Alpaca paper account — MUST be separate from the wheel's keys so the
    # two books never collide. Read from env so credentials never live in the repo.
    # If unset, the runner stays virtual (sizes off start_capital, no positions read)
    # and refuses full_execute.
    "alpaca_key_env": "ALPHA_ALPACA_API_KEY",
    "alpaca_secret_env": "ALPHA_ALPACA_SECRET_KEY",
    "alpaca_base_env": "ALPHA_ALPACA_BASE_URL",   # e.g. https://paper-api.alpaca.markets
    "cash_buffer": 0.01,                 # tiny uninvested cash for settlement/slippage slack

    # ── Live-run safety gates (structural — never LLM-tunable, not in ALPHA_CLAMPS) ──
    "require_market_open": os.getenv("ALPHA_REQUIRE_MARKET_OPEN", "true").lower() == "true",
    "allow_live": os.getenv("ALPHA_ALLOW_LIVE", "false").lower() == "true",  # full_execute on a non-paper host needs this
    "research_max_age_hours": float(os.getenv("ALPHA_RESEARCH_MAX_AGE_HOURS", "36")),
    "research_min_confidence": float(os.getenv("ALPHA_RESEARCH_MIN_CONFIDENCE", "0.5")),
    "research_min_usable": int(os.getenv("ALPHA_RESEARCH_MIN_USABLE", "15")),

    # ── Core sleeve — SMALL market anchor (fraction of the non-reserve equity book) ──
    "core_weight": 0.12,                 # ~12% VOO/QQQ; alpha comes from stock selection
    "core_etfs": {"VOO": 0.6, "QQQ": 0.4},

    # ── Satellite — the conviction book (all names from research.json) ──
    "satellite_max_names": 22,           # target 18–25 high-conviction names
    "satellite_min_names": 12,           # shown on the dashboard and tunable; the builder does not enforce a minimum
    "hold_rank_ratio": 1.5,              # a HELD name keeps its slot while it ranks within
                                         # 1.5x the satellite size (33 of 22); a new name must
                                         # make the top 22. Stops names at the cut swapping in
                                         # and out on every research rebuild. Not governor-tunable.
    "satellite_min_score": 55,           # composite floor to be eligible
    "eligible_tiers": ("strong", "buy", "watch"),   # loosened; 'watch' gated by score below
    "watch_min_score": 68,               # a 'watch'-tier name needs a higher score to qualify
    "weight_scheme": "conviction",       # conviction = composite score tilted up by DCF upside
    "dcf_tilt": 0.5,                     # how strongly DCF upside boosts a name's weight
    "max_position_weight": 0.08,         # per-name cap (fraction of the whole book)
    "max_sector_weight": 0.30,           # per-sector cap (fraction of the whole book)

    # ── Dynamic T-bill reserve (dry powder) — replaces the old static cash sleeve ──
    "reserve_etf": "BIL",                # 1–3mo T-bills: ultra-safe, liquid, no duration risk
    "reserve_floor": 0.03,               # min reserve when good buys are plentiful
    "reserve_ceiling": 0.18,             # max reserve when good buys are scarce
    "opportunity_score_min": 72,         # an "extremely good buy" = composite ≥ this AND
    "opportunity_dcf_min": 35.0,         #   DCF upside% ≥ this
    "opportunity_rich_count": 12,        # ≥ this many good buys → reserve at the floor
    "opportunity_lean_count": 3,         # ≤ this many good buys → reserve at the ceiling

    # ── Sell discipline (evaluated every run, before the rebalance diff) ──
    "take_profit_pct": 0.30,             # +30% unrealized gain → take profit (trim/exit)
    "take_profit_trim": 0.5,             # fraction of the position to trim on a take-profit
    "dcf_exhausted_upside": 5.0,         # DCF upside% ≤ this (price ≥ intrinsic) → exit
    "stop_loss_pct": 0.15,               # −15% unrealized loss → exit (avoid further loss)
    "thesis_exit_tiers": ("pass", "avoid"),  # research tier fell to these → thesis broken, exit
    "thesis_exit_min_score": 45,         # composite fell below this → thesis broken, exit
    "sell_min_hold_days": 5,             # don't churn: a name held fewer days is not trimmed on
                                         # take-profit or rotated out on a ranking change (stop-
                                         # loss and broken-thesis exits still apply at once)
    "reentry_cooldown_days": 5,          # discipline-exited names sit out this many days (no rebuy loop)
    "trim_cap_days": 5,                  # after a take-profit trim, hold the reduced size this long
                                         # (else the next rebalance buys the trim straight back)

    # ── Rebalance / trading discipline ──
    "rebalance_cadence": "weekly",       # informational; cron drives the actual cadence
    "drift_band": 0.02,                  # don't trade a name unless |target−current| weight > 2%
    "min_trade_usd": 50.0,
    "transaction_cost_bps": 1.0,         # modeled per-side commission in the backtest
    "slippage_bps": 3.0,                 # modeled per-side slippage in the backtest (honest friction)

    # ── Paths (relative paths resolve against the project root) ──
    "research_path": os.getenv("ALPHA_RESEARCH_PATH", _RESEARCH_JSON),
    "state_path": os.getenv("ALPHA_STATE_PATH", "data/alpha_state.json"),
    "policy_path": os.getenv("ALPHA_POLICY_PATH", "data/alpha_policy.json"),
    "positions_path": os.getenv("ALPHA_POSITIONS_PATH", "data/alpha_positions.json"),

    # ── Backtest-only: point-in-time price-momentum ranking (no fundamental lookahead) ──
    "bt_mom_windows": [63, 21],          # ~3m, ~1m trading-day momentum
    "bt_trend_sma": 50,                  # trend = price / SMA50 − 1
    "bt_vol_window": 21,                 # realized-vol lookback (lower is better)
    "bt_warmup": 63,                     # bars before the first ranking
    "bt_rebalance_days": 21,             # ~monthly
}

# Hard clamps: the ONLY knobs the governor may tune, each bounded to a safe range so
# a bad LLM output can never blow up the book. Keys not listed here are structural
# (mode, capital, account, tickers, tiers, paths) and are NEVER LLM-tunable.
ALPHA_CLAMPS = {
    "core_weight": (0.05, 0.30),
    "satellite_max_names": (12, 30),
    "satellite_min_names": (8, 20),
    "satellite_min_score": (45, 70),
    "watch_min_score": (60, 82),
    "dcf_tilt": (0.0, 1.0),
    "max_position_weight": (0.04, 0.12),
    "max_sector_weight": (0.20, 0.40),
    "reserve_floor": (0.02, 0.10),
    "reserve_ceiling": (0.10, 0.25),
    "opportunity_score_min": (65, 85),
    "opportunity_dcf_min": (20.0, 60.0),
    "opportunity_rich_count": (6, 25),
    "opportunity_lean_count": (0, 6),
    "take_profit_pct": (0.15, 0.60),
    "take_profit_trim": (0.25, 1.0),
    "dcf_exhausted_upside": (-5.0, 15.0),
    "stop_loss_pct": (0.08, 0.30),
    "thesis_exit_min_score": (30, 55),
    "sell_min_hold_days": (0, 30),
    "drift_band": (0.01, 0.05),
}

# The local-LLM governor that reviews performance and re-tunes the knobs above.
ALPHA_GOVERNOR = {
    "mode": os.getenv("ALPHA_GOVERNOR_MODE", "advisory"),   # off | advisory | apply
    "model": os.getenv("ALPHA_GOVERNOR_MODEL", "llama3.2:3b"),
    "ollama_url": os.getenv("OLLAMA_URL", "http://127.0.0.1:11434"),
    "timeout_s": int(os.getenv("ALPHA_GOVERNOR_TIMEOUT", "300")),  # 8GB box: cold llama3.2:3b load ~150s
    "temperature": 0.3,
    "min_history_points": 4,      # need ≥ this many perf points before a re-tune is trusted
    "max_step_frac": 0.34,        # LLM may move a knob at most 34% of its clamp range per run
    "log_path": os.getenv("ALPHA_GOVERNOR_LOG", "data/alpha_governor_log.jsonl"),
}


# ─── ALPHA policy overlay (deterministic baseline ← governor's clamped tweaks) ───
_PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _alpha_resolve(p: str) -> Path:
    """Resolve a (possibly relative) ALPHA path against the project root."""
    q = Path(p)
    return q if q.is_absolute() else (_PROJECT_ROOT / q)


def clamp_alpha_policy(knobs: dict) -> dict:
    """Return only the recognised, in-range knobs from `knobs`.

    Unknown/structural keys are dropped; every tunable knob is clamped to its
    ALPHA_CLAMPS range and coerced to the base type (int knobs stay int). This is
    the hard guardrail the governor cannot cross — a garbage LLM output collapses
    to the nearest safe value or is ignored entirely.
    """
    out: dict = {}
    for k, v in (knobs or {}).items():
        if k not in ALPHA_CLAMPS:
            continue
        lo, hi = ALPHA_CLAMPS[k]
        try:
            val = float(v)
        except (TypeError, ValueError):
            continue
        val = max(lo, min(hi, val))
        if isinstance(ALPHA.get(k), int):
            val = int(round(val))
        out[k] = val
    return out


def load_alpha_policy(cfg: dict | None = None) -> dict:
    """ALPHA defaults with the on-disk governor policy overlaid (clamped).

    Precedence: alpha_policy.json (clamped) > ALPHA defaults. The file is optional;
    if it's missing or unreadable the caller gets the plain defaults, so the runner
    and backtest work identically with or without a governor having ever run.
    """
    merged = dict(cfg or ALPHA)
    path = _alpha_resolve(merged.get("policy_path", "data/alpha_policy.json"))
    if path.exists():
        try:
            raw = json.loads(path.read_text())
            knobs = raw.get("knobs", raw)   # accept {"knobs":{...},...} or a flat dict
            merged.update(clamp_alpha_policy(knobs))
        except Exception:
            pass   # bad policy file → silently keep the safe defaults
    return merged

# ─── Liquidity Scoring ────────────────────────────────────────────────────────
LIQUIDITY = {
    "oi_for_max_score": 10_000,       # OI ≥ 10,000 = perfect OI score
    "volume_for_max_score": 1_000,    # Volume ≥ 1,000 = perfect volume score
    "spread_pct_for_zero": 0.20,      # Spread ≥ 20% = zero spread score
}

# ─── Default Stock Universe ───────────────────────────────────────────────────
# Optionable stocks under $100 with real revenue or thesis. Max price gate
# (SCREENER.max_price) handles names that drift over $100 — keep them here so
# they're rescanned if/when they pull back.
# Bankrupt, delisted and penny names are taken out as they turn up, and a
# renamed ticker is replaced by its successor (SQ became XYZ, GPS became GAP):
# a retired symbol can be reissued to an unrelated company. The quantum names
# (RGTI, IONQ, QBTS) are speculative and are scored accordingly.
DEFAULT_UNIVERSE = [
    # Fintech / online brokers
    "SOFI", "HOOD", "COIN", "FUTU", "PYPL", "XYZ", "AFRM",
    # Tech / semis / cloud / SaaS (all <$100)
    "PLTR", "AMD", "INTC", "CSCO", "SNAP", "PINS", "RBLX",
    "HPQ", "HPE", "MRVL", "ON", "MU", "TWLO", "DOCU", "ZM",
    "ROKU", "NET", "OKTA", "S",
    # Mobility / gig / travel
    "UBER", "LYFT", "DKNG", "ABNB", "DASH",
    # EVs / autos
    "RIVN", "LCID", "NIO", "XPEV", "LI", "F", "GM",
    # Banks / financials
    "BAC", "WFC", "C", "USB", "SCHW", "AIG",
    # Telecom / media
    "VZ", "T", "WBD", "SIRI",
    # Travel / cruise / airlines / leisure
    "NCLH", "CCL", "AAL", "DAL", "UAL", "LUV", "MGM", "WYNN",
    # Retail / consumer
    "GAP", "M", "KSS", "BBY", "EBAY", "ETSY", "KR",
    # Consumer staples / dividend
    "KO", "MO", "KHC", "TAP",
    # Healthcare / pharma
    "PFE", "BMY", "GILD", "CVS",
    # Energy
    "KMI", "OXY", "SLB", "HAL", "DVN",
    # Materials / industrial
    "CLF", "FCX",
    # REITs
    "O", "VICI",
    # Meme / high-IV (non-penny, optionable)
    "GME",
    # Crypto miners (high-IV, BTC-correlated)
    "MARA", "RIOT", "HUT",
    # Real estate / housing
    "OPEN",
    # Quantum computing (speculative, high IV)
    "RGTI", "IONQ", "QBTS",
]

# ─── LLM Config ───────────────────────────────────────────────────────────────
LLM = {
    "provider": "anthropic",
    "model": "claude-haiku-4-5",
    "max_tokens": 200,
    "use_llm_explanations": True,         # AI reasoning per top-N pick
    "top_n_for_llm": 20,                  # Generate reasoning for the top 20 trades
}


# ─── ETF blocklist (hard exclusion from universe) ────────────────────────────
# Leveraged/inverse/volatility ETFs are the worst possible CSP underlyings:
# POP is unreliable when the underlying can move 15%+ on a normal day. We
# scan and grade equities only; ad-hoc ETF lookups still work via the
# dashboard for users who want to peek.
ETF_BLOCKLIST = {
    # Leveraged long and inverse index/sector funds (2x/3x)
    "TQQQ", "SQQQ", "SOXL", "SOXS", "SPXL", "SPXS", "FNGU", "FNGD",
    "TNA", "TZA", "TMF", "TMV", "TYD", "TYO", "ERX", "ERY",
    "LABU", "LABD", "CURE", "RXL", "FAS", "FAZ", "RETL", "DRV",
    "DPST", "WEBL", "WEBS", "BOIL", "KOLD", "GUSH", "DRIP",
    "JNUG", "JDST", "NUGT", "DUST", "YANG", "YINN", "CWEB",
    # Crypto-leveraged
    "BITX", "BITU", "ETHU", "ETHD", "MSTU", "MSTX", "MSTZ", "BITI",
    # Volatility
    "UVXY", "VXX", "SVXY", "VIXY", "VIXM", "UVIX", "SVIX",
    # Single-stock leveraged (TSLL, NVDL, etc.)
    "TSLL", "TSLT", "TSLQ", "NVDL", "NVDU", "NVDD", "NVDX",
    "AAPU", "AAPD", "AMZU", "AMZD", "GGLL", "GOOX", "MSFU", "MSFD",
    "METU", "METD", "BABX", "BABU", "BRKU", "BRKD",
}


# ─── Flag multipliers (replaces linear flag_penalty) ─────────────────────────
# Each flag now scales the raw score down. Multiplicative, so multiple flags
# compound — better reflects how a single bad signal should haircut quality.
# Defaults to 0.92 (8% haircut) for any unrecognized flag.
FLAG_MULTIPLIERS = {
    "earnings":     0.75,   # earnings in <30d
    "wide_spread":  0.85,
    "low_oi":       0.92,
    "itm":          0.70,   # already in the money — bad CSP, bad CC
    "locks_loss":   0.50,   # CC strike below cost basis
    "bankruptcy":   0.60,
    "dilution":     0.65,
    "declining":    0.88,
    "below_200ma":  0.92,
}


# ─── Analyst consensus (wheel conviction) ────────────────────────────────────
# A CSP wheel can be ASSIGNED the stock, so the Street's Buy/Hold/Sell view and
# the mean price target (implied upside) are conviction inputs — not trivia.
# Applied as a MULTIPLIER on the final composite_rank_score (and fed to the AI
# would-own analyst), never a hard filter: a Hold name with strong trade metrics
# can still rank, just not above comparable Buy names. Neutral (1.0) when fewer
# than `min_analysts` cover the name. This is what stops a Hold-rated blue-chip
# (e.g. GS) from topping the list purely on premium + balance-sheet quality.
ANALYST_CONSENSUS = {
    "enabled": True,
    "min_analysts": 5,                 # below this, coverage is too thin to weight
    "rating_mult": {                   # yfinance recommendationKey -> base multiplier
        "strong_buy":   1.08,
        "buy":          1.04,
        "outperform":   1.04,
        "hold":         0.90,
        "neutral":      0.90,
        "underperform": 0.78,
        "sell":         0.75,
        "strong_sell":  0.60,
    },
    # implied upside to mean target -> adjustment (rewards room to run; penalizes
    # names already at/above target, where assignment = downside w/o appreciation)
    "upside_bands": [                  # (min_upside_fraction, multiplier)
        (0.15,  1.03),                 # > +15% upside to target
        (0.05,  1.00),                 # +5% .. +15%
        (-0.05, 0.97),                 # -5% .. +5% (near/at target)
        (-1.00, 0.92),                 # price above mean target
    ],
    "mult_floor": 0.55,
    "mult_cap":   1.10,
}


# ─── Regime-aware scoring weights ────────────────────────────────────────────
# REWEIGHTED 2026-06-29 after a point-in-time backtest of 2,150 recommended CSPs
# (May 7–Jun 12 2026) replayed to expiry. What the data showed:
#   • The old fundamentals-heavy weighting was ANTI-predictive — its top-quartile
#     trades realized the WORST returns (~-5.6%) and lost out-of-sample.
#   • The signals that actually protected capital were POP and DOWNSIDE BUFFER
#     (depth OTM). A POP+buffer-led score beat the old weighting out-of-sample
#     (+0.63%/cycle, profitable rate 68%→75%).
# So: POP up, a new explicit `buffer` term added, `fundamentals` ~halved, raw
# `monthly_return`/`iv_rank` trimmed to noise level. Each regime sums to 1.00 and
# stays POP-dominant; defensiveness scales POP+buffer further under stress.
SCORE_WEIGHTS_BY_REGIME = {
    "calm": {
        "monthly_return":  0.03,
        "pop":             0.35,
        "buffer":          0.20,   # downside cushion (deeper OTM = safer)
        "liquidity":       0.12,
        "fundamentals":    0.18,
        "iv_rank":         0.02,
        "earnings_safety": 0.10,
    },
    "caution": {
        "monthly_return":  0.03,
        "pop":             0.40,
        "buffer":          0.20,
        "liquidity":       0.12,
        "fundamentals":    0.13,
        "iv_rank":         0.02,
        "earnings_safety": 0.10,
    },
    "paused": {
        "monthly_return":  0.02,
        "pop":             0.44,   # max defensive
        "buffer":          0.22,
        "liquidity":       0.10,
        "fundamentals":    0.10,
        "iv_rank":         0.02,
        "earnings_safety": 0.10,
    },
    "extreme": {
        "monthly_return":  0.02,
        "pop":             0.46,
        "buffer":          0.24,
        "liquidity":       0.08,
        "fundamentals":    0.08,
        "iv_rank":         0.02,
        "earnings_safety": 0.10,
    },
}

# Covered calls use a SEPARATE weight set. The 2026-06-29 reweight above was
# backtested on cash-secured PUTS only — its POP+buffer tilt and halved
# fundamentals are right for put-selling but wrong for covered calls (a CC's
# "buffer" is premium/upside cushion, not depth-OTM, and you already own the
# stock so fundamentals matter MORE, not less). These are the pre-reweight
# weights, kept intact so the CSP change can't silently re-tune the CC engine.
# No `buffer` key → composite_trade_score's buffer term is a no-op for CCs.
CC_SCORE_WEIGHTS_BY_REGIME = {
    "calm":    {"monthly_return": 0.05, "pop": 0.25, "liquidity": 0.20,
                "fundamentals": 0.35, "iv_rank": 0.05, "earnings_safety": 0.10},
    "caution": {"monthly_return": 0.05, "pop": 0.35, "liquidity": 0.20,
                "fundamentals": 0.25, "iv_rank": 0.05, "earnings_safety": 0.10},
    "paused":  {"monthly_return": 0.05, "pop": 0.40, "liquidity": 0.20,
                "fundamentals": 0.20, "iv_rank": 0.05, "earnings_safety": 0.10},
    "extreme": {"monthly_return": 0.05, "pop": 0.45, "liquidity": 0.20,
                "fundamentals": 0.15, "iv_rank": 0.05, "earnings_safety": 0.10},
}


# ─── Deep DCF (full valuation models, inside a hard budget) ─────────────────
# A full investment-banking DCF (three scenarios, sensitivity grids, every cell
# a live formula) built by Claude from a company's SEC filings.
# Code: deep_dcf/ + jobs/deep_dcf_runner.py. ADVISORY ONLY — nothing here feeds
# the composite score or any trader.
#
# Budget: this job gets a fixed monthly slice (`monthly_ceiling_usd`), reserved
# before each run and paced through the billing cycle (deep_dcf/budget.py).
# `cycle_day` is the day the cycle renews. The ceiling is a hard limit: every
# run is counted at its full per-run cap until it settles, so worst-case spend
# cannot pass it, and an environment variable can lower it but never raise it.
def _dd_num(env: str, default: float, lo: float, hi: float) -> float:
    """A FINITE number in [lo, hi] from the environment, else `default`.

    Two failure modes this exists for. (1) settings.py is imported by the wheel
    and alpha traders too, so a typo in a DEEP_DCF_* variable must never raise
    here. (2) A non-finite value must never disable a spending limit:
    min(nan, 70.0) is nan, and every comparison against nan is False, so a
    `spent + cap > ceiling` test would pass forever."""
    import math
    try:
        v = float(os.getenv(env, "").strip() or default)
    except ValueError:
        return float(default)
    if not math.isfinite(v):
        return float(default)
    return min(max(v, lo), hi)


DEEP_DCF = {
    "enabled": os.getenv("DEEP_DCF_ENABLED", "1").strip().lower() not in ("0", "false", "no", "off"),
    # Who gets modelled:
    #   "held"    names actually held (the brokerage account and the paper
    #             equity book), nothing else
    #   "events"  held names, plus a NON-held name only when an entry event fires
    #             (alpha buy order, or promotion to tier strong)
    #   "full"    also back-fills every strong / high-confidence-buy / shortlist
    #             name once and refreshes entrants each quarter (~57 names)
    #   "universe" every eligible research name in `universe_tiers`, refreshed
    #             when a new 10-K/10-Q lands, plus everything above      ← default
    # The batch engine (below) made a model about ten times cheaper, which is
    # what made universe-wide coverage fit inside the same ceiling.
    "scope": os.getenv("DEEP_DCF_SCOPE", "universe").strip().lower(),
    # The whole research universe (~600 names), pass tier included. Blocked
    # archetypes stay refused.
    "universe_tiers": ("strong", "buy", "watch", "pass"),    # research tiers the universe scope covers
    # How a model is produced:
    #   "batch"    one structured request per company via the Message Batches API
    #              (50% off, cached system prompt); the workbook is built by
    #              deep_dcf/tools/build_model.py.                          ← default
    #   "agentic"  the original headless `claude -p` run of the dcf-model skill.
    #              Roughly ten times the cost. Still available per run
    #              (--engine agentic).
    "engine": os.getenv("DEEP_DCF_ENGINE", "batch").strip().lower(),
    "batch": {
        # Requests per weekly window. The ceiling and pacing bound the dollars;
        # this bounds the blast radius of a systematic fault.
        "window_cap": int(_dd_num("DEEP_DCF_BATCH_WINDOW_CAP", 150, 0, 400)),
        "max_tokens": 16000,          # thinking + the JSON answer; also the reservation bound
        "effort": "medium",
        "discount": 0.5,              # Batch API: 50% off every token, cache included
        # Standard $/MTok (before the batch discount). Used to reserve before a
        # request and to settle it from its usage block — change with the models.
        "token_rates": {
            "claude-opus-5-5": {"input": 4.0, "output": 20.0, "cache_read": 0.20,
                                "cache_write_5m": 5.0, "cache_write_1h": 8.0},
            "claude-sonnet-5-5": {"input": 2.0, "output": 10.0, "cache_read": 0.10,
                                  "cache_write_5m": 2.5, "cache_write_1h": 4.0},
        },
        # Health gate: if the last finished batch had at least `health_min_results`
        # models and fewer than `health_min_accept` of them passed, scheduled
        # submissions stop until a human looks (a manual --force overrides).
        "health_min_results": 5,
        "health_min_accept": 0.5,
    },
    "monthly_ceiling_usd": _dd_num("DEEP_DCF_MONTHLY_CEILING_USD", 70.0, 0.0, 70.0),
    "cycle_day": int(_dd_num("DEEP_DCF_CYCLE_DAY", 15, 1, 28)),    # day of the month the spending cycle starts
    "window_cap": int(_dd_num("DEEP_DCF_WINDOW_CAP", 5, 0, 10)),   # models per weekly window
    "fast_lane_cap": 1,                                            # sell-decision models per day
    # The larger model for held names, the smaller one for the rest.
    "model_held": os.getenv("DEEP_DCF_MODEL_HELD", "claude-opus-5-5"),
    "model_other": os.getenv("DEEP_DCF_MODEL_OTHER", "claude-sonnet-5-5"),
    # --max-budget-usd per run. Also the amount RESERVED in the ledger before
    # the run starts, so a crash counts at its worst case. Measured 2026-11-15:
    # a clean agentic run cost $0.25 on Sonnet and $1.05 on Opus; the caps
    # leave ~4x for a run that needs several fix-and-recheck rounds.
    "run_cap_usd": {
        "claude-opus-5-5": _dd_num("DEEP_DCF_RUN_CAP_OPUS_USD", 4.0, 0.25, 10.0),
        "claude-sonnet-5-5": _dd_num("DEEP_DCF_RUN_CAP_SONNET_USD", 2.0, 0.25, 10.0),
    },
    # The CLI checks --max-budget-usd BETWEEN turns, so a run can finish one turn
    # past it. The CLI is therefore given this fraction of the reserved cap; the
    # rest of the reservation is headroom for that last turn, so a settled run
    # stays inside what was reserved for it.
    "cli_budget_fraction": 0.75,
    # Clean runs take 2-5 minutes. 25 minutes is a stuck run, not a slow one.
    "run_timeout_s": int(_dd_num("DEEP_DCF_RUN_TIMEOUT_S", 1500, 120, 3600)),
    # ── triggers ────────────────────────────────────────────────────────────
    "min_refresh_days": 21,     # an accepted model younger than this refreshes only on a NEW filing
    "stale_days": 120,          # held name: refresh a model older than this regardless
    "divergence_pts": 20.0,     # deterministic DCF upside moved this many points since the model
    "price_move_pct": 25.0,     # …or price moved this much
    "sell_proximity": 0.90,     # alpha position this close to a sell rule = decision pending
    "fail_park_days": 30,       # 2 straight failed runs park the name this long
    "retry_after_days": 6,      # one attempt per name per window
    # ── eligibility ─────────────────────────────────────────────────────────
    # An FCF DCF is the wrong tool for these (mirrors rh_archetype.BLOCKED). Refused
    # even on a manual trigger unless --force (which is logged).
    "blocked_archetypes": ("FINANCIAL", "REIT", "RESOURCE", "PRE_PROFIT"),
    "min_fiscal_years": 3,      # of revenue + operating income + CFO in SEC XBRL
    "revenue_mismatch_pct": 10.0,   # XBRL vs fundamentals.json latest-FY revenue; above = skip
    # ── WACC inputs handed to the model ─────────────────────────────────────
    "equity_risk_premium": 0.055,   # same ERP as the deterministic engine
    # CAPM is unusable at the extremes. The published beta is bounded to this
    # range (and further, so base + bear WACC stay inside accept.wacc_max at the
    # current risk-free rate) BEFORE the model sees it: NVDA's 2.25 would give a
    # 17.5% cost of equity and a run that is paid for and then rejected.
    "beta_clamp": (0.70, 1.70),
    "risk_free_fallback": 0.045,    # only if the FRED 10Y fetch fails
    # ── acceptance: what a delivered model must satisfy to be published ─────
    # Checked by Python against the LibreOffice-recalculated workbook, never
    # taken from the model's own summary.
    "accept": {
        "value_tol": 0.005,          # summary number vs recalculated cell (relative)
        "wacc_min": 0.05, "wacc_max": 0.16,
        "tg_min": 0.0, "tg_max": 0.045,
        "min_wacc_tg_spread": 0.02,
        # A 5-year DCF at WACC ~9% / g ~2.5% carries ~70-80% of EV in the
        # terminal value by construction, so the skill's 50-70% "typical" band
        # is a warning here, not a gate. Above 90% the model is all terminal.
        "tv_fraction_warn": 0.80, "tv_fraction_max": 0.90,
        "tv_fraction_min": 0.20,     # below this the "PV of terminal value" cell is something else
        "fair_to_price_min": 0.2, "fair_to_price_max": 5.0,   # outside = artifact, not a target
        "min_formulas": 150,
        "price_tol": 0.01,           # model's price input vs inputs.json
        "shares_tol": 0.10,          # diluted shares vs the corroborated count in inputs.json
        "revenue_base_tol": 0.10,    # model's base revenue vs LTM / latest FY
    },
    "sec_user_agent": os.getenv("SEC_USER_AGENT", "ForgeResearch/1.0 personal-research-tool"),
}


# ─── AI reads (weekly Claude read of EVERY research name) ───────────────────
# One short structured read per company — five business dimensions scored 0-10,
# a one-line thesis and a one-line risk — for the whole research universe, once
# a week. The dashboard ranks the reads across the universe and uses that rank
# as the composite's `ai` factor, so the factor averages to neutral and every
# name is treated the same. Code: ai_reads/ + jobs/ai_universe_read.py; the
# calibration lives in forge-dashboard/backend/adapters/rh_ai_reads.py.
#
# It replaced a local-model verdict that covered 35 of 598 names and said SELL
# on 77% of them.
#
# Cost: a few thousand tokens a name on the small model, well under a dollar
# for the universe. `run_cap_usd` and `monthly_ceiling_usd` are still enforced
# through the same reserve-then-settle ledger as the deep DCF. The ceiling is
# hard-capped at $4 so that the valuation models ($70) plus these reads stay
# inside one fixed monthly budget; an env var can lower it, never raise it.
AI_READS = {
    "enabled": os.getenv("AI_READS_ENABLED", "1").strip().lower() not in ("0", "false", "no", "off"),
    "model": os.getenv("AI_READS_MODEL", "claude-haiku-5-5"),
    "effort": os.getenv("AI_READS_EFFORT", "medium"),
    "max_tokens": 4000,                 # thinking counts toward this
    "workers": int(_dd_num("AI_READS_WORKERS", 4, 1, 8)),
    # A name read within this many days is not re-read. With a weekly timer the
    # oldest read the dashboard can see is about refresh_days + 7, which must
    # stay inside rh_ai_reads.FRESH_DAYS (11) or the factor switches off for
    # everyone until the next run. A read from mid-week is simply redone on Sunday.
    "refresh_days": 3,
    "run_cap_usd": _dd_num("AI_READS_RUN_CAP_USD", 1.5, 0.05, 1.5),
    "monthly_ceiling_usd": _dd_num("AI_READS_MONTHLY_CEILING_USD", 4.0, 0.0, 4.0),
    "cycle_day": 15,                    # same cycle as the deep DCF
    "input_tokens_worst_case": 3000,    # per call, for the pre-flight budget check
    "max_consecutive_errors": 8,        # API trouble: stop the run rather than burn the list
    # $ per million tokens for the configured model (Claude Haiku 5.5, prompts
    # under 100K tokens). Used only to account for spend from each response's
    # usage block — change them with the model.
    "rates_per_mtok": {"input": 0.10, "output": 0.50, "cache_read": 0.01, "cache_write": 0.125},
}
