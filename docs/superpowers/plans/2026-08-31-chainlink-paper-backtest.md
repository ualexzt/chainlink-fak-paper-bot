# Chainlink-native Paper Backtest Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build and run a read-only, streaming paper-taker backtest over the recorder's server JSONL data using Chainlink fair value, live best asks, inferred settlement, fees, and a 3×3 parameter sweep.

**Architecture:** A standard-library analyzer reads sorted daily JSONL files one row at a time and keeps only short-lived per-market state. It records the first qualifying signal for each margin/floor configuration, settles it only when the exact terminal Chainlink row arrives, and emits compact JSON/CSV reports. The production invocation streams the server directory through SSH; it never writes to `/opt/recorder/data`.

**Tech Stack:** Python 3 standard library (`json`, `csv`, `argparse`, `unittest`, `datetime`, `collections`), JSONL input, JSON/CSV output, SSH for remote execution.

---

### Task 1: Add failing unit tests for strategy primitives

**Files:**
- Create: `research/test_backtest_live_recorder.py`
- Test: `research/test_backtest_live_recorder.py`

- [ ] **Step 1: Write the failing tests**

Create tests that import these not-yet-existing functions/classes from `research.backtest_live_recorder`:

```python
import unittest

from research.backtest_live_recorder import (
    CONFIGS,
    MarketState,
    fee,
    pnl_for,
    row_signal,
    settle_market,
)


class StrategyPrimitiveTests(unittest.TestCase):
    def test_row_signal_uses_the_leader_ask_and_margin(self):
        row = {
            "age": 60,
            "leader_cl": "UP",
            "fair_leader_lut": 0.80,
            "dist_cl_bps": 12.0,
            "up_ask": 0.75,
            "dn_ask": 0.20,
        }
        self.assertEqual(row_signal(row, margin=0.04, floor=8.0), ("UP", 0.75, 0.80, 12.0))
        self.assertIsNone(row_signal(row, margin=0.06, floor=8.0))

    def test_row_signal_uses_down_ask_and_rejects_invalid_rows(self):
        row = {
            "age": 60,
            "leader_cl": "DOWN",
            "fair_leader_lut": 0.70,
            "dist_cl_bps": -10.0,
            "up_ask": 0.90,
            "dn_ask": 0.65,
        }
        self.assertEqual(row_signal(row, margin=0.04, floor=8.0), ("DOWN", 0.65, 0.70, -10.0))
        for bad in ({**row, "dn_ask": None}, {**row, "dn_ask": 0.0}, {**row, "dist_cl_bps": -3.0}, {**row, "age": 30}):
            self.assertIsNone(row_signal(bad, margin=0.04, floor=8.0))

    def test_settle_market_uses_final_twap_and_treats_tie_as_up(self):
        state = MarketState("btc", "15m", 1000, 900)
        state.strike_cl = 100.0
        self.assertEqual(settle_market(state, 100.0), "UP")
        self.assertEqual(settle_market(state, 99.9), "DOWN")

    def test_settle_market_rejects_missing_exact_values(self):
        state = MarketState("btc", "15m", 1000, 900)
        self.assertIsNone(settle_market(state, None))
        state.strike_cl = 0.0
        self.assertIsNone(settle_market(state, 100.0))

    def test_fee_and_pnl_match_existing_research_model(self):
        self.assertAlmostEqual(fee(0.70), 0.03)
        self.assertAlmostEqual(pnl_for(0.70, True), 0.27)
        self.assertAlmostEqual(pnl_for(0.70, False), -0.73)

    def test_parameter_grid_is_the_approved_nine_combinations(self):
        self.assertEqual(len(CONFIGS), 9)
        self.assertEqual({c.margin for c in CONFIGS}, {0.03, 0.04, 0.06})
        self.assertEqual({c.floor for c in CONFIGS}, {3.0, 8.0, 15.0})


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run tests to verify they fail for the expected reason**

Run:

```bash
python3 -m unittest research/test_backtest_live_recorder.py -v
```

Expected: import failure because `research/backtest_live_recorder.py` does not exist yet.

---

### Task 2: Implement the streaming analyzer primitives and market state

**Files:**
- Create: `research/backtest_live_recorder.py`
- Test: `research/test_backtest_live_recorder.py`

- [ ] **Step 1: Implement the minimal tested primitives**

Define the exact public interfaces exercised by Task 1:

```python
@dataclass(frozen=True)
class Config:
    margin: float
    floor: float

