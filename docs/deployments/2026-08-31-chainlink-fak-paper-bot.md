# Chainlink FAK paper bot deployment runbook

## Acceptance checklist

- [ ] Recorder backup manifest, archive hash, extraction, and per-file verification are captured before any cleanup.
- [ ] The exact reviewed source commit is captured.
- [ ] The built image digest is captured.
- [ ] Compose config and non-root/container safety checks are captured.
- [ ] Engine smoke evidence includes fresh dashboard heartbeat, database write progress, public-feed health, and paper-only banner.
- [ ] The rollback source/image and the command to stop the new stack are captured.

Do not fill these items from memory or from a local build that was not deployed. Evidence is recorded only after the corresponding command completes.

## Scope and safety gate

This deployment is for the paper-only `paper-engine` service. It has no credentials, wallet mounts, host Docker socket, authenticated WebSocket, or order-submission path. It consumes only public Gamma, market WebSocket, and Chainlink RTDS data. `paper-watch` is read-only and may be attached or closed without changing engine state.

Before starting, review `paper/.env.example`, ensure `paper/runtime` is the intended bind directory, and confirm that no credential-like environment keys are present. Do not reuse an old recorder directory as the new runtime directory.

## Local build and smoke workflow

```sh
PYTHONPATH=paper python -m unittest discover -s paper/tests -p 'test_*.py'
python paper/scripts/security_scan.py paper
python -m py_compile paper/paper_bot/*.py paper/tests/*.py paper/scripts/*.py
cd paper
docker compose config
docker compose build paper-engine
docker compose up -d paper-engine
./scripts/paper-bot status
./scripts/paper-watch
./scripts/paper-bot stop
```

The Compose healthcheck requires a fresh `dashboard_snapshots.snapshot_ts_ms` heartbeat and runs SQLite checks through a read-only connection. A stale or missing heartbeat is unhealthy even when the container process is still running.

## Runtime locations and rollback

The service writes `/data/paper.db` and `/data/raw-journal`. Preserve the runtime directory when stopping for diagnosis. To roll back, stop the paper service, restore the previously reviewed source/image, and start only that reviewed version; do not run destructive Docker pruning or delete the runtime directory as part of rollback.

## Post-start review

After the service is healthy, attach the watcher and verify the explicit `PAPER ONLY — NO ORDERS` banner, market/book health, resolver freshness, strategy matrix, open inventory, and database/disk health. Record observed state and timestamps in the deployment evidence only after live commands run. Do not claim profitability, live execution, or reward income from this paper deployment.
