# Chainlink-Confirmed FAK Paper TUI Bot Design

## 1. Goal

Build a continuously running, strictly paper-only Polymarket 5-minute Up/Down observer that:

- monitors BTC, ETH, and SOL markets in real time;
- models Polymarket FAK execution from the full live order book;
- compares entry thresholds of 80¢, 85¢, 89¢, and 90¢;
- compares no Chainlink filter, Chainlink direction, and Chainlink direction-plus-momentum confirmation;
- compares hold, immediate reverse, and Chainlink-confirmed reverse policies;
- presents live state and results in an attachable terminal dashboard;
- stores enough raw and derived data to replay revised rules later;
- cannot submit real orders or load trading credentials.

This phase does not claim profitability and cannot be promoted to live trading automatically. Any live-order capability requires a separate design and explicit user approval after the paper experiment.

## 2. Scope

### Included

- Current and next BTC/ETH/SOL 5m market discovery.
- Full Polymarket market-channel order books.
- Chainlink TWAP-60 through Polymarket RTDS.
- Exact paper FAK book walking with partial fills.
- A fixed pre-registered 36-lane experiment per asset.
- One optional reverse sequence per filled lane.
- SQLite operational persistence and compressed raw event journals.
- A read-only terminal dashboard.
- Strict Gamma settlement and fee accounting.
- Backup and removal of the old recorder stack before deployment.

### Excluded

- Private keys, API credentials, signatures, allowances, balances, or wallet access.
- Authenticated User WebSocket.
- `POST /order` or any other trading endpoint.
- Real orders of any size.
- Repeated switching, DCA, martingale sizing, or averaging.
- A browser dashboard.
- Automatic strategy promotion based on in-sample performance.

## 3. Architecture

### 3.1 `paper-engine`

A Dockerized daemon running continuously on the server. It owns market discovery, WebSocket/RTDS connections, full books, strategy state, virtual execution, settlement, event journaling, and SQLite writes.

Closing SSH or the dashboard must not stop the engine.

### 3.2 `paper-watch`

A separate read-only terminal application. It reads SQLite in WAL mode and displays live state. It does not control strategy state and has no network path capable of submitting orders.

Expected operator commands:

```text
./paper-bot start
./paper-bot status
./paper-watch
./paper-bot stop
```

### 3.3 Storage

- SQLite WAL: markets, resolver state, experiment definitions, signals, virtual orders, fill legs, inventory lots, settlements, health events, and lane statistics.
- Daily compressed raw journal: accepted Polymarket market-channel events and Chainlink RTDS messages with receive timestamps and source timestamps.
- Configuration hash and experiment version stored on every signal and position so results from changed rules cannot be mixed.

## 4. Public Data Sources

### 4.1 Gamma API

Discover current and next markets for each configured symbol. A market is accepted only when all of the following validate:

- slug exactly matches `{symbol}-updown-5m-{mkt_ts}`;
- `eventStartTime` equals `mkt_ts` and `endDate` equals `mkt_ts + 300`; the top-level Gamma `startDate` is listing metadata and is not the 5-minute interval boundary;
- outcomes contain one Up and one Down token;
- both token IDs are present and distinct;
- order book is enabled;
- tick size and minimum order size are valid;
- fee schedule is present when fees are enabled;
- `secondsDelay` is either omitted or numerically zero; delayed matching is unsupported in this paper phase and a non-zero value skips the market;
- `resolutionSource` exactly identifies the symbol-specific `data.chain.link` TWAP-60 stream; when present, Gamma crypto-market configuration must corroborate the symbol, 5-minute duration, and TWAP enablement.

The engine must fail closed for a market when validation fails.

### 4.2 Polymarket Market WebSocket

The engine subscribes only to public market data and maintains complete books per token:

- initial book snapshot establishes a generation;
- incremental updates apply only to the current generation;
- sequence/order validation rejects stale updates;
- reconnect invalidates all books until new snapshots arrive;
- signals are evaluated only on the specific accepted event that changes relevant executable prices or depth;
- PING/PONG and reconnect logic follow current official channel requirements.

### 4.3 Chainlink RTDS TWAP-60

For BTC/USD, ETH/USD, and SOL/USD:

- only monotonically newer observations are accepted;
- future or more-than-10-second-old observations are rejected before state mutation;
- each symbol has an independent 10-second freshness watchdog;
- stale data disables Chainlink-dependent lanes but does not stop Book-only lanes;
- reconnect and resubscribe occur when any configured symbol remains stale.

