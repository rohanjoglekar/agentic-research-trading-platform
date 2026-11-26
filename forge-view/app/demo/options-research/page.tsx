"use client";

// Options research: the morning scan that feeds the options trader. Each idea
// is a cash-secured put (get paid now, agree to buy the stock lower), scored
// out of 100 and rated A to D, then researched by an AI analyst. Shows the
// day's ideas, what the rating weighs in today's market, how pay and safety
// trade off, and the analyst's write-up. Research output only: nothing personal.

import { Fragment, useMemo, useState } from "react";
import { DashHeader, KpiRow, Panel, type Kpi } from "@/components/demo/Dash";
import { RankBars } from "@/components/demo/Bits";
import { StockDrawerProvider, useStock } from "@/components/demo/StockDrawer";
import { useWidth } from "@/components/demo/PerfChart";
import { useOptionsScan, useTrader, type ScanPick, type ScanStrike } from "@/components/demo/data";
import { article, daysUntil, isNum, monthDay, unbrand } from "@/components/demo/fmt";

/* What the rating weighs, by market mood. Same numbers as the scanner's
   settings (put-selling weights, reset in June after replaying 2,150 past picks
   to expiry: the chance of keeping the payment and the cushion were what
   avoided losses, so they lead; the size of the payment barely counts). */
const WEIGHTS: Record<string, [string, number][]> = {
  calm: [["Chance the option expires unused", 35], ["How far the stock can fall first", 20], ["Health of the business", 18], ["How easily the option trades", 12], ["Distance from the next earnings report", 10], ["Size of the payment", 3], ["How pricey options are right now", 2]],
  caution: [["Chance the option expires unused", 40], ["How far the stock can fall first", 20], ["Health of the business", 13], ["How easily the option trades", 12], ["Distance from the next earnings report", 10], ["Size of the payment", 3], ["How pricey options are right now", 2]],
  paused: [["Chance the option expires unused", 44], ["How far the stock can fall first", 22], ["Health of the business", 10], ["How easily the option trades", 10], ["Distance from the next earnings report", 10], ["Size of the payment", 2], ["How pricey options are right now", 2]],
  extreme: [["Chance the option expires unused", 46], ["How far the stock can fall first", 24], ["Health of the business", 8], ["How easily the option trades", 8], ["Distance from the next earnings report", 10], ["Size of the payment", 2], ["How pricey options are right now", 2]],
};

/* The hard limits: a stock or a trade that fails any one is dropped before
   anything is scored. Same numbers as the scanner's settings
   (stock-options-ai/config/settings.py: SCREENER, OPTIONS, TARGETS). */
const LIMITS: [string, string][] = [
  ["Share price", "$5 to $100"],
  ["Shares traded a day", "1 million or more"],
  ["Company size", "$500 million or more"],
  ["Next earnings report", "At least 14 days away"],
  ["Time to expiry", "14 to 60 days"],
  ["Agreed price", "2% to 20% below today's"],
  ["Chance it expires unused", "50% or better"],
  ["Payment", "2% a month or more"],
];

