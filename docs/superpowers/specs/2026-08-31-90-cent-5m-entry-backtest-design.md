# 5m 90-Cent Entry Backtest Design

## Goal

Measure the settlement win rate of buying the first 5-minute UP/YES or DOWN/NO token that rises to the 90-cent region. Compare immediate taker execution with a post-trigger maker pullback entry.

## Scope

- Recorder data from `/opt/recorder/data` on `158.178.155.78`.
- Only complete 5-minute BTC, ETH, and SOL markets.
- Independent threshold runs at 88¢, 89¢, 90¢, 91¢, and 92¢.
- At most one selected side and one fill per market per threshold/variant.
- Paper backtest only; no real orders.

## Settlement

Use official resolved Gamma `outcomePrices` for each market slug (`{symbol}-updown-5m-{mkt_ts}`):

- the outcome whose resolved price is `1` is the winner;
- require a recorder age-299 row and a resolved Gamma outcome;
- markets missing either are incomplete and excluded from win-rate and PnL denominators.

The original recorder Chainlink fallback was rejected during validation: 3,878 of 5,085 complete markets had `terminal_cl == strike_cl`, and its inferred winner disagreed with official Gamma outcomes in 2,126 markets.

## Rising-Cross Trigger

A side triggers only when its valid best ask moves from strictly below the threshold on the previous valid paired-book observation to greater than or equal to the threshold on the current observation. This avoids the incorrect rule `ask <= 0.90`, which would enter immediately near market open.

If both sides trigger at the same timestamp, mark the market ambiguous and skip it. Missing book snapshots break crossing continuity; the next valid observation establishes a new baseline but cannot itself trigger.

## Variant A: Taker

At the first rising-cross trigger in a market:

1. Buy the triggering side at the current recorded best ask.
2. Require a positive recorded ask quantity.
3. Hold to settlement.
4. Do not trade the other side.

The observed ask may be above the threshold because recorder resolution is one second. Report the actual entry-price distribution. Gross settlement PnL per share is `1 - entry` for a win and `-entry` for a loss. Net PnL uses each resolved market's Gamma `feeSchedule`: `rate × (price × (1-price))^exponent`; all tested markets reported rate `0.07`, exponent `1`, and taker-only fees.

## Variant B: Maker Pullback

At the first rising-cross trigger in a market:

1. Select the triggering side and place a simulated post-only bid one tick below the threshold (`threshold - $0.01`; therefore 89¢ for the 90¢ trigger).
2. The order remains active until market end.
3. Count an optimistic quote-touch fill only on a later observation when best ask is at or below the bid and positive ask quantity exists.
4. Fill at the limit price, hold to settlement, and do not trade the other side.
5. Never reprice the order.

Maker fee is modeled as zero. This is an execution upper bound because recorder data lacks queue position, trade prints, and real order acknowledgements.

## Outputs

For every threshold and variant:

- complete markets, signals, fills, no-fills, and fill rate;
- wins, losses, win rate, and 95% Wilson confidence interval;
- average/min/max entry price;
- gross and modeled net PnL per share and total for one share per fill;
- break-even win rate at the average entry;
- breakdown by BTC/ETH/SOL and by UTC day;
- trigger age and maker time-to-fill distributions;
- ambiguous and malformed-event counters.

Write:

- `research/backtest_90cent_5m.py`
- `research/test_backtest_90cent_5m.py`
- `research/results/90cent_5m/summary.json`
- `research/results/90cent_5m/trades.csv`
- `research/results/90cent_5m/FINAL_REPORT.md`

## Validation

- Unit tests for rising-cross semantics, missing-snapshot continuity, taker fill, maker later-touch fill, no same-row maker fill, one-side-only behavior, settlement, and incomplete-market exclusion.
- Synthetic end-to-end fixture validating generated summary and trade rows.
- Run all new tests and existing recorder-backtest tests.
- Reconcile aggregate counts against the generated trade artifact before reporting results.

## Limitations

- One-second snapshots cannot measure sub-second crossing, slippage, API latency, or queue position.
- Variant A assumes execution at the observed best ask with recorded quantity available.
- Variant B quote-touch fills are optimistic and are not proof of actual maker execution.
- Settlement and PnL results are historical paper estimates, not live-trading guarantees.
