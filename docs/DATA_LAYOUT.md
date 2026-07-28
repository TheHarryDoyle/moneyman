# MoneyMan Data Layout

MoneyMan should treat raw market data as immutable and derived data as rebuildable.

## Root Folders

Recommended environment variables:

```text
MONEYMAN_DATA_ROOT      top-level local or external data folder
MONEYMAN_RAW_ROOT       immutable raw captures
MONEYMAN_DERIVED_ROOT   normalized tables, features, and backtests
MONEYMAN_CATALOG_ROOT   inventory manifests and data-quality reports
MONEYMAN_QUARANTINE_ROOT malformed or unknown records
```

Example:

```text
D:\MoneyManData\
|-- raw\
|-- derived\
|-- catalog\
|-- quarantine\
`-- reports\
```

On this Windows machine, a practical local root is:

```text
C:\Users\doyle\Downloads\MoneyManData\
```

The intended local structure is:

```text
MoneyManData\
|-- raw\          immutable source captures, including legacy_ws_data and new logger sessions
|-- catalog\      inventory, coverage, gap, and quality reports
|-- derived\      normalized research tables and features rebuilt from raw data
|-- quarantine\   malformed or unknown records kept for review
`-- reports\      human-facing summaries when needed
```

The repository itself may also contain ignored folders such as `data\`, `catalog\`, or `tmp\` from tests or accidental local runs. Those are not the preferred long-term data root. New logger sessions should go under `C:\Users\doyle\Downloads\MoneyManData\raw`.

## Human Map Of The Folders

Think of `MoneyManData` as the local filing cabinet and the Git repo as the code/manual. The big market files belong in the filing cabinet, not in Git.

`raw\` is the source footage. It contains the exact WebSocket messages that Coinbase sent, plus tiny session metadata files. A raw `.jsonl` file is plain text with one JSON message per line. A raw `.jsonl.gz` file is the same thing compressed with gzip. These files may include level 2 book updates, market trades, ticker/candle messages, heartbeats, status, subscriptions, receive timestamps, and source timestamps. Raw is what we keep even when every derived file is rebuilt.

`raw\legacy_ws_data\` is the moved old logger archive. Inside it are old product folders such as `btc-usd_ws_data`, then session folders such as `11`, then files such as `btc_usd_2025-08-14_02-42.jsonl.gz`. Some old folders also contain `.parquet`, `.feather`, `.csv`, or old script leftovers. Those derived files are not the source of truth; they are old convenience outputs.

`raw\coinbase_advanced_trade\` is where the current logger writes new sessions. Each run exclusively creates a collision-resistant `session=<SESSION_ID>` folder confined beneath the resolved raw root. Product and channel route names are strict rather than lossy path substitutions; invalid network routes are retained in an explicit review route instead of escaping or silently colliding. Product messages go into `product=<PRODUCT-ID>` folders. Non-product messages such as heartbeats, status, and subscriptions go into `channel=<CHANNEL>` folders. `manifest.json` binds the effective config, an execution-source bundle including project configuration, host/runtime including the installed `websockets` version, connection epochs, sequence/heartbeat/latency findings, routing counts, and immutable evidence for every closed raw file. `audit-collector-session` independently derives expected destinations, replays the files, and rejects malformed manifest counts or paths instead of trusting writer metadata or summary fields.

`raw\external_ohlcv\` is the source shelf for outside candle files. This includes old CSVs moved out of Downloads and Coinbase Exchange public candle downloads. These files are raw external price data, not captured L2 order-book data. They are kept so fallback candle tables can be rebuilt and audited by provider, product, and granularity.

`catalog\` is the index and inspection shelf. Inventory reports, storage audits, coverage audits, gap reports, quality reports, relocation manifests, and cleanup plans live here. These files answer questions like "what files exist?", "what time range do they cover?", "which windows are missing?", and "what did we move?" Catalog files should be small enough to read and can usually be regenerated.

`derived\` is the research workbench. It contains normalized tables and features rebuilt from raw. `normalization.v2` now provides `trades`, `l2_updates`, `quotes`, `candles`, `heartbeats`, `status`, `control`, and `sessions`; older exploratory `microstructure_features`, price-only fallback candles, audited book-reconstruction runs, and strict L2 gridbot fills remain separate consumers or lanes. Account-ledger tables are later work.

`derived\v1\candles_fallback\` is a separate price-only candle lane. It can bridge gaps for rough price-path backtests, but it is not L2 and cannot say anything about spread, depth, queue, or order-book imbalance.

`quarantine\` is the review box. If a raw row is malformed, missing required fields, or cannot safely fit a table, it goes here with the reason instead of being silently thrown away.

`reports\` is for human-facing summaries. The current project mostly writes machine-readable catalog JSON first, then reports can summarize those outputs later.

In file terms: raw JSONL/GZ files store original messages; normalized files store cleaned event rows; feature files store calculated measurements such as midpoint, spread, relative spread, book imbalance, and trade imbalance; catalog files explain coverage and data quality. The existing row-exploded `microstructure_features` table is exploratory and must not be used as proof of a valid reconstructed book. Strict consumers use audited `book_reconstruction` manifests.

## Raw Data

Raw data should keep the collector's original files and folder shape whenever possible.

Legacy capture pattern from the roadmap branch:

```text
raw\
|-- btc-usd_ws_data\
|   `-- 1\
|       |-- session_start.txt
|       `-- btc_usd_2026-01-01_12-00.jsonl.gz
|-- eth-usd_ws_data\
`-- xrp-usd_ws_data\
```