const money = (v: number) => `$${v.toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
const popNum = (p?: string | null) => { const n = parseFloat(String(p ?? "").replace("%", "")); return Number.isFinite(n) ? n : null; };
const cap = (s: string) => s.charAt(0).toUpperCase() + s.slice(1);
const median = (xs: number[]) => { const s = [...xs].sort((a, b) => a - b); return s.length ? s[Math.floor(s.length / 2)] : null; };

export default function OptionsResearchPage() {
  return (
    <StockDrawerProvider back="Back to options research">
      <Body />
    </StockDrawerProvider>
  );
}

function Body() {
  const { data: scan } = useOptionsScan();
  const { data: trader } = useTrader();
  const picks = useMemo(() => [...(scan?.picks ?? [])].sort((a, b) => (b.score ?? 0) - (a.score ?? 0)), [scan]);
  const queued = useMemo(() => new Set(((trader as { candidates?: { symbol: string }[] } | undefined)?.candidates ?? []).map((c) => c.symbol)), [trader]);
  const regime = trader?.regime;
  const mood = (regime?.regime ?? "calm").toLowerCase();
  const weights = WEIGHTS[mood] ?? WEIGHTS.calm;

  const top = picks[0];
  const ratedA = picks.filter((p) => p.rating === "A").length;
  const med = median(picks.map((p) => p.monthly_pct).filter(isNum));
  const medPop = median(picks.map((p) => popNum(p.pop)).filter(isNum));

  const finding = top && isNum(top.strike) && isNum(top.monthly_pct)
    ? `Top idea today: sell a ${top.symbol} ${money(top.strike)} put to ${top.expiration ? monthDay(top.expiration) : "expiry"} for ${top.monthly_pct.toFixed(1)}% a month${top.pop ? `, with ${article(top.pop)} ${top.pop} chance of keeping all of it` : ""}.`
    : null;

  const kpis: Kpi[] = [
    // (no "trade ideas today" tile: the scan always keeps its best 20)
    { label: "Rated A", value: picks.length ? String(ratedA) : "–", note: picks.length ? `Of today's ${picks.length} ideas, scored 75 or more` : "Scored 75 or more out of 100" },
    { label: "Typical monthly payment", value: isNum(med) ? `${med.toFixed(1)}%` : "–", note: "Of the cash set aside, per month" },
    { label: "Typical chance of keeping it", value: isNum(medPop) ? `${Math.round(medPop)}%` : "–", note: "That the option expires unused" },
    { label: "Market mood", value: cap(mood), note: isNum(regime?.vix) ? `Volatility index at ${regime!.vix!.toFixed(1)}` : undefined },
  ];

  return (
    <>
      <DashHeader
        title="Options research"
        finding={finding}
        lead={<>
          Each morning the scanner looks for the best stocks to sell put options on. Selling a put pays you now, in return for
          agreeing to buy the stock at a set lower price if it falls below that price before a date. Every trade it finds is
          scored out of 100, and the best 20 are researched by an AI analyst. The options trader works from this list.
        </>}
        source="Option prices, company data and an AI analyst"
      />

      <div className="mt-6"><KpiRow items={kpis} /></div>

      <div className="mt-4 grid gap-4 lg:grid-cols-[minmax(0,1.5fr)_minmax(0,1fr)]">
        <Panel id="pay-safety" title="How well each idea pays against how safe it is"
          sub="Further right is more likely to keep the payment; higher pays more each month. The best trades sit up and to the right.">
          {picks.length ? <PaySafety picks={picks} /> : <p className="fs-note m-0">Loading today&apos;s scan…</p>}
        </Panel>
        <Panel id="weights" title="What the rating weighs" sub={`Points out of 100 in today's ${mood} market. When markets get nervous, safety counts for more.`}>
          <RankBars rows={weights.map(([name, value]) => ({ name, value }))} format={(v) => String(v)} labelWidth="min(15rem, 58%)" />
          <ul className="mt-5 mb-0 pl-5 grid gap-1.5 fs-note">
            <li>Health of the business blends profits, revenue growth, debt, cash and analyst views with the price trend.</li>
            <li>The equity research grade counts for a fifth of the final score.</li>
            <li>Red flags cut the score: earnings within a month, thin trading, dilution or bankruptcy risk, a stock that just spiked.</li>
            <li>Ratings: A from 75, B from 60, C from 45, D below.</li>
          </ul>
        </Panel>
      </div>

      <Panel className="mt-4" id="limits" title="What every idea must clear">
        <dl className="m-0 grid grid-cols-2 lg:grid-cols-4 gap-x-8 gap-y-5">
          {LIMITS.map(([name, value]) => (
            <div key={name} className="min-w-0">
              <dt className="text-[0.8125rem]" style={{ color: "var(--fs-ink-2)" }}>{name}</dt>
              <dd className="m-0 mt-1 text-[1rem] font-semibold">{value}</dd>
            </div>
          ))}
        </dl>
      </Panel>

      <Panel className="mt-4" id="ideas" title="Today's trade ideas"
        sub="Ranked by score. Choose a company for its full detail, or Details for the rating's parts and the AI analyst's write-up.">
        <IdeasTable picks={picks} queued={queued} />
      </Panel>
    </>
  );
}

