"use client";

// The company detail: a full page opened when a company is chosen anywhere on
// the research page, or looked up by ticker. Rebuild of the
// research platform's click-through: price chart, performance, fundamentals,
// what drives the grade, the valuation, the cached AI read and recent news.
//
// Reads GET /api/robinhood/symbol/{sym} and /chart/{sym}. The public gateway
// answers both without opening the owner's account, so there is no position,
// order or watchlist in the payload to hide (backend/viewonly.py).

import {
  createContext, useCallback, useContext, useMemo, useRef, useState, type ReactNode,
} from "react";
import useSWR from "swr";
import { fetcher } from "@/lib/api";
import { FullPage } from "./FullPage";
import { niceDomain, tickDigits, useWidth } from "./PerfChart";
import { Delta } from "./Bits";
import { ModelView, type FullModel } from "./ModelView";
import { fixed, isNum, monthDay, monthShort, parseDay, plainPct, shortDay, signedPct, unbrand } from "./fmt";
import {
  FACTOR_LABEL, REASON_NOTE, confLevel, dcfConfidence, dcfTrusted, declineReason, gradeOf,
  impliedNote, ratingOf, sectorName, type DcfBlock, type Factor, type ResearchRow,
} from "./research";

/* ── context ─────────────────────────────────────────────────────────────── */
// One panel at a time: the company detail, or the valuation model.
interface Ctx { open: (symbol: string) => void; openModel: (symbol: string) => void; hasModel: (symbol: string) => boolean }
const StockCtx = createContext<Ctx>({ open: () => {}, openModel: () => {}, hasModel: () => false });
export const useStock = () => useContext(StockCtx);

const cleanSym = (s: string) => s.trim().toUpperCase().replace(/[^A-Z0-9.\-]/g, "").slice(0, 10);

export function StockDrawerProvider({
  children, lookup, back,
}: { children: ReactNode; lookup?: (symbol: string) => ResearchRow | undefined; back?: string }) {
  const [panel, setPanel] = useState<{ kind: "stock" | "model"; symbol: string } | null>(null);
  const opener = useRef<HTMLElement | null>(null);
  const { data: deep } = useSWR<{ models?: FullModel[] }>("/api/robinhood/deep-dcf", fetcher, ONCE);
  const models = useMemo(() => new Map((deep?.models ?? []).map((m) => [m.symbol, m])), [deep]);
  const show = useCallback((kind: "stock" | "model", s: string) => {
    const sym = cleanSym(s);
    if (!sym) return;
    if (!panel) opener.current = (document.activeElement as HTMLElement) ?? null;
    setPanel({ kind, symbol: sym });
  }, [panel]);
  const open = useCallback((s: string) => show("stock", s), [show]);
  const openModel = useCallback((s: string) => show("model", s), [show]);
  const hasModel = useCallback((s: string) => models.has(cleanSym(s)), [models]);
  const close = useCallback(() => {
    setPanel(null);
    requestAnimationFrame(() => opener.current?.focus?.());
  }, []);
  return (
    <StockCtx.Provider value={{ open, openModel, hasModel }}>
      {children}
      {panel?.kind === "stock" && (
        <Drawer symbol={panel.symbol} row={lookup?.(panel.symbol)} onClose={close} back={back}
          onOpenModel={models.has(panel.symbol) ? () => openModel(panel.symbol) : undefined} />
      )}
      {panel?.kind === "model" && (
        <ModelView symbol={panel.symbol} row={lookup?.(panel.symbol)} model={models.get(panel.symbol)}
          onClose={close} onOpenStock={open} back={back} />
      )}
    </StockCtx.Provider>
  );
}

