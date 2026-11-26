// The research screen, typed loosely (the backend returns plain JSON) and
// translated for readers who do not know DCF gate codes. Every helper is pure.
//
// The public API carries no account data (`held`, `pl_pct`, `position`,
// `orders`, `watchlists`, `ai_verdict.position_*`), and nothing here may read
// those fields either. The demo shows model output only.

export interface FactorComponent { name: string; points?: number | null; max_points?: number | null; detail?: string | null }
export interface Factor { score?: number | null; coverage?: number | null; components?: FactorComponent[] | null }

export interface Sensitivity { wacc_pcts: number[]; growth_pcts: number[]; fair_values: (number | null)[][] }
export interface Gate { id?: string; level?: string; reason?: string }
export interface DcfBlock {
  available?: boolean;
  suppressed?: boolean;
  blocked?: boolean;
  reason?: string | null;
  archetype?: string | null;
  why_archetype?: string | null;
  confidence?: string | null;
  fair_value?: number | null;
  current_price?: number | null;
  upside_pct?: number | null;
  wacc_pct?: number | null;
  stage1_growth_pct?: number | null;
  terminal_growth_pct?: number | null;
  implied_growth_pct?: number | null;
  implied_wacc_pct?: number | null;
  terminal_value_fraction_pct?: number | null;
  sensitivity?: Sensitivity | null;
  base_fcf?: number | null;
  owner_fcf_source?: string | null;
  caveats?: string[] | null;
  assumptions?: string | null;
  alt_fair_value?: number | null;
  alt_upside_pct?: number | null;
  alt_detail?: string | null;
  gates?: Gate[] | null;
}

export interface ResearchRow {
  symbol: string;
  name?: string | null;
  sector?: string | null;
  last?: number | null;
  score?: number | null;
  tier?: string | null;
  confidence?: number | null;
  confidence_label?: string | null;
  confidence_notes?: string[] | null;
  rank?: number | null;
  rank_score?: number | null;
  stage?: string | null;
  factors?: Record<string, Factor> | null;
  signals?: { ret_1y_pct?: number | null } | null;
  value?: { consensus?: string | null; num_analysts?: number | null; is_dip_buy?: boolean } | null;
  dcf?: DcfBlock | null;
}

export interface Research {
  generated_at?: string;
  count?: number;
  universe_size?: number;
  stocks?: ResearchRow[];
  shortlists?: Record<string, string[]>;
  factor_weights?: Record<string, number>;
}

export interface Scenario { fair_value?: number | null }
export interface DeepModel {
  symbol: string;
  as_of?: string;
  price?: number | null;
  scenarios?: { bear?: Scenario; base?: Scenario; bull?: Scenario } | null;
  thesis?: string | null;
}
export interface DeepModels { available?: boolean; count?: number; models?: DeepModel[] }

export interface FactorPerf {
  name: string; n_periods?: number; ann_spread_pct?: number; hit_rate?: number;
  q5_avg_pct?: number; q1_avg_pct?: number;
}
export interface FactorPerfData { window?: string; factors?: FactorPerf[]; caveat?: string }

/* ── The grade ───────────────────────────────────────────────────────────────
   One 0-100 number per stock: the multi-factor composite adjusted for how much
   the data supports it (the server's rank_score). Rows sort by it, and the
   rating is derived from it, exactly as on the research platform. */
export const gradeOf = (r: ResearchRow): number => Math.round(r.rank_score ?? 0);

export function ratingOf(grade: number): string {
  if (grade >= 66) return "Strong buy";
  if (grade >= 54) return "Buy";
  if (grade >= 42) return "Hold";
  if (grade >= 30) return "Sell";
  return "Strong sell";
}

export type ConfLevel = "high" | "medium" | "low";
export function confLevel(c: number | null | undefined): ConfLevel {
  const v = c ?? 1;
  return v >= 0.75 ? "high" : v >= 0.5 ? "medium" : "low";
}

/* ── Lenses: each one FILTERS the universe; ranking stays by grade ─────────── */
export type LensKey =
  | "all" | "value" | "growth" | "quality" | "momentum" | "analyst" | "dcf" | "dip" | "emerging" | "ai";
