# Chainlink-Confirmed FAK Paper TUI Bot Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build and deploy a strictly paper-only BTC/ETH/SOL 5m engine with exact full-book FAK simulation, 36 pre-registered strategy lanes per asset, Chainlink TWAP-60 confirmation, durable SQLite/raw journals, and an attachable terminal dashboard.

**Architecture:** A continuously running `paper-engine` consumes validated public Gamma, Polymarket Market WebSocket, and Chainlink RTDS data, evaluates immutable experiment lanes, simulates partial FAK legs with `Decimal`, and persists idempotent state. A separate read-only `paper-watch` process renders SQLite state through Rich. There is no authenticated client, credential configuration, or order-submission path.

**Tech Stack:** Python 3.12+, stdlib `unittest`, `asyncio`, `sqlite3`, `Decimal`; `aiohttp`, `websockets`, `rich`, `zstandard`; Docker Compose; SQLite WAL.

**Approved design:** `docs/superpowers/specs/2026-08-31-chainlink-fak-paper-tui-bot-design.md`

---

## Execution and commit protocol

1. Create branch `feat/chainlink-fak-paper-bot` in worktree `/home/alex/Project/up_down/.worktrees/chainlink-fak-paper-bot` using the `using-git-worktrees` skill.
2. Set `PY=/home/alex/Project/up_down/.venv/bin/python` and run all commands from the worktree root.
3. Use one low-cost `delegate`/worker subagent at a time. Each worker receives only the approved spec and one task, edits only listed files, runs targeted tests, and does not commit or deploy.
4. The parent reviews every diff, reruns targeted and cumulative tests, checks safety invariants, then creates the task's commit.
5. Never run concurrent writers in the same worktree.
6. Reviewer agents remain read-only. Only the parent may merge, back up/delete server state, or deploy.
7. No task may introduce a private key, API credential, authenticated User WebSocket, generic HTTP `request()` method, or POST-capable client.

## Planned package layout

```text
paper/
├── .env.example
├── Dockerfile
├── docker-compose.yml
├── requirements.txt
├── paper_bot/
│   ├── __init__.py
│   ├── accounting.py
│   ├── books.py
│   ├── cli.py
│   ├── config.py
│   ├── domain.py
│   ├── engine.py
│   ├── fak.py
│   ├── gamma.py
│   ├── journal.py
│   ├── market_ws.py
│   ├── resolver.py
│   ├── rtds.py
│   ├── settlement.py
│   ├── storage.py
│   ├── strategy.py
│   └── tui.py
├── scripts/
│   ├── backup_recorder.py
│   ├── paper-bot
│   ├── paper-watch
│   └── security_scan.py
└── tests/
    ├── __init__.py
    ├── fixtures/
    │   ├── gamma_market.json
    │   ├── market_ws_replay.jsonl
    │   └── rtds_replay.jsonl
    ├── test_accounting.py
    ├── test_books.py
    ├── test_config_safety.py
    ├── test_engine_replay.py
    ├── test_fak.py
    ├── test_gamma.py
    ├── test_journal.py
    ├── test_market_ws.py
    ├── test_resolver.py
    ├── test_reverse.py
    ├── test_settlement.py
    ├── test_storage.py
    ├── test_strategy.py
    └── test_tui.py
```

---

### Task 1: Paper-only package scaffold and security boundary

**Files:**
- Create: `paper/requirements.txt`
- Create: `paper/paper_bot/__init__.py`
- Create: `paper/paper_bot/config.py`
- Create: `paper/paper_bot/domain.py`
- Create: `paper/scripts/security_scan.py`
- Create: `paper/tests/__init__.py`
- Create: `paper/tests/test_config_safety.py`
- Modify: `.gitignore`

- [ ] **Step 1: Write failing configuration and safety tests**

Create tests with these exact behaviors:

```python
class ConfigSafetyTests(unittest.TestCase):
    def test_default_settings_define_fixed_experiment(self):
        settings = load_settings({})
        self.assertEqual(settings.symbols, ("btc", "eth", "sol"))
        self.assertEqual(settings.thresholds, tuple(map(Decimal, ("0.80", "0.85", "0.89", "0.90"))))
        self.assertEqual(settings.paper_notional_usd, Decimal("5.00"))
        self.assertEqual(settings.rtds_stale_seconds, Decimal("10"))

    def test_forbidden_credentials_fail_closed(self):
        with self.assertRaisesRegex(ValueError, "forbidden credential"):
            load_settings({"PRIVATE_KEY": "not-allowed"})

    def test_only_public_endpoints_are_configurable(self):
        settings = load_settings({})
        self.assertEqual(settings.gamma_url, "https://gamma-api.polymarket.com")
        self.assertEqual(settings.market_ws_url, "wss://ws-subscriptions-clob.polymarket.com/ws/market")
        self.assertEqual(settings.rtds_url, "wss://ws-live-data.polymarket.com")
```

The security scanner test invokes `paper/scripts/security_scan.py` against `paper/paper_bot` and expects exit zero. Add a negative fixture in a temporary directory containing `PRIVATE_KEY=x` and `client.post("/order")`; expect non-zero without printing the value.

- [ ] **Step 2: Verify RED**

Run:

```bash
PYTHONPATH=paper $PY -m unittest paper.tests.test_config_safety -v
```

Expected: import failure because `paper_bot.config` and scanner do not exist.

- [ ] **Step 3: Implement the minimal safe scaffold**

Set `paper/requirements.txt` to the complete runtime dependency set:

```text
aiohttp>=3.9,<4
websockets>=12,<16
rich>=13.7,<15
zstandard>=0.22,<1
```

