# Strict L2 Gridbot Fill Model

Last updated: 2026-07-22

## Frozen V1 Hypothesis

This document froze MoneyMan's first strict-L2 execution hypothesis before implementation and now records two validated V1 comparisons. The experiment changes only how the existing arithmetic pooled gridbot decides whether an active order filled. Grid construction, anchor selection, starting balances, quote size per order, adjacent-level rearming, fees, and pooled inventory remain the same as the price-only engine. The reserve-grid engine, overflow, EMA guard, allocation, exits, compounding, band handoffs, paper execution, and live execution are out of scope.

The hypothesis is deliberately conservative: an audited L2 book can disprove a candle touch, bound executable visible quantity, and distinguish a resting order from an order that crosses on arrival. It cannot reveal exact queue position, cancellations versus trades, hidden liquidity, or L3 order lifecycles.

## Eligible Input Only

- Rediscover with `discover_audited_book_runs` inside the consumer before reading any book artifact. A caller-supplied earlier discovery report is never selection authority.
- Consume exactly one run and one `strict_l2_eligible` window. Never join windows across a gap or reconnect.
- Resolve `book_snapshots.jsonl` and `book_windows.jsonl` relative to the audited manifest.
- Bind the exact manifest bytes across fresh pre-consumption and post-consumption audits, then recheck the consumed artifact hashes. Reject ambiguous run/window selection, contract corruption, product mismatch, invalid rows, clock gaps, or a window boundary.
- The first frozen XRP run has no receive timestamps and therefore uses `message_ts`. The independent second run has complete, unique, monotonic `message_ts` and `recv_ts`. Every run selects one clock explicitly and never mixes timestamp fields.
- Use only emitted top-10 `bid_levels` and `ask_levels`. Aggregate full-book depth is not executable price-level evidence.

## Unchanged Grid Decisions

- Build the same inclusive arithmetic levels as the price-only engine.
- Strict mode chooses the nearest level to its first book midpoint. Price-only mode retains its existing nearest-level rule using the first candle open. The frozen comparison is valid only because both observations select the same grid index; neither data mode borrows the other's future or coarser observation.
- Initialize buys below the anchor and sells above it.
- A full buy arms only the adjacent sell at `level_index + 1`.
- A full sell arms only the adjacent buy at `level_index - 1`.
- Buy target base remains `order_quote / limit_price`; sell target base remains `order_quote / limit_price`.
- A partial fill never resizes or arms an adjacent order. Its uncertain remainder is canceled.

## Order State Machine

```text
pending_latency
    -> resting
    -> filled
    -> partial_canceled
    -> missed_insufficient_quote
    -> missed_insufficient_base
    -> canceled_no_visible_depth
    -> canceled_window_end
```

Every initial or rearmed order has a decision timestamp. It becomes eligible on the first later audited book row whose configured clock is at least `decision_ts + latency`. The frozen V1 comparison uses a deterministic 100 ms latency. A rearmed order is not allowed to cascade through the same book row that filled its parent. Output keeps configured latency, actual decision-to-observed-arrival time, resting time, and total decision-to-fill time separate.

A previously resting cohort is evaluated against the event represented by a row before orders whose latency first expires on that row arrive against the remaining shadow depth. Within either cohort, higher buys and lower sells receive deterministic price priority. This avoids inventing sub-row priority for a newly arriving order.

### Arrival Crossing: Taker

On arrival, a buy is marketable only when a visible ask is at or below its limit. A sell is marketable only when a visible bid is at or above its limit. A marketable order is a taker and consumes visible contra levels in price priority up to its limit. Its execution notional uses the consumed visible prices and the taker fee rate.

If the market moved through the level during latency and moved away before arrival, V1 records no retroactive fill.

### Resting Order: Maker With Strict Price-Through

An order that does not cross on arrival becomes resting. It receives no fill from:

- price merely approaching the limit;
- best contra price merely touching the limit;
- same-level displayed quantity decreasing or disappearing; or
- repeated unchanged book rows.

A resting buy becomes fill-eligible only when `best_ask < buy_limit`. A resting sell becomes fill-eligible only when `best_bid > sell_limit`. This strict price-through rule is the conservative queue assumption: a touch cannot prove that the hypothetical order reached the front of an unknowable queue. A maker fill executes at its limit and pays the maker fee rate.

