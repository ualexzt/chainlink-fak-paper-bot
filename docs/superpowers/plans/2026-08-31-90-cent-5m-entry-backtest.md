# 5m 90-Cent Entry Backtest Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build and run a reproducible paper backtest comparing a rising-cross taker entry with a post-trigger maker pullback entry on 5-minute Polymarket UP/DOWN markets.

**Architecture:** A focused Python module streams recorder JSONL into per-market states. Each threshold state selects the first side crossing upward, records an immediate taker fill, and independently tracks a fixed one-tick-lower maker order. Pure primitives are unit-tested before the streaming runner aggregates complete markets and writes JSON/CSV/report artifacts.

**Tech Stack:** Python 3 standard library (`dataclasses`, `json`, `csv`, `statistics`, `unittest`).

**Covered configurations:** Variant A (taker) and Variant B (maker pullback), independently evaluated at 88¢, 89¢, 90¢, 91¢, and 92¢.

**Repository note:** `/home/alex/Project/up_down` is not a Git repository, so commit steps cannot be executed.

---

### Task 1: Define and test entry-state semantics

**Files:**
- Create: `research/test_backtest_90cent_5m.py`
- Create: `research/backtest_90cent_5m.py`

- [ ] **Step 1: Write failing primitive tests**

Create tests using a wished-for `MarketState.process_row()` API. Cover:

```python
def test_initial_ask_above_threshold_is_baseline_not_cross():
    state = MarketState("btc", 1000)
    state.process_row(row(age=0, up_ask=.91, dn_ask=.09))
    assert not state.entries[.90].signal


def test_taker_uses_first_rising_cross_actual_ask():
    state = MarketState("btc", 1000)
    state.process_row(row(age=0, up_ask=.89, dn_ask=.11))
    state.process_row(row(age=1, up_ask=.91, dn_ask=.09))
    entry = state.entries[.90]
    assert entry.side == "UP"
    assert entry.taker_entry_price == .91


def test_maker_cannot_fill_on_trigger_row_but_fills_later_at_limit():
    state = MarketState("btc", 1000)
    state.process_row(row(age=0, up_ask=.89, dn_ask=.11))
    state.process_row(row(age=1, up_ask=.90, dn_ask=.10))
    assert state.entries[.90].maker_entry_price is None
    state.process_row(row(age=2, up_ask=.89, dn_ask=.11))
    assert state.entries[.90].maker_entry_price == .89
```

Also test missing-book continuity reset, ambiguous simultaneous crossings, first-side-only behavior, and maker no-fill.

- [ ] **Step 2: Run tests and verify RED**

Run:

```bash
python3 -m unittest research.test_backtest_90cent_5m -v
```

Expected: import failure because `research.backtest_90cent_5m` does not exist.

- [ ] **Step 3: Implement minimal state machine**

Create:

```python
THRESHOLDS = (0.88, 0.89, 0.90, 0.91, 0.92)

@dataclass
class EntryState:
    signal: bool = False
    ambiguous: bool = False
    side: str | None = None
    trigger_ts: int | None = None
    trigger_age: int | None = None
    trigger_ask: float | None = None
    taker_entry_price: float | None = None
    maker_limit: float | None = None
    maker_entry_price: float | None = None
    maker_fill_ts: int | None = None
    maker_fill_age: int | None = None

@dataclass
class MarketState:
    symbol: str
    mkt_ts: int
    entries: dict[float, EntryState] = field(
        default_factory=lambda: {t: EntryState() for t in THRESHOLDS}
    )
    previous_asks: dict[str, float] = field(default_factory=dict)
```

`process_row()` first checks a previously selected maker order against a later row, then detects rising crossings for unselected thresholds, rejects ambiguous simultaneous crossings, and updates paired-book baselines. Missing either ask clears both baselines.

- [ ] **Step 4: Run primitive tests and verify GREEN**

Run the same unittest command. Expected: all primitive tests pass.

### Task 2: Add settlement, accounting, and streaming runner

**Files:**
- Modify: `research/test_backtest_90cent_5m.py`
- Modify: `research/backtest_90cent_5m.py`