Implement immutable settings and public domain enums:

```python
FORBIDDEN_ENV_PARTS = ("PRIVATE_KEY", "API_KEY", "API_SECRET", "PASSPHRASE", "CREDENTIAL")

@dataclass(frozen=True)
class Settings:
    symbols: tuple[str, ...]
    thresholds: tuple[Decimal, ...]
    paper_notional_usd: Decimal
    rtds_stale_seconds: Decimal
    data_dir: Path
    gamma_url: str
    market_ws_url: str
    rtds_url: str


def load_settings(env: Mapping[str, str]) -> Settings:
    forbidden = sorted(key for key in env if any(part in key.upper() for part in FORBIDDEN_ENV_PARTS))
    if forbidden:
        raise ValueError("forbidden credential environment keys: " + ",".join(forbidden))
    return Settings(
        symbols=tuple(x.strip().lower() for x in env.get("SYMBOLS", "btc,eth,sol").split(",")),
        thresholds=tuple(Decimal(x) for x in env.get("ENTRY_THRESHOLDS", "0.80,0.85,0.89,0.90").split(",")),
        paper_notional_usd=Decimal(env.get("PAPER_NOTIONAL_USD", "5.00")),
        rtds_stale_seconds=Decimal(env.get("RTDS_STALE_SEC", "10")),
        data_dir=Path(env.get("DATA_DIR", "/data")),
        gamma_url="https://gamma-api.polymarket.com",
        market_ws_url="wss://ws-subscriptions-clob.polymarket.com/ws/market",
        rtds_url="wss://ws-live-data.polymarket.com",
    )
```

`security_scan.py` must scan filenames and text/AST for forbidden assignments, credential field names, `post_order`, `create_order`, `create_market_order`, `POST /order`, authenticated User WebSocket paths, and generic `aiohttp.ClientSession.request/post/put/patch/delete` calls. It prints only path, line, and rule name—never matching content.

Add these ignores:

```text
/paper/runtime/
/paper/.env
/paper/**/*.db*
/paper/**/*.jsonl.zst
```

- [ ] **Step 4: Verify GREEN and parent review**

Run:

```bash
PYTHONPATH=paper $PY -m unittest paper.tests.test_config_safety -v
$PY paper/scripts/security_scan.py paper/paper_bot
$PY -m py_compile paper/paper_bot/*.py paper/tests/test_config_safety.py
```

Expected: all tests pass, scanner and compilation exit zero. Parent checks that no dependency can place orders.

- [ ] **Step 5: Commit**

```bash
git add .gitignore paper
git commit -m "chore(paper): establish paper-only safety boundary"
```

---

### Task 2: Decimal domain model and exact FAK simulator

**Files:**
- Modify: `paper/paper_bot/domain.py`
- Create: `paper/paper_bot/fak.py`
- Create: `paper/tests/test_fak.py`

- [ ] **Step 1: Write failing FAK tests**

Cover these exact cases:

- BUY `$5` across asks `0.89×3` and `0.90×5` with `max_price=0.90` fills 5.588888 shares after six-decimal floor and records two legs.
- BUY ignores asks above max price and cancels remaining quote.
- BUY with no eligible ask returns zero fill.
- SELL walks bids highest to lowest down to minimum tick.
- SELL partial fill retains exact unsold shares.
- Requested size below market minimum is rejected before book walking.
- Prices not aligned to tick are rejected.
- Every persisted amount has at most six decimal places.
- Fee is calculated per actual level fill from the supplied schedule, never from VWAP.

Use concrete expected `Decimal` values and assert `sum(leg.shares) == result.filled_shares` and `sum(leg.quote) == result.quote_amount`.

- [ ] **Step 2: Verify RED**

```bash
PYTHONPATH=paper $PY -m unittest paper.tests.test_fak -v
```

Expected: missing `paper_bot.fak`.

- [ ] **Step 3: Implement domain values and simulator**

Public API:

```python
SIX_PLACES = Decimal("0.000001")

@dataclass(frozen=True)
class BookLevel:
    price: Decimal
    shares: Decimal

@dataclass(frozen=True)
class FeeSchedule:
    rate: Decimal
    exponent: Decimal

@dataclass(frozen=True)
class FillLeg:
    price: Decimal
    shares: Decimal
    quote: Decimal
    fee: Decimal

@dataclass(frozen=True)
class FakResult:
    requested_quote: Decimal | None
    requested_shares: Decimal | None
    filled_shares: Decimal
    quote_amount: Decimal
    unfilled_quote: Decimal | None
    unfilled_shares: Decimal | None
    fee: Decimal
    legs: tuple[FillLeg, ...]
    status: str
```

The public functions must expose these exact contracts:

```text
simulate_buy_fak(asks, requested_usdc, max_price, tick_size, min_order_shares, fee_schedule) -> FakResult
simulate_sell_fak(bids, requested_shares, min_price, tick_size, min_order_shares, fee_schedule) -> FakResult
quote_for_target_shares(asks, target_shares, max_price) -> Decimal
```

Implementation rules are exactly Design §7: sort levels, floor atomic amounts to six decimals, never mutate the input book, and derive `full`, `partial`, or `zero` from actual fill. `simulate_buy_fak` iterates ascending asks and decrements quote; `simulate_sell_fak` iterates descending bids and decrements shares; `quote_for_target_shares` returns only the quote consumed while accumulating at most the target shares.

- [ ] **Step 4: Verify GREEN and cumulative safety**

