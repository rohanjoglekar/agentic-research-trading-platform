"use client";

// Options dashboard — the income book. Header with the finding, a KPI row,
// then panels: performance beside the open book, open positions beside recent
// closes, monthly results beside assigned shares, and the hard rules.
//
// Only broker-verified numbers appear: the account value, live positions, and
// orders that actually filled. The platform's lifetime "premium collected" and
// "win rate" are left out on purpose (see components/demo/data.ts).

import { Fragment, useMemo, useState } from "react";
import PerfChart from "@/components/demo/PerfChart";
import MonthlyBars from "@/components/demo/MonthlyBars";
import { Delta } from "@/components/demo/Bits";
import { DashHeader, KpiRow, Panel, ToggleLink, type Kpi } from "@/components/demo/Dash";
import { performanceSentence } from "@/components/demo/copy";
import { alignedChanges, num, useTrader, useTraderAnalytics } from "@/components/demo/data";
import { daysUntil, isNum, monthDay, signedPct, signedPts, signedUsd, usd, usdCompact } from "@/components/demo/fmt";
// A replay of the put-selling rules on real traded option prices, one setting
// changed at a time. Research output, written by
// stock-options-ai/backtests/csp_universe/export_demo.py; not live data.
import replay from "@/components/demo/optionsReplay.json";

const TOP_N = 10;
const monthYear = (iso: string) =>
  new Date(`${iso}T12:00:00Z`).toLocaleDateString("en-US", { month: "long", year: "numeric", timeZone: "UTC" });

function last(values: (number | null)[]): number | null {
  for (let i = values.length - 1; i >= 0; i--) if (isNum(values[i])) return values[i] as number;
  return null;
}

/** "AAL261127P00013000" → ticker, ISO expiry, put/call, strike. */
function parseContract(symbol: string) {
  const m = /^(.+?)(\d{2})(\d{2})(\d{2})([CP])(\d{8})$/.exec((symbol ?? "").toUpperCase());
  if (!m) return null;
  const strike = parseInt(m[6], 10) / 1000;
  return {
    ticker: m[1],
    expiry: `20${m[2]}-${m[3]}-${m[4]}`,
    kind: m[5] === "P" ? ("put" as const) : ("call" as const),
    strikeText: `$${Number.isInteger(strike) ? strike : strike.toFixed(2)}`,
  };
}

