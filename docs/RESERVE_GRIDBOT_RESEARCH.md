# Reserve Gridbot Research

Last updated: 2026-07-21

## What This Adds

MoneyMan now has a separate research engine for the user's banded reserve idea. It does not replace the original pooled gridbot and it cannot place orders.

The engine uses:

- one real shared quote-cash balance;
- contiguous half-open bands such as `[2.60, 2.80)`;
- a fixed ceiling on each band's open lots whose cash has not yet been recovered;
- one independently tracked lot per entry level;
- an all-in level budget that includes the gross buy fee;
- a fee-aware exit that restores the lot's actual cash cost plus positive cash profit;
- residual XRP tagged to the purchase lot and originating band as reserve; and
- gross fees, modeled Coinbase One rebates, settled cash, open XRP, and reserve XRP kept separate.

Reserve-grid v1.4 is deliberately XRP-only. Its XRP amount, cash-allocation, and research price increments are explicit assumptions. The quote increment floors the all-in cash envelope assigned to each slot; it does not round prices, fees, or exchange settlement. The optional overflow tranche introduced in v1.2 remains available at every price level under a separate global active-cost ceiling, but stays disabled by default. V1.4 adds one optional causal EMA entry guard; `none` remains the default and exactly preserves the v1.3 fixed-control behavior.

Unused cash is shared idle cash. It may fund another band's lot, subject to that band's active-lot cap, but the caps do not create separate wallets or additional money. Completed reserve does not consume the active-lot cash cap because its principal has been returned, but it remains real XRP market exposure and is reported with cost basis and unrealized PnL.

## Exit Rule

For a filled lot:

```text
C = actual cash cost including the gross buy fee
Q = actual XRP received after base-increment rounding
P = rounded target exit price
f = gross modeled sell-fee rate
G = desired positive cash profit

sell_quantity = round_up((C + G) / (P * (1 - f)), base_increment)
reserve_quantity = Q - sell_quantity
```

The rounded result is checked again. The entry is skipped if the sale cannot return `C + G` while retaining at least one base increment of reserve. A modeled rebate is never used to make an exit viable.

## First Bounded XRP Experiment

The test period was selected from data coverage, not profitability. It is the known raw L2 outage patched by a complete Coinbase Exchange public candle file:

```text
2025-09-20T04:37:00Z through 2025-10-09T19:00:00Z
28,223 one-minute candles
0 missing minutes
provider: coinbase_exchange_public
raw source SHA-256: 3cee33117ecf2e9c4dcc4e4d02f3b9931fd72baa3b68c0fc5c29b7e7d48809da
exact selected-row SHA-256: 9b746a0c67a20d60d3d5c6b30730ef42a6b3879205214a0686c358745523c90a
engine: banded_lot_reserve_gridbot_v1.1
engine source SHA-256: 0f98fa346403eec00a6aaa4369d16a3159543848415eb7730cd7917ae1782ef3
```

Every run hashes the ordered candle rows actually consumed, the complete source file, the saved configuration, and the engine source. Run IDs include a random suffix and output folders cannot be silently reused.

The first candle opened at `2.9916`. The range rule was frozen from that first observation: take its globally anchored `$0.20` band and one adjacent band on each side. This produced `[2.60, 3.20)` without reading the future low or high.

Shared settings:

```text
starting cash:                 $1,000
starting XRP:                 0
bands:                        [2.60,2.80), [2.80,3.00), [3.00,3.20)
levels per band:              20
all-in cash budget per level: $5
active-lot cash cap per band: $100
cash-profit target:           20 bps of each lot's actual cash cost
gross maker fee assumption:   0.60% per fill
modeled rebate:               25%, capped at $100 across the run
```

The historical fee assumption is deliberately frozen. It is not claimed to be the account's actual September 2025 tier. The run crosses a month boundary, while the current rebate model uses one cap across the whole run, so before-rebate equity is authoritative.

Each candle path first traverses the real gap from the previous close to the recorded open. It then uses either `open -> low -> high -> close` or `open -> high -> low -> close`. Those are deterministic price-only assumptions, not observed trade order or L2 execution.

### Controlled Results

