"use client";

// Research — rebuild of the research platform's Equity › Research page for the
// demo: look up any ticker, switch between the factor Screen and the DCF
// Valuation, filter by lens and sector, open "why" on any row, and open the
// full stock detail. Same data and the same rules as the original; only the
// presentation is new. Personal fields in the payload are never shown.

import { Fragment, useMemo, useState } from "react";
import { DashHeader, KpiRow, Panel, ToggleLink, type Kpi } from "@/components/demo/Dash";
import { Delta, RankBars } from "@/components/demo/Bits";
import { StockDrawerProvider, useStock, Valuation } from "@/components/demo/StockDrawer";
import { useDeepModels, useFactorPerf, useResearch } from "@/components/demo/data";
import { isNum, plainPct, signedPct } from "@/components/demo/fmt";
import {
  FACTOR_LABEL, FACTOR_ORDER, FACTOR_WHAT, LENSES, LENS_NOTE, REASON_NOTE, SIGNAL_LABEL,
  confLevel, dcfConfidence, dcfDeclined, dcfTrusted, declineReason, gradeOf, impliedNote, ratingOf,
  sectorName, tidyNote, type DeepModel, type LensKey, type Research, type ResearchRow,
} from "@/components/demo/research";

const PAGE = 50;
const money = (v: number) => `$${v.toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;

export default function ResearchPage() {
  const { data: rs } = useResearch();
  const rows = useMemo(
    () => [...(rs?.stocks ?? [])].filter((r) => r.symbol)
      .sort((a, b) => (b.rank_score ?? 0) - (a.rank_score ?? 0) || (b.score ?? 0) - (a.score ?? 0)),
    [rs],
  );
  const bySymbol = useMemo(() => new Map(rows.map((r) => [r.symbol, r])), [rows]);
  return (
    <StockDrawerProvider lookup={(s) => bySymbol.get(s)}>
      <ResearchBody rs={rs} rows={rows} bySymbol={bySymbol} />
    </StockDrawerProvider>
  );
}

function ResearchBody({ rs, rows, bySymbol }: { rs?: Research; rows: ResearchRow[]; bySymbol: Map<string, ResearchRow> }) {
  const { open } = useStock();
  const [view, setView] = useState<"screen" | "valuation">("screen");
  const [ticker, setTicker] = useState("");
  const top = rows[0];
  const strongBuys = rows.filter((r) => gradeOf(r) >= 66).length;
  const finding = top
    ? `${top.name ?? top.symbol} has the highest grade today, ${gradeOf(top)} out of 100, one of ${strongBuys} rated a strong buy.`
    : null;

  return (
    <>
      <DashHeader
        title="Equity research"
        finding={finding}
        lead="About 600 US companies are scored every weekday on seven weighted factors and condensed into one grade. Both strategies draw their stocks from this screen."
        asOf={rs?.generated_at}
        source="Market data and SEC filings"
      />

      {/* look up any ticker + the Screen / Valuation switch */}
      <div className="mt-6 flex flex-wrap items-center justify-between gap-3">
        <form
          className="flex w-full sm:w-auto items-center gap-2"
          onSubmit={(e) => { e.preventDefault(); if (ticker.trim()) open(ticker); }}
          role="search"
        >
          <label htmlFor="rs-ticker" className="sr-only">Look up any ticker</label>
          <input
            id="rs-ticker" className="fs-input flex-1 sm:w-[260px] uppercase placeholder:normal-case" placeholder="Look up any ticker, e.g. NVDA"
            value={ticker} onChange={(e) => setTicker(e.target.value)} autoComplete="off" spellCheck={false}
          />
          <button type="submit" className="fs-button" style={{ minHeight: 40 }}>Look up</button>
        </form>
        <div className="fs-seg" role="group" aria-label="Research view">
          <button type="button" aria-pressed={view === "screen"} onClick={() => setView("screen")}>Screen</button>
          <button type="button" aria-pressed={view === "valuation"} onClick={() => setView("valuation")}>Valuation</button>
        </div>
      </div>

      <Methodology view={view} weights={rs?.factor_weights} />

      {!rows.length ? (
        <Panel className="mt-4" title="Loading today's research"><p className="fs-note m-0">The screen is about 600 companies; it takes a moment.</p></Panel>
      ) : view === "screen" ? (
        <ScreenView rs={rs!} rows={rows} />
      ) : (
        <ValuationView rows={rows} bySymbol={bySymbol} />
      )}
    </>
  );
}

/* ── How it works (collapsible, like the original's methodology box) ────────── */
function Methodology({ view, weights }: { view: "screen" | "valuation"; weights?: Record<string, number> }) {
  const [isOpen, setOpen] = useState(false);
  return (
    <details className="fs-panel mt-4" style={{ padding: 0 }} onToggle={(e) => setOpen((e.currentTarget as HTMLDetailsElement).open)}>
      <summary className="list-none cursor-pointer px-5 sm:px-6 py-4 flex items-center justify-between gap-4">
        <span>
          <span className="fs-h3 block">{view === "screen" ? "How the grade is built" : "How the valuation works"}</span>
          <span className="fs-note block mt-0.5">
            {view === "screen"
              ? "Seven weighted factors and a measure of how much data stands behind them produce one grade."
              : "A ten-year cash-flow model with honesty checks that refuses to value what it cannot."}
          </span>
        </span>
        <span className="fs-note shrink-0 underline underline-offset-4">{isOpen ? "Hide" : "Show"}</span>
      </summary>
      <div className="px-5 sm:px-6 pb-6 grid gap-6 lg:grid-cols-2 text-[0.9375rem]" style={{ color: "var(--fs-ink-2)" }}>
        {view === "screen" ? (
          <>
            <ul className="list-none p-0 m-0 grid gap-3">
              {FACTOR_ORDER.map((k) => (
                <li key={k}>
                  <span className="font-semibold" style={{ color: "var(--fs-ink)" }}>
                    {FACTOR_LABEL[k]}{isNum(weights?.[k]) ? `, ${weights![k]}%` : ""}
                  </span>
                  <span className="block">{FACTOR_WHAT[k]}</span>
                </li>
              ))}
            </ul>
            <div className="grid gap-3 content-start">
              <p className="m-0">Each factor&apos;s weight is scaled by how much of its data is actually present, so missing data loses influence instead of being guessed. Market-driven factors are capped near 38% of the total, so enthusiasm cannot outweigh fundamentals.</p>
              <p className="m-0">The grade is the composite adjusted for confidence. A high score the data cannot back ranks lower. Ratings follow the grade: strong buy from 66, buy from 54, hold from 42, sell from 30.</p>
              <p className="m-0">Companies that are not yet profitable are scored on a variant suited to their stage, using gross margin, cash runway and revenue growth instead of earnings.</p>
            </div>
          </>
        ) : (
          <>
            <div className="grid gap-3 content-start">
              <p className="m-0">For each company the model projects owner free cash flow for ten years, fading growth toward a 2.5% terminal rate, and discounts it at a cost of capital built from the market, the company&apos;s own debt and its volatility.</p>
              <p className="m-0">It then runs it backwards: what growth does today&apos;s price already assume? When the market and the model disagree wildly, that disagreement is reported instead of a target.</p>
            </div>
            <div className="grid gap-3 content-start">
              <p className="m-0">Fifteen checks decide whether the answer can be trusted. Banks, property trusts, commodity producers and unprofitable companies are declined outright, because a cash-flow model is the wrong tool for them.</p>
              <p className="m-0">For every company a cash-flow model suits, an AI model also reads the latest SEC filings and sets bear, base and bull assumptions. The system then builds the full spreadsheet from them, and a separate program recalculates every figure before it is published. Each model is rebuilt when the company files new results.</p>
            </div>
          </>
        )}
      </div>
    </details>
  );
}

/* ── Screen view ───────────────────────────────────────────────────────────── */
function ScreenView({ rs, rows }: { rs: Research; rows: ResearchRow[] }) {
  const { open, openModel, hasModel } = useStock();
  const [lens, setLens] = useState<LensKey>("all");
  const [sector, setSector] = useState("all");
  const [limit, setLimit] = useState(PAGE);
  const [why, setWhy] = useState<string | null>(null);

  const dipCount = rs.shortlists?.dip_buys?.length ?? rows.filter((r) => r.value?.is_dip_buy).length;
  const dcfUp = rows.filter((r) => dcfTrusted(r.dcf) && (r.dcf?.upside_pct ?? -1) > 10).length;
  const top = rows[0];
  const sectors = useMemo(() => [...new Set(rows.map((r) => sectorName(r.sector)))].sort(), [rows]);

  // Lens = filter (prefer the server's curated shortlist, else the original's
  // client fallback). Ranking always stays by grade.
  const shown = useMemo(() => {
    const base = rows.filter((r) => sector === "all" || sectorName(r.sector) === sector);
    if (lens === "all") return base;
    const L = LENSES.find((l) => l.key === lens)!;
    const sl = L.shortlist ? rs.shortlists?.[L.shortlist] : undefined;
    if (sl?.length) { const set = new Set(sl); return base.filter((r) => set.has(r.symbol)); }
    if (lens === "dip") return base.filter((r) => r.value?.is_dip_buy);
    if (lens === "dcf") return base.filter((r) => dcfTrusted(r.dcf) && (r.dcf?.upside_pct ?? -1) > 0);
    if (L.factor) {
      const fac = L.factor;
      const topN = new Set([...base].sort((a, b) => (b.factors?.[fac]?.score ?? -1) - (a.factors?.[fac]?.score ?? -1)).slice(0, 50).map((r) => r.symbol));
      return base.filter((r) => topN.has(r.symbol));
    }
    return base;
  }, [rows, rs.shortlists, lens, sector]);

  const kpis: Kpi[] = [
    { label: "Companies screened", value: String(rs.count ?? rows.length), note: isNum(rs.universe_size) ? `Of ${rs.universe_size} covered stocks` : undefined },
    { label: "Undervalued", value: String(dipCount), note: "Down from their highs, not in steep decline" },
    { label: "Cash-flow upside", value: String(dcfUp), note: "More than 10% below estimated value" },
  ];
  if (top) kpis.push({ label: "Top pick", value: top.symbol, note: `${top.name ?? top.symbol}, grade ${gradeOf(top)} of 100` });

  const count = (k: LensKey) => (k === "dip" ? ` ${dipCount}` : "");

  return (
    <>
      <div className="mt-4"><KpiRow items={kpis} /></div>

      <div className="mt-4 grid gap-4 lg:grid-cols-2">
        <Panel id="rating-split" title="How the ratings split" sub="Every company screened today, by the rating its grade earns.">
          <RatingSplit rows={rows} />
        </Panel>
        <Panel
          id="sector-grade"
          title="Average grade by sector"
          sub="Mean grade out of 100. Choose a sector to filter the screen below."
          action={sector !== "all" ? (
            <button type="button" className="fs-link bg-transparent border-0 p-0 cursor-pointer text-[0.875rem]" onClick={() => { setSector("all"); setLimit(PAGE); setWhy(null); }}>All sectors</button>
          ) : undefined}
        >
          <SectorGrades rows={rows} active={sector} onPick={(s) => { setSector((cur) => (cur === s ? "all" : s)); setLimit(PAGE); setWhy(null); }} />
        </Panel>
      </div>

      <Panel
        className="mt-4"
        id="screen"
        title="The screen"
        sub="Ranked by grade. Choose a company for its full detail, Valuation for what it is worth, or Why for how its grade is made."
      >
        {/* filters scope the table below them */}
        <div className="flex flex-wrap items-center gap-2 mb-3" role="group" aria-label="Lens">
          {LENSES.map((l) => (
            <button key={l.key} type="button" className="fs-chip" aria-pressed={lens === l.key} onClick={() => { setLens(l.key); setLimit(PAGE); setWhy(null); }}>
              {l.label}{count(l.key)}
            </button>
          ))}
        </div>
        <div className="flex flex-wrap items-center justify-between gap-3 mb-4">
          <div className="flex items-center gap-2">
            <label htmlFor="rs-sector" className="text-[0.875rem]" style={{ color: "var(--fs-ink-2)" }}>Sector</label>
            <select id="rs-sector" className="fs-input" value={sector} onChange={(e) => { setSector(e.target.value); setLimit(PAGE); setWhy(null); }}>
              <option value="all">All sectors</option>
              {sectors.map((s) => <option key={s} value={s}>{s}</option>)}
            </select>
          </div>
          <p className="fs-note m-0">{LENS_NOTE[lens] ?? `${shown.length} ${shown.length === 1 ? "company" : "companies"}`}</p>
        </div>

        {!shown.length ? (
          <p className="fs-note m-0">No company matches that lens in this sector. Choose another.</p>
        ) : (
          <div className="overflow-x-auto">
            <table className="fs-table">
              <thead>
                <tr>
                  <th scope="col" className="r hidden sm:table-cell" style={{ paddingLeft: 0, paddingRight: 12 }}>#</th>
                  <th scope="col">Company</th>
                  <th scope="col" className="r hidden sm:table-cell">Price</th>
                  <th scope="col" className="fs-gradecol">Grade</th>
                  {(["value", "quality", "growth", "momentum", "analyst"] as const).map((k) => (
                    <th key={k} scope="col" className="r hidden lg:table-cell" title={FACTOR_WHAT[k]}>{FACTOR_LABEL[k].replace(" view", "")}</th>
                  ))}
                  <th scope="col" className="r"><span className="sr-only">Valuation and why</span></th>
                </tr>
              </thead>
              <tbody>
                {shown.slice(0, limit).map((r) => {
                  const g = gradeOf(r);
                  const lvl = confLevel(r.confidence);
                  const isOpen = why === r.symbol;
                  return (
                    <Fragment key={r.symbol}>
                      <tr className={`fs-pick ${isOpen ? "fs-open" : ""}`} onClick={() => open(r.symbol)}>
                        <td className="r fs-note hidden sm:table-cell" style={{ paddingLeft: 10, paddingRight: 12 }}>{r.rank ?? ""}</td>
                        <th scope="row">
                          <span className="inline-flex items-center gap-2">
                            <button type="button" className="fs-rowbtn" onClick={(e) => { e.stopPropagation(); open(r.symbol); }}>
                              {r.symbol}
                            </button>
                            {r.stage && <span className="fs-note">{r.stage}</span>}
                            {hasModel(r.symbol) && <span className="fs-tag">Full model</span>}
                          </span>
                          <span className="block fs-note truncate max-w-[140px] sm:max-w-[220px]">{r.name}</span>
                        </th>
                        <td className="r hidden sm:table-cell">{isNum(r.last) ? money(r.last) : "–"}</td>
                        <td className="fs-gradecol">
                          <span className="inline-flex items-center gap-2 whitespace-nowrap">
                            <span className="fs-num font-semibold w-[1.6rem] text-right">{g}</span>
                            <ConfMark level={lvl} />
                            <span className="hidden sm:inline" style={{ color: g >= 66 ? "var(--fs-ink)" : "var(--fs-ink-2)", fontWeight: g >= 66 ? 600 : 400 }}>{ratingOf(g)}</span>
                          </span>
                        </td>
                        {(["value", "quality", "growth", "momentum", "analyst"] as const).map((k) => (
                          <td key={k} className="r hidden lg:table-cell fs-heatcell"><FactorNum v={r.factors?.[k]?.score} /></td>
                        ))}
                        <td className="r">
                          <span className="inline-flex flex-col items-end gap-1 whitespace-nowrap sm:flex-row sm:items-center sm:gap-4">
                            {/* straight to the valuation: the full model when one was built,
                                else the screen's cash-flow estimate. Absent when the screen
                                declined to value the company (Why explains that). */}
                            {(hasModel(r.symbol) || dcfTrusted(r.dcf)) && (
                              <button
                                type="button"
                                className="fs-link bg-transparent border-0 p-0 cursor-pointer text-[0.875rem]"
                                onClick={(e) => { e.stopPropagation(); openModel(r.symbol); }}
                              >
                                Valuation
                              </button>
                            )}
                            <button
                              type="button"
                              className="fs-link bg-transparent border-0 p-0 cursor-pointer text-[0.875rem]"
                              aria-expanded={isOpen}
                              onClick={(e) => { e.stopPropagation(); setWhy(isOpen ? null : r.symbol); }}
                            >
                              Why
                            </button>
                          </span>
                        </td>
                      </tr>
                      {isOpen && (
                        <tr className="fs-why">
                          <td colSpan={10}><Breakdown r={r} weights={rs.factor_weights} /></td>
                        </tr>
                      )}
                    </Fragment>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}
        <div className="mt-4 flex flex-wrap items-center justify-between gap-3">
          <p className="fs-note m-0 flex flex-wrap items-center gap-x-4 gap-y-1">
            <span>Showing {Math.min(limit, shown.length)} of {shown.length}</span>
            <span className="inline-flex items-center gap-1.5"><ConfMark level="high" /> high confidence</span>
            <span className="inline-flex items-center gap-1.5"><ConfMark level="medium" /> medium</span>
            <span className="inline-flex items-center gap-1.5"><ConfMark level="low" /> low</span>
          </p>
          {shown.length > limit && (
            <ToggleLink open={false} onClick={() => setLimit((n) => n + PAGE)} more={`Show ${Math.min(PAGE, shown.length - limit)} more`} less="" />
          )}
        </div>
      </Panel>

      <FactorPerformance />
    </>
  );
}

/** Confidence as SHAPE, not colour alone: filled, half, ring. */
function ConfMark({ level }: { level: "high" | "medium" | "low" }) {
  const c = "var(--fs-ink-2)";
  return (
    <svg width={10} height={10} viewBox="0 0 10 10" role="img" aria-label={`${level} confidence`} className="shrink-0">
      {level === "high" && <circle cx={5} cy={5} r={4} fill={c} />}
      {level === "medium" && (<><circle cx={5} cy={5} r={3.5} fill="none" stroke={c} strokeWidth={1.2} /><path d="M5 1.5 A3.5 3.5 0 0 1 5 8.5 Z" fill={c} /></>)}
      {level === "low" && <circle cx={5} cy={5} r={3.5} fill="none" stroke={c} strokeWidth={1.2} />}
    </svg>
  );
}

/** A factor score as a heat cell: one hue, deeper for stronger scores. The
 *  number stays in text ink, so the tint is a second cue, never the only one. */
function FactorNum({ v }: { v?: number | null }) {
  if (!isNum(v)) return <span className="fs-heat fs-note">–</span>;
  const s = Math.round(v);
  const tint = Math.round((Math.max(0, Math.min(100, s) - 20) / 80) * 34);
  return (
    <span className="fs-heat" style={{ background: tint > 0 ? `color-mix(in srgb, var(--fs-blue) ${tint}%, transparent)` : undefined, fontWeight: s >= 70 ? 600 : 400 }}>
      {s}
    </span>
  );
}

const RATINGS = ["Strong buy", "Buy", "Hold", "Sell", "Strong sell"] as const;

/* Ratings in order, one hue deepening toward strong buy. */
function RatingSplit({ rows }: { rows: ResearchRow[] }) {
  const counts = RATINGS.map((name) => rows.filter((r) => ratingOf(gradeOf(r)) === name).length);
  const total = counts.reduce((a, b) => a + b, 0) || 1;
  const max = Math.max(...counts, 1);
  const depth = [100, 72, 46, 30, 20];
  const range = ["Grade 66 and up", "54 to 65", "42 to 53", "30 to 41", "Below 30"];
  const most = counts.indexOf(max);
  return (
    <>
    <ul className="list-none p-0 m-0 grid gap-y-4">
      {RATINGS.map((name, i) => (
        <li key={name} className="grid items-center gap-x-4" style={{ gridTemplateColumns: "minmax(0, 8rem) 1fr" }}>
          <span className="text-[0.9375rem] leading-tight" style={{ color: "var(--fs-ink-2)" }}>
            {name}
            <span className="block fs-note text-[0.8125rem]">{range[i]}</span>
          </span>
          <span className="flex items-center gap-3 min-w-0">
            <span className="flex-1 min-w-0">
              <span aria-hidden className="block h-[10px] rounded-r-[4px]"
                style={{ width: `${Math.max((counts[i] / max) * 100, counts[i] ? 1 : 0)}%`, background: `color-mix(in srgb, var(--fs-blue) ${depth[i]}%, var(--fs-canvas))` }} />
            </span>
            <span className="fs-num text-[0.9375rem] whitespace-nowrap w-[4.75rem] shrink-0">
              {counts[i]}<span className="fs-note ml-1.5">{Math.round((counts[i] / total) * 100)}%</span>
            </span>
          </span>
        </li>
      ))}
    </ul>
    <p className="fs-note mt-5 mb-0">
      Most companies rate {RATINGS[most].toLowerCase()}. The bar for strong buy is deliberately high: {counts[0]} {counts[0] === 1 ? "company clears" : "companies clear"} it today.
    </p>
    </>
  );
}

/* Mean grade per sector on a 0 to 100 scale; each row filters the screen. */
function SectorGrades({ rows, active, onPick }: { rows: ResearchRow[]; active: string; onPick: (s: string) => void }) {
  const items = useMemo(() => {
    const m = new Map<string, { sum: number; n: number }>();
    for (const r of rows) {
      const s = sectorName(r.sector);
      const cur = m.get(s) ?? { sum: 0, n: 0 };
      cur.sum += gradeOf(r); cur.n += 1; m.set(s, cur);
    }
    return [...m.entries()].map(([name, { sum, n }]) => ({ name, n, avg: sum / n })).sort((a, b) => b.avg - a.avg);
  }, [rows]);
  return (
    <ul className="list-none p-0 m-0 grid gap-y-0.5">
      {items.map((it) => {
        const on = active === it.name;
        const dim = active !== "all" && !on;
        return (
          <li key={it.name}>
            <button type="button" className="fs-barrow" aria-pressed={on} onClick={() => onPick(it.name)} title={`${it.n} companies, average grade ${Math.round(it.avg)}`}>
              <span className="truncate text-left" style={{ color: on ? "var(--fs-ink)" : "var(--fs-ink-2)", fontWeight: on ? 600 : 400 }}>{it.name}</span>
              <span className="flex items-center gap-3 min-w-0">
                <span aria-hidden className="h-[10px] rounded-r-[4px] shrink-0"
                  style={{ width: `${(it.avg / 100) * 76}%`, background: on ? "var(--fs-blue)" : "var(--fs-steel)", opacity: dim ? 0.35 : 1 }} />
                <span className="fs-num text-[0.9375rem] whitespace-nowrap">{Math.round(it.avg)}</span>
              </span>
            </button>
          </li>
        );
      })}
    </ul>
  );
}

/* "Why": the composite blend, every component, and the valuation. */
function Breakdown({ r, weights }: { r: ResearchRow; weights?: Record<string, number> }) {
  const notes = (r.confidence_notes ?? []).slice(0, 3).map(tidyNote);
  return (
    <div className="px-4 sm:px-6 py-5 text-[0.9375rem]">
      <p className="m-0">
        <span className="font-semibold">{r.name ?? r.symbol}</span>
        <span style={{ color: "var(--fs-ink-2)" }}>
          : composite {isNum(r.score) ? Math.round(r.score) : "–"} of 100
          {isNum(r.confidence) ? `, confidence ${Math.round(r.confidence * 100)}%` : ""}, giving a grade of {gradeOf(r)}.
        </span>
      </p>
      {notes.length > 0 && (
        <ul className="list-none p-0 m-0 mt-1.5 fs-note">{notes.map((n) => <li key={n}>{n}</li>)}</ul>
      )}
      <div className="mt-4 grid gap-x-10 gap-y-5 md:grid-cols-2">
        {FACTOR_ORDER.map((k) => {
          const fac = r.factors?.[k];
          if (!fac?.components?.length || fac.score == null) return null;
          return (
            <div key={k}>
              <p className="m-0 mb-1.5 font-semibold">
                {FACTOR_LABEL[k]} {Math.round(fac.score)}
                {isNum(weights?.[k]) && <span className="fs-note font-normal">, weight {weights![k]}%</span>}
              </p>
              <dl className="m-0">
                {fac.components.map((c) => (
                  <div key={c.name} className="grid grid-cols-[minmax(0,9rem)_1fr_auto] gap-x-3 py-1" style={{ borderBottom: "1px solid var(--fs-line)" }}>
                    <dt style={{ color: "var(--fs-ink-2)" }}>{c.name}</dt>
                    <dd className="m-0 fs-note">{c.detail}</dd>
                    <dd className="m-0 fs-num text-right whitespace-nowrap">
                      {/* a line worth no points (the AI read's thesis and risk) is text only */}
                      {!c.max_points ? null
                        : isNum(c.points) ? <>{c.points}<span className="fs-note">/{c.max_points}</span></> : <span className="fs-note">n/a</span>}
                    </dd>
                  </div>
                ))}
              </dl>
            </div>
          );
        })}
      </div>
      <div className="mt-5 pt-4" style={{ borderTop: "1px solid var(--fs-line)" }}>
        <p className="m-0 mb-1.5 font-semibold">Valuation</p>
        <Valuation dcf={r.dcf ?? null} row={r} />
      </div>
    </div>
  );
}

/* The original's backtest panel: does each price signal predict? */
function FactorPerformance() {
  const { data } = useFactorPerf();
  const factors = (data?.factors ?? []).filter((f) => f.n_periods);
  if (!factors.length) return null;
  return (
    <Panel
      className="mt-4"
      id="factor-perf"
      title="Does each signal predict returns?"
      sub={`Stocks are split into fifths by each price signal every month; this is the top fifth's next-month return minus the bottom fifth's.${data?.window ? ` ${data.window.replace("→", "to")}.` : ""}`}
    >
      <div className="overflow-x-auto">
        <table className="fs-table">
          <thead>
            <tr>
              <th scope="col">Signal</th>
              <th scope="col" className="r">Yearly spread</th>
              <th scope="col" className="r">Months it worked</th>
              <th scope="col" className="r hidden sm:table-cell">Top fifth, per month</th>
              <th scope="col" className="r hidden sm:table-cell">Bottom fifth, per month</th>
            </tr>
          </thead>
          <tbody>
            {factors.map((f) => (
              <tr key={f.name}>
                <th scope="row">{SIGNAL_LABEL[f.name] ?? f.name.replace(/_/g, " ")}</th>
                <td className="r">{isNum(f.ann_spread_pct) ? <Delta value={f.ann_spread_pct} text={signedPct(f.ann_spread_pct)} /> : "–"}</td>
                <td className="r">{isNum(f.hit_rate) ? `${Math.round(f.hit_rate)}%` : "–"}</td>
                <td className="r hidden sm:table-cell">{isNum(f.q5_avg_pct) ? signedPct(f.q5_avg_pct, 2) : "–"}</td>
                <td className="r hidden sm:table-cell">{isNum(f.q1_avg_pct) ? signedPct(f.q1_avg_pct, 2) : "–"}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {data?.caveat && <p className="fs-note mt-4 mb-0">{data.caveat.replace(/—/g, ",")}</p>}
    </Panel>
  );
}

/* ── Valuation view (rebuild of the original's DCF valuation tab) ──────────── */
type VFilter = "under" | "over" | "all" | "declined" | "models";

/* Upside bands for the distribution. Bands widen away from zero, so the bars
   are categories (a count per band), not a density. */
const BANDS: { key: string; lo: number; hi: number; label: string }[] = [
  { key: "a", lo: -Infinity, hi: -50, label: "downside of more than 50%" },
  { key: "b", lo: -50, hi: -25, label: "downside of 25% to 50%" },
  { key: "c", lo: -25, hi: -10, label: "downside of 10% to 25%" },
  { key: "d", lo: -10, hi: 0, label: "downside of up to 10%" },
  { key: "e", lo: 0, hi: 10, label: "upside of up to 10%" },
  { key: "f", lo: 10, hi: 25, label: "upside of 10% to 25%" },
  { key: "g", lo: 25, hi: 50, label: "upside of 25% to 50%" },
  { key: "h", lo: 50, hi: 100, label: "upside of 50% to 100%" },
  { key: "i", lo: 100, hi: Infinity, label: "upside of more than 100%" },
];
const EDGES = ["−50%", "−25%", "−10%", "0%", "10%", "25%", "50%", "100%"];
const inBand = (v: number, b: (typeof BANDS)[number]) => v >= b.lo && v < b.hi;

function ValuationView({ rows, bySymbol }: { rows: ResearchRow[]; bySymbol: Map<string, ResearchRow> }) {
  const { openModel, hasModel } = useStock();
  const { data: deep } = useDeepModels();
  const [filter, setFilter] = useState<VFilter>("under");
  const [band, setBand] = useState<string | null>(null);
  const [limit, setLimit] = useState(PAGE);

  const valued = useMemo(() => rows.filter((r) => dcfTrusted(r.dcf) && isNum(r.dcf?.upside_pct)), [rows]);
  const declined = useMemo(() => rows.filter((r) => dcfDeclined(r.dcf)), [rows]);
  const up = (r: ResearchRow) => r.dcf!.upside_pct as number;
  const under = valued.filter((r) => up(r) > 0);
  const over = valued.filter((r) => up(r) <= 0);
  const ups = valued.map(up).sort((a, b) => a - b);
  const median = ups.length ? ups[Math.floor(ups.length / 2)] : null;
  const cheap = valued.filter((r) => up(r) > 10).length;
  const models = deep?.models ?? [];

  const activeBand = BANDS.find((b) => b.key === band) ?? null;
  const list = activeBand ? valued.filter((r) => inBand(up(r), activeBand)) : filter === "under" ? under : filter === "over" ? over : valued;
  const sorted = [...list].sort((a, b) => up(b) - up(a));
  const tableKind: "valued" | "declined" | "models" = activeBand ? "valued" : filter === "declined" ? "declined" : filter === "models" ? "models" : "valued";

  const reasons = useMemo(() => {
    const m = new Map<string, number>();
    for (const r of declined) { const w = declineReason(r); if (w) m.set(w, (m.get(w) ?? 0) + 1); }
    return [...m.entries()].map(([name, value]) => ({ name, value })).sort((a, b) => b.value - a.value);
  }, [declined]);

  const kpis: Kpi[] = [
    { label: "Valued", value: String(valued.length), note: `Of ${rows.length} screened stocks` },
    { label: "Median upside", value: isNum(median) ? signedPct(median, 0) : "–", note: "Estimated value against price" },
    { label: "More than 10% below value", value: String(cheap), note: "Candidates worth a closer look" },
    { label: "Full models", value: String(models.length), note: "Spreadsheet models built from SEC filings" },
  ];
  const chips: [VFilter, string, number][] = [
    ["under", "Undervalued", under.length], ["over", "Overvalued", over.length], ["all", "All valued", valued.length],
    ["declined", "Declined", declined.length], ["models", "Full models", models.length],
  ];
  const pick = (k: VFilter) => { setFilter(k); setBand(null); setLimit(PAGE); };
  const pickBand = (k: string) => { setBand((cur) => (cur === k ? null : k)); setLimit(PAGE); };

  const title = activeBand
    ? `Estimated ${activeBand.label}`
    : tableKind === "declined" ? "Declined, and why" : tableKind === "models" ? "Full valuation models" : "Estimated value against price";
  const sub = tableKind === "declined"
    ? "Rather than print a target it cannot stand behind, the model says why it declined. Choose a company to see its numbers."
    : tableKind === "models"
      ? "Assumptions from each company's latest SEC filing, a full spreadsheet built from them, every figure recalculated before publishing. Choose one to open it."
      : "Sorted by upside. Choose a company to open its valuation model.";
  const total = tableKind === "declined" ? declined.length : sorted.length;

  return (
    <>
      <div className="mt-4"><KpiRow items={kpis} /></div>

      <div className="mt-4 grid gap-4 lg:grid-cols-2">
        <Panel
          id="upside-dist"
          title="Where prices sit against estimated value"
          sub={`${valued.length} companies with a value the model stands behind. Choose a bar to list them.`}
        >
          <UpsideHistogram values={ups} active={band} onPick={pickBand} />
        </Panel>
        <Panel
          id="decline-why"
          title="Why companies were declined"
          sub={`${declined.length} companies are not valued, because a cash-flow model would mislead.`}
          action={<button type="button" className="fs-link bg-transparent border-0 p-0 cursor-pointer text-[0.875rem]" onClick={() => pick("declined")}>List them</button>}
        >
          <RankBars rows={reasons} format={(v) => String(v)} labelWidth="min(15.5rem, 52%)" />
        </Panel>
      </div>

      <Panel
        className="mt-4"
        id="valuation"
        title={title}
        sub={sub}
        action={activeBand ? (
          <button type="button" className="fs-link bg-transparent border-0 p-0 cursor-pointer text-[0.875rem]" onClick={() => setBand(null)}>Clear</button>
        ) : undefined}
      >
        <div className="flex flex-wrap gap-2 mb-5" role="group" aria-label="Valuation filter">
          {chips.filter(([k, , n]) => k !== "models" || n > 0).map(([k, label, n]) => (
            <button key={k} type="button" className="fs-chip" aria-pressed={!activeBand && filter === k} onClick={() => pick(k)}>
              {label} <span className="fs-num">{n}</span>
            </button>
          ))}
        </div>

        {tableKind === "models" ? (
          <DeepTable models={models} names={bySymbol} />
        ) : tableKind === "declined" ? (
          <div className="overflow-x-auto">
            <table className="fs-table">
              <thead>
                <tr>
                  <th scope="col">Company</th>
                  <th scope="col" className="r hidden sm:table-cell">Price</th>
                  <th scope="col" style={{ paddingLeft: 24 }}>Why it was not valued</th>
                </tr>
              </thead>
              <tbody>
                {declined.slice(0, limit).map((r) => {
                  const why = declineReason(r);
                  const implied = impliedNote(r.dcf);
                  return (
                    <tr key={r.symbol} className="fs-pick" onClick={() => openModel(r.symbol)}>
                      <th scope="row">
                        <Company r={r} hasModel={hasModel(r.symbol)} onOpen={() => openModel(r.symbol)} />
                      </th>
                      <td className="r hidden sm:table-cell">{isNum(r.last) ? money(r.last) : "–"}</td>
                      <td style={{ paddingLeft: 24 }}>
                        <span className="font-medium">{why}</span>
                        <span className="block fs-note">{implied ?? (why ? REASON_NOTE[why] : "")}</span>
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        ) : !sorted.length ? (
          <p className="fs-note m-0">No company falls in that band today.</p>
        ) : (
          <div className="overflow-x-auto">
            <table className="fs-table">
              <thead>
                <tr>
                  <th scope="col">Company</th>
                  <th scope="col" className="r hidden sm:table-cell">Price</th>
                  <th scope="col" className="r hidden sm:table-cell">Estimated value</th>
                  <th scope="col" className="r">Upside</th>
                  <th scope="col" className="r hidden md:table-cell">Discount rate</th>
                  <th scope="col" className="r hidden md:table-cell">Growth</th>
                  <th scope="col" className="r hidden lg:table-cell">Price implies</th>
                  <th scope="col" className="hidden lg:table-cell" style={{ paddingLeft: 24 }}>Confidence</th>
                </tr>
              </thead>
              <tbody>
                {sorted.slice(0, limit).map((r) => {
                  const d = r.dcf!;
                  return (
                    <tr key={r.symbol} className="fs-pick" onClick={() => openModel(r.symbol)}>
                      <th scope="row">
                        <Company r={r} hasModel={hasModel(r.symbol)} onOpen={() => openModel(r.symbol)} />
                      </th>
                      <td className="r hidden sm:table-cell">{isNum(r.last) ? money(r.last) : "–"}</td>
                      <td className="r font-semibold hidden sm:table-cell">{isNum(d.fair_value) ? money(d.fair_value) : "–"}</td>
                      <td className="r">
                        <span className="inline-flex items-center justify-end gap-3">
                          <UpsideBar v={up(r)} />
                          <span className="w-[3.25rem]"><Delta value={up(r)} text={signedPct(up(r), 0)} /></span>
                        </span>
                      </td>
                      <td className="r hidden md:table-cell">{isNum(d.wacc_pct) ? plainPct(d.wacc_pct) : "–"}</td>
                      <td className="r hidden md:table-cell">{isNum(d.stage1_growth_pct) ? plainPct(d.stage1_growth_pct) : "–"}</td>
                      <td className="r hidden lg:table-cell">{isNum(d.implied_growth_pct) ? plainPct(d.implied_growth_pct) : "–"}</td>
                      <td className="hidden lg:table-cell" style={{ paddingLeft: 24, color: "var(--fs-ink-2)" }}>{dcfConfidence(d.confidence) ?? "–"}</td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}

        {tableKind !== "models" && total > 0 && (
          <div className="mt-4 flex flex-wrap items-center justify-between gap-3">
            <p className="fs-note m-0">Showing {Math.min(limit, total)} of {total}</p>
            {total > limit && (
              <ToggleLink open={false} onClick={() => setLimit((n) => n + PAGE)} more={`Show ${Math.min(PAGE, total - limit)} more`} less="" />
            )}
          </div>
        )}
      </Panel>
    </>
  );
}

/* Symbol, a "Full model" tag when one exists, and the company name. */
function Company({ r, hasModel, onOpen }: { r: ResearchRow; hasModel: boolean; onOpen: () => void }) {
  return (
    <>
      <span className="inline-flex items-center gap-2">
        <button type="button" className="fs-rowbtn" onClick={(e) => { e.stopPropagation(); onOpen(); }}>{r.symbol}</button>
        {hasModel && <span className="fs-tag">Full model</span>}
      </span>
      <span className="block fs-note truncate max-w-[160px] sm:max-w-[220px]">{r.name}</span>
    </>
  );
}

/* Upside as a diverging bar around zero, capped at ±100% so outliers do not
   flatten everyone else. The number beside it carries the exact value. */
function UpsideBar({ v }: { v: number }) {
  const W = 72, mid = W / 2;
  const len = (Math.min(Math.abs(v), 100) / 100) * (mid - 1);
  const pos = v > 0;
  return (
    <svg width={W} height={12} aria-hidden className="hidden sm:block shrink-0">
      <line x1={mid} x2={mid} y1={0} y2={12} stroke="var(--fs-line-strong)" strokeWidth={1} />
      {len > 0.5 && (
        <rect
          x={pos ? mid + 1 : mid - 1 - len} y={3} width={len} height={6} rx={2}
          fill={pos ? "var(--fs-blue)" : "var(--fs-steel)"}
        />
      )}
    </svg>
  );
}

/* Count of valued companies per upside band. Blue is below estimated value
   (upside), grey is above it. Each bar is a button that filters the table. */
function UpsideHistogram({ values, active, onPick }: { values: number[]; active: string | null; onPick: (k: string) => void }) {
  const counts = BANDS.map((b) => values.filter((v) => inBand(v, b)).length);
  const max = Math.max(...counts, 1);
  const H = 210;
  return (
    <div>
      <div className="flex flex-wrap gap-x-6 gap-y-1 mb-4 text-[0.8125rem]" style={{ color: "var(--fs-ink-2)" }}>
        <span className="inline-flex items-center gap-2"><span aria-hidden className="inline-block w-[10px] h-[10px] rounded-[3px]" style={{ background: "var(--fs-blue)" }} />Price below estimated value</span>
        <span className="inline-flex items-center gap-2"><span aria-hidden className="inline-block w-[10px] h-[10px] rounded-[3px]" style={{ background: "var(--fs-steel)" }} />Price above it</span>
      </div>
      <div className="grid gap-[2px]" style={{ gridTemplateColumns: `repeat(${BANDS.length}, minmax(0, 1fr))` }}>
        {BANDS.map((b, i) => {
          const n = counts[i];
          const h = n ? Math.max((n / max) * H, 3) : 0;
          const dim = active && active !== b.key;
          return (
            <button
              key={b.key}
              type="button"
              className="fs-bin"
              aria-pressed={active === b.key}
              aria-label={`${n} ${n === 1 ? "company" : "companies"} with an estimated ${b.label}`}
              title={`${n} with an estimated ${b.label}`}
              onClick={() => onPick(b.key)}
              disabled={!n}
              style={{ height: H + 22 }}
            >
              <span className="fs-num text-[0.8125rem]" style={{ color: "var(--fs-ink-2)" }}>{n || ""}</span>
              <span
                aria-hidden
                className="block w-full rounded-t-[4px]"
                style={{ height: h, background: b.lo >= 0 ? "var(--fs-blue)" : "var(--fs-steel)", opacity: dim ? 0.3 : 1 }}
              />
            </button>
          );
        })}
      </div>
      <div className="relative h-5 mt-1" style={{ borderTop: "1px solid var(--fs-line-strong)" }} aria-hidden>
        {EDGES.map((e, i) => (
          <span
            key={e}
            className={`absolute top-1 -translate-x-1/2 fs-num text-[0.75rem] whitespace-nowrap ${e === "−25%" || e === "25%" ? "hidden sm:block" : ""}`}
            style={{ left: `${((i + 1) / BANDS.length) * 100}%`, color: e === "0%" ? "var(--fs-ink)" : "var(--fs-ink-3)", fontWeight: e === "0%" ? 600 : 400 }}
          >
            {e}
          </span>
        ))}
      </div>
      <p className="fs-note mt-3 mb-0">Upside to estimated value. Bands widen away from zero.</p>
    </div>
  );
}

/* Full models: bear to bull range with the base case and today's price. */
function Range({ bear, base, bull, price }: { bear: number; base: number; bull: number; price: number }) {
  const W = 168, H = 22, pad = 6;
  const lo = Math.min(bear, price), hi = Math.max(bull, price);
  const x = (v: number) => pad + ((v - lo) / (hi - lo || 1)) * (W - 2 * pad);
  return (
    <svg width={W} height={H} aria-hidden className="block">
      <line x1={x(bear)} x2={x(bull)} y1={H / 2} y2={H / 2} stroke="var(--fs-steel)" strokeWidth={2} strokeLinecap="round" />
      <line x1={x(price)} x2={x(price)} y1={3} y2={H - 3} stroke="var(--fs-ink)" strokeWidth={2} strokeLinecap="round" />
      <circle cx={x(base)} cy={H / 2} r={5} fill="var(--fs-ember)" stroke="var(--fs-canvas)" strokeWidth={2} />
    </svg>
  );
}

function DeepTable({ models, names }: { models: DeepModel[]; names: Map<string, ResearchRow> }) {
  const { openModel } = useStock();
  const [limit, setLimit] = useState(PAGE);
  const rows = models
    .map((m) => {
      const base = m.scenarios?.base?.fair_value;
      // today's price from the screen; the model's own price is from the day it was built
      const today = names.get(m.symbol)?.last;
      const price = isNum(today) ? today : m.price;
      const up = isNum(base) && isNum(price) && price > 0 ? ((base - price) / price) * 100 : null;
      return { m, price, name: names.get(m.symbol)?.name ?? m.symbol, bear: m.scenarios?.bear?.fair_value, base, bull: m.scenarios?.bull?.fair_value, up };
    })
    .sort((a, b) => (b.up ?? -Infinity) - (a.up ?? -Infinity));
  const ups = rows.map((r) => r.up).filter(isNum).sort((a, b) => a - b);
  const median = ups.length ? ups[Math.floor(ups.length / 2)] : null;
  const above = ups.filter((u) => u > 0).length;
  return (
    <>
      {isNum(median) && (
        <p className="fs-note mt-0 mb-4 max-w-[72ch]">
          Read these to compare companies, not as price targets. They lean conservative: the median base case is {signedPct(median, 0)} against today&apos;s
          price and {above} of {ups.length} come out above it, mostly because a 10-year Treasury above 5% puts most discount rates near 10% to 11%.
        </p>
      )}
      <div className="hidden md:flex flex-wrap gap-x-6 gap-y-1 mb-4 text-[0.8125rem]" style={{ color: "var(--fs-ink-2)" }}>
        <span className="inline-flex items-center gap-2"><span aria-hidden className="inline-block w-[18px] h-[2px] rounded-full" style={{ background: "var(--fs-steel)" }} />Bear case to bull case</span>
        <span className="inline-flex items-center gap-2"><span aria-hidden className="inline-block w-[10px] h-[10px] rounded-full" style={{ background: "var(--fs-ember)" }} />Base case</span>
        <span className="inline-flex items-center gap-2"><span aria-hidden className="inline-block w-[2px] h-[14px] rounded-full" style={{ background: "var(--fs-ink)" }} />Today&apos;s price</span>
      </div>
      <div className="overflow-x-auto">
        <table className="fs-table">
          <thead>
            <tr>
              <th scope="col">Company</th>
              <th scope="col" className="r hidden sm:table-cell">Price</th>
              <th scope="col" className="r hidden md:table-cell">Bear</th>
              <th scope="col" className="r">Base</th>
              <th scope="col" className="r hidden md:table-cell">Bull</th>
              <th scope="col" className="r">Base vs price</th>
              <th scope="col" className="hidden lg:table-cell"><span className="sr-only">Range</span></th>
            </tr>
          </thead>
          <tbody>
            {rows.slice(0, limit).map(({ m, price, name, bear, base, bull, up }) => (
              <tr key={m.symbol} className="fs-pick" onClick={() => openModel(m.symbol)}>
                <th scope="row">
                  <button type="button" className="fs-rowbtn" onClick={(e) => { e.stopPropagation(); openModel(m.symbol); }}>{m.symbol}</button>
                  <span className="block fs-note truncate max-w-[160px] sm:max-w-[220px]">{name}</span>
                </th>
                <td className="r hidden sm:table-cell">{isNum(price) ? money(price) : "–"}</td>
                <td className="r hidden md:table-cell">{isNum(bear) ? money(bear) : "–"}</td>
                <td className="r font-semibold">{isNum(base) ? money(base) : "–"}</td>
                <td className="r hidden md:table-cell">{isNum(bull) ? money(bull) : "–"}</td>
                <td className="r">{isNum(up) ? <Delta value={up} text={signedPct(up, 0)} /> : "–"}</td>
                <td className="hidden lg:table-cell" style={{ paddingLeft: 24 }}>
                  {isNum(bear) && isNum(base) && isNum(bull) && isNum(price) && <Range bear={bear} base={base} bull={bull} price={price} />}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <div className="mt-4 flex flex-wrap items-center justify-between gap-3">
        <p className="fs-note m-0">Showing {Math.min(limit, rows.length)} of {rows.length}, sorted by base case against price</p>
        {rows.length > limit && (
          <ToggleLink open={false} onClick={() => setLimit((n) => n + PAGE)} more={`Show ${Math.min(PAGE, rows.length - limit)} more`} less="" />
        )}
      </div>
    </>
  );
}
