# Paired UP/DOWN Maker Rewards Backtest Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Simulate simultaneous 50-share UP/DOWN resting bids one cent below midpoint on recorder JSONL data, including quote-touch fills, residual settlement risk, and reward-eligibility metrics.

**Architecture:** Add a standalone standard-library analyzer that streams the server's daily JSONL files and keeps one small state machine per market. It tests the previous resting quotes before repricing, supports both partial/quote-touch and strict-full-order fill modes, and writes compact pair-level and aggregate artifacts without modifying recorder data.

**Tech Stack:** Python 3 standard library, JSONL, CSV, JSON, unittest, SSH.

---

### Task 1: Define failing tests for pair pricing, fills, settlement, and reward eligibility

**Files:**
- Create: `research/test_paired_maker_backtest.py`
- Test: `research/test_paired_maker_backtest.py`

- [ ] **Step 1: Write the failing tests**

```python
import json
import tempfile
import unittest
from pathlib import Path

from research.paired_maker_backtest import (
    Config,
    MarketState,
    quote_price,
    settle_pair,
    simulate_market,
)


class PairPrimitiveTests(unittest.TestCase):
    def test_quote_is_one_cent_below_midpoint_on_tick_grid(self):
        self.assertEqual(quote_price(0.505), 0.49)
        self.assertEqual(quote_price(0.51), 0.50)
        self.assertEqual(quote_price(0.01), 0.0)

    def test_settlement_pays_winning_leg_and_exposes_unhedged_loss(self):
        state = MarketState("btc", "15m", 1000, 900)
        state.up_filled = 50
        state.down_filled = 0
        state.up_cost = 24.5
        self.assertEqual(settle_pair(state, "UP"), {
            "payout": 50.0,
            "cost": 24.5,
            "pnl": 25.5,
            "paired": False,
        })
        self.assertEqual(settle_pair(state, "DOWN")["pnl"], -24.5)

    def test_full_pair_locks_two_share_balanced_profit(self):
        state = MarketState("btc", "15m", 1000, 900)
        state.up_filled = state.down_filled = 50
        state.up_cost = 24.5
        state.down_cost = 24.0
        result = settle_pair(state, "UP")
        self.assertEqual(result["paired"], True)
        self.assertAlmostEqual(result["payout"], 50.0)
        self.assertAlmostEqual(result["pnl"], 1.5)

    def test_quote_touch_fills_before_requote_and_partial_quantity_is_capped(self):
        rows = [
            {"ts": 1000, "age": 0, "up_bid": 0.49, "up_ask": 0.51, "up_askq": 100,
             "dn_bid": 0.49, "dn_ask": 0.51, "dn_askq": 100},
            {"ts": 1001, "age": 1, "up_bid": 0.48, "up_ask": 0.49, "up_askq": 7,
             "dn_bid": 0.49, "dn_ask": 0.51, "dn_askq": 100},
        ]
        state = simulate_market("btc", "15m", 1000, rows, mode="quote_touch")
        self.assertEqual(state.up_filled, 7.0)
        self.assertEqual(state.down_filled, 0.0)
        self.assertEqual(state.up_fill_count, 1)

    def test_strict_full_mode_does_not_count_small_touch(self):
        rows = [
            {"ts": 1000, "age": 0, "up_bid": 0.49, "up_ask": 0.51, "up_askq": 100,
             "dn_bid": 0.49, "dn_ask": 0.51, "dn_askq": 100},
            {"ts": 1001, "age": 1, "up_bid": 0.48, "up_ask": 0.49, "up_askq": 7,
             "dn_bid": 0.49, "dn_ask": 0.51, "dn_askq": 100},
        ]
        state = simulate_market("btc", "15m", 1000, rows, mode="strict_full")
        self.assertEqual(state.up_filled, 0.0)

    def test_reward_eligibility_requires_both_50_share_orders_within_1_5_cents(self):
        row = {"up_bid": 0.49, "up_ask": 0.51, "dn_bid": 0.49, "dn_ask": 0.51}
        state = MarketState("btc", "15m", 1000, 900)
        state.up_order_price = state.down_order_price = 0.49
        state.up_remaining = state.down_remaining = 50.0
        state.update_reward_seconds(row, min_size=50, max_spread_cents=1.5)
        self.assertEqual(state.reward_eligible_seconds, 1)
        state.up_remaining = 49.0
        state.update_reward_seconds(row, min_size=50, max_spread_cents=1.5)
        self.assertEqual(state.reward_eligible_seconds, 1)


class PairRunTests(unittest.TestCase):
    def test_run_backtest_writes_summary_and_pair_rows(self):
        from research.paired_maker_backtest import run_backtest
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "data"
            out = Path(tmp) / "out"
            root.mkdir()
            rows = []
            for age in range(300):
                rows.append({
                    "ts": 1000 + age,
                    "symbol": "btc",
                    "tf": "5m",
                    "mkt_ts": 1000,
                    "age": age,
                    "strike_cl": 100.0,
                    "cl_twap60": 101.0 if age == 299 else 100.0,
                    "up_bid": 0.49,
                    "up_ask": 0.51 if age == 0 else 0.49,
                    "up_bidq": 100,
                    "up_askq": 50,
                    "dn_bid": 0.49,
                    "dn_ask": 0.51,
                    "dn_bidq": 100,
                    "dn_askq": 50,
                })
            (root / "2026-01-01.jsonl").write_text(
                "".join(json.dumps(row) + "\n" for row in rows)
            )
            summary = run_backtest(root, out)
            self.assertEqual(summary["config"]["order_size"], 50)
            self.assertIn("quote_touch", summary["modes"])
            self.assertTrue((out / "paired_maker_summary.json").exists())
            self.assertTrue((out / "paired_maker_pairs.csv").exists())


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run tests and verify the expected red failure**

```bash
python3 -m unittest research/test_paired_maker_backtest.py -v
```

Expected: import failure because `research/paired_maker_backtest.py` does not exist.

---

### Task 2: Implement the pair state machine and pass primitive tests

**Files:**
- Create: `research/paired_maker_backtest.py`
- Test: `research/test_paired_maker_backtest.py`

- [ ] **Step 1: Implement the minimum tested API**

Define:

```python
@dataclass
class Config:
    order_size: float = 50.0
    offset_cents: float = 1.0
    tick: float = 0.01
    min_reward_size: float = 50.0
    max_reward_spread_cents: float = 1.5