| Exit policy | Exit move | Completed lots | Cash-flow profit | Reserve XRP | End-open lots | Gross fees | Final equity before rebate | Final equity after modeled rebate |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| Principal recovery | 3% | 69 | $0.6901 | 1.866237 | 33 | $5.1284 | $996.5120 | $997.7941 |
| Principal recovery | 4% | 50 | $0.5001 | 2.192760 | 33 | $3.9872 | $997.2319 | $998.2287 |
| Principal recovery | 5% | 42 | $0.4201 | 2.545608 | 33 | $3.5067 | $998.1351 | $999.0118 |
| Full-lot cash control | 5% | 42 | $7.8698 | 0 | 33 | $3.5517 | $998.4912 | $999.3791 |

Verified run IDs:

- 3% reserve, low-first: `20260711T224036Z-15398069`
- 4% reserve, low-first: `20260711T224034Z-6137e91b`
- 5% reserve, low-first: `20260711T224034Z-2632cc43`
- 5% reserve, high-first: `20260711T224034Z-f2210c1a`
- 5% full-lot, low-first: `20260711T224035Z-2ea3d14f`
- 5% full-lot, high-first: `20260711T224036Z-6896ff7a`

Both candle-path assumptions produced the same 5% trade counts and final-equity results. Their close-sampled average active-cost utilization differed negligibly because some intermediate within-candle state differed.

The no-trade cash result was `$1,000`. Idealized, fee-free, fractional buy-and-hold fell to `$931.4748`. All tested grid profiles beat that idealized buy-and-hold reference during this falling period, but none beat holding cash before the modeled rebate.

### What The Result Means

The reserve mechanism worked exactly as intended:

- every completed 5% lot returned its all-in cash cost;
- each returned about one cent of cash-flow profit per `$5` envelope;
- each retained approximately `3.42%` of its purchased XRP as tagged reserve; and
- cash, base, PnL, fee, rebate, net-fee, turnover, cash-profit, and band-to-portfolio reconciliation errors were all zero.

The 5% reserve run ended with:

```text
shared cash:                 $835.4201
open-lot cash awaiting exit: $164.99995
open XRP:                    55.846336
reserve XRP:                 2.545608
reserve value:               $7.0936
reserve cost basis:          $7.1806
longest end-open lot:        1,691,520 seconds, about 19.6 days
maximum active cash:         $164.99996
average close-sampled active unrecovered cost: 10.58% of starting cash
```

The upper band completed no lots because a 5% target above its entries was not reached. The lower band completed 20 lots and the middle band completed 22. The reserve lost a small amount of marked value by the final candle, so the full-lot cash control finished about `$0.36` higher before rebate. That is the correct interpretation: reserve is a choice to remain long XRP, not value created from nowhere.

Reducing the exit from 5% to 3% produced more cycles, but the extra turnover and fees outweighed the smaller gains in this window. The 5% version was the strongest tested reserve profile here, although one falling 19.6-day period is not strategy validation.

## Bounded Overflow Experiment

V1.2 tested whether otherwise idle cash should duplicate the `$5` entry at active levels. This changed only capital allocation:

- the original base tranche and `$100` base cap in every band stayed unchanged;
- every price level had one overflow lane with at most one concurrently open `$5` overflow tranche;
- all overflow lots shared one `$100` global cap on cash still awaiting recovery;
- base always received fill priority at the same price;
- both tranches used the same real quote-cash wallet;
- overflow exits used the same fee, profit, reserve, and rounding rules; and
- reserve XRP from either tranche remained tagged and nonspendable.

The three base caps totaled `$300`, leaving `$700` above them at launch. The `$100` overflow cap was therefore fully covered by initially unallocated cash; this was not yet quiet-band borrowing. Fixed and overflow runs used the same v1.2 source, exact candle rows, fees, and paths:

```text
engine: banded_lot_reserve_gridbot_v1.2
engine source SHA-256: 48e6fe74495edfe593fcc2132d7dba866c84e43ffc9dc24895fd5b4d1a857763
exact selected-row SHA-256: 9b746a0c67a20d60d3d5c6b30730ef42a6b3879205214a0686c358745523c90a
```

### Overflow Results

| Allocation | Exit policy | Completed lots | End-open lots | Reserve XRP | Gross fees | Maximum active cash | Close-sampled max drawdown | Final equity before rebate |
|---|---|---:|---:|---:|---:|---:|---:|---:|
| Fixed base only | Principal recovery | 42 | 33 | 2.545608 | $3.5067 | $165.00 | $11.91 | $998.1351 |
| Base + $100 overflow | Principal recovery | 60 | 53 | 3.610260 | $5.1843 | $265.00 | $21.61 | $993.4949 |
| Fixed base only | Full-lot control | 42 | 33 | 0 | $3.5517 | $165.00 | $11.09 | $998.4912 |
| Base + $100 overflow | Full-lot control | 60 | 53 | 0 | $5.2485 | $265.00 | $20.44 | $994.0770 |