## Visible Depth And Partial Fills

- The first observation of each emitted book-side/price starts a persistent shadow quantity equal to its visible quantity.
- A later positive absolute-quantity delta is the only evidence that replenishes shadow depth. A decrease removes shadow depth and floors at zero; an unchanged row restores nothing.
- Leaving the emitted top 10 and later re-entering does not reset a price's prior observation or simulated consumption. Only a later positive observed delta adds quantity.
- All orders evaluated on one row share the same persistent shadow ledger, and same-cohort orders use deterministic price priority: higher buys first and lower sells first.
- A visible quantity can therefore be consumed only once unless a later observed positive delta exposes additional quantity.
- Never infer more quantity from `full_bid_depth`, `full_ask_depth`, hidden liquidity, or levels beyond the emitted top 10.
- When eligible visible quantity is smaller than the target, V1 executes one partial fill and cancels the remainder immediately. It does not reuse unchanged depth on later rows.

## Inventory, Cash, Fees, And Conservation

Before any execution, a buy must be able to fund the full original order at its limit plus the applicable gross fee. A sell must own the full original target base quantity. Failure records the same insufficient-quote or insufficient-base outcome as the price-only engine and makes no balance change.

V1 deliberately preserves the existing pooled grid-intent abstraction: funding is checked at execution rather than reserved when an intent is created. An initially unbacked sell intent may therefore be observed as resting and can execute only if the shared portfolio fully owns it by the later fill row. This is not a claim that the exchange continuously accepted that order, and it is one reason V1 remains a research comparison rather than paper/live execution. Changing reservation or allocation belongs in a separate experiment.

For executed quantity:

```text
buy:  quote -= execution_notional + gross_fee
      base  += filled_base

sell: quote += execution_notional - gross_fee
      base  -= filled_base
```

Coinbase One rebates remain a separate nonspendable receivable. Every run must reconcile exactly:

```text
end_base = start_base + bought_base - sold_base
end_quote = start_quote - buy_notional - buy_gross_fees
                        + sell_notional - sell_gross_fees
turnover = buy_notional + sell_notional
```

Negative quote or base balances are a hard failure, not a reportable backtest result.

## Window End

Pending and resting orders are canceled at the selected window end. No order, visible depth, inventory decision, or latency timer crosses into another validity window. Balances resulting from fills remain the final research portfolio state.

## Hand-Checked Falsification Cases

V1 is not accepted unless focused fixtures prove all of these:

1. A nearby price, exact touch, and same-level depth reduction produce zero maker fill.
2. Strict price-through produces a maker fill and maker fee.
3. Crossing on arrival sweeps visible prices and pays the taker fee.
4. Two same-row or staggered orders cannot reuse unchanged visible quantity.
5. Positive visible-quantity deltas replenish only the increment; decreases, top-N disappearance/re-entry, both book sides, and independent prices preserve shadow accounting.
6. Insufficient visible depth produces one partial fill and a canceled remainder.
7. Resting orders precede newly arriving orders on the same row, with price priority inside each cohort.
8. Latency can worsen, miss, or change maker/taker execution without looking ahead, and arrival latency is not mislabeled as time to fill.
9. Quote including gross fee and base inventory can never be exceeded.
10. A full buy/sell cycle reconciles base, quote, fees, rebates, and turnover exactly.
11. Corrupted contracts, stale discovery, ambiguous selections, invalid windows, and boundary changes fail closed.
12. The summary fingerprints the exact fill-engine source, gridbot source, saved config, fill-contract config, audited manifest/artifacts, selected ordered rows, and emitted output artifacts.

## Frozen First Comparison

Use the wholly overlapping minute-aligned interval:

```text
audited run: 20260721T235740933455Z-0760dcde
window: window-000001
time: [2025-08-01T21:22:00Z, 2025-08-01T21:41:00Z)
product: XRP-USD
grid: 2.995 through 3.016, 21 intervals, 0.001 spacing
order quote: $25
strict latency: 100 ms on message_ts
fallback provider: coinbase_exchange_public
```

The strict slice and fallback candles use the same product, wall-clock bounds, grid, balances, order size, and fee profile. Only the data and fill mode differ. One bounded comparison can validate mechanics and expose false price touches; it cannot establish profitability.

## Validated First Result

