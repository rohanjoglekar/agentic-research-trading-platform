# Design — data integrity as an interface requirement

This design document defines how the platform's dashboard presents quantitative
research, trading performance, and operational state without distorting the
evidence used for model-promotion and execution decisions. It explains the
interface rules for measurement windows, risk vocabulary, calibrated
precision, log filtering, analytical tables, and live-data failure, together
with the production incidents that established those rules.

The dashboard described here is the private one, and several examples come
from the BTC research lab, which was paused at the end of August 2026 and is
no longer shown on the public demo.

## Methodology is progressive disclosure, not hidden context

The **“How the BTC Research Lab works”** row is an expandable methodology
guide. Its collapsed state keeps the research workspace focused on current
results; expanding it reveals the chronological split, friction model,
modeled-price limitations, backtest gates, shadow-promotion gates, installation
semantics, and scorekeeping rules in one place.

The control changes presentation state only. It cannot run research, modify a
gate, install a candidate, arm a trader, or place an order. Fee and
backtest-gate values, which change with engine configuration, are read from
the dashboard payload rather than duplicated as static UI copy; the
shadow-promotion gates are still maintained by hand in the component. The
design removes repeated caveat text from every table without removing the
caveats themselves.

## Every metric identifies its measurement window

A performance statistic without a defined time window changes meaning whenever a counter resets or a strategy is replaced. The platform makes the measurement start date part of the data contract: when a figure is measured from an anchor date, the backend returns that date with the result and the interface labels every dependent figure with an explicit qualifier such as *since Jul 11*.

The rule cuts both ways. That Kalshi anchor was introduced in July, when 51% of the account's lifetime orders predated the strategy then running. It was withdrawn in August: a period-by-period check showed nothing in the process had changed on the chosen date, so the window was selecting on its own outcome. The lifetime record is what is shown now, and an anchor may return only when it marks a real change of system.

This rule exposed two defects, the second considerably more serious than the first. A stale component rendered a profit figure beside an equity balance that could not be reconciled against it, because the two values were measured from different starting points. Relabeling the card was the obvious repair.

Requiring the numbers to reconcile *arithmetically*, rather than merely to be labeled, is what exposed the underlying defect. The reported profit could not be derived from the settled trades, so the trade-level records were recomputed independently and reconciled against the broker's own balance. The P&L reconstruction had been reading cost basis from field names the venue does not return; any settlement outside the recent fill window was therefore priced at zero cost and recorded its entire payout as profit. The account had not been profitable at all. Correcting the field names inverted the reported result from a substantial gain to a modest loss, and the corrected figure replaced it everywhere it was shown.

The operational rule that follows is stronger than the labeling rule that started it: reconcile every derived financial figure against an external source of truth — here, the broker's balance — rather than against another internal computation. Two internal paths agreeing means only that they share assumptions.

## Trading modes use one risk vocabulary

Early dashboard sections described equivalent states inconsistently: “LIVE · Alpaca paper” appeared in green, “PAPER · EXECUTING” appeared in red, and “live · disarmed” appeared in lowercase. The dashboard now expresses operating state through two explicit dimensions: capital mode (`LIVE / PAPER / SHADOW`) and system behavior (`EXECUTING / ADVISORY / DISARMED`).

Color encodes financial risk, not activity or success. A service executing with real capital is always red, a connected but disarmed real account is amber, a paper account that is executing is blue, and everything else is neutral. Green is not used for any trading mode, because no mode is inherently good and green means "healthy" elsewhere in the interface. A label such as “LIVE” may describe current data, but it is never styled in a way that could be mistaken for a safety state.

## Calibrated values retain calibrated precision

The measured tail half-spread is 4.25¢. Rendering it as 4.3¢ changes a calibrated input and creates an apparent discrepancy between the interface and the underlying configuration. The cents formatter therefore preserves meaningful decimal places and removes only trailing zeros.

The same policy applies throughout the dashboard. Raw binary floating-point artifacts never reach the interface; an earlier order-book table once displayed `0.4200000000000001`, leading to a rule that all prices pass through domain-specific formatting. Strategy identifiers may be abbreviated in summary views only when the complete configuration remains accessible in one interaction.

## Logs prioritize the signal required by their audience

The shadow-testing log records every live, order-free prediction. One ensemble strategy, since retired, frequently recorded `PASS`, indicating that no position should be opened, and those events once represented approximately 80% of the raw public log. For an operator, pass frequency is a monitoring signal; for a public reader, it obscures executed decisions.

The private dashboard therefore defaults to the complete event stream, while the public view of the time defaulted to trade signals and provided a one-click control for revealing all predictions. Both surfaces used the same underlying data and explicitly identified any active filter.

## Analytical tables must expose their conclusions without horizontal discovery

At a common laptop width of 1,366 pixels, the shadow-testing table's win-rate, z-statistic, and P&L columns were positioned more than 300 pixels beyond the viewport without a visible indication that horizontal scrolling was available. Removing a redundant field, wrapping strategy identifiers, and introducing a denser spacing mode reduced the table from 1,699 pixels to 1,069 pixels. The columns that determine promotion eligibility are now visible at the screen width used for operational review.

## Static context must survive live-data failure

The public landing page renders its explanatory content without network requests. Live elements, including each book's P&L and summary statistics, remain absent until the data request succeeds; they do not produce blocking spinners or error cards. A transient backend failure can therefore suppress current numbers, but it cannot prevent a visitor from understanding what the platform is or how it operates. The demo pages in this repository follow the same rule: a missing figure removes its tile or sentence rather than breaking the page.