Both low-first and high-first paths produced the same filled-trade counts and end financial results. Close-sampled average utilization differed slightly, and overflow-cap miss attempts were `2,057` versus `2,063`, because intraminute traversal order still changed intermediate eligibility. All 38 total, layer, band, count, fee, turnover, reserve, and cap checks were zero in every run.

Verified v1.2 run IDs:

- fixed reserve, low/high: `20260712T000230Z-ffc5c287`, `20260712T000230Z-dd2a12a0`
- overflow reserve, low/high: `20260712T000231Z-dc23ea6b`, `20260712T000232Z-c57511fa`
- fixed full-lot, low/high: `20260712T000230Z-2c0af071`, `20260712T000230Z-1a8b246a`
- overflow full-lot, low/high: `20260712T000232Z-76cf3372`, `20260712T000232Z-5308a5c2`

The fixed and overflow runs have identical base-trade fingerprints within each path. The base tranche still completed 42 lots and ended with 33 open lots. Overflow alone added 18 completed and 20 end-open lots. That isolates the result: the extra `$100` deployment caused the difference rather than changing the original bot.

The simple overflow rule failed this bounded test. Relative to fixed principal recovery it:

- added only `$0.1800` of realized cash-flow profit;
- added about `$1.6775` of gross fees;
- added `20` unrecovered lots;
- increased maximum drawdown by about `$9.69`; and
- reduced final pre-rebate equity by about `$4.64`.

The full-lot overflow control was also about `$4.41` below fixed full-lot, so the main problem was additional falling-market exposure, not reserve retention alone.

Overflow allocation also showed deterministic path-order concentration. At the end, about `$45` of overflow cost was open in `[2.80,3.00)` and `$55` in `[3.00,3.20)`, while `[2.60,2.80)` received no overflow fills despite many later attempts. A first-eligible global pool is therefore not the same thing as intelligently lending cash to the most productive band.

## V1.3 Diagnostic-Only Recovery Layer

V1.3 adds observation without changing strategy behavior. It writes `lot_diagnostics.jsonl` separately from `lots.jsonl` and `events.jsonl`. Path/close observers update isolated diagnostic state during replay, aggregate labels are finalized afterward, and no diagnostic value can authorize, block, resize, recall, or exit a lot. A fixed-profile replay preserved the historical low-first and high-first base decision fingerprints exactly:

```text
low-first:  9cb35b763a22696a8a4317d28494926d416e91e96d5558362bef5dc5016a2655
high-first: a0419d634ddbdec2e82c63049231878b3ec34180e0548d40abfe6347ce3d9825
```

Verified v1.3 diagnostic runs:

```text
low-first:  20260712T173842Z-5ec1b606
high-first: 20260712T173903Z-118c645b
engine source SHA-256: f1abc1ffdb0f7b2944c7d279ff1888a3bc8aa478726c4985f25bb047142435ff
```

Both v1.3 paths also preserved `42` completed lots, `33` end-open lots, and `$998.1350891` final pre-rebate equity. The 28,223-row input declared a one-minute timeframe, had a 60-second maximum observed gap, and had zero diagnostic coverage gaps.

The sidecar reports:

- recovery time for completed lots and a right-censored observed age for open lots;
- 7-day, 14-day, and 28-day recovery cohorts, excluding lots without full follow-up from each percentage;
- exact 1-hour, 6-hour, 24-hour, and 7-day close markouts after every buy, even when the lot exits earlier;
- a close-sampled maximum adverse excursion and a separately labeled assumed-path adverse excursion;
- actual all-in unrecovered cash cost multiplied by observed lock time; and
- an independent event-stream cash-cost-time integral that must reconcile to zero.

The fixed 5% low-first replay produced:

| Diagnostic | Result |
|---|---:|
| Lots observed | 75 |
| Complete 7-day cohort | 53 |
| Recovered within 7 days | 31 of 53, 58.49% |
| Complete 14-day cohort | 46 |
| Recovered within 14 days | 40 of 46, 86.96% |
| Complete 28-day cohort | 0; run too short |
| Average assumed-path adverse excursion | 487.18 bps |
| Average close-sampled adverse excursion | 458.98 bps |
| Observed unrecovered cash-cost-time | 2,072.60 quote-currency days |
| Cash-cost-time reconciliation error | 0 |