/* ── payload types (only the fields shown) ───────────────────────────────── */
interface SymbolPayload {
  symbol: string;
  as_of?: string;
  quote?: { last?: number | null; day_pct?: number | null } | null;
  fundamentals?: {
    name?: string; sector?: string; industry?: string; market_cap?: number | null;
    pe?: number | null; forward_pe?: number | null; ev_ebitda?: number | null; fcf_yield_pct?: number | null;
    dividend_yield_pct?: number | null; gross_margin?: number | null; operating_margin?: number | null;
    revenue_growth?: number | null; debt_to_equity?: number | null;
  } | null;
  stats?: {
    ret_1w_pct?: number | null; ret_1m_pct?: number | null; ret_3m_pct?: number | null; ret_ytd_pct?: number | null;
    ret_1y_pct?: number | null; vol_ann_pct?: number | null; beta_1y?: number | null; sharpe_1y?: number | null;
    max_dd_1y_pct?: number | null; rsi_14?: number | null;
  } | null;
  score?: number | null;
  confidence?: number | null;
  confidence_label?: string | null;
  factors?: Record<string, Factor> | null;
  dcf?: DcfBlock | null;
  deep_dcf?: { scenarios?: { bear?: { fair_value?: number }; base?: { fair_value?: number }; bull?: { fair_value?: number } } } | null;
  news?: { title?: string; source?: string; published?: string; url?: string }[] | null;
  /** the weekly AI read of the business */
  ai_verdict?: AiReadPayload | null;
  /** the daily closes the price is taken from */
  history?: { dates?: string[] } | null;
}
interface ChartPayload { dates?: string[]; closes?: (number | null)[] }
interface AiReadPayload {
  ai_pct?: number | null; ai_conviction?: string | null; ai_thesis?: string | null; ai_risk?: string | null;
  ai_sub?: Record<string, number> | null; ai_read_at?: string | null;
}
const AI_DIMENSIONS: [string, string][] = [
  ["business_quality", "Business quality"], ["growth_durability", "Growth durability"],
  ["financial_resilience", "Financial strength"], ["risk_outlook", "Risk outlook"],
  ["valuation_vs_quality", "Price for the quality"],
];

const ONCE = { revalidateOnFocus: false, revalidateIfStale: false, shouldRetryOnError: false } as const;

