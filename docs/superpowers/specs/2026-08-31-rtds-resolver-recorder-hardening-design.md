# RTDS Resolver Recorder Hardening Design

## Goal

Make future recorder data reliable enough to test whether Chainlink TWAP-60 leads Polymarket reversal signals.

## Confirmed Problem

The public Polymarket RTDS stream independently delivered BTC/ETH/SOL Chainlink TWAP-60 updates roughly every second with 2–3 second observation age. At the same time, the running recorder persisted a fixed `cl_obs_ts` more than 332 seconds old. Its WebSocket stayed open and received control/unrelated frames, but `rtds_task` had no valid-data freshness watchdog and therefore did not reconnect.

RTDS provides no public history or replay. No direct Chainlink Data Streams credentials are configured.

## RTDS Watchdog

- Add configurable `RTDS_STALE_SEC`, default 10 seconds.
- A connection must receive a valid TWAP-60 update for every configured symbol within the startup grace interval.
- Only a valid selected-symbol TWAP-60 observation refreshes that symbol's watchdog; PONG, empty, malformed, unrelated-symbol, spot, and TWAP-30 frames do not.
- If any configured symbol is not refreshed within the threshold, close the socket, log the stale symbols, and reconnect/resubscribe through the existing retry loop.
- Ignore an observation timestamp older than the cached timestamp so stale/out-of-order messages cannot regress state.

## Resolver Fields

For each aggregate row record:

- `cl_age_ms`: row time minus TWAP-60 observation time;
- `cl_fresh`: `0 <= cl_age_ms <= RTDS_STALE_SEC * 1000`;
- `resolver_start_twap`: first fresh TWAP-60 observation whose observation timestamp is within `[mkt_ts, mkt_ts + 10s]`;
- `resolver_distance`: fresh current TWAP-60 minus `resolver_start_twap`;
- `resolver_distance_bps`: distance relative to the start value;
- `resolver_leader`: `UP`, `DOWN`, or `TIE`;
- `resolver_momentum_5s_bps`: current TWAP-60 versus the latest retained observation at least five seconds earlier.

Do not label the locally captured value as official Gamma `priceToBeat`; Gamma live responses expose the Chainlink resolution source and `cryptoMarketConfig`, but not a numeric live price-to-beat field. If the recorder starts late or misses the first ten seconds, resolver fields depending on the start remain null rather than using a fabricated value.

## State and Retention

- Keep a bounded per-symbol TWAP-60 history sufficient for five-second momentum.
- Keep per-market resolver start values while the market can still produce rows.
- Clean resolver start state with the same stale-market lifecycle as market metadata.

## Deployment and Validation

- Add deterministic unit tests for watchdog staleness, out-of-order updates, start capture window, freshness, leader/distance, and five-second momentum.
- Run recorder tests and compilation locally.
- Build and restart only `pm-recorder`; do not place orders or modify trading services.
- Confirm the container is healthy, heartbeat advances, persisted `cl_age_ms` stays within threshold, observation timestamps advance, and new resolver fields are present.
- Preserve existing JSONL fields for compatibility.