The strict run consumed 18,067 audited rows from sequence 827 through 25,627. Every selected row came from `window-000001`, used emitted depth 10, and was marked depth-truncated. The consumer audit found the hardened run valid with no errors and excluded the older draft run for an engine-source mismatch.

Verified run IDs:

- strict L2, 100 ms: `20260722T024413Z`;
- price-only low-first: `20260722T024444Z`;
- price-only high-first: `20260722T024501Z`.

All three used `$1,000` quote, zero starting XRP, `$25` order quote, a manual 0.60% maker/taker fee, and the same 25% simplified rebate assumption.

The strict first midpoint was `3.00925`; both candle controls opened at `3.0093`. All three independently selected arithmetic grid index 14, so the comparison did not drift at initialization. The two candle controls selected the same 19 ordered rows, identified by `bfda1eb667576e5ec83f1e85260dbb95da8f95c302cff73143bf1512dfcae423`.

| Mode | Full fills, buy / sell | Turnover | Gross fees | End quote / XRP | Final marked equity after modeled rebate |
|---|---:|---:|---:|---:|---:|
| Strict L2, 100 ms | 68, 39 / 29 | `$1,700` | `$10.20` | `$739.80` / `83.316582` | `$992.1998` |
| Price-only low-first | 72, 36 / 36 | `$1,800` | `$10.80` | `$989.20` / `0.099668` | `$992.1989` |
| Price-only high-first | 70, 41 / 29 | `$1,750` | `$10.50` | `$689.50` / `99.997017` | `$992.0161` |

The strict replay produced only maker fills in this slice: no order crossed the visible spread when its 100 ms latency expired. All 68 strict fills had enough emitted top-10 shadow quantity, so the real slice produced no partial fill. The fixture suite separately proves taker sweeping, persistent no-reuse depth accounting, delta-only replenishment, one-shot partial fill plus remainder cancellation, cohort priority, and latency-driven better, worse, and missed outcomes.

The strict result rejected four candle-mode full touches versus low-first and two versus high-first. More importantly, the three modes ended with materially different cash/XRP inventory even though their final marked equity happened to be close. That is the execution lesson from this window; the sub-cent equity ordering is not meaningful evidence of an advantage because strict mode marks at the final book midpoint while candle mode marks at the final candle close.

Strict quote, base, gross-fee, and turnover reconciliation errors were exactly zero. Five strict sell attempts were rejected for insufficient base, no balance became negative, no fill crossed a window boundary, and pending/resting orders were canceled at the selected slice end. Every mode remained below the `$1,000` no-trade cash baseline. This is mechanics validation, not profitability evidence.

The official strict artifact records selected-row hash `9f879c33841b70a1c29293334dcf2d54de09d4cc6d0b13114e3e00db2b6311d2`, fill-engine source hash `cbc47674a025b0a8903dad902954558982712cbf7362ba28cf432e899c018e98`, and shared gridbot source hash `7665acfe267400369e8ca64eedf20d5dd12aa72b9b4e1b28eb0ef50cde1d0c84`. All three final summaries record config, input-row, and output-artifact fingerprints; their saved catalog copies match the run summaries exactly.

## Pre-registered Second Validation

The second validation was frozen before reconstruction or replay. It uses a different legacy logger session and no raw file, book row, or candle from the first comparison:

```text
capture stream: legacy-xrp-ws-2025-08-02-session-2
replay file 1: xrp_ws_data/2/xrp_2025-08-02_15-58.jsonl
replay file 2: xrp_ws_data/2/xrp_2025-08-02_16-08.jsonl
right-boundary sentinel: xrp_ws_data/2/xrp_2025-08-02_16-18.jsonl
expected replay sequence: 0 through 35433
expected sentinel first sequence: 35434
full-book checkpoints: 17890, 17891, 35432
shared comparison interval: [2025-08-02T15:59:00Z, 2025-08-02T16:18:00Z)
```

The grid is translated from the first strict row only; it is not selected from later highs, lows, fills, or PnL. Preserve the first comparison's geometry and initial order distribution:

```text
tick: 0.001
grid intervals: 21
anchor index: 14
lower = floor(first strict midpoint / tick) * tick - 14 * tick
upper = lower + 21 * tick
quote start: $1,000
base start: 0 XRP
order quote: $25
manual maker/taker fee: 0.60%
modeled fee rebate: 25%
strict latency: 100 ms on message_ts
```