- [ ] **Step 1: Write failing settlement/accounting tests**

Add tests asserting:

```python
assert trade_result("UP", "UP", .91, "taker")["won"] is True
assert trade_result("UP", "DOWN", .91, "taker")["gross_pnl"] == -.91
assert trade_result("DOWN", "DOWN", .89, "maker")["net_pnl"] == .11
```

Add strict Gamma loader tests that derive the winner from `outcomes` + `outcomePrices`, require resolved status/expected slug/feeSchedule, and reject a contradictory convenience `winner`. Add a synthetic JSONL test with one complete and one incomplete 5m market. Assert incomplete markets do not enter fill/win-rate denominators and that summary/trade files are written.

- [ ] **Step 2: Run targeted tests and verify RED**

Run:

```bash
python3 -m unittest research.test_backtest_90cent_5m.BacktestRunnerTests -v
```

Expected: failures for missing `load_gamma_outcomes`, `trade_result`, or `run_backtest` behavior.

- [ ] **Step 3: Implement runner and aggregations**

Implement:

```python
def taker_fee(price: float, rate: float = .07, exponent: float = 1.0) -> float:
    return rate * (price * (1.0 - price)) ** exponent


def trade_result(side: str, winner: str, price: float, variant: str) -> dict:
    fee = taker_fee(price) if variant == "taker" else 0.0
    won = side == winner
    gross = (1.0 if won else 0.0) - price
    return {"won": won, "gross_pnl": gross, "fee": fee, "net_pnl": gross - fee}
```

`run_backtest(data_dir, out_dir, outcomes)` must require official Gamma outcomes, stream only `tf == "5m"` and BTC/ETH/SOL, deduplicate exact per-market timestamps, build two records per threshold per market, aggregate complete records with Wilson intervals, and write portable `summary.json` plus `trades.csv` metadata.

- [ ] **Step 4: Verify all new tests GREEN**

Run:

```bash
python3 -m unittest research.test_backtest_90cent_5m -v
python3 -m py_compile research/backtest_90cent_5m.py
```

Expected: all tests pass and compilation exits zero.

### Task 3: Run production backtest and produce the final report

**Files:**
- Create: `research/results/90cent_5m/summary.json`
- Create: `research/results/90cent_5m/trades.csv`
- Create: `research/results/90cent_5m/FINAL_REPORT.md`

- [ ] **Step 1: Run on a fixed server snapshot**

Stream the script to the recorder host and write into a new temporary output directory:

```bash
scp research/results/90cent_5m/gamma_outcomes.jsonl ubuntu@158.178.155.78:/tmp/gamma_outcomes_5m.jsonl
ssh ubuntu@158.178.155.78 \
  'rm -rf /tmp/backtest-90cent-5m && mkdir -p /tmp/backtest-90cent-5m && python3 - --data-dir /opt/recorder/data --out-dir /tmp/backtest-90cent-5m --outcomes-file /tmp/gamma_outcomes_5m.jsonl' \
  < research/backtest_90cent_5m.py
```

Expected: exit zero with row, market, and fill counts.

- [ ] **Step 2: Retrieve artifacts**

Copy `summary.json` and `trades.csv` into `research/results/90cent_5m/`.

- [ ] **Step 3: Reconcile generated data**

Parse both artifacts and assert, for every variant/threshold, that CSV complete-fill counts, wins, losses, and PnL sums equal JSON summary values within floating-point tolerance.

- [ ] **Step 4: Write final report**

Report the 90¢ headline first, then 88–92¢ sensitivity, BTC/ETH/SOL breakdown, confidence intervals, entry prices, maker fill/time-to-fill, PnL, and limitations. Clearly distinguish win rate from profitability and maker quote-touch from proven execution.

- [ ] **Step 5: Fresh final verification**

Run:

```bash
python3 -m unittest research.test_backtest_90cent_5m research.test_backtest_live_recorder -v
python3 -m py_compile research/backtest_90cent_5m.py research/test_backtest_90cent_5m.py
```

Then rerun artifact reconciliation. Expected: zero test failures, zero compilation errors, and exact summary/CSV reconciliation.
