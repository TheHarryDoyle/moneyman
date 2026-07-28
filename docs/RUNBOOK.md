# MoneyMan Runbook

This file is the command map for future agents and for moving the project to another computer.

## Inspect The Repo

```powershell
git status --short
git branch --all --verbose
git log --oneline --decorate -n 12
```

If the default branch looks empty, inspect the roadmap branch before recreating work:

```powershell
git show origin/codex/add-coinbase-logger-roadmap:README.md
git show origin/codex/add-coinbase-logger-roadmap:docs/ROADMAP.md
git show origin/codex/add-coinbase-logger-roadmap:coinbase_ws_stable_logger.py
```

The useful branch work has been selectively recovered onto `main`; the branch remains reference material.

## Find Legacy Logger Folders

The prototype logger created product folders in the current working directory, usually matching `*_ws_data/<session>/`.

Look for likely folders before assuming data is missing:

```powershell
Get-ChildItem -Directory -Recurse -Filter "*_ws_data" | Select-Object FullName
```

On a user-provided raw root:

```powershell
Get-ChildItem -LiteralPath "D:\MoneyManData" -Directory -Recurse -Filter "*_ws_data" | Select-Object FullName
```

The user has now explicitly requested that the local Downloads legacy files be moved into the canonical data root, not copied or hardlinked.

Known local folders found on this machine:

```text
C:\Users\doyle\Downloads\btc-usd_ws_data
C:\Users\doyle\Downloads\eth-usd_ws_data
C:\Users\doyle\Downloads\hbar-usd_ws_data
C:\Users\doyle\Downloads\sol-usd_ws_data
C:\Users\doyle\Downloads\xlm-usd_ws_data
C:\Users\doyle\Downloads\xrp-usd_ws_data
C:\Users\doyle\Downloads\xrp_ws_data
```

## Moved Legacy Downloads Data

The local legacy Downloads files were moved into one central MoneyMan raw root on 2026-07-08. Do not repeat this move unless new legacy folders appear.

```powershell
$env:MONEYMAN_RAW_ROOT = "C:\Users\doyle\Downloads\MoneyManData\raw"
$env:MONEYMAN_CATALOG_ROOT = "C:\Users\doyle\Downloads\MoneyManData\catalog"
python -m moneyman centralize-legacy --legacy-search-root "C:\Users\doyle\Downloads" --raw-root $env:MONEYMAN_RAW_ROOT --catalog-root $env:MONEYMAN_CATALOG_ROOT --mode move
```

The central moved layout is:

```text
raw\
`-- legacy_ws_data\
    |-- btc-usd_ws_data\
    |   `-- 1\
    |       `-- btc_usd_....jsonl
    `-- xrp-usd_ws_data\
```

The actual cleanup was completed with same-drive folder moves and a locked-session fallback for `xrp-usd_ws_data\15`. Use `--mode plan` for any future preview. Do not use `--mode copy` or `--mode hardlink` for this local cleanup unless the user explicitly changes the request.

## Move Stranded Repo-Local Logger Sessions

If the logger is accidentally started without the intended environment variables, it may write under this repo:

```text
C:\Users\doyle\Downloads\moneyman\data\raw\coinbase_advanced_trade\session=<SESSION_ID>\
```

Do not move that folder while the logger is running. Stop the logger first, then run a plan:

```powershell
python -m moneyman move-stranded-sessions --source-raw-root ".\data\raw" --raw-root "C:\Users\doyle\Downloads\MoneyManData\raw" --catalog-root "C:\Users\doyle\Downloads\MoneyManData\catalog" --mode plan
```

If the plan looks right, run the real move:

```powershell
python -m moneyman move-stranded-sessions --source-raw-root ".\data\raw" --raw-root "C:\Users\doyle\Downloads\MoneyManData\raw" --catalog-root "C:\Users\doyle\Downloads\MoneyManData\catalog" --mode move
```

This command moves whole `session=...` folders. It does not copy, hardlink, convert, or delete raw files. It writes a catalog manifest under `catalog\relocate\`. By default it skips sessions whose `manifest.json` has no `end_ts`, because that usually means the logger is still open.

## Set Up Python

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
```

If `requirements.txt` is not yet on the current branch, recover it from `origin/codex/add-coinbase-logger-roadmap` before installing dependencies.

## Configure Local Paths

Use a private `.env` or session environment variables. Do not commit `.env`.

When no environment variable is set, MoneyMan now prefers `C:\Users\doyle\Downloads\MoneyManData` if that folder exists, then falls back to repo-local `data`. Explicit environment variables still win and are recommended for clarity.

```powershell
$env:MONEYMAN_DATA_ROOT = "D:\MoneyManData"
$env:MONEYMAN_RAW_ROOT = "D:\MoneyManData\raw"
$env:MONEYMAN_DERIVED_ROOT = "D:\MoneyManData\derived"
$env:MONEYMAN_CATALOG_ROOT = "D:\MoneyManData\catalog"
$env:MONEYMAN_QUARANTINE_ROOT = "D:\MoneyManData\quarantine"
```

For Codex Study Hub integration:

```powershell
$env:CODEX_STUDY_HUB_ROOT = "C:\Path\To\CodexWordGame"
$env:CODEX_STUDY_HUB_PORT = "8000"
```

To set the user-level MoneyMan variables permanently for future PowerShell windows:

```powershell
[Environment]::SetEnvironmentVariable("MONEYMAN_DATA_ROOT", "C:\Users\doyle\Downloads\MoneyManData", "User")
[Environment]::SetEnvironmentVariable("MONEYMAN_RAW_ROOT", "C:\Users\doyle\Downloads\MoneyManData\raw", "User")
[Environment]::SetEnvironmentVariable("MONEYMAN_DERIVED_ROOT", "C:\Users\doyle\Downloads\MoneyManData\derived", "User")
[Environment]::SetEnvironmentVariable("MONEYMAN_CATALOG_ROOT", "C:\Users\doyle\Downloads\MoneyManData\catalog", "User")
[Environment]::SetEnvironmentVariable("MONEYMAN_QUARANTINE_ROOT", "C:\Users\doyle\Downloads\MoneyManData\quarantine", "User")
```