```bash
PYTHONPATH=paper $PY -m unittest paper.tests.test_fak paper.tests.test_config_safety -v
$PY paper/scripts/security_scan.py paper/paper_bot
```

- [ ] **Step 5: Commit**

```bash
git add paper/paper_bot paper/tests
git commit -m "feat(paper): add Decimal FAK execution simulator"
```

---

### Task 3: Generation-safe full order books

**Files:**
- Create: `paper/paper_bot/books.py`
- Create: `paper/tests/test_books.py`

- [ ] **Step 1: Write failing order-book tests**

Tests must prove:

- a snapshot sorts/normalizes all levels and sets generation valid;
- zero-size deltas remove levels;
- stale event timestamps and older sequence values are rejected;
- a reconnect invalidates the generation and blocks executable views;
- deltas before a replacement snapshot are ignored;
- best bid/ask and cloned FAK levels are derived from the same generation;
- crossed books and non-positive price/size values fail closed;
- UP and DOWN token books remain independent.

- [ ] **Step 2: Verify RED**

```bash
PYTHONPATH=paper $PY -m unittest paper.tests.test_books -v
```

- [ ] **Step 3: Implement `OrderBook`**

`OrderBook` must expose these exact operations:

```text
apply_snapshot(bids, asks, event_ts_ms, sequence) -> generation int
apply_delta(side, price, shares, event_ts_ms, sequence) -> accepted bool
invalidate() -> None
executable_bids() -> tuple[BookLevel, ...]
executable_asks() -> tuple[BookLevel, ...]
generation -> int
valid -> bool
```

Every accepted snapshot increments generation. `apply_delta` returns false without mutation for stale sequence/timestamp data, removes zero-size levels, and validates the resulting spread. Executable methods raise `InvalidBook` unless a post-connect snapshot exists.

- [ ] **Step 4: Verify and commit**

```bash
PYTHONPATH=paper $PY -m unittest paper.tests.test_books paper.tests.test_fak -v
git add paper/paper_bot/books.py paper/tests/test_books.py
git commit -m "feat(paper): maintain generation-safe full books"
```

---

### Task 4: Strict Gamma market discovery and validation

**Files:**
- Create: `paper/paper_bot/gamma.py`
- Create: `paper/tests/fixtures/gamma_market.json`
- Create: `paper/tests/test_gamma.py`

- [ ] **Step 1: Write failing validation tests**

Use a complete sanitized Gamma fixture. Tests reject:

- wrong slug/symbol/timestamp;
- interval other than 300 seconds;
- missing/distinct-token violations;
- wrong outcomes;
- disabled order book;
- invalid tick/minimum size;
- missing enabled fee schedule;
- non-Chainlink resolution source;
- non-zero `secondsDelay`;
- current/next responses that do not match requested slugs.

A valid fixture must produce immutable `MarketDefinition` with UP/DOWN token IDs, tick, min size, fee schedule, start/end, and raw market id.

- [ ] **Step 2: Verify RED**

```bash
PYTHONPATH=paper $PY -m unittest paper.tests.test_gamma -v
```

- [ ] **Step 3: Implement a GET-only Gamma client**

```python
@dataclass(frozen=True)
class MarketDefinition:
    symbol: str
    slug: str
    market_id: str
    mkt_ts: int
    end_ts: int
    up_token_id: str
    down_token_id: str
    tick_size: Decimal
    min_order_shares: Decimal
    fee_schedule: FeeSchedule
```

Exact callable contracts:

```text
validate_market(payload, symbol, mkt_ts) -> MarketDefinition
GammaClient.get_market_by_slug(slug) -> Mapping | None
GammaClient.discover_current_and_next(symbols, now) -> tuple[MarketDefinition, ...]
```

`GammaClient` exposes GET methods only and accepts an injected async callable `get_json(url, params)` for tests. Discovery requests exactly the floor-aligned current slug and current-plus-300 next slug for each symbol, then passes every response through `validate_market`.

- [ ] **Step 4: Verify and commit**

```bash
PYTHONPATH=paper $PY -m unittest paper.tests.test_gamma paper.tests.test_config_safety -v
$PY paper/scripts/security_scan.py paper/paper_bot
git add paper/paper_bot/gamma.py paper/tests
git commit -m "feat(paper): validate public Gamma markets"
```

---

### Task 5: Chainlink TWAP-60 resolver state

**Files:**
- Create: `paper/paper_bot/resolver.py`
- Create: `paper/tests/test_resolver.py`

- [ ] **Step 1: Write failing resolver tests**

Port the proven recorder edge cases and add typed state tests:

- reject stale, future, duplicate, and out-of-order observations before mutation;
- independent per-symbol 10-second freshness;
- capture the first observation in `[mkt_ts, mkt_ts+10s]` even if two arrive before evaluation;
- never fabricate a missed start;
- calculate UP/DOWN/TIE, distance, bps, and latest qualifying 5-second momentum;
- emit momentum without a start when history is fresh;
- stale resolver disables confirmation but preserves last display values;
- reset market starts independently across rollovers.

- [ ] **Step 2: Verify RED**

```bash
PYTHONPATH=paper $PY -m unittest paper.tests.test_resolver -v
```

- [ ] **Step 3: Implement typed resolver API**

```python
@dataclass(frozen=True)
class ResolverView:
    symbol: str
    current: Decimal | None
    start: Decimal | None
    observation_ts_ms: int | None
    age_ms: int | None
    fresh: bool
    distance: Decimal | None
    distance_bps: Decimal | None
    leader: str | None
    momentum_5s_bps: Decimal | None
```

Exact `ResolverState` contracts:

```text
accept(symbol, value, observation_ts_ms, receive_ts_ms) -> bool
view(symbol, mkt_ts, now_ms) -> ResolverView
stale_symbols(now_ms) -> tuple[str, ...]
```

`accept` validates before mutation and appends accepted values to a bounded ordered history. `view` scans that history for the first eligible start and the latest observation at least five seconds behind current. Use no Binance fallback.

- [ ] **Step 4: Verify and commit**

```bash
PYTHONPATH=paper $PY -m unittest paper.tests.test_resolver -v
git add paper/paper_bot/resolver.py paper/tests/test_resolver.py
git commit -m "feat(paper): track fresh Chainlink resolver state"
```

---

### Task 6: Fixed lane matrix and entry state machine

**Files:**
- Create: `paper/paper_bot/strategy.py`
- Create: `paper/tests/test_strategy.py`

- [ ] **Step 1: Write failing lane/entry tests**

Assert exactly 36 unique lane keys:

```python
self.assertEqual(len(all_lane_keys()), 36)
self.assertEqual({x.threshold for x in all_lane_keys()}, {D("0.80"), D("0.85"), D("0.89"), D("0.90")})
```

Test:

- only `0 < seconds_to_close <= 150` is eligible;
- continuity and rising-cross semantics;
- jump over max price produces one recorded zero-fill attempt;
- simultaneous UP/DOWN crossing is ambiguous;
- Book-only, Direction, and Confirmed gates differ exactly as approved;
- stale/tied Chainlink blocks only dependent variants;
- same threshold/confirmation performs one FAK calculation and clones evidence to Hold/Immediate/Confirmed policies;
- no re-entry or repeated accumulation after full, partial, or zero attempt;
- entry records book generation, event timestamp, config hash, actual legs, and fees.

- [ ] **Step 2: Verify RED**

```bash
PYTHONPATH=paper $PY -m unittest paper.tests.test_strategy -v
```

- [ ] **Step 3: Implement lane keys and `MarketStrategyState`**

```python
class Confirmation(str, Enum):
    BOOK_ONLY = "BOOK_ONLY"
    CHAINLINK_DIRECTION = "CHAINLINK_DIRECTION"
    CHAINLINK_CONFIRMED = "CHAINLINK_CONFIRMED"

class PositionPolicy(str, Enum):
    HOLD = "HOLD"
    IMMEDIATE_REVERSE = "IMMEDIATE_REVERSE"
    CHAINLINK_REVERSE = "CHAINLINK_REVERSE"

@dataclass(frozen=True, order=True)
class LaneKey:
    threshold: Decimal
    confirmation: Confirmation
    policy: PositionPolicy


def all_lane_keys(thresholds: Sequence[Decimal]) -> tuple[LaneKey, ...]:
    return tuple(
        LaneKey(threshold, confirmation, policy)
        for threshold in thresholds
        for confirmation in Confirmation
        for policy in PositionPolicy
    )
```

`MarketStrategyState.on_book_event(market, books, resolver, event_ts_ms, now_ts)` returns an immutable tuple of `StrategyEvent` records. It updates continuity baselines first, rejects ambiguity/staleness, marks one attempt per threshold/confirmation, runs entry FAK once, and clones its evidence to the three policy lanes. Entry FAK uses the Task 2 simulator and configured `$5` default notional.

- [ ] **Step 4: Verify and commit**

```bash
PYTHONPATH=paper $PY -m unittest paper.tests.test_strategy paper.tests.test_fak paper.tests.test_books -v
git add paper/paper_bot/strategy.py paper/tests/test_strategy.py
git commit -m "feat(paper): evaluate fixed entry experiment"
```

---

### Task 7: One-shot reverse state machine

**Files:**
- Modify: `paper/paper_bot/strategy.py`
- Create: `paper/tests/test_reverse.py`

- [ ] **Step 1: Write failing reverse tests**

Test the approved behavior:

- Hold never reverses.
- Immediate reverses at first opposite ask in `[0.89,0.90]`.
- Ask above 0.90 does not fill; a later return into range can trigger.
- Chainlink reverse waits for fresh opposite leader and same-sign momentum.
- Chainlink confirmation arriving while the book remains eligible triggers.
- No reverse on the entry event.
- SELL sweeps held shares to minimum tick and may partially fill.
- Reverse BUY targets only actual `sold_shares` using `quote_for_target_shares` and max 0.90.
- Partial BUY persists both old residual and actual opposite inventory.
- Exactly one reverse sequence is attempted; no second switch/DCA.
- SELL/BUY book generations and elapsed leg time are recorded.

Use a regression example where 5.555555 held shares, only 3.000000 sell, and only 2.500000 opposite shares buy. Assert no inventory becomes negative and reverse BUY target is 3.000000, not 5.555555.

- [ ] **Step 2: Verify RED**

```bash
PYTHONPATH=paper $PY -m unittest paper.tests.test_reverse -v
```

- [ ] **Step 3: Implement reverse transitions**

Add immutable `ReverseSequence` and `InventoryLot` domain records. The transition order is:

```text
ELIGIBLE -> SELL_ATTEMPTED -> SELL_FILLED_OR_PARTIAL -> BUY_ATTEMPTED -> COMPLETE
```

Zero SELL fill ends the sequence without BUY. BUY request quote is computed from current opposite asks for `sold_shares`; actual partial fills are retained.

- [ ] **Step 4: Verify and commit**

```bash
PYTHONPATH=paper $PY -m unittest paper.tests.test_reverse paper.tests.test_strategy -v
git add paper/paper_bot paper/tests/test_reverse.py
git commit -m "feat(paper): simulate one-shot reverse execution"
```

