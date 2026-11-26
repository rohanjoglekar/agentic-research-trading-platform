"use client";

// Equities dashboard — the stock portfolio. Header with the finding, a KPI
// row, then panels: performance beside allocation, holdings beside sector and
// monthly results, and the decision rules along the bottom.

import { useMemo, useState } from "react";
import PerfChart from "@/components/demo/PerfChart";
import MonthlyBars from "@/components/demo/MonthlyBars";
import { Delta, RankBars, SplitBar } from "@/components/demo/Bits";
import { DashHeader, KpiRow, Panel, Steps, ToggleLink, type Kpi } from "@/components/demo/Dash";
import { performanceSentence } from "@/components/demo/copy";
import {
  alignedChanges, useAlphaAnalytics, useAlphaPortfolio, useLanding, type AlphaPosition,
} from "@/components/demo/data";
import { fixed, isNum, pct, signedPct, signedPts, usdCompact } from "@/components/demo/fmt";
import { sectorName } from "@/components/demo/research";

const TOP_N = 10;

const sectorOf = sectorName;

function last(values: (number | null)[]): number | null {
  for (let i = values.length - 1; i >= 0; i--) if (isNum(values[i])) return values[i] as number;
  return null;
}

export default function EquitiesPage() {
  const { data: an } = useAlphaAnalytics();
  const { data: pf } = useAlphaPortfolio();
  const { data: landing } = useLanding();
  const [showAll, setShowAll] = useState(false);

  // Headline, KPIs and chart all read the SAME aligned series.
  const chart = useMemo(() => alignedChanges(an), [an]);
  const ret = last(chart.strat);
  const bench = last(chart.bench);
  const since = chart.dates[0];

  const stocks = useMemo(
    () => (pf?.positions ?? [])
      .filter((p) => !p.reserved && isNum(p.current_weight))
      .sort((a, b) => (b.current_weight ?? 0) - (a.current_weight ?? 0)),
    [pf],
  );

  const split = useMemo(() => {
    const pos = pf?.positions ?? [];
    if (!pos.length) return [];
    const reserveSym = pf?.reserve?.symbol ?? "BIL";
    const w = (list: AlphaPosition[]) => list.reduce((a, p) => a + (p.current_weight ?? 0), 0);
    const stockW = w(pos.filter((p) => !p.reserved));
    const billW = w(pos.filter((p) => p.reserved && p.symbol === reserveSym));
    const indexW = w(pos.filter((p) => p.reserved && p.symbol !== reserveSym));
    const cashW = Math.max(0, 1 - stockW - billW - indexW);
    const eq = isNum(pf?.equity) ? (pf!.equity as number) : null;
    const amt = (w: number) => (eq != null ? usdCompact(w * eq) : undefined);
    return [
      { name: "Individual stocks", share: stockW, color: "var(--fs-ember)", amount: amt(stockW) },
      { name: "Index funds", share: indexW, color: "var(--fs-blue)", amount: amt(indexW) },
      { name: "Treasury bills", share: billW, color: "var(--fs-aqua)", amount: amt(billW) },
      { name: "Cash", share: cashW, color: "var(--fs-steel)", amount: amt(cashW) },
    ];
  }, [pf]);

  const sectors = useMemo(() => {
    const by = new Map<string, number>();
    stocks.forEach((p) => by.set(sectorOf(p.sector), (by.get(sectorOf(p.sector)) ?? 0) + (p.current_weight ?? 0)));
    const total = [...by.values()].reduce((a, b) => a + b, 0) || 1;
    return [...by.entries()].map(([name, v]) => ({ name, value: (v / total) * 100 }))
      .sort((a, b) => b.value - a.value);
  }, [stocks]);

  const kpis: Kpi[] = [];
  if (isNum(ret)) kpis.push({ label: "Return since start", value: signedPct(ret), note: isNum(bench) ? `S&P 500 ${signedPct(bench)}` : undefined });
  if (isNum(ret) && isNum(bench)) kpis.push({ label: "Against the S&P 500", value: signedPts(ret - bench), tone: ret - bench, });
  if (isNum(pf?.equity)) {
    kpis.push({
      label: "Portfolio value", value: usdCompact(pf!.equity as number),
      note: isNum(pf?.start_capital) ? `Started at ${usdCompact(pf!.start_capital as number)}` : undefined,
    });
  }
  const dd = an?.headline?.max_drawdown_pct;
  if (isNum(dd)) kpis.push({ label: "Largest drop from a peak", value: signedPct(dd) });
  const sharpe = an?.headline?.sharpe;
  if (isNum(sharpe)) kpis.push({ label: "Sharpe ratio", value: fixed(sharpe, 2) });
  const beta = an?.relative?.beta;
  if (isNum(beta)) kpis.push({ label: "Market sensitivity (beta)", value: fixed(beta, 2) });

  const policy = pf?.policy;
  const screened = landing?.equities?.screened;
  const rows = showAll ? stocks : stocks.slice(0, TOP_N);

  return (
    <>
      <DashHeader
        title="Equity trading"
        finding={performanceSentence({ ret, bench, since })}
        lead={`${stocks.length || "About 20"} stocks the research ranks highest, held beside index funds and Treasury bills. Code decides what to buy, how much, and when to sell.`}
        asOf={pf?.as_of ?? an?.as_of}
      />

      <div className="mt-7"><KpiRow items={kpis} /></div>

      <div className="mt-4 grid gap-4 lg:grid-cols-12">
        <Panel
          className="lg:col-span-8"
          id="perf"
          title="Performance against the S&P 500"
        >
          <PerfChart
            height={320}
            dates={chart.dates}
            series={[
              { key: "strat", name: "Equity trading", values: chart.strat, emphasis: true },
              { key: "bench", name: "S&P 500", values: chart.bench },
            ]}
          />
        </Panel>
        <Panel
          className="lg:col-span-4"
          id="alloc"
          title="Where the money is"
        >
          {split.length > 0 && <SplitBar parts={split} label="How the portfolio is divided" stacked total={isNum(pf?.equity) ? usdCompact(pf!.equity as number) : undefined} />}
          {stocks.length > 0 && (
            <dl className="grid grid-cols-3 gap-x-4 m-0 mt-6 pt-5" style={{ borderTop: "1px solid var(--fs-line)" }}>
              {([
                ["Stocks held", String(stocks.length)],
                ["Largest stock", pct((stocks[0].current_weight ?? 0) * 100)],
                ["Sectors", String(sectors.length)],
              ] as const).map(([k, v]) => (
                <div key={k}>
                  <dt className="fs-note">{k}</dt>
                  <dd className="m-0 mt-1 text-[1.25rem] font-semibold">{v}</dd>
                </div>
              ))}
            </dl>
          )}
        </Panel>
      </div>

      <div className="mt-4 grid gap-4 lg:grid-cols-12 items-start">
        <Panel
          className="lg:col-span-8"
          id="holdings"
          title="Holdings"
          action={stocks.length > TOP_N
            ? <ToggleLink open={showAll} onClick={() => setShowAll((v) => !v)} more={`Show all ${stocks.length}`} less={`Show the largest ${TOP_N}`} />
            : undefined}
        >
          <div className="overflow-x-auto">
            <table className="fs-table">
              <thead>
                <tr>
                  <th scope="col">Company</th>
                  <th scope="col" className="hidden md:table-cell">Sector</th>
                  <th scope="col" className="r"><span className="sm:hidden">Share</span><span className="hidden sm:inline">Share of portfolio</span></th>
                  <th scope="col" className="r">Since bought</th>
                  <th scope="col" className="r hidden sm:table-cell">Research score</th>
                </tr>
              </thead>
              <tbody>
                {rows.map((p) => (
                  <tr key={p.symbol}>
                    <th scope="row">
                      <span className="font-medium" style={{ color: "var(--fs-ink)" }}>{p.name || p.symbol}</span>{" "}
                      <span className="fs-note whitespace-nowrap">{p.symbol}</span>
                    </th>
                    <td className="hidden md:table-cell" style={{ color: "var(--fs-ink-2)" }}>{p.sector ? sectorOf(p.sector) : "–"}</td>
                    <td className="r">{pct((p.current_weight ?? 0) * 100)}</td>
                    <td className="r">
                      {isNum(p.unrealized_plpc) ? <Delta value={p.unrealized_plpc} text={signedPct(p.unrealized_plpc)} /> : "–"}
                    </td>
                    <td className="r hidden sm:table-cell">{isNum(p.score) ? Math.round(p.score) : "–"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </Panel>

        <div className="lg:col-span-4 grid gap-4">
          {sectors.length > 1 && (
            <Panel id="sectors" title="Stocks by sector">
              <RankBars rows={sectors.slice(0, 7)} format={(v) => `${Math.round(v)}%`} />
            </Panel>
          )}
          {(an?.monthly?.length ?? 0) > 1 && (
            <Panel id="months" title="Month by month">
              <MonthlyBars rows={an!.monthly!} stratName="Equity trading" height={200} />
            </Panel>
          )}
        </div>
      </div>

      <Panel className="mt-4" id="rules" title="How it decides">
        <Steps steps={[
          ["Screen", `Every weekday it scores ${isNum(screened) ? screened.toLocaleString("en-US") : "about 600"} US companies on value, quality, growth and momentum.`],
          ["Value", "A cash-flow model estimates what each business is worth, and flags its own answer when the inputs are too shaky to trust."],
          ["Build", `It buys the highest-ranked names, at most ${isNum(policy?.max_position_weight) ? Math.round(policy!.max_position_weight! * 100) : 8}% in one stock and ${isNum(policy?.max_sector_weight) ? Math.round(policy!.max_sector_weight! * 100) : 30}% in one sector. A holding keeps its place while it still ranks near the top, for at least ${isNum(policy?.sell_min_hold_days) ? policy!.sell_min_hold_days : 5} days. The rest goes to index funds and Treasury bills.`],
          ["Sell", `It takes part of a winner off the table at +${isNum(policy?.take_profit_pct) ? Math.round(policy!.take_profit_pct! * 100) : 30}%, sells a loser at −${isNum(policy?.stop_loss_pct) ? Math.round(policy!.stop_loss_pct! * 100) : 15}%, and exits when the research no longer supports it.`],
        ]} />
      </Panel>
    </>
  );
}