/* ── payment against safety ─────────────────────────────────────────────── */
// One series: rating is shown by fill (solid A, tinted B, hollow C and D) as
// well as by the table below, so it never rests on colour alone.
function PaySafety({ picks }: { picks: ScanPick[] }) {
  const { ref, width } = useWidth<HTMLDivElement>();
  const [hover, setHover] = useState<string | null>(null);
  const pts = picks.map((p) => ({ p, x: popNum(p.pop), y: p.monthly_pct })).filter((d): d is { p: ScanPick; x: number; y: number } => isNum(d.x) && isNum(d.y));
  const H = 300, M = { top: 12, right: 16, bottom: 34, left: 44 };
  const xs = pts.map((d) => d.x), ys = pts.map((d) => d.y);
  const x0 = Math.floor((Math.min(...xs, 60) - 2) / 5) * 5, x1 = Math.ceil((Math.max(...xs, 80) + 2) / 5) * 5;
  const y1 = Math.ceil(Math.max(...ys, 4) + 0.5);
  const plotW = Math.max(width - M.left - M.right, 10), plotH = H - M.top - M.bottom;
  const X = (v: number) => M.left + ((v - x0) / (x1 - x0)) * plotW;
  const Y = (v: number) => M.top + (1 - v / y1) * plotH;
  const xt: number[] = []; for (let t = x0; t <= x1; t += 5) xt.push(t);
  const yt: number[] = []; for (let t = 0; t <= y1; t += y1 > 6 ? 2 : 1) yt.push(t);
  // Label the best-scored ideas first, skipping any label that would collide
  // with one already placed; the rest are named on hover and in the table.
  const labelled = new Set<string>();
  if (width > 0) {
    const placed: { x: number; y: number }[] = [];
    for (const d of [...pts].sort((a, b) => (b.p.score ?? 0) - (a.p.score ?? 0))) {
      if (labelled.size >= 10) break;
      const lx = M.left + ((d.x - x0) / (x1 - x0)) * plotW, ly = M.top + (1 - d.y / y1) * plotH;
      if (placed.some((q) => Math.abs(q.y - ly) < 15 && Math.abs(q.x - lx) < 56)) continue;
      placed.push({ x: lx, y: ly });
      labelled.add(d.p.symbol);
    }
  }
  const fill = (r?: string | null) => (r === "A" ? "var(--fs-blue)" : r === "B" ? "color-mix(in srgb, var(--fs-blue) 45%, var(--fs-canvas))" : "var(--fs-canvas)");
  const hov = pts.find((d) => d.p.symbol === hover);
  return (
    <figure className="m-0">
      <div className="flex flex-wrap gap-x-6 gap-y-1 mb-3 text-[0.8125rem]" style={{ color: "var(--fs-ink-2)" }}>
        {(["A", "B", "C"] as const).map((r) => (
          <span key={r} className="inline-flex items-center gap-2">
            <svg width={12} height={12} aria-hidden><circle cx={6} cy={6} r={4.5} fill={fill(r)} stroke="var(--fs-blue)" strokeWidth={1.5} /></svg>
            {r === "C" ? "Rated C or D" : `Rated ${r}`}
          </span>
        ))}
      </div>
      <div ref={ref} className="relative" style={{ height: H }}>
        {width > 0 && (
          <svg width={width} height={H} role="img" aria-label="Each trade idea by its chance of keeping the payment and its monthly payment; the table below lists the same figures.">
            {yt.map((t) => (
              <g key={`y${t}`}>
                <line x1={M.left} x2={M.left + plotW} y1={Y(t)} y2={Y(t)} stroke={t === 0 ? "var(--fs-line-strong)" : "var(--fs-line)"} />
                <text className="fs-axis" x={M.left - 8} y={Y(t)} textAnchor="end" dominantBaseline="central">{t}%</text>
              </g>
            ))}
            {xt.map((t) => <text key={`x${t}`} className="fs-axis" x={X(t)} y={H - 14} textAnchor="middle">{t}%</text>)}
            <text className="fs-axis" x={M.left + plotW} y={H - 1} textAnchor="end">Chance of keeping the payment</text>
            {pts.map(({ p, x, y }) => {
              const cx = X(x), cy = Y(y);
              const right = cx < M.left + plotW - 60;
              return (
                <g key={p.symbol} onPointerEnter={() => setHover(p.symbol)} onPointerLeave={() => setHover(null)}>
                  <circle cx={cx} cy={cy} r={14} fill="transparent" />
                  <circle cx={cx} cy={cy} r={hover === p.symbol ? 7 : 5.5} fill={fill(p.rating)} stroke="var(--fs-blue)" strokeWidth={1.5} />
                  {labelled.has(p.symbol) && (
                    <text x={right ? cx + 9 : cx - 9} y={cy} dominantBaseline="central" textAnchor={right ? "start" : "end"}
                      className="fs-axis" style={{ fill: "var(--fs-ink-2)", fontWeight: 560 }}>{p.symbol}</text>
                  )}
                </g>
              );
            })}
          </svg>
        )}
        {hov && (
          <div className="fs-tip" style={{ top: Math.max(0, Y(hov.y) - 70), left: Math.max(0, Math.min(X(hov.x) + 12, width - 190)), minWidth: 170 }}>
            <div className="font-semibold">{hov.p.symbol} <span className="fs-note">rated {hov.p.rating}</span></div>
            <div className="fs-num">{hov.y.toFixed(2)}% a month</div>
            <div className="fs-num">{hov.x.toFixed(1)}% chance of keeping it</div>
          </div>
        )}
      </div>
      <figcaption className="fs-note mt-2">Monthly payment as a share of the cash set aside to buy the shares.</figcaption>
    </figure>
  );
}

