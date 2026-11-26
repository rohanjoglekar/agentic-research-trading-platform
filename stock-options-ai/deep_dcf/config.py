"""Deep-DCF settings object.

Policy knobs live in config/settings.py:DEEP_DCF (project convention: thresholds
are never hardcoded in scripts). This module only turns that dict plus the
deployment paths into one frozen `Settings`; tests build `Settings` directly.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

PKG_DIR = Path(__file__).resolve().parent
SOAI_DIR = PKG_DIR.parent                       # …/stock-options-ai
FORGE_DIR = SOAI_DIR.parent                     # …/forge


def _p(name: str, default: Path) -> Path:
    raw = os.environ.get(name, "").strip()
    return Path(raw) if raw else Path(default)


@dataclass(frozen=True)
class Settings:
    # ── inputs the queue is built from ──────────────────────────────────────
    research_path: Path
    fundamentals_path: Path
    alpha_state_path: Path
    alpha_store_path: Path
    verdicts_url: str
    # ── results, ledger, state (runtime data — gitignored, never in git) ────
    data_dir: Path
    # ── sandbox ─────────────────────────────────────────────────────────────
    sandbox_user: str
    sandbox_home: Path          # the sandbox user's HOME (writable by it)
    runs_dir: Path              # root-owned parent of the per-run work dirs
    opt_dir: Path               # root-owned: tools/, venv/, skills/dcf-model/
    claude_bin: str
    # ── policy (from config.settings.DEEP_DCF) ──────────────────────────────
    enabled: bool = True
    scope: str = "events"
    monthly_ceiling_usd: float = 70.0
    cycle_day: int = 15
    window_cap: int = 5
    fast_lane_cap: int = 1
    model_held: str = "claude-opus-5-5"
    model_other: str = "claude-sonnet-5-5"
    run_cap_usd: dict = field(default_factory=lambda: {"claude-opus-5-5": 4.0,
                                                       "claude-sonnet-5-5": 2.0})
    cli_budget_fraction: float = 0.75
    run_timeout_s: int = 1500
    min_refresh_days: int = 21
    stale_days: int = 120
    divergence_pts: float = 20.0
    price_move_pct: float = 25.0
    sell_proximity: float = 0.90
    fail_park_days: int = 30
    retry_after_days: int = 6
    blocked_archetypes: tuple = ("FINANCIAL", "REIT", "RESOURCE", "PRE_PROFIT")
    min_fiscal_years: int = 3
    revenue_mismatch_pct: float = 10.0
    equity_risk_premium: float = 0.055
    beta_clamp: tuple = (0.70, 1.70)
    risk_free_fallback: float = 0.045
    accept: dict = field(default_factory=dict)
    sec_user_agent: str = "ForgeResearch/1.0 personal-research-tool"
    # ── batch engine ────────────────────────────────────────────────────────
    engine: str = "batch"
    universe_tiers: tuple = ("strong", "buy", "watch")
    batch_window_cap: int = 150
    batch_max_tokens: int = 16000
    batch_effort: str = "medium"
    batch_discount: float = 0.5
    token_rates: dict = field(default_factory=lambda: {
        "claude-opus-5-5": {"input": 4.0, "output": 20.0, "cache_read": 0.20,
                            "cache_write_5m": 5.0, "cache_write_1h": 8.0},
        "claude-sonnet-5-5": {"input": 2.0, "output": 10.0, "cache_read": 0.10,
                              "cache_write_5m": 2.5, "cache_write_1h": 4.0}})
    batch_health_min_results: int = 5
    batch_health_min_accept: float = 0.5

    # ── derived paths ───────────────────────────────────────────────────────
    @property
    def ledger_path(self) -> Path:
        return self.data_dir / "ledger.jsonl"

    @property
    def state_path(self) -> Path:
        return self.data_dir / "state.json"

    @property
    def latest_path(self) -> Path:
        return self.data_dir / "latest.json"

    @property
    def public_path(self) -> Path:
        return self.data_dir / "public.json"

    @property
    def models_dir(self) -> Path:
        return self.data_dir / "models"

    @property
    def cache_dir(self) -> Path:
        return self.data_dir / "cache"

    @property
    def lock_path(self) -> Path:
        return self.data_dir / "runner.lock"

    @property
    def tools_dir(self) -> Path:
        return self.opt_dir / "tools"

    @property
    def venv_bin(self) -> Path:
        return self.opt_dir / "venv" / "bin"

    @property
    def skill_dir(self) -> Path:
        return self.opt_dir / "skills" / "dcf-model"

    def run_cap(self, model: str) -> float:
        """--max-budget-usd for this model. An unknown model gets the SMALLEST
        configured cap (never an unbounded run)."""
        caps = {k: float(v) for k, v in self.run_cap_usd.items()}
        return caps.get(model, min(caps.values(), default=5.0))

    def acc(self, key: str) -> float:
        return float(self.accept[key])

    def api_key(self) -> tuple[str, str]:
        """(key, env-var name it came from). A dedicated key wins; otherwise the
        shared one. The key is never logged — log the NAME only."""
        for name in ("DEEP_DCF_ANTHROPIC_API_KEY", "ANTHROPIC_API_KEY"):
            val = os.environ.get(name, "").strip()
            if val and not val.startswith("your_") and val != "sk-dummy":
                return val, name
        return "", ""


def load() -> Settings:
    from config.settings import DEEP_DCF as D   # late: settings.py runs load_dotenv()

    dash = FORGE_DIR / "forge-dashboard" / "data"
    scope = str(D.get("scope", "universe"))
    B = dict(D.get("batch") or {})
    engine = str(D.get("engine", "batch"))
    return Settings(
        research_path=_p("DEEP_DCF_RESEARCH_PATH", dash / "robinhood" / "research.json"),
        fundamentals_path=_p("DEEP_DCF_FUNDAMENTALS_PATH", dash / "robinhood" / "fundamentals.json"),
        alpha_state_path=_p("DEEP_DCF_ALPHA_STATE_PATH", SOAI_DIR / "data" / "alpha_state.json"),
        alpha_store_path=_p("DEEP_DCF_ALPHA_STORE_PATH", SOAI_DIR / "data" / "alpha_positions.json"),
        verdicts_url=os.environ.get("DEEP_DCF_VERDICTS_URL",
                                    "http://127.0.0.1:8700/api/robinhood/verdicts"),
        data_dir=_p("DEEP_DCF_DATA_DIR", dash / "deep_dcf"),
        sandbox_user=os.environ.get("DEEP_DCF_SANDBOX_USER", "forge-dcf"),
        sandbox_home=_p("DEEP_DCF_SANDBOX_HOME", Path("/var/lib/forge-dcf/home")),
        runs_dir=_p("DEEP_DCF_RUNS_DIR", Path("/var/lib/forge-dcf/runs")),
        opt_dir=_p("DEEP_DCF_OPT_DIR", Path("/opt/forge-dcf")),
        claude_bin=os.environ.get("DEEP_DCF_CLAUDE_BIN",
                                  "/opt/forge-dcf/claude/node_modules/.bin/claude"),
        enabled=bool(D["enabled"]),
        scope=scope if scope in ("held", "events", "full", "universe") else "events",
        monthly_ceiling_usd=float(D["monthly_ceiling_usd"]),
        cycle_day=min(max(int(D["cycle_day"]), 1), 28),
        window_cap=max(int(D["window_cap"]), 0),
        fast_lane_cap=max(int(D["fast_lane_cap"]), 0),
        model_held=str(D["model_held"]),
        model_other=str(D["model_other"]),
        run_cap_usd=dict(D["run_cap_usd"]),
        cli_budget_fraction=min(max(float(D["cli_budget_fraction"]), 0.25), 1.0),
        run_timeout_s=int(D["run_timeout_s"]),
        min_refresh_days=int(D["min_refresh_days"]),
        stale_days=int(D["stale_days"]),
        divergence_pts=float(D["divergence_pts"]),
        price_move_pct=float(D["price_move_pct"]),
        sell_proximity=float(D["sell_proximity"]),
        fail_park_days=int(D["fail_park_days"]),
        retry_after_days=int(D["retry_after_days"]),
        blocked_archetypes=tuple(D["blocked_archetypes"]),
        min_fiscal_years=int(D["min_fiscal_years"]),
        revenue_mismatch_pct=float(D["revenue_mismatch_pct"]),
        equity_risk_premium=float(D["equity_risk_premium"]),
        beta_clamp=tuple(D["beta_clamp"]),
        risk_free_fallback=float(D["risk_free_fallback"]),
        accept=dict(D["accept"]),
        sec_user_agent=str(D["sec_user_agent"]),
        engine=engine if engine in ("batch", "agentic") else "batch",
        universe_tiers=tuple(D.get("universe_tiers") or ("strong", "buy", "watch")),
        batch_window_cap=max(int(B.get("window_cap", 150)), 0),
        batch_max_tokens=min(max(int(B.get("max_tokens", 16000)), 2000), 32000),
        batch_effort=str(B.get("effort", "medium")),
        batch_discount=min(max(float(B.get("discount", 0.5)), 0.0), 1.0),
        token_rates=dict(B.get("token_rates") or {}),
        batch_health_min_results=int(B.get("health_min_results", 5)),
        batch_health_min_accept=float(B.get("health_min_accept", 0.5)),
    )
