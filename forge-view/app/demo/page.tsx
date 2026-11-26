"use client";

// Overview: one title, then the four parts of the system, each with a live
// headline, a figure, what it does in plain words, where AI comes in, what is
// on its page, and the way in. Research parts sit on the first row, trading
// parts (with their performance charts) on the second, so each row lines up.

import Link from "next/link";
import { useMemo, type ReactNode } from "react";
import PerfChart from "@/components/demo/PerfChart";
import { performanceSentence } from "@/components/demo/copy";
import {
  alignedChanges, useAlphaAnalytics, useDeepModels, useLanding, useOptionsScan, useTrader, useTraderAnalytics,
  type Analytics,
} from "@/components/demo/data";
import { article, isNum, monthDay } from "@/components/demo/fmt";

function last(values: (number | null)[]): number | null {
  for (let i = values.length - 1; i >= 0; i--) if (isNum(values[i])) return values[i] as number;
  return null;
}

function Part({ title, finding, figure, does, ai, inside, href, cta }: {
  title: string; finding: ReactNode; figure: ReactNode; does: ReactNode; ai: ReactNode; inside: string[]; href: string; cta: string;
}) {
  return (
    <article className="flex flex-col min-w-0">
      <h2 className="fs-h2 m-0">{title}</h2>
      <p className="mt-2 mb-0 text-[1.125rem] leading-snug font-medium" style={{ minHeight: "2.6em", textWrap: "balance" } as React.CSSProperties}>
        {finding ?? ""}
      </p>
      <div className="mt-5">{figure}</div>
      <p className="fs-prose mt-7 mb-0">{does}</p>
      <p className="fs-prose mt-3 mb-0"><span className="font-semibold" style={{ color: "var(--fs-ink)" }}>Where AI comes in: </span>{ai}</p>
      <div className="mt-6">
        <h3 className="fs-h3 m-0">On its page</h3>
        <ul className="mt-2 mb-0 pl-5 grid gap-1.5" style={{ color: "var(--fs-ink-2)" }}>
          {inside.map((t) => <li key={t}>{t}</li>)}
        </ul>
      </div>
      <p className="mt-auto pt-7 mb-0"><Link href={href} className="fs-button">{cta}</Link></p>
    </article>
  );
}

/** Two or three headline numbers for the research parts. */
function Figures({ items }: { items: [string, string, string?][] }) {
  return (
    <dl className="m-0 grid grid-cols-3 gap-x-6">
      {items.map(([label, value, note]) => (
        <div key={label} className="min-w-0">
          <dt className="fs-note">{label}</dt>
          <dd className="m-0 mt-1 text-[2rem] leading-none font-semibold fs-num" style={{ fontStretch: "106%" }}>{value}</dd>
          {note && <dd className="m-0 mt-1.5 fs-note">{note}</dd>}
        </div>
      ))}
    </dl>
  );
}

function Performance({ analytics, seriesName }: { analytics: Analytics | undefined; seriesName: string }) {
  const chart = useMemo(() => alignedChanges(analytics), [analytics]);
  return (
    <PerfChart
      compact
      height={230}
      dates={chart.dates}
      series={[
        { key: "strat", name: seriesName, values: chart.strat, emphasis: true },
        { key: "bench", name: "S&P 500", values: chart.bench },
      ]}
    />
  );
}

const dollars = (v: number) => `$${v.toFixed(2)}`;

