# 90¢ Late-Window Single-Switch Backtest Design

## Goal

Measure whether a 5-minute BTC/ETH/SOL strategy improves by entering the first side that rises to 90¢ in the final 150 seconds, then performing at most one full position switch when the opposite side subsequently rises to 90¢.

## Scope

Paper-only historical simulation over recorder one-second top-of-book snapshots. Official resolved Gamma `outcomePrices` remain the only settlement source. Chainlink confirmation is excluded from this historical run because recorder `cl_obs_ts` was demonstrably stale and RTDS has no public history/replay.

## Entry

- Universe: BTC, ETH, SOL 5m Up/Down markets.
- Position size: 50 shares.
- Eligible time: `age >= 150` and `age < 300`.
- Trigger: first unambiguous rising crossing from `< 0.90` to `>= 0.90` by one side after eligibility begins.
- Crossing continuity requires paired valid books and consecutive timestamps with no gap over two seconds.
- A market where both sides cross on one row is ambiguous and skipped.
- A crossing above 90¢ is a signal but cannot fill the 90¢ limit; no market order above the limit is modeled.
- Actual initial fill price is exactly 90¢.

## Execution Models

### Strict 50-share

A 90¢ initial fill requires displayed top-ask quantity `>= 50`. A switch requires both held-side top-bid quantity `>= 50` and opposite-side 90¢ top-ask quantity `>= 50`.

### Optimistic touch

The same full 50 shares are modeled at top price whenever each required displayed quantity is merely positive. This is explicitly an optimistic upper bound because the recorder has no queue position, order acknowledgements, or sub-second book path.

## Switch

- At most one switch per market.
- The trigger is the first later unambiguous rising crossing of the opposite ask from `< 0.90` to `>= 0.90`.
- The switch cannot occur on the initial-entry row.
- It executes atomically in the model as:
  1. taker SELL all 50 held shares at the current held-side best bid;
  2. taker BUY 50 opposite shares at 90¢.
- If the opposite ask jumps above 90¢ or required depth is absent, that crossing is not filled. A later new rising crossing may fill.
- No second switch or averaging is permitted.

## Accounting

Use each market's official Gamma fee schedule and `fee = rate × (price × (1-price))^exponent` per share on initial BUY, switch SELL, and switch BUY.

For a switched trade:

`net = -initial_buy - initial_fee + switch_sell - sell_fee - reverse_buy - reverse_buy_fee + final_payout`

The report compares this with the counterfactual of holding the identical initial fill to settlement. It reports total and per-fill PnL for 50 shares, incremental switch effect, switch frequency, saved initial losers, harmed initial winners, symbol breakdown, and execution-model sensitivity.

## Outputs

- `research/backtest_90cent_switch_5m.py`
- `research/test_backtest_90cent_switch_5m.py`
- `research/results/90cent_switch_5m/summary.json`
- `research/results/90cent_switch_5m/trades.csv`
- `research/results/90cent_switch_5m/FINAL_REPORT.md`

## Limitations

One-second snapshots cannot prove an atomic sell-and-buy switch, full top-level execution, ordering within a second, or queue position. Strict top depth is necessary but not sufficient for a real fill. The historical run does not use stale Chainlink values.
