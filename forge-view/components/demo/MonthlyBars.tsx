"use client";

// Month by month: the strategy beside the benchmark. Grouped columns on ONE
// percent axis growing from a zero baseline. Bars are capped at 24px with a
// 2px surface gap inside each pair, rounded only at the data end. No number on
// every bar — the axis, the per-month tooltip (hover or keyboard focus) and the
// table twin carry the values.

import { useId, useMemo, useState } from "react";
import { isNum, monthLong, monthShort, signedPct } from "./fmt";
import { niceDomain, tickDigits, useWidth } from "./PerfChart";

export interface MonthRow { month: string; strat_pct: number | null; bench_pct: number | null }

/** A column from the baseline to `yv`, rounded (r) at the data end only. */
function column(xl: number, w: number, y0: number, yv: number, r: number): string {
  const up = yv < y0;
  const h = Math.abs(yv - y0);
  const rr = Math.min(r, h, w / 2);
  if (h < 0.5) return "";
  return up
    ? `M${xl},${y0}V${yv + rr}Q${xl},${yv} ${xl + rr},${yv}H${xl + w - rr}Q${xl + w},${yv} ${xl + w},${yv + rr}V${y0}Z`
    : `M${xl},${y0}V${yv - rr}Q${xl},${yv} ${xl + rr},${yv}H${xl + w - rr}Q${xl + w},${yv} ${xl + w},${yv - rr}V${y0}Z`;
}

export default function MonthlyBars({
  rows, stratName, benchName = "S&P 500", height = 240,
}: {
  rows: MonthRow[];
  stratName: string;
  benchName?: string;
  height?: number;
}) {
  const { ref, width } = useWidth<HTMLDivElement>();
  const [active, setActive] = useState<number | null>(null);
  const titleId = useId();
  const data = useMemo(
    () => rows.filter((r) => isNum(r.strat_pct) || isNum(r.bench_pct)),
    [rows],
  );
  const vals = data.flatMap((r) => [r.strat_pct, r.bench_pct].filter(isNum));
  const { lo, hi, step } = niceDomain(vals, 4);
  const M = { top: 10, right: 0, bottom: 28, left: 44 };
  const plotW = Math.max(width - M.left - M.right, 10);
  const plotH = Math.max(height - M.top - M.bottom, 10);
  const band = data.length ? plotW / data.length : plotW;
  const barW = Math.max(6, Math.min(24, band * 0.26));
  const y = (v: number) => M.top + (1 - (v - lo) / (hi - lo)) * plotH;
  const ticks: number[] = [];
  for (let t = lo; t <= hi + 1e-9; t += step) ticks.push(Number(t.toFixed(6)));
  const digits = tickDigits(step);
  const label = (m: string) => {
    const [yy, mm] = m.split("-").map(Number);
    return { short: monthShort(mm), long: `${monthLong(mm)} ${yy}` };
  };

  return (
    <figure className="fs-chart m-0">
      <figcaption id={titleId} className="flex flex-wrap gap-x-6 gap-y-1 mb-3 text-[0.875rem] text-[color:var(--fs-ink-2)]">
        <span className="inline-flex items-center gap-2">
          <span aria-hidden className="inline-block w-[10px] h-[10px] rounded-[2px]" style={{ background: "var(--fs-ember)" }} />
          {stratName}
        </span>
        <span className="inline-flex items-center gap-2">
          <span aria-hidden className="inline-block w-[10px] h-[10px] rounded-[2px]" style={{ background: "var(--fs-steel)" }} />
          {benchName}
        </span>
      </figcaption>

      <div ref={ref} className="relative" style={{ height }}>
        {width > 0 && data.length > 0 && (
          <svg width={width} height={height} role="group" aria-labelledby={titleId} onPointerLeave={() => setActive(null)}>
            {ticks.map((t) => (
              <g key={t} aria-hidden>
                <line x1={M.left} x2={M.left + plotW} y1={y(t)} y2={y(t)}
                  stroke={t === 0 ? "var(--fs-line-strong)" : "var(--fs-line)"} strokeWidth={1} />
                <text className="fs-axis" x={M.left - 10} y={y(t)} textAnchor="end" dominantBaseline="central">
                  {t === 0 ? "0%" : signedPct(t, digits)}
                </text>
              </g>
            ))}
            {data.map((r, i) => {
              const cx = M.left + band * (i + 0.5);
              const l = label(r.month);
              const text = `${l.long}: ${stratName} ${isNum(r.strat_pct) ? signedPct(r.strat_pct) : "no data"}, `
                + `${benchName} ${isNum(r.bench_pct) ? signedPct(r.bench_pct) : "no data"}`;
              return (
                <g
                  key={r.month}
                  tabIndex={0}
                  role="img"
                  aria-label={text}
                  onPointerEnter={() => setActive(i)}
                  onPointerDown={() => setActive(i)}
                  onFocus={() => setActive(i)}
                  onBlur={() => setActive(null)}
                  style={{ outline: "none" }}
                >
                  {/* the whole band is the hit target, far bigger than the bars */}
                  <rect x={cx - band / 2} y={M.top} width={band} height={plotH + M.bottom}
                    fill={active === i ? "var(--fs-wash)" : "transparent"} rx={6} />
                  {isNum(r.strat_pct) && (
                    <path d={column(cx - barW - 1, barW, y(0), y(r.strat_pct), 4)} fill="var(--fs-ember)" />
                  )}
                  {isNum(r.bench_pct) && (
                    <path d={column(cx + 1, barW, y(0), y(r.bench_pct), 4)} fill="var(--fs-steel)" />
                  )}
                  <text className="fs-axis" x={cx} y={height - 8} textAnchor="middle" aria-hidden>{l.short}</text>
                </g>
              );
            })}
          </svg>
        )}
        {active != null && data[active] && (
          <div
            className="fs-tip"
            role="status"
            style={{ top: 4, left: Math.max(0, Math.min(M.left + band * (active + 0.5) - 84, width - 190)) }}
          >
            <div className="text-[color:var(--fs-ink-3)] mb-1">{label(data[active].month).long}</div>
            {([[stratName, data[active].strat_pct, "var(--fs-ember)"], [benchName, data[active].bench_pct, "var(--fs-steel)"]] as const)
              .map(([name, v, color]) => (
                <div key={name} className="flex items-center gap-2">
                  <span aria-hidden className="inline-block w-[10px] h-[10px] rounded-[2px] shrink-0" style={{ background: color }} />
                  <span className="fs-num font-semibold text-[color:var(--fs-ink)] min-w-[52px]">
                    {isNum(v) ? signedPct(v) : "no data"}
                  </span>
                  <span className="text-[color:var(--fs-ink-2)]">{name}</span>
                </div>
              ))}
          </div>
        )}
      </div>

      <details className="fs-details mt-2">
        <summary>Show these numbers as a table</summary>
        <div className="mt-3 max-w-[520px] overflow-x-auto">
          <table className="fs-table">
            <thead>
              <tr>
                <th scope="col">Month</th>
                <th scope="col" className="r">{stratName}</th>
                <th scope="col" className="r">{benchName}</th>
              </tr>
            </thead>
            <tbody>
              {data.map((r) => (
                <tr key={r.month}>
                  <th scope="row">{label(r.month).long}</th>
                  <td className="r">{isNum(r.strat_pct) ? signedPct(r.strat_pct) : "–"}</td>
                  <td className="r">{isNum(r.bench_pct) ? signedPct(r.bench_pct) : "–"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </details>
    </figure>
  );
}