Permanent environment-variable changes apply to new terminals. For the current PowerShell window, also run:

```powershell
$env:MONEYMAN_DATA_ROOT = "C:\Users\doyle\Downloads\MoneyManData"
$env:MONEYMAN_RAW_ROOT = "C:\Users\doyle\Downloads\MoneyManData\raw"
$env:MONEYMAN_DERIVED_ROOT = "C:\Users\doyle\Downloads\MoneyManData\derived"
$env:MONEYMAN_CATALOG_ROOT = "C:\Users\doyle\Downloads\MoneyManData\catalog"
$env:MONEYMAN_QUARANTINE_ROOT = "C:\Users\doyle\Downloads\MoneyManData\quarantine"
```

## Configure Read-Only Coinbase Fee Pull

MoneyMan can use Coinbase credentials to pull account fee-tier facts for backtests. This is read-only: the current code calls fee/account endpoints only and does not place orders.

The credential variables are:

```powershell
$env:COINBASE_API_KEY_NAME = "organizations/<org-id>/apiKeys/<key-id>"
$env:COINBASE_API_PRIVATE_KEY = "<private key from Coinbase>"
```

`COINBASE_API_KEY` and `COINBASE_API_SECRET` are also accepted for compatibility with common naming. Do not commit real values.

The committed example scenario uses `Coinbase Advanced rebates: 25% rebate on spot fees, up to 100 USDC/mo`. MoneyMan defaults to that research profile unless overridden or disabled:

```powershell
$env:MONEYMAN_COINBASE_ONE_ADVANCED_REBATE_RATE = "0.25"
$env:MONEYMAN_COINBASE_ONE_ADVANCED_REBATE_CAP_USDC = "100"
$env:MONEYMAN_COINBASE_ONE_ADVANCED_REBATE_USED_USDC = "0"
$env:MONEYMAN_GRIDBOT_LIQUIDITY_ASSUMPTION = "maker"
```

Use a read-only Coinbase key when possible. If a key has trade permission, MoneyMan still does not call order-placement endpoints in this slice.

## Logger Config JSON

The logger can also read a local JSON config:

```text
config\logger.json
```

Use `config\logger.example.json` as the committed template. The real `config\logger.json` is ignored by Git because it is local-machine configuration.

Useful fields:

- `raw_root`: where new sessions write;
- `products`: product list such as `["XRP-USD", "BTC-USD", "ETH-USD"]`;
- `channels`: Coinbase public WebSocket channels;
- `roll_interval_seconds`: how often files roll;
- `progress_interval_messages`: how often console progress prints.

Existing running logger processes do not reload this file. Stop and restart the logger to apply changes.

Each new hardened session uses an exclusive, confined session directory and strict product/channel route names. Its manifest binds the effective config, an execution-source bundle that includes project configuration, host/runtime including the installed `websockets` version, connection epochs, sequence/heartbeat/latency findings, and every closed raw file. The auditor independently derives expected routes and fails closed on malformed manifest counts or paths. Audit a stopped session read-only before treating it as complete input:

```powershell
python -m moneyman audit-collector-session --manifest "$env:MONEYMAN_RAW_ROOT\coinbase_advanced_trade\session=<SESSION_ID>\manifest.json"
```

Exit `0` means the manifest and raw-derived findings reconciled. Exit `1` means the session is not eligible; do not repair the manifest by hand.

## Raw Data Inventory

The first data command is read-only. It inspects file metadata and a tiny sample, searches legacy logger folders when requested, then writes a manifest outside the raw folder.

Command shape:

```powershell
python -m moneyman inventory --raw-root $env:MONEYMAN_RAW_ROOT --catalog-root $env:MONEYMAN_CATALOG_ROOT --include-legacy-ws-folders
```

Expected behavior:

- no raw file writes;
- no file moves;
- no conversion yet;
- manifest rows for path, size, modified time, compression, product, channel, session, row count estimate, parse errors, and time range.

Tiny local legacy check example:

```powershell
python -m moneyman inventory --raw-root "C:\Users\doyle\Downloads\btc-usd_ws_data" --catalog-root .\catalog --sample-records 3 --max-files 5
```

## Read One File First

Use `read-check` when debugging file readability, naming drift, or cleanup safety. It probes one or a few files and prints JSON with first/last records, row counts, parse errors, dependency errors, and same-stem comparisons. It does not move, delete, or convert files.

```powershell
python -m moneyman read-check "C:\Users\doyle\Downloads\MoneyManData\raw\legacy_ws_data\btc-usd_ws_data\11\btc_usd_2025-08-14_02-52.jsonl.gz" --sample-records 1 --scan-all
```

In the current Codex shell, plain `python` may not be on PATH. The bundled runtime can read raw JSONL/GZ:

```powershell
& "C:\Users\doyle\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe" -m moneyman read-check "<file.jsonl.gz>" --sample-records 1 --scan-all
```

The bundled runtime does not currently have `pyarrow`, so it cannot read Parquet or Feather. The local Python install does have pandas and pyarrow:

```powershell
& "C:\Users\doyle\AppData\Local\Programs\Python\Python313\python.exe" -m moneyman read-check "<file.jsonl.gz>" "<file.parquet>" "<file.feather>" --sample-records 1 --scan-all
```

Known old-logger naming issue: a derived file named with a roll timestamp may correspond to the previous raw JSONL window. Example verified on this machine:

```text
btc_usd_2025-08-14_02-42.jsonl.gz  -> 2025-08-14T02:42:40Z through 2025-08-14T02:52:40Z, 16,041 records
btc_usd_2025-08-14_02-52.parquet   -> same window, 16,041 rows
btc_usd_2025-08-14_02-52.feather   -> same window, 16,041 rows
btc_usd_2025-08-14_02-52.jsonl.gz  -> next window, 16,014 records
```

## Storage Audit

Use this before deleting or relocating legacy derived files:

```powershell
python -m moneyman storage-audit --root "C:\Users\doyle\Downloads\MoneyManData\raw\legacy_ws_data" --catalog-root "C:\Users\doyle\Downloads\MoneyManData\catalog"
```

