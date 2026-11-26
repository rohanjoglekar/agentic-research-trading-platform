"use client";

// Strategy vs benchmark, as percent change since the first shared day.
//
// Form: this is an EMPHASIS chart — one series is the point (ember), the other
// is context (steel). One axis, in percent; no dual scales. Spec follows the
// dataviz rules the platform uses elsewhere: 2px lines, solid hairline grid,
// end dots with a surface ring, a legend plus sparing end labels (values lead),
// a crosshair that finds the date so nobody has to aim at a 2px line, and a
// table twin so every number is reachable without hovering.

import { useEffect, useId, useMemo, useState } from "react";
import { isNum, monthLong, monthShort, parseDay, shortDay, signedPct } from "./fmt";

export interface PerfSeries {
  key: string;
  name: string;
  values: (number | null)[];
  emphasis?: boolean;
}

// A callback ref (not a ref object): the measured element can be swapped — an
// empty placeholder before the data arrives, the real plot after — and the
// observer has to follow it.
export function useWidth<T extends HTMLElement>() {
  const [el, setEl] = useState<T | null>(null);
  const [width, setWidth] = useState(0);
  useEffect(() => {
    if (!el) return;
    const ro = new ResizeObserver(([entry]) => setWidth(Math.round(entry.contentRect.width)));
    ro.observe(el);
    setWidth(Math.round(el.getBoundingClientRect().width));
    return () => ro.disconnect();
  }, [el]);
  return { ref: setEl, width };
}

/** A "nice" tick step (1, 2 or 5 × 10ⁿ) giving roughly `target` intervals.
 *  No 2.5s: an axis reading +2.5% / +7.5% is harder to scan than +2% / +4%. */
export function niceStep(span: number, target = 4): number {
  if (!(span > 0)) return 1;
  const raw = span / target;
  const pow = Math.pow(10, Math.floor(Math.log10(raw)));
  const n = raw / pow;
  const step = n < 1.5 ? 1 : n < 3.5 ? 2 : n < 7.5 ? 5 : 10;
  return step * pow;
}

/** Decimals an axis needs so that no tick label is a rounded lie. */
export function tickDigits(step: number): number {
  return Number.isInteger(step) ? 0 : Number.isInteger(step * 10) ? 1 : 2;
}

export function niceDomain(values: number[], target = 4): { lo: number; hi: number; step: number } {
  const min = Math.min(0, ...values);
  const max = Math.max(0, ...values);
  const step = niceStep(max - min || 1, target);
  const lo = Math.floor(min / step - 1e-9) * step;
  const hi = Math.ceil(max / step + 1e-9) * step;
  return { lo, hi: hi === lo ? lo + step : hi, step };
}

function lastIndex(values: (number | null)[]): number {
  for (let i = values.length - 1; i >= 0; i--) if (isNum(values[i])) return i;
  return -1;
}

function linePath(values: (number | null)[], x: (i: number) => number, y: (v: number) => number): string {
  let d = "";
  let pen = false;
  values.forEach((v, i) => {
    if (!isNum(v)) { pen = false; return; }
    d += `${pen ? "L" : "M"}${x(i).toFixed(1)},${y(v).toFixed(1)}`;
    pen = true;
  });
  return d;
}

const colorOf = (s: PerfSeries) => (s.emphasis ? "var(--fs-ember)" : "var(--fs-steel)");