Gross price-only close markouts averaged `+54.24` bps at 1 hour across 75 observable lots, `-23.19` bps at 6 hours across 74, `+45.96` bps at 24 hours across 69, and `+165.55` bps at 7 days across 53. Those averages are descriptive labels from one falling window. They are not fee-adjusted PnL, do not prove executable prices, and must not be read as a strategy signal without chronological training/holdout work.

An exact markout candle is required. A later close across a data gap is labeled delayed and not scored. A recovery percentage also requires the full follow-up horizon and adequate coverage, preventing young end-open lots from being silently counted as failures or early completed lots from biasing an incomplete cohort. A lot stamped at a gap's right boundary is conservatively cohort-ineligible because the candle timestamp cannot distinguish a close-to-open gap fill from a later intrabar fill. Path adverse excursion follows the configured low-first/high-first assumption. Recorded close values do not, although the assumed path can change a lot's entry/exit interval and therefore which closes belong to that lot. Neither measure is an L2 liquidity, spread, depth, queue, or toxic-flow measurement.

## Four-Window 2025 Regime Validation

The fixed 5% profile was next replayed over four non-overlapping calendar windows from May through August 2025. The windows were frozen before their backtests were run. Each range used the first candle only: take the globally anchored `$0.20` band containing the first open and add one adjacent band on each side. Every other setting stayed at the documented fixed control: 20 levels per band, a `$5` all-in level budget, a `$100` base active-cost cap per band, `$1,000` starting cash, 20 bps cash profit, a 0.60% manual maker-fee assumption, and overflow disabled.

The new Coinbase Exchange public fallback file covers `2025-05-01T00:00:00Z` through `2025-09-20T04:36:00Z`:

```text
raw source: C:\Users\doyle\Downloads\MoneyManData\raw\external_ohlcv\product=XRP-USD\provider=coinbase_exchange_public\granularity=60\part_20260712T215636Z.jsonl
raw source SHA-256: f9806c3dc02758009e35bbce1eb34d27c0d70e0582ba3ce39f8e65db7c93237f
derived fallback: C:\Users\doyle\Downloads\MoneyManData\derived\v1\candles_fallback\part_20260712T215636Z.jsonl
fetch report: C:\Users\doyle\Downloads\MoneyManData\catalog\quality\candle_fallback_fetch_20260712T215636Z.json
rows written: 204,755 of 204,757 expected one-minute buckets
missing buckets: 2025-08-11T20:00:00Z and 2025-08-11T20:01:00Z
request errors: 0
engine source SHA-256: f1abc1ffdb0f7b2944c7d279ff1888a3bc8aa478726c4985f25bb047142435ff
```

The duplicate count in the fetch report comes from inclusive request-window boundaries and was removed by timestamp before writing. The two truly missing August minutes remain visible as one 180-second diagnostic gap; they were not filled or hidden.

### Frozen Windows And Primary Results

The table uses the low-first `principal_recovery` run as the primary descriptive replay. `Final equity` is before the simplified modeled rebate.

| Window | First open / frozen range | Observed path | Completed / end-open | End active cost | Max drawdown | Final equity |
|---|---|---|---:|---:|---:|---:|
| May 2025 | `$2.1910`; `[1.80,2.40)` | high `$2.6562`, close `$2.1753` | 50 / 29 | `$145.00` | `$13.53` | `$1,003.24` |
| June 2025 | `$2.1752`; `[1.80,2.40)` | high `$2.3385`, low `$1.9093`, close `$2.2373` | 81 / 12 | `$60.00` | `$22.37` | `$1,014.17` |
| July 2025 | `$2.2369`; `[2.00,2.60)` | high `$3.6662`, close `$3.0224` | 46 / 0 | `$0` | `$2.86` | `$1,010.48` |
| August 2025 | `$3.0223`; `[2.80,3.40)` | high `$3.3836`, low `$2.7273`, close `$2.7757` | 95 / 59 | `$295.00` | `$32.23` | `$985.29` |

July is the user's breakout case in concrete form. The fixed grid did not chase XRP above its frozen range: every active lot recovered, settled cash reached `$1,000.46`, and 3.313848 tagged reserve XRP remained. That limited drawdown, but it also missed most of the rally; idealized fee-free buy-and-hold ended at `$1,351.16`. A fixed grid that stops above its range is protected from immediately buying the top, but it does not solve higher-band handoff or upside opportunity cost.