export const LENSES: { key: LensKey; label: string; shortlist?: string; factor?: string }[] = [
  { key: "all", label: "All picks" },
  { key: "value", label: "Cheapest", shortlist: "cheapest", factor: "value" },
  { key: "growth", label: "Growth", shortlist: "fastest_growth", factor: "growth" },
  { key: "quality", label: "Quality", shortlist: "top_quality", factor: "quality" },
  { key: "momentum", label: "Momentum", shortlist: "momentum", factor: "momentum" },
  { key: "analyst", label: "Analyst favourites", shortlist: "analyst", factor: "analyst" },
  { key: "dcf", label: "Cash-flow upside", shortlist: "dcf_upside" },
  { key: "dip", label: "Undervalued", shortlist: "dip_buys" },
  { key: "emerging", label: "Emerging", shortlist: "emerging" },
  { key: "ai", label: "AI picks", shortlist: "ai_buys" },
];

export const LENS_NOTE: Partial<Record<LensKey, string>> = {
  dip: "Down at least 12% from the one-year high, not in a steep decline, and analysts are not bearish.",
  dcf: "Priced more than 10% below what the cash-flow model estimates, where that estimate is trusted.",
  emerging: "Smaller and earlier-stage companies the screen tracks separately.",
  ai: "The top fifth of this week's AI reads of each business. The read carries 5% of the grade.",
};

/* ── Factors ─────────────────────────────────────────────────────────────── */
export const FACTOR_ORDER = ["value", "quality", "growth", "momentum", "analyst", "news", "ai"] as const;
export const FACTOR_LABEL: Record<string, string> = {
  value: "Value", quality: "Quality", growth: "Growth", momentum: "Momentum",
  analyst: "Analyst view", news: "News tone", ai: "AI read",
};
export const FACTOR_WHAT: Record<string, string> = {
  value: "Price to earnings and EV to EBITDA against the company's own sector, free-cash-flow yield, growth-adjusted price, and a small check against the cash-flow model.",
  quality: "Gross and operating margins, return on equity, free-cash-flow margin and balance-sheet strength.",
  growth: "Revenue and earnings growth and whether it is accelerating. Extreme rates are discounted as likely rebounds from a low base.",
  momentum: "Three-month and one-month returns, the trend against the 50- and 200-day averages, risk-adjusted return and recovery from drawdowns. Parabolic runs are discounted.",
  analyst: "Consensus rating, distance to the average price target, and how many analysts cover the stock.",
  news: "The tone of recent headlines about the company.",
  ai: "A weekly AI read of the business: quality, how durable its growth is, financial resilience, risks, and price against quality. Ranked against every other company, so it averages out to neutral.",
};

/* ── Valuation status ────────────────────────────────────────────────────── */
export function dcfTrusted(d?: DcfBlock | null): boolean {
  return !!(d?.available && d.confidence && d.confidence !== "LOW/ARTIFACT" && d.confidence !== "N/A");
}
/** Declined = the model withheld a number (blocked, suppressed, or an artifact). */
export function dcfDeclined(d?: DcfBlock | null): boolean {
  return (!d?.available && !!(d?.suppressed || d?.blocked)) || d?.confidence === "LOW/ARTIFACT";
}

const ARCH_REASON: Record<string, string> = {
  FINANCIAL: "Banks, insurers and payment firms",
  REIT: "Property trusts",
  RESOURCE: "Oil, gas and mining producers",
  PRE_PROFIT: "Not yet profitable",
};
const GATE_REASON: Record<string, string> = {
  G5: "Price only fits an unusual discount rate",
  G6: "Answer too far from the price to trust",
  G7: "Growing faster than the model can follow",
  G8: "Recent decline the model would paper over",
  G9: "Spending more cash than it brings in",
  G2: "Assumptions do not hold together",
  G4: "Value rests almost entirely on the distant future",
  G11: "Financial data in inconsistent units",
};

