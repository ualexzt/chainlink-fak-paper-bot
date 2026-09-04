# Quality-first shadow deployment — 2026-09-04

## Scope

- Server: `/home/ubuntu/chainlink-fak-paper-bot`
- Service: `paper-paper-engine-1`
- Final code commit: `6bc77174a8b3ab4b3e9a214a0b719bf92293ea55`
- Strictly public-data, paper-only operation; no venue order path or credentials.
- `QUALITY_SHADOW_ONLY=true` disables legacy lanes, Monte Carlo decisions, simulated
  order creation, and the high-volume raw feed journal.

## Forward rule

1. At exact market age 30, select exactly one side with best ask at least `0.60`.
2. Apply the frozen warning filters through ages 90 and 120.
3. At age 120, record a paper entry only when selected ask is at least `0.88`.
4. From ages 121 through 240, arm one full switch after a selected-bid drawdown
   of at least `0.20` for three consecutive complete one-second samples.
5. Execute the paper switch on the next complete second and settle from official
   Gamma outcome data only.

The journal retains one compact causal top-of-book row per market second,
including top-level price/size, aggregate depth, book generation, and the latest
public resolver view. It also records candidate, rejection, paper entry, switch,
and official settlement events.

## Preservation and verification

- Pre-deploy backup:
  `paper/runtime/backups/paper-pre-quality-shadow-20260904T045906Z.db`
- Backup SHA-256:
  `1430e9cb54226badb06e6ae904038e27a24db8f0a6f1af694a3b2cdaa15be4f7`
- SQLite integrity was `ok`; foreign-key violations were zero.
- Local suite: 234 tests passed; compile, diff, and security checks passed.
- Server image: targeted quality lifecycle/restart/timing tests and security scan passed.

## Live evidence

- Container reached `running/healthy`, restart count zero, OOM false.
- Dashboard reported `QUALITY_SHADOW_ONLY`; books were valid and all public health
  reasons were clear.
- The initial one-second timer skipped age 30 in one live round. Commit `6bc7717`
  changed boundary polling to 0.2 seconds while retaining exactly one journal row
  per integer market second.
- In the next untouched round (`mkt_ts=1788498900`), all three assets recorded a
  complete age-30 sample: ETH selected `DOWN @ 0.62`, SOL selected `DOWN @ 0.70`,
  and BTC recorded `NO_SIGNAL` at asks `0.44/0.57`.
- At age 120, ETH was rejected at `0.78` by the `0.88` entry floor; SOL recorded
  a paper-only entry at `0.93` for `1.075268817204301075268817204` shares per USD 1.
- Legacy database counters remained unchanged across the live observation:
  signals `1763`, paper orders `1801`, Monte Carlo forecasts `3625`, settlements
  `1301`, and lane results `1725`.
- The new journal was owned by UID/GID `1000:1000`, grew during observation, and
  contained only `quality_shadow` source rows.

This verifies immediate operation and causal capture, not long-run profitability.
