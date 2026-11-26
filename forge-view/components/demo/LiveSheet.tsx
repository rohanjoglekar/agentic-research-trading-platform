"use client";

// The screen's ten-year cash-flow model as a spreadsheet you can edit. Three
// assumptions are inputs (blue, the banking convention); every other cell is
// calculated in the browser by screenModel.ts and recalculates as you type or
// drag. With the model's own inputs it reproduces the published value to the cent.

import { useEffect, useMemo, useState, type ReactNode } from "react";
import { Delta, SplitBar } from "./Bits";
import { isNum, signedPct } from "./fmt";
import { project, type Rebuilt, type ScreenInputs } from "./screenModel";

const money = (v: number) => `${v < 0 ? "−" : ""}$${Math.abs(v).toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
/** Dollars shown in millions: whole millions from 100, one decimal below. */
const millions = (v: number) => {
  const m = v / 1e6, a = Math.abs(m);
  const txt = a >= 100 ? Math.round(a).toLocaleString("en-US") : a.toFixed(1);
  return `${m < 0 ? "−" : ""}${txt}`;
};
const pctOf = (v: number, d = 1) => `${v < 0 ? "−" : ""}${Math.abs(v * 100).toFixed(d)}%`;
const two = (v: number) => String(Number(v.toFixed(2)));
const clamp = (v: number, lo: number, hi: number) => Math.min(hi, Math.max(lo, v));

type Key = "g1" | "tg" | "wacc";
/** Inputs in percent, as the controls show them. */
type Pcts = Record<Key, number>;
const toPcts = (i: ScreenInputs): Pcts => ({ g1: i.g1 * 100, tg: i.tg * 100, wacc: i.wacc * 100 });

export function LiveSheet({ rebuilt, price, intro }: { rebuilt: Rebuilt; price: number | null; intro?: ReactNode }) {
  const base = rebuilt.inputs;
  const model = useMemo(() => toPcts(base), [base]);
  const [p, setP] = useState<Pcts>(model);
  useEffect(() => setP(model), [model]);

  const inputs: ScreenInputs = { ...base, g1: p.g1 / 100, tg: p.tg / 100, wacc: p.wacc / 100 };
  const res = project(inputs);
  const changed = (k: Key) => Math.abs(p[k] - model[k]) > 1e-9;
  const anyChanged = changed("g1") || changed("tg") || changed("wacc");
  const set = (k: Key, v: number) => setP((cur) => {
    const next = { ...cur, [k]: v };
    // the perpetuity needs the discount rate above long-run growth
    if (next.wacc < next.tg + 0.5) {
      if (k === "wacc") next.wacc = next.tg + 0.5; else next.tg = next.wacc - 0.5;
    }
    return next;
  });

  if (!res) return null;
  const upside = isNum(price) && price > 0 ? (res.value / price - 1) * 100 : null;
  const gMax = Math.max(40, Math.ceil(model.g1 + 10));

  return (
    <div className="mt-6">
      {intro && <p className="fs-prose m-0 mb-5">{intro}</p>}

      {/* the three assumptions, and what they give */}
      <div className="grid gap-6 lg:grid-cols-[minmax(0,1fr)_17rem] rounded-xl p-5 sm:p-6" style={{ background: "var(--fs-wash)", border: "1px solid var(--fs-line)" }}>
        <div className="grid gap-6 sm:grid-cols-3">
          <Control id="ls-g1" label="Growth in year one" value={p.g1} min={-10} max={gMax}
            hint="Then fades by year ten" onChange={(v) => set("g1", v)} />
          <Control id="ls-tg" label="Long-run growth" value={p.tg} min={0} max={4}
            hint="After year ten, forever" onChange={(v) => set("tg", v)} />
          <Control id="ls-wacc" label="Discount rate" value={p.wacc} min={4} max={20}
            hint="Return investors require" onChange={(v) => set("wacc", v)} />
        </div>
        <div className="lg:pl-6 lg:border-l" style={{ borderColor: "var(--fs-line)" }} aria-live="polite">
          <p className="fs-note m-0">Value per share</p>
          <p className="m-0 mt-1 text-[2.25rem] leading-none font-semibold fs-num">{money(res.value)}</p>
          {isNum(upside) && (
            <p className="m-0 mt-2 text-[0.9375rem]">
              <Delta value={upside} text={signedPct(upside, 0)} />
              <span style={{ color: "var(--fs-ink-2)" }}> against today&apos;s price of {money(price!)}</span>
            </p>
          )}
          {anyChanged ? (
            <p className="fs-note m-0 mt-3">
              The model&apos;s own value is {money(rebuilt.published)}.{" "}
              <button type="button" className="fs-link bg-transparent border-0 p-0 cursor-pointer" onClick={() => setP(model)}>Reset</button>
            </p>
          ) : (
            <p className="fs-note m-0 mt-3">The same as the published value. Move a slider to see it change.</p>
          )}
        </div>
      </div>

      <div className="mt-6">
        <p className="m-0 mb-3 font-semibold">Where the value comes from</p>
        <SplitBar
          label="Share of enterprise value"
          parts={[
            { name: "Years one to ten", share: res.sumPv / res.ev, color: "var(--fs-blue)", amount: `$${millions(res.sumPv)}M` },
            { name: "Beyond year ten", share: res.pvTv / res.ev, color: "var(--fs-steel)", amount: `$${millions(res.pvTv)}M` },
          ]}
        />
      </div>

      <p className="fs-note mt-8 mb-0">
        Blue figures are inputs; everything else is calculated. Money in millions of dollars unless a row says otherwise.
      </p>

      <Sheet title="Inputs">
        <tbody>
          <Row label="Starting free cash flow" v={millions(base.fcf0)} />
          <Row label="Growth in year one" v={pctOf(inputs.g1, 2)} input changed={changed("g1")} />
          <Row label="Long-run growth" v={pctOf(inputs.tg, 2)} input changed={changed("tg")} />
          <Row label="Discount rate" v={pctOf(inputs.wacc, 2)} input changed={changed("wacc")} />
          <Row label="Net debt" v={millions(base.netDebt)} />
          <Row label="Shares outstanding (millions)" v={(base.shares / 1e6).toLocaleString("en-US", { maximumFractionDigits: 1 })} />
        </tbody>
      </Sheet>

      <Sheet title="Ten-year projection">
        <thead>
          <tr>
            <th scope="col" className="fs-sticky">Year</th>
            {res.years.map((y) => <th key={y.year} scope="col" className="r">{y.year}</th>)}
          </tr>
        </thead>
        <tbody>
          <tr><th scope="row" className="fs-sticky">Growth</th>{res.years.map((y) => <td key={y.year} className="r">{pctOf(y.growth)}</td>)}</tr>
          <tr><th scope="row" className="fs-sticky" style={{ fontWeight: 600 }}>Free cash flow</th>{res.years.map((y) => <td key={y.year} className="r" style={{ fontWeight: 600 }}>{millions(y.fcf)}</td>)}</tr>
          <tr><th scope="row" className="fs-sticky">Discount factor (mid-year)</th>{res.years.map((y) => <td key={y.year} className="r">{y.factor.toFixed(3)}</td>)}</tr>
          <tr><th scope="row" className="fs-sticky">Present value</th>{res.years.map((y) => <td key={y.year} className="r">{millions(y.pv)}</td>)}</tr>
        </tbody>
      </Sheet>

      <Sheet title="Valuation">
        <tbody>
          <Row label="Present value of years one to ten" v={millions(res.sumPv)} />
          <Row label="Year-ten cash flow grown one more year" v={millions(res.terminalFcf)} />
          <Row label="Terminal value (that ÷ (discount rate − long-run growth))" v={millions(res.tv)} />
          <Row label="Present value of the terminal value" v={millions(res.pvTv)} />
          <Row label="Enterprise value" v={millions(res.ev)} strong />
          <Row label="Less: net debt" v={millions(base.netDebt)} />
          <Row label="Equity value" v={millions(res.equity)} strong />
          <Row label="Shares outstanding (millions)" v={(base.shares / 1e6).toLocaleString("en-US", { maximumFractionDigits: 1 })} />
          <Row label="Value per share ($)" v={money(res.value)} strong />
          {isNum(price) && <Row label="Today's price ($)" v={money(price)} />}
          {isNum(upside) && <Row label="Upside / (downside)" v={signedPct(upside, 1)} />}
          <Row label="Share of value beyond year ten" v={pctOf(res.tvShare, 0)} />
        </tbody>
      </Sheet>

      <Grid inputs={inputs} />

      <p className="fs-note mt-8 mb-0">
        Share count and net debt are not published on their own; they are solved from the published values. With the model&apos;s
        own inputs this sheet gives {money(rebuilt.published)}, the same as the published value.
      </p>
    </div>
  );
}

/* One assumption: a typed value and a slider that stay in step. */
function Control({ id, label, value, min, max, hint, onChange }: {
  id: string; label: string; value: number; min: number; max: number; hint: string; onChange: (v: number) => void;
}) {
  const [text, setText] = useState(two(value));
  const [editing, setEditing] = useState(false);
  useEffect(() => { if (!editing) setText(two(value)); }, [value, editing]);
  return (
    <div>
      <label htmlFor={id} className="block text-[0.875rem] font-medium">{label}</label>
      <span className="block fs-note text-[0.8125rem]">{hint}</span>
      <div className="mt-2 flex items-center gap-2">
        <input
          id={id} type="text" inputMode="decimal" autoComplete="off" spellCheck={false}
          className="fs-input fs-num w-[5.5rem] text-right" style={{ color: "var(--fs-blue)", fontWeight: 600 }}
          value={text}
          onFocus={() => setEditing(true)}
          onBlur={() => setEditing(false)}
          onChange={(e) => {
            setText(e.target.value);
            const n = parseFloat(e.target.value.replace(/[−–]/g, "-"));
            if (Number.isFinite(n)) onChange(clamp(n, min, max));
          }}
        />
        <span aria-hidden style={{ color: "var(--fs-ink-2)" }}>%</span>
      </div>
      <input
        type="range" className="fs-range mt-3 w-full" min={min} max={max} step="any"
        value={clamp(value, min, max)} aria-label={label} aria-valuetext={`${two(value)}%`}
        onChange={(e) => onChange(Number(e.target.value))}
      />
    </div>
  );
}

function Sheet({ title, children }: { title: string; children: ReactNode }) {
  return (
    <section className="mt-8">
      <h3 className="fs-h3 m-0">{title}</h3>
      <div className="overflow-x-auto mt-3 fs-sheet">
        <table className="fs-table">{children}</table>
      </div>
    </section>
  );
}

function Row({ label, v, input = false, changed = false, strong = false }: { label: string; v: string; input?: boolean; changed?: boolean; strong?: boolean }) {
  return (
    <tr>
      <th scope="row" className="fs-sticky" style={{ fontWeight: strong ? 600 : 400 }}>{label}</th>
      <td className="r" style={{
        color: input ? "var(--fs-blue)" : undefined,
        fontWeight: strong || input ? 600 : undefined,
        background: changed ? "color-mix(in srgb, var(--fs-blue) 10%, transparent)" : undefined,
      }}>
        {v}
      </td>
    </tr>
  );
}

/* Value per share around the current inputs, recalculated live. */
function Grid({ inputs }: { inputs: ScreenInputs }) {
  const gs = [-0.02, -0.01, 0, 0.01, 0.02];
  const ws = [-0.01, -0.005, 0, 0.005, 0.01];
  const sign = (d: number) => (d > 0 ? "+" : d < 0 ? "−" : "");
  return (
    <Sheet title="Value per share under other assumptions">
      <thead>
        <tr>
          <th scope="col" className="fs-sticky">Growth in year one, then discount rate</th>
          {ws.map((dw) => <th key={dw} scope="col" className="r">{pctOf(inputs.wacc + dw, 2)}</th>)}
        </tr>
      </thead>
      <tbody>
        {gs.map((dg) => (
          <tr key={dg}>
            <th scope="row" className="fs-sticky">{pctOf(inputs.g1 + dg, 2)}{dg ? <span className="fs-note"> ({sign(dg)}{Math.abs(dg * 100)} pt)</span> : ""}</th>
            {ws.map((dw) => {
              const r = project({ ...inputs, g1: inputs.g1 + dg, wacc: inputs.wacc + dw });
              const mid = dg === 0 && dw === 0;
              return (
                <td key={dw} className="r" style={mid ? { fontWeight: 700, background: "var(--fs-wash)" } : undefined}>
                  {r && inputs.wacc + dw > inputs.tg ? money(r.value) : "–"}
                </td>
              );
            })}
          </tr>
        ))}
      </tbody>
    </Sheet>
  );
}