For each market, `resolver_start_twap` is the first fresh observation whose Chainlink timestamp lies within `[mkt_ts, mkt_ts + 10s]`. It is never reconstructed from Binance or a later fallback.

Derived values:

```text
resolver_distance = current_twap60 - resolver_start_twap
resolver_distance_bps = resolver_distance / resolver_start_twap * 10,000
resolver_leader = UP | DOWN | TIE
resolver_momentum_5s_bps = current_twap60 versus latest observation at least 5s earlier
```

If the start observation is unavailable, Direction and Confirmed lanes do not trade that market. Five-second momentum may still be displayed when fresh history exists.

## 5. Experiment Matrix

### 5.1 Assets

- BTC
- ETH
- SOL

Statistics remain separate by asset as well as aggregated.

### 5.2 Entry thresholds

- 0.80
- 0.85
- 0.89
- 0.90

### 5.3 Entry confirmations

1. `BOOK_ONLY`: no resolver condition.
2. `CHAINLINK_DIRECTION`: resolver leader equals the candidate side.
3. `CHAINLINK_CONFIRMED`: resolver leader equals the candidate side and 5-second momentum has the same non-zero sign.

### 5.4 Position policies

1. `HOLD`: no reverse.
2. `IMMEDIATE_REVERSE`: reverse on the eligible opposite book condition.
3. `CHAINLINK_REVERSE`: reverse only when the opposite book condition and opposite Chainlink confirmation are simultaneously true.

The product is 4 thresholds × 3 confirmations × 3 position policies = 36 lanes per asset.

Initial FAK execution is calculated once for identical threshold/confirmation signals and copied as immutable fill evidence into the three position-policy lanes.

## 6. Entry State Machine

### 6.1 Eligibility

```text
0 < seconds_to_close <= 150
```

Time comes from validated market timestamps and synchronized UTC system time.

### 6.2 Rising-cross trigger

For each side and threshold, a signal requires continuous valid book state and:

```text
previous_best_ask < threshold <= current_best_ask
```

Rules:

- the crossing must occur during the eligible window;
- simultaneous qualifying UP and DOWN crossings are ambiguous and skipped;
- each threshold/confirmation combination gets one entry attempt per market;
- no retries, repeated accumulation, or re-entry occur after that attempt;
- if the ask jumps above the lane's max price, the FAK receives zero fill and the attempt remains recorded as a no-fill.

### 6.3 Chainlink gating

- `BOOK_ONLY` evaluates with a valid current book only.
- `CHAINLINK_DIRECTION` additionally requires a fresh, non-tied resolver leader matching the candidate side.
- `CHAINLINK_CONFIRMED` additionally requires fresh 5-second momentum with the same sign.

### 6.4 Paper entry request

- Side: BUY.
- Requested amount: `PAPER_NOTIONAL_USD`, default `$5.00`.
- Order type: FAK.
- `max_price`: the lane threshold.
- The signed BUY target is derived from `makerAmount / max_price` with the official tick-dependent SDK precision; position size is the actual fill of that target.

`PAPER_NOTIONAL_USD` is configurable at startup, defaulting to `$5.00`. Changing it creates a new experiment version so results with different notionals are never mixed. Reports include both actual-dollar results and normalized PnL/EV per filled share.

## 7. FAK Simulation

The simulator follows the official Polymarket market-order semantics documented at:

- <https://docs.polymarket.com/trading/place-orders>
- <https://docs.polymarket.com/api-reference/trade/post-a-new-order>
- <https://docs.polymarket.com/concepts/order-lifecycle>
- <https://github.com/Polymarket/clob-client/blob/main/src/order-builder/helpers.ts>

### 7.1 Order construction and numeric rules

Use `Decimal` for all prices, sizes, quote amounts, fees, and PnL. Binary floating point never enters order construction, accounting, or persisted fill evidence.

The simulator reproduces the official market-order rounding table:

| Tick size | Price decimals | Direct size/amount decimals | Calculated counter-amount decimals |
| --- | ---: | ---: | ---: |
| `0.1` | 1 | 2 | 3 |
| `0.01` | 2 | 2 | 4 |
| `0.001` | 3 | 2 | 5 |
| `0.0001` | 4 | 2 | 6 |