MoneyMan's source of truth is raw `.jsonl` and `.jsonl.gz`. The old logger also wrote `.parquet` and `.feather` snapshots from the same rolling buffer. Do not delete raw JSONL/GZ. If space cleanup is approved, prefer removing or relocating `.feather` first while keeping Parquet as a smaller derived fallback.

Do not rely on same-stem filename coverage alone. Because of the old roll-timestamp behavior, compare derived files against the previous raw window or run several `read-check` probes before deciding any derived files are safe to remove.

## Feather Cleanup

Use this to plan or delete old `.feather` files after checking for raw candidates:

```powershell
python -m moneyman cleanup-feather --root "C:\Users\doyle\Downloads\MoneyManData\raw\legacy_ws_data" --catalog-root "C:\Users\doyle\Downloads\MoneyManData\catalog" --mode plan --coverage-required any-raw-candidate
```

Delete mode uses the same classifier and writes a manifest:

```powershell
python -m moneyman cleanup-feather --root "C:\Users\doyle\Downloads\MoneyManData\raw\legacy_ws_data" --catalog-root "C:\Users\doyle\Downloads\MoneyManData\catalog" --mode delete --coverage-required any-raw-candidate
```

On 2026-07-09, delete mode removed 132,313 eligible Feather files, totaling 663,459,967,538 bytes, with zero failures. One tiny 49,338 byte Feather file with no raw candidate was intentionally left in place. Delete manifest:

```text
C:\Users\doyle\Downloads\MoneyManData\catalog\cleanup\feather_cleanup_20260709T031649Z.jsonl
```

## Legacy Coverage Audit

Use this to quantify how many old Parquet/Feather files have raw JSONL/GZ candidates. It separates same-stem raw files from previous-window raw files because the stable gzip logger wrote derived files at the roll timestamp.

```powershell
python -m moneyman legacy-coverage --root "C:\Users\doyle\Downloads\MoneyManData\raw\legacy_ws_data" --catalog-root "C:\Users\doyle\Downloads\MoneyManData\catalog"
```

Output goes under `catalog\coverage\legacy_coverage_<RUN_ID>.json`.

Read these fields first:

- `previous_window_raw_files`: strongest filename-level signal for old gzip logger coverage.
- `exact_stem_raw_files`: useful but not proof, because same-stem raw may be the next window.
- `same_directory_raw_files`: weaker signal; usually means old plain JSONL or irregular naming needs sampling.
- `no_raw_candidate_files`: files that need inspection before any cleanup.

This command is metadata-only. It does not delete, move, rewrite, decompress, or convert raw data.

## Raw Gap Audit

Use `raw-gaps` to find likely missing capture windows. The fast mode uses filename timestamps and is the right first pass for power-off outages:

```powershell
python -m moneyman raw-gaps --raw-root "C:\Users\doyle\Downloads\MoneyManData\raw\legacy_ws_data" --catalog-root "C:\Users\doyle\Downloads\MoneyManData\catalog" --mode filename --roll-seconds 600 --tolerance-seconds 90
```

The stronger mode reads raw JSONL/JSONL.GZ files and records first/last receive timestamps. Use it on a product or small folder first, because a full archive run must read hundreds of GB of compressed raw files:

```powershell
python -m moneyman raw-gaps --raw-root "C:\Users\doyle\Downloads\MoneyManData\raw\legacy_ws_data\sol-usd_ws_data" --catalog-root "C:\Users\doyle\Downloads\MoneyManData\catalog" --mode content
```

Content mode writes:

```text
catalog\gaps\raw_gaps_content_<RUN_ID>.json
catalog\gaps\raw_file_boundaries_<RUN_ID>.jsonl
```

Filename mode finds missing expected file windows. Content mode proves the actual beginning/end timestamps inside each file.

## Candle Fallback For Missing L2 Windows

The old Downloads scripts confirm there are two different data lanes:

- `fromal2.py` turns captured market-trade JSONL/GZ files into one-minute OHLCV candles.
- `morecandles.py` and `moremorecandles.py` fetch one-minute OHLCV from outside services.
- `backtesterverify.py` uses OHLC candles for EMA/SMA-style optimization.

Those candles can be useful for a temporary contiguous price path. They can answer rough questions like whether a grid level was touched during a missing L2 window. They cannot recreate bid/ask spread, book depth, queue behavior, partial fills, or L2 imbalance for that missing window.

Any backtest that uses candles to bridge a gap must label those rows as `price_only_fallback`. Any depth-aware or spread-aware L2 backtest should skip missing L2 windows unless true replacement L2 data is imported and tagged by provider/schema.

Import local OHLCV CSVs into the fallback candle table:

```powershell
python -m moneyman import-candles --input "C:\Users\doyle\Downloads\MoneyManData\raw\external_ohlcv\product=XRP-USD\provider=coinbase_ccxt\XRP_1m_from_election.csv" --product XRP-USD --provider coinbase_ccxt --derived-root "C:\Users\doyle\Downloads\MoneyManData\derived" --catalog-root "C:\Users\doyle\Downloads\MoneyManData\catalog"
```

Move local external OHLCV source files out of Downloads and into the central raw data root:

```powershell
python -m moneyman move-external-ohlcv --input "C:\Users\doyle\Downloads\XRP_1m_from_election.csv" --product XRP-USD --provider coinbase_ccxt --raw-root "C:\Users\doyle\Downloads\MoneyManData\raw" --catalog-root "C:\Users\doyle\Downloads\MoneyManData\catalog" --mode move
```

On 2026-07-09, these source CSVs were moved, not copied:

```text
C:\Users\doyle\Downloads\MoneyManData\raw\external_ohlcv\product=XRP-USD\provider=coinbase_ccxt\XRP_1m_from_election.csv
C:\Users\doyle\Downloads\MoneyManData\raw\external_ohlcv\product=XRP-USD\provider=cryptocompare\XRP_1m_from_election_cc.csv
```

Fetch Coinbase Exchange public candles as labeled fallback data for a missing L2 window:

```powershell
python -m moneyman fetch-candles --product XRP-USD --product BTC-USD --product ETH-USD --start "2025-09-20T04:37:00Z" --end "2025-10-09T19:00:00Z" --granularity-seconds 60 --raw-root "C:\Users\doyle\Downloads\MoneyManData\raw" --derived-root "C:\Users\doyle\Downloads\MoneyManData\derived" --catalog-root "C:\Users\doyle\Downloads\MoneyManData\catalog"
```

Current imported fallback outputs:

```text
C:\Users\doyle\Downloads\MoneyManData\derived\v1\candles_fallback\part_20260709T162228Z.jsonl
C:\Users\doyle\Downloads\MoneyManData\derived\v1\candles_fallback\part_20260709T162335Z.jsonl
```

The first imported CSV covers 2020-11-05 through 2021-01-19. The CryptoCompare CSV covers 2025-07-28 through 2025-08-04. They were re-imported after the source CSVs were moved into `raw\external_ohlcv`, and the two stale pre-move generated fallback files were removed.

Current fetched missing-gap fallback outputs:

```text
C:\Users\doyle\Downloads\MoneyManData\raw\external_ohlcv\product=XRP-USD\provider=coinbase_exchange_public\granularity=60\part_20260709T161654Z.jsonl
C:\Users\doyle\Downloads\MoneyManData\raw\external_ohlcv\product=BTC-USD\provider=coinbase_exchange_public\granularity=60\part_20260709T161741Z.jsonl
C:\Users\doyle\Downloads\MoneyManData\raw\external_ohlcv\product=ETH-USD\provider=coinbase_exchange_public\granularity=60\part_20260709T161832Z.jsonl
C:\Users\doyle\Downloads\MoneyManData\derived\v1\candles_fallback\part_20260709T161654Z.jsonl
C:\Users\doyle\Downloads\MoneyManData\derived\v1\candles_fallback\part_20260709T161741Z.jsonl
C:\Users\doyle\Downloads\MoneyManData\derived\v1\candles_fallback\part_20260709T161832Z.jsonl
```

Each product fetched 28,223 one-minute candles from `2025-09-20T04:37:00Z` through `2025-10-09T18:59:00Z`, with zero missing buckets and zero request errors in the generated quality reports. Coinbase Exchange public candles are useful here because they do not require account credentials. Coinbase's documentation notes this endpoint returns at most 300 candles per request and historical rate data may be incomplete when no ticks exist, so MoneyMan still records missing-bucket counts in every fetch report.

The reserve-grid regime validation also fetched XRP candles from May through September 2025:

```text
C:\Users\doyle\Downloads\MoneyManData\raw\external_ohlcv\product=XRP-USD\provider=coinbase_exchange_public\granularity=60\part_20260712T215636Z.jsonl
C:\Users\doyle\Downloads\MoneyManData\derived\v1\candles_fallback\part_20260712T215636Z.jsonl
C:\Users\doyle\Downloads\MoneyManData\catalog\quality\candle_fallback_fetch_20260712T215636Z.json
```

It contains 204,755 of 204,757 expected one-minute candles from `2025-05-01T00:00:00Z` through `2025-09-20T04:36:00Z`, with zero request errors. The two missing buckets are `2025-08-11T20:00:00Z` and `2025-08-11T20:01:00Z`; backtest diagnostics preserve that outage as one 180-second gap. The verified May-August fixed-profile matrix and all 16 run IDs are recorded in `docs/RESERVE_GRIDBOT_RESEARCH.md`.

## Audited L2 Book Reconstruction

Use `reconstruct-book` only after identifying one bounded capture stream and its complete top-level envelope order. Coinbase `sequence_num` is connection-global, so do not feed only flattened L2 rows or sort updates by event timestamp.

The first verified legacy XRP command is:

```powershell
python -m moneyman reconstruct-book `
  --raw-file "C:\Users\doyle\Downloads\MoneyManData\raw\legacy_ws_data\xrp_ws_data\xrp_2025-08-01_21-21.jsonl" `
  --raw-file "C:\Users\doyle\Downloads\MoneyManData\raw\legacy_ws_data\xrp_ws_data\xrp_2025-08-01_21-31.jsonl" `
  --product XRP-USD `
  --capture-stream-id legacy-xrp-ws-2025-08-01-session `
  --sequence-scope complete --input-order file --source-layout ordered_files `
  --depth-limit 10 --emit-every-l2-messages 1 `
  --full-hash-sequence 13632 --full-hash-sequence 13633 --full-hash-sequence 26232 `
  --max-envelope-gap-seconds 1 --ticker-tolerance 0.0001 `
  --right-boundary-file "C:\Users\doyle\Downloads\MoneyManData\raw\legacy_ws_data\xrp_ws_data\xrp_2025-08-01_21-41.jsonl" `
  --derived-root "C:\Users\doyle\Downloads\MoneyManData\derived" `
  --catalog-root "C:\Users\doyle\Downloads\MoneyManData\catalog"
```

The independent session-2 validation uses the same contract with these inputs and checkpoints:

```powershell
python -m moneyman reconstruct-book `
  --raw-file "C:\Users\doyle\Downloads\MoneyManData\raw\legacy_ws_data\xrp_ws_data\2\xrp_2025-08-02_15-58.jsonl" `
  --raw-file "C:\Users\doyle\Downloads\MoneyManData\raw\legacy_ws_data\xrp_ws_data\2\xrp_2025-08-02_16-08.jsonl" `
  --product XRP-USD `
  --capture-stream-id legacy-xrp-ws-2025-08-02-session-2 `
  --sequence-scope complete --input-order file --source-layout ordered_files `
  --depth-limit 10 --emit-every-l2-messages 1 `
  --full-hash-sequence 17890 --full-hash-sequence 17891 --full-hash-sequence 35432 `
  --max-envelope-gap-seconds 1 --ticker-tolerance 0.0001 `
  --right-boundary-file "C:\Users\doyle\Downloads\MoneyManData\raw\legacy_ws_data\xrp_ws_data\2\xrp_2025-08-02_16-18.jsonl" `
  --derived-root "C:\Users\doyle\Downloads\MoneyManData\derived" `
  --catalog-root "C:\Users\doyle\Downloads\MoneyManData\catalog"
```