The inventory tool discovers this legacy pattern. The current hardened logger writes new captures under the explicit canonical root instead of wherever the command happens to be run.

Centralized legacy moved layout:

```text
raw\
`-- legacy_ws_data\
    |-- btc-usd_ws_data\
    |   `-- 1\
    |       |-- session_start.txt
    |       `-- btc_usd_2025-08-02_23-11.jsonl
    `-- xrp-usd_ws_data\
```

External OHLCV raw layout:

```text
raw\
`-- external_ohlcv\
    `-- product=XRP-USD\
        |-- provider=coinbase_ccxt\
        |   `-- XRP_1m_from_election.csv
        |-- provider=cryptocompare\
        |   `-- XRP_1m_from_election_cc.csv
        `-- provider=coinbase_exchange_public\
            `-- granularity=60\
                `-- part_<RUN_ID>.jsonl
```

For this user's local Downloads cleanup, centralization means moving files into this root so raw files are no longer scattered across separate Downloads folders. Do not substitute hardlinks or duplicate copies for a requested move.

Legacy derived naming caveat: the old stable logger wrote `.parquet` and `.feather` with the roll timestamp, while opening the next raw `.jsonl.gz` file with that same timestamp. A derived file can therefore match the previous raw JSONL window rather than the same filename stem. Check row counts and first/last timestamps before using old derived filenames as proof of coverage.

Canonical capture layout for new logger sessions:

```text
raw\
`-- coinbase_advanced_trade\
    `-- session=20260723T010524123456Z\
        |-- manifest.json
        |-- product=BTC-USD\
        |   `-- btc_usd_part-000000_2026-07-23_01-05.jsonl.gz
        |-- product=ETH-USD\
        |-- product=XRP-USD\
        `-- channel=heartbeats\
            `-- heartbeats_part-000000_2026-07-23_01-05.jsonl.gz
```

Session roots and roll-file paths are created exclusively; an existing path is a hard failure, never a target to reopen or overwrite. A closed session is complete input only when `python -m moneyman audit-collector-session --manifest <manifest>` returns `valid=true`.

Frozen local proof session `20260723T021818Z-d5a6ccbfc0a5` met that rule: 271 frames/envelopes/routed rows, four immutable files, one connection, 18 heartbeats, no parse/sequence/duplicate/regression/reconnect/stale-heartbeat/audit defects, and a public audit with `valid=true` and `warnings=[]`. Its histogram deliberately retains 225 negative latency samples, down to `-2.711` ms, as clock-offset evidence. Longer reconnect soaks are later operational validation.

