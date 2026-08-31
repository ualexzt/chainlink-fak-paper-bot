# Chainlink-native paper backtest — design

**Date:** 2026-08-31  
**Status:** Approved for implementation

## Goal

Evaluate the Chainlink-native directional paper-taker hypothesis on the recorder data stored on the production recorder VM. The test must answer whether buying the currently leading UP/DOWN contract at a sufficiently large discount to the Chainlink-based fair value has positive net expectancy after the existing taker-fee model.

This is a quote-based paper backtest. It does not claim that an order was actually executed.

## Data source and isolation

- Read-only input: `/opt/recorder/data/*.jsonl` on the recorder server.
- Do not stop, restart, or modify the recorder container or its data files.
- Process the files in place over SSH to avoid downloading the 1.6 GB dataset.
- The same analyzer accepts a local `--data-dir` for repeatability.
- Input rows are keyed by `(symbol, tf, mkt_ts, ts)`; the recorder emits BTC, ETH, and SOL with 5m and 15m windows.
- The current LUT is enabled only for 15m (`fair_leader_lut` is absent for 5m), so 15m is the primary tested timeframe. 5m rows are still audited and reported as unavailable for this run rather than scored with a mismatched 15m LUT.

## Signal definition

For each market `(symbol, tf, mkt_ts)` and each chronological row:

1. Ignore rows before `age >= 59`, rows at/after the window length, or rows missing `leader_cl`, `fair_leader_lut`, or `dist_cl_bps`.
2. Require `abs(dist_cl_bps) >= floor`; `leader_cl` already determines whether UP or DOWN is leading.
3. Select the leader's best ask: `up_ask` for UP, `dn_ask` for DOWN.
4. Require a valid ask in `(0, 1)`.
5. A candidate entry exists when `ask <= fair_leader_lut - margin`.
6. Take only the first qualifying entry per market. Position size is one share and it is held to settlement.

The primary sweep uses:

- `margin`: 3c, 4c, and 6c;
- `floor`: 3bps, 8bps, and 15bps.

## Settlement and PnL

The recorder's existing resolution model is used: the strike is the first valid `strike_cl` captured at age 59, the final value is `cl_twap60` at exactly `age == window_seconds - 1`, and UP wins when `final_cl_twap60 >= strike_cl` (ties follow the existing model). Markets without a valid exact final row are reported as incomplete and excluded from win-rate/PnL denominators.

For entry price `p`, use the existing research fee model:

```text
fee(p) = 0.10 * min(p, 1 - p)
```

For one share:

```text
win:  1 - p - fee(p)
loss: -p - fee(p)
```

No maker rebate, queue priority, full-depth liquidity, or assumed price improvement is included in this primary test.

## Outputs

Produce:

1. A compact run summary for every margin/floor combination:
   - markets seen, complete markets, incomplete markets;
   - signal candidates and accepted fills;
   - wins, win rate and confidence interval;
   - average entry/fair value, EV/share, total PnL/share;
   - counts of missing quote/fair/settlement fields.
2. Breakdowns by symbol, timeframe, and UTC day.
3. A small per-fill CSV/JSON artifact containing parameter configuration, market key, entry timestamp, age, leader, ask, fair value, distance, strike, final Chainlink value, winner, fee, and PnL.
4. A data-quality section reporting malformed rows, duplicate market/timestamp rows, and row/quote coverage. It must distinguish missing quotes from zero-valued quotes.

## Architecture

Create a standalone standard-library analyzer under `research/` so it can stream multi-gigabyte JSONL files without loading the complete dataset. Keep parsing, market settlement reconstruction, signal selection, fee/PnL calculation, and reporting as separate functions. The command must support:

```text
python research/backtest_live_recorder.py --data-dir PATH --out-dir PATH
```

The first production run will execute the analyzer against `/opt/recorder/data` over SSH and copy back only the compact report/artifacts. It must not write into `/opt/recorder/data`.

## Validation

Before the production run, unit tests will cover:

- selecting UP versus DOWN ask;
- rejecting pre-strike, post-expiry, null, invalid, and insufficient-distance rows;
- first-fill-only behavior per market;
- exact settlement/tie behavior and incomplete-market exclusion;
- fee/PnL arithmetic for wins and losses;
- independent results for all 9 parameter combinations, including zero-fill and unavailable-timeframe cases.

The final report must explicitly label this as a paper result and state the limitations of seven calendar days, best-quote-only data, and inferred settlement.