---

### Task 8: Strict settlement and exact accounting

**Files:**
- Create: `paper/paper_bot/accounting.py`
- Create: `paper/paper_bot/settlement.py`
- Create: `paper/tests/test_accounting.py`
- Create: `paper/tests/test_settlement.py`

- [ ] **Step 1: Write failing accounting tests**

Cover:

- provisional 0.9995/0.0005 is not final;
- only closed/resolved unique official winner settles;
- payout includes every remaining old/new lot;
- entry BUY, reverse SELL, and reverse BUY fees reconcile from fill legs;
- lane net PnL formula exactly matches Design §9;
- hold counterfactual and reverse incremental effect;
- rescued loss, harmed winner, false reverse, and unresolved classifications;
- open markets excluded from final WR/PnL denominators;
- aggregate net equals sum of lane-market results and max drawdown uses settled chronological cash equity.

- [ ] **Step 2: Verify RED**

```bash
PYTHONPATH=paper $PY -m unittest paper.tests.test_accounting paper.tests.test_settlement -v
```

- [ ] **Step 3: Implement strict parser/accountant**

```python
@dataclass(frozen=True)
class OfficialSettlement:
    winner: str
    resolved_at: int | None
```

Exact contracts:

```text
parse_official_settlement(payload, expected_slug) -> OfficialSettlement | None
settle_lane(position, settlement) -> LaneResult
aggregate_results(results) -> LaneStats
```

`parse_official_settlement` returns `None` until every strict condition passes. `settle_lane` sums winning inventory payouts and persisted cash/fee legs exactly once. `aggregate_results` sorts by market close time, computes settled denominators and running-equity drawdown, and never includes unresolved rows. Do not call recorder inference.

- [ ] **Step 4: Verify and commit**

```bash
PYTHONPATH=paper $PY -m unittest paper.tests.test_accounting paper.tests.test_settlement paper.tests.test_reverse -v
git add paper/paper_bot paper/tests
git commit -m "feat(paper): settle and account virtual positions"
```

---

### Task 9: SQLite WAL persistence and restart idempotency

**Files:**
- Create: `paper/paper_bot/storage.py`
- Create: `paper/tests/test_storage.py`

- [ ] **Step 1: Write failing storage tests**

Against a temporary database assert:

- WAL and foreign keys are enabled;
- all Design §10 tables exist;
- experiment identity is a SHA-256 of strategy-affecting settings;
- same signal id cannot insert twice;
- order fill totals equal persisted fill legs inside one transaction;
- inventory cannot become negative;
- reverse and settlement writes are idempotent;
- changed notional creates a new experiment version;
- read-only URI cannot write;
- restart reloads open positions and attempted lane keys without re-entry.

- [ ] **Step 2: Verify RED**

```bash
PYTHONPATH=paper $PY -m unittest paper.tests.test_storage -v
```

- [ ] **Step 3: Implement schema and storage API**

Use stdlib `sqlite3` with explicit transactions and small writes through one engine-owned writer. Required API:

`Storage` exposes these exact transaction boundaries:

```text
initialize() -> None
ensure_experiment(settings) -> experiment_hash str
record_strategy_events(events) -> None
load_open_market_states() -> tuple[PersistedMarketState, ...]
record_settlement(market_id, settlement, results) -> None
dashboard_snapshot() -> DashboardSnapshot
```

`record_strategy_events` inserts signal/order/fill/inventory rows in one `BEGIN IMMEDIATE` transaction and rolls back all rows on any invariant failure. `record_settlement` uses one unique market settlement key and upserts no mutable result fields after success. Store decimals as canonical strings, not SQLite REAL.

- [ ] **Step 4: Verify and commit**

```bash
PYTHONPATH=paper $PY -m unittest paper.tests.test_storage paper.tests.test_accounting -v
git add paper/paper_bot/storage.py paper/tests/test_storage.py
git commit -m "feat(paper): persist idempotent experiment state"
```

---

### Task 10: Compressed raw event journal and disk guard

**Files:**
- Create: `paper/paper_bot/journal.py`
- Create: `paper/tests/test_journal.py`
- Modify: `paper/requirements.txt`

- [ ] **Step 1: Write failing journal tests**

Test:

- canonical JSON rows include source, receive timestamp, source timestamp, symbol/token, and payload;
- daily UTC rotation;
- previous file compresses to `.jsonl.zst` and remains readable;
- restart appends without corrupting the open day;
- write error or disk threshold emits critical state and prevents new virtual entries;
- journals never contain environment values or credential-like keys;
- initial seven-day run has no retention deletion.

- [ ] **Step 2: Verify RED**

```bash
PYTHONPATH=paper $PY -m unittest paper.tests.test_journal -v
```

- [ ] **Step 3: Implement journal**

Use `zstandard` streaming compression after rotation. Public API:

`RawJournal` exposes:

```text
append(event) -> None
rotate_if_needed(now) -> None
writable() -> bool
```

`append` serializes one canonical JSON object and flushes the active line-buffered file. `rotate_if_needed` closes the prior UTC day, writes a zstd stream to a temporary path, fsyncs, atomically renames it, and only then removes the uncompressed prior file. `writable` checks the sticky critical state and disk threshold. No logger may emit raw environment or headers.

- [ ] **Step 4: Verify and commit**

```bash
$PY -m pip install -r paper/requirements.txt
PYTHONPATH=paper $PY -m unittest paper.tests.test_journal -v
git add paper/requirements.txt paper/paper_bot/journal.py paper/tests/test_journal.py
git commit -m "feat(paper): journal replayable public events"
```