Raw rules:

- do not edit raw files in place;
- do not rename raw files as the first organization step;
- do not commit raw files;
- derive metadata by inventorying, not by manual guesses;
- preserve timestamps and folder context.
- for this machine's local Downloads cleanup, use the explicit move workflow requested by the user; do not hardlink or copy unless explicitly requested.
- `MONEYMAN_RAW_ROOT` is the single raw data root for future captures.
- if a logger session accidentally writes to repo-local `data/raw`, stop the logger before moving that session into the canonical raw root with `python -m moneyman move-stranded-sessions --mode move`.
- external OHLCV files belong under `raw\external_ohlcv`; they can feed price-only fallback tables but must not be blended into L2 tables.

## Catalog Data

Catalog files are small enough to review and can be regenerated. They explain what was found.

Recommended catalog files:

```text
catalog\
|-- inventory\
|   |-- inventory_<run_id>.json
|   `-- inventory_<run_id>.jsonl
|-- manifests\
|   `-- session_manifest_<run_id>.json
|-- quality\
|   `-- quality_<product>_<date>.md
`-- schema\
    `-- schema_version_<version>.json
```

Inventory columns should include:

- `source_path`
- `source_size_bytes`
- `modified_time`
- `sha256` when practical
- `compression`
- `product_id`
- `channel`
- `session_id`
- `first_recv_ts`
- `last_recv_ts`
- `first_event_ts`
- `last_event_ts`
- `estimated_rows`
- `sample_parse_errors`
- `inventory_run_id`

The current `python -m moneyman inventory` command writes JSON and JSONL manifests under `catalog/inventory/`.

## Derived Tables

Derived data should be partitioned and rebuildable. The current stable normalization contract is JSONL `normalization.v2`; Parquet remains a later storage optimization after schemas settle and a compatible runtime is available.

Optional future columnar export layout (not the current contract):

```text
derived\
|-- v2\
|   |-- normalization_datasets\<DATASET_ID>\
|   |   |-- manifest.json
|   |   `-- quality.json
|   |-- trades\product=BTC-USD\date=2026-01-01\part-<DATASET_ID>.jsonl
|   |-- l2_updates\product=BTC-USD\date=2026-01-01\part-<DATASET_ID>.jsonl
|   |-- quotes\product=BTC-USD\date=2026-01-01\part-<DATASET_ID>.jsonl
|   |-- candles\product=BTC-USD\date=2026-01-01\part-<DATASET_ID>.jsonl
|   |-- heartbeats\product=__none__\date=2026-01-01\part-<DATASET_ID>.jsonl
|   |-- status\product=BTC-USD\date=2026-01-01\part-<DATASET_ID>.jsonl
|   |-- control\product=__none__\date=2026-01-01\part-<DATASET_ID>.jsonl
|   `-- sessions\product=__none__\date=2026-01-01\part-<DATASET_ID>.jsonl
`-- v1\
    |-- candles_fallback\part_<RUN_ID>.jsonl
    |-- microstructure_features\features_<RUN_ID>.jsonl
    `-- book_reconstruction\<RUN_ID>\...
