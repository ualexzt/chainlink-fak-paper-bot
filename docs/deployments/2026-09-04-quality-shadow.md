# Quality-first shadow deployment — 2026-09-04

## Scope

- Server: `/home/ubuntu/chainlink-fak-paper-bot`
- Service: `paper-paper-engine-1`
- Quality strategy commit: `6bc77174a8b3ab4b3e9a214a0b719bf92293ea55`
- Modern dashboard commit: `f7f454fb60ec8e175933ba05c932a4f85c5b6bb6`
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
- SOL armed repair at age 167 (`bid 0.45`) and switched at age 168 by selling
  `DOWN @ 0.45` and observing the equal-share `UP @ 0.55` purchase. Official
  settlement later reported `UP`; the recorded no-fee paper PnL was
  `-0.032258064516129032258064516`, substantially better than the unswitched
  USD 1 loss but correctly classified as still negative.
- Legacy database counters remained unchanged across the live observation:
  signals `1763`, paper orders `1801`, Monte Carlo forecasts `3625`, settlements
  `1301`, and lane results `1725`.
- The new journal was owned by UID/GID `1000:1000`, grew during observation, and
  contained only `quality_shadow` source rows.

This verifies immediate operation and causal capture, not long-run profitability.

## Modern dashboard deployment

- Added responsive per-asset cards with market phase, 300-second progress rail,
  bid/ask movement, ask sparklines, signal/entry/filter/repair state, and explicit
  public-data context.
- Reworked `performance` around quality-shadow outcomes: settled decisions,
  signal hits, entries, repairs, positive outcomes, no-fee paper net, per-asset
  score, and recent official settlements.
- Reworked `activity` around the live strategy timeline and decision tape. The
  header always states `QUALITY SHADOW · NO ORDERS` so paper observations cannot
  be mistaken for live positions or venue orders.
- The engine now retains a bounded 48-sample price trail per market and a rolling
  250-result dashboard summary. The compact raw journal remains authoritative.
- Pre-recreate backup:
  `paper/runtime/backups/paper-pre-modern-tui-20260904T055203Z.db`
- Backup SHA-256:
  `163987280c431d4cbf850e06551ebd2819df0a9c34d4b9b6b9229dc7a104fac4`
- Local verification: 236 tests passed; compile, diff, and security checks passed.
- New server image: 43 strategy/engine/TUI tests passed; all 29 packaging and
  configuration tests passed from the server checkout; the image security scan
  passed.
- Live verification: all three views rendered without overflow at 120 and 180
  columns. The recreated container reached `running/healthy`, restart count zero,
  and OOM false; legacy database counters remained unchanged.
- Forward performance cards begin accumulating settled outcomes from this
  deployment. Earlier observations remain preserved in the raw journal rather
  than being reconstructed into dashboard statistics after seeing their result.

### Per-asset statistics extension

- Commit `f2ed025` replaced the compact asset table with separate BTC, ETH, and
  SOL statistic cards. Each card reports settled rounds, age-30 signal accuracy,
  skipped-signal accuracy, entry rate, average entry, profitable-trade rate,
  repair rate, net/average no-fee PnL, cumulative PnL sparkline, and recent trade
  direction.
- `30s OUTCOMES` includes every officially settled signal (`✓` hit, `×` miss,
  `·` no signal), including candidates rejected at age 120. Rejected candidates
  remain no-trades and are shown as `SKIP · HIT` or `SKIP · MISS` in the decision
  table; they never enter paper PnL.
- Pre-recreate backup:
  `paper/runtime/backups/paper-pre-asset-stats-20260904T063942Z.db`
- Backup SHA-256:
  `dd2751bfb9ee5a189ebd790839f93fee0781334fb611ffe49b61313bb5df8bd4`
- Verification: 236 local tests passed; 18 TUI/quality tests and the security
  scan passed in the server image. Live `performance` rendered without clipping
  at both 120 and 180 columns; the recreated service returned healthy with zero
  restarts and no OOM kill.
