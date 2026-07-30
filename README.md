# MoneyMan

MoneyMan is a local algorithmic-trading research project for Coinbase Advanced Trade market data, especially full level 2 order-book captures. It emphasizes auditable collection, deterministic reconstruction, conservative fill modeling, and reproducible backtest methods. Paper and live trading remain deliberately out of scope.

> **Public review snapshot.** MoneyMan is a personal research project I direct and maintain with AI-assisted coding tools. AI assistance was also used to audit and sanitize this copy. The source, tests, and research intent are retained; raw data and deployment-specific material remain private. See the [complete no-hash development graph](DEVELOPMENT_HISTORY.md) and [publication boundary](PUBLIC_REVIEW_SNAPSHOT.md).

## Understand This Project

The fastest review path is to follow the evidence from project boundary to data integrity to research result:

| Review question | Best starting point | What it lets you verify |
| --- | --- | --- |
| What is implemented, limited, or next? | [Project state](docs/PROJECT_STATE.md) and [project plan](docs/PROJECT_PLAN.md) | Current boundaries, completed gates, known limitations, and the next portability check. |
| Can a capture be checked independently of the collector that wrote it? | [`coinbase_ws_stable_logger.py`](coinbase_ws_stable_logger.py), [`moneyman/collector_audit.py`](moneyman/collector_audit.py), and [collector tests](tests/test_collector.py) | Public WebSocket capture, confined path handling, closed-file hashes, independently derived routing, and adversarial audit cases. |
| How does raw input become canonical data without hiding defects? | [`moneyman/normalize.py`](moneyman/normalize.py), [normalization tests](tests/test_normalize.py), and [data layout](docs/DATA_LAYOUT.md) | Streaming table production, quarantine behavior, source-to-artifact binding, sequence findings, and count reconciliation. |
| How are L2 books reconstructed and fills constrained? | [`moneyman/book.py`](moneyman/book.py), [`moneyman/l2_fills.py`](moneyman/l2_fills.py), [book contract](docs/L2_BOOK_RECONSTRUCTION.md), and [fill model](docs/L2_GRIDBOT_FILL_MODEL.md) | Gap-aware invalidation and recovery, deterministic outputs, visible-depth consumption, latency/clock rules, and conservation checks. |
| What did the strategy experiments show, including negative results? | [`moneyman/reserve_gridbot.py`](moneyman/reserve_gridbot.py), [reserve-grid research](docs/RESERVE_GRIDBOT_RESEARCH.md), and [strategy tests](tests/test_reserve_gridbot.py) | Tagged-lot accounting, recovery diagnostics, causal controls, pre-registered comparisons, and results that did not justify enabling a feature. |
| What was retained in this public copy? | [Development history](DEVELOPMENT_HISTORY.md) and [publication boundary](PUBLIC_REVIEW_SNAPSHOT.md) | The sanitized branch topology and the explicit boundary between retained source evidence and omitted local material. |

The longer research direction is real-time limit-order-book observation: use historical data for replay, calibration, and backtesting, then reuse the same normalization and feature interfaces on live WebSocket windows. ClusterLOB-style research is an inspiration, but MoneyMan should not add clustering, participant labels, spoofing claims, or live trading until the data foundation is ready.

This repository is for learning and research. It is not financial advice, and the project should not place live trades until the data pipeline, backtester, risk controls, and paper-trading path are proven.

## Current Status

`main` now contains the recovered collector/data foundation plus audited reconstruction and gridbot research layers:

- `coinbase_ws_stable_logger.py`: Coinbase Advanced Trade public WebSocket raw logger.
- `moneyman/`: small Python package and CLI skeleton.
- `docs/ROADMAP.md`: recovered roadmap content reconciled with the current plan.
- `tests/fixtures/`: tiny synthetic WebSocket examples for parser tests.
- `python -m moneyman fee-profile`: read-only fee profile check for gridbot backtests.
- `python -m moneyman gridbot-reserve-backtest`: research-only banded lots with fee-aware principal recovery and tagged reserve coin.
- `python -m moneyman reconstruct-book`: deterministic, gap-aware Coinbase L2 reconstruction with audited valid windows.
- strict `python -m moneyman gridbot-backtest`: conservative audited-window fills with explicit latency, strict price-through queue uncertainty, visible-depth limits, partial cancellation, and conservation checks.
- `python -m moneyman audit-collector-session`: read-only verification of a closed collector manifest, its exact raw files, independently derived strict routes, and raw-derived sequence/heartbeat/latency findings. Collector paths are confined, and provenance includes the project config plus the installed `websockets` version.
- `python -m moneyman normalize`: streaming `normalization.v2` tables for trades, L2 updates, quotes, candles, heartbeats, status/control, and session provenance, partitioned by product/date and bound to source and artifact hashes. Complete claims fail closed on either normalizer- or collector-derived sequence defects, malformed product IDs, and malformed manifest counts or paths.
- `python -m moneyman audit-normalization`: read-only rehash and count reconciliation for one normalization dataset; complete-session datasets also rerun their collector audit.

