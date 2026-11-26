// Building blocks for the three dashboards: the page header, the KPI row and
// the panel every block of content sits in. Presentational only.

import type { ReactNode } from "react";
import { updatedAt } from "./fmt";

/* ── Header ─────────────────────────────────────────────────────────────────
   Title and freshness on one line, then the finding sentence written from
   live data, then one short line on what the strategy is. */
export function DashHeader({
  title, finding, lead, asOf, source = "Paper account on live markets",
}: {
  title: string;
  finding: string | null;
  lead?: ReactNode;
  asOf?: string | null;
  source?: string;
}) {
  const stamp = updatedAt(asOf);
  return (
    <header>
      <div className="flex flex-wrap items-baseline justify-between gap-x-8 gap-y-1">
        <h1 className="fs-dash-title m-0">{title}</h1>
        <p className="fs-note m-0">{source}{stamp ? `, updated ${stamp}` : ""}</p>
      </div>
      {/* reserves its line height so the page does not jump when data lands */}
      <p className="fs-finding mt-3 mb-0" style={{ minHeight: "1.35em" }} aria-live="polite">
        {finding ?? ""}
      </p>
      {lead && <p className="fs-prose mt-2 mb-0">{lead}</p>}
    </header>
  );
}

/* ── KPI row ────────────────────────────────────────────────────────────────
   Each figure in its own small panel: sentence-case label, the value in the
   same sans with proportional figures, and an optional line that says what
   it means. `tone` colours a signed value (with the sign always printed). */
export interface Kpi { label: string; value: string; note?: string; tone?: number }

export function KpiRow({ items }: { items: Kpi[] }) {
  if (!items.length) return null;
  const cols = items.length >= 6 ? "xl:grid-cols-6" : items.length === 5 ? "xl:grid-cols-5" : "xl:grid-cols-4";
  return (
    <dl className={`grid grid-cols-2 md:grid-cols-3 ${cols} gap-3 sm:gap-4 m-0`}>
      {items.map((k) => {
        const color = k.tone == null || Math.abs(k.tone) < 1e-9
          ? "var(--fs-ink)"
          : k.tone > 0 ? "var(--fs-up)" : "var(--fs-down)";
        return (
          <div key={k.label} className="fs-panel fs-kpi">
            <dt className="text-[0.8125rem] leading-snug" style={{ color: "var(--fs-ink-2)" }}>{k.label}</dt>
            <dd className="fs-kpi-value m-0 mt-2" style={{ color }}>{k.value}</dd>
            {k.note && <dd className="fs-note m-0 mt-1.5">{k.note}</dd>}
          </div>
        );
      })}
    </dl>
  );
}

/* ── Panel ──────────────────────────────────────────────────────────────────
   A titled block of content. `action` sits at the right of the title row
   (a toggle, a count); `sub` is one short line under the title. */
export function Panel({
  title, sub, action, children, className = "", id,
}: {
  title: string;
  sub?: ReactNode;
  action?: ReactNode;
  children: ReactNode;
  className?: string;
  id?: string;
}) {
  return (
    <section className={`fs-panel ${className}`} aria-labelledby={id}>
      <div className="flex items-start justify-between gap-x-6">
        <div className="min-w-0 flex-1">
          <h2 id={id} className="fs-h3 m-0">{title}</h2>
          {sub && <p className="fs-note mt-1 mb-0">{sub}</p>}
        </div>
        {action && <div className="shrink-0 pt-0.5">{action}</div>}
      </div>
      <div className="mt-5">{children}</div>
    </section>
  );
}

/** A quiet text button for "show all / show fewer". */
export function ToggleLink({ open, onClick, more, less }: { open: boolean; onClick: () => void; more: string; less: string }) {
  return (
    <button
      type="button"
      onClick={onClick}
      aria-expanded={open}
      className="fs-link bg-transparent border-0 p-0 cursor-pointer text-[0.875rem]"
    >
      {open ? less : more}
    </button>
  );
}

/** Columns as a numbered rule list, used for "How it decides". */
export function Steps({ steps }: { steps: [string, string][] }) {
  return (
    <ol className="list-none p-0 m-0 grid gap-x-10 gap-y-6 sm:grid-cols-2 xl:grid-cols-4">
      {steps.map(([name, text], i) => (
        <li key={name} className="grid grid-cols-[1.75rem_1fr] gap-x-2">
          <span aria-hidden className="text-[1.25rem] leading-none font-semibold" style={{ color: "var(--fs-ink-3)", fontStretch: "112%" }}>{i + 1}</span>
          <span>
            <span className="fs-h3 block">{name}</span>
            <span className="block mt-1 text-[0.9375rem]" style={{ color: "var(--fs-ink-2)" }}>{text}</span>
          </span>
        </li>
      ))}
    </ol>
  );
}