Use `--source-layout ordered_files --input-order file` only for one stream and its sequential roll files. Current logger product/channel shards require `--source-layout routed_shards --input-order receive_time`; every selected record must have a receive timestamp, and identical cross-shard routing copies are collapsed before duplicate policy. `--sequence-scope complete` is an explicit completeness attestation. When the next roll exists, `--right-boundary-file` strengthens that evidence by hashing the file and requiring its first sequence to immediately follow the replayed window without replaying it.

Outputs go under:

```text
derived\v1\book_reconstruction\<RUN_ID>\
|-- config.json
|-- book_snapshots.jsonl
|-- book_quality_events.jsonl
|-- book_windows.jsonl
`-- manifest.json
```

The matching catalog report is under `catalog\quality\book_reconstruction_<RUN_ID>.json`. Exact full-book hashes are computed for every fresh snapshot and each repeated `--full-hash-sequence`; all other emitted states use an O(1) deterministic full-state fingerprint plus an exact visible-depth hash. Checkpoint keys include connection epoch, for example `0:13632`.

The hardened frozen runs are `20260721T235740933455Z-0760dcde` with 19,051 states and `20260722T032803947186Z-9593d3b9` with 22,778 states. Each contains one eligible window, has a contiguous right-boundary sentinel, and returned no independent audit errors. Full results and contract rules are in `docs/L2_BOOK_RECONSTRUCTION.md`.

Do not consume a run merely because `book_snapshots.jsonl` exists. Strict consumers call `audit_book_reconstruction_run` through discovery and fail closed on source, engine, config, artifact, state, window-origin, or fingerprint mismatch. Changing `moneyman/book.py` invalidates the current engine-source audit and requires reconstruction with the new engine version/hash.

## Gridbot Backtest

The pooled gridbot backtester has two deliberately separate data modes: explicit price-only fallback candles and audited strict L2 execution. The reserve-grid engine remains a separate candle-only command.

Check fees first:

```powershell
python -m moneyman fee-profile --source auto
```

`--source auto` tries to pull the current maker/taker fee tier from Coinbase's authenticated `GET /api/v3/brokerage/transaction_summary` endpoint. If the auto pull cannot run, it falls back to `--fee-rate` and writes a warning in the result. `--source coinbase` is stricter and fails instead of falling back.

MoneyMan models the Coinbase One Advanced rebate as:

```text
gross_fee = fill_notional * maker_or_taker_fee_rate
rebate = min(gross_fee * 0.25, remaining_monthly_rebate_cap)
net_fee = gross_fee - rebate
```

For fallback-candle backtests, grid orders are modeled as maker fills by default because gridbots normally place limit orders. This is still an assumption: candle data cannot prove queue position or maker/taker status. Run `--liquidity-assumption taker` for a more conservative fee stress test.

Run a fallback-candle gridbot backtest:

```powershell
python -m moneyman gridbot-backtest --product XRP-USD --lower 2.00 --upper 3.00 --grid-count 20 --quote-start 1000 --base-start 0 --order-quote 25 --fee-source auto --include-fallback-candles --start "2025-09-20T04:37:00Z" --end "2025-10-09T19:00:00Z" --provider coinbase_exchange_public --derived-root "C:\Users\doyle\Downloads\MoneyManData\derived" --catalog-root "C:\Users\doyle\Downloads\MoneyManData\catalog"
```

Run the first frozen audited-window strict comparison:

```powershell
python -m moneyman gridbot-backtest `
  --product XRP-USD --lower 2.995 --upper 3.016 --grid-count 21 `
  --quote-start 1000 --base-start 0 --order-quote 25 `
  --fee-source manual --fee-rate 0.006 `
  --start "2025-08-01T21:22:00Z" --end "2025-08-01T21:41:00Z" `
  --l2-run-id 20260721T235740933455Z-0760dcde `
  --l2-window-id window-000001 `
  --l2-latency-ms 100 --l2-clock-source message_ts `
  --derived-root "C:\Users\doyle\Downloads\MoneyManData\derived" `
  --catalog-root "C:\Users\doyle\Downloads\MoneyManData\catalog"
```

Use `message_ts` for this first frozen legacy run because every audited `recv_ts` is null. The selected clock is explicit; strict mode never mixes clock fields.

Run the frozen second strict comparison:

```powershell
python -m moneyman gridbot-backtest `
  --product XRP-USD --lower 2.816 --upper 2.837 --grid-count 21 `
  --quote-start 1000 --base-start 0 --order-quote 25 `
  --fee-source manual --fee-rate 0.006 `
  --start "2025-08-02T15:59:00Z" --end "2025-08-02T16:18:00Z" `
  --l2-run-id 20260722T032803947186Z-9593d3b9 `
  --l2-window-id window-000001 `
  --l2-latency-ms 100 --l2-clock-source message_ts `
  --derived-root "C:\Users\doyle\Downloads\MoneyManData\derived" `
  --catalog-root "C:\Users\doyle\Downloads\MoneyManData\catalog"