/* ── the ideas ──────────────────────────────────────────────────────────── */
function IdeasTable({ picks, queued }: { picks: ScanPick[]; queued: Set<string> }) {
  const { open } = useStock();
  const [why, setWhy] = useState<string | null>(null);
  if (!picks.length) return <p className="fs-note m-0">Loading today&apos;s scan…</p>;
  return (
    <div className="overflow-x-auto">
      <table className="fs-table">
        <thead>
          <tr>
            <th scope="col">Company</th>
            <th scope="col" className="r hidden sm:table-cell">Price</th>
            <th scope="col" className="r">Sell a put at</th>
            <th scope="col" className="r hidden md:table-cell">Expires</th>
            <th scope="col" className="r">Pays a month</th>
            <th scope="col" className="r hidden md:table-cell">Chance of keeping it</th>
            <th scope="col" className="r hidden lg:table-cell">Room to fall</th>
            <th scope="col" className="fs-gradecol">Rating</th>
            <th scope="col" className="r hidden lg:table-cell">AI would own</th>
            <th scope="col" className="r"><span className="sr-only">Details</span></th>
          </tr>
        </thead>
        <tbody>
          {picks.map((p) => {
            const best = bestStrike(p);
            const isOpen = why === p.symbol;
            const days = p.expiration ? daysUntil(p.expiration) : null;
            return (
              <Fragment key={p.symbol}>
                <tr className={`fs-pick ${isOpen ? "fs-open" : ""}`} onClick={() => setWhy(isOpen ? null : p.symbol)}>
                  <th scope="row">
                    <span className="inline-flex items-center gap-2">
                      <button type="button" className="fs-rowbtn" onClick={(e) => { e.stopPropagation(); open(p.symbol); }}>{p.symbol}</button>
                      {queued.has(p.symbol) && <span className="fs-tag">On the trader&apos;s list</span>}
                    </span>
                    <span className="block fs-note">{best?.Sector ?? ""}</span>
                  </th>
                  <td className="r hidden sm:table-cell">{isNum(p.price) ? money(p.price) : "–"}</td>
                  <td className="r font-semibold">{isNum(p.strike) ? money(p.strike) : "–"}</td>
                  <td className="r hidden md:table-cell">{p.expiration ? monthDay(p.expiration) : "–"}{isNum(days) && <span className="block fs-note">{days} days</span>}</td>
                  <td className="r">{isNum(p.monthly_pct) ? `${p.monthly_pct.toFixed(1)}%` : "–"}{isNum(p.premium) && <span className="block fs-note">{money(p.premium)} a share</span>}</td>
                  <td className="r hidden md:table-cell">{p.pop ?? "–"}</td>
                  <td className="r hidden lg:table-cell">{isNum(best?.["Buffer %"]) ? `${best!["Buffer %"]!.toFixed(0)}%` : "–"}</td>
                  <td className="fs-gradecol">
                    <span className="inline-flex items-baseline gap-2 whitespace-nowrap">
                      <span className="font-semibold">{p.rating ?? "–"}</span>
                      <span className="fs-note fs-num">{isNum(p.score) ? Math.round(p.score) : ""}</span>
                    </span>
                  </td>
                  <td className="r hidden lg:table-cell">{isNum(p.would_own) ? `${p.would_own} of 10` : "–"}</td>
                  <td className="r">
                    <button type="button" className="fs-link bg-transparent border-0 p-0 cursor-pointer text-[0.875rem]" aria-expanded={isOpen}
                      onClick={(e) => { e.stopPropagation(); setWhy(isOpen ? null : p.symbol); }}>
                      Details
                    </button>
                  </td>
                </tr>
                {isOpen && <tr className="fs-why"><td colSpan={10}><IdeaDetail p={p} best={best} /></td></tr>}
              </Fragment>
            );
          })}
        </tbody>
      </table>
      <p className="fs-note mt-4 mb-0">
        &ldquo;Room to fall&rdquo; is how far the stock can drop before the trade loses money. &ldquo;On the trader&apos;s list&rdquo; marks ideas that also pass the
        trader&apos;s own checks for its next run.
      </p>
    </div>
  );
}