const money = (v: number) => `$${v.toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
const bigMoney = (v: number) =>
  v >= 1e12 ? `$${(v / 1e12).toFixed(2)}T` : v >= 1e9 ? `$${(v / 1e9).toFixed(1)}B` : `$${(v / 1e6).toFixed(0)}M`;
const ratio = (v: number) => `${v.toFixed(1)}×`;

/** When the latest price move happened: "today" only if the last close carries
    today's New York date, otherwise the day itself ("on Nov 20"). The price is
    the latest cached close, so on a weekend it is Friday's. */
function sessionLabel(dates?: string[]): string {
  const last = dates?.[dates.length - 1]?.slice(0, 10);
  if (!last) return "";
  const today = new Intl.DateTimeFormat("en-CA", { timeZone: "America/New_York" }).format(new Date());
  return last === today ? "today" : "on " + monthDay(last);
}
const pctOf = (v: number) => `${(v * 100).toFixed(0)}%`;

/* ── drawer shell ────────────────────────────────────────────────────────── */
function Drawer({ symbol, row, onClose, onOpenModel, back }: { symbol: string; row?: ResearchRow; onClose: () => void; onOpenModel?: () => void; back?: string }) {
  const { data, error } = useSWR<SymbolPayload>(`/api/robinhood/symbol/${encodeURIComponent(symbol)}`, fetcher, ONCE);
  const f = data?.fundamentals;
  const name = row?.name ?? f?.name ?? symbol;
  return (
    <FullPage
      eyebrow="Company detail"
      title={name}
      meta={<>{symbol}{(f?.sector || row?.sector) && `, ${sectorName(f?.sector ?? row?.sector)}`}{f?.industry && `, ${f.industry}`}</>}
      link={onOpenModel && (
        <button type="button" className="fs-link bg-transparent border-0 p-0 cursor-pointer text-[0.875rem]" onClick={onOpenModel}>
          Valuation model
        </button>
      )}
      onClose={onClose}
      back={back}
    >
      {error ? (
        <p className="fs-prose mt-6">No research data is available for {symbol}. Check the ticker and try another.</p>
      ) : !data ? (
        <p className="fs-note mt-6">Loading {symbol}…</p>
      ) : !(isNum(data.quote?.last) || data.fundamentals?.name || isNum(data.score)) ? (
        <p className="fs-prose mt-6">There is no research data for {symbol}. Check the ticker, or try one of the roughly 600 companies in the screen.</p>
      ) : (
        <DrawerBody symbol={symbol} data={data} row={row} onOpenModel={onOpenModel} />
      )}
    </FullPage>
  );
}

function SubHead({ children }: { children: ReactNode }) {
  return <h3 className="fs-h3 mt-8 mb-3">{children}</h3>;
}

function Facts({ items }: { items: [string, ReactNode][] }) {
  const shown = items.filter(([, v]) => v != null && v !== "");
  if (!shown.length) return null;
  return (
    <dl className="m-0 grid grid-cols-2 sm:grid-cols-3 gap-x-5 gap-y-4">
      {shown.map(([k, v]) => (
        <div key={k}>
          <dt className="fs-note">{k}</dt>
          <dd className="m-0 mt-0.5 text-[1rem] font-semibold fs-num">{v}</dd>
        </div>
      ))}
    </dl>
  );
}

function DrawerBody({ symbol, data, row, onOpenModel }: { symbol: string; data: SymbolPayload; row?: ResearchRow; onOpenModel?: () => void }) {
  const q = data.quote ?? {};
  const s = data.stats ?? {};
  const f = data.fundamentals ?? {};
  const { data: chart } = useSWR<ChartPayload>(`/api/robinhood/chart/${encodeURIComponent(symbol)}`, fetcher, ONCE);

  const grade = row ? gradeOf(row) : null;
  const composite = row?.score ?? data.score;
  const conf = row?.confidence ?? data.confidence;
  const confWord = row?.confidence_label ?? data.confidence_label ?? confLevel(conf);
  const factors = row?.factors ?? data.factors ?? {};
  const dcf = row?.dcf ?? data.dcf ?? null;
  const deep = data.deep_dcf?.scenarios;

  return (
    <>
      {/* price + grade */}
      <div className="mt-5 flex flex-wrap items-end justify-between gap-x-6 gap-y-3">
        <p className="m-0">
          <span className="text-[2rem] font-semibold tracking-[-0.01em]">{isNum(q.last) ? money(q.last) : "–"}</span>
          {isNum(q.day_pct) && (
            <span className="ml-2 text-[0.9375rem]"><Delta value={q.day_pct} text={[signedPct(q.day_pct), sessionLabel(data.history?.dates)].filter(Boolean).join(" ")} /></span>
          )}
        </p>
        {isNum(grade) ? (
          <p className="m-0 text-right">
            <span className="block fs-note">Grade</span>
            <span className="text-[1.5rem] font-semibold">{grade}</span>
            <span className="fs-note"> of 100, {ratingOf(grade).toLowerCase()}</span>
          </p>
        ) : isNum(composite) ? (
          <p className="m-0 text-right">
            <span className="block fs-note">Composite score</span>
            <span className="text-[1.5rem] font-semibold">{Math.round(composite)}</span><span className="fs-note"> of 100</span>
          </p>
        ) : null}
      </div>

      <div className="grid gap-x-14 lg:grid-cols-[minmax(0,1.55fr)_minmax(0,1fr)]">
      <div className="min-w-0">
      {chart?.dates?.length && chart.closes?.length ? (
        <div className="mt-5"><PriceChart dates={chart.dates} closes={chart.closes} /></div>
      ) : null}

      <SubHead>Performance</SubHead>
      <Facts items={[
        ["One week", isNum(s.ret_1w_pct) ? <Delta value={s.ret_1w_pct} text={signedPct(s.ret_1w_pct)} /> : null],
        ["One month", isNum(s.ret_1m_pct) ? <Delta value={s.ret_1m_pct} text={signedPct(s.ret_1m_pct)} /> : null],
        ["Three months", isNum(s.ret_3m_pct) ? <Delta value={s.ret_3m_pct} text={signedPct(s.ret_3m_pct)} /> : null],
        ["Year to date", isNum(s.ret_ytd_pct) ? <Delta value={s.ret_ytd_pct} text={signedPct(s.ret_ytd_pct)} /> : null],
        ["One year", isNum(s.ret_1y_pct) ? <Delta value={s.ret_1y_pct} text={signedPct(s.ret_1y_pct)} /> : null],
        ["Largest drop, one year", isNum(s.max_dd_1y_pct) ? signedPct(s.max_dd_1y_pct) : null],
        ["Volatility, yearly", isNum(s.vol_ann_pct) ? `${s.vol_ann_pct.toFixed(0)}%` : null],
        ["Beta to the market", isNum(s.beta_1y) ? fixed(s.beta_1y) : null],
        ["Sharpe ratio, one year", isNum(s.sharpe_1y) ? fixed(s.sharpe_1y) : null],
      ]} />

      <SubHead>Fundamentals</SubHead>
      <Facts items={[
        ["Market value", isNum(f.market_cap) ? bigMoney(f.market_cap) : null],
        ["Price to earnings", isNum(f.pe) ? ratio(f.pe) : null],
        ["Forward P/E", isNum(f.forward_pe) ? ratio(f.forward_pe) : null],
        ["EV to EBITDA", isNum(f.ev_ebitda) ? ratio(f.ev_ebitda) : null],
        ["Free-cash-flow yield", isNum(f.fcf_yield_pct) ? `${f.fcf_yield_pct.toFixed(1)}%` : null],
        ["Dividend yield", isNum(f.dividend_yield_pct) ? `${f.dividend_yield_pct.toFixed(1)}%` : null],
        ["Gross margin", isNum(f.gross_margin) ? pctOf(f.gross_margin) : null],
        ["Operating margin", isNum(f.operating_margin) ? pctOf(f.operating_margin) : null],
        ["Revenue growth", isNum(f.revenue_growth) ? signedPct(f.revenue_growth * 100, 0) : null],
      ]} />

      <News items={data.news} />
      </div>

      <div className="min-w-0 lg:pt-1">
      <SubHead>What drives the grade</SubHead>
      {isNum(composite) && (
        <p className="fs-note mt-0 mb-3">
          Composite {Math.round(composite)} of 100{isNum(conf) ? `, adjusted for ${confWord} confidence (${Math.round(conf * 100)}%)` : ""}.
        </p>
      )}
      <ul className="list-none p-0 m-0 grid gap-y-2.5">
        {(["value", "quality", "growth", "momentum", "analyst"] as const).map((k) => {
          const sc = factors?.[k]?.score;
          return (
            <li key={k} className="grid grid-cols-[7.5rem_1fr_2rem] items-center gap-x-3">
              <span className="text-[0.9375rem]">{FACTOR_LABEL[k]}</span>
              <span className="h-[6px] rounded-full overflow-hidden" style={{ background: "var(--fs-line)" }} aria-hidden>
                <span className="block h-full rounded-full" style={{ width: `${isNum(sc) ? Math.max(0, Math.min(100, sc)) : 0}%`, background: "var(--fs-steel)" }} />
              </span>
              <span className="fs-num text-right text-[0.9375rem]">{isNum(sc) ? Math.round(sc) : "–"}</span>
            </li>
          );
        })}
      </ul>

      <SubHead>Valuation</SubHead>
      <Valuation dcf={dcf} row={row ?? ({ symbol, dcf } as ResearchRow)} />
      {deep && isNum(deep.base?.fair_value) && (
        <div className="mt-4 p-4 rounded-lg" style={{ background: "var(--fs-wash)" }}>
          <div className="flex flex-wrap items-baseline justify-between gap-2 mb-2">
            <p className="fs-note m-0">Full valuation model, built from SEC filings and independently recalculated</p>
            {onOpenModel && (
              <button type="button" className="fs-link bg-transparent border-0 p-0 cursor-pointer text-[0.875rem]" onClick={onOpenModel}>
                Open the model
              </button>
            )}
          </div>
          <dl className="m-0 grid grid-cols-3 gap-x-3">
            {([["Bear", deep.bear?.fair_value], ["Base", deep.base?.fair_value], ["Bull", deep.bull?.fair_value]] as const).map(([k, v]) => (
              <div key={k}>
                <dt className="fs-note">{k} case</dt>
                <dd className="m-0 mt-0.5 font-semibold">{isNum(v) ? money(v) : "–"}</dd>
              </div>
            ))}
          </dl>
        </div>
      )}

      <AiRead read={data.ai_verdict} />

      </div>
      </div>
    </>
  );
}

/* Recent headlines, https links only. */
function News({ items }: { items?: SymbolPayload["news"] }) {
  const shown = (items ?? []).filter((n) => n.title && n.url && /^https:\/\//.test(n.url)).slice(0, 5);
  if (!shown.length) return null;
  return (
    <>
      <SubHead>Recent news</SubHead>
      <ul className="list-none p-0 m-0 grid gap-y-3">
        {shown.map((n) => (
          <li key={n.url}>
            <a href={n.url} target="_blank" rel="noopener noreferrer" className="fs-link font-normal text-[0.9375rem]">
              {n.title!.replace(/\s+-\s+[^-]+$/, "")}
            </a>
            <span className="block fs-note mt-0.5">
              {n.source}{n.published ? `, ${shortDay(new Date(n.published).toISOString().slice(0, 10))}` : ""}
            </span>
          </li>
        ))}
      </ul>
    </>
  );
}

export function Valuation({ dcf, row }: { dcf: DcfBlock | null; row: ResearchRow }) {
  if (dcfTrusted(dcf) && isNum(dcf?.fair_value)) {
    const conf = dcfConfidence(dcf!.confidence);
    return (
      <>
        <p className="m-0 text-[0.9375rem]">
          The cash-flow model puts it at about <span className="font-semibold">{money(dcf!.fair_value!)}</span> a share
          {isNum(dcf!.upside_pct) && <>, <Delta value={dcf!.upside_pct!} text={signedPct(dcf!.upside_pct!, 0)} /> against today&apos;s price</>}.
          {conf && <span className="fs-note"> {conf} confidence.</span>}
        </p>
        <Facts items={[
          ["Discount rate", isNum(dcf!.wacc_pct) ? plainPct(dcf!.wacc_pct) : null],
          ["Growth, first stage", isNum(dcf!.stage1_growth_pct) ? `${plainPct(dcf!.stage1_growth_pct)} a year` : null],
          ["Price implies", isNum(dcf!.implied_growth_pct) ? `${plainPct(dcf!.implied_growth_pct)} a year` : null],
        ]} />
      </>
    );
  }
  const why = declineReason(row);
  const implied = impliedNote(dcf);
  return (
    <p className="m-0 text-[0.9375rem]">
      <span className="font-medium">Not valued: {(why ?? "not enough reliable data").toLowerCase()}.</span>
      {why && REASON_NOTE[why] && <span className="block fs-note mt-1">{REASON_NOTE[why]}</span>}
      {implied && <span className="block fs-note mt-1">{implied}</span>}
    </p>
  );
}

/* The weekly AI read: what the model thinks of the business, the main risk it
   sees, and its five scores out of ten. */
function AiRead({ read }: { read?: AiReadPayload | null }) {
  const thesis = unbrand((read?.ai_thesis ?? "").trim());
  if (!read || !thesis) return null;
  const pct = read.ai_pct;
  const rank = !isNum(pct) ? null
    : pct >= 50 ? `Top ${Math.max(1, Math.round(100 - pct))}% of companies read`
    : `Bottom ${Math.max(1, Math.round(pct))}% of companies read`;
  const scores = AI_DIMENSIONS.filter(([k]) => isNum(read.ai_sub?.[k]));
  return (
    <>
      <SubHead>AI read</SubHead>
      {(rank || read.ai_read_at) && (
        <p className="fs-note mt-0 mb-3">
          {[rank, read.ai_read_at ? shortDay(read.ai_read_at.slice(0, 10)) : null].filter(Boolean).join(", ")}
        </p>
      )}
      <p className="m-0 text-[0.9375rem]">{thesis}</p>
      {read.ai_risk && (
        <p className="mt-2 mb-0 text-[0.9375rem]">
          <span className="font-medium">Main risk: </span>{unbrand(read.ai_risk)}
        </p>
      )}
      {scores.length > 0 && (
        <ul className="list-none p-0 mt-4 mb-0 grid gap-y-2.5">
          {scores.map(([k, label]) => {
            const v = read.ai_sub![k];
            return (
              <li key={k} className="grid grid-cols-[9.5rem_1fr_2rem] items-center gap-x-3">
                <span className="text-[0.9375rem]">{label}</span>
                <span className="h-[6px] rounded-full overflow-hidden" style={{ background: "var(--fs-line)" }} aria-hidden>
                  <span className="block h-full rounded-full" style={{ width: `${Math.max(0, Math.min(10, v)) * 10}%`, background: "var(--fs-steel)" }} />
                </span>
                <span className="fs-num text-right text-[0.9375rem]">{v}</span>
              </li>
            );
          })}
        </ul>
      )}
    </>
  );
}

/* ── one-year price chart ────────────────────────────────────────────────── */
function PriceChart({ dates, closes }: { dates: string[]; closes: (number | null)[] }) {
  const { ref, width } = useWidth<HTMLDivElement>();
  const [active, setActive] = useState<number | null>(null);
  const height = 200;
  const vals = closes.filter(isNum) as number[];
  const n = closes.length;
  const lo0 = Math.min(...vals), hi0 = Math.max(...vals);
  const pad = (hi0 - lo0) * 0.08 || hi0 * 0.05 || 1;
  const { lo, hi, step } = useMemo(() => {
    const d = niceDomain([lo0 - pad, hi0 + pad].map((v) => v - (lo0 - pad)), 4);
    const base = Math.floor((lo0 - pad) / d.step) * d.step;
    return { lo: base, hi: base + Math.ceil((hi0 + pad - base) / d.step) * d.step, step: d.step };
  }, [lo0, hi0, pad]);
  const M = { top: 8, right: 8, bottom: 24, left: 52 };
  const plotW = Math.max(width - M.left - M.right, 10);
  const plotH = height - M.top - M.bottom;
  const x = (i: number) => M.left + (n > 1 ? (i / (n - 1)) * plotW : 0);
  const y = (v: number) => M.top + (1 - (v - lo) / (hi - lo || 1)) * plotH;
  let d = "";
  let pen = false;
  closes.forEach((v, i) => {
    if (!isNum(v)) { pen = false; return; }
    d += `${pen ? "L" : "M"}${x(i).toFixed(1)},${y(v).toFixed(1)}`;
    pen = true;
  });
  const ticks: number[] = [];
  for (let t = lo; t <= hi + 1e-9; t += step) ticks.push(t);
  const months: { i: number; label: string }[] = [];
  let lastX = -Infinity;
  for (let i = 1; i < n; i++) {
    const a = parseDay(dates[i - 1]), b = parseDay(dates[i]);
    if (!a || !b || a.m === b.m) continue;
    if (x(i) - lastX < 44) continue;
    months.push({ i, label: monthShort(b.m) });
    lastX = x(i);
  }
  const digits = tickDigits(step);
  const first = vals[0], latest = vals[vals.length - 1];
  return (
    <figure className="fs-chart m-0">
      <figcaption className="fs-note mb-2">
        One year of daily closes, {isNum(first) && isNum(latest) ? `${signedPct((latest / first - 1) * 100)} over the period` : ""}
      </figcaption>
      <div
        ref={ref}
        className="relative"
        style={{ height }}
        onPointerLeave={() => setActive(null)}
        onPointerMove={(e) => {
          const r = e.currentTarget.getBoundingClientRect();
          setActive(Math.max(0, Math.min(n - 1, Math.round(((e.clientX - r.left - M.left) / plotW) * (n - 1)))));
        }}
      >
        {width > 0 && (
          <svg width={width} height={height} aria-hidden>
            {ticks.map((t) => (
              <g key={t}>
                <line x1={M.left} x2={M.left + plotW} y1={y(t)} y2={y(t)} stroke="var(--fs-line)" strokeWidth={1} />
                <text className="fs-axis" x={M.left - 8} y={y(t)} textAnchor="end" dominantBaseline="central">
                  ${t.toLocaleString("en-US", { minimumFractionDigits: digits, maximumFractionDigits: digits })}
                </text>
              </g>
            ))}
            {months.map((m) => <text key={m.i} className="fs-axis" x={x(m.i)} y={height - 6} textAnchor="middle">{m.label}</text>)}
            <path d={`${d}L${x(n - 1)},${M.top + plotH}L${x(0)},${M.top + plotH}Z`} fill="var(--fs-blue)" opacity={0.08} />
            <path d={d} fill="none" stroke="var(--fs-blue)" strokeWidth={2} strokeLinejoin="round" strokeLinecap="round" />
            {active != null && isNum(closes[active]) && (
              <g>
                <line x1={x(active)} x2={x(active)} y1={M.top} y2={M.top + plotH} stroke="var(--fs-line-strong)" strokeWidth={1} />
                <circle cx={x(active)} cy={y(closes[active] as number)} r={4.5} fill="var(--fs-blue)" stroke="var(--fs-canvas)" strokeWidth={2} />
              </g>
            )}
          </svg>
        )}
        {active != null && isNum(closes[active]) && (
          <div className="fs-tip" style={{ top: 0, left: Math.max(0, Math.min(x(active) + 12, width - 150)), minWidth: 120 }}>
            <div className="fs-note">{shortDay(dates[active])}</div>
            <div className="font-semibold fs-num">{money(closes[active] as number)}</div>
          </div>
        )}
      </div>
    </figure>
  );
}