August approximates arming a fresh higher range after the breakout. All three bands finished almost fully loaded: `[2.80,3.00)` and `[3.00,3.20)` each held 20 open `$5` lots, while `[3.20,3.40)` held 19. The run retained `$705.95` idle cash, so it did not exhaust the wallet, but `$295` was unrecovered and open-lot unrealized PnL was `-$30.78`. This happened with overflow already disabled. Lending extra quiet-band money would increase exposure to the failure mode rather than fix it.

### Recovery And Capital Lock

| Window | 7-day recovery | 14-day recovery | 28-day recovery | Cash-cost-time |
|---|---:|---:|---:|---:|
| May | 38 / 59, 64.41% | 43 / 46, 93.48% | 9 / 9, 100% | 1,606.64 quote-days |
| June | 63 / 87, 72.41% | 36 / 47, 76.60% | 8 / 8, 100% | 2,915.52 quote-days |
| July | 45 / 46, 97.83% | 46 / 46, 100% | 17 / 17, 100% | 574.56 quote-days |
| August | 59 / 81, 72.84% | 4 / 23, 17.39% | no eligible cohort | 5,487.27 quote-days |

Across these four disjoint low-first windows, the eligible-count aggregate was 205 of 273 at 7 days and 129 of 162 at 14 days. The 28-day aggregate was 34 of 34, but it is a small, early-entry cohort and August contributed no eligible lots. These percentages use different full-follow-up cohorts at each horizon; they must not be read as a monotonic survival curve. In August, 19 lots were separately labeled data-gap unknown for the 7-day and 14-day cohorts because their horizon crossed the two-minute outage.

Capital lock distinguished the regimes more clearly than cycle count. July recovered every lot with only 574.56 quote-days of unrecovered cost-time. August completed more cycles but accumulated 5,487.27 quote-days and ended with 59 open lots. More fills were not evidence of a safer or better grid.

### Reserve Versus Full-Lot Control

The same-path `full_lot` policy preserved entries, targets, recovery times, and active cash, changing only whether completed exits retained residual XRP. Low-first pre-rebate equity differences, `principal_recovery - full_lot`, were:

| Window | Reserve equity minus full-lot equity |
|---|---:|
| May | `-$0.6721` |
| June | `+$0.3266` |
| July | `+$1.8565` |
| August | `-$1.7287` |

Reserve helped when its retained XRP appreciated after exits and hurt when it depreciated. It is continuing long exposure, not downtrend protection, free cash, or proof that a lot safely recovered.

Low-first and high-first had identical end financials in May and June. July had identical trades and final equity with about `$0.03` maximum-drawdown sensitivity. August was path-sensitive: low-first completed 95 lots at `$985.29`; high-first completed 97 at `$985.61`. One-minute candles therefore do not settle the exact fill order in a volatile range. Both paths reached the same core conclusion, and neither is L2 execution evidence.

### Verified Run Provenance

| Window | Selected-row SHA-256 | Reserve low / high | Full-lot low / high |
|---|---|---|---|
| May | `0fd0b8fc1dcf97142fc08e0054a4295e4cf606cc86bb72dbdd61f0b0ea775b8a` | `20260712T220504Z-c48f333f` / `20260712T220515Z-6db05a88` | `20260712T220526Z-b8ac2c13` / `20260712T220537Z-ba29439f` |
| June | `34326c13294194cfb07461dbf463a32012f0ca863f65b4de4324f29441b43634` | `20260712T220505Z-b71daaa3` / `20260712T220517Z-5a0425f7` | `20260712T220528Z-141ab40e` / `20260712T220539Z-e4cab24f` |
| July | `fe3f7f95ee9015278cf4a5938da8f26b6948dc18f31762240b2c9d0c4ea7ffdf` | `20260712T220503Z-97eb4cc2` / `20260712T220514Z-ffd9a9e1` | `20260712T220524Z-b5d445d7` / `20260712T220535Z-4cd19a8b` |
| August | `a1938857ea05e05bcf43fd56f82e9ad39cd3ea86d1bb5613e895116ed0b097b7` | `20260712T220506Z-7ad7de24` / `20260712T220518Z-9cdf7a62` | `20260712T220530Z-f7b4c6d8` / `20260712T220542Z-5ba53326` |

All 16 runs completed with overflow at zero. Every reported cash, base, tranche, band, fee, turnover, profit, reserve, count, and cap reconciliation error was zero.

### Decision From This Validation

The capital decision from this validation rejected quiet-band borrowing. The fixed profile had already shown the dangerous state without overflow: a new high range can fill nearly all three band caps during a decline.

