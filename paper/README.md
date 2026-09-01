# Chainlink FAK paper bot

This is a read-only, paper-only experiment for BTC, ETH, and SOL five-minute Up/Down markets. It consumes public Gamma market metadata, the public market WebSocket, and public Chainlink RTDS TWAP-60 observations. It simulates FAK fills with exact `Decimal` arithmetic and persists immutable evidence in SQLite; it has no wallet, authenticated client, user WebSocket, order endpoint, or live-order capability.

## Components and safety boundary

`paper-engine` owns discovery, public feeds, books, resolver freshness, one-shot strategy lanes, virtual FAK execution, reverse simulation, settlement, raw JSONL journaling, and SQLite WAL persistence. `paper-watch` is a separate Rich terminal process. It opens the database with SQLite `mode=ro` and cannot alter engine state.

New virtual entries are fail-closed when the journal, database, dashboard snapshot, discovery, or processing health is critical. Open positions are retained through disconnects and are settled only from final official Gamma `outcomePrices`; provisional or inferred values are not settlement evidence.

## Local operation

From the repository root:

```sh
cd paper
cp .env.example .env                 # optional; contains public settings only
./scripts/paper-bot start             # build and start paper-engine
./scripts/paper-bot status
./scripts/paper-watch                 # attach read-only dashboard
./scripts/paper-bot logs              # follow engine logs
./scripts/paper-bot stop
```

The lifecycle wrapper accepts only `start`, `stop`, `status`, and `logs`. The watcher invokes only `python -m paper_bot.cli watch --db /data/paper.db`; it never receives credentials or network adapters. For a one-shot database check:

```sh
PYTHONPATH=paper python -m paper_bot.cli check-db
```

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

There is no command in this project that submits, cancels, or authenticates an order.
