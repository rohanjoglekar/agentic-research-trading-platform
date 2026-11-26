# Agentic Research & Trading Platform

AI agents and rule-based code research about 600 US stocks every weekday,
build full valuation models from SEC filings, and run two paper trading
strategies on what they find — each measured against the S&P 500.

**Live read-only demo: [agentic-research-trading.vercel.app/demo](https://agentic-research-trading.vercel.app/demo)** — production data, no sign-in, and no way to place an order.

This repository holds the source for the four parts the demo shows — equity
research, options research, and the two paper trading books — with their
tests, the demo's own pages, and the documents that explain how the platform
is built and what has been measured, including the results that were not
flattering. The parts that read a real brokerage account, and the server that
runs it all, stay private; [what is here and what is not](#what-is-in-this-repository)
is listed below.

## What is in this repository

| Part | Where the code is | Start reading at |
|---|---|---|
| **Equity research** | [`forge-dashboard/backend/adapters/`](forge-dashboard/backend/adapters) | [`rh_research.py`](forge-dashboard/backend/adapters/rh_research.py) builds the screen, [`rh_factors.py`](forge-dashboard/backend/adapters/rh_factors.py) scores the seven factors and the confidence that discounts a thin grade, [`rh_dcf.py`](forge-dashboard/backend/adapters/rh_dcf.py) is the screen DCF and its gates |
| Valuation engine | [`stock-options-ai/deep_dcf/`](stock-options-ai/deep_dcf) | [`batch.py`](stock-options-ai/deep_dcf/batch.py) asks for assumptions, [`tools/build_model.py`](stock-options-ai/deep_dcf/tools/build_model.py) writes the workbook, [`tools/check_delivery.py`](stock-options-ai/deep_dcf/tools/check_delivery.py) decides whether it may be published, [`budget.py`](stock-options-ai/deep_dcf/budget.py) is the spending ledger, [`store.py`](stock-options-ai/deep_dcf/store.py) keeps the public index |
| Weekly AI read | [`stock-options-ai/ai_reads/`](stock-options-ai/ai_reads) | [`reader.py`](stock-options-ai/ai_reads/reader.py) holds the prompt and schema; [`rh_ai_reads.py`](forge-dashboard/backend/adapters/rh_ai_reads.py) turns reads into percentiles |
| **Options research** | [`stock-options-ai/screener/`](stock-options-ai/screener), [`scoring/`](stock-options-ai/scoring), [`agents/`](stock-options-ai/agents) | [`jobs/daily_scan.py`](stock-options-ai/jobs/daily_scan.py) is the pipeline, [`scoring/composite_score.py`](stock-options-ai/scoring/composite_score.py) the rating, [`agents/per_stock_analyst.py`](stock-options-ai/agents/per_stock_analyst.py) the AI analyst |
| **Equity trading** | [`stock-options-ai/trading/`](stock-options-ai/trading), [`strategies/`](stock-options-ai/strategies) | [`strategies/alpha_portfolio.py`](stock-options-ai/strategies/alpha_portfolio.py) decides what to hold, [`trading/alpha_runner.py`](stock-options-ai/trading/alpha_runner.py) turns that into orders, [`trading/alpha_governor.py`](stock-options-ai/trading/alpha_governor.py) is the weekly review and its limits |
| **Options trading** | [`stock-options-ai/trading/`](stock-options-ai/trading) | [`wheel_runner.py`](stock-options-ai/trading/wheel_runner.py) is one run, [`agentic_manager.py`](stock-options-ai/trading/agentic_manager.py) the hold, close or roll decision, [`covered_call_runner.py`](stock-options-ai/trading/covered_call_runner.py) the covered calls, [`reserve.py`](stock-options-ai/trading/reserve.py) the cash floor |
| Put-selling replay | [`stock-options-ai/backtests/csp_universe/`](stock-options-ai/backtests/csp_universe) | [`replay.py`](stock-options-ai/backtests/csp_universe/replay.py); read its docstring before quoting a number |
| Public gateway policy | [`forge-dashboard/backend/viewonly.py`](forge-dashboard/backend/viewonly.py) | and [its tests](forge-dashboard/backend/tests/test_view_only_boundary.py) |
| The demo's pages | [`forge-view/`](forge-view) | [`app/demo/`](forge-view/app/demo), a small Next.js app that runs against the public API |

Every threshold the scan and the two books use is in one file,
[`stock-options-ai/config/settings.py`](stock-options-ai/config/settings.py).

The Python files are published exactly as they run in production, which is
why two directories keep the project's internal name, and why the research
modules carry an `rh_` prefix and the API paths say `/api/robinhood/`: the
screen began as a tab beside a brokerage view, and the names stayed.

**Not here:** the API server and everything that reads the brokerage account
(holdings, orders, portfolio risk, position advice), the valuation engine's
scheduler, which ranks its queue by what the account holds, notifications,
the deployment scripts and service definitions, and the platform's other
research programs. The weekly read job takes the account's list of positions
as an input when there is one, and runs without it. Before a file is
published, a release gate checks it for secrets and for anything that would
say what the account holds.

### Running it

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
(cd stock-options-ai && python -m pytest tests -q)          # options research, both books, valuation engine
(cd forge-dashboard/backend && python -m pytest tests -q)   # the screen, the screen DCF, the gateway policy
```

The tests need no keys and no network. The backend tests that exercise the
private API server are skipped here, and one engine test needs LibreOffice.

```bash
cd forge-view && npm ci && npm run dev    # the demo's pages, reading the public API
python stock-options-ai/jobs/daily_scan.py   # the scan pipeline, offline, on mock data
```

Running a job against live data needs the keys listed in
[`.env.example`](.env.example). The trading jobs only ever address Alpaca
paper accounts.

## System at a glance

```mermaid
%%{init: {"theme": "base", "themeVariables": {"fontSize": "17px", "lineColor": "#64748b", "edgeLabelBackground": "#ffffff"}, "flowchart": {"nodeSpacing": 34, "rankSpacing": 42, "curve": "basis"}}}%%
flowchart TB
    subgraph research["01 · RESEARCH"]
        direction LR
        data(["SEC FILINGS · PRICES<br/>OPTION CHAINS · NEWS"]):::source --> grade["600-STOCK GRADE<br/>seven factors"]:::research
        data --> dcf["AGENTIC DCF ENGINE<br/>bear · base · bull"]:::agent
        data --> read["WEEKLY AI READ<br/>every company"]:::agent
        read --> grade
        data --> scan["PUT-SELLING SCAN<br/>rated A–D"]:::research
    end

    subgraph trading["02 · RULE-BASED TRADING"]
        direction LR
        rules{"SIZING · CAPS<br/>VOLATILITY GATES"}:::gate --> books["TWO PAPER BOOKS<br/>equities · options"]:::execution
        books --> bench(["MEASURED VS<br/>S&P 500"]):::neutral
    end

    grade --> rules
    scan --> rules
    bench -. "weekly AI review<br/>within fixed limits" .-> rules

    classDef source fill:#fff7ed,stroke:#f59e0b,color:#7c2d12,stroke-width:2px,font-weight:600;
    classDef research fill:#111827,stroke:#f59e0b,color:#f9fafb,stroke-width:2px,font-weight:600;
    classDef agent fill:#ecfeff,stroke:#0891b2,color:#164e63,stroke-width:2px,font-weight:600;
    classDef gate fill:#fef3c7,stroke:#b45309,color:#78350f,stroke-width:2px,font-weight:700;
    classDef execution fill:#eff6ff,stroke:#2563eb,color:#1e3a8a,stroke-width:2px,font-weight:600;
    classDef neutral fill:#f8fafc,stroke:#64748b,color:#334155,stroke-width:2px,font-weight:600;
    style research fill:#fffaf0,stroke:#f59e0b,stroke-width:2px,color:#7c2d12
    style trading fill:#f8fafc,stroke:#334155,stroke-width:2px,color:#111827
    linkStyle default stroke:#64748b,stroke-width:2px;
```

AI does the reading, the valuation modeling, and the judgment calls on trades
that have gone wrong. Rules decide every new position: no model can open one.
On a trade that is already open and under stress, an agent may choose to hold,
close or roll it, inside hard limits it cannot override. The public demo has
no path back into trading controls.

## Where AI is used and where rules decide

| Part | AI's role | What stays rule-based |
|---|---|---|
| **Equity research** | Claude reads every company each week and scores the business; Claude creates full bear/base/bull DCF models for eligible companies. | The 0–100 grade, its factor weights, and the screen's own DCF. |
| **Options research** | An AI analyst researches the 20 best put-selling candidates on the web, writes up the risks, and scores 1–10 how willing it would be to own the shares. | Each contract's score and A–D rating. |
| **Equity trading** | Once a week an AI reviews results against the S&P 500 and adjusts settings, only within fixed limits. | Every buy and sell. The AI never picks stocks. |
| **Options trading** | When an open trade moves against the book, an agent researches the company and decides whether to hold, close early, or roll. | Which puts to sell, the cash reserve, and hard stops the agent cannot override. |

Models in use: Claude Opus 5.5 and Sonnet 5.5 for valuation models, Claude
Haiku 5.5 for the weekly reads, Gemini 2.5 Flash with Google Search grounding
for options research and trade management (falling back to Claude, then to
rules), and local models served through Ollama for the weekly review and
off-hours news research.

## Equity research

Every weekday each company in a ~600-stock US universe gets a grade from 0 to
100. The grade blends seven weighted measures: value against its industry
(25), quality (20), growth (20), price momentum (20), analyst expectations
(10), news tone (5), and the weekly AI read (5). Thin data lowers a grade
rather than being guessed.

**Screen DCF.** A 10-year cash-flow valuation, with growth fading to a
long-run rate and a Gordon terminal value, runs beside the grade as a second
check on price. Fifteen gates check every result, and a value that trips a
red gate is reported as an artifact, not a target. One gate values
multi-class companies on every share class: before that fix the share count
was more than 10% off on 51 of 625 names, which overstated fair value by up
to 5×. Only a DCF that clears its gates may drive a holdings action. In the browser, a live spreadsheet with editable
assumptions reproduces each published value to the cent.

**Agentic DCF engine.** For eligible companies, Claude builds a full
banking-style model:

- bear, base, and bull cases over a 10-year projection, with three 5×5
  sensitivity grids where every cell is a live formula;
- inputs from SEC XBRL filings (five fiscal years plus the trailing twelve
  months) and the FRED 10-year yield. The model is deliberately not shown the
  screen's fair value or the analyst target, so its view is independent;
- the default engine sends one Message Batches API request per company and
  asks the model only for its assumptions, as JSON in a fixed schema. Python
  then writes the workbook and computes every case. On the comparison run its
  base value matched the original headless agent's to within 1%;
- before a model is published, LibreOffice recalculates the workbook under
  each case. The model is rejected if any formula errors, if the summary
  disagrees with the recalculated cells, if price ≠ (EV − net debt) / shares,
  if bear ≤ base ≤ bull fails, or if WACC, terminal growth, terminal-value
  share or fair value fall outside sane bounds;
- scheduled batches stop if fewer than half of a batch pass;
- new 10-K and 10-Q filings trigger refreshes. Financials, REITs, resource
  companies, pre-profit companies, non-SEC filers and names the screen itself
  distrusts are never modelled. The rest of the ~600-stock universe is in
  scope, and more than 300 models are published on the demo.

The deep models are advisory. They feed neither the grade nor either trading
book.

**Weekly AI read.** Once a week Claude reads every research and held company
and returns five 0–10 scores (business quality, growth durability, financial
resilience, risk outlook, valuation against quality), a conviction, a
one-line thesis and a one-line risk. The prompt withholds the screen's score,
tier, DCF and analyst target. Scores enter the grade as weekly percentiles,
and only once 90% of the universe has a fresh read. The read replaced a
local-model verdict that covered 35 of 598 names and said SELL on 77% of
them. The first full run covered 608 names with zero errors.

## Options research

Each morning the scanner looks for the best stocks to sell cash-secured puts
on. Every contract is scored out of 100 and rated A to D. The weights come
from replaying 2,150 past picks to expiry. The heaviest are the chance the
option expires worthless and how far the stock can fall before the trade
loses money. Business quality, option liquidity and distance from earnings
count too, and premium size counts least.

## Trading books

| Book | Account | Mandate |
|---|---|---|
| **Equity trader** | Alpaca, $1M paper | Long-only. Holds up to 22 of the best-graded stocks beside an index-fund core, with more weight on stronger conviction and deeper discounts to estimated value. Caps: 8% per stock, 30% per sector. Holds more Treasury bills when few stocks look compelling. Sells half after a 30% gain, exits after a 15% loss, and exits when the grade falls below 45. |
| **Options trader** | Alpaca, $100K paper | Cash-secured-put wheel, run three times each trading day. Takes profit at 80% of premium. Sells covered calls on assigned shares. A volatility gate halves new size above VIX 25 and stops new opens above 35. |

Both books trade paper capital at live prices. The point is to test the
machinery under real conditions, not to curate good returns, so the demo
reports every result against the S&P 500, including the unflattering ones.

## Recent engineering work

- **Stopped same-day churn in the equity book.** Of 321 closed positions, 57%
  had closed within three days. Three rules fixed it: a held name keeps its
  slot while it ranks inside the top 1.5× of slots, a five-day minimum hold
  applies (stop-losses still fire), and the book never buys a name its sell
  rules would reject. A test checks the buy and sell rules agree across 1,344
  combinations. Modeled turnover fell from 75× to 38× and friction from 3.0%
  to 1.5%.
- **Replayed the put-selling rules on real option prices.** The replay used
  Alpaca daily option bars, expired contracts included, from Jan 2024 to
  Nov 2026: 11,087 stock-month chains and 374,638 candidate puts. A $100K
  account ran through the live sizing, cash floor, volatility gate, sector cap
  and exits, paying 4% of the option price per side plus fees. The live setup
  returned +5.2% a year with losing trades held to expiry (max drawdown
  −12.7%, 81% win rate over 1,253 trades) and +4.1% with losers bought back,
  against the S&P 500's +14.7%. Widening to the 300 most-traded stocks did
  worse in all 200 randomised replays with losers held, and in 94% of them
  with losers bought back. The universe and price cap stayed. The strategy
  trails the index, and the replay is reported as a decision tool, not a
  performance claim.
- **Fixed a silent volatility-gate failure.** The fallback volatility measure
  read about 0.55× the real VIX, so the risk gates had not been firing. The
  gate now tries the official VIX close from several sources first.
- **Measured the options cash reserve against free cash.** The paper wheel
  had held $68,850 of short-put collateral against $50,702 of cash while
  reporting $36,000 deployable. Collateral is now netted out, and unreadable
  collateral counts as fully committed.
- **Hardened the public gateway.** Account namespaces are closed by default,
  only nine research routes are open, and ownership and P&L fields are
  stripped. Tests require the public answer to be identical whichever account
  sits behind it.
- **Treated timing as account data.** The research jobs worked through held
  names first and stamped each result, so the timestamps alone gave the order
  away. The public now gets the day of a read, not the time. Valuation models
  built for the account stay out of a separate public index. A company page
  is answered only for a screened company, so a held name outside the screen
  looks like any unknown ticker.

## The demo

The [demo](https://agentic-research-trading.vercel.app/demo) is a guided,
read-only walk through the platform, served by the same live API as the
private dashboard. Its pages are in [`forge-view/`](forge-view):

- [Overview](https://agentic-research-trading.vercel.app/demo): one section per part, with its headline finding.
- [Equity research](https://agentic-research-trading.vercel.app/demo/research): the ranked universe, valuations, full DCF models and the editable spreadsheet.
- [Options research](https://agentic-research-trading.vercel.app/demo/options-research): the morning scan, the rating weights, the AI analyst's write-ups.
- [Equity trading](https://agentic-research-trading.vercel.app/demo/equities): the book against the S&P 500, holdings, sectors and rules.
- [Options trading](https://agentic-research-trading.vercel.app/demo/options): open positions, collateral against cash, closed trades and the replay.

Every number comes from the public API. A missing figure removes its tile
rather than breaking the page.

## Public/private boundary

The public site is its own Next.js app, compiled with a view-only flag and
served from Vercel. Its API calls are rewritten to a read-only gateway on the
server.

- **Personal brokerage data** — holdings, cost-basis P&L, orders and
  watchlists — is not served at all. Selecting a symbol shows research, never
  a position.
- **Anything that writes** is refused: the gateway accepts read requests
  only and answers every other method with HTTP 403.
- **Operational controls** are removed from the public bundle at compile time
  and independently rejected by the gateway.
- **Which model built a valuation, and what it cost,** is left out of the
  public API. The larger model is reserved for held names, so the model name
  alone would say what is held.
- **When something was built** is treated the same way. The public feed is a
  separate index that holds only the models the account had no part in, and
  it carries days, not timestamps.

The policy is [`viewonly.py`](forge-dashboard/backend/viewonly.py): account
namespaces are closed by default, and a route added later is private until it
is listed.

## Technical documentation

The documents below were written while the platform's main research program
was a BTC 15-minute prediction lab on Kalshi. That lab is not part of the
public demo and its source is not in this repository, but its methodology,
safety and infrastructure lessons carry over to everything above.

- **[Engineering case study](docs/ENGINEERING.md)** — scope, the hardest engineering problems, and the evidence available for review.
- **[Methodology](docs/METHODOLOGY.md)** — measured transaction costs (which repriced the shadow book from +$67.53 to −$26.50), a multiple-testing floor on promotion, and three studies whose conclusion was not to proceed.
- **[Safety](docs/SAFETY.md)** — sandboxing LLM-generated strategy code, and the independent controls that keep live execution under explicit human authorization.
- **[Architecture](docs/ARCHITECTURE.md)** — the full runtime on a four-core, 8 GB server, priority-aware scheduling, and deployment incidents.
- **[Design](docs/DESIGN.md)** — the dashboard's data-integrity rules: every metric carries its measurement window, calibrated values keep their precision, and color signals risk.

## Technology

Python 3.12 on the server, 3.13 in this repository's CI (NumPy, pandas,
openpyxl); FastAPI; Next.js, React and Tailwind;
cron, systemd services and timers on a single Linux VPS; the Anthropic API
including Message Batches; Gemini; local inference through Ollama;
LibreOffice for model recalculation; Alpaca; SEC EDGAR XBRL, FRED and Finnhub
data; GitHub Actions deployment; and Vercel for the public front.

## Related project

[Concordia](https://github.com/rohanjoglekar/voteconcordia) is a real-money,
vote-governed investment club that executes one shared portfolio inside each
member's own brokerage account. It shares this project's operating principle:
keep running where that protects people, and fail closed wherever uncertainty
could touch capital.

## Operating status

Research runs every weekday and both books trade paper accounts at live
prices. Switching to live trading is a manual, human-authorized step by
design.

The code is published for review. All rights reserved.

Contact: rohannj29@gmail.com