/** Why the cash-flow model declined this company, in plain words; null if valued. */
export function declineReason(r: ResearchRow): string | null {
  const d = r.dcf ?? {};
  if (dcfTrusted(d)) return null;
  const reason = (d.reason ?? "").toLowerCase();
  if (d.archetype && ARCH_REASON[d.archetype] && !(d.archetype === "RESOURCE" && reason.includes("float"))) {
    return ARCH_REASON[d.archetype];
  }
  if (reason.includes("float")) return ARCH_REASON.FINANCIAL;
  if (reason.includes("free cash flow") && reason.includes("≤ 0")) return GATE_REASON.G9;
  const red = (d.gates ?? []).find((g) => g.level === "RED" && g.id && GATE_REASON[g.id]);
  if (red?.id) return GATE_REASON[red.id];
  return "Not enough reliable data";
}

export const REASON_NOTE: Record<string, string> = {
  [ARCH_REASON.FINANCIAL]: "Their cash flows belong partly to depositors and customers, so they are valued on assets and earnings instead.",
  [ARCH_REASON.REIT]: "Property trusts are valued on the rent their buildings produce, not on free cash flow.",
  [ARCH_REASON.RESOURCE]: "Their cash flow follows commodity prices, so they are valued on reserves instead.",
  [ARCH_REASON.PRE_PROFIT]: "There is no profit yet to project forward.",
  [GATE_REASON.G9]: "It is investing more than it earns right now, so there is no cash flow to discount.",
  [GATE_REASON.G5]: "The market price only makes sense at a discount rate far from the model's, so the answer would reflect that assumption rather than the business.",
  [GATE_REASON.G6]: "The answer is more than three times away from the price, which points to a modelling gap rather than a real bargain.",
  [GATE_REASON.G7]: "Its growth is beyond what a ten-year model can project reliably.",
  [GATE_REASON.G8]: "The model would assume growth where the business has recently been shrinking.",
};

/** What the market price implies, when the model declined on a numeric gate. */
export function impliedNote(d?: DcfBlock | null): string | null {
  if (!d) return null;
  const pct = (v: number) => `${v < 0 ? "−" : ""}${Math.abs(v)}%`;
  if (typeof d.implied_growth_pct === "number" && typeof d.stage1_growth_pct === "number") {
    return `Today's price implies about ${pct(d.implied_growth_pct)} growth a year, against the model's ${pct(d.stage1_growth_pct)} ceiling.`;
  }
  if (typeof d.implied_wacc_pct === "number") {
    return `Today's price implies a cost of capital of about ${d.implied_wacc_pct}%.`;
  }
  return null;
}

const CONF_WORD: Record<string, string> = { HIGH: "High", "MEDIUM-HIGH": "Medium-high", MEDIUM: "Medium" };
export const dcfConfidence = (c: string | null | undefined) => (c ? CONF_WORD[c] ?? null : null);

/* ── Sectors ─────────────────────────────────────────────────────────────── */
const SECTOR_ALIAS: Record<string, string> = {
  Financials: "Financial Services",
  "Health Care": "Healthcare",
  "Information Technology": "Technology",
  "Consumer Discretionary": "Consumer Cyclical",
  "Consumer Staples": "Consumer Defensive",
  Materials: "Basic Materials",
};
export const sectorName = (s: string | null | undefined) => (s ? SECTOR_ALIAS[s] ?? s : "Other");

/* ── Factor-backtest signal names ────────────────────────────────────────── */
export const SIGNAL_LABEL: Record<string, string> = {
  trend_50d: "Above its 50-day average",
  momentum_3m: "Three-month momentum",
  momentum_1m: "One-month momentum",
  low_vol: "Low volatility",
};

/** Confidence notes, lightly cleaned of internal shorthand. */
export function tidyNote(n: string): string {
  return n
    .replace(/^DCF blocked\/unavailable — /, "Cash-flow model declined: ")
    .replace(/^DCF is a screen artifact — /, "Cash-flow result not trusted: ")
    .replace(/\bDCF\b/g, "cash-flow model")
    .replace(/\bWACC\b/g, "discount rate");
}
