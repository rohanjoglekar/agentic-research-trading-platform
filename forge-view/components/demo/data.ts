"use client";

// One place where the /demo pages read the platform's API. Every field is
// optional on purpose: the pages are built so that a missing number means a
// missing sentence or tile, never a broken page.
//
// What is deliberately NOT read here: the options book's "net premium" and
// "win rate" (lifetime.net_premium, stats.win_rate_pct). Both are computed from
// submitted orders, including ones that never filled, so they overstate the
// result. Everything used below comes from the broker's account value, its
// live positions, or orders that actually filled.

import useSWR from "swr";
import { fetcher } from "@/lib/api";

export interface Analytics {
  available?: boolean;
  as_of?: string;
  n_days?: number;
  since?: string;
  headline?: {
    total_return_pct?: number | null;
    max_drawdown_pct?: number | null;
    sharpe?: number | null;
    vol_ann_pct?: number | null;
  };
  relative?: {
    beta?: number | null;
    bench_total_return_pct?: number | null;
    active_return_pct?: number | null;
  };
  charts?: {
    dates?: string[];
    equity_rebased?: (number | null)[];
    bench_rebased?: (number | null)[];
  };
  monthly?: { month: string; strat_pct: number | null; bench_pct: number | null }[];
}

export interface AlphaPosition {
  symbol: string;
  name?: string | null;
  sector?: string | null;
  market_value?: number | null;
  current_weight?: number | null;
  unrealized_plpc?: number | null;
  holding_days?: number | null;
  reserved?: boolean;
  score?: number | null;
}

export interface AlphaPortfolio {
  available?: boolean;
  as_of?: string;
  equity?: number | null;
  start_capital?: number | null;
  n_satellite?: number | null;
  reserve?: { symbol?: string | null } | null;
  positions?: AlphaPosition[];
  policy?: {
    take_profit_pct?: number | null;
    stop_loss_pct?: number | null;
    max_position_weight?: number | null;
    max_sector_weight?: number | null;
    sell_min_hold_days?: number | null;
  } | null;
}

export interface BrokerPosition {
  symbol: string;
  asset_class?: string;
  qty?: string | number;
  avg_entry_price?: string | number;
  current_price?: string | number;
  market_value?: string | number;
  cost_basis?: string | number;
  unrealized_pl?: string | number;
}

export interface TraderClose {
  submitted_at?: string;
  ticker?: string;
  strategy?: string;
  strike?: number;
  expiration?: string;
  qty?: number;
  entry_premium?: number | null;
  status?: string;
  filled_at?: string | null;
  filled_avg_price?: string | number | null;
}

export interface Trader {
  equity?: number | null;
  pl_vs_start?: number | null;
  cash?: number | null;
  /** cash left if every put it has sold were assigned (negative = it would borrow) */
  cash_after_assignment?: number | null;
  open_positions?: number | null;
  positions?: BrokerPosition[];
  recent_closes?: TraderClose[];
  liabilities?: {
    put_assignment?: number | null; short_put_contracts?: number | null; short_call_contracts?: number | null;
  } | null;
  regime?: { vix?: number | null; regime?: string | null } | null;
  reserve?: { floor_pct?: number | null } | null;
  guardrails?: {
    close_at_profit_pct?: number | null;
    close_at_loss_multiplier?: number | null;
    kill_switch_loss_mult?: number | null;
    max_scan_age_hours?: number | null;
  } | null;
}

export interface Landing {
  equities?: { screened?: number | null; dcf_upside?: number | null; dip_buys?: number | null; top?: string | null } | null;
}

const OPTS = { refreshInterval: 60_000, revalidateOnFocus: false, keepPreviousData: true } as const;

export const useAlphaAnalytics = () => useSWR<Analytics>("/api/alpha/analytics", fetcher, OPTS);
export const useAlphaPortfolio = () => useSWR<AlphaPortfolio>("/api/alpha/portfolio", fetcher, OPTS);
export const useTraderAnalytics = () => useSWR<Analytics>("/api/trader/analytics", fetcher, OPTS);
export const useTrader = () => useSWR<Trader>("/api/trader", fetcher, OPTS);
export const useLanding = () => useSWR<Landing>("/api/landing", fetcher, OPTS);

// The research screen is ~2.5 MB of JSON (about 260 KB on the wire): fetched
// once per visit, never polled — it changes once a day.
const ONCE = { revalidateOnFocus: false, revalidateIfStale: false, keepPreviousData: true } as const;
export const useResearch = () => useSWR<import("./research").Research>("/api/robinhood/research", fetcher, ONCE);
export const useDeepModels = () => useSWR<import("./research").DeepModels>("/api/robinhood/deep-dcf", fetcher, ONCE);
export const useFactorPerf = () => useSWR<import("./research").FactorPerfData>("/api/robinhood/factor-performance", fetcher, ONCE);

/* The daily options scan: the top put-selling candidates, each with its best
   strike, the per-strike measures, and the AI analyst's written read. */
export interface ScanStrike {
  Strategy?: string; Expiration?: string; Strike?: number; Premium?: number; "Monthly %"?: number; "Ann. %"?: number;
  Breakeven?: number; "Buffer %"?: number; "IV Rank"?: number; OI?: number; "Spread %"?: number; "Earnings Days"?: number | null;
  "Fund Score"?: number; "Tech Score"?: number; "Liq Score"?: number; POP?: string; "Final Score"?: number; Rating?: string; Sector?: string;
}
export interface ScanPick {
  symbol: string; price?: number | null; rating?: string | null; score?: number | null; would_own?: number | null;
  strike?: number | null; expiration?: string | null; premium?: number | null; monthly_pct?: number | null; pop?: string | null;
  analysis?: string | null; strikes?: ScanStrike[] | null;
}
export const useOptionsScan = () => useSWR<{ picks?: ScanPick[] }>("/api/stock-options/scan", fetcher, ONCE);

export const num = (v: unknown): number | null => {
  const n = typeof v === "string" ? Number(v) : v;
  return typeof n === "number" && Number.isFinite(n) ? n : null;
};

/** Indexed-to-100 series → percent change since the first real point. */
export function toChange(series: (number | null)[] | undefined): (number | null)[] {
  if (!series) return [];
  const base = series.find((v) => typeof v === "number" && Number.isFinite(v) && v !== 0);
  if (base == null) return series.map(() => null);
  return series.map((v) => (typeof v === "number" && Number.isFinite(v) ? (v / base - 1) * 100 : null));
}

/**
 * The benchmark can start a few days after the strategy (no price yet). Re-base
 * both lines at the first day BOTH exist so they leave zero together; an S&P
 * line that starts late and above zero would misstate the comparison.
 */
export function alignedChanges(a: Analytics | undefined): {
  dates: string[]; strat: (number | null)[]; bench: (number | null)[];
} {
  const dates = a?.charts?.dates ?? [];
  const eq = a?.charts?.equity_rebased ?? [];
  const bm = a?.charts?.bench_rebased ?? [];
  const ok = (v: unknown): v is number => typeof v === "number" && Number.isFinite(v) && v !== 0;
  const start = dates.findIndex((_, i) => ok(eq[i]) && ok(bm[i]));
  if (start < 0) return { dates, strat: toChange(eq), bench: [] };
  const e0 = eq[start] as number, b0 = bm[start] as number;
  return {
    dates: dates.slice(start),
    strat: eq.slice(start).map((v) => (ok(v) ? (v / e0 - 1) * 100 : null)),
    bench: bm.slice(start).map((v) => (ok(v) ? (v / b0 - 1) * 100 : null)),
  };
}