The candle controls must use those exact resulting bounds and all other core settings. Their first open must independently choose index 14; if it does not, report the initialization mismatch rather than moving the interval or grid. The reconstruction must produce exactly one freshly audited eligible window with stable raw hashes and a contiguous right boundary. Strict quote, base, fee, and turnover reconciliation must remain exactly zero, all balances must remain nonnegative, and every declared input/output fingerprint must verify. There is no fill-count, PnL, maker/taker, or partial-fill pass target: this is an untouched mechanics validation, not optimization.

## Validated Second Result

The frozen reconstruction completed as `20260722T032803947186Z-9593d3b9/window-000001`. It replayed sequences 0 through 35433 from the independent session-2 capture, verified the next roll at sequence 35434, emitted 22,778 valid states, and passed a fresh consumer audit with no errors. The shared strict slice contained 21,613 rows from sequence 1030 through 34622.

The first strict midpoint was `2.83015`, so the pre-registered rule produced the grid `2.816` through `2.837`. The first Coinbase-public candle opened at `2.8301`. Strict and both candle modes independently selected grid index 14. Both controls used the same 19 ordered candle rows, identified by `62244bdbae4a3860ffc9d8551d79fb0740598f9872c4d94b7b3fb163458f1b19`.

Verified run IDs:

- strict L2, 100 ms: `20260722T033005Z`;
- price-only low-first: `20260722T033049Z`;
- price-only high-first: `20260722T033102Z`.

| Mode | Full fills, buy / sell | Turnover | Gross fees | End quote / XRP | Final marked equity after modeled rebate |
|---|---:|---:|---:|---:|---:|
| Strict L2, 100 ms | 45, 23 / 22 | `$1,125` | `$6.75` | `$968.25` / `8.905831` | `$995.1325` |
| Price-only low-first | 22, 11 / 11 | `$550` | `$3.30` | `$996.70` / `0.034395` | `$997.6223` |
| Price-only high-first | 28, 17 / 11 | `$700` | `$4.20` | `$845.80` / `53.103568` | `$997.0959` |

Every one of the 45 strict fills matched its exact saved book row, executed at its maker limit only after strict price-through, and consumed no more than the displayed opposite-side quantity. An independent observed-delta shadow replay reproduced every consumed-depth list with no negative availability; the tightest eligible shadow quantity was still 1.413 times the order target. All 66 arrivals first rested because none crossed the visible spread. Observed arrival latency ranged from `100.082609` to `363.689882` ms because activation waits for the first eligible book row after the configured 100 ms threshold. The real slice therefore produced 45 makers, zero takers, zero partials, and zero no-visible-depth cancellations. Taker sweeping and partial-remainder cancellation remained fixture-only in the two 100 ms comparison slices; the later 500 ms stress exercised both paths on recorded rows.

The first order is a concrete lifecycle check. A buy at `2.829` was decided at `15:59:00.048320857Z`, became eligible 100 ms later, and arrived at sequence 1033 after `103.143553` ms with ask `2.8294`, so it rested. It filled at sequence 1054 only when ask `2.8286` was strictly below the limit, consuming `8.8370448922` XRP from 540 XRP displayed at that price. Seven sell attempts were rejected because the full original quantity was not owned, fourteen open orders were canceled at the selected slice end, and no quote, base, or rebate balance became negative.

Replaying the strict ledger independently recovered `$575` of buys, `$550` of sells, `203.3771167857` XRP bought, `194.4712855872` XRP sold, `$6.75` of gross fees, `$1.6875` of modeled rebate, `$968.25` ending quote, `8.9058311985` ending XRP, and `$1,125` turnover. Saved quote, base, fee, and turnover reconciliation errors were all exactly zero. Row-by-row replays of both candle-control ledgers also matched every saved quote, base, and rebate balance, with no negative balance.

Unlike the first window, strict L2 produced more fills than either candle path. The two results together show that price-only touches are neither an upper nor a lower execution bound: candle paths can invent touches, but one low/high traversal per minute can also omit repeated event-level crossings. The modes also ended with materially different inventory. Their marked-equity ordering is not execution-alpha evidence, and every mode remained below the `$1,000` no-trade cash baseline.

