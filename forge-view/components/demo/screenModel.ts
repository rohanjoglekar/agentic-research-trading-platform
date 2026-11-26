// The research screen's ten-year cash-flow model, rebuilt in the browser so it
// can be shown as a spreadsheet and edited. Same math as the server's rh_dcf.py:
// owner free cash flow grows at a first-year rate that fades evenly to the
// long-run rate by year ten, each year is discounted mid-year, and a Gordon
// terminal value is added. Value per share = (enterprise value − net debt) ÷ shares.
//
// The public data carries every input except share count and net debt, and it
// rounds the discount rate and growth to 0.1 pt. It also carries ten values
// computed from the exact inputs (the published value and a 3×3 sensitivity
// grid at ±2 pt growth, ±1 pt discount rate). Value is linear in present value
// with slope 1/shares and offset net debt/shares, so for a candidate discount
// rate and growth rate the two unknowns follow from a least-squares line; the
// search keeps the candidate that reproduces all ten values. A company is only
// rebuilt when every published value comes back within a cent.

/** Kept local so this module stays dependency-free (and runnable under Node). */
interface Sensitivity { wacc_pcts: number[]; growth_pcts: number[]; fair_values: (number | null)[][] }
interface DcfIn {
  available?: boolean; fair_value?: number | null; wacc_pct?: number | null;
  stage1_growth_pct?: number | null; terminal_growth_pct?: number | null;
  base_fcf?: number | null; sensitivity?: Sensitivity | null;
}

export const YEARS = 10;
const G_OFFSETS = [-0.02, 0, 0.02];
const W_OFFSETS = [-0.01, 0, 0.01];
const TOLERANCE = 0.011; // published values are rounded to the cent

export interface ScreenInputs {
  fcf0: number;      // starting owner free cash flow, $
  g1: number;        // growth in year one
  tg: number;        // long-run (terminal) growth
  wacc: number;      // discount rate
  netDebt: number;   // $
  shares: number;
}

export interface YearRow { year: number; growth: number; fcf: number; period: number; factor: number; pv: number }

export interface ScreenResult {
  years: YearRow[];
  sumPv: number;
  terminalFcf: number;
  tv: number;
  pvTv: number;
  ev: number;
  equity: number;
  value: number;
  tvShare: number;
}

export function project(i: ScreenInputs): ScreenResult | null {
  if (!(i.wacc > i.tg) || !(i.shares > 0) || !Number.isFinite(i.fcf0)) return null;
  const years: YearRow[] = [];
  let fcf = i.fcf0, sumPv = 0;
  for (let t = 1; t <= YEARS; t++) {
    const growth = i.g1 + (i.tg - i.g1) * (t - 1) / (YEARS - 1);
    fcf *= 1 + growth;
    const period = t - 0.5;
    const factor = 1 / (1 + i.wacc) ** period;
    const pv = fcf * factor;
    sumPv += pv;
    years.push({ year: t, growth, fcf, period, factor, pv });
  }
  const terminalFcf = fcf * (1 + i.tg);
  const tv = terminalFcf / (i.wacc - i.tg);
  const pvTv = tv / (1 + i.wacc) ** (YEARS - 0.5);
  const ev = sumPv + pvTv;
  const equity = ev - i.netDebt;
  return { years, sumPv, terminalFcf, tv, pvTv, ev, equity, value: equity / i.shares, tvShare: ev ? pvTv / ev : 0 };
}

/** Present value of the ten years plus terminal value (no debt, per company). */
function pvTotal(g1: number, w: number, tg: number, f0: number): number {
  let fcf = f0, pv = 0;
  for (let t = 1; t <= YEARS; t++) {
    fcf *= 1 + g1 + (tg - g1) * (t - 1) / (YEARS - 1);
    pv += fcf / (1 + w) ** (t - 0.5);
  }
  return pv + (fcf * (1 + tg)) / (w - tg) / (1 + w) ** (YEARS - 0.5);
}

export interface Rebuilt { inputs: ScreenInputs; published: number; maxError: number }

/** Recover the exact inputs, or null when the published values cannot be
 *  reproduced to the cent (then the page does not offer the spreadsheet). */
export function rebuild(d: DcfIn | null | undefined): Rebuilt | null {
  const s = d?.sensitivity;
  if (!d?.available || !s || s.fair_values?.length !== 3) return null;
  const f0 = d.base_fcf, fv = d.fair_value, wr = d.wacc_pct, gr = d.stage1_growth_pct;
  if (![f0, fv, wr, gr].every((v) => typeof v === "number" && Number.isFinite(v)) || (f0 as number) <= 0) return null;
  const tg = (d.terminal_growth_pct ?? 2.5) / 100;
  const targets: { gi: number; wj: number; v: number }[] = [];
  for (let gi = 0; gi < 3; gi++) for (let wj = 0; wj < 3; wj++) {
    const v = s.fair_values[gi]?.[wj];
    if (typeof v !== "number") return null;
    targets.push({ gi, wj, v });
  }
  targets.push({ gi: 1, wj: 1, v: fv as number });

  const fit = (w0: number, g0: number) => {
    const P = targets.map((t) => pvTotal(g0 + G_OFFSETS[t.gi], Math.max(w0 + W_OFFSETS[t.wj], tg + 0.015), tg, f0 as number));
    const n = P.length;
    let sx = 0, sy = 0, sxx = 0, sxy = 0;
    P.forEach((p, k) => { sx += p; sy += targets[k].v; sxx += p * p; sxy += p * targets[k].v; });
    const den = n * sxx - sx * sx;
    if (!den) return null;
    const a = (n * sxy - sx * sy) / den;      // 1 / shares
    const b = (a * sx - sy) / n;              // net debt / shares
    let err = 0;
    P.forEach((p, k) => { err = Math.max(err, Math.abs(a * p - b - targets[k].v)); });
    return { err, a, b, w0, g0 };
  };

  // Coarse-to-fine search inside the ±0.05 pt rounding of each published rate.
  let best: ReturnType<typeof fit> = null;
  let cw = (wr as number) / 100, cg = (gr as number) / 100, span = 0.0005;
  for (const steps of [20, 10, 10]) {
    const step = span / steps;
    for (let i = -steps; i <= steps; i++) {
      for (let j = -steps; j <= steps; j++) {
        const w0 = cw + i * step;
        if (w0 <= tg) continue;
        const r = fit(w0, cg + j * step);
        if (r && (!best || r.err < best.err)) best = r;
      }
    }
    if (!best) return null;
    cw = best.w0; cg = best.g0; span = step;
  }
  if (!best || best.err > TOLERANCE || !(best.a > 0)) return null;
  return {
    inputs: { fcf0: f0 as number, g1: best.g0, tg, wacc: best.w0, netDebt: best.b / best.a, shares: 1 / best.a },
    published: fv as number,
    maxError: best.err,
  };
}
