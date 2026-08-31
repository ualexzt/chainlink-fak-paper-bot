# Фінальний звіт: BUY UP 50 + BUY DOWN 50

**Стратегія:** одночасно виставляти `BUY UP` і `BUY DOWN` по 50 shares, кожен ордер на 1¢ нижче midpoint, з перепоставленням котирування.  
**Тип:** quote-touch paper backtest, не підтверджені реальні maker fills.

## 1. Умови Polymarket

Офіційні сторінки:

- https://docs.polymarket.com/programs/maker-rebates
- https://docs.polymarket.com/programs/liquidity-rewards
- https://docs.polymarket.com/market-data/market-details#liquidity-reward-settings

Перевірені поточні параметри crypto-TWAP ринків:

| Параметр | Значення |
|---|---:|
| Мінімальний reward order size | 50 shares |
| Максимальна reward-відстань | 1.5¢ від midpoint |
| Tick size | 1¢ |
| Мінімальний CLOB notional | $5 |
| Maker fee | 0 за `takerOnly=true` |

Отже, 50 shares на відстані 1¢ формально проходять поточні пороги. Але reward виплата є pro-rata та залежить від конкуренції, часу, розміру pool і фактичної присутності ордера. Доларову reward/rebate суму в цей backtest не додавали, бо recorder не має історичного denominator усіх maker-ів і фактичних payout.

## 2. Дані та модель

- Snapshot із сервера: **2026-08-25 16:05:09 UTC — 2026-08-31 09:11:05 UTC**
- 7 JSONL-файлів
- **2,953,434** валідні рядки
- **6,579** ринків, з них 6,552 повних і 27 неповних
- BTC, ETH, SOL; таймфрейми 5m і 15m
- `quote_touch`: touch best ask виконує частину ордера за ціною нашого bid, використовуючи доступну ask quantity
- `strict_full`: fill лише коли видима ask quantity покриває всі 50 shares
- після partial fill ордер не replenished
- неповні ринки не включаються в PnL

Якість входу: malformed JSON — 0, дублікати — 0, out-of-order — 0.

## 3. Результат

| Режим | Complete | Full pairs | One/partial | Zero fill | Pair rate | Filled volume | PnL |
|---|---:|---:|---:|---:|---:|---:|---:|
| Quote-touch | 6,552 | 6,057 | 158 | 337 | 92.45% | 618,310 shares | **−$21,102.31** |
| Strict-full | 6,552 | 5,788 | 417 | 347 | 88.34% | 601,750 shares | **−$15,604.00** |

Середній результат на settled market:

- quote-touch: **−$3.22**;
- strict-full: **−$2.38**.

Щоб лише компенсувати settlement PnL, без урахування інших ризиків, потрібна середня reward/rebate виплата приблизно:

- quote-touch: **3.41¢ на кожен filled share**;
- strict-full: **2.59¢ на кожен filled share**.

## 4. Важливе уточнення: де саме виник збиток

Ти правий, що **одночасне** котирування може бути прибутковим. Окремий scan синхронних recorder snapshots перевірив саме це:

- 2,430,347 спостережень мали валідні UP і DOWN книги;
- у 1,853,166 спостереженнях 50-share quotes були валідні за notional;
- `quote_UP + quote_DOWN` був у діапазоні **$0.97–$0.98**;
- випадків `quote_UP + quote_DOWN > $1` — **0**;
- 99.94% таких quotes одночасно проходили reward-відстань ≤1.5¢;
- median longest continuous paired-book run на market — **158 секунд**.

Тобто на одному snapshot справді є gross edge 2–3¢ на paired unit, і часу для відправки двох ордерів зазвичай достатньо. Recorder має лише 1-second resolution, тому він не вимірює фактичну millisecond API latency.

Для повної пари потрібно:

```text
UP_fill_price + DOWN_fill_price < $1
```

Попередній PnL backtest виявив проблему не в початковій ціні, а в **lifecycle**: після fill однієї ноги симулятор залишав її ціну зафіксованою, але незалежно перепоставляв незаповнену ногу. Через це дві фактичні fill prices могли вже не походити з одного safe snapshot.

Результат цієї незалежної-requote моделі:

| Режим | Full pairs із сумарною вартістю ≤ $50 | Full pairs із вартістю > $50 | Середня вартість пари |
|---|---:|---:|---:|
| Quote-touch | 1,075 / 6,057 | 4,982 / 6,057 | **$53.39** |
| Strict-full | 1,688 / 5,788 | 4,100 / 5,788 | **$52.74** |

Отже, попередній −$21.1k/−$15.6k — це результат **наївного незалежного перепоставлення**, а не доказ того, що guarded pair strategy неможлива. Додатково 2.4–6.4% settled ринків залишали односторонній/partial inventory.

## 5. Reward metrics

У моделі було:

| Режим | Reward-eligible seconds | Relative score |
|---|---:|---:|
| Quote-touch | 112,366 | 2,593.67 |
| Strict-full | 193,522 | 5,636.89 |

Це лише відносний score з офіційною quadratic shape, без перерахунку в долари. Strict-full має більше reward-time, бо partial fill не зменшує залишок ордера нижче 50 shares.

## 6. Висновок

**Синхронно виставити safe pair у даних можливо.** Попередній негативний результат стосується лише моделі, яка після першого fill незалежно перепоставляла другу ногу й втрачала pair-price lock.

Правильна реалізація повинна:

1. рахувати обидві ціни з одного snapshot;
2. приймати пару лише коли `quote_UP + quote_DOWN <= $1 − buffer`;
3. після partial/first fill перепоставляти hedge leg лише якщо фінальна собівартість paired quantity все ще ≤$1 − buffer;
4. інакше cancel, reduce-only або обмежений inventory risk;
5. відправляти обидва ордери через batch `POST /orders` (Polymarket дозволяє 1–15 ордерів у запиті та обробляє їх паралельно, але latency/atomicity не гарантує);
6. виміряти фактичний час signal → accepted order → fill у live-paper режимі.

Maker rebates і liquidity rewards можуть бути додатковим доходом, але їх не можна використовувати як гарантію без фактичних payout даних.

Артефакти:

- `paired_maker_summary.json`
- `paired_maker_pairs.csv`
- `research/paired_maker_backtest.py`
- `research/test_paired_maker_backtest.py`