export default function OverviewPage() {
  const { data: alpha } = useAlphaAnalytics();
  const { data: traderA } = useTraderAnalytics();
  const { data: trader } = useTrader();
  const { data: landing } = useLanding();
  const { data: deep } = useDeepModels();
  const { data: scan } = useOptionsScan();

  const eq = landing?.equities;
  const graded = isNum(eq?.screened) ? eq!.screened! : null;
  const models = deep?.models?.length ?? null;
  const picks = scan?.picks ?? [];
  const top = picks[0];
  const ratedA = picks.filter((p) => p.rating === "A").length;
  const regime = trader?.regime;
  const mood = regime?.regime ? regime.regime.charAt(0).toUpperCase() + regime.regime.slice(1) : null;

  const alphaChart = useMemo(() => alignedChanges(alpha), [alpha]);
  const traderChart = useMemo(() => alignedChanges(traderA), [traderA]);

  return (
    <>
      <h1 className="fs-display m-0">Agentic trading and research</h1>
      <p className="fs-lead mt-6 mb-0" style={{ maxWidth: "62ch" }}>
        AI agents and rule-based code research {graded ? graded.toLocaleString("en-US") : "about 600"} US stocks every weekday,
        then run two trading strategies on what they find and measure both against the S&P 500. Each part below has its own page.
      </p>

      <div className="fs-section grid gap-x-20 gap-y-20 lg:grid-cols-2">
        <Part
          title="Equity research"
          finding={eq?.top ? <>{eq.top} has the highest grade of the {graded ?? "about 600"} companies graded today.</> : null}
          figure={
            <Figures items={[
              ["Companies graded", graded ? graded.toLocaleString("en-US") : "–", "Every weekday"],
              ["Full valuation models", isNum(models) ? String(models) : "–", "Built from SEC filings"],
            ]} />
          }
          does={<>
            Every weekday each company gets a grade from 0 to 100. The grade blends seven measures, weighted by how much they
            matter: how cheap the stock is next to its industry, how healthy and profitable the business is, how fast it is
            growing, which way the share price is trending, what Wall Street analysts expect, the tone of recent news, and a
            weekly AI read of the business. When a company&apos;s data is thin, its grade is marked down rather than guessed. A
            ten-year cash-flow valuation runs alongside as a second check on price; it is one input, not the ranking.
          </>}
          ai="An AI model reads every company each week and scores the business. For every company a cash-flow model suits, it also builds a full valuation spreadsheet from the company's SEC filings, and a separate program recalculates every figure before it is shown."
          inside={[
            "Every company ranked, with the reasons behind its grade",
            "A page per company: price history, performance, financials and news",
            "Which stocks trade below their estimated value, and why some cannot be valued at all",
            "Full valuation models, including a spreadsheet where you change the assumptions and watch the value move",
          ]}
          href="/demo/research"
          cta="Open equity research"
        />

        <Part
          title="Options research"
          finding={top && isNum(top.strike) && isNum(top.monthly_pct) ? (
            <>Top idea today: sell a {top.symbol} {dollars(top.strike)} put to {top.expiration ? monthDay(top.expiration) : "expiry"} for {top.monthly_pct.toFixed(1)}% a month{top.pop ? `, with ${article(top.pop)} ${top.pop} chance of keeping all of it` : ""}.</>
          ) : null}
          figure={
            <Figures items={[
              ["Rated A", picks.length ? String(ratedA) : "–", picks.length ? `Of today's ${picks.length} trade ideas` : "The top rating"],
              ["Market mood", mood ?? "–", isNum(regime?.vix) ? `Volatility index at ${regime!.vix!.toFixed(1)}` : undefined],
            ]} />
          }
          does={<>
            Each morning it looks for the best stocks to sell put options on. Selling a put pays you now, in return for
            agreeing to buy the stock at a set lower price if it falls below that price before a date. Every possible trade
            is scored out of 100 and rated A to D. What counts most is the chance the option expires unused, so the payment is kept, and how
            far the stock can fall before the trade loses money. The health of the business, how easily the option trades and the
            distance from the next earnings report count too; the size of the payment counts least. Those weights come from
            replaying 2,150 past picks to their expiry dates.
          </>}
          ai="An AI analyst researches each of the 20 best candidates on the web, writes up the risks, and scores from 1 to 10 how happy it would be to own the shares if it had to buy them."
          inside={[
            "The day's best trades: price, expiry, payment and the odds of keeping it",
            "How each rating is reached, and what the rating weighs",
            "The AI analyst's write-up for every candidate",
            "Every idea plotted by how well it pays against how safe it is",
          ]}
          href="/demo/options-research"
          cta="Open options research"
        />

        <Part
          title="Equity trading"
          finding={performanceSentence({ ret: last(alphaChart.strat), bench: last(alphaChart.bench), since: alphaChart.dates[0] })}
          figure={<Performance analytics={alpha} seriesName="Equity trading" />}
          does={<>
            A long-only portfolio built from the equity research, started with $1 million of paper money and traded at live
            prices. It holds up to 22 of the best-graded stocks beside an index-fund core, puts more money behind stronger
            convictions and stocks furthest below their estimated value, and caps any one stock at 8% and any sector at 30%.
            When few stocks look compelling it holds more short-term Treasury bills. It sells half a position after a 30% gain,
            sells out after a 15% loss, and exits when a stock&apos;s grade falls below 45.
          </>}
          ai="Each week an AI reviews the results against the S&P 500 and adjusts the settings above, only within fixed limits. It never picks the stocks."
          inside={[
            "Performance against the S&P 500, day by day and month by month",
            "Every holding and how much of the portfolio it takes",
            "The split by sector and how concentrated the portfolio is",
            "The buying and selling rules",
          ]}
          href="/demo/equities"
          cta="Open equity trading"
        />

        <Part
          title="Options trading"
          finding={performanceSentence({ ret: last(traderChart.strat), bench: last(traderChart.bench), since: traderChart.dates[0] })}
          figure={<Performance analytics={traderA} seriesName="Options trading" />}
          does={<>
            Trades the options research three times a day on a $100,000 paper account at live prices. Rules decide which puts
            to sell, and a cash reserve grows when markets get nervous. It takes its profit once 80% of a payment is earned. If a
            stock falls and the account has to buy the shares, it sells call options on them for extra income until they are sold.
          </>}
          ai="When an open trade moves against it, an AI agent researches the company and decides whether to hold, close early or roll to a later date. Hard limits still apply that it cannot override: once a loss reaches four times the payment the rules take over, and it may only roll a trade when that brings in more money."
          inside={[
            "Performance against the S&P 500",
            "Open positions, grouped by expiry date",
            "Recently closed trades and what they earned",
            "Shares owned after being assigned, and the rules",
          ]}
          href="/demo/options"
          cta="Open options trading"
        />
      </div>
    </>
  );
}