The strict artifact records selected-row hash `1e10c9906e973d79089e1491d0806e1183af6a3912cf06805e2a080773a36da9`, unchanged fill-engine source hash `cbc47674a025b0a8903dad902954558982712cbf7362ba28cf432e899c018e98`, unchanged gridbot source hash `7665acfe267400369e8ca64eedf20d5dd12aa72b9b4e1b28eb0ef50cde1d0c84`, and unchanged fill-contract hash `0d354601f902805527d2540d824730e2d71cc5fe42c45d52fac7a346184b40b6`. Every declared output hash matched its file, each catalog copy matched its run summary byte for byte, and no strategy or live-trading behavior changed.

## Frozen Clock Sensitivity Protocol (Completed)

The clock-only execution experiment was frozen after the second comparison and before replay. It used the entire eligible `20260722T032803947186Z-9593d3b9/window-000001`, with no `--start` or `--end` filter. All 22,778 states have complete, unique, monotonic `recv_ts`; both clock loaders return the exact same ordered states, sequence 6 through the final L2 state at 35432, and selected-row hash `86ae456663f2583f3dfb82b406ecde549c5a013c58b83a24f1cbbee90d3262f8`.

The paired 100 ms A/B kept grid `2.816` through `2.837`, 21 intervals, `$1,000` quote, zero XRP, `$25` order quote, manual 0.60% maker/taker fees, and the 25% modeled rebate. The full-window first midpoint is `2.82985` and independently selects index 14. It changed only `l2_clock_source` from `message_ts` to `recv_ts`; it did not mix clocks, add a latency sweep, or change queue, depth, partial, sizing, reserve, overflow, EMA, allocation, exits, compounding, handoffs, or live behavior.

The full-window `message_ts` run was the exact-row control. The completed minute-aligned message run and candle paths remain historical context, not direct clock controls. The comparison covered activation rows, maker/taker/full/partial/missed counts, visible-depth use, turnover, fees, quote/base inventory, and exact conservation. There was no performance or fill-count pass target; this tested transport-clock sensitivity only.

## Validated Clock Sensitivity

The paired full-window runs completed without changing source code or any non-clock setting:

- `message_ts`: `20260722T120726Z`;
- `recv_ts`: `20260722T120807Z`.

Both consumed all 22,778 audited depth-10 states from `20260722T032803947186Z-9593d3b9/window-000001`, sequences 6 through 35432, with selected-row hash `86ae456663f2583f3dfb82b406ecde549c5a013c58b83a24f1cbbee90d3262f8`. Their saved configs differ at exactly one field: `l2_clock_source`. Grid, balances, sizing, 100 ms latency, fee profile, queue, depth, partial policy, inventory, and every strategy rule are identical.

| Clock | Full makers, buy / sell | Taker / partial | Turnover | Gross fees | End quote / XRP | Final equity after modeled rebate |
|---|---:|---:|---:|---:|---:|---:|
| `message_ts` | 52, 27 / 25 | 0 / 0 | `$1,300` | `$7.80` | `$942.20` / `17.755369` | `$994.3684` |
| `recv_ts` | 52, 27 / 25 | 0 / 0 | `$1,300` | `$7.80` | `$942.20` / `17.755369` | `$994.3684` |

Clock choice changed timing evidence without changing an execution. Thirty-eight of 73 orders activated on different book rows; 35 activated on the same row. `recv_ts` minus `message_ts` activation sequence shifts ranged from -2 to +6. For example, `order-000001` activated at sequence 15 after `108.181374` ms under `message_ts` and sequence 21 after `130.142` ms under `recv_ts`; both were nonmarketable and filled as makers at sequence 30. In the opposite direction, `order-000046` activated at sequence 4542 under `message_ts` and 4540 under `recv_ts`, then filled at the same sequence 32100.

All 52 fills matched exactly on order ID, execution sequence, side, grid level, limit and book prices, quantity, visible-depth consumption, maker/full classification, fees, and post-fill balances. The 22,778 equity rows matched on every non-time field. Both lifecycle ledgers contained 73 submissions, 73 resting arrivals, 52 fills, seven insufficient-base misses, and fourteen window-end cancellations. An independent persistent-shadow replay found zero negative depth, zero strict-price-through violations, the same 52 one-level consumptions, and the same 81 exact-touch rows ignored by both clocks.