```

Use schema versions such as `v1`, `v2`, and keep migrations explicit. Do not silently change a table's meaning under the same version.

The normalization dataset ID binds ordered raw-input and sibling-session-manifest evidence, effective configuration, and a path-independent execution-source bundle. The manifest also binds every output artifact and six zero-error reconciliation identities. `audit-normalization` rehashes inputs and outputs, recounts JSONL rows, recalculates reconciliation, verifies current execution sources, and reruns the collector audit for any connection-complete claim. Complete claims fail closed on either normalizer- or collector-derived missing, malformed, unsequenced, regressed, or conflicting sequence evidence, as well as malformed manifest counts or paths. Product IDs that cannot safely and uniquely name a partition are quarantined before writing. Historical/partial selections stay `observed_only` even when their emitted rows reconcile perfectly.

Frozen local proofs are complete-session dataset `712a360e77c76b63686731d4` and historical observed-only dataset `7b46883be85803f8c453d7ee`. The complete dataset contains 271 canonical envelopes, 16,351 semantic rows plus one session row, zero quarantine or sequence defects, six zero reconciliations, and a successful collector re-audit. The historical dataset contains 35,574 canonical envelopes, 344,664 semantic rows plus one session row, zero quarantine or reconciliation errors, and one 53-number gap that correctly kept the complete claim false. Both public audits returned valid with no warnings and unchanged input/source evidence. The one remaining required foundation check is portability from a clean checkout on the other computer.

This V2 contract is structural and provenance-focused. It preserves Coinbase numeric text. Finiteness, sign, OHLC consistency, and BBO relationship validation remain a separate, optional later semantic-certification gate before normalized rows are called model-ready.

## Target Tables

### `trades`

Useful columns:

- `event_ts`
- `recv_ts`
- `product_id`
- `trade_id`
- `side`
- `price`
- `size`
- `sequence_num`
- `source_path`

### `quotes`

Best bid/ask rows from ticker or reconstructed book snapshots.

Useful columns:

- `event_ts`
- `recv_ts`
- `product_id`
- `best_bid`
- `best_ask`
- `midpoint`
- `spread`
- `relative_spread`
- `source`
- `source_path`

### `candles`

Useful columns:

- `start_ts`
- `recv_ts`
- `product_id`
- `open`
- `high`
- `low`
- `close`
- `volume`
- `source_path`

Fallback candle rows must also include:

- `source_kind` = `price_only_fallback`
- `source_provider`
- `limitations`

Current fallback candle rows may come from old local CSVs or from fetched Coinbase Exchange public candles. They are valid for rough price-path tests such as checking whether a grid level was touched during a missing L2 window. They are not valid for spread, depth, queue, book imbalance, or realistic fill simulation.

### `l2_updates`

Useful columns:

- `event_ts`
- `recv_ts`
- `product_id`
- `side`
- `price_level`
- `new_quantity`
- `sequence_num`
- `source_path`

### `book_snapshots`

This table should only contain windows marked valid by the reconstruction engine.

Useful columns:

- `event_ts`
- `recv_ts`
- `product_id`
- `depth_limit`
- `best_bid`
- `best_ask`
- `midpoint`
- `spread`
- `bid_depth`
- `ask_depth`
- `imbalance`
- `valid_from_sequence`
- `valid_to_sequence`
- `validity_status`

### `microstructure_features`

This is the first ClusterLOB-preparation table. It is not an ML clustering output. It should be usable for batch historical files and future real-time rolling windows.

Useful columns:

- `event_ts`
- `recv_ts`
- `product_id`
- `midpoint`
- `spread`
- `relative_spread`
- `book_imbalance`
- `trade_imbalance`
- `window`
- `source_tables`
- `quality_status`

## Real-Time Event Shape

Future live processing should reuse the same normalized schemas as archived processing. Avoid a separate "live-only" data model.

Preferred path:

```text
raw WebSocket message
    -> normalized trade or L2 update event
    -> rolling feature row
    -> append-only report/event store
```

Real-time outputs should include data-quality state such as stale feed, sequence gap, duplicate, invalid book window, and insufficient depth. They should not place live orders.

### `sessions`

Useful columns:

- `session_id`
- `collector_version`
- `git_commit`
- `products`
- `channels`
- `start_ts`
- `end_ts`
- `raw_root`
- `host`
- `shutdown_reason`
- `message_count`
- `gap_count`
- `duplicate_count`

## Reconstructed Book Contract

Current strict book outputs use this rebuildable layout:

```text
derived\
`-- v1\
    `-- book_reconstruction\
        `-- <RUN_ID>\
            |-- config.json
            |-- book_snapshots.jsonl
            |-- book_quality_events.jsonl
            |-- book_windows.jsonl
            `-- manifest.json
