# Paired UP/DOWN maker-rewards backtest — design

**Date:** 2026-08-31  
**Status:** Approved for implementation by user

## Objective

Test the proposed market-neutral maker strategy on the recorder snapshot:

- submit `BUY UP` for 50 shares;
- submit `BUY DOWN` for 50 shares;
- price each bid at one cent below that outcome's current midpoint, rounded down to the 0.01 tick;
- keep/requote resting orders while the market is active;
- model fills, residual one-leg risk, paired settlement PnL, and qualifying liquidity-reward time.

The test must separate deterministic pair economics from uncertain program payouts. It must not invent a dollar value for liquidity rewards when the historical reward pool and competing-maker denominator are not present in recorder rows.

## Official conditions captured

The current Gamma market metadata for BTC/ETH/SOL crypto 5m/15m markets reports `rewardsMinSize=50`, `rewardsMaxSpread=1.5` cents, `orderPriceMinTickSize=0.01`, and `orderMinSize=5`. A 50-share quote at one cent from midpoint is within these current displayed thresholds. Maker rebate/reward eligibility still depends on the market's live configuration and actual resting/executed liquidity.

## Quote and lifecycle model

For each `(symbol, tf, mkt_ts)` market with valid best bid/ask on both complementary books:

1. At the first valid row at/after age 0, create one 50-share UP bid and one 50-share DOWN bid.
2. A quote is `floor((midpoint - 0.01) / 0.01) * 0.01`, clamped to `(0, 1)`.
3. At each later one-second observation, test the previous resting quote against the current ask before replacing it. A leg fills when `current_ask <= previous_bid`; filled quantity is `min(remaining_order, current_ask_quantity)`.
4. Requote unfilled remainder from the current midpoint after fill testing. A post-only order is assumed, so it never intentionally crosses the current ask.
5. Do not replenish a leg after it fills. Once both 50-share legs are filled, the pair is locked and no further orders are simulated for that market.
6. If a quote/book is missing, do not create a new quote; existing resting state remains, but no fill or reward score is credited for that missing observation.
7. Any remainder is settled at the market's exact terminal row. This exposes the one-leg directional risk when only one order fills.

This is a quote-touch paper model, not proof of actual execution: the recorder has no trade prints, queue position, or order identifiers.

## Settlement and economics

Use the recorder's resolution model: strike is `strike_cl` captured from age 59, final is `cl_twap60` at `age == window_seconds - 1`, and UP wins when final >= strike.

For each filled quantity:

- cost is `quantity * fill_price`;
- at settlement, each winning share pays $1 and each losing share pays $0;
- paired locked gross PnL is `50 * (1 - up_avg_fill - down_avg_fill)` when both legs are full;
- unpaired residuals are valued by the actual inferred winner;
- maker fees are zero under the current `takerOnly` schedule;
- maker rebates are reported as eligible filled volume, not assumed cash, because the program pays pro-rata per market and the recorder lacks the historical denominator;
- liquidity rewards are reported as qualifying seconds and a normalized relative score only. No fixed reward dollars are added.

## Reward eligibility metrics

At each valid observation, count a quote as threshold-eligible when each active remaining order is at least 50 shares and its distance from the relevant midpoint is <= 1.5 cents. Count two-sided eligibility only when both UP and DOWN orders qualify. A relative score uses the official quadratic shape with `v=1.5c` and the observed distance, omitting the unknown market multiplier and normalization denominator.

## Outputs

Produce:

1. Pair-level CSV with market key, quote times, fill quantities/prices/times, paired/unpaired status, settlement winner, costs, payout, gross PnL, eligible seconds, and relative reward score.
2. Summary JSON/Markdown with:
   - markets observed/completed/incomplete;
   - order and fill counts by leg;
   - both-leg, one-leg, and zero-leg rates;
   - pair gross PnL and residual directional PnL;
   - capital/exposure maxima;
   - qualifying reward seconds/score;
   - breakdowns by symbol, timeframe, and UTC day;
   - official threshold assumptions and all limitations.
3. Sensitivity for a no-queue quote-touch model versus a stricter full-50-share fill model, if both can be computed without duplicating input parsing.

## Validation

Unit tests must cover tick rounding, simultaneous initial quoting, fill-before-requote ordering, partial fills, no replenishment, both-leg locked PnL, one-leg settlement loss/win, exact tie settlement, missing terminal rows, reward threshold eligibility, and zero/one/two-leg classifications.
