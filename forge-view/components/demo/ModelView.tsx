"use client";

// The valuation model for one company, opened from the Valuation view.
//
// A company with a FULL model (assumptions from SEC filings, spreadsheet built and
// recalculated by a separate check) shows: bear/base/bull, the thesis, the key
// assumptions, what would make it wrong, and the spreadsheet itself, read back
// as computed values from GET /api/robinhood/deep-dcf/{sym}/sheet.
// Every other company shows the screen's own cash-flow model, or why it was
// declined. Nothing personal is read: only model output.

import { useMemo, useState, type ReactNode } from "react";
import useSWR from "swr";
import { fetcher } from "@/lib/api";
import { Delta } from "./Bits";
import { FullPage } from "./FullPage";
import { LiveSheet } from "./LiveSheet";
import { rebuild } from "./screenModel";
import { isNum, longDay, signedPct, plainPct, unbrand } from "./fmt";
import {
  REASON_NOTE, dcfConfidence, dcfTrusted, declineReason, impliedNote, sectorName, tidyNote,
  type ResearchRow,
} from "./research";

/* ── payloads ──────────────────────────────────────────────────────────────── */
export interface FullModel {
  symbol: string;
  as_of?: string;
  price?: number | null;
  thesis?: string | null;
  key_assumptions?: string[] | null;
  caveats?: string[] | null;
  tv_fraction?: number | null;
  filing?: { form?: string; filing_date?: string; period_end?: string } | null;
  scenarios?: Partial<Record<"bear" | "base" | "bull", { fair_value?: number | null; wacc?: number | null; terminal_growth?: number | null }>> | null;
  upside_pct?: Partial<Record<"bear" | "base" | "bull", number | null>> | null;
}
type Cell = string | number | boolean;
interface SheetPayload { symbol: string; as_of?: string; sheets: { name: string; rows: { r: number; c: Record<string, Cell> }[] }[] }