Every observed arrival remained at or beyond the configured threshold. Actual arrival latency ranged from `100.076845` to `363.689882` ms under `message_ts` and `100.055` to `359.877` ms under `recv_ts`. The `recv_ts` capture offset relative to exchange message time is timestamp provenance, not an added simulated order latency.

Both ledgers reconciled `$675` of buys, `$625` of sells, `$1,300` turnover, `$7.80` gross fees, `$1.95` modeled rebate, `$942.20` ending quote, and `17.7553688219` ending XRP. Quote, base, fee, and turnover reconciliation errors were all exactly zero. Every declared artifact hash and row count verified, both catalog copies matched their summaries byte for byte, and source hashes remained unchanged. Output artifacts are intentionally not byte-identical because clock labels, timestamps, latency fields, config hashes, and artifact hashes differ.

This is one transport-clock robustness result: receive-clock jitter moved activation rows but never far enough to change a later strict-price-through fill. It does not prove clocks are interchangeable on other windows, does not validate a latency setting, and is not profitability evidence. Both variants remained below the `$1,000` no-trade cash baseline.

## Frozen 500 ms Receive-Clock Latency Protocol (Completed)

Before replay, the protocol froze exactly one `500 ms` `recv_ts` stress against 100 ms run `20260722T120807Z`. The selection rule was data-independent: multiply the existing 100 ms research control by five to create one severe round-number subsecond stress. It was not latency calibration, and the value did not change after replay. No other slower latency belonged to this experiment.

It reused the entire `20260722T032803947186Z-9593d3b9/window-000001`, with no time bounds or row cap: 22,778 ordered states, sequences 6 through 35432, selected-row hash `86ae456663f2583f3dfb82b406ecde549c5a013c58b83a24f1cbbee90d3262f8`. It preserved `recv_ts`, grid `2.816` through `2.837`, 21 intervals, `$1,000` quote, zero XRP, `$25` orders, manual 0.60% maker/taker fees, 25% modeled rebate, strict price-through queue policy, persistent observed-delta shadow depth, partial-remainder cancellation, inventory rules, and every strategy setting. The saved stress and control configs differ only at `l2_latency_ms`, from 100 to 500.

There was no fill-count, turnover, PnL, equity, maker/taker, or partial-fill pass target. The mechanics-only acceptance rule required a fresh valid reconstruction audit, exact row/hash parity, first-eligible-row activation at or after 500 ms, valid spread and maker/taker classification, strict price-through for resting fills, no reused or negative visible shadow depth, correct partial cancellation and funding/inventory enforcement, verified artifact hashes and catalog copy, nonnegative balances, and exactly zero quote, base, fee, and turnover reconciliation errors. Observed execution differences had to be reported rather than tuned away. Clock, grid, queue, depth, partial policy, fees, sizing, inventory, reserve, overflow, EMA, allocation, exits, compounding, handoffs, and live behavior remained unchanged.

## Validated 500 ms Receive-Clock Latency Stress

The sole pre-registered slower replay completed as `20260722T213307Z`. Commit `f0d5594` froze 500 ms and the mechanics-only rule before the run existed; the saved control and stress configs differ only at `l2_latency_ms`, from 100 to 500. Both freshly audited the same 22,778 depth-10 rows, sequences 6 through 35432, and selected-row hash `86ae456663f2583f3dfb82b406ecde549c5a013c58b83a24f1cbbee90d3262f8`. No Python or strategy source changed, and this is the only preserved strict replay above 100 ms.

| `recv_ts` latency | Submitted | Full orders, buy / sell | Execution rows, maker / taker | Partial | Turnover | Gross fees | End quote / XRP | Final equity after rebate | Max drawdown |
|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 100 ms control | 73 | 52, 27 / 25 | 52, 52 / 0 | 0 | `$1,300` | `$7.80` | `$942.20` / `17.755369` | `$994.3684` | `$5.6538` |
| 500 ms stress | 60 | 39, 20 / 19 | 40, 39 / 1 | 1 buy | `$980.6686` | `$5.8840` | `$963.4474` / `10.904591` | `$995.7604` | `$4.2533` |

All 60 stress orders activated on the first eligible `recv_ts` row at or after 500 ms; observed arrival latency was `500.044` to `684.277` ms, with median `534.198` ms. Across 59 causal intents shared with the control, activation moved forward by 5 through 16 sequence rows. This reduced full fills by thirteen and total execution rows by twelve, but those changes are descriptive rather than pass criteria.