CONFIGS = tuple(Config(m, f) for m in (0.03, 0.04, 0.06) for f in (3.0, 8.0, 15.0))

@dataclass
class MarketState:
    symbol: str
    tf: str
    mkt_ts: int
    step: int
    strike_cl: float | None = None
    final_cl: float | None = None
    final_age: int | None = None
    settled: bool = False
    rows: int = 0
    last_ts: int | None = None
    duplicate_rows: int = 0
    fair_rows: int = 0
    candidates: dict[Config, dict] = field(default_factory=dict)


def fee(price: float) -> float:
    return 0.10 * min(price, 1.0 - price)


def pnl_for(price: float, won: bool) -> float:
    return (1.0 - price - fee(price)) if won else (-price - fee(price))
```

`row_signal(row, margin, floor)` must return `(leader, ask, fair, dist_bps)` only when `age >= 59`, leader is UP/DOWN, fair and distance are finite, `abs(dist_bps) >= floor`, the selected leader ask is a numeric value strictly between 0 and 1, and `ask <= fair - margin`; otherwise return `None`.

`settle_market(state, final_cl)` must return `None` if `state.strike_cl` or `final_cl` is absent/non-positive; otherwise return `UP` for `final_cl >= state.strike_cl` and `DOWN` otherwise.

- [ ] **Step 2: Run the focused tests**

Run:

```bash
python3 -m unittest research/test_backtest_live_recorder.py -v
```

Expected: all primitive tests pass.

- [ ] **Step 3: Commit the tested primitive slice if a Git repository is available**

```bash
git add research/backtest_live_recorder.py research/test_backtest_live_recorder.py
git commit -m "test: define Chainlink paper backtest primitives"
```

If this workspace is not a Git repository, retain the files and continue without creating an unrelated repository.

---

### Task 3: Add one-pass JSONL ingestion, settlement, and sweep aggregation

**Files:**
- Modify: `research/backtest_live_recorder.py`
- Modify: `research/test_backtest_live_recorder.py`

- [ ] **Step 1: Add a failing end-to-end fixture test**

Append this test class before the `if __name__` block:

```python
import json
import tempfile
from pathlib import Path

from research.backtest_live_recorder import run_backtest