```

The completed full-window clock sensitivity omitted `--start` and `--end` from that second command and ran it twice, changing only `--l2-clock-source message_ts` versus `recv_ts`. Runs `20260722T120726Z` and `20260722T120807Z` each consumed the same 22,778 ordered states and selected-row hash `86ae456663f2583f3dfb82b406ecde549c5a013c58b83a24f1cbbee90d3262f8`. The receive clock changed 38 of 73 activation sequence rows, but all 52 fills retained the same order IDs, execution rows, depth consumption, fees, ending inventory, and exact conservation. Artifact bytes differ because their time fields and hashes differ; the execution/economic equality is the valid comparison. Strict mode never mixes clock fields, and the minute-aligned price controls remain historical context rather than an exact-row clock control.

The one-point latency stress was pre-registered at exactly 500 ms before any candidate replay, using the fixed rule `5 * 100 ms control`, and completed as run `20260722T213307Z`. This command reproduces its exact saved configuration with environment-sensitive values made explicit; do not add another latency value to this window:

```powershell
python -m moneyman gridbot-backtest `
  --product XRP-USD --lower 2.816 --upper 2.837 --grid-count 21 `
  --quote-start 1000 --base-start 0 --order-quote 25 `
  --fee-source manual --fee-rate 0.006 `
  --maker-fee-rate 0.006 --taker-fee-rate 0.006 `
  --liquidity-assumption maker --candle-path-assumption low-first `
  --coinbase-one-advanced-rebate-rate 0.25 `
  --coinbase-one-monthly-rebate-cap 100 `
  --coinbase-one-monthly-rebate-used 0 `
  --l2-run-id 20260722T032803947186Z-9593d3b9 `
  --l2-window-id window-000001 `
  --l2-latency-ms 500 --l2-clock-source recv_ts `
  --derived-root "C:\Users\doyle\Downloads\MoneyManData\derived" `
  --catalog-root "C:\Users\doyle\Downloads\MoneyManData\catalog"
```

The frozen control is run `20260722T120807Z`. The stress selected the same 22,778 rows and hash above, and its saved config differed only at `l2_latency_ms`. It produced 39 full orders plus one partial, including one taker; all 40 depth consumptions, artifacts, balances, fees, and conservation identities independently verified. Acceptance was based on execution rules, artifact integrity, and exact conservation, not fill count or performance. Do not interpret its slightly higher ending equity as evidence that slower latency helps: both cases lost versus no-trade cash.

Outputs go under:

```text
derived\v1\backtests\gridbot\<RUN_ID>\
|-- config.json
|-- fills.jsonl
|-- order_events.jsonl   strict L2 only
|-- equity_curve.jsonl
`-- summary.json
```

The summary reports filled buys, filled sells, missed buys from insufficient quote, missed sells from insufficient base, gross fees, Coinbase One rebates, net fees, turnover, final equity, no-trade equity, buy-and-hold equity, and max drawdown. Fills include `fee_gross_quote`, `fee_rebate_quote`, and `fee_net_quote`. Completed summaries also fingerprint the gridbot source, saved config, selected input rows, and emitted config/fill/event/equity artifacts; strict runs additionally fingerprint the fill-engine source and frozen fill contract.

Important assumptions:

- `--include-fallback-candles` means price-only mode. It can test whether grid prices were touched, but not real spread, depth, queue, or exact intraminute order.
- `--candle-path-assumption low-first` assumes each candle trades from the prior close down to low, then up to high. `high-first` flips that order. Run both when the order of high/low matters.
- Without `--include-fallback-candles`, the command uses strict L2 mode. It returns `requires_book_snapshots` when no contract exists, `requires_valid_book_snapshots` when no audited eligible window exists, and a selection status when more than one run/window is eligible without explicit IDs.
- A strict arrival that crosses visible contra depth is a taker. A nonmarketable arrival rests and earns maker treatment only after the opposite best price moves strictly through its limit. Touch, same-level depletion, and disappearance are not fills.
- Strict execution carries one persistent emitted-depth shadow ledger across rows. Unchanged quantity cannot be reused; only a positive observed quantity delta replenishes a side/price, and leaving then re-entering top-N does not reset prior consumption. Same-row orders share that ledger. Insufficient visible quantity causes one partial execution and immediate cancellation of the uncertain remainder. Full-depth aggregates and hidden liquidity are never consumed.
- Strict buys require funding for the full original limit order plus gross fee; sells require the full original base target. Only a full fill rearms the adjacent grid order. Quote, base, fee, and turnover conservation must be exactly zero.
- L2 gaps do not make the whole archive unusable. Strict L2 backtests should use only continuous valid book windows and skip or segment at gaps.
- L2 is not L3/MBO. Even good L2 windows cannot provide exact order lifecycles, queue position, hidden liquidity, named participant identity, or proof of spoofing.

The validated first run IDs are `20260722T024413Z` strict, `20260722T024444Z` low-first, and `20260722T024501Z` high-first. The independent second IDs are `20260722T033005Z` strict, `20260722T033049Z` low-first, and `20260722T033102Z` high-first. The full-window clock-only pair is `20260722T120726Z` for `message_ts` and `20260722T120807Z` for `recv_ts`; the sole 500 ms stress is `20260722T213307Z`. Read `docs/L2_GRIDBOT_FILL_MODEL.md` before interpreting or changing them.

## Banded Lot Reserve Backtest

Use the separate reserve command when testing the user's principal-recovery design. V1.4 is XRP-only. It preserves the original pooled gridbot as a historical reference; use `full_lot` in this engine as the same-path causal control.

```powershell
python -m moneyman gridbot-reserve-backtest `
  --product XRP-USD `
  --lower 2.60 --upper 3.20 --band-width 0.20 `
  --levels-per-band 20 --band-active-lot-budget-cap 100 `
  --overflow-global-active-lot-budget-cap 0 `
  --quote-start 1000 --exit-move-pct 0.05 --cash-profit-bps 20 `
  --exit-policy principal_recovery `
  --base-increment 0.000001 --quote-increment 0.01 --price-increment 0.0001 `
  --min-quote-notional 1 --fee-source manual --fee-rate 0.006 `
  --include-fallback-candles --candle-path-assumption low-first `
  --start "2025-09-20T04:37:00Z" --end "2025-10-09T19:00:00Z" `
  --provider coinbase_exchange_public `
  --derived-root "C:\Users\doyle\Downloads\MoneyManData\derived" `
  --catalog-root "C:\Users\doyle\Downloads\MoneyManData\catalog"
```

Accounting rules:

- `quote-start` is the only initial cash wallet; band caps do not add deposits.
- `band-active-lot-budget-cap` limits base-tranche all-in cash cost in open lots awaiting recovery in that band.
- `overflow-global-active-lot-budget-cap` adds one duplicate tranche per price level under a separate portfolio-wide unrecovered-cost ceiling. It uses the same quote wallet; `0` disables it.
- level cash budgets include gross buy fees.
- `quote-increment` floors each slot's all-in cash allocation; it does not round prices, fees, or settlement.
- unused cash is shared and may fund any band that still has cap headroom.
- `principal_recovery` sizes each exit to restore the lot's actual cash cost plus the requested cash-profit basis points.
- the residual base stays tagged to its lot and origin band as reserve and is not spendable cash.
- exit feasibility uses gross fees before rebates.
- modeled rebates remain a separate, nonspendable receivable in v1.
- `full_lot` is the controlled cash-exit comparison; it sells the entire lot at the same target and retains no reserve.
- each candle traverses previous close to recorded open before the selected open/low/high/close assumption.
- invalid price-tick spacing, non-XRP inputs, mismatched products, and duplicate or unsorted candle times fail before trading.

Outputs go under `derived\v1\backtests\gridbot_reserve\<RUN_ID>`. Read `events.jsonl` to hand-check chronological cash/base changes, `lots.jsonl` for per-purchase accounting, `lot_diagnostics.jsonl` for post-trade recovery and downside labels, `bands.jsonl` for origin-band accounting, and `summary.json` for portfolio results and reconciliation errors. The summary records a collision-resistant run ID, engine version/source hash, config hash, full input-file hashes, and the hash of the exact ordered candle rows selected. Output folders are never silently reused.

V1.3 diagnostics are observational only:

- 1-hour, 6-hour, 24-hour, and 7-day buy markouts use the candle close at the exact horizon and continue to be measured after a lot exits;
- 7-day, 14-day, and 28-day recovery rates use full-follow-up cohorts, while young open lots remain right-censored rather than counted as failures;
- close-sampled adverse excursion is separate from the low-first/high-first assumed-path excursion;
- cash-cost-time uses the actual all-in unrecovered cost multiplied by observed lock duration and reconciles independently to the event stream; and
- a missing exact markout candle is not replaced with a later close across a gap, and lots stamped at a gap's right boundary are excluded from complete-coverage cohorts.

These diagnostics do not change entry, exit, sizing, cap, reserve, or overflow behavior. They are price-only labels, not executable L2 markouts or proof of toxic order flow.

V1.4 also provides one optional causal entry guard. It is off unless explicitly requested. Append these flags to the fixed-control command above:

```powershell
--entry-guard ema_cross `
--entry-guard-fast-ema-span-candles 360 `
--entry-guard-slow-ema-span-candles 1440
```

The guard allows a newly crossed empty buy slot only when the fast EMA through the previous candle's close is at least the slow EMA through that same close. It never blocks exits, changes order size, or releases reserve. With `--start`, the runner loads up to the final slow-span rows before the window as signal-only pre-roll; those rows cannot trade, and fewer than a full span leaves the guard in fail-closed warmup. Trading and pre-roll selected-row hashes and derived-source provenance are recorded separately. Missing minutes fail closed for the first post-gap decision, while a gap at least as long as the slow span resets the signal and requires a full new warmup. Use one-minute candles only.

The frozen 360/1,440 experiment passed all pre-registered June 9-July 9, 2026 holdout criteria on both paths: active cost fell by about `$40`, cash-cost-time by about 33.6%, drawdown by about 35.5%, and both equity measures improved slightly. It still ended below `$1,000` cash, and development equity was lower in all four May-August 2025 windows. Keep `--entry-guard none` as the default, do not tune on that holdout, and do not combine the first allocator test with this guard. Exact run IDs and criteria are in `docs/RESERVE_GRIDBOT_RESEARCH.md`.

Run both candle paths for any new profile and freeze one manual fee profile across the comparison. Treat `final_equity_before_rebate` as authoritative because the current rebate model applies one simplified cap across the full run. Utilization, reserve maxima, and drawdown are sampled at candle closes. See `docs/RESERVE_GRIDBOT_RESEARCH.md` for the first verified comparison and known limitations.

Keep overflow at `0` for the default research profile. The first `$100` overflow experiment preserved the base trade fingerprint and completed more cycles, but it also added 20 end-open lots, increased maximum drawdown from about `$11.91` to `$21.61`, and reduced pre-rebate final equity by about `$4.64`. Do not describe overflow as quiet-band lending unless the configured cap is fully covered by initial cash above all base caps; inspect `overflow_cap_fully_covered_by_initial_cash_above_base_caps` in the summary.

## Current Canonical Raw Layout

The hardened logger writes under one explicit root:

```text
$env:MONEYMAN_RAW_ROOT\coinbase_advanced_trade\session=<SESSION_ID>\product=<PRODUCT_ID>\
```

After the move, inventory should run against `C:\Users\doyle\Downloads\MoneyManData\raw`. The old Downloads folders may remain as empty or partially empty shells if non-raw artifacts are left behind.

## Normalization

Run only after inventory succeeds.

Tiny slice:

```powershell
python -m moneyman normalize --raw-root "C:\path\to\selected-session" --derived-root $env:MONEYMAN_DERIVED_ROOT --catalog-root $env:MONEYMAN_CATALOG_ROOT --quarantine-root $env:MONEYMAN_QUARANTINE_ROOT --input-order receive_time --sequence-scope observed --limit-files 6 --max-open-partitions 32
```

Expected behavior:

- reads raw JSONL or JSONL.GZ;
- writes rebuildable `normalization.v2` JSONL partitions for `trades`, `l2_updates`, `quotes`, `candles`, `heartbeats`, `status`, `control`, and `sessions` under `derived/v2/`;
- partitions product data by table/product/date while bounding simultaneously open partition files;
- writes malformed or incomplete records to `quarantine/`;
- writes a quality report and dataset manifest with exact input/session-manifest/source/artifact hashes;
- quarantines malformed product IDs before they can collide in one partition;
- fails closed if input, semantic-row, emitted-row, quarantine, or artifact-row reconciliation is nonzero, if the normalizer or bound collector reports disqualifying sequence evidence, or if manifest counts or paths are malformed; and
- proves raw bytes, sizes, and modification times stayed unchanged.