The isolated strategy experiment therefore tested one price-only downtrend activation guard while leaving band budgets, exits, reserve retention, and overflow unchanged; its completed evidence follows below. A protected/flexible cross-band allocator remains a separate optional later A/B. Existing reserve must not be sold or silently converted back into cash as part of that separate test.

## Frozen v1.4 EMA Entry-Guard Experiment

This experiment was pre-registered on 2026-07-20 before any holdout path or result was inspected. It changes one decision only: whether a newly crossed empty buy slot may enter. Overflow remains zero. Lot size, per-band caps, exits, fees, reserve retention, rearming, accounting, and sell processing remain identical to the v1.3 fixed control.

The optional guard is `ema_cross`, disabled by default. Its public name is generic because the configured spans are part of the run identity; this frozen experiment uses 360 and 1,440 one-minute candles:

```text
frozen v1.4 engine source SHA-256: 1dda2f2c911eb24ebf479622bd1919e958a36ada5afa4f688243c783166a4f9f
frozen candle-loader source SHA-256: 02952863eadbc2327b52807b5820e48ff043438359cde4468894a1117845d7e8
```

```text
fast alpha = 2 / (360 + 1)
slow alpha = 2 / (1440 + 1)

allow new buys in candle t when:
EMA360 through close[t-1] >= EMA1440 through close[t-1]
```

The decision is frozen once at the start of each candle and applies to every modeled leg in that candle. The current candle's open, high, low, and close cannot change it. Existing lots may always exit. A blocked empty slot records `buy_missed`, consumes no cash or fee, and must rearm through normal price traversal before a later downward recross can fill. Guard misses are split into downtrend, warmup, and stale-signal causes and reconcile across portfolio, tranche, and band ledgers.

The guard requires 1,440 observed one-minute closes before the trading start. The runner retains exactly the final 1,440 eligible pre-roll rows. Both recursive EMAs seed to the first retained close and then use the formulas above (`adjust=False`-style initialization). Those rows are signal-only pre-roll: they cannot trade, alter balances, create lots, or appear in the trading equity curve. No missing close is imputed. A sub-24-hour gap makes only the first decision after the gap stale and fail-closed; the following observed close updates both EMAs normally. A gap of at least 1,440 minutes resets the signal and requires a new warmup. Small-gap blocks must be reported separately and cannot be credited as trend-filter success. Trading and pre-roll selected-row hashes, contributing derived-file paths, file hashes, row counts, and time ranges are recorded separately.

May through August 2025 are development windows only. Each gets an unchanged guard-off control and the guarded rule under both low-first and high-first candle paths. The fixed inputs remain `principal_recovery`, 5% exit movement, 20 bps cash profit, 20 levels per band, `$5` all-in per level, `$100` base cap per band, `$1,000` shared cash, 0.60% manual maker fee, and overflow zero. The May guard uses the fetched April 29-May 1 pre-roll file `part_20260721T024533Z.jsonl` (2,880 rows; derived-file SHA-256 `e7b75e2073b6fb3c9d697516073807476df5022e41fae9953f43b50913326190a`) and selects its final 1,440 rows.

The untouched chronological holdout is frozen as:

```text
trade window: 2026-06-09T00:00:00Z through 2026-07-09T00:00:00Z exclusive
provider: coinbase_exchange_public
derived source: part_20260709T185009Z.jsonl
derived source SHA-256: 3f592eb60d7218d11e33b0557dae190adbdcc5f7f8baa4a9541ef5dd636344e8
coverage: 43,183 of 43,200 expected one-minute candles; 17 explicit missing buckets
first open: $1.1676
frozen first-observation range: [0.80,1.40)
```

At pre-registration, no existing backtest configuration or summary referenced that window. The range used only the first open: take its globally anchored `$0.20` band and one band on each side. The remaining price path stayed unopened until the rule, code, metrics, and criteria were frozen. The guard pre-roll came from `part_20260721T024557Z.jsonl` (2,880 rows from June 7-9; derived-file SHA-256 `3f6d74096dedaac5a653c0e0b411700c90fc9ddbfefa8580044f1947c2d2e9ec`) and selected its final 1,440 rows.

The holdout passes only if both candle paths satisfy all of these conditions:

- every existing accounting, cap, and diagnostic reconciliation remains zero;
- at least five candidate buys are blocked by the downtrend state, excluding warmup and stale blocks;
- cash-cost-time is no more than 90% of its same-path control;
- either end active cost improves by at least `$25` or maximum close-sampled drawdown improves by at least 10%;
- neither end active cost nor drawdown worsens;
- marked and estimated-liquidation pre-rebate equity each trail control by no more than `$2`;
- gross fees do not increase; and
- low-first and high-first reach the same verdict.

Fewer than five genuine downtrend blocks or a control with no meaningful risk is inconclusive, not a pass. Completed lots, realized cash profit, turnover, and fees are reported as opportunity cost. The rule is rejected if its apparent benefit mainly comes from warmup or missing-data suppression. No parameter may be tuned on the holdout.

### Development Result

The unchanged 360/1,440 rule reduced capital lock and close-sampled drawdown in every May-August development window, but it also finished with lower marked equity in every window. The low-first results summarize the tradeoff; high-first reached the same qualitative result with only small path-order differences:

| Window | End active cost, control -> guard | Cash-cost-time change | Max drawdown, control -> guard | Marked equity change |
|---|---:|---:|---:|---:|
| May 2025 | `$145.00` -> `$85.00` | `-39.6%` | `$13.53` -> `$8.74` | `-$2.56` |
| June 2025 | `$60.00` -> `$60.00` | `-22.3%` | `$22.37` -> `$16.48` | `-$7.81` |
| July 2025 | `$0.00` -> `$0.00` | `-8.3%` | `$2.86` -> `$2.79` | `-$1.48` |
| August 2025 | `$295.00` -> `$230.00` | `-25.9%` | `$32.23` -> `$28.37` | `-$4.27` |

Development run IDs:

| Window | Low-first control / guard | High-first control / guard |
|---|---|---|
| May | `20260721T025802Z-cdf2990f` / `20260721T025808Z-aa16f1da` | `20260721T025759Z-610a5602` / `20260721T025808Z-88111958` |
| June | `20260721T025937Z-cd27e4bd` / `20260721T025951Z-72fa28ff` | `20260721T025901Z-6a9bc09d` / `20260721T025931Z-0d118179` |
| July | `20260721T030051Z-2aa0786c` / `20260721T030139Z-b6546171` | `20260721T030051Z-6807f649` / `20260721T030138Z-eab425ec` |
| August | `20260721T030248Z-2727295c` / `20260721T030358Z-3446d838` | `20260721T030242Z-de925e82` / `20260721T030352Z-71fd930c` |

All eight new guard-off runs reproduced their corresponding v1.3 fingerprints and financial results exactly. All development accounting, cap, entry-guard, and diagnostic reconciliations were zero. This matrix says the rule is a real risk throttle, not a free improvement: it can avoid trapped buys, but it can also skip profitable oscillations.

### Frozen Holdout Result

The frozen June 9-July 9, 2026 holdout passed every pre-registered criterion under both candle paths:

| Metric | Low-first control | Low-first guard | High-first control | High-first guard |
|---|---:|---:|---:|---:|
| Completed lots | 27 | 22 | 27 | 22 |
| End-open lots | 23 | 15 | 23 | 15 |
| End active cost | `$115.00` | `$75.00` | `$115.00` | `$75.00` |
| Cash-cost-time, quote-days | `2,629.57` | `1,744.43` | `2,630.96` | `1,747.90` |
| Max close-sampled drawdown | `$18.73` | `$12.03` | `$18.86` | `$12.16` |
| Marked pre-rebate equity | `$995.6890` | `$995.9966` | `$995.6890` | `$995.9966` |
| Estimated-liquidation pre-rebate equity | `$995.0265` | `$995.5719` | `$995.0265` | `$995.5719` |
| Gross fees | `$2.3076` | `$1.7687` | `$2.3076` | `$1.7687` |
| Genuine downtrend blocks | 0 | 2,024 | 0 | 2,059 |

Holdout run IDs:

- low-first control / guard: `20260721T030525Z-6f30197e` / `20260721T030544Z-b7ac37fb`;
- high-first control / guard: `20260721T030525Z-d2973bbb` / `20260721T030547Z-9041b87b`.

All four holdout summaries report trading selected-row SHA-256 `d163429631a2581296b4affa894b09527b5aebd48ee215f740d795c092e41aee`. Both guard summaries report signal-pre-roll selected-row SHA-256 `d4d850db89af38c3c8c7ea9dc8946a4b94d582d144bf8a7dff4864a39f90e1e9`.