The first order supplies the real-window taker check. `order-000001`, a buy at limit `2.829`, was decided at sequence 6 and became eligible at `15:58:28.414682Z`. Sequence 30 at `15:58:28.397935Z` was still too early; sequence 32 at `15:58:28.448880Z` was the first eligible row. Its visible ask `2.8286` crossed the limit, so the order consumed exactly `8.837044892188052315305761753` XRP there as a taker, for notional `$24.99646518204312477907387769` and gross fee `$0.1499787910922587486744432661`.

The real-window partial is equally bounded. Rearmed `order-000023`, a buy at `2.829` targeting `8.837044892188052315305761753` XRP, first arrived nonmarketable at sequence 84 after `504.506` ms and rested. Ask `2.829` at sequence 380 was an exact touch and correctly did not fill. At sequence 382, only `2.005003` XRP at ask `2.8289` was strictly through; the next visible ask equaled the limit and remained ineligible. The model filled only `2.005003`, canceled the `6.832041892188052315305761753` remainder, and armed no adjacent order.

An independent persistent-shadow replay reproduced all 40 consumed-depth lists across all 22,778 rows. It tracked 430 side/price keys, found zero negative or reused quantity, allowed repeated consumption at seven prices only after positive observed replenishment, verified all 39 makers strictly through their limits, and counted 50 exact-touch observations ignored. Sequence 32 exercised price priority across the simultaneous arrival cohort. No real row combined a marketable resting cohort with newly due arrivals, so resting-before-arrival cohort ordering remains fixture evidence.

The independent ledger recovered `178.8741099481816606622943451` XRP bought for `$505.6686186690431247790738777`, `167.9695188027008363586054332` XRP sold for `$475`, `$5.884011712014258748674443266` gross fees, `$1.471002928003564687168610816` modeled rebate, `$963.447369618942616472251679` ending quote, and `10.9045911454808243036889119` ending XRP. Quote, base, fee, and turnover reconciliation errors were exactly zero; balances never went negative. All artifact hashes and row counts verified, and the catalog report was byte-identical to the run summary.

The unchanged gridbot and fill-engine source hashes are `7665acfe267400369e8ca64eedf20d5dd12aa72b9b4e1b28eb0ef50cde1d0c84` and `cbc47674a025b0a8903dad902954558982712cbf7362ba28cf432e899c018e98`. The stress canonical-config hash is `7f40d2e2b0b450af8d3ac97c8c09463be7d094a82fecf05ebdf28e9d09916a09`, and its fill-contract hash is `e44598b0570a2a8c4221120b9c89722f597b71b9c182c2c39555e5573a55950b`. Saved fills/events/equity hashes are `ad266ab405e8fde1bdaa6e665fdb1d6af9ec31c36b60ecd98eb2fce4a81e6edd`, `671c98762ccaf2810b13afb0bc8a18e17c729375c4ac48d9d38878cd6fd7ac06`, and `52ba6368a3ec0b69800167df7375ee909c1962e6df7e2c4b0fcb08e5b39f206d`; the byte-identical summary/catalog hash is `92256412adc02fd1f25b5531c0773e6ecb324607c8a2e37be7a43b5962c40c2c`.

The post-stress repository suite passed all 116 tests unchanged, including the hand-checked spread-crossing, maker/taker, shared-depth, partial, latency, strict-queue, inventory, fee, and conservation fixtures.

The stress finished closer to cash than the control, but both remained below the `$1,000` no-trade baseline. One slower point cannot show that latency is beneficial, calibrate real execution delay, or establish profitability. It shows that latency can change activation, maker/taker status, visible-depth availability, partial fills, inventory, fees, and the entire downstream grid path while the conservative accounting contract still holds.

## Future Optional Replication Constraint

Strict-execution V1 mechanics validation is closed for the two frozen windows. Do not add another latency to the passed window. If a later research question needs cross-session generalization, pre-register an independent untouched-window replication of the already frozen 100 ms and 500 ms points, changing the data window rather than tuning latency or strategy. That replication is optional validation, not the project's immediate prerequisite. If no new window qualifies, stop at the data boundary. Keep queue, depth, partial policy, grid, fees, sizing, reserve, overflow, EMA, allocation, exits, compounding, handoffs, and live behavior unchanged.
