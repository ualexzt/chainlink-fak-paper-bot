# Pair-quote feasibility scan

**Snapshot:** recorder data on `158.178.155.78`, scanned 2026-08-31 around 09:50 UTC. This is a separate live-growing snapshot from the settlement PnL run.

## Rule

For every valid one-second snapshot:

```text
q_up   = floor_to_tick(mid_up   - $0.01)
q_down = floor_to_tick(mid_down - $0.01)
```

A quote is usable for 50 shares when each price is in `(0, $1)` and has at least `$5` notional. The pair is safe when `q_up + q_down <= $1`.

## Results

- 2,970,918 physical/valid rows
- 6,615 markets with paired books
- 2,430,347 snapshots with both valid books
- 1,853,166 snapshots with usable 50-share quotes
- **1,853,166 / 1,853,166 (100%)** had `q_up + q_down <= $1`
- Pair-sum range: **$0.97–$0.98**; median **$0.97**
- 99.94% of usable safe quotes were also within the current 1.5¢ reward distance on both legs
- 20,704 contiguous safe runs lasted at least 2 seconds; 17,354 lasted at least 5 seconds
- Across all runs, median duration was 30 seconds and maximum was 603 seconds
- Median of each market's longest run was 158 seconds

The scan therefore confirms that a synchronized pair quote with a 2–3¢ gross edge per paired unit is commonly available in this recorder data.

## Important limitation

This tests **quote availability**, not network/API order placement. Recorder resolution is one second and contains no order acceptance timestamps, queue position, or real fills. Polymarket's batch `POST /orders` supports 1–15 orders and processes them in parallel, but the official API documentation gives no latency or atomicity guarantee.

The earlier negative PnL run independently re-quoted the still-open leg after the first leg filled. A production-safe pair strategy must preserve the pair-cost lock after every partial/first fill.