The guard cut cash-cost-time by `33.66%` low-first and `33.56%` high-first, cut drawdown by `35.77%` and `35.53%`, improved end active cost by `$40`, slightly improved both equity measures, and reduced fees. Every reconciliation was zero. The 17 missing minute buckets created 10 stale candle decisions; only 3 low-first and 2 high-first slot crossings were blocked for staleness, versus more than 2,000 genuine downtrend blocks. The pass therefore did not mainly come from suppressing trades around missing data.

This is one strong price-only holdout pass, not deployment evidence. Every variant still finished below the original `$1,000` cash before rebate, so the pass is relative to the same-engine control and does not establish profitability. Recovery-cohort percentages are weak in this window because gaps leave only three or four eligible 7-day lots and no eligible 14-day or 28-day cohorts; cash-cost-time, drawdown, and accounting are the reliable holdout measures. Because development equity was consistently lower and candles cannot model spread, depth, latency, queue position, or partial fills, `entry_guard=none` remains the default. Do not tune the spans on this holdout or treat the result as permission to enable overflow or cross-band borrowing.

## Legacy Gridbot Reference

The unchanged original gridbot was also run over the same candle window with twelve `$0.05` grid intervals and `$25` quote orders. It finished at `$1,000.1364` after adding a `$1.50` modeled rebate. Its before-rebate equity was about `$998.6364`.

This is only a historical compatibility reference, not a causal comparison. The legacy engine uses different initialization and candle traversal, adjacent-level exits, `$25` orders, pooled XRP, and different accounting. No performance difference between it and the reserve engine should be attributed to reserve handling. The verified historical causal reserve-versus-no-reserve control used v1.2 `principal_recovery` versus v1.2 `full_lot` with every other setting and path held fixed; v1.3 preserved those decisions while adding post-trade diagnostics, and v1.4 still preserves them exactly when the optional entry guard is off. The legacy engine cannot report completed lots, end-open duration, reserve provenance, or principal recovery. Its leftover XRP is an incidental pooled residual rather than the protected reserve ledger described above.

## What Comes Next

Keep overflow and the EMA guard disabled by default. Do not proceed from either result to borrowing the quiet bands' base allocations: the safer surplus-only overflow test increased trapped exposure and losses, while the EMA guard has only one untouched holdout pass and mixed development equity.

Minimal valid-window L2 reconstruction is complete on two independent frozen XRP windows. The conservative pooled-grid strict fill model is complete across both comparisons plus the clock A/B and single 500 ms stress; see `docs/L2_BOOK_RECONSTRUCTION.md` and `docs/L2_GRIDBOT_FILL_MODEL.md`. None of that work edited this reserve engine or changed reserve, overflow, EMA, sizing, exits, allocation, compounding, or handoffs. Another audited-window fill replication and another untouched chronological EMA validation remain separate optional validations before broader claims; neither is the immediate data-pipeline milestone. Do not reuse the June-July 2026 holdout for tuning.

The protected/flexible allocator remains a later, separate A/B. It must not borrow from lower protected bands during a decline, and it must not be combined with the entry guard in its first test.

Reserve release remains its own later experiment. Do not combine it with overflow, compounding, band handoffs, L2 signals, or optimization in one test.

## Reproduce The 5% Overflow Reserve Run

```powershell
python -m moneyman gridbot-reserve-backtest `
  --product XRP-USD `
  --lower 2.60 --upper 3.20 --band-width 0.20 `
  --levels-per-band 20 --band-active-lot-budget-cap 100 `
  --overflow-global-active-lot-budget-cap 100 `
  --quote-start 1000 --exit-move-pct 0.05 --cash-profit-bps 20 `
  --exit-policy principal_recovery `
  --base-increment 0.000001 --quote-increment 0.01 `
  --price-increment 0.0001 --min-quote-notional 1 `
  --fee-source manual --fee-rate 0.006 --liquidity-assumption maker `
  --coinbase-one-advanced-rebate-rate 0.25 `
  --coinbase-one-monthly-rebate-cap 100 `
  --include-fallback-candles --candle-path-assumption low-first `
  --start "2025-09-20T04:37:00Z" --end "2025-10-09T19:00:00Z" `
  --provider coinbase_exchange_public `
  --derived-root "C:\Users\doyle\Downloads\MoneyManData\derived" `
  --catalog-root "C:\Users\doyle\Downloads\MoneyManData\catalog"
```

Use `--overflow-global-active-lot-budget-cap 0` for the fixed control.

Generated backtest folders and catalog reports are derived artifacts and stay out of Git.
