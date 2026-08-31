# 90¢ late-window strategy with one switch — final paper backtest

## Headline

**Один switch суттєво покращив результат, але не зробив всю стратегію прибутковою.**

Для основної моделі з реальною displayed depth 50 shares:

- 979 початкових fills;
- hold: **89.89% WR**, PnL **−$363.39**;
- зі switch: **91.52% WR**, PnL **−$213.47**;
- покращення від switch: **+$149.92**;
- 16 виконаних switch, усі 16 правильно визначили фінального переможця;
- однак кожен успішний switch все одно завершився приблизно **−$35.95 на 50 shares**.

Отже, switch справді зменшує збиток програшної угоди, але схема `BUY 90¢ → SELL 10¢ → BUY opposite 90¢` не перекуповує її в нуль.

## Правила

- BTC, ETH, SOL; 5-minute Up/Down.
- Вхід лише в останні 150 секунд (`age >= 150`).
- Перший однозначний rising cross сторони з `<90¢` до `>=90¢`.
- 50 shares по limit **90¢**; jump вище 90¢ не вважається fill.
- Максимум один switch після пізнішого rising cross протилежної сторони до 90¢.
- Switch: SELL усі 50 shares початкової сторони за її best bid, потім BUY 50 протилежної по 90¢.
- Усі три операції — taker із fee з офіційного Gamma `feeSchedule`.
- Settlement лише через resolved Gamma `outcomePrices`.

## Dataset

- UTC: **2026-08-25 — 2026-08-31**.
- 3,111,918 physical rows; 1,554,954 valid BTC/ETH/SOL 5m rows.
- 5,196 markets seen.
- **5,085 complete** markets з офіційними outcomes.
- 111 incomplete markets виключено з WR і PnL.
- 3,648 complete markets дали late-window 90¢ signal; 2 simultaneous crossings були ambiguous.

## Main results

| Metric | Strict 50-share depth | Optimistic touch |
|---|---:|---:|
| Complete markets | 5,085 | 5,085 |
| 90¢ signals | 3,648 | 3,648 |
| Initial fills | 979 | 1,756 |
| Fill rate per signal | **26.84%** | **48.14%** |
| Hold wins / losses | 880 / 99 | 1,574 / 182 |
| Hold WR | **89.89%** | **89.64%** |
| Hold PnL, 50 shares | **−$363.39** | **−$873.14** |
| Executed switches | 16 | 56 |
| Switch rate per fill | **1.63%** | **3.19%** |
| Opposite side finally won | 16/16 | 55/56 |
| WR after switch policy | **91.52%** | **92.71%** |
| Strategy PnL, 50 shares | **−$213.47** | **−$448.42** |
| Incremental switch effect | **+$149.92** | **+$424.72** |
| Average strategy PnL / fill | **−$0.218** | **−$0.255** |
| Net EV/share | **−0.436¢** | **−0.511¢** |

95% Wilson intervals:

- strict hold WR: **87.84–91.62%**;
- strict post-switch-policy WR: **89.61–93.11%**;
- strict switch direction: 16/16, but wide CI **80.64–100%**;
- optimistic switch direction: 55/56 = **98.21%**, CI **90.55–99.68%**.

## Що реально зробив switch

### Strict model

- Switch спрацював лише в **16 із 99** початкових losers — 16.16%.
- Усі 16 протилежних сторін зрештою виграли.
- Кожний switch продав held side по 10¢ і купив opposite по 90¢.
- Hold loser: приблизно **−$45.32** на 50 shares.
- Successful switch: приблизно **−$35.95**.
- Економія: **+$9.37** на rescued trade, але угода залишається великою втратою.

### Optimistic model

- 55 із 182 початкових losers були покращені.
- Один switch був false reversal: початкова сторона відновилась і виграла.
- 55 successful switches дали по +$9.37 проти hold loser.
- Один false switch погіршив результат приблизно на **−$90.63** проти hold winner.
- Сукупний net improvement все одно позитивний: **+$424.72**.

Це показує асиметрію: один whipsaw коштує приблизно як 9.7 успішних rescue switches.

## Timing

| Metric | Strict | Optimistic |
|---|---:|---:|
| Average initial age | 190.4s | 191.3s |
| Median switch age | 258s | 249s |
| Switch age range | 192–296s | 192–296s |
| Median time after entry | 59.5s | 68.5s |
| Time-after-entry range | 28–120s | 12–121s |

Switch переважно з'являється дуже пізно, коли held side вже торгується близько 10¢.

## Symbol breakdown

### Strict 50-share depth

| Symbol | Fills | Switches | Hold PnL | Strategy PnL | Switch improvement |
|---|---:|---:|---:|---:|---:|
| BTC | 509 | 9 | +$34.66 | **+$118.99** | +$84.33 |
| ETH | 263 | 5 | −$167.85 | −$121.00 | +$46.85 |
| SOL | 207 | 2 | −$230.21 | −$211.47 | +$18.74 |

### Optimistic touch

| Symbol | Fills | Switches | Hold PnL | Strategy PnL | Switch improvement |
|---|---:|---:|---:|---:|---:|
| BTC | 629 | 17 | −$53.14 | **+$106.15** | +$159.29 |
| ETH | 570 | 16 | +$170.45 | **+$320.37** | +$149.92 |
| SOL | 557 | 23 | −$990.46 | −$874.95 | +$115.51 |

Symbol results are exploratory; filtering to profitable symbols after seeing this sample would be in-sample selection and requires an independent validation period.

## Interpretation

1. The user's reversal hypothesis is supported directionally: a full opposite 90¢ crossing was highly predictive of final resolution in this sample.
2. The switch improved PnL in both execution models.
3. It does **not** hedge to zero because most of the original 90¢ stake has already disappeared before the switch.
4. Strict execution leaves too few switches (16) for a production conclusion.
5. A Chainlink-confirmed trigger may reduce the rare but extremely expensive false switch; that must be evaluated on newly recorded fresh resolver data.

## Limitations

- Paper-only; no orders were submitted.
- One-second snapshots cannot establish sub-second ordering or atomic SELL+BUY execution.
- Top-of-book depth does not prove the entire 50 shares would execute before the book moved.
- Optimistic touch assumes 50 shares from any positive displayed quantity and is an upper-bound model.
- No queue position, trade prints, acknowledgements, latency, or slippage beyond top price.
- Historical recorder Chainlink observations were stale, so they were deliberately excluded from reversal confirmation.

## Artifacts

- `summary.json`
- `trades.csv`
- Source: `research/backtest_90cent_switch_5m.py`
- Tests: `research/test_backtest_90cent_switch_5m.py`