class StreamingRunTests(unittest.TestCase):
    def test_run_backtest_accepts_first_signal_once_and_scores_terminal_row(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "data"
            out = Path(tmp) / "out"
            root.mkdir()
            rows = []
            for age in range(60, 900):
                rows.append({
                    "ts": 1000 + age,
                    "symbol": "btc",
                    "tf": "15m",
                    "mkt_ts": 1000,
                    "age": age,
                    "strike_cl": 100.0,
                    "cl_twap60": 100.0 if age == 899 else 101.0,
                    "leader_cl": "UP",
                    "fair_leader_lut": 0.80,
                    "dist_cl_bps": 20.0,
                    "up_ask": 0.75 if age == 60 else 0.74,
                    "dn_ask": 0.20,
                })
            (root / "2026-01-01.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
            result = run_backtest(root, out)
            config = next(c for c in result["configs"] if c["margin"] == 0.04 and c["floor"] == 8.0)
            self.assertEqual(config["fills"], 1)
            self.assertEqual(config["wins"], 1)
            self.assertAlmostEqual(config["pnl_total"], 0.22)
            self.assertEqual(len(list(out.glob("*fills.csv"))), 1)
```

- [ ] **Step 2: Run the fixture test to verify it fails**

Run:

```bash
python3 -m unittest research/test_backtest_live_recorder.py -v
```

Expected: failure because `run_backtest` is not implemented.

- [ ] **Step 3: Implement streaming ingestion and aggregation**

Add these functions:

```python
def iter_jsonl_files(data_dir: Path):
    yield each sorted `*.jsonl` Path


def run_backtest(data_dir: Path, out_dir: Path) -> dict:
    # Create only out_dir; never write under data_dir.
    # Keep dict[(symbol, tf, mkt_ts), MarketState] for active/completed markets.
    # For each valid row:
    #   - count total/malformed/duplicate/quote/fair coverage;
    #   - create MarketState for supported 5m/15m keys;
    #   - save first valid strike_cl at age >= 59;
    #   - count fair rows (5m should remain zero with the current LUT);
    #   - for each of the 9 Config values, count every qualifying signal row
    #     and save only the first candidate in state.candidates[config];
    #   - when age == step - 1 and cl_twap60 + strike are valid, settle once;
    #     score every saved candidate and append a compact fill record;
    #   - do not score a state without an exact terminal row.
    # At EOF, mark all un-settled states incomplete.
    # Build config, symbol/tf/day aggregates from fills and counters.
    # Write `live_chainlink_backtest_summary.json` and
    # `live_chainlink_backtest_fills.csv` under out_dir.
    # Return the same summary dict written to JSON.
```

Use UTC dates from the row timestamp for fill/day grouping. Treat a duplicate as a second row with the same `(symbol, tf, mkt_ts, ts)`; count it and do not process it twice. Count `None` separately from numeric zero for quote coverage. Limit signals to `age < step`; post-expiry rows are data-quality counters only.

The summary must include:

- run metadata and file/date coverage;
- global malformed/duplicate/total row counters;
- row-level quote/fair coverage;
- market counts seen, complete, incomplete, and fair-available by timeframe;
- 9 config records with candidates, signal markets, fills, wins, win rate, Wilson 95% interval, average entry/fair, EV/share, total PnL;
- `by_symbol_tf` and `by_day` fill breakdowns;
- explicit `paper_only: true`, `settlement: inferred_chainlink_terminal_twap`, and `limitations`.

- [ ] **Step 4: Run all unit tests**

Run:

```bash
python3 -m unittest research/test_backtest_live_recorder.py -v
```

Expected: all tests pass with no warnings or errors.

- [ ] **Step 5: Run a small fixture smoke test and inspect only summary values**

Run:

```bash
python3 -m unittest research/test_backtest_live_recorder.py -v
```

Expected: the end-to-end fixture reports one fill, one win, and the expected PnL for the selected configuration.

- [ ] **Step 6: Commit the analyzer slice if a Git repository is available**

```bash
git add research/backtest_live_recorder.py research/test_backtest_live_recorder.py
git commit -m "feat: add streaming Chainlink paper backtest"
```

---

### Task 4: Run the production backtest on the recorder VM

**Files:**
- Create during run: `/tmp/live-chainlink-backtest` on the server only
- Create locally: `research/results/live_chainlink_backtest/live_chainlink_backtest_summary.json`
- Create locally: `research/results/live_chainlink_backtest/live_chainlink_backtest_fills.csv`

- [ ] **Step 1: Run the analyzer remotely without touching recorder data**

Use the local script as stdin so no application files are installed on the server:

```bash
mkdir -p research/results/live_chainlink_backtest
ssh -o BatchMode=yes -o ConnectTimeout=10 ubuntu@158.178.155.78 \
  'mkdir -p /tmp/live-chainlink-backtest && python3 - --data-dir /opt/recorder/data --out-dir /tmp/live-chainlink-backtest' \
  < research/backtest_live_recorder.py
```

The analyzer must print only compact progress/final locations and exit zero. Before/after checks must confirm the recorder container remains healthy and the data directory's newest file continues growing.

- [ ] **Step 2: Retrieve only compact artifacts**

```bash
scp -q ubuntu@158.178.155.78:/tmp/live-chainlink-backtest/live_chainlink_backtest_summary.json research/results/live_chainlink_backtest/
scp -q ubuntu@158.178.155.78:/tmp/live-chainlink-backtest/live_chainlink_backtest_fills.csv research/results/live_chainlink_backtest/
```

- [ ] **Step 3: Validate artifact shape and production safety**

Run a standard-library validation that parses the JSON and CSV, confirms nine config records, confirms `paper_only` is true, and prints only counts, date range, fill count, and PnL range. Verify the remote `pm-recorder` container is still `healthy` and no recorder data file was modified by the analyzer.

---

### Task 5: Produce the final report

**Files:**
- Create: `research/results/live_chainlink_backtest/FINAL_REPORT.md`

- [ ] **Step 1: Generate the Ukrainian final report from the validated JSON/CSV**

Include:

- exact server/data period and data-quality counters;
- whether the data was sufficient for this paper test;
- a table of all nine margin/floor configurations;
- fills, win rate, EV/share, total PnL, and breakdowns by symbol/timeframe/day;
- the 5m LUT-unavailable caveat;
- explicit conclusion on whether the tested Chainlink-native taker rule showed positive expectancy;
- limitations: paper fills, best ask only, inferred settlement, seven calendar days, no queue/slippage/full depth.

Do not call the strategy profitable based on a small or zero sample; state sample-size uncertainty directly.

- [ ] **Step 2: Run the final verification**

Run:

```bash
python3 -m unittest research/test_backtest_live_recorder.py -v
```

Then parse the final report inputs once more and ensure the displayed headline numbers match the JSON summary.