- Prices must already align to the market tick; misaligned prices fail closed rather than being silently changed.
- The direct market-order amount is rounded down to two decimals.
- The calculated counter-amount follows the official SDK sequence: divide or multiply by the normalized price, round up to four guard decimals beyond the configured counter-amount precision, then round down to the configured counter-amount precision.
- Signed `makerAmount` and `takerAmount`, caller-requested amounts, fill legs, fees, and residuals are persisted as canonical six-decimal `Decimal` values.
- The market minimum share size is checked against the SDK-rounded submitted share amount before execution.

### 7.2 BUY FAK

BUY input amount is USDC notional and `max_price` is the signed order's worst-price limit.

1. Validate and sort asks from lowest to highest; ignore execution levels above `max_price`.
2. Round the requested USDC down to the two-decimal direct amount to obtain submitted `makerAmount`.
3. Derive submitted target shares (`takerAmount`) from `makerAmount / max_price` with the tick-dependent calculated precision.
4. Reject the order before execution when submitted target shares are below the market minimum.
5. Walk eligible asks at their actual resting prices and fill:

```text
fill_shares = min(level_shares, remaining_target_shares)
fill_quote = fill_shares * level_price
```

6. Stop when submitted target shares are filled or eligible liquidity ends. FAK cancels the remaining submitted target and every unspent part of the requested quote.

Price improvement always benefits the taker: an ask below `max_price` reduces actual USDC spent; it never increases shares beyond the signed `takerAmount`. For tick `0.01`, a `$5.00` BUY with `max_price=0.90` submits `5.5555` target shares. Against `0.89×3` and `0.90×5`, it fills `5.555500` shares for `$4.969950`, leaving `$0.030050` unspent.

Persist caller-requested quote, submitted maker/taker amounts, every actual-price fill leg, total quote spent, total shares, unfilled quote, and full/partial/zero-fill status.

### 7.3 SELL FAK

SELL input amount is shares and `min_price` is the signed order's worst-price floor.

1. Validate and sort bids from highest to lowest.
2. Round requested shares down to the two-decimal direct amount to obtain submitted `makerAmount`.
3. Derive the minimum submitted quote (`takerAmount`) from submitted shares times `min_price` with the tick-dependent calculated precision.
4. Reject the order before execution when submitted shares are below the market minimum.
5. Walk eligible bids down to `min_price`, filling at actual resting prices until submitted shares are filled or liquidity ends.
6. Retain FAK-unfilled shares and any caller-request dust excluded by two-decimal submission in the original inventory.

Persist caller-requested shares, submitted maker/taker amounts, every actual-price fill leg, shares sold, quote received, and exact unsold shares.

### 7.4 Fill status and price improvement

`full`, `partial`, and `zero` are derived from the submitted executable share target: BUY compares actual shares with submitted `takerAmount`; SELL compares actual shares with submitted `makerAmount`. A fully filled BUY can still have unspent quote because execution below `max_price` is price improvement, not a partial fill.

### 7.5 Fees

Fees use the market's validated Gamma fee schedule and apply only to actual filled quantities at actual level prices. The implementation must not hard-code the previously observed rate/exponent.

Each filled entry BUY, reverse SELL, and reverse BUY is accounted as a separate taker leg.

## 8. Reverse State Machine

A filled position can attempt at most one reverse sequence. No reverse occurs on the same event as the entry.

### 8.1 Opposite book eligibility

The opposite best ask must satisfy:

```text
0.89 <= opposite_best_ask <= 0.90
```

An ask above 0.90 cannot fill. If it later returns into the eligible range before close, it can trigger the still-unused reverse attempt.

### 8.2 Immediate reverse

`IMMEDIATE_REVERSE` executes on the first eligible opposite book event.

### 8.3 Chainlink-confirmed reverse

`CHAINLINK_REVERSE` executes when all are true on an accepted state:

- opposite ask is within 0.89–0.90;
- resolver observation is fresh;
- resolver leader is the opposite side;
- 5-second momentum has the opposite side's non-zero sign.

If the book enters the range first and Chainlink confirms later while the book remains eligible, the confirmation event triggers the sequence.

### 8.4 Non-atomic execution sequence