---

### Task 11: Public Market WS and RTDS adapters

**Files:**
- Create: `paper/paper_bot/market_ws.py`
- Create: `paper/paper_bot/rtds.py`
- Create: `paper/tests/fixtures/market_ws_replay.jsonl`
- Create: `paper/tests/fixtures/rtds_replay.jsonl`
- Create: `paper/tests/test_market_ws.py`
- Modify: `paper/tests/test_resolver.py`

- [ ] **Step 1: Write failing parser/reconnect tests**

Market WS tests cover initial snapshots, price/size changes, best-price updates, zero removals, malformed payloads, heartbeat, reconnect invalidation, and resubscribe token sets.

RTDS tests cover exact E18 parsing, symbol filtering, TWAP-60-only watchdog refresh, PONG/non-TWAP frames not refreshing, array messages, future/stale rejection, and reconnect/resubscribe.

Use deterministic fixture files; no network in unit tests.

- [ ] **Step 2: Verify RED**

```bash
PYTHONPATH=paper $PY -m unittest paper.tests.test_market_ws paper.tests.test_resolver -v
```

- [ ] **Step 3: Implement GET/WS-only adapters**

Exact async adapter contracts:

```text
MarketWsClient.run(token_ids_supplier, market_event_queue) -> None
RtdsClient.run(resolver_event_queue) -> None
```

On connection, `MarketWsClient` obtains the current immutable token set, subscribes, waits for full snapshots, and only then emits executable deltas. `RtdsClient` subscribes to the three approved crypto TWAP-60 streams and emits only parsed observations. Both clients use bounded exponential reconnect, explicit PING, and injected connectors in tests. Neither accepts headers/credentials from environment.

- [ ] **Step 4: Verify and commit**

```bash
PYTHONPATH=paper $PY -m unittest paper.tests.test_market_ws paper.tests.test_resolver paper.tests.test_books -v
$PY paper/scripts/security_scan.py paper/paper_bot
git add paper/paper_bot paper/tests
git commit -m "feat(paper): consume public market and resolver streams"
```

---

### Task 12: Engine orchestration, replay, rollover, and restart

**Files:**
- Create: `paper/paper_bot/engine.py`
- Create: `paper/paper_bot/cli.py`
- Create: `paper/tests/test_engine_replay.py`

- [ ] **Step 1: Write failing deterministic engine tests**

Drive the engine with injected clock/transports and fixture events. Cover:

- current/next market discovery and rollover;
- ordinary win/loss;
- partial entry;
- successful and false reverse;
- disconnect while open;
- journal/database critical state pauses new entries but keeps monitoring;
- restart before reverse and before settlement without duplicate attempts;
- provisional then final settlement;
- three assets remain isolated;
- engine starts with an empty environment and no credentials.

At test end reconcile every persisted fill, inventory lot, lane result, and aggregate against emitted events.

- [ ] **Step 2: Verify RED**

```bash
PYTHONPATH=paper $PY -m unittest paper.tests.test_engine_replay -v
```

- [ ] **Step 3: Implement engine task graph**

Exact engine contracts:

```text
initialize() -> None
run() -> None
stop() -> None
process_market_event(event) -> None
process_resolver_event(event) -> None
reconcile_settlements() -> None
```

`initialize` opens storage/journal, restores persisted open states, discovers markets, and constructs subscriptions. `run` supervises discovery, both feeds, event processing, settlement polling, heartbeat, and shutdown as named asyncio tasks. Event processing journals first, validates state, computes strategy events, and persists them atomically. Use one bounded internal event queue and one SQLite writer. CLI supports only `engine`, `status`, and `check-db`; there is no trade command.

- [ ] **Step 4: Verify and commit**

```bash
PYTHONPATH=paper $PY -m unittest discover -s paper/tests -p 'test_*.py' -v
$PY paper/scripts/security_scan.py paper/paper_bot
$PY -m py_compile paper/paper_bot/*.py paper/tests/*.py
git add paper/paper_bot paper/tests
git commit -m "feat(paper): orchestrate replayable paper engine"
```

---

### Task 13: Read-only Rich terminal dashboard

**Files:**
- Create: `paper/paper_bot/tui.py`
- Create: `paper/tests/test_tui.py`
- Modify: `paper/paper_bot/cli.py`
- Modify: `paper/requirements.txt`

- [ ] **Step 1: Write failing dashboard tests**

Build a temporary populated database and assert rendered text contains:

- BTC/ETH/SOL market countdown and book health;
- resolver start/current/distance/leader/momentum/age;
- threshold × confirmation × policy filters;
- signal/full/partial/zero counts, resolved W/L, net PnL, EV/share, drawdown;
- open old/new inventory and projected payouts;
- reconnect/stale/database/disk events;
- explicit `PAPER ONLY — NO ORDERS` banner.

Open the DB with SQLite URI `mode=ro`; assert a write attempt fails. Start/stop the TUI twice and verify engine/database state is unchanged.

- [ ] **Step 2: Verify RED**

```bash
PYTHONPATH=paper $PY -m unittest paper.tests.test_tui -v
```

- [ ] **Step 3: Implement dashboard**

Use Rich `Live`, `Layout`, `Table`, and `Panel`. CLI subcommand:

```text
python -m paper_bot.cli watch --db /data/paper.db --refresh 1.0
```

The dashboard process imports no network adapter modules and receives no environment credentials.

- [ ] **Step 4: Verify and commit**