The local foundation gate is closed. Fresh collector session `20260723T021818Z-d5a6ccbfc0a5`, complete-session dataset `712a360e77c76b63686731d4`, and historical observed-only dataset `7b46883be85803f8c453d7ee` all passed their public audits with no warnings, zero quarantine or reconciliation errors, and unchanged raw/source evidence. The historical proof retained one 53-number gap and correctly stayed observed-only. The current suite passes 160/160 tests, and all strategy/book sources stayed byte-identical. The next foundational goal is a clean-checkout portability rehearsal, not another L2 or strategy replay; numeric semantic certification remains an optional later lane.

The validated full-window clock-only pair changed `message_ts` to `recv_ts` and nothing else. It moved 38 of 73 order activation rows but left all 52 fill identities, execution rows, depth consumption, fees, balances, and conservation results unchanged. This is one-window execution sensitivity evidence, not profitability evidence.

The separately pre-registered 500 ms `recv_ts` stress changed only latency against the frozen 100 ms control. It produced 39 full orders plus one partial, including one arrival taker, while all depth, funding, inventory, fee, artifact, and conservation audits passed. This is a one-point mechanics stress, not a latency recommendation.

Existing raw data may live in legacy logger folders such as `btc-usd_ws_data/<session>/`, `eth-usd_ws_data/<session>/`, or `xrp-usd_ws_data/<session>/`. The inventory command discovers those folders without moving or rewriting them. New logger sessions write under one explicit raw root.

## Commands

Set paths in your shell or private `.env`:

```powershell
$env:MONEYMAN_RAW_ROOT = "D:\MoneyManData\raw"
$env:MONEYMAN_CATALOG_ROOT = "D:\MoneyManData\catalog"
$env:MONEYMAN_DERIVED_ROOT = "D:\MoneyManData\derived"
$env:MONEYMAN_QUARANTINE_ROOT = "D:\MoneyManData\quarantine"
```

Read-only inventory:

```powershell
python -m moneyman inventory --raw-root $env:MONEYMAN_RAW_ROOT --catalog-root $env:MONEYMAN_CATALOG_ROOT --include-legacy-ws-folders
```

Probe one or a few files before any broad scan or cleanup:

```powershell
python -m moneyman read-check "C:\path\to\file.jsonl.gz" --scan-all --sample-records 1
```

Audit legacy raw coverage before deleting or relocating old Parquet/Feather:

```powershell
python -m moneyman legacy-coverage --root "C:\Users\doyle\Downloads\MoneyManData\raw\legacy_ws_data" --catalog-root "C:\Users\doyle\Downloads\MoneyManData\catalog"
```

Find likely logger outages from raw filename windows:

```powershell
python -m moneyman raw-gaps --raw-root "C:\Users\doyle\Downloads\MoneyManData\raw\legacy_ws_data" --catalog-root "C:\Users\doyle\Downloads\MoneyManData\catalog" --mode filename
```

Read raw files to get true first/last receive timestamps. This is slower on the full archive:

```powershell
python -m moneyman raw-gaps --raw-root "C:\Users\doyle\Downloads\MoneyManData\raw\legacy_ws_data\sol-usd_ws_data" --catalog-root "C:\Users\doyle\Downloads\MoneyManData\catalog" --mode content
```

Move legacy Downloads captures into one raw root:

```powershell
python -m moneyman centralize-legacy --legacy-search-root "C:\Users\doyle\Downloads" --raw-root $env:MONEYMAN_RAW_ROOT --catalog-root $env:MONEYMAN_CATALOG_ROOT --mode move
```

Move a repo-local logger session into the central raw root after the logger is stopped:

```powershell
python -m moneyman move-stranded-sessions --source-raw-root ".\data\raw" --raw-root $env:MONEYMAN_RAW_ROOT --catalog-root $env:MONEYMAN_CATALOG_ROOT --mode move
```

Normalize a bounded raw JSONL/JSONL.GZ selection into versioned table/product/date partitions:

```powershell
python -m moneyman normalize --raw-root "C:\path\to\selected-session" --derived-root $env:MONEYMAN_DERIVED_ROOT --catalog-root $env:MONEYMAN_CATALOG_ROOT --quarantine-root $env:MONEYMAN_QUARANTINE_ROOT --input-order receive_time --sequence-scope observed --max-open-partitions 32
```

Use `--sequence-scope complete` only when the selected files are the exact closed-file set in a valid hardened collector manifest. Record bounds such as `--limit-files`, `--limit-records-per-file`, or `--max-records` deliberately; bounded runs never claim complete connection coverage.

Audit the saved dataset without resupplying its inputs or configuration:

```powershell
python -m moneyman audit-normalization --manifest "$env:MONEYMAN_DERIVED_ROOT\v2\normalization_datasets\<DATASET_ID>\manifest.json"
```

Calculate exploratory non-ML features from one audited v2 dataset and one product:

```powershell
python -m moneyman features --derived-root $env:MONEYMAN_DERIVED_ROOT --catalog-root $env:MONEYMAN_CATALOG_ROOT --product XRP-USD --normalization-dataset-id <DATASET_ID>
```

The v2 contract currently certifies structure, timestamps, provenance, duplicate policy, partitioning, and count conservation. Numeric strings remain source-faithful; finiteness, sign, OHLC, and bid/ask relationship validation is an optional later semantic-quality gate before model-ready feature claims.

Reconstruct an ordered Coinbase L2 window after verifying that every top-level envelope is present:

```powershell
python -m moneyman reconstruct-book --raw-file "C:\path\to\first.jsonl" --raw-file "C:\path\to\next.jsonl" --product XRP-USD --capture-stream-id my-closed-stream --sequence-scope complete --input-order file --source-layout ordered_files --depth-limit 10 --max-envelope-gap-seconds 1 --derived-root $env:MONEYMAN_DERIVED_ROOT --catalog-root $env:MONEYMAN_CATALOG_ROOT
```

Coinbase sequence numbers are connection-global, so the input must retain ticker, trade, candle, subscription, and other envelopes even though only `l2_data` changes the book. Use `--source-layout routed_shards --input-order receive_time` for current logger shards. Use `--right-boundary-file` when the next roll file is available; it is hashed as continuity evidence but not replayed. See [the L2 contract](docs/L2_BOOK_RECONSTRUCTION.md) before labeling a run complete.

Run the raw logger:

```powershell
python coinbase_ws_stable_logger.py
```

The logger reads `config\logger.json` when present, then environment variables, then defaults. Copy `config\logger.example.json` to `config\logger.json` to edit products, channels, output root, and progress settings for this computer. Existing running logger processes need a restart to pick up config changes.

The logger writes compressed JSONL under `MONEYMAN_RAW_ROOT\coinbase_advanced_trade\session=<SESSION_ID>\...`. If `MONEYMAN_RAW_ROOT` is not set but `~/Downloads/MoneyManData/raw` exists, the logger uses that central raw root. It prints the raw root, session path, roll messages, and periodic progress counts. It does not place trades.

After a session closes, independently audit it before using `sequence_scope=complete`:

```powershell
python -m moneyman audit-collector-session --manifest "$env:MONEYMAN_RAW_ROOT\coinbase_advanced_trade\session=<SESSION_ID>\manifest.json"
```

Plan or delete old Feather files after raw coverage checks:

```powershell
python -m moneyman cleanup-feather --root "C:\Users\doyle\Downloads\MoneyManData\raw\legacy_ws_data" --catalog-root "C:\Users\doyle\Downloads\MoneyManData\catalog" --mode plan
```

Import one-minute OHLCV candles as price-only fallback data:

```powershell
python -m moneyman import-candles --input "C:\Users\doyle\Downloads\MoneyManData\raw\external_ohlcv\product=XRP-USD\provider=coinbase_ccxt\XRP_1m_from_election.csv" --product XRP-USD --provider coinbase_ccxt --derived-root "C:\Users\doyle\Downloads\MoneyManData\derived" --catalog-root "C:\Users\doyle\Downloads\MoneyManData\catalog"
```