```

`book_snapshots.jsonl` stores valid target-product book states only. Rows include `capture_stream_id`, `connection_epoch`, `window_id`, global `sequence_num`, originating snapshot sequence, source file/line/hash, source channel, top-N bids and offers, BBO, midpoint, spread, visible/full depth, imbalance, full-state fingerprint, optional canonical full-book checkpoint hash, and visible-book hash. Depth truncation affects output only; reconstruction retains the full internal book.

`book_quality_events.jsonl` records validation, invalidation, unsequenced controls, gaps, duplicates, timestamp failures, malformed target envelopes, and auxiliary ticker rejection. `book_windows.jsonl` records each valid interval and its fresh-snapshot origin. A normal update never starts or repairs a window.

`manifest.json` is the consumer contract. It binds schema versions, exact engine source, saved config, ordered immutable raw inputs, optional right-boundary sentinel, all derived artifacts, semantic/state streams, exact checkpoint hashes, and semantic/provenance run fingerprints. Consumers must audit it; a filename or row count alone is not eligibility proof. Because the exact engine-source hash is enforced, changing `moneyman/book.py` requires rebuilding a run before it can pass the current audit.

The verified hardened runs are `20260721T235740933455Z-0760dcde` with 19,051 XRP-USD states and global sequences 0 through 26234, and independent session-2 run `20260722T032803947186Z-9593d3b9` with 22,778 states and sequences 0 through 35433. Each has one eligible window, contiguous right-boundary evidence, and no audit errors. Exact sources, results, canonical hashes, and commands are documented in `docs/L2_BOOK_RECONSTRUCTION.md`.

Strict fill outputs remain separate rebuildable artifacts under `derived/v1/backtests/gridbot/<RUN_ID>/`. The validated full-window clock pair is `20260722T120726Z` for `message_ts` and `20260722T120807Z` for `recv_ts`; both select the same 22,778 reconstruction rows. The sole pre-registered 500 ms `recv_ts` stress is `20260722T213307Z` and selects those identical rows. Generated backtest artifacts stay outside Git.

## Quarantine

Quarantine is for records that cannot safely enter a target table.

Recommended layout:

```text
quarantine\
`-- v1\
    |-- malformed_json\
    |-- unknown_channel\
    |-- missing_product\
    |-- schema_mismatch\
    `-- invalid_book_window\
```

Quarantine rows should keep the raw payload or a pointer to the raw payload, plus the reason for rejection.

## Backtest Outputs

Backtests are derived research artifacts and should not be committed by default.

Recommended layout:

```text
derived\
`-- v1\
    `-- backtests\
        `-- gridbot\
            `-- <run_id>\
                |-- config.json
                |-- fills.parquet
                |-- equity_curve.parquet
                |-- inventory.parquet
                |-- summary.json
                `-- report.md
```

Gridbot outputs must report missed sells due to insufficient base inventory and missed buys due to insufficient quote inventory.

Current authoritative gridbot V1 output uses JSON/JSONL files:

```text
derived\
`-- v1\
    `-- backtests\
        `-- gridbot\
            `-- <RUN_ID>\
                |-- config.json
                |-- fills.jsonl
                |-- order_events.jsonl
                |-- equity_curve.jsonl
                `-- summary.json
```

In fallback mode, `fills.jsonl` records attempted grid fills, including `missed_insufficient_base` and `missed_insufficient_quote` rows; no `order_events.jsonl` is written. In strict mode, `fills.jsonl` contains positive full or partial executions with per-fill maker/taker fees and visible-depth evidence, while `order_events.jsonl` records submission, latency arrival, resting, insufficient-funds/inventory rejection, partial cancellation, adjacent rearm, and window-end cancellation. `equity_curve.jsonl` records quote balance, base balance, estimated quote equity, and drawdown by candle or audited book row. `summary.json` records the data mode, exact audited run/window when strict, clock and latency, queue/partial policy, fees, turnover, final inventory/equity, baselines, and reconciliation errors.

The separate banded-lot reserve engine writes:

```text
derived\
`-- v1\
    `-- backtests\
        `-- gridbot_reserve\
            `-- <RUN_ID>\
                |-- config.json
                |-- events.jsonl
                |-- lots.jsonl
                |-- lot_diagnostics.jsonl
                |-- bands.jsonl
                |-- equity_curve.jsonl
                `-- summary.json
```

