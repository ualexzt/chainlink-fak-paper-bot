# 90¢ Late-Window Single-Switch Backtest Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Backtest a 50-share 90¢ entry in the last 150 seconds with at most one full sell-and-rebuy switch at an opposite 90¢ crossing.

**Architecture:** A focused streaming simulator owns one state object per market and two execution tracks (`strict_50`, `optimistic_touch`). It reuses the reviewed official Gamma outcome loader and fee formula, emits one trade row per market/model, and compares switch PnL against holding the same initial fill.

**Tech Stack:** Python 3.11+, stdlib `dataclasses`, `csv`, `json`, `unittest`; existing recorder JSONL and Gamma outcomes.

---

The working directory is not a Git repository, so commit steps are intentionally omitted.

### Task 1: Entry and switch state machine

**Files:**
- Create: `research/test_backtest_90cent_switch_5m.py`
- Create: `research/backtest_90cent_switch_5m.py`

- [ ] **Step 1: Write failing state tests**

Test an entry before age 150 is rejected; an age-150 rising cross fills only at exactly 90¢; a jump to 91¢ signals but does not fill; strict requires ask quantity 50 while optimistic requires positive quantity; an opposite later crossing switches once; same-row, same-side, ambiguous, missing-book, timestamp-gap, insufficient depth, and second-switch cases do not execute incorrectly.

- [ ] **Step 2: Verify RED**

Run: `python3 -m unittest research.test_backtest_90cent_switch_5m.SwitchStateTests -v`

Expected: import failure because the implementation file does not exist.

- [ ] **Step 3: Implement minimal state machine**

Create `ExecutionTrack` and `MarketState.process_row(row)`. Keep previous paired asks for crossing continuity. Store initial and switch timestamps, ages, prices, quantities, and depth-rejection counters. Use constants `ENTRY_PRICE=0.90`, `POSITION_SIZE=50.0`, `MIN_ENTRY_AGE=150`, and `MAX_GAP_SECONDS=2`.

- [ ] **Step 4: Verify GREEN**

Run the same unittest class. Expected: all state tests pass.

### Task 2: Accounting and streaming runner

**Files:**
- Modify: `research/test_backtest_90cent_switch_5m.py`
- Modify: `research/backtest_90cent_switch_5m.py`

- [ ] **Step 1: Write failing accounting/runner tests**

Assert all three taker fee legs, hold counterfactual, switched settlement, rescued/harmed classification, incomplete-outcome exclusion, BTC/ETH/SOL filtering, artifact creation, and exact CSV-to-summary reconciliation for both execution models.

- [ ] **Step 2: Verify RED**

Run: `python3 -m unittest research.test_backtest_90cent_switch_5m -v`

Expected: accounting/runner tests fail because aggregation is absent.

- [ ] **Step 3: Implement runner**

Reuse `load_gamma_outcomes` and `taker_fee` from `research.backtest_90cent_5m.py`. Stream `*.jsonl`, validate/deduplicate rows, aggregate only official complete outcomes, write portable artifact names, and expose a CLI requiring `--outcomes-file`.

- [ ] **Step 4: Verify GREEN and compile**

Run:

```bash
python3 -m unittest research.test_backtest_90cent_switch_5m -v
python3 -m py_compile research/backtest_90cent_switch_5m.py
```

Expected: all tests pass and compilation exits zero.

### Task 3: Production-data run and report

**Files:**
- Create: `research/results/90cent_switch_5m/summary.json`
- Create: `research/results/90cent_switch_5m/trades.csv`
- Create: `research/results/90cent_switch_5m/FINAL_REPORT.md`

- [ ] **Step 1: Run on recorder host**

Copy the packaged outcomes and stream the script to `158.178.155.78`, using `/opt/recorder/data` and `/tmp/backtest-90cent-switch-5m`.

- [ ] **Step 2: Retrieve and reconcile artifacts**

Assert for both models that CSV fills, switches, wins, hold/switch PnL sums, rescued/harmed counts, and complete-market denominators equal `summary.json`.

- [ ] **Step 3: Write report**

Headline whether switching improves total PnL versus hold. Include execution-model range, fill/switch rates, rescued versus harmed cases, BTC/ETH/SOL, fee drag, and non-atomic one-second limitations.

- [ ] **Step 4: Fresh verification and review**

Run all research tests, compilation, artifact reconciliation, and a read-only reviewer pass. Expected: no Critical/Important findings.