Move external OHLCV source files into the central raw data root:

```powershell
python -m moneyman move-external-ohlcv --input "C:\Users\doyle\Downloads\XRP_1m_from_election.csv" --product XRP-USD --provider coinbase_ccxt --raw-root "C:\Users\doyle\Downloads\MoneyManData\raw" --catalog-root "C:\Users\doyle\Downloads\MoneyManData\catalog" --mode move
```

Fetch Coinbase Exchange public one-minute candles as a labeled price-only fallback for a missing L2 window:

```powershell
python -m moneyman fetch-candles --product XRP-USD --product BTC-USD --product ETH-USD --start "2025-09-20T04:37:00Z" --end "2025-10-09T19:00:00Z" --raw-root "C:\Users\doyle\Downloads\MoneyManData\raw" --derived-root "C:\Users\doyle\Downloads\MoneyManData\derived" --catalog-root "C:\Users\doyle\Downloads\MoneyManData\catalog"
```

Check the gridbot fee profile without placing trades:

```powershell
python -m moneyman fee-profile --source auto
```

With Coinbase credentials visible to that PowerShell window, `--source auto` tries to pull the current Advanced Trade fee tier from Coinbase's authenticated `transaction_summary` endpoint. If the pull is unavailable, it falls back to the manual fee rate and reports the warning. The committed example research scenario uses a 25% spot-fee rebate capped at 100 USDC per month; both values are configurable or disableable.

Run the first price-only gridbot backtest against fallback candles:

```powershell
python -m moneyman gridbot-backtest --product XRP-USD --lower 2.00 --upper 3.00 --grid-count 20 --quote-start 1000 --base-start 0 --order-quote 25 --fee-source auto --include-fallback-candles --start "2025-09-20T04:37:00Z" --end "2025-10-09T19:00:00Z" --provider coinbase_exchange_public --derived-root "C:\Users\doyle\Downloads\MoneyManData\derived" --catalog-root "C:\Users\doyle\Downloads\MoneyManData\catalog"
```

Without `--include-fallback-candles`, the command stays in strict L2 mode. It audits reconstructed contracts rather than counting files, selects exactly one eligible run/window, and fails closed on ambiguity or corruption. The first conservative model uses an explicit clock and latency, treats spread-crossing arrivals as takers, requires strict price-through for resting maker fills, and carries simulated consumption across rows in a visible-depth shadow ledger. Unchanged depth is never reusable; only a positive observed quantity delta replenishes it. The model cancels uncertain partial remainders and checks quote/base/fee conservation.

Run the frozen first strict comparison:

```powershell
python -m moneyman gridbot-backtest --product XRP-USD --lower 2.995 --upper 3.016 --grid-count 21 --quote-start 1000 --base-start 0 --order-quote 25 --fee-source manual --fee-rate 0.006 --start "2025-08-01T21:22:00Z" --end "2025-08-01T21:41:00Z" --l2-run-id 20260721T235740933455Z-0760dcde --l2-window-id window-000001 --l2-latency-ms 100 --l2-clock-source message_ts --derived-root "C:\Users\doyle\Downloads\MoneyManData\derived" --catalog-root "C:\Users\doyle\Downloads\MoneyManData\catalog"
```

Run `--include-fallback-candles --provider coinbase_exchange_public` with the same grid and time bounds for the price-only control. The first audited strict run used 18,067 book rows and completed 68 full makers versus 72 low-first and 70 high-first candle touches. An independently pre-registered second run used 21,613 rows and completed 45 full makers versus 22 and 28 touches. All modes selected grid index 14, all strict reconciliation errors were zero, and both 100 ms comparison slices had no taker or partial fill. The opposite fill ordering shows that price-only touches are neither an upper nor lower execution bound. The later full-window 500 ms stress exercised one real arrival taker and one real visible-depth partial/cancellation; hand-checked fixtures still cover cases absent from the recorded rows, including a marketable resting cohort competing with new arrivals. Summaries fingerprint the code, configuration, selected inputs, and emitted artifacts. See the fill-model document for exact run IDs, results, and limitations.

Run the first banded-lot reserve experiment:

```powershell
python -m moneyman gridbot-reserve-backtest --product XRP-USD --lower 2.60 --upper 3.20 --band-width 0.20 --levels-per-band 20 --band-active-lot-budget-cap 100 --quote-start 1000 --exit-move-pct 0.05 --cash-profit-bps 20 --exit-policy principal_recovery --fee-source manual --fee-rate 0.006 --include-fallback-candles --candle-path-assumption low-first --start "2025-09-20T04:37:00Z" --end "2025-10-09T19:00:00Z" --provider coinbase_exchange_public --derived-root "C:\Users\doyle\Downloads\MoneyManData\derived" --catalog-root "C:\Users\doyle\Downloads\MoneyManData\catalog"
```

This XRP-only v1.4 engine uses one shared cash wallet. Each band has a ceiling on all-in base-lot cash still awaiting recovery; unused cash remains available and is never duplicated. Completed residual XRP stays tagged to its lot and origin band, is marked to market, and is not reusable cash. Candle replay always traverses previous close to recorded open before the chosen low-first or high-first intraminute assumption. The same-engine `full_lot` policy is the causal cash-exit control; the unchanged original `gridbot-backtest` is only a historical pooled reference because its path and accounting differ.

V1.3 adds diagnostic-only `lot_diagnostics.jsonl` rows and aggregate `summary.json` fields for 1-hour, 6-hour, 24-hour, and 7-day price-only close markouts; 7-day, 14-day, and 28-day recovery cohorts; close-sampled and assumed-path adverse excursion; right-censored open-lot age; and actual cash-cost-time. Diagnostic state is isolated from decision logic and finalized after replay, so it cannot authorize, block, resize, recall, or exit a lot. Exact-horizon markouts are not substituted across candle gaps, and recovery percentages exclude lots without full follow-up.

V1.4 adds a default-off, causal price-only entry guard. Add `--entry-guard ema_cross --entry-guard-fast-ema-span-candles 360 --entry-guard-slow-ema-span-candles 1440` to let only prior completed closes authorize new buys; existing exits are never blocked. The runner loads exactly 1,440 signal-only pre-roll rows before `--start` and fingerprints trading and pre-roll sources separately. A frozen June-July 2026 holdout passed every registered risk criterion on both candle paths, but May-August 2025 development equity was lower with the guard, so `--entry-guard none` remains the default. This is price-only research, not L2 or deployment evidence.

Set `--overflow-global-active-lot-budget-cap 100` to test one additional `$5` tranche per crossed level under a shared `$100` overflow ceiling; `0` is the default fixed control. The first bounded overflow test increased completed cycles but also trapped exposure, fees, drawdown, and loss, so overflow remains disabled by default. See [docs/RESERVE_GRIDBOT_RESEARCH.md](docs/RESERVE_GRIDBOT_RESEARCH.md) for the exact accounting and verified comparison.

## Profiling and Performance Notes

These measurements are a local verification baseline, not a production-throughput or exchange-latency claim. They used the sanitized checkout on Windows with CPython 3.12.13, generated test fixtures, and `PYTHONDONTWRITEBYTECODE=1`; filesystem cache and machine load were not controlled. Three fresh process-level runs of `python -m unittest discover -s tests -q` produced:

| Measurement | Samples | Average | Minimum | Median | Maximum | Result |
| --- | ---: | ---: | ---: | ---: | ---: | --- |
| Full synthetic test suite | 3 | 6.425 s | 6.359 s | 6.422 s | 6.495 s | 160 tests discovered; 159 passed and one Windows symlink-permission test skipped |

A separate `cProfile` run completed in 6.303 profile seconds. Its largest cumulative paths were:

| Profile path | Calls | Cumulative time | What the path covers |
| --- | ---: | ---: | --- |
| `_io.open` | 3,780 | 2.786 s | Temporary fixture, artifact, and audit-file access across the suite |
| `normalize_files` | 18 | 2.258 s | Parsing, validation, partitioned output, reconciliation, and provenance work |
| Normalizer `_sha256_file` | 1,185 | 1.563 s | Source and artifact integrity hashing |
| `run_book_reconstruction` | 48 | 0.940 s | Gap-aware book replay and emitted-window construction |
| Book `_sha256_file` | 455 | 0.755 s | Reconstruction input and artifact integrity hashing |

Cumulative profile rows overlap because a caller includes time spent in its callees. The useful conclusion is therefore limited: in this synthetic suite, temporary-file I/O, hashing, and repeated audit work cost more than the strategy arithmetic.

The repository also records larger functional validations. These counts show the amount of evidence processed, but the original runs did not record comparable wall-clock timings, so they are not throughput benchmarks:

| Audited path | Recorded scale | Integrity result |
| --- | --- | --- |
| [Normalization](docs/PROJECT_PLAN.md) | 35,574 canonical envelopes to 344,664 semantic rows plus one session row | Zero quarantine or reconciliation errors; one 53-number source gap remained explicit and observed-only |
| [L2 reconstruction](docs/L2_BOOK_RECONSTRUCTION.md) | 61,671 envelopes across two bounded prefixes to 41,829 emitted valid states | Zero strict-consumer audit errors; this was bounded-window, not archive-wide, evidence |
| [Strict fill replay](docs/L2_GRIDBOT_FILL_MODEL.md) | 22,778 depth-10 book states in one frozen full-window comparison | Clock-only variants retained identical fill identities and conservation results |

For compute-cost discussion, let `N` be input envelopes, `F` input files, `R` emitted rows, `L` live price levels, `B` emitted book states, `D` visible depth, `A` active simulated orders, `C` candles, and `S` grid slots:

| Stage | Approximate work and retained state | Current scaling pressure |
| --- | --- | --- |
| Collection audit and normalization | File-order processing is linear in input and output; receive-order merging adds `O(N log F)`. Hashing is linear in bytes, the merge holds one head per input file, and partition writers cap open handles. | JSON parsing/serialization, SQLite-backed sequence tracking, partitioned writes, and source/artifact rereads for hashes and reconciliation. |
| L2 reconstruction | A price-level insert or removal can be `O(L)` because sorted Python lists shift. Visible snapshots cost `O(D)`; full-hash checkpoints scan the book. The runner currently retains selected envelopes and emitted replay state. | Large windows and emit-every-message settings increase memory and output volume as well as replay work. |
| Strict L2 fills | Per book state, active orders are sorted and checked against visible depth, approximately `O(A log A + D)` before output costs. | Dense order cohorts, deeper visible books, and retained order/fill/equity events. |
| Reserve-grid replay | The main replay is approximately `O(C * S)`, followed by per-lot diagnostic horizon lookups. | Long candle histories, dense grids, and retained lots, events, equity rows, and diagnostics. |

The next useful performance work would be a checked-in generated-fixture benchmark that sweeps those dimensions and reports records per second, output bytes, and peak resident memory. That would separate transformation cost from end-to-end integrity-audit cost without weakening the hashes or reconciliations. If memory becomes limiting, the first implementation candidate to measure is streaming reconstruction outputs instead of retaining complete replay snapshots.

## Web GUI Rule

If MoneyMan needs a web GUI, it should plug into the existing Codex Word Game / Codex Study Hub web app instead of starting a separate visible server, URL, or port. The intended user experience is one local hub, one visible port, and a MoneyMan section inside that hub.

## Complete Sanitized Git Graph

The complete public-safe history explanation is in [`DEVELOPMENT_HISTORY.md`](DEVELOPMENT_HISTORY.md). This graph includes every commit reachable from the preserved real branch heads.

```text
* [main] 2026-07-22 | Close collector and normalization audit gaps
*        2026-07-22 | Finish audited collector and normalization foundation
*        2026-07-22 | Validate 500 ms strict L2 latency stress
*        2026-07-22 | Pre-register 500 ms strict L2 latency stress
*        2026-07-22 | Validate strict L2 clock sensitivity
*        2026-07-21 | Validate strict L2 fills on second XRP window
*        2026-07-21 | Add conservative strict L2 grid fills
*        2026-07-21 | Add audited Coinbase L2 book reconstruction
*        2026-07-20 | Add causal EMA reserve-grid entry guard
*        2026-07-12 | Document multi-window reserve grid validation
*        2026-07-12 | Add gridbot lot recovery diagnostics
*        2026-07-11 | Add bounded gridbot overflow experiment
*        2026-07-11 | Add banded reserve gridbot research
*        2026-07-09 | Add Coinbase fee profile for gridbot
*        2026-07-09 | Add first gridbot backtester
*        2026-07-09 | Add external candle fallback tools
*        2026-07-08 | Add cleanup and fallback data tools
*        2026-07-08 | Recover MoneyMan pipeline and data tools
*        2026-07-04 | Add MoneyMan project planning docs
| * [codex/add-coinbase-logger-roadmap] 2026-06-19 | add Coinbase logger and research roadmap
|/
*        2025-08-03 | Initial commit
```