`--sequence-scope complete` is accepted only for receive-time replay of the exact file set attested by a currently valid hardened collector manifest. Bounds such as `--limit-records-per-file` and `--max-records` force an observed-only claim. Historical selections without the new collector contract must use `observed` and report gaps rather than claiming connection completeness.

Audit an existing dataset independently:

```powershell
python -m moneyman audit-normalization --manifest "$env:MONEYMAN_DERIVED_ROOT\v2\normalization_datasets\<DATASET_ID>\manifest.json"
```

The v2 schema preserves source numeric strings and currently validates shape, required fields, explicit UTC timestamps, provenance, duplication, partitioning, and conservation. Numeric finiteness/sign rules, OHLC relationships, and bid/ask relationship checks remain an optional later semantic-certification gate.

Local frozen proofs: complete-session dataset `712a360e77c76b63686731d4` contains 271 canonical envelopes, 16,351 semantic rows plus one session row, zero quarantine or sequence defects, six zero reconciliations, and a successful collector re-audit. Historical observed-only dataset `7b46883be85803f8c453d7ee` contains 35,574 canonical envelopes, 344,664 semantic rows plus one session row, zero quarantine or reconciliation errors, and one 53-number sequence gap that correctly keeps the complete claim false. Both pass `audit-normalization` with no warnings and unchanged raw/source evidence. Reproduce the bounded observed workflow from a clean checkout on the other computer before calling the research-MVP portable.

## Normalization Quality Report And Exploratory Features

```powershell
python -m moneyman features --derived-root $env:MONEYMAN_DERIVED_ROOT --catalog-root $env:MONEYMAN_CATALOG_ROOT --product BTC-USD --normalization-dataset-id <DATASET_ID>
```

The normalization quality report and manifest include:

- row counts by table;
- time coverage;
- product/channel coverage;
- null rates;
- sequence gaps;
- duplicate counts;
- reconnect windows;
- latency distribution when receive and exchange times exist;
- quarantine counts.

The separate `features` command adds exploratory midpoint, spread, relative spread, book imbalance, and trade imbalance rows when available. It is not a substitute for strict book reconstruction or numeric semantic certification.

## ClusterLOB Boundary And Exploratory Feature Path

ClusterLOB-style research is a later lane. LOB means limit order book. The non-ML structural foundation exists; an authoritative exploratory feature run names one audited V2 dataset and product:

```powershell
python -m moneyman features --derived-root $env:MONEYMAN_DERIVED_ROOT --catalog-root $env:MONEYMAN_CATALOG_ROOT --product BTC-USD --normalization-dataset-id <DATASET_ID>
```

Current exploratory outputs:

- midpoint;
- spread;
- relative spread;
- book imbalance;
- trade imbalance;
- feature coverage report.

Do not add clustering, participant labels, spoofing claims, or live trading merely because these exploratory rows exist.

## Planned Real-Time Feature Path

MoneyMan should be shaped for real-time observation, even if the first implementation is tested with archived files.

Planned design:

```text
archived JSONL replay or live WebSocket message
    -> raw event reader
    -> normalizer
    -> rolling feature calculator
    -> append-only feature rows and quality warnings
```

The same feature code should work for backfill/replay and future live windows. A live feature process may warn about spread, imbalance, stale data, or invalid book windows, but it must not place trades.

## Hardened Logger Recovered From The Roadmap Branch

The prototype from `origin/codex/add-coinbase-logger-roadmap` was recovered as `coinbase_ws_stable_logger.py` and is now the hardened V1 collector using `MONEYMAN_RAW_ROOT`.

After recovering it and installing dependencies:

```powershell
$env:MONEYMAN_RAW_ROOT = "C:\Users\doyle\Downloads\MoneyManData\raw"
python coinbase_ws_stable_logger.py
```

If launching from the user's normal PowerShell where `py` works:

```powershell
$env:MONEYMAN_RAW_ROOT = "C:\Users\doyle\Downloads\MoneyManData\raw"
py coinbase_ws_stable_logger.py
```

The logger should print the raw root and session path at startup, then print periodic `Logged ... messages` and `Rolled ...` lines. If it only prints `Subscribed` and appears quiet, check whether an older running process is still using the pre-progress logger code. Current files can also be checked with:

Logger output lines include a UTC timestamp, for example:

```text
[2026-07-09 02:16:18 UTC] Logged 5,000 messages (...)
```

```powershell
python -m moneyman read-check "<current product jsonl.gz>" --sample-records 1 --scan-all
```

Open gzip files may report an end-of-stream warning while the logger still has them open. If records and current timestamps are present, that warning usually means "active file is not closed yet," not data loss.

The V1 collector audit contract now covers exclusive confined session creation, strict route names, exact closed-file evidence, routing replicas, connection-scoped sequence/duplicate findings, heartbeat intervals/counters/timeouts, reconnect records, fixed-histogram latency, project-config and installed-`websockets` provenance, and a separate read-only auditor that independently derives expected destinations. Malformed manifest counts or paths fail closed. Public session `20260723T021818Z-d5a6ccbfc0a5` passed the frozen contract with 271 frames/envelopes/routed rows, four verified files, one connection, 18 heartbeats, no parse/sequence/duplicate/regression/reconnect/stale-heartbeat/audit defects, and `warnings=[]`. Its histogram retained 225 negative latency samples down to `-2.711` ms as clock-offset evidence; these were recorded, not hidden. The full suite passes 160/160 tests, and all protected strategy/book sources are unchanged. A deliberately interrupted long-duration reconnect soak remains useful later operational validation. Keep `ticker` versus `ticker_batch` an explicit config choice.

## Web GUI Rule

Do not start a separate visible MoneyMan web GUI server. Future UI should be mounted into Codex Word Game / Codex Study Hub, ideally as routes such as:

```text
/moneyman
/moneyman/data
/moneyman/gridbots
/moneyman/reports
```

The user should still open one hub URL, normally on port `8000`.

## Verification Commands

Use these when files exist:

```powershell
python -m py_compile <changed-python-files>
python -m unittest discover -s tests -v
git diff --check
git status --short
```

For documentation-only changes, at minimum run:

```powershell
git diff --check
git status --short
```