const ONCE = { revalidateOnFocus: false, revalidateIfStale: false, shouldRetryOnError: false } as const;
const sheetKey = (s: string) => `/api/robinhood/deep-dcf/${encodeURIComponent(s)}/sheet`;
const money = (v: number) => `$${v.toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;

/* ── shell ─────────────────────────────────────────────────────────────────── */
export function ModelView({
  symbol, row, model, onClose, onOpenStock, back,
}: {
  back?: string;
  symbol: string;
  row?: ResearchRow;
  model?: FullModel;
  onClose: () => void;
  onOpenStock: (s: string) => void;
}) {
  const [tab, setTab] = useState<"summary" | "sheet" | "live">("summary");
  // The Spreadsheet tab appears only once the workbook can actually be read,
  // so a deployment without the sheet endpoint never shows a dead tab.
  const { data: sheet } = useSWR<SheetPayload>(model ? sheetKey(symbol) : null, fetcher, ONCE);
  const hasSheet = !!sheet?.sheets?.length;
  // The screen's model, rebuilt in the browser so it can be edited (null when it
  // cannot be reproduced to the cent, or the screen declined to value it).
  const rebuilt = useMemo(() => (dcfTrusted(row?.dcf) ? rebuild(row?.dcf) : null), [row]);
  const tabs: ["summary" | "sheet" | "live", string][] = [["summary", "Summary"]];
  if (model && hasSheet) tabs.push(["sheet", "Spreadsheet"]);
  if (rebuilt) tabs.push(["live", model ? "Screen model" : "Spreadsheet"]);

  const name = row?.name ?? symbol;
  // Today's price from the research screen; the full model's own price is from the day it was built.
  const price = row?.last ?? model?.price ?? null;

  return (
    <FullPage
      eyebrow={model ? "Full valuation model" : "Cash-flow valuation"}
      title={name}
      meta={<>{symbol}{row?.sector ? `, ${sectorName(row.sector)}` : ""}{isNum(price) ? `, ${money(price)}` : ""}</>}
      link={
        <button type="button" className="fs-link bg-transparent border-0 p-0 cursor-pointer text-[0.875rem]" onClick={() => onOpenStock(symbol)}>
          Company detail
        </button>
      }
      side={tabs.length > 1 && (
        <div className="fs-seg" role="group" aria-label="Model view">
          {tabs.map(([k, label]) => (
            <button key={k} type="button" aria-pressed={tab === k} onClick={() => setTab(k)}>{label}</button>
          ))}
        </div>
      )}
      onClose={onClose}
      back={back}
    >
      {tab === "live" && rebuilt ? (
        <LiveSheet
          rebuilt={rebuilt}
          price={isNum(price) ? price : isNum(row?.dcf?.current_price) ? row!.dcf!.current_price! : null}
          intro={model
            ? "The research screen's simpler ten-year model for the same company, separate from the full model. Change the blue assumptions and every figure recalculates."
            : "The research screen's ten-year cash-flow model for this company. Change the blue assumptions and every figure recalculates."}
        />
      ) : model ? (
        tab === "sheet" && hasSheet ? <Spreadsheet symbol={symbol} /> : <FullSummary model={model} row={row} price={price} />
      ) : (
        <ScreenModel row={row} symbol={symbol} live={!!rebuilt} />
      )}
    </FullPage>
  );
}

/* ── full model: summary ───────────────────────────────────────────────────── */
function FullSummary({ model, row, price }: { model: FullModel; row?: ResearchRow; price: number | null }) {
  const sc: NonNullable<FullModel["scenarios"]> = model.scenarios ?? {};
  const cases: { key: "bear" | "base" | "bull"; label: string }[] = [
    { key: "bear", label: "Bear case" }, { key: "base", label: "Base case" }, { key: "bull", label: "Bull case" },
  ];
  const vals = cases.map((c) => sc[c.key]?.fair_value).filter(isNum) as number[];
  const screen = row?.dcf;
  return (
    <>
      <div className="mt-6 grid gap-3 sm:grid-cols-3">
        {cases.map(({ key, label }) => {
          const s = sc[key];
          // against today's price, so the cards, the range tick and the header agree
          const up = isNum(s?.fair_value) && isNum(price) && price > 0 ? (s!.fair_value! / price - 1) * 100 : model.upside_pct?.[key];
          return (
            <div key={key} className="rounded-xl p-4" style={{
              border: `1px solid ${key === "base" ? "var(--fs-ink-3)" : "var(--fs-line)"}`,
              background: key === "base" ? "var(--fs-wash)" : "var(--fs-canvas)",
            }}>
              <p className="m-0 text-[0.875rem]" style={{ color: "var(--fs-ink-2)" }}>{label}</p>
              <p className="m-0 mt-1.5 text-[1.75rem] leading-none font-semibold">{isNum(s?.fair_value) ? money(s!.fair_value!) : "–"}</p>
              {isNum(up) && <p className="m-0 mt-1.5 text-[0.9375rem]"><Delta value={up} text={`${signedPct(up, 0)} vs price`} /></p>}
              <p className="fs-note m-0 mt-2">
                {isNum(s?.wacc) && <>Discount rate {plainPct(+(s!.wacc! * 100).toFixed(1))}</>}
                {isNum(s?.terminal_growth) && <>, long-run growth {plainPct(+(s!.terminal_growth! * 100).toFixed(1))}</>}
              </p>
            </div>
          );
        })}
      </div>

      {vals.length === 3 && isNum(price) && <BigRange bear={vals[0]} base={vals[1]} bull={vals[2]} price={price} />}

      {model.thesis && (
        <Block title="The case in brief">
          <p className="m-0 fs-prose" style={{ maxWidth: "none" }}>{model.thesis}</p>
        </Block>
      )}

      {(model.key_assumptions?.length ?? 0) > 0 && (
        <Block title="What the model assumes">
          <ul className="m-0 p-0 list-none grid gap-3">
            {model.key_assumptions!.map((a) => (
              <li key={a} className="pl-4 text-[0.9375rem]" style={{ borderLeft: "2px solid var(--fs-line-strong)", color: "var(--fs-ink-2)" }}>{a}</li>
            ))}
          </ul>
        </Block>
      )}

      {(model.caveats?.length ?? 0) > 0 && (
        <Block title="What would make it wrong">
          <ul className="m-0 p-0 list-none grid gap-3">
            {model.caveats!.map((c) => (
              <li key={c} className="pl-4 text-[0.9375rem]" style={{ borderLeft: "2px solid var(--fs-line-strong)", color: "var(--fs-ink-2)" }}>{c}</li>
            ))}
          </ul>
        </Block>
      )}

      <Block title="How it was built">
        <dl className="m-0 grid gap-x-8 gap-y-4 sm:grid-cols-2 text-[0.9375rem]">
          <Fact k="Source">
            SEC filings{model.filing?.form ? `, latest ${model.filing.form}` : ""}{model.filing?.filing_date ? ` filed ${longDay(model.filing.filing_date)}` : ""}
          </Fact>
          <Fact k="Built">{model.as_of ? longDay(model.as_of) : "–"}; assumptions set by an AI model from the filing, valued with a standard ten-year DCF</Fact>
          <Fact k="Checked">Every figure recalculated by a separate program before publishing; scenarios, units and sensitivities had to agree</Fact>
          {isNum(model.tv_fraction) && <Fact k="Long-run share">{Math.round(model.tv_fraction * 100)}% of the value comes from beyond the ten-year forecast</Fact>}
          {dcfTrusted(screen) && isNum(screen?.fair_value) && (
            <Fact k="The screen's own model">
              {money(screen!.fair_value!)}{isNum(screen?.upside_pct) ? `, ${signedPct(screen!.upside_pct!, 0)}` : ""}, a simpler one-stage model used for ranking
            </Fact>
          )}
        </dl>
      </Block>
    </>
  );
}

function Block({ title, children }: { title: string; children: ReactNode }) {
  return (
    <section className="mt-8">
      <h3 className="fs-h3 m-0 mb-3">{title}</h3>
      {children}
    </section>
  );
}
function Fact({ k, children }: { k: string; children: ReactNode }) {
  return (
    <div>
      <dt className="fs-note">{k}</dt>
      <dd className="m-0 mt-0.5">{children}</dd>
    </div>
  );
}

function BigRange({ bear, base, bull, price }: { bear: number; base: number; bull: number; price: number }) {
  const lo = Math.min(bear, price), hi = Math.max(bull, price);
  const pos = (v: number) => `${((v - lo) / (hi - lo || 1)) * 100}%`;
  return (
    <figure className="m-0 mt-6">
      <div className="relative h-12 mx-2" aria-hidden>
        <div className="absolute top-1/2 h-[3px] -translate-y-1/2 rounded-full" style={{ left: pos(bear), right: `calc(100% - ${pos(bull)})`, background: "var(--fs-steel)" }} />
        <div className="absolute top-1/2 w-[2px] h-7 -translate-y-1/2 rounded-full" style={{ left: pos(price), background: "var(--fs-ink)" }} />
        <div className="absolute top-1/2 w-3.5 h-3.5 -translate-x-1/2 -translate-y-1/2 rounded-full" style={{ left: pos(base), background: "var(--fs-ember)", boxShadow: "0 0 0 2px var(--fs-canvas)" }} />
      </div>
      <figcaption className="fs-note flex flex-wrap gap-x-5 gap-y-1 mt-1">
        <span>Line: bear to bull, {money(bear)} to {money(bull)}</span>
        <span>Dot: base case, {money(base)}</span>
        <span>Tick: today&apos;s price, {money(price)}</span>
      </figcaption>
    </figure>
  );
}

/* ── no full model: the screen's own cash-flow model ───────────────────────── */
function ScreenModel({ row, symbol, live = false }: { row?: ResearchRow; symbol: string; live?: boolean }) {
  const d = row?.dcf;
  if (!row || !d) return <p className="fs-prose mt-6">No valuation is available for {symbol}.</p>;
  if (!dcfTrusted(d) || !isNum(d.fair_value)) {
    const why = declineReason(row);
    return (
      <div className="mt-6">
        <p className="m-0 text-[1.0625rem] font-semibold">Not valued: {(why ?? "not enough reliable data").toLowerCase()}.</p>
        {why && REASON_NOTE[why] && <p className="fs-prose mt-2 mb-0">{REASON_NOTE[why]}</p>}
        {impliedNote(d) && <p className="fs-prose mt-2 mb-0">{impliedNote(d)}</p>}
        <p className="fs-note mt-6 mb-0">Rather than print a number it cannot stand behind, the model declines and says why.</p>
      </div>
    );
  }
  const s = d.sensitivity;
  const midR = s ? Math.floor(s.growth_pcts.length / 2) : -1;
  const midC = s ? Math.floor(s.wacc_pcts.length / 2) : -1;
  return (
    <>
      <div className="mt-6 flex flex-wrap items-end gap-x-10 gap-y-4">
        <p className="m-0">
          <span className="block fs-note">Estimated value</span>
          <span className="text-[2rem] font-semibold">{money(d.fair_value!)}</span>
        </p>
        {isNum(d.upside_pct) && (
          <p className="m-0"><span className="block fs-note">Against today&apos;s price</span>
            <span className="text-[1.5rem] font-semibold"><Delta value={d.upside_pct} text={signedPct(d.upside_pct, 0)} /></span></p>
        )}
        {dcfConfidence(d.confidence) && <p className="m-0"><span className="block fs-note">Confidence</span><span className="text-[1.25rem] font-semibold">{dcfConfidence(d.confidence)}</span></p>}
      </div>
      <Block title="The inputs">
        <dl className="m-0 grid grid-cols-2 sm:grid-cols-4 gap-x-6 gap-y-4 text-[0.9375rem]">
          <Fact k="Discount rate">{isNum(d.wacc_pct) ? plainPct(d.wacc_pct) : "–"}</Fact>
          <Fact k="Growth, first years">{isNum(d.stage1_growth_pct) ? `${plainPct(d.stage1_growth_pct)} a year` : "–"}</Fact>
          <Fact k="Long-run growth">{isNum(d.terminal_growth_pct) ? plainPct(d.terminal_growth_pct) : "–"}</Fact>
          <Fact k="Today's price implies">{isNum(d.implied_growth_pct) ? `${plainPct(d.implied_growth_pct)} growth` : "–"}</Fact>
        </dl>
        {d.assumptions && <p className="fs-note mt-4 mb-0">{d.assumptions}</p>}
      </Block>
      {s && (
        <Block title="Estimated value under other assumptions">
          <div className="overflow-x-auto">
            <table className="fs-table" style={{ width: "auto" }}>
              <thead><tr><th scope="col">Growth, then discount rate</th>{s.wacc_pcts.map((w) => <th key={w} scope="col" className="r">{plainPct(w)}</th>)}</tr></thead>
              <tbody>
                {s.growth_pcts.map((g, i) => (
                  <tr key={g}>
                    <th scope="row">{plainPct(g)} growth</th>
                    {s.fair_values[i].map((v, j) => (
                      <td key={j} className="r" style={i === midR && j === midC ? { fontWeight: 700, color: "var(--fs-ink)", background: "var(--fs-wash)" } : undefined}>
                        {isNum(v) ? money(v) : "–"}
                      </td>
                    ))}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <p className="fs-note mt-2 mb-0">The highlighted figure is the base case.</p>
        </Block>
      )}
      {(d.caveats ?? []).length > 0 && (
        <Block title="Notes from the model">
          <ul className="m-0 p-0 list-none grid gap-2">
            {d.caveats!.slice(0, 3).map((c) => <li key={c} className="fs-note">{tidyNote(c)}</li>)}
          </ul>
        </Block>
      )}
      <p className="fs-note mt-8 mb-0">
        No full model yet. Full models cover companies rated strong buy, buy or watch and are rebuilt with each new
        filing, so this is the screen&apos;s own ten-year cash-flow model.
        {live && " Open the Spreadsheet tab to change its assumptions and watch the value move."}
      </p>
    </>
  );
}

/* ── the spreadsheet, read back as values ──────────────────────────────────── */

interface Section {
  title: string;
  notes: string[];
  header: Record<string, Cell> | null;
  rows: { r: number; c: Record<string, Cell> }[];
  grid: boolean;                 // a sensitivity grid: header + row labels are inputs, body is price
}

const SKIP = /^(CASE SELECT|SELECTED CASE|ACTIVE CASE|SENSITIVITY ENGINE|SCENARIO ASSUMPTIONS$)|HELPER/i;
/** Case-selector rows that sit above the first section in Forge-built workbooks. */
const SKIP_ROW = /^(case \(1|case selector|active case$)/i;

/** Mostly capital letters, ignoring anything in parentheses. */
function shouty(text: string, minLetters: number): boolean {
  const letters = text.replace(/\([^)]*\)/g, "").replace(/[^A-Za-z]/g, "");
  if (letters.length < minLetters) return false;
  return letters.replace(/[^A-Z]/g, "").length / letters.length >= 0.75;
}

/** A section title row, as the skill writes them: "VALUATION SUMMARY ($M except
 *  per share)", or a title with a trailing note, "HISTORICAL PERFORMANCE ($M) -
 *  FY ends June 30; ...". Returns the title and the note, if any. */
function titleOf(c: Record<string, Cell>): { title: string; note: string | null } | null {
  const a = c.A;
  if (typeof a !== "string" || a.trim().length < 5) return null;
  if (Object.entries(c).some(([k, v]) => k !== "A" && typeof v === "number")) return null;
  const t = a.trim();
  if (shouty(t, 4)) return { title: t, note: null };
  const m = t.match(/^(.{5,}?)(?:\s+[-–—]\s+|:\s+|;\s+)(.+)$/);
  if (m && shouty(m[1], 6)) return { title: m[1].trim(), note: m[2].trim() };
  return null;
}

/* Shouted spreadsheet titles in sentence case, keeping finance acronyms. */
const ACRONYMS = new Set(["WACC", "EBIT", "EBITA", "EBITDA", "FCF", "CAPM", "LTM", "SEC", "GAAP", "TV", "PV", "NPV", "FY", "Y1", "D&A", "R&D", "SBC", "US"]);
function sentenceCase(t: string): string {
  const lower = t.toLowerCase();
  const out = lower.charAt(0).toUpperCase() + lower.slice(1);
  return out.replace(/[A-Za-z0-9&]+/g, (w) => (ACRONYMS.has(w.toUpperCase()) ? w.toUpperCase() : w)).replace(/\$m\b/g, "$M");
}

function cellsOf(c: Record<string, Cell>): [string, Cell][] {
  return Object.entries(c).filter(([k]) => k !== "A").sort(([a], [b]) => (a.length - b.length) || a.localeCompare(b));
}

function parseSections(rows: { r: number; c: Record<string, Cell> }[]): { title: string; subtitle: string; sections: Section[] } {
  let title = "", subtitle = "";
  const out: Section[] = [];
  let cur: Section | null = null;
  for (const row of rows) {
    const c = row.c;
    const head = titleOf(c);
    if (!title && typeof c.A === "string" && !head && out.length === 0 && !cur) { title = c.A; continue; }
    if (title && !subtitle && typeof c.A === "string" && !head && out.length === 0 && !cur && Object.keys(c).length === 1) { subtitle = c.A; continue; }
    if (head) {
      cur = { title: head.title, notes: head.note ? [head.note] : [], header: null, rows: [], grid: /SENSITIVITY/i.test(head.title) };
      out.push(cur);
      continue;
    }
    if (typeof c.A === "string" && SKIP_ROW.test(c.A.trim())) continue;
    // A sheet with no section titles (Forge's History sheet) is one untitled section.
    if (!cur) { cur = { title: "", notes: [], header: null, rows: [], grid: false }; out.push(cur); }
    // Forge-built sensitivity grids keep their row axis in column C, not A.
    let cells = c;
    if (cur.grid && c.A == null) {
      const [k0, v0] = cellsOf(c)[0] ?? [];
      if (k0) { cells = { ...c, A: v0 }; delete cells[k0]; }
    }
    const rest = cellsOf(cells);
    const onlyA = rest.length === 0;
    if (onlyA && typeof cells.A === "string" && cells.A.length > 40 && !cur.header) { cur.notes.push(cells.A); continue; }
    if (!cur.header && cur.rows.length === 0) {
      const restStrings = rest.length >= 2 && rest.every(([, v]) => typeof v === "string" && v.length <= 40);
      const gridHead = cur.grid && rest.length > 0 && rest.every(([, v]) => typeof v === "number");
      if (restStrings || gridHead) { cur.header = cells; continue; }
    }
    cur.rows.push(cells === c ? row : { ...row, c: cells });
  }
  return { title, subtitle, sections: out.filter((s) => !SKIP.test(s.title) && (s.rows.length > 0)) };
}

const PCT_LABEL = /%|margin|growth|rate\b|rate |wacc|yield|premium|weight|cost of|spread|upside|share of|contribution|risk-free/i;
const NOT_PCT = /factor|period|\(\$|shares|price|beta|value \(|\$m/i;

function fmtNumber(v: number, label: string, colHead: string): string {
  const l = `${label} ${colHead}`.toLowerCase();
  if (/factor/.test(l)) return v.toFixed(3);
  if (/discount period|^\s*period\b/.test(label.toLowerCase())) return v.toFixed(1);
  if (/\bbeta\b/.test(label.toLowerCase())) return v.toFixed(2);
  if (/per share|price|implied price/.test(l) && Math.abs(v) < 1e5) return money(v);
  if ((PCT_LABEL.test(label) || PCT_LABEL.test(colHead)) && !NOT_PCT.test(label) && Math.abs(v) < 5) {
    const p = v * 100;
    const digits = Math.abs(p) < 10 && Math.round(p * 10) !== p * 10 ? 2 : 1;
    return `${p < 0 ? "−" : ""}${Math.abs(p).toFixed(digits).replace(/\.0+$/, "").replace(/(\.\d*?)0+$/, "$1")}%`;
  }
  const a = Math.abs(v);
  const txt = a >= 100 ? Math.round(a).toLocaleString("en-US") : a >= 10 ? a.toFixed(1) : a.toFixed(2);
  return `${v < 0 ? "−" : ""}${txt}`;
}

/** Sensitivity axis values: discount rates and growth as %, betas as numbers. */
function fmtAxis(v: number, varName: string): string {
  const n = varName.toLowerCase();
  if (/beta/.test(n) || Math.abs(v) > 0.5) return v.toFixed(2);
  const p = v * 100;
  const shift = /shift|^d|Δ|delta/.test(n);
  const txt = `${Math.abs(p).toFixed(2).replace(/\.?0+$/, "")}%`;
  return `${p < 0 ? "−" : shift && p > 0 ? "+" : ""}${txt}`;
}

function SheetSection({ s }: { s: Section }) {
  const cols = useMemo(() => {
    const set = new Set<string>();
    [s.header, ...s.rows.map((r) => r.c)].forEach((c) => c && Object.keys(c).forEach((k) => k !== "A" && set.add(k)));
    return [...set].sort((a, b) => (a.length - b.length) || a.localeCompare(b));
  }, [s]);
  const axis = typeof s.header?.A === "string" ? (s.header.A as string) : "";
  const shiftAxes = /shift/i.test(s.title) ? " shift" : "";
  const [rowVar, colVar] = axis.includes("\\") ? axis.split("\\").map((x) => x.trim() + shiftAxes) : ["", ""];
  const heads = (k: string) => {
    const v = s.header?.[k];
    if (v == null) return "";
    if (typeof v === "number") return s.grid ? fmtAxis(v, colVar) : fmtNumber(v, "", "");
    return unbrand(String(v));
  };
  return (
    <section className="mt-8">
      {s.title && <h3 className="fs-h3 m-0">{sentenceCase(unbrand(s.title))}</h3>}
      {s.notes.map((n) => <p key={n} className="fs-note m-0 mt-1">{unbrand(n)}</p>)}
      <div className="overflow-x-auto mt-3 fs-sheet">
        <table className="fs-table">
          {s.header && (
            <thead>
              <tr>
                <th scope="col" className="fs-sticky">{s.grid ? axis.replace("\\", "→").replace(/\s+→\s+/, " (rows) → ") : (typeof s.header.A === "string" ? s.header.A : "")}</th>
                {cols.map((k) => <th key={k} scope="col" className="r">{heads(k)}</th>)}
              </tr>
            </thead>
          )}
          <tbody>
            {s.rows.map((row) => {
              const a = row.c.A;
              const label = typeof a === "string" ? a : "";
              const indent = /^\s{2,}/.test(label);
              const strong = /^(EBIT|Revenue|Unlevered free cash flow|Enterprise value|Equity value|Implied (price|value) per share|WACC|Free cash flow)(\s*\(|\s*$)/i.test(label.trim());
              return (
                <tr key={row.r}>
                  <th scope="row" className="fs-sticky" style={{ paddingLeft: indent ? 14 : 0, color: indent ? "var(--fs-ink-3)" : "var(--fs-ink)", fontWeight: strong ? 600 : 400 }}>
                    {typeof a === "number" ? (s.grid ? fmtAxis(a, rowVar) : fmtNumber(a, "", "")) : unbrand(label.trim())}
                  </th>
                  {cols.map((k) => {
                    const v = row.c[k];
                    const colHead = typeof s.header?.[k] === "string" ? (s.header![k] as string) : "";
                    const text = v == null ? "" : typeof v === "number" ? (s.grid ? money(v) : fmtNumber(v, label, colHead)) : unbrand(String(v));
                    const long = typeof v === "string" && v.length > 24;
                    return (
                      <td key={k} className={long ? "fs-note" : "r"} style={long ? { minWidth: 220, whiteSpace: "normal" } : strong ? { fontWeight: 600 } : undefined}>
                        {text}
                      </td>
                    );
                  })}
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </section>
  );
}

const SHEET_NAME: Record<string, string> = { WACC: "Discount rate (WACC) build", History: "Reported history", Notes: "Method and sources" };
const sheetRank = (n: string) => ({ DCF: 0, History: 1, WACC: 2, Notes: 3 } as Record<string, number>)[n] ?? 2;
/** A notes sheet: every row is a label in A and a sentence in B. The thesis,
 *  assumptions and caveats are already on the Summary tab, so those are dropped. */
const isTextSheet = (rows: { c: Record<string, Cell> }[]) =>
  rows.length > 0 && rows.every((r) => typeof r.c.A === "string" && typeof r.c.B === "string" && Object.keys(r.c).length === 2);

function Spreadsheet({ symbol }: { symbol: string }) {
  const { data, error } = useSWR<SheetPayload>(sheetKey(symbol), fetcher, ONCE);
  if (error) {
    return <p className="fs-prose mt-6">The spreadsheet for this model is not available here yet. The summary shows its results.</p>;
  }
  if (!data) return <p className="fs-note mt-6">Loading the spreadsheet…</p>;
  const sheets = data.sheets
    .filter((sh) => !/calc|helper/i.test(sh.name))
    .sort((a, b) => sheetRank(a.name) - sheetRank(b.name));
  return (
    <>
      <p className="fs-note mt-6 mb-0">
        The model as built, shown as its computed values. Money in millions of dollars unless a row says otherwise.
      </p>
      {sheets.map((sh) => {
        const heading = sh.name !== "DCF" && <h3 className="fs-h2 m-0 mt-12">{SHEET_NAME[sh.name] ?? sh.name}</h3>;
        if (isTextSheet(sh.rows)) {
          return (
            <div key={sh.name}>
              {heading}
              <dl className="m-0 mt-4 grid gap-y-3 text-[0.9375rem]">
                {sh.rows.filter((r) => !/^(thesis|assumption \d|caveat \d)/i.test(String(r.c.A))).map((r) => (
                  <div key={r.r} className="grid gap-x-6 sm:grid-cols-[10rem_1fr]">
                    <dt className="font-semibold">{unbrand(String(r.c.A))}</dt>
                    <dd className="m-0" style={{ color: "var(--fs-ink-2)" }}>{unbrand(String(r.c.B))}</dd>
                  </div>
                ))}
              </dl>
            </div>
          );
        }
        const parsed = parseSections(sh.rows);
        return (
          <div key={sh.name}>
            {heading}
            {parsed.title && sh.name !== "DCF" && <p className="fs-note m-0 mt-1">{unbrand(parsed.title)}</p>}
            {parsed.sections.map((s, i) => <SheetSection key={`${sh.name}-${s.title}-${i}`} s={s} />)}
          </div>
        );
      })}
    </>
  );
}
