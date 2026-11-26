// Small presentational pieces shared by the three factsheets. No data fetching
// here, and nothing that needs the client runtime.

import type { ReactNode } from "react";
import { useWidth } from "./PerfChart";

/* ── Stat row ───────────────────────────────────────────────────────────────
   A handful of figures in one quiet row, separated by hairlines rather than
   boxed into cards. Label in sentence case, value in the same sans with
   proportional figures, and an optional line that says what the number means. */
export interface StatItem { label: string; value: string; note?: string }

export function StatRow({ items }: { items: StatItem[] }) {
  if (!items.length) return null;
  return (
    <dl className="grid grid-cols-2 lg:grid-cols-4 gap-y-7 m-0">
      {items.map((it, i) => (
        <div
          key={it.label}
          className={`pr-5 ${i % 2 === 1 ? "pl-5 border-l" : ""} ${i > 0 ? "lg:pl-6 lg:border-l" : ""} border-[color:var(--fs-line)]`}
        >
          <dt className="text-[0.875rem] leading-snug text-[color:var(--fs-ink-2)]">{it.label}</dt>
          <dd className="m-0 mt-1.5 text-[1.75rem] leading-none font-semibold tracking-[-0.01em]">{it.value}</dd>
          {it.note && <dd className="m-0 mt-2 fs-note">{it.note}</dd>}
        </div>
      ))}
    </dl>
  );
}

/* ── Split bar ──────────────────────────────────────────────────────────────
   One whole divided into a few parts, read at a glance. An optional total sits
   above the bar; each block prints its amount (or share) inside where it fits,
   and the legend gives name, amount and share. Near-black labels sit on every
   fill (best contrast on all four in both themes). */
export interface SplitPart { name: string; share: number; color: string; amount?: string }

export function SplitBar({ parts, label, stacked = false, total }: { parts: SplitPart[]; label: string; stacked?: boolean; total?: string }) {
  const shown = parts.filter((p) => p.share > 0.001);
  const sum = shown.reduce((a, p) => a + p.share, 0) || 1;
  const pc = (p: SplitPart) => Math.round((p.share / sum) * 100);
  const spoken = shown.map((p) => `${p.name} ${p.amount ? `${p.amount}, ` : ""}${pc(p)}%`).join(", ");
  // A block prints its label only when the label fits in its real width.
  const { ref, width } = useWidth<HTMLDivElement>();
  const usable = Math.max(0, width - 2 * (shown.length - 1));
  const fits = (p: SplitPart, text: string) => width > 0 && (p.share / sum) * usable >= text.length * 7.6 + 16;
  return (
    <div>
      {total && (
        <p className="m-0 mb-3 flex items-baseline gap-2">
          <span className="text-[1.75rem] leading-none font-semibold fs-num">{total}</span>
          <span style={{ color: "var(--fs-ink-2)" }}>in total</span>
        </p>
      )}
      <div ref={ref} className="flex gap-[2px] h-8" role="img" aria-label={`${label}${total ? `, ${total} in total` : ""}: ${spoken}`}>
        {shown.map((p) => (
          <div
            key={p.name}
            title={`${p.name}: ${p.amount ? `${p.amount}, ` : ""}${pc(p)}%`}
            className="h-full rounded-[4px] flex items-center px-2 overflow-hidden min-w-[3px]"
            style={{ flexGrow: p.share, flexBasis: 0, background: p.color }}
          >
            {/* printed only when the block is wide enough to hold it uncropped */}
            {fits(p, p.amount ?? `${pc(p)}%`) && (
              <span className="text-[0.8125rem] font-semibold fs-num whitespace-nowrap" style={{ color: "#0b0f14" }}>{p.amount ?? `${pc(p)}%`}</span>
            )}
          </div>
        ))}
      </div>
      <ul className={`mt-4 grid gap-x-8 gap-y-2.5 list-none p-0 m-0 ${stacked ? "" : "sm:grid-cols-2"}`}>
        {shown.map((p) => (
          <li key={p.name} className="grid grid-cols-[10px_1fr_auto_3rem] items-center gap-x-3">
            <span aria-hidden className="w-[10px] h-[10px] rounded-[2px]" style={{ background: p.color }} />
            <span className="font-medium min-w-0">{p.name}</span>
            <span className="fs-num text-right">{p.amount ?? ""}</span>
            <span className="fs-num text-right" style={{ color: "var(--fs-ink-2)" }}>{pc(p)}%</span>
          </li>
        ))}
      </ul>
    </div>
  );
}

/* ── Ranked bars ────────────────────────────────────────────────────────────
   Parts compared with each other: one row each, largest first, one hue, the
   value at the tip of the bar. */
export function RankBars({ rows, format, labelWidth = "11.5rem" }: {
  rows: { name: string; value: number }[];
  format: (v: number) => string;
  labelWidth?: string;
}) {
  const max = Math.max(...rows.map((r) => r.value), 0) || 1;
  return (
    <ul className="list-none p-0 m-0 grid gap-y-3">
      {rows.map((r) => (
        <li key={r.name} className="grid items-center gap-x-4" style={{ gridTemplateColumns: `minmax(0, ${labelWidth}) 1fr` }}>
          <span className="text-[0.9375rem] leading-snug text-[color:var(--fs-ink-2)]">{r.name}</span>
          <span className="flex items-center gap-3 min-w-0">
            <span
              aria-hidden
              className="h-[10px] rounded-r-[4px] shrink-0"
              style={{ width: `max(2px, calc(min(78%, 100% - 3.5rem) * ${r.value / max}))`, background: "var(--fs-steel)" }}
            />
            <span className="fs-num text-[0.9375rem]">{format(r.value)}</span>
          </span>
        </li>
      ))}
    </ul>
  );
}

/* ── Section ────────────────────────────────────────────────────────────────
   A hairline, then air, then a sentence-case title. No eyebrow labels. */
export function Section({ title, intro, children, id }: { title: string; intro?: ReactNode; children: ReactNode; id?: string }) {
  return (
    <section className="fs-section" aria-labelledby={id}>
      <h2 id={id} className="fs-h2 m-0">{title}</h2>
      {intro && <p className="fs-prose mt-2 mb-0">{intro}</p>}
      <div className="mt-6">{children}</div>
    </section>
  );
}

/** Gain/loss text: an explicit sign plus a colour, never colour alone. */
export function Delta({ value, text }: { value: number; text: string }) {
  const color = Math.abs(value) < 1e-9 ? "var(--fs-ink-2)" : value > 0 ? "var(--fs-up)" : "var(--fs-down)";
  return <span className="fs-num" style={{ color }}>{text}</span>;
}