/** The strike the pick's headline refers to (falls back to the first one). */
function bestStrike(p: ScanPick): ScanStrike | undefined {
  const s = p.strikes ?? [];
  return s.find((x) => x.Strike === p.strike && x.Expiration === p.expiration) ?? s[0];
}

/* The rating's parts, the other strikes considered, and the analyst's read. */
function IdeaDetail({ p, best }: { p: ScanPick; best?: ScanStrike }) {
  const sections = analysisSections(p.analysis);
  const others = (p.strikes ?? []).filter((s) => s !== best);
  const facts: [string, string | null][] = best ? [
    ["Business health", isNum(best["Fund Score"]) ? `${Math.round(best["Fund Score"]!)} of 100` : null],
    ["Price trend", isNum(best["Tech Score"]) ? `${Math.round(best["Tech Score"]!)} of 100` : null],
    ["How easily it trades", isNum(best["Liq Score"]) ? `${Math.round(best["Liq Score"]!)} of 100` : null],
    ["Breakeven price", isNum(best.Breakeven) ? money(best.Breakeven!) : null],
    ["Yearly rate", isNum(best["Ann. %"]) ? `${best["Ann. %"]!.toFixed(0)}%` : null],
    ["Next earnings report", isNum(best["Earnings Days"]) ? `In ${Math.round(best["Earnings Days"]!)} days` : null],
    ["Contracts open", isNum(best.OI) ? best.OI!.toLocaleString("en-US") : null],
    ["Gap between bid and ask", isNum(best["Spread %"]) ? `${best["Spread %"]!.toFixed(0)}% of the price` : null],
  ] : [];
  return (
    <div className="px-4 sm:px-6 py-5 grid gap-8 lg:grid-cols-[minmax(0,1fr)_minmax(0,1.3fr)] text-[0.9375rem]">
      <div>
        <p className="m-0 mb-3 font-semibold">The trade, measured</p>
        <dl className="m-0 grid grid-cols-2 gap-x-6 gap-y-3">
          {facts.filter(([, v]) => v).map(([k, v]) => (
            <div key={k}><dt className="fs-note">{k}</dt><dd className="m-0 mt-0.5 font-semibold fs-num">{v}</dd></div>
          ))}
        </dl>
        {others.length > 0 && (
          <>
            <p className="m-0 mt-6 mb-2 font-semibold">Other strikes it considered</p>
            <ul className="list-none p-0 m-0 grid gap-1">
              {others.slice(0, 4).map((s, i) => (
                <li key={i} className="fs-num" style={{ color: "var(--fs-ink-2)" }}>
                  {isNum(s.Strike) ? money(s.Strike!) : "–"} to {s.Expiration ? monthDay(s.Expiration) : "–"}: {isNum(s["Monthly %"]) ? `${s["Monthly %"]!.toFixed(1)}% a month` : "–"}, {s.POP ?? "–"} chance, rated {s.Rating ?? "–"}
                </li>
              ))}
            </ul>
          </>
        )}
      </div>
      <div>
        <p className="m-0 mb-3 font-semibold">
          The AI analyst&apos;s read{isNum(p.would_own) && <span className="fs-note font-normal">: would own the shares, {p.would_own} of 10</span>}
        </p>
        {sections.length ? sections.map(([head, body]) => (
          <div key={head} className="mb-3">
            {head && <p className="m-0 font-medium">{head}</p>}
            <p className="m-0 mt-1" style={{ color: "var(--fs-ink-2)" }}>{body}</p>
          </div>
        )) : <p className="fs-note m-0">No write-up for this one today.</p>}
      </div>
    </div>
  );
}

/** "**Risk assessment**: …\n\n**Why it is a wheel candidate**: …\n\nWOULD_OWN_SCORE: 6"
 *  → [["Risk assessment", "…"], ["Why it is a candidate", "…"]]. */
function analysisSections(text?: string | null): [string, string][] {
  if (!text) return [];
  return unbrand(text)
    .replace(/WOULD_OWN_SCORE:\s*\S+/gi, "")
    .split(/\n\s*\n/)
    .map((b) => b.trim())
    .filter(Boolean)
    .map((b) => {
      const m = b.match(/^\*\*(.+?)\*\*:?\s*([\s\S]*)$/);
      const head = m ? m[1].replace(/wheel candidate/i, "candidate") : "";
      return [head, (m ? m[2] : b).replace(/\*\*/g, "").trim()] as [string, string];
    })
    .filter(([, body]) => body.length > 0);
}
