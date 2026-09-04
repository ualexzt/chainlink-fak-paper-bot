# Chainlink FAK paper bot

This is a read-only, paper-only experiment for BTC, ETH, and SOL five-minute Up/Down markets. It consumes public Gamma market metadata, the public market WebSocket, and public Chainlink RTDS TWAP-60 observations. It simulates FAK fills with exact `Decimal` arithmetic and persists immutable evidence in SQLite; it has no wallet, authenticated client, user WebSocket, order endpoint, or live-order capability.

## Components and safety boundary

`paper-engine` owns discovery, public feeds, books, resolver freshness, one-shot strategy lanes, virtual FAK execution, reverse simulation, settlement, raw JSONL journaling, and SQLite WAL persistence. `paper-watch` is a separate Rich terminal process. It opens the database with SQLite `mode=ro` and cannot alter engine state.

The original 36 lanes remain fixed: four price thresholds times three Chainlink confirmations times three position policies. A separate HOLD-only Monte Carlo shadow experiment observes the first valid causal event in each fixed `(60,90]`, `(30,60]`, and `(0,30]` seconds-to-close window. It block-bootstraps 10,000 paths from accepted Chainlink TWAP-60 changes, preserving short local dependence, and records both accepted and rejected forecasts. Feed gaps split history into independent segments and are never converted into synthetic returns; stale or incomplete data waits only within its fixed window and becomes an explicit rejection if no valid snapshot arrives. It simulates a `$5` FAK entry only when the leading outcome ask is between `0.85` and `0.90` and the estimated win probability exceeds the exact fee-adjusted break-even probability by at least three percentage points. The versioned `MC_BOOTSTRAP_*_V3` lanes cannot submit real orders; V1/V2 evidence remains immutable for comparison.

New virtual entries are fail-closed when the journal, database, dashboard snapshot, discovery, or processing health is critical. Open positions are retained through disconnects and are settled only from final official Gamma `outcomePrices`; provisional or inferred values are not settlement evidence.

By default `QUALITY_SHADOW_ONLY=true` disables all legacy paper-entry lanes and the
high-volume raw feed journal. The engine records one compact causal top-of-book row
per market second and observes only the forward-validation rule selected on the
frozen historical cohort: an unambiguous `ask >= 0.60` side at age 30, the two
warning filters through ages 90/120, entry eligibility at age 120 with
`ask >= 0.88`, and at most one full-switch repair after a three-second `0.20` bid
drawdown (trigger no later than age 240, execution on the next observed second).
These are observational paper actions only; no venue order is constructed or sent.
In this mode the overview uses live asset cards with a 300-second progress rail,
recent ask sparkline, exact 30/120-second milestones, warning-filter lights, and
repair state. `--view performance` shows only forward, officially settled shadow
results; `--view activity` separates the live decision timeline from the settled
decision tape. Paper PnL remains explicitly fee-free and is never presented as a
live fill or account balance.

## Local operation

From the repository root:

```sh
cd paper
cp .env.example .env                 # optional; contains public settings only
./scripts/paper-bot start             # build and start paper-engine
./scripts/paper-bot status
./scripts/paper-watch                         # calm overview with market pulse and composite signals
./scripts/paper-watch --view performance      # settled strategy and Monte Carlo scoreboards
./scripts/paper-watch --view activity         # recent Monte Carlo decisions and feed events
./scripts/paper-bot logs              # follow engine logs
./scripts/paper-bot stop
```

The lifecycle wrapper accepts only `start`, `stop`, `status`, and `logs`. The watcher invokes only `python -m paper_bot.cli watch --db /data/paper.db`; it never receives credentials or network adapters. For a one-shot database check:

```sh
PYTHONPATH=paper python -m paper_bot.cli check-db
```

The default `overview` is deliberately compact and refreshes every two seconds in
the terminal alternate screen to avoid scrollback churn and visible full-screen
flashing. `Our signals` is a derived, read-only explanation layer over persisted
evidence:

- `CONFIRMED` means the deduplicated base-lane direction and eligible Monte Carlo direction agree;
- `BASE ONLY` or `MC ONLY` identifies which model has produced directional evidence;
- `CONFLICT` means the sources disagree, and `WAIT` means neither has an eligible direction;
- base support counts a threshold/confirmation/side trigger once, not once per exit policy;
- Monte Carlo support reports eligible horizons out of the immutable observations received so far.

These labels are observational paper signals. They never create an order or change engine state.

`Open paper inventory` is position state, not a new signal. It shows the persisted
UTC round and market ID. A row is labelled `ACTIVE POSITION` before market close
and `AWAITING SETTLEMENT` afterward; it remains visible until an official Gamma
outcome settles and closes the virtual lots.

The container binds `paper/runtime` to `/data`. The database is `/data/paper.db`; raw journals are `/data/raw-journal/raw-events-YYYY-MM-DD.jsonl` and compressed closed-day archives. The runtime directory is intentionally excluded from the image and should be backed up independently.

## Configuration and experiment identity

`.env.example` is the complete public configuration surface:

```text
SYMBOLS=btc,eth,sol
ENTRY_THRESHOLDS=0.80,0.85,0.89,0.90
PAPER_NOTIONAL_USD=5.00
RTDS_STALE_SEC=10
DATA_DIR=/data
LOG_LEVEL=INFO
```

Strategy-affecting settings are canonicalized into an experiment hash. Changing them creates a new experiment version; old signals, fills, inventory, settlements, and lane results are never rewritten. Operational paths and log verbosity do not change the strategy identity.

## Backup and verification

Recorder backup tooling is deliberately non-destructive. It creates a sorted manifest of regular files with sizes and streaming SHA-256 hashes, and verifies an extracted tree plus archive hash:

```sh
python paper/scripts/backup_recorder.py manifest \
  --root /path/to/recorder/data --output /path/to/recorder-manifest.json
python paper/scripts/backup_recorder.py verify \
  --root /path/to/extracted/data \
  --manifest /path/to/recorder-manifest.json \
  --archive /path/to/recorder-data.tar.zst
```

The script rejects symlinks, special files, absolute or escaping manifest paths, and changed bytes. It never removes recorder data. Remote stop, download, and deletion remain explicit operator-controlled steps.

## Evaluation policy

The initial experiment is paper-only and evaluated over seven calendar days. Report realized settlement P&L, open exposure, fill/reverse evidence, and unresolved markets separately. Do not infer profitability from container liveness, quote attempts, or provisional settlement values. Scaling decisions require the documented observation period and must not be made by changing a bucket limit alone.

Evaluate Monte Carlo independently by window and asset. Report every observation (`ENTER`, `REJECT`, and `MISSED`), probability calibration, win rate, fee-inclusive net P&L, entry count, rejection reasons, and chaotic-tail losses. Compare it with the contemporaneous `0.85` baseline lanes; do not compare only selected Monte Carlo entries against all baseline markets. The model is a causal empirical filter, not a guarantee that a quoted probability is correct.

There is no command in this project that submits, cancels, or authenticates an order.