export default function OptionsPage() {
  const { data: an } = useTraderAnalytics();
  const { data: tr } = useTrader();
  const [showAll, setShowAll] = useState(false);

  const chart = useMemo(() => alignedChanges(an), [an]);
  const ret = last(chart.strat);
  const bench = last(chart.bench);
  const since = chart.dates[0];

  const contracts = useMemo(() => (tr?.positions ?? [])
    .filter((p) => p.asset_class === "us_option")
    .map((p) => {
      const c = parseContract(p.symbol);
      const qty = Math.abs(num(p.qty) ?? 0);
      const entry = num(p.avg_entry_price);
      if (!c || !qty) return null;
      return {
        ...c, qty, id: p.symbol,
        collected: entry != null ? entry * 100 * qty : null,
        result: num(p.unrealized_pl),
        days: daysUntil(c.expiry),
      };
    })
    .filter((x): x is NonNullable<typeof x> => x != null)
    .sort((a, b) => a.expiry.localeCompare(b.expiry) || a.ticker.localeCompare(b.ticker)),
  [tr]);

  const shares = useMemo(() => (tr?.positions ?? [])
    .filter((p) => p.asset_class !== "us_option")
    .map((p) => ({ ticker: p.symbol, qty: num(p.qty) ?? 0, value: num(p.market_value), result: num(p.unrealized_pl) }))
    .filter((p) => p.qty > 0)
    .sort((a, b) => (b.value ?? 0) - (a.value ?? 0)),
  [tr]);

  // Only closes that FILLED, scored on the fill price — not on an estimate.
  const closed = useMemo(() => (tr?.recent_closes ?? [])
    .map((c) => {
      const entry = num(c.entry_premium);
      const fill = num(c.filled_avg_price);
      if (c.status !== "filled" || entry == null || fill == null || entry <= 0 || !c.ticker) return null;
      return {
        ticker: c.ticker, kind: c.strategy === "CC" ? "call" : "put", strike: c.strike,
        when: c.filled_at || c.submitted_at || "",
        kept: ((entry - fill) / entry) * 100,
        usd: (entry - fill) * 100 * (c.qty ?? 1),
        id: `${c.ticker}-${c.strike}-${c.expiration}-${c.filled_at}`,
      };
    })
    .filter((x): x is NonNullable<typeof x> => x != null)
    .sort((a, b) => b.when.localeCompare(a.when))
    .slice(0, 6),
  [tr]);

  // The open book at a glance: totals from the broker's own position records.
  const book = useMemo(() => {
    const collected = contracts.reduce((a, c) => a + (c.collected ?? 0), 0);
    const result = contracts.reduce((a, c) => a + (c.result ?? 0), 0);
    const sharesValue = shares.reduce((a, s) => a + (s.value ?? 0), 0);
    const byDate = new Map<string, number>();
    contracts.forEach((c) => byDate.set(c.expiry, (byDate.get(c.expiry) ?? 0) + c.qty));
    const dates = [...byDate.entries()].sort((a, b) => a[0].localeCompare(b[0]));
    const head = dates.slice(0, 5);
    const tail = dates.slice(5).reduce((a, [, n]) => a + n, 0);
    return { collected, result, sharesValue, expiries: head, later: tail };
  }, [contracts, shares]);

  const puts = contracts.filter((c) => c.kind === "put").reduce((a, c) => a + c.qty, 0);
  const calls = contracts.filter((c) => c.kind === "call").reduce((a, c) => a + c.qty, 0);
  const equity = num(tr?.equity);
  const plStart = num(tr?.pl_vs_start);

  const kpis: Kpi[] = [];
  if (isNum(ret)) kpis.push({ label: "Return since start", value: signedPct(ret), note: isNum(bench) ? `S&P 500 ${signedPct(bench)}` : undefined });
  if (isNum(ret) && isNum(bench)) kpis.push({ label: "Against the S&P 500", value: signedPts(ret - bench), tone: ret - bench, });
  if (equity != null) {
    kpis.push({ label: "Account value", value: usdCompact(equity), note: plStart != null ? `Started at ${usdCompact(equity - plStart)}` : undefined });
  }
  const dd = an?.headline?.max_drawdown_pct;
  if (isNum(dd)) kpis.push({ label: "Largest drop from a peak", value: signedPct(dd) });
  if (contracts.length) kpis.push({ label: "Contracts open", value: String(puts + calls), note: `${puts} ${puts === 1 ? "put" : "puts"} and ${calls} ${calls === 1 ? "call" : "calls"} sold` });
  const vix = tr?.regime?.vix;
  if (isNum(vix)) kpis.push({ label: "Market volatility (VIX)", value: vix.toFixed(1) });

  // The rules, in the trader's own numbers. Where the platform reports a
  // setting it is read live; the volatility levels, the concentration limits
  // and the earnings window are fixed settings (stock-options-ai/config/settings.py).
  const g = tr?.guardrails;
  const floor = tr?.reserve?.floor_pct;
  const rules: string[] = [];
  if (isNum(g?.close_at_profit_pct)) rules.push(`Buys a trade back once it has earned ${Math.round(g!.close_at_profit_pct! * 100)}% of what it was paid.`);
  if (isNum(g?.close_at_loss_multiplier)) {
    rules.push(`Cuts a losing trade at ${g!.close_at_loss_multiplier}× the amount collected${
      isNum(g?.kill_switch_loss_mult) ? `, and at ${g!.kill_switch_loss_mult}× at the latest` : ""}, unless the stock is one it is prepared to own.`);
  }
  // The floor is measured on FREE cash: cash less what the open puts would
  // cost if assigned (wheel_runner.committed_put_collateral, 2026-11-20).
  if (isNum(floor)) rules.push(`Opens nothing new unless ${Math.round(floor * 100)}% of the account stays in cash that no open trade has a claim on.`);
  rules.push("Halves trade size when the volatility index is above 25, and opens nothing new above 35.");
  rules.push("Normally limits one stock to 10% of the account and one sector to 30%, and skips companies reporting earnings within 14 days.");
  if (isNum(g?.max_scan_age_hours)) rules.push(`Refuses to trade if its market scan is more than ${Math.round(g!.max_scan_age_hours!)} hours old.`);

  // What the open puts commit it to, against the cash it holds.
  const owes = num(tr?.liabilities?.put_assignment);
  const cash = num(tr?.cash);

  const rows = showAll ? contracts : contracts.slice(0, TOP_N);
  const maxExp = Math.max(1, ...book.expiries.map(([, n]) => n), book.later);

  return (
    <>
      <DashHeader
        title="Options trading"
        finding={performanceSentence({ ret, bench, since })}
        lead="An income strategy. It is paid up front to sell put options on stocks from the same research, buys the shares if they fall to the agreed price, then sells call options against them."
        asOf={an?.as_of}
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
              { key: "strat", name: "Options trading", values: chart.strat, emphasis: true },
              { key: "bench", name: "S&P 500", values: chart.bench },
            ]}
          />
        </Panel>

        <Panel className="lg:col-span-4" id="book" title="The open book">
          <dl className="grid grid-cols-2 gap-x-5 gap-y-5 m-0">
            <div>
              <dt className="text-[0.8125rem]" style={{ color: "var(--fs-ink-2)" }}>Collected on open contracts</dt>
              <dd className="m-0 mt-1 text-[1.25rem] font-semibold">{usd(book.collected)}</dd>
            </div>
            <div>
              <dt className="text-[0.8125rem]" style={{ color: "var(--fs-ink-2)" }}>Result so far</dt>
              <dd className="m-0 mt-1 text-[1.25rem] font-semibold"><Delta value={book.result} text={signedUsd(book.result)} /></dd>
            </div>
            {owes != null && owes > 0 && (
              <div>
                <dt className="text-[0.8125rem]" style={{ color: "var(--fs-ink-2)" }}>To buy if every put is assigned</dt>
                <dd className="m-0 mt-1 text-[1.25rem] font-semibold">{usd(owes)}</dd>
              </div>
            )}
            {cash != null && (
              <div>
                <dt className="text-[0.8125rem]" style={{ color: "var(--fs-ink-2)" }}>Cash on hand</dt>
                <dd className="m-0 mt-1 text-[1.25rem] font-semibold">{usd(cash)}</dd>
              </div>
            )}
            <div className="col-span-2">
              <dt className="text-[0.8125rem]" style={{ color: "var(--fs-ink-2)" }}>Shares it ended up owning</dt>
              <dd className="m-0 mt-1 text-[1.25rem] font-semibold">
                {usd(book.sharesValue)}{" "}
                <span className="fs-note font-normal">in {shares.length} {shares.length === 1 ? "company" : "companies"}</span>
              </dd>
            </div>
          </dl>
          {book.expiries.length > 0 && (
            <div className="mt-6">
              <h3 className="text-[0.8125rem] font-normal m-0 mb-3" style={{ color: "var(--fs-ink-2)" }}>Contracts by expiry date</h3>
              <ul className="list-none p-0 m-0 grid gap-y-2.5">
                {[...book.expiries.map(([d, n]) => [monthDay(d), n] as const), ...(book.later ? [["Later", book.later] as const] : [])]
                  .map(([label, n]) => (
                    <li key={label} className="grid grid-cols-[4rem_1fr] items-center gap-x-3">
                      <span className="text-[0.9375rem]" style={{ color: "var(--fs-ink-2)" }}>{label}</span>
                      <span className="flex items-center gap-3 min-w-0">
                        <span aria-hidden className="h-[10px] rounded-r-[4px] shrink-0" style={{ width: `${Math.max((n / maxExp) * 75, 2)}%`, background: "var(--fs-steel)" }} />
                        <span className="fs-num text-[0.9375rem]">{n}</span>
                      </span>
                    </li>
                  ))}
              </ul>
            </div>
          )}
        </Panel>
      </div>

      <div className="mt-4 grid gap-4 lg:grid-cols-12 items-start">
        <Panel
          className="lg:col-span-8"
          id="open"
          title="Open positions"
          action={contracts.length > TOP_N
            ? <ToggleLink open={showAll} onClick={() => setShowAll((v) => !v)} more={`Show all ${contracts.length}`} less={`Show the next ${TOP_N}`} />
            : undefined}
        >
          <div className="overflow-x-auto">
            <table className="fs-table">
              <thead>
                <tr>
                  <th scope="col">Stock</th>
                  <th scope="col">Promise</th>
                  <th scope="col">Expires</th>
                  <th scope="col" className="r hidden sm:table-cell">Collected</th>
                  <th scope="col" className="r">Result so far</th>
                </tr>
              </thead>
              <tbody>
                {rows.map((c) => (
                  <tr key={c.id}>
                    <th scope="row">
                      <span className="font-medium" style={{ color: "var(--fs-ink)" }}>{c.ticker}</span>
                      {c.qty > 1 && <span className="fs-note"> ×{c.qty}</span>}
                    </th>
                    <td style={{ color: "var(--fs-ink-2)" }}>{c.kind === "put" ? "Buy" : "Sell"} at {c.strikeText}</td>
                    <td style={{ color: "var(--fs-ink-2)" }}>
                      {monthDay(c.expiry)}
                      {isNum(c.days) && c.days >= 0 && (
                        <span className="fs-note hidden sm:inline"> {c.days === 0 ? "today" : c.days === 1 ? "in 1 day" : `in ${c.days} days`}</span>
                      )}
                    </td>
                    <td className="r hidden sm:table-cell">{c.collected != null ? usd(c.collected) : "–"}</td>
                    <td className="r">{c.result != null ? <Delta value={c.result} text={signedUsd(c.result)} /> : "–"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </Panel>

        <Panel className="lg:col-span-4" id="closed" title="Recently closed">
          {closed.length ? (
            <table className="fs-table">
              <thead>
                <tr>
                  <th scope="col">Trade</th>
                  <th scope="col" className="r">Kept</th>
                </tr>
              </thead>
              <tbody>
                {closed.map((c) => (
                  <tr key={c.id}>
                    <th scope="row">
                      <span className="font-medium" style={{ color: "var(--fs-ink)" }}>{c.ticker}</span>{" "}
                      <span className="fs-note">{c.kind}{isNum(c.strike) ? ` at $${c.strike}` : ""}, {monthDay(c.when)}</span>
                    </th>
                    <td className="r">
                      <Delta value={c.kept} text={signedPct(c.kept, 0)} />
                      <span className="block fs-note">{signedUsd(c.usd)}</span>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          ) : <p className="fs-note m-0">No filled closes in the latest records.</p>}
        </Panel>
      </div>

      <div className="mt-4 grid gap-4 lg:grid-cols-12 items-start">
        {(an?.monthly?.length ?? 0) > 1 && (
          <Panel className="lg:col-span-8" id="months" title="Month by month">
            <MonthlyBars rows={an!.monthly!} stratName="Options trading" height={220} />
          </Panel>
        )}
        {shares.length > 0 && (
          <Panel className="lg:col-span-4" id="shares" title="Shares it ended up owning">
            <table className="fs-table">
              <thead>
                <tr>
                  <th scope="col">Stock</th>
                  <th scope="col" className="r">Worth now</th>
                  <th scope="col" className="r">Result</th>
                </tr>
              </thead>
              <tbody>
                {shares.map((p) => (
                  <tr key={p.ticker}>
                    <th scope="row">
                      <span className="font-medium" style={{ color: "var(--fs-ink)" }}>{p.ticker}</span>{" "}
                      <span className="fs-note">{p.qty.toLocaleString("en-US")}</span>
                    </th>
                    <td className="r">{p.value != null ? usd(p.value) : "–"}</td>
                    <td className="r">{p.result != null ? <Delta value={p.result} text={signedUsd(p.result)} /> : "–"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </Panel>
        )}
      </div>

      {rules.length > 0 && (
        <Panel className="mt-4" id="rules" title="The rules it cannot break">
          <ul className="list-none p-0 m-0 grid gap-x-10 gap-y-4 md:grid-cols-2 xl:grid-cols-3">
            {rules.map((r) => (
              <li key={r} className="pl-4 border-l-2 text-[0.9375rem]" style={{ borderColor: "var(--fs-line-strong)", color: "var(--fs-ink-2)" }}>{r}</li>
            ))}
          </ul>
        </Panel>
      )}

      <Panel
        className="mt-4"
        id="replay"
        title="The rules, replayed on real prices"
        sub={`A $${replay.account.toLocaleString("en-US")} account run through each setting on traded option prices, ${monthYear(replay.from)} to ${monthYear(replay.to)}, after trading costs. The S&P 500 made ${replay.spy_cagr}% a year over the same stretch.`}
      >
        <div className="overflow-x-auto">
          <table className="fs-table">
            <thead>
              <tr>
                <th scope="col">Setting</th>
                <th scope="col" className="r">A year</th>
                <th scope="col" className="r">Largest drop</th>
                <th scope="col" className="r hidden sm:table-cell">Trades</th>
                <th scope="col" className="r hidden sm:table-cell">Won</th>
              </tr>
            </thead>
            <tbody>
              {replay.groups.map((g) => (
                <Fragment key={g.title}>
                  <tr>
                    <th scope="colgroup" colSpan={5} className="font-semibold" style={{ color: "var(--fs-ink)", paddingTop: 18 }}>{g.title}</th>
                  </tr>
                  {g.rows.map((r) => (
                    <tr key={`${g.title}-${r.name}`}>
                      <th scope="row">
                        {r.name}
                        {r.live && <span className="fs-tag ml-2">In use</span>}
                      </th>
                      <td className="r"><Delta value={r.cagr} text={signedPct(r.cagr)} /></td>
                      <td className="r">{signedPct(r.mdd)}</td>
                      <td className="r hidden sm:table-cell">{r.trades.toLocaleString("en-US")}</td>
                      <td className="r hidden sm:table-cell">{r.win}%</td>
                    </tr>
                  ))}
                </Fragment>
              ))}
            </tbody>
          </table>
        </div>
      </Panel>
    </>
  );
}