@dataclass
class MarketState:
    symbol: str
    tf: str
    mkt_ts: int
    step: int
    up_filled: float = 0.0
    down_filled: float = 0.0
    up_cost: float = 0.0
    down_cost: float = 0.0
    up_fill_count: int = 0
    down_fill_count: int = 0
    up_order_price: float | None = None
    down_order_price: float | None = None
    up_remaining: float = 50.0
    down_remaining: float = 50.0
    reward_eligible_seconds: int = 0
    reward_relative_score: float = 0.0
```

`quote_price(midpoint, offset=0.01, tick=0.01)` rounds downward to the tick grid and clamps prices to `[0, 1]`. `settle_pair` returns cost, payout, PnL, and `paired` using filled quantities and the supplied winner. `simulate_market` processes rows in timestamp order, initializes both bids from the first valid pair of books, checks the previous quote against the current ask before repricing, supports `quote_touch` and `strict_full`, and settles on the exact terminal row.

- [ ] **Step 2: Run focused tests and verify green**

```bash
python3 -m unittest research/test_paired_maker_backtest.py -v
```

Expected: all primitive and state-machine tests pass.

---

### Task 3: Add streaming production ingestion and reward/economic reporting

**Files:**
- Modify: `research/paired_maker_backtest.py`
- Test: `research/test_paired_maker_backtest.py`

- [ ] **Step 1: Add a failing test for incomplete settlement and config aggregates**

Add a fixture with a market that has valid books and a one-leg fill but no `age == step - 1` row. Assert it appears in `incomplete_markets`, has no settlement PnL, and contributes to order/fill counters but not completed pair PnL.

- [ ] **Step 2: Run the focused test and verify it fails**

```bash
python3 -m unittest research/test_paired_maker_backtest.py -v
```

Expected: failure because incomplete-market accounting is not yet present.

- [ ] **Step 3: Implement `run_backtest(data_dir, out_dir)`**

Stream sorted `*.jsonl` files. For every valid `(symbol, tf, mkt_ts, ts)` row:

- count malformed, blank, duplicate, out-of-order, missing-book, invalid-book, and missing-terminal rows;
- maintain one `MarketState` per market;
- capture first positive `strike_cl` at age >= 59;
- process quote-touch and strict-full simulations independently without rereading input;
- treat `up_askq`/`dn_askq` as available quantity for quote-touch partial fills;
- only settle on `age == step - 1` with valid `cl_twap60` and strike;
- score UP/DOWN winning shares at $1 and losing shares at $0;
- count reward-qualified seconds only while both remaining orders are at least 50 and each quote is within 1.5 cents of its midpoint;
- calculate relative quadratic reward score with multiplier and denominator omitted;
- classify each market as zero-leg, one-leg, or full-pair and aggregate by symbol, timeframe, and UTC day.

Write:

- `paired_maker_summary.json` with data quality, official thresholds, market/leg/fill classifications, quote/fill coverage, reward-qualified seconds/relative score, and both simulation modes;
- `paired_maker_pairs.csv` with one row per market and mode, including fill quantities/prices, costs, payout, settlement winner, PnL, reward metrics, and completeness.

Use `paper_only: true` and explicitly state that rebates/reward dollars are not estimated without historical pool/competitor denominators.

- [ ] **Step 4: Run all focused tests**

```bash
python3 -m unittest research/test_paired_maker_backtest.py -v
```

Expected: all tests pass.

---

### Task 4: Run on the server and validate artifacts

**Files:**
- Create remotely: `/tmp/paired-maker-backtest/*`
- Create locally: `research/results/paired_maker_backtest/paired_maker_summary.json`
- Create locally: `research/results/paired_maker_backtest/paired_maker_pairs.csv`

- [ ] **Step 1: Execute without modifying recorder data**

```bash
mkdir -p research/results/paired_maker_backtest
ssh -o BatchMode=yes -o ConnectTimeout=10 ubuntu@158.178.155.78 \
  'rm -rf /tmp/paired-maker-backtest && mkdir -p /tmp/paired-maker-backtest && python3 - --data-dir /opt/recorder/data --out-dir /tmp/paired-maker-backtest' \
  < research/paired_maker_backtest.py
```

- [ ] **Step 2: Retrieve compact artifacts**

```bash
scp -q ubuntu@158.178.155.78:/tmp/paired-maker-backtest/paired_maker_summary.json research/results/paired_maker_backtest/
scp -q ubuntu@158.178.155.78:/tmp/paired-maker-backtest/paired_maker_pairs.csv research/results/paired_maker_backtest/
```

- [ ] **Step 3: Validate invariants**

Parse JSON/CSV and assert:

- both modes exist;
- rows are only 5m/15m and symbols are BTC/ETH/SOL;
- `paired` means both filled quantities are exactly 50;
- every settled PnL equals payout minus cost;
- no settlement is scored for incomplete markets;
- reward dollars are absent/unknown, while relative score and qualifying seconds are present;
- recorder remains `running/healthy` and its current data file grows over a five-second check.

---

### Task 5: Write and verify the final report

**Files:**
- Create: `research/results/paired_maker_backtest/FINAL_REPORT.md`

- [ ] **Step 1: Generate the Ukrainian report**

Include official current thresholds, strategy definition, all mode totals, paired/one-leg/zero-leg rates, cost/payout/PnL, reward eligibility metrics, symbol/timeframe/day breakdowns, and the distinction between deterministic pair economics and unknown reward payouts. State that the result is quote-touch paper data, not actual maker execution, and that 50 shares at exactly the minimum can lose eligibility after a partial fill.

- [ ] **Step 2: Fresh final verification**

```bash
python3 -m unittest research/test_paired_maker_backtest.py -v
python3 -m py_compile research/paired_maker_backtest.py research/test_paired_maker_backtest.py
```

Then validate the report headline values against `paired_maker_summary.json` before presenting the final report.