```bash
$PY -m pip install -r paper/requirements.txt
PYTHONPATH=paper $PY -m unittest paper.tests.test_tui paper.tests.test_storage -v
git add paper
git commit -m "feat(paper): add attachable terminal dashboard"
```

---

### Task 14: Docker packaging, wrappers, health, and safety gate

**Files:**
- Create: `paper/.env.example`
- Create: `paper/Dockerfile`
- Create: `paper/docker-compose.yml`
- Create: `paper/scripts/paper-bot`
- Create: `paper/scripts/paper-watch`
- Modify: `paper/tests/test_config_safety.py`

- [ ] **Step 1: Write failing packaging/safety assertions**

Tests parse Compose/env/Docker files and assert:

- one daemon service named `paper-engine`;
- no credential environment keys;
- data is a bind mount under `/data`;
- engine healthcheck verifies heartbeat and database write progress;
- image runs as non-root;
- `paper-watch` invokes read-only watch command;
- `paper-bot` exposes start/stop/status/logs only;
- no host Docker socket or wallet files are mounted.

- [ ] **Step 2: Verify RED**

```bash
PYTHONPATH=paper $PY -m unittest paper.tests.test_config_safety -v
```

- [ ] **Step 3: Implement packaging**

`paper/.env.example` contains only:

```text
SYMBOLS=btc,eth,sol
ENTRY_THRESHOLDS=0.80,0.85,0.89,0.90
PAPER_NOTIONAL_USD=5.00
RTDS_STALE_SEC=10
DATA_DIR=/data
LOG_LEVEL=INFO
```

Compose builds `paper-engine`, mounts `./runtime:/data`, uses `restart: unless-stopped`, bounded JSON logs, and no secrets. Wrapper scripts use exact Compose file paths and `set -euo pipefail`.

- [ ] **Step 4: Build and verify container**

```bash
PYTHONPATH=paper $PY -m unittest discover -s paper/tests -v
$PY paper/scripts/security_scan.py paper
cd paper && docker compose config && docker compose build paper-engine
cd paper && docker compose run --rm paper-engine python -m unittest discover -s /app/tests -v
cd paper && docker compose up -d paper-engine
cd paper && ./scripts/paper-bot status
cd paper && docker compose down
```

Expected: tests pass, scanner clean, image builds, health/status works, no credential environment variables inside the container.

- [ ] **Step 5: Commit**

```bash
git add paper
git commit -m "chore(paper): package daemon and TUI"
```

---

### Task 15: Tested recorder backup and verification tooling

**Files:**
- Create: `paper/scripts/backup_recorder.py`
- Create: `paper/tests/test_backup_recorder.py`

- [ ] **Step 1: Write failing local backup tests**

With a temporary fake recorder tree, test:

- manifest contains sorted relative paths, sizes, and SHA-256;
- symlinks and paths escaping the root are rejected;
- archive SHA-256 is generated;
- extracted file count/bytes/hashes exactly reconcile;
- one changed byte fails verification;
- deletion is never performed by this script;
- destination is under `backups/recorder/YYYYMMDDTHHMMSSZ` with the timestamp parsed as UTC.

- [ ] **Step 2: Verify RED**

```bash
PYTHONPATH=paper $PY -m unittest paper.tests.test_backup_recorder -v
```

- [ ] **Step 3: Implement manifest/archive verifier**

CLI modes are deliberately non-destructive:

```text
backup_recorder.py manifest --root PATH --output MANIFEST
backup_recorder.py verify --root EXTRACTED --manifest MANIFEST --archive ARCHIVE
```

Use SHA-256 streaming reads and canonical JSON manifest. Remote stop/archive/download/delete remains parent-controlled shell work.

- [ ] **Step 4: Verify and commit**

```bash
PYTHONPATH=paper $PY -m unittest paper.tests.test_backup_recorder -v
git add paper/scripts/backup_recorder.py paper/tests/test_backup_recorder.py
git commit -m "chore(ops): verify recorder backups before cleanup"
```

---

### Task 16: Final local review and acceptance gate

**Files:**
- Create: `paper/README.md`
- Create: `docs/deployments/2026-08-31-chainlink-fak-paper-bot.md`

- [ ] **Step 1: Document exact operator workflow**

README includes architecture, safety boundary, start/status/watch/stop, database/journal locations, experiment version behavior, and seven-day evaluation policy. It must state that there is no live-order capability.

Deployment document begins with a checklist for backup evidence, source commit, image id, smoke evidence, and rollback path; leave evidence fields absent until commands run rather than inserting placeholders.

- [ ] **Step 2: Run full fresh verification**

```bash
PYTHONPATH=paper $PY -m unittest discover -s paper/tests -p 'test_*.py' -v
$PY paper/scripts/security_scan.py paper
$PY -m py_compile paper/paper_bot/*.py paper/tests/*.py paper/scripts/*.py
cd paper && docker compose config
cd paper && docker compose build paper-engine
cd paper && docker compose run --rm paper-engine python -m unittest discover -s /app/tests -v
git diff --check
git status --short
```

Expected: zero failures, security findings, compile errors, Docker errors, whitespace errors, staged secrets, or unexpected generated files.

- [ ] **Step 3: Parent and reviewer gates**

Parent reviews complete `git diff ea6c51c..HEAD` against every design section. Run two read-only reviewers in parallel:

- correctness/accounting/replay reviewer;
- security/operations/TUI reviewer.

Fix every Critical/Important finding through RED/GREEN tests and a dedicated fix commit. Re-run the complete gate.

- [ ] **Step 4: Commit documentation**

```bash
git add paper/README.md docs/deployments/2026-08-31-chainlink-fak-paper-bot.md
git commit -m "docs(paper): add operation and deployment runbook"
```