1. Submit/simulate an emergency SELL FAK for held old-side shares down to the minimum tick; official two-decimal direct-size rounding can leave inventory dust outside the submitted order.
2. Let `sold_shares` be the actual SELL fill.
3. Set opposite `max_price=0.90` and calculate the largest two-decimal BUY `makerAmount` whose SDK-derived `takerAmount` does not exceed `sold_shares`.
4. Optionally walk current opposite asks with `quote_for_target_shares` to record expected actual spend and liquidity, but never use expected spend as the signed BUY `makerAmount`.
5. Submit/simulate the BUY FAK with the calculated maker amount. Price improvement reduces actual spend without increasing the submitted opposite-share target.
6. Partial opposite fill is allowed; unfilled quote and any non-representable share dust remain explicit.

The reverse BUY's SDK-rounded target never exceeds shares actually sold. Any old-side residual, submission dust, and acquired opposite shares remain separate inventory lots through settlement.

The two FAKs are explicitly non-atomic. Paper records include trigger time, SELL book generation, BUY book generation, and elapsed wall time between legs. The dashboard and reports must not label the sequence as guaranteed execution.

## 9. Settlement and Accounting

Gamma is polled after market close. A market settles only when official resolved `outcomePrices` identify one unique winner and the market is closed/resolved. Provisional values such as 0.9995/0.0005 are displayed but not treated as final settlement.

For every lane:

```text
net_pnl = payouts
          + reverse_sell_proceeds
          - entry_buy_cost
          - reverse_buy_cost
          - all modeled taker fees
```

Report:

- caller-requested, SDK-submitted maker/taker, and actually filled amounts;
- price-improvement savings, submission-rounding dust, and partial-fill rates;
- initial side and final inventories;
- hold counterfactual;
- reverse incremental effect;
- false reverse count;
- rescued losses and harmed winners;
- per-market and cumulative net PnL;
- normalized EV per filled share;
- win rate, profit factor, and maximum drawdown.

Open/unresolved markets do not enter final WR or settled PnL denominators.

## 10. Persistence Model

Minimum SQLite entities:

- `experiment_versions`
- `markets`
- `tokens`
- `resolver_observations`
- `book_generations`
- `signals`
- `paper_orders`
- `paper_fill_legs`
- `inventory_lots`
- `reverse_sequences`
- `settlements`
- `lane_results`
- `health_events`

Required invariants:

- unique signal key by experiment, lane, market, and entry/reverse phase;
- virtual order idempotency across restart;
- filled shares equal the sum of persisted fill legs;
- inventory cannot become negative;
- settlement is idempotent;
- strategy- or execution-affecting configuration changes create a new experiment version and never rewrite old results.

Raw journals rotate daily and are compressed after close. No automatic deletion occurs during the initial seven-day experiment. Disk usage is monitored and surfaced in TUI health.

## 11. Terminal Dashboard

### 11.1 Market panel

For BTC/ETH/SOL:

- market slug and countdown;
- UP/DOWN best bid/ask and top depth;
- current book generation and connection health;
- threshold-cross indicators.

### 11.2 Resolver panel

- start TWAP-60;
- current TWAP-60;
- distance and distance bps;
- leader;
- 5-second momentum;
- observation age and stale status.

### 11.3 Strategy matrix

Filterable by asset, threshold, Chainlink variant, and position policy. Columns:

- signals;
- zero/partial/full FAK fills;
- resolved trades;
- wins/losses;
- net PnL;
- EV/share;
- drawdown;
- reverse attempts and completion.

### 11.4 Open positions

- lane and side;
- requested versus filled size;
- fill VWAP;
- old/new inventory after reverse;
- cash flow;
- projected payout scenarios.

### 11.5 Event and health panels

- triggers and skip reasons;
- fill legs;
- reverse lifecycle;
- settlements;
- reconnects, stale feeds, invalid books, database lag, journal errors, and disk usage.

The dashboard is read-only and can be opened or closed without changing engine behavior.

## 12. Failure Handling

- Gamma validation failure: skip market and log structured reason.
- Market WS disconnect: invalidate books, reconnect, resubscribe, wait for snapshots.
- RTDS stale symbol: disable Chainlink lanes for that symbol, reconnect, retain Book-only operation when the market book remains valid.
- SQLite transient lock: bounded retry; engine pauses signal creation rather than dropping orders/fills.
- Journal write failure or low disk: stop creating new virtual positions, retain monitoring, emit critical health state.
- Process restart: reconcile persisted open paper positions and current market state before accepting new signals.
- Settlement unavailable: retry with bounded backoff; do not infer from recorder or price trend.

## 13. Testing

### 13.1 Unit tests