`events.jsonl` is the chronological accounting trace. `lots.jsonl` preserves each purchase's all-in cash cost, gross fees, target cash profit, rounded exit quantity, residual reserve, cost-basis allocation, status, and `tranche` (`base` or `overflow`). `lot_diagnostics.jsonl` is a separate post-trade sidecar keyed by `lot_id`; it records recovery/censoring time, 7/14/28-day recovery outcomes, exact 1-hour/6-hour/24-hour/7-day price-only close markouts, close-sampled and assumed-path adverse excursion, candle coverage, and actual cash-cost-time. It does not alter the accounting lot or event stream and is never a strategy input. `bands.jsonl` reports total/base/overflow unrecovered cash, realized cash-flow profit, end-open lots, entry-guard misses, and reserve provenance/value by half-open band. Reserve outputs must keep shared settled cash, base and overflow open-lot cost, completed reserve by tranche, and modeled rebate receivable separate. `summary.json` fingerprints the engine and candle-loader sources, saved config, full input files, exact ordered trading candle rows, signal-only pre-roll rows when enabled, and the base decision stream used to prove fixed/overflow A/B isolation. Trading and pre-roll derived-file provenance, hashes, selected counts, and time ranges stay separate. Its utilization, reserve-maximum, and drawdown metrics are explicitly close-sampled; base-band and overflow-cap headroom is theoretical rather than a promise that eligible slots exist.

In reserve-grid v1.4, a blocked entry still appears as a chronological `buy_missed` event with an `entry_guard_*` reason and the prior-close EMA snapshot that made the decision. Warmup, downtrend, and stale-signal blocks reconcile separately across portfolio, tranche, and band totals. Guard-off equity rows omit guard fields so the default control remains compatible with v1.3 artifacts.

Diagnostic markouts require a candle at the exact requested horizon. A later close after a data gap is labeled delayed and is not scored. Open-lot durations end at the final candle's `start_ts`, so they are right-censored lower bounds. Recovery rates include only cohorts with the entire requested follow-up window and adequate candle coverage. A lot timestamped at the right boundary of a gap is conservatively coverage-incomplete because a candle start timestamp cannot prove whether its fill occurred on the modeled close-to-open gap leg or later inside the candle. These are fallback-candle price-path labels, not executable L2 markouts or evidence of toxic order flow.

Current fee fields split the cost into:

- `fee_gross_quote`: normal Coinbase maker/taker fee before Coinbase One;
- `fee_rebate_quote`: estimated Coinbase One Advanced rebate, modeled as USDC-equivalent accrued value;
- `fee_net_quote`: gross fee minus rebate;
- `liquidity_assumption`: `maker` or `taker`.

The fee profile can be pulled from Coinbase's authenticated transaction summary endpoint for maker/taker rates, while the Coinbase One Advanced rebate is a configurable research assumption. The committed example scenario uses a 25% Advanced spot-fee rebate up to 100 USDC per month. Exact rebate timing is simplified in the backtest.

Strict L2 gridbot discovery uses audited `book_reconstruction` manifests and valid windows. Gaps split or invalidate windows; they are never bridged as if the order book continued through missing data. The strict fill layer lives in `moneyman/l2_fills.py`, outside the reconstruction engine. It consumes only emitted price levels through a persistent shadow ledger: unchanged displayed quantity is not reusable, and only positive observed quantity deltas replenish depth. It cancels all pending/resting orders at the selected window boundary. Verified strict outputs are `20260722T024413Z`, `20260722T033005Z`, full-window clock pair `20260722T120726Z`/`20260722T120807Z`, and sole 500 ms stress `20260722T213307Z` under `derived\v1\backtests\gridbot`; generated artifacts remain ignored and rebuildable.
