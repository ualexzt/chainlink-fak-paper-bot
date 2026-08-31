# RTDS Resolver Recorder Hardening Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Reconnect a silently stale RTDS feed and persist fresh Chainlink resolver-direction fields for future lead/lag research.

**Architecture:** Add small pure helpers for observation acceptance, connection watchdog status, start-value capture, and resolver metrics. `rtds_task` uses per-connection selected-symbol TWAP-60 monotonic timestamps to force reconnects; `aggregator` emits backward-compatible additional fields.

**Tech Stack:** Python 3.12, asyncio, websockets, aiohttp, unittest, Docker Compose.

---

The working directory is not a Git repository, so commit steps are intentionally omitted.

### Task 1: Pure resolver and watchdog behavior

**Files:**
- Create: `recorder/test_recorder.py`
- Modify: `recorder/recorder.py`

- [ ] **Step 1: Write failing tests**

Set `DATA_DIR` to a temporary directory before importing recorder. Test:

```python
assert stale_rtds_symbols({"btc": 1.0}, ["btc", "eth"], now=12.0, timeout=10.0) == ["btc", "eth"]
assert accept_cl_observation((100.0, 2000), 99.0, 1999) is False
assert capture_resolver_start(None, 100.0, 300_000, 300) == 100.0
assert resolver_metrics(101.0, 100.0) == (1.0, 100.0, "UP")
```

Also test the first ten-second capture boundary, fresh/stale age, and five-second history momentum.

- [ ] **Step 2: Verify RED**

Run: `python3 -m unittest recorder.test_recorder -v`

Expected: missing helper failures.

- [ ] **Step 3: Implement pure helpers and state**

Add `RTDS_STALE_SEC`, `RTDS_START_CAPTURE_SEC`, bounded `State.cl_history`, and `State.resolver_starts`. Implement helpers with finite/positive timestamp validation and no fallback when start capture is missed.

- [ ] **Step 4: Verify GREEN**

Run the same unittest command. Expected: all helper tests pass.

### Task 2: Wire the RTDS watchdog and output fields

**Files:**
- Modify: `recorder/test_recorder.py`
- Modify: `recorder/recorder.py`
- Modify: `recorder/.env.example`

- [ ] **Step 1: Write failing integration-oriented tests**

Test that a valid newer TWAP-60 update refreshes the selected symbol and history, while PONG/unrelated/stale observations do not. Test an aggregate-row helper emits `cl_age_ms`, `cl_fresh`, resolver start/distance/leader, and 5s momentum while stale/missed-start values remain null.

- [ ] **Step 2: Verify RED**

Run: `python3 -m unittest recorder.test_recorder -v`.

- [ ] **Step 3: Implement minimal wiring**

Refactor frame handling into a tested helper. In each RTDS connection, track last valid TWAP-60 monotonic time per configured symbol. After every frame, raise a reconnect exception when any symbol exceeds `RTDS_STALE_SEC`; preserve the existing five-second retry. Extend aggregate rows through a tested resolver-field helper. Add `RTDS_STALE_SEC=10` to `.env.example` and production `.env` without changing other values.

- [ ] **Step 4: Verify tests and compilation**

Run:

```bash
python3 -m unittest recorder.test_recorder -v
python3 -m py_compile recorder/recorder.py recorder/test_recorder.py
```

Expected: all tests pass and compilation exits zero.

### Task 3: Deploy recorder-only and validate fresh data

**Files:**
- Modify on host: `/opt/recorder/recorder.py`, `/opt/recorder/.env`

- [ ] **Step 1: Capture pre-deployment state**

Confirm `pm-recorder` health, heartbeat, most recent JSONL timestamp, and no trading containers are touched.

- [ ] **Step 2: Deploy and rebuild**

Back up the remote file, copy `recorder.py`, update only `RTDS_STALE_SEC`, then rebuild/recreate `pm-recorder` with its existing Compose project.

- [ ] **Step 3: Validate**

Observe at least 30 seconds. Require: healthy/running container, advancing heartbeat and row timestamps, advancing `cl_obs_ts`, `0 <= cl_age_ms <= 10_000` on current BTC/ETH/SOL rows, `cl_fresh=true`, and resolver fields populated once an eligible market start is captured.

- [ ] **Step 4: Review**

Run a read-only reviewer pass over recorder changes and deployment evidence. Expected: no Critical/Important findings.