---

### Task 17: Fixed-boundary recorder backup and old-stack cleanup

**Server:** `ubuntu@158.178.155.78`

- [ ] **Step 1: Read-only preflight**

Record current source commit, container/image ids, Compose services, `/opt/recorder` size, dataset file count/bytes, free local/server disk, and active processes. Confirm only the old recorder scope will be removed.

- [ ] **Step 2: Stop recorder and create fixed manifest/archive**

```bash
scp paper/scripts/backup_recorder.py ubuntu@158.178.155.78:/tmp/backup_recorder.py
ssh ubuntu@158.178.155.78 'cd /opt/recorder && sudo docker compose stop pm-recorder'
ssh ubuntu@158.178.155.78 'python3 /tmp/backup_recorder.py manifest --root /opt/recorder/data --output /tmp/recorder-manifest.json'
ssh ubuntu@158.178.155.78 'tar --zstd -C /opt/recorder -cf /tmp/recorder-data.tar.zst data && sha256sum /tmp/recorder-data.tar.zst > /tmp/recorder-data.tar.zst.sha256'
```

No rows may be written after the manifest boundary.

- [ ] **Step 3: Download and independently verify before deletion**

Download into exact local directory:

```text
/home/alex/Project/up_down/backups/recorder/YYYYMMDDTHHMMSSZ/
```

Copy archive, archive hash, and manifest. Verify archive SHA, extractability, file count, total bytes, and every file hash with `backup_recorder.py verify`. Store verification output in the deployment document.

If any check fails, restart the old recorder and stop. Do not delete anything.

- [ ] **Step 4: Remove only verified old-recorder resources**

After successful local verification:

```bash
ssh ubuntu@158.178.155.78 'cd /opt/recorder && sudo docker compose down --volumes --remove-orphans'
ssh ubuntu@158.178.155.78 'sudo rm -rf /opt/recorder /tmp/recorder-build-candidate /tmp/recorder.py.candidate /tmp/recorder-data.tar.zst /tmp/recorder-data.tar.zst.sha256 /tmp/recorder-manifest.json'
```

Remove recorder-specific images only after checking no container references them. Do not run `docker system prune`. Confirm `pm-recorder`, `/opt/recorder`, and recorder-specific temporary paths are absent.

- [ ] **Step 5: Commit backup evidence without committing the archive**

Update deployment document with manifest summary, local backup path, archive SHA-256, verified file count/bytes, and cleanup evidence. The ignored archive and raw data remain outside Git.

```bash
git add docs/deployments/2026-08-31-chainlink-fak-paper-bot.md
git commit -m "docs(ops): record verified recorder backup"
```

---

### Task 18: Deploy paper engine and perform 30-minute smoke

**Server:** `ubuntu@158.178.155.78`

- [ ] **Step 1: Deploy reviewed commit only**

Push or copy the exact reviewed tree to `/opt/paper-bot`, record commit SHA, and verify remote source hashes. Create `.env` only from `paper/.env.example`; assert no forbidden credential keys exist.

- [ ] **Step 2: Build and start only paper engine**

```bash
ssh ubuntu@158.178.155.78 'cd /opt/paper-bot && sudo docker compose config && sudo docker compose build paper-engine && sudo docker compose up -d paper-engine'
```

Verify running image id, non-root user, mounts, environment key names, restart count, heartbeat, SQLite WAL, and absence of authenticated/order endpoints.

- [ ] **Step 3: Run condition-based 30-minute smoke**

Require throughout the window:

- BTC/ETH/SOL current markets discovered and validated;
- both token books snapshot-valid after reconnect;
- Chainlink observations advance with age ≤10 seconds;
- one full market rollover;
- SQLite/journal rows advance;
- TUI attaches and detaches while engine remains running;
- zero ERROR/Traceback, restart, security finding, or negative inventory;
- no outbound POST requests and no real orders.

Force one Market WS reconnect and one RTDS reconnect; require snapshot/resubscribe recovery before new signals.

- [ ] **Step 4: Reconcile smoke artifacts**

Read SQLite and journal independently. Reconcile signal ids, virtual orders, fill legs, inventories, experiment versions, and any settled results. Record counts and health evidence in the deployment document.

- [ ] **Step 5: Final review and deployment evidence commit**

Run a final read-only reviewer over deployed code and evidence. Fix blockers locally through tests before redeploying. When clean:

```bash
git add docs/deployments/2026-08-31-chainlink-fak-paper-bot.md
git commit -m "docs(ops): record paper bot deployment smoke"
```

Report the exact `./paper-watch` command and state that the seven-day forward experiment has started; do not claim profitability.

---

## Final acceptance checklist

- [ ] Every implementation function was introduced after a witnessed failing test.
- [ ] Each task has one parent-reviewed commit.
- [ ] Full unit/replay suite passes locally and inside Docker.
- [ ] Security scanner proves no credentials, authenticated user channel, or order POST path.
- [ ] Decimal FAK fills reconcile level-by-level.
- [ ] 36 lanes per asset are pre-registered and immutable per experiment version.
- [ ] Partial reverse sells buy only actually sold shares.
- [ ] SQLite restart/idempotency and raw journal replay pass.
- [ ] Recorder backup is locally verified before remote deletion.
- [ ] Only old recorder resources are removed; no broad prune occurs.
- [ ] Server smoke lasts at least 30 minutes and includes forced reconnects.
- [ ] TUI is read-only and attachable without stopping engine.
- [ ] No real order is placed.
- [ ] Seven-day paper evaluation begins under one fixed config hash.