export default function PerfChart({
  dates, series, height = 340, compact = false,
}: {
  dates: string[];
  series: PerfSeries[];
  height?: number;
  compact?: boolean;
}) {
  const { ref, width } = useWidth<HTMLDivElement>();
  const [active, setActive] = useState<number | null>(null);
  const titleId = useId();
  const n = dates.length;

  const live = useMemo(() => series.filter((s) => lastIndex(s.values) >= 0), [series]);
  const all = useMemo(
    () => live.flatMap((s) => s.values.filter(isNum)),
    [live],
  );

  const showEndLabels = width >= 560;
  const M = { top: 10, right: showEndLabels ? 116 : 10, bottom: 28, left: 44 };
  const plotW = Math.max(width - M.left - M.right, 10);
  const plotH = Math.max(height - M.top - M.bottom, 10);
  const { lo, hi, step } = useMemo(() => niceDomain(all, compact ? 3 : 4), [all, compact]);
  const x = (i: number) => M.left + (n > 1 ? (i / (n - 1)) * plotW : plotW / 2);
  const y = (v: number) => M.top + (1 - (v - lo) / (hi - lo)) * plotH;

  const yTicks = useMemo(() => {
    const out: number[] = [];
    for (let t = lo; t <= hi + 1e-9; t += step) out.push(Number(t.toFixed(6)));
    return out;
  }, [lo, hi, step]);
  const digits = tickDigits(step);

  // Month boundaries; a label is dropped when it would crowd the previous one.
  const xTicks = useMemo(() => {
    const out: { i: number; label: string }[] = [];
    let lastX = -Infinity;
    for (let i = 1; i < n; i++) {
      const a = parseDay(dates[i - 1]);
      const b = parseDay(dates[i]);
      if (!a || !b || a.m === b.m) continue;
      const px = x(i);
      if (px - lastX < 52) continue;
      out.push({ i, label: b.m === 1 ? `${monthShort(1)} ${b.y}` : monthShort(b.m) });
      lastX = px;
    }
    return out;
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [dates, n, plotW]);

  // End labels: keep them at least `gap` apart; a nudged label gets a leader.
  const ends = useMemo(() => {
    const items = live.map((s) => {
      const i = lastIndex(s.values);
      const v = s.values[i] as number;
      return { s, i, v, dotY: y(v), labelY: y(v) };
    }).sort((a, b) => a.dotY - b.dotY);
    const gap = 40;
    for (let k = 1; k < items.length; k++) {
      const short = items[k - 1].labelY + gap - items[k].labelY;
      if (short > 0) {
        items[k - 1].labelY -= short / 2;
        items[k].labelY += short / 2;
      }
    }
    return items;
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [live, lo, hi, plotH]);

  const pick = (clientX: number, el: Element) => {
    const rect = el.getBoundingClientRect();
    const rel = (clientX - rect.left - M.left) / plotW;
    setActive(Math.max(0, Math.min(n - 1, Math.round(rel * (n - 1)))));
  };

  const summary = live
    .map((s) => `${s.name} ${signedPct(s.values[lastIndex(s.values)] as number)}`)
    .join(", ");
  const tipLeft = active == null ? 0 : Math.max(0, Math.min(x(active) + 14, width - 190));

  // Table twin: one row per month end, plus the latest day.
  const rows = useMemo(() => {
    const out: { label: string; idx: number }[] = [];
    for (let i = 0; i < n; i++) {
      const cur = parseDay(dates[i]);
      const nxt = i + 1 < n ? parseDay(dates[i + 1]) : null;
      if (!cur) continue;
      if (!nxt) out.push({ label: `Latest, ${shortDay(dates[i])}`, idx: i });
      else if (nxt.m !== cur.m) out.push({ label: `End of ${monthLong(cur.m)} ${cur.y}`, idx: i });
    }
    return out;
  }, [dates, n]);

  if (n < 2 || live.length === 0) return <div ref={ref} style={{ height }} aria-hidden />;

  return (
    <figure className="fs-chart m-0">
      {/* Legend: always present for two series; a line key mirrors the mark. */}
      <figcaption id={titleId} className="flex flex-wrap gap-x-6 gap-y-1 mb-3 text-[0.875rem] text-[color:var(--fs-ink-2)]">
        {live.map((s) => (
          <span key={s.key} className="inline-flex items-center gap-2">
            <span aria-hidden className="inline-block w-[18px] h-[2px] rounded-full" style={{ background: colorOf(s) }} />
            {s.name}
          </span>
        ))}
        <span className="sr-only">. Percent change since {shortDay(dates[0])}: {summary}.</span>
      </figcaption>

      <div
        ref={ref}
        className="relative"
        style={{ height }}
        tabIndex={0}
        role="group"
        aria-labelledby={titleId}
        aria-describedby={`${titleId}-keys`}
        onFocus={() => setActive((a) => a ?? n - 1)}
        onBlur={() => setActive(null)}
        onKeyDown={(e) => {
          if (e.key === "ArrowLeft") { setActive((a) => Math.max(0, (a ?? n - 1) - 1)); e.preventDefault(); }
          else if (e.key === "ArrowRight") { setActive((a) => Math.min(n - 1, (a ?? n - 1) + 1)); e.preventDefault(); }
          else if (e.key === "Home") { setActive(0); e.preventDefault(); }
          else if (e.key === "End") { setActive(n - 1); e.preventDefault(); }
          else if (e.key === "Escape") setActive(null);
        }}
      >
        <span id={`${titleId}-keys`} className="sr-only">Use the left and right arrow keys to read each day.</span>
        {width > 0 && (
          <svg
            width={width}
            height={height}
            aria-hidden
            onPointerMove={(e) => pick(e.clientX, e.currentTarget)}
            onPointerDown={(e) => pick(e.clientX, e.currentTarget)}
            onPointerLeave={() => setActive(null)}
            style={{ touchAction: "pan-y" }}
          >
            {/* grid + y labels in a left gutter, clear of where the lines start */}
            {yTicks.map((t) => (
              <g key={t}>
                <line
                  x1={M.left} x2={M.left + plotW} y1={y(t)} y2={y(t)}
                  stroke={t === 0 ? "var(--fs-line-strong)" : "var(--fs-line)"} strokeWidth={1}
                />
                <text className="fs-axis" x={M.left - 10} y={y(t)} textAnchor="end" dominantBaseline="central">
                  {t === 0 ? "0%" : signedPct(t, digits)}
                </text>
              </g>
            ))}
            {xTicks.map((t) => (
              <text key={t.i} className="fs-axis" x={x(t.i)} y={height - 8} textAnchor="middle">{t.label}</text>
            ))}

            {/* context first, the point last so it sits on top */}
            {[...live].sort((a, b) => Number(!!a.emphasis) - Number(!!b.emphasis)).map((s) => (
              <path
                key={s.key}
                d={linePath(s.values, x, y)}
                fill="none"
                stroke={colorOf(s)}
                strokeWidth={2}
                strokeLinejoin="round"
                strokeLinecap="round"
              />
            ))}

            {/* end dots + sparing direct labels: the value leads, the name follows */}
            {ends.map(({ s, i, v, dotY, labelY }) => (
              <g key={s.key}>
                <circle cx={x(i)} cy={dotY} r={5} fill={colorOf(s)} stroke="var(--fs-canvas)" strokeWidth={2} />
                {showEndLabels && (
                  <>
                    {Math.abs(labelY - dotY) > 6 && (
                      <line x1={x(i) + 7} y1={dotY} x2={x(i) + 13} y2={labelY - 4} stroke="var(--fs-line-strong)" strokeWidth={1} />
                    )}
                    <text x={x(i) + 16} y={labelY - 1} fill="var(--fs-ink)" style={{ fontSize: 15, fontWeight: 620 }}>
                      {signedPct(v)}
                    </text>
                    <text x={x(i) + 16} y={labelY + 15} fill="var(--fs-ink-3)" style={{ fontSize: 12.5 }}>
                      {s.name}
                    </text>
                  </>
                )}
              </g>
            ))}

            {/* crosshair */}
            {active != null && (
              <g pointerEvents="none">
                <line x1={x(active)} x2={x(active)} y1={M.top} y2={M.top + plotH} stroke="var(--fs-line-strong)" strokeWidth={1} />
                {live.map((s) => isNum(s.values[active]) && (
                  <circle
                    key={s.key} cx={x(active)} cy={y(s.values[active] as number)} r={5}
                    fill={colorOf(s)} stroke="var(--fs-canvas)" strokeWidth={2}
                  />
                ))}
              </g>
            )}
          </svg>
        )}

        {active != null && (
          <div className="fs-tip" style={{ left: tipLeft, top: 4 }} role="status" aria-live="polite">
            <div className="text-[color:var(--fs-ink-3)] mb-1">{shortDay(dates[active])}</div>
            {live.map((s) => (
              <div key={s.key} className="flex items-center gap-2">
                <span aria-hidden className="inline-block w-[14px] h-[2px] rounded-full shrink-0" style={{ background: colorOf(s) }} />
                <span className="fs-num font-semibold text-[color:var(--fs-ink)] min-w-[52px]">
                  {isNum(s.values[active]) ? signedPct(s.values[active] as number) : "no data"}
                </span>
                <span className="text-[color:var(--fs-ink-2)]">{s.name}</span>
              </div>
            ))}
          </div>
        )}
      </div>

      {!compact && (
        <details className="fs-details mt-2">
          <summary>Show these numbers as a table</summary>
          <div className="mt-3 max-w-[520px] overflow-x-auto">
            <table className="fs-table">
              <thead>
                <tr>
                  <th scope="col">Change since {shortDay(dates[0])}</th>
                  {live.map((s) => <th key={s.key} scope="col" className="r">{s.name}</th>)}
                </tr>
              </thead>
              <tbody>
                {rows.map((r) => (
                  <tr key={r.idx}>
                    <th scope="row">{r.label}</th>
                    {live.map((s) => (
                      <td key={s.key} className="r">
                        {isNum(s.values[r.idx]) ? signedPct(s.values[r.idx] as number) : "–"}
                      </td>
                    ))}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </details>
      )}
    </figure>
  );
}
