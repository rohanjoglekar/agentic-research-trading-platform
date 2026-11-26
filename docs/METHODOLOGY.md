# Methodology — statistical validity before strategy promotion

This methodology document defines how the platform determines whether a quantitative
strategy result is statistically credible and eligible to advance from
research into shadow testing and automated paper trading. It covers measured
transaction costs, chronological data isolation, family-wise multiple-testing
correction, versioned market economics, and the retention of negative findings.

It describes the BTC research lab, the platform's main research program until
it was paused at the end of August 2026. The lab's source is not in this
repository; the excerpts below are quoted from the private code as it stands
today. When this was written the pipeline had evaluated more than 33,000
strategy variants across 540 days of BTC market data (about 395,000 by the
time it was paused), and the overwhelming majority failed. That rejection rate
is intentional; promotion is reserved for results that remain defensible after
costs, selection effects, and live out-of-sample observation.

## Trading costs are measured, not assumed

Every backtest applies the venue's actual fee schedule. For Kalshi 15-minute
binary contracts, that is `ceil(0.07·C·P·(1−P))` per execution, independently
verified against the published schedule, plus a bid–ask half-spread measured
by a collector that records the live order book:

```python
# algo-trading/btc_lab/config.py
mid_half_spread_cents: float = 5.0    # against the trader, 0.10 <= p <= 0.90
tail_half_spread_cents: float = 4.25  # deci-cent tick region: p<0.10 or p>0.90
```

The original values were 0.5¢ and 0.1¢: superficially plausible assumptions
that understated observed execution costs by approximately one order of
magnitude. Analysis of 398 tradable entry-minute quotes found a median full
spread of about 10¢, requiring recalibration to 5.0¢ and complete repricing of
the leaderboard. The shadow book the pipeline was gating on moved from
**+$67.53 to −$26.50** under the corrected economics. The result was published
unchanged. A promotion system is only useful if adverse recalibrations are
reflected immediately rather than suppressed. A spread-sensitivity study gives
each variant's break-even half-spread (6.11¢ on validation for the leading
strategy, against the measured 5.0¢), and the dashboard shows the modelled
spread beside the observed one: the gap between those values is the remaining
execution margin.

The original 0.1¢ tail value also exposed a category error: the venue's tick
size below 10¢ had been treated as its spread. Fine quote increments do not
imply narrow liquidity; an order book that moves in 0.1¢ ticks can still be
8¢ wide.

## Chronological train, validation, and holdout isolation

Data is divided chronologically into 60% training, 20% validation, and 20%
holdout segments. Parameters and thresholds are fit on training data. Ranking,
the choice of which candidates to refine, and every promotion gate read
validation data. Holdout results are computed and reported for every variant
but never rank, select or gate one. There is one disclosed exception: a single
gate threshold was calibrated against holdout on 2026-08-03, which spends a
little of its independence and is recorded as such beside the setting.
Dashboard comparisons identify the segment they use, preventing
selection-conditioned results from being presented as out-of-sample evidence.

## Multiple-testing control on promotion

As the number of candidates increases, selecting the best result will
eventually clear any fixed significance threshold through chance alone.
The platform therefore raises the promotion t-statistic floor with the number of
concurrent shadow tests:

```python
# algo-trading/btc_lab/pipeline.py (docstring abridged)
def _mt_floor(n_concurrent: int) -> float:
    """FWER-controlling promotion floor for N concurrent shadow tests: the
    one-sided Bonferroni tail quantile Φ⁻¹(1 − α/N), α=0.05. A pure-null
    best-of-N clears it only ~5% of the time.
    ...
    At N=1 the Bonferroni form is 1.645 (a plain one-sided 5% test); it rises
    with N."""
```

An earlier version of this docstring, quoted here until November 2026, went on
to dismiss the √(2·ln N) deflation used by the backtest gate as something
half of null batches exceed. That was wrong, and the code now says so: at the
lab's scale that rule is a family-wise test at roughly 10%, close to this one.

The underlying statistic is a t-test on the mean P&L of each strategy's
**realized payoff distribution**, net of fees and measured spread. This
replaced a fixed hold-to-expiry break-even win rate that was structurally
inapplicable to seven of ten live strategies; a strategy with protective exits
does not share the break-even rate of a hold-to-expiry strategy (52.5% under
the original spread assumption, 57.0% under the measured one). The concurrent
test count, N, is seeded when a strategy enters shadow testing and afterwards
only rises, to the live count of concurrent shadow tests. It never falls when
competitors are evicted, so a survivor is not judged at the significance of a
single test.

## Results are stamped with the economics that produced them

When execution economics change, historical results cease to be directly
comparable. Each venue defines an `ECONOMICS_VERSION`, every result is stamped
with that version, and all downstream consumers — leaderboard ranking,
candidate selection, reevaluation, and admission to shadow testing — reject
results produced under stale economics. Engine versioning is intentionally
separate: non-economic engine changes should not invalidate months of pricing
history, while a pricing correction must never remain hidden inside an
otherwise compatible release.

## Negative findings terminate unsupported work

Three completed studies produced explicit no-go decisions:

- **Maker viability** — Repriced all 625 perpetual variants under favorable
  maker assumptions: complete entry fills at 5 basis points. None achieved
  holdout significance, so the maker entry path, already written behind a
  flag, stays switched off.
- **Timeframe sensitivity** — Evaluated the perpetual strategy families across
  six bar sizes from 1h to 1d: 870 scored configurations, with a fee-sensitivity
  control. None cleared even the selection bar, providing no evidence that the
  proposed edge existed at another horizon.
- **BTC/ETH pair trading** — Tested 202 spread-reversion configurations; none
  met the viability threshold.

After 1,161 evaluations, the current perpetual-futures conclusion is that
losses approximately equal fees, with no surviving hypothesis beyond
walk-forward retraining and LLM-generated families that remain subject to the
standard gates. The dashboard presents that conclusion directly. Preserving
negative findings prevents rejected hypotheses from quietly re-entering the
execution roadmap.

## Shadow testing is the same code path as backtesting

Strategies under shadow testing generate predictions for live 15-minute
windows without placing orders. They use the same engine, exit logic, and cost
model as their backtests. A reconciliation process flags material divergence
between live and historical signal frequencies, providing an operational
control for detecting look-ahead bias or implementation drift after a model
enters production data flow.