- BUY and SELL FAK full, partial, and zero fills across multiple levels.
- Tick-dependent SDK maker/taker rounding, minimum-size checks on submitted shares, price improvement, six-decimal persistence, and fee calculations.
- Rising crossing, gaps, stale generations, simultaneous sides, and one-attempt rules.
- Chainlink freshness, first-start capture, direction, tie, and momentum filters.
- Immediate and confirmed reverse state machines.
- Partial SELL followed by BUY of only sold shares.
- Inventory and PnL reconciliation.

### 13.2 Replay tests

Deterministic raw-event fixtures cover:

- normal win;
- ordinary loss;
- successful reverse;
- false reverse;
- partial entry;
- partial exit;
- ask jump over limit;
- disconnect during an open position;
- restart before settlement.

### 13.3 Safety tests

- repository contains no private key/API secret configuration fields;
- engine starts without credentials;
- no authenticated WebSocket client exists;
- no code path can call `POST /order`;
- network allowlist permits only documented public GET/WebSocket endpoints used by this phase.

### 13.4 Integration verification

- local test suite and static compilation;
- Docker image import/smoke;
- 30-minute server paper smoke;
- dashboard attach/detach while engine continues;
- forced WS/RTDS reconnect;
- restart/idempotency check;
- artifact/database reconciliation.

## 14. Server Cleanup and Deployment

Target server: `158.178.155.78`.

The cleanup scope is the old recorder stack, not the operating system, SSH configuration, or unrelated host resources.

Required sequence:

1. Stop the old `pm-recorder` service to create a fixed backup boundary.
2. Create a manifest of `/opt/recorder/data` containing every path, byte size, and SHA-256.
3. Create a compressed archive and archive SHA-256.
4. Download archive and manifest to `/home/alex/Project/up_down/backups/recorder/<UTC-timestamp>/` on the local machine.
5. Verify local archive hash, extractability, file count, total bytes, and every file hash against the manifest.
6. Only after verification, remove old recorder containers, recorder-specific images/volumes, `/opt/recorder`, and temporary recorder build artifacts.
7. Confirm no old recorder process/container/path remains.
8. Deploy the new stack under `/opt/paper-bot`.
9. Start only `paper-engine`; confirm no credential variables are present.
10. Run health/reconnect/database checks, then attach `paper-watch`.
11. Observe a minimum 30-minute smoke before declaring deployment healthy.

No broad `docker system prune` or deletion of unrelated resources is permitted.

## 15. Evaluation Protocol

The initial fixed experiment runs continuously for at least seven days. Rules are not edited in place. Any change creates a new experiment version and resets forward validation for that version.

Evaluation remains separated by asset and includes aggregated results only as a secondary view.

Primary comparisons:

- each threshold with Book-only versus Chainlink Direction versus Chainlink Confirmed;
- Hold versus Immediate Reverse versus Chainlink Reverse;
- net PnL after modeled fees;
- fill quality and partial-fill impact;
- Chainlink lead/lag relative to opposite 89–90¢ book entry;
- reverse benefit versus false-reverse cost;
- maximum drawdown and worst individual market.

A lane is not considered a live candidate merely because cumulative PnL is positive. At minimum it needs:

- positive out-of-sample net EV after fees;
- sufficient independent resolved base markets, not inflated correlated lane counts;
- stable results across more than one asset or an explicitly validated single-asset restriction;
- acceptable drawdown and false-reverse tail risk;
- operationally credible FAK depth/partial-fill behavior.

Live trading, wallet integration, authenticated User WebSocket, and real execution remain prohibited until a separate reviewed specification is approved.

## 16. Official References

- Gamma market response schema: <https://docs.polymarket.com/api-reference/markets/list-markets>
- Place Orders and market-order/FAK semantics: <https://docs.polymarket.com/trading/place-orders>
- Order lifecycle and taker price improvement: <https://docs.polymarket.com/concepts/order-lifecycle>
- Post New Order API response semantics: <https://docs.polymarket.com/api-reference/trade/post-a-new-order>
- Official TypeScript order rounding helpers: <https://github.com/Polymarket/clob-client/blob/main/src/order-builder/helpers.ts>
- Market WebSocket channel: <https://docs.polymarket.com/market-data/websocket/market-channel>
- Chainlink TWAP through RTDS: <https://docs.polymarket.com/market-data/chainlink-twap>
- Official Python CLOB client reference: <https://github.com/Polymarket/py-clob-client>
