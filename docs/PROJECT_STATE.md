# MoneyMan Project State

Last updated: 2026-07-22

## Current Reality

- `main` contains the recovered data pipeline, audited collector/normalizer contracts, research baselines, planning, and safety guardrails.
- `origin/codex/add-coinbase-logger-roadmap` remains useful branch history and was used to recover:
  - the Coinbase WebSocket logger concept;
  - `requirements.txt`;
  - `.gitignore` raw-data protections;
  - README operating details; and
  - `docs/ROADMAP.md`.
- `coinbase_ws_stable_logger.py` writes future sessions under an exclusive, confined `MONEYMAN_RAW_ROOT\coinbase_advanced_trade\session=<SESSION_ID>\...` directory and closes with code/config/host provenance, including project configuration and the installed `websockets` version, per-connection quality findings, and exact immutable-file evidence. Routes use strict names; `audit-collector-session` independently derives expected destinations and replays the closed files before a session can support a complete claim. Malformed manifest counts or paths fail closed.
- `python -m moneyman inventory` can scan raw JSONL/JSONL.GZ files read-only and write a catalog manifest.
- `python -m moneyman normalize` now writes audited `normalization.v2` table/product/date JSONL partitions for trades, L2 updates, quotes, candles, heartbeats, status, control, and session provenance. It binds raw/session/source/artifact hashes, quarantines malformed product IDs before partitioning, and fails closed on its own or collector sequence evidence, malformed manifest counts or paths, and six count-reconciliation identities.
- `python -m moneyman audit-normalization` independently rehashes/recounts one saved dataset and reruns the collector audit behind any complete-session claim.
- Local collector/normalization proof is complete. Public session `20260723T021818Z-d5a6ccbfc0a5` closed four immutable files with 271 frames/envelopes/routed rows, one connection, 18 heartbeats, and zero parse, sequence, duplicate, regression, reconnect, stale-heartbeat, or audit defects. Its public audit returned `valid=true` with `warnings=[]`. The latency histogram preserved 225 negative samples with a minimum of `-2.711` ms as clock-offset evidence rather than silently dropping them.
- Complete-session dataset `712a360e77c76b63686731d4` normalized those exact four files from 271 canonical envelopes into 16,351 semantic rows plus one session row. It had zero quarantine, zero sequence defects, all six count reconciliations at zero, unchanged raw/session/source hashes, and a valid public audit that reran the collector audit.
- Historical multi-product dataset `7b46883be85803f8c453d7ee` normalized six exact files from 35,574 canonical envelopes into 344,664 semantic rows plus one session row with zero quarantine and zero reconciliation errors. Its one observed gap covering 53 missing sequence numbers correctly kept `connection_complete_claim=false`. Its public audit was valid with no warnings, and all selected raw/session/source hashes remained unchanged.
- The current foundation regression suite passes 160/160 tests across reconstruction, collector auditing, strict fills, normalization, pipeline safety, reserve accounting, and EMA behavior. The four protected strategy/book sources are byte-identical to their pre-work hashes; no reserve, overflow, EMA, sizing, allocation, exit, compounding, handoff, reconstruction, or live-trading behavior changed.
- `python -m moneyman features` can calculate exploratory non-ML midpoint, spread, relative spread, book imbalance, and trade imbalance rows from one audited v2 dataset and one explicitly selected product. These features remain exploratory rather than a strict reconstructed-book substitute.
- `normalization.v2` currently certifies shape, timestamps, provenance, duplicate policy, partitioning, and conservation while preserving Coinbase numeric text. Finiteness/sign rules and OHLC/BBO relationship validation remain an optional later semantic-certification task before model-ready claims; they are not the next required milestone.
- A large raw Coinbase level 2 archive exists under `C:\Users\doyle\Downloads\MoneyManData\raw` on this computer. Another computer may also hold captures, but this checkout can inspect the local centralized archive directly.
- Local legacy logger-created folders were found in `C:\Users\doyle\Downloads`, including `btc-usd_ws_data`, `eth-usd_ws_data`, `hbar-usd_ws_data`, `sol-usd_ws_data`, `xlm-usd_ws_data`, `xrp-usd_ws_data`, and `xrp_ws_data`.
- The local legacy Downloads `*_ws_data` folders have been moved into `C:\Users\doyle\Downloads\MoneyManData\raw\legacy_ws_data`. The move did not create a second terabyte-scale copy.
- The old top-level Downloads `*_ws_data` folders are gone. The previous old Coinbase logger process was stopped before moving the locked BTC/ETH/XRP folders.
- A bounded centralized inventory was run with `--max-files 100`; it wrote `C:\Users\doyle\Downloads\MoneyManData\catalog\inventory\inventory_20260708T231546Z.json`.
- A tiny normalization slice was run with `--limit-files 2`; it produced 4,152 trades, 208,328 L2 updates, and zero quarantined rows for XRP-USD.
- The first optimized feature slice produced 203,856 XRP-USD microstructure feature rows.
- A follow-up bounded BTC-USD normalization slice produced 279,546 L2 updates, zero quarantined rows, and no trades in that file. The matching feature run produced 252,351 BTC-USD feature rows.
- Central raw storage audit by extension:
  - `.feather`: 132,314 files, 663,460,016,876 bytes;
  - `.parquet`: 132,314 files, 265,100,026,183 bytes;
  - `.jsonl.gz`: 143,456 files, 209,870,246,422 bytes;
  - `.jsonl`: 166 files, 5,231,227,537 bytes;
  - small `.csv`, `.py`, and `.txt` leftovers.
- Space recommendation: keep raw `.jsonl`/`.jsonl.gz` as source of truth. Treat `.feather` and `.parquet` as likely rebuildable legacy derived snapshots, but do not delete them until a coverage audit confirms the raw files cover the same sessions/time windows.
- The old Downloads logger was checked: it wrote `.jsonl.gz`, `.parquet`, and `.feather` from the same rolling buffer. The repo logger now writes only `.jsonl.gz` raw captures.
- Storage audit report `C:\Users\doyle\Downloads\MoneyManData\catalog\storage\storage_audit_20260709T000216Z.json` found:
  - `.feather`: 132,314 files, 663,460,016,876 bytes;
  - `.parquet`: 132,314 files, 265,100,026,183 bytes;
  - same-stem raw coverage for `.feather` and `.parquet`: 126,881 files each;
  - same-stem raw missing for `.feather` and `.parquet`: 5,433 files each, likely roll-timestamp/session-start edge cases from the old logger.
- `python -m moneyman read-check` now probes one or a few files and reports first/last records, row counts, parse errors, dependency errors, and same-stem comparisons without moving or converting data.
- `python -m moneyman legacy-coverage` now audits old Parquet/Feather files against raw JSONL/GZ candidates, including the previous-window match created by the old stable logger's roll-timestamp behavior.
- Legacy coverage report `C:\Users\doyle\Downloads\MoneyManData\catalog\coverage\legacy_coverage_20260709T015635Z.json` found:
  - raw JSONL/JSONL.GZ files: 143,607;
  - legacy derived files: 264,628;
  - `.feather`: 132,314 files, 663,460,016,876 bytes;
  - `.parquet`: 132,314 files, 265,100,026,183 bytes;
  - previous-window raw candidates: 126,824 `.feather` files and 126,824 `.parquet` files;
  - exact-or-previous raw candidates: 127,106 `.feather` files and 127,106 `.parquet` files;
  - same-directory raw candidates only: 5,207 `.feather` files and 5,207 `.parquet` files;
  - no raw candidate: 1 `.feather` file and 1 `.parquet` file, both old `output\XRP_1m_candles` artifacts inside a copied XRP folder.
- Feather cleanup plan `C:\Users\doyle\Downloads\MoneyManData\catalog\cleanup\feather_cleanup_20260709T031117Z.jsonl` found 132,313 eligible `.feather` files with raw candidates and 1 skipped `.feather` file with no raw candidate.
- Feather cleanup delete run `C:\Users\doyle\Downloads\MoneyManData\catalog\cleanup\feather_cleanup_20260709T031649Z.jsonl` deleted 132,313 eligible `.feather` files totaling 663,459,967,538 bytes, with zero failures. One 49,338 byte Feather file with no raw candidate remains.
- Filename-time coverage by product:
  - BTC-USD raw range `2025-08-02_23-11` through `2026-07-08_23-02`, 47,803 raw files;
  - ETH-USD raw range `2025-08-02_23-11` through `2026-07-08_23-02`, 47,788 raw files;
  - XRP-USD raw range `2025-08-02_23-11` through `2026-07-08_23-02`, 47,862 raw files;
  - older XRP raw range `2025-08-01_21-21` through `2025-08-02_22-08`, 141 raw files;
  - SOL-USD, XLM-USD, and HBAR-USD have small partial captures.
- File-read diagnosis:
  - plain `python` is not on PATH in this Codex shell;
  - the bundled Codex Python can read `.jsonl` and `.jsonl.gz`, but lacks `pyarrow`, so it cannot read Parquet or Feather;
  - `C:\Users\doyle\AppData\Local\Programs\Python\Python313\python.exe` has pandas 2.3.1 and pyarrow 21.0.0 and can read the legacy Parquet/Feather files.
- Bounded read checks succeeded:
  - BTC raw gzip `btc_usd_2025-08-14_02-52.jsonl.gz`: 16,014 JSON records, zero parse errors, receive range about `2025-08-14T02:52:40Z` to `2025-08-14T03:02:40Z`;
  - SOL plain JSONL `sol_usd_2025-08-14_02-42.jsonl`: 25 JSON records, zero parse errors.
- Important legacy naming finding: the old stable logger wrote Parquet/Feather with the roll timestamp. For example, `btc_usd_2025-08-14_02-52.parquet` and `.feather` match the prior raw window `btc_usd_2025-08-14_02-42.jsonl.gz`, not the same-stem `02-52.jsonl.gz`.
- Current cleanup recommendation: do not remove raw `.jsonl`/`.jsonl.gz`. Do not delete `.feather` or `.parquet` based only on same-stem filename coverage. First run a shifted-window coverage check or several product/session read checks proving raw files cover the derived file windows.
- Live logger diagnosis on 2026-07-08 local time:
  - the user-started logger process was running;
  - it was writing current JSONL.GZ files under `C:\Users\doyle\Downloads\moneyman\data\raw` because `MONEYMAN_RAW_ROOT` was not set in that terminal;
  - current files contained live records through about `2026-07-09T01:42:59Z`;
  - reading an open gzip file can show an end-of-stream warning until the logger closes or rolls the file.
- The logger has been patched so the next start prints the raw root, session path, roll messages, and periodic progress counts. If `MONEYMAN_RAW_ROOT` is not set but `~/Downloads/MoneyManData/raw` exists, it now uses that central raw root instead of falling back immediately to repo-local `data/raw`.
- Logger console output now includes UTC timestamps on every logger line.
- The closed stranded repo-local session `session=20260708T233816Z` was moved into `C:\Users\doyle\Downloads\MoneyManData\raw\coinbase_advanced_trade\session=20260708T233816Z` on 2026-07-09. The source folder is gone, the destination contains 71 files totaling 68,980,400 bytes, and the move manifest is `C:\Users\doyle\Downloads\MoneyManData\catalog\relocate\move_stranded_sessions_20260709T025219Z.jsonl`.
- `python -m moneyman move-stranded-sessions` can move closed repo-local logger sessions from `.\data\raw\coinbase_advanced_trade\session=...` into the canonical raw root. It writes a manifest under `catalog\relocate\`, skips sessions that still look open by default, and uses a true same-drive move rather than copy or hardlink.
- `python -m moneyman raw-gaps` now finds likely missing raw capture windows:
  - `--mode filename` uses expected roll windows from raw filenames and is fast enough for full legacy scans;
  - `--mode content` reads JSONL/JSONL.GZ records and writes true per-file first/last timestamp boundaries, but full-archive content mode will be much slower.
- Fast raw filename gap report `C:\Users\doyle\Downloads\MoneyManData\catalog\gaps\raw_gaps_filename_20260709T021414Z.json` found 143,607 raw files and 353 gaps across legacy raw data. Main product summaries:
  - BTC-USD: 47,803 raw intervals, 115 gaps, largest gap `2025-09-20T04:37:00Z` to `2025-10-09T19:00:00Z`;
  - ETH-USD: 47,788 raw intervals, 116 gaps, largest gap `2025-09-20T04:37:00Z` to `2025-10-09T19:00:00Z`;
  - XRP-USD: 47,862 raw intervals, 117 gaps, largest gap `2025-09-20T04:37:00Z` to `2025-10-09T19:00:00Z`;
  - smaller partial products also have expected partial coverage, for example SOL-USD has 6 raw intervals and 2 gaps.
- Content raw gap proof run for SOL-USD wrote `C:\Users\doyle\Downloads\MoneyManData\catalog\gaps\raw_gaps_content_20260709T021618Z.json` and `C:\Users\doyle\Downloads\MoneyManData\catalog\gaps\raw_file_boundaries_20260709T021618Z.jsonl`.
- Older Downloads backtest files found:
  - `C:\Users\doyle\Downloads\XRP_1m_from_election.csv`, one-minute XRP OHLCV starting at `2020-11-05T00:00:00Z`;
  - `C:\Users\doyle\Downloads\XRP_1m_from_election_cc.csv`, one-minute XRP OHLCV starting at `2025-07-28T00:55:00Z`;
  - old scripts such as `backtesterverify.py`, `fromal2.py`, `morecandles.py`, and `moremorecandles.py`.
- The old backtest script `backtesterverify.py` used OHLC candles and EMA/SMA-style rules with Optuna, Hyperopt, and Scikit-Optimize. It was price/candle based, not L2-depth based.
- `fromal2.py` built one-minute XRP candles from local market-trade JSONL/GZ files, while `morecandles.py` and `moremorecandles.py` fetched outside one-minute OHLCV. These can support rough price-only baselines, but they cannot patch missing L2 order-book depth, spread, or queue information. Missing L2 windows must be labeled and skipped for L2-dependent simulations unless true external L2 data is obtained.
- `python -m moneyman import-candles` now imports OHLCV CSV files into `derived\v1\candles_fallback` with `source_kind=price_only_fallback`.
- Imported XRP fallback candles:
  - `C:\Users\doyle\Downloads\MoneyManData\derived\v1\candles_fallback\part_20260709T162228Z.jsonl`, provider `coinbase_ccxt`, 109,037 rows, range `2020-11-05T00:00:00Z` through `2021-01-19T18:04:00Z`;
  - `C:\Users\doyle\Downloads\MoneyManData\derived\v1\candles_fallback\part_20260709T162335Z.jsonl`, provider `cryptocompare`, 10,081 rows, range `2025-07-28T00:55:00Z` through `2025-08-04T00:55:00Z`.
- The original local OHLCV CSV files were moved out of Downloads and into the central raw external OHLCV shelf:
  - `C:\Users\doyle\Downloads\MoneyManData\raw\external_ohlcv\product=XRP-USD\provider=coinbase_ccxt\XRP_1m_from_election.csv`;
  - `C:\Users\doyle\Downloads\MoneyManData\raw\external_ohlcv\product=XRP-USD\provider=cryptocompare\XRP_1m_from_election_cc.csv`.
- The two stale pre-move generated fallback files that pointed at the old Downloads CSV paths were removed after re-importing from the central raw paths. Raw CSV source files were not deleted.
- `python -m moneyman move-external-ohlcv` now moves external OHLCV source files into `raw\external_ohlcv` and writes a manifest under `catalog\external_ohlcv`.
- `python -m moneyman fetch-candles` now fetches Coinbase Exchange public historical candles in 300-candle chunks, writes raw external OHLCV JSONL under `raw\external_ohlcv`, and writes matching `price_only_fallback` rows under `derived\v1\candles_fallback`.
- The large BTC/ETH/XRP `2025-09-20T04:37:00Z` through `2025-10-09T19:00:00Z` L2 gap now has a candle fallback patch:
  - XRP-USD: 28,223 one-minute candles, zero missing buckets, zero request errors, raw output `C:\Users\doyle\Downloads\MoneyManData\raw\external_ohlcv\product=XRP-USD\provider=coinbase_exchange_public\granularity=60\part_20260709T161654Z.jsonl`;
  - BTC-USD: 28,223 one-minute candles, zero missing buckets, zero request errors, raw output `C:\Users\doyle\Downloads\MoneyManData\raw\external_ohlcv\product=BTC-USD\provider=coinbase_exchange_public\granularity=60\part_20260709T161741Z.jsonl`;
  - ETH-USD: 28,223 one-minute candles, zero missing buckets, zero request errors, raw output `C:\Users\doyle\Downloads\MoneyManData\raw\external_ohlcv\product=ETH-USD\provider=coinbase_exchange_public\granularity=60\part_20260709T161832Z.jsonl`.
- The fetched candle patch is good for contiguous price-path/grid-touch tests only. It still cannot replace missing L2 spread, book depth, queue, partial-fill, or imbalance history.
- `python -m moneyman gridbot-backtest` now runs the first inventory-aware gridbot simulator. With `--include-fallback-candles`, it reads `derived\v1\candles_fallback` and writes `config.json`, `fills.jsonl`, `equity_curve.jsonl`, and `summary.json` under `derived\v1\backtests\gridbot\<RUN_ID>`.
- `python -m moneyman fee-profile --source auto` shows the fee profile MoneyMan will use without placing trades. It can pull current Coinbase Advanced maker/taker rates from the authenticated transaction summary endpoint when local env credentials are available, or fall back to manual rates with a warning.
- The gridbot summary reports filled buys, filled sells, missed buys from insufficient quote, missed sells from insufficient base, gross fees, Coinbase One Advanced rebates, net fees, turnover, final equity, no-trade equity, buy-and-hold equity, and max drawdown.
- The committed example Coinbase One Advanced scenario models a 25% rebate on spot fees up to 100 USDC per month; the values can be overridden or disabled. Fallback-candle mode assumes maker fills by default, because candle data cannot prove actual maker/taker status.
- Without `--include-fallback-candles`, `gridbot-backtest` stays in strict L2 mode and audits reconstruction manifests. With no contract it returns `requires_book_snapshots`; with no audited eligible window it returns `requires_valid_book_snapshots`; ambiguous eligible inputs require explicit `--l2-run-id` or `--l2-window-id`. A selected eligible window now runs the conservative V1 fill model.
- `python -m moneyman gridbot-reserve-backtest` now provides a separate XRP-only banded-lot research engine at v1.4. It uses one shared cash balance, half-open bands, fixed all-in level budgets, per-band ceilings on base-tranche cash still awaiting recovery, independently tracked purchase lots, fee-aware principal recovery, positive cash-flow profit, and residual reserve coin tagged to the originating lot, band, and capital tranche.
- V1.3 writes `lot_diagnostics.jsonl` separately from the accounting ledger. It measures exact 1-hour/6-hour/24-hour/7-day price-only close markouts, 7/14/28-day recovery cohorts, close-sampled and assumed-path adverse excursion, right-censored open duration, and actual all-in cash-cost-time. A separate event-stream integral must reconcile cash-cost-time to zero. Diagnostic observers are isolated from strategy state, are never read by trading decisions, and are finalized after replay.
- The reserve engine traverses every recorded previous-close-to-open gap before its low-first/high-first candle assumption, rejects invalid price-tick grids and nonchronological/mismatched candle rows, permanently disables infeasible slots after one attempt, and records collision-resistant run IDs plus exact selected-row, input-file, config, engine-source, and candle-loader-source hashes.
- `--overflow-global-active-lot-budget-cap` optionally gives every price level one duplicate overflow tranche governed by one global unrecovered-cost ceiling. Overflow and base spend the same quote wallet, base receives tie priority, overflow does not consume the per-band base cap, and reserve from either layer remains nonspendable. The default is `0`.
- Reserve-grid v1 does not spend modeled Coinbase One rebates, compound level sizes, release reserve, implement band handoffs, use L2 signals, optimize parameters, or place orders. The original pooled `gridbot-backtest` remains unchanged.
- The first coverage-selected XRP experiment used 28,223 complete Coinbase-public one-minute candles from `2025-09-20T04:37:00Z` through `2025-10-09T19:00:00Z`, three `$0.20` bands from `$2.60` through `$3.20`, twenty `$5` all-in levels per band, a `$100` active-lot cap per band, `$1,000` shared cash, 0.60% gross maker fees, and a 20-basis-point cash-profit target.
- In the corrected 5% principal-recovery run, 42 lots completed, 2.545608 XRP became tagged reserve, 33 lots remained open and unrecovered, and final equity was `$998.1351` before rebate and `$999.0118` after the simplified modeled rebate. Cash, base, PnL, fee, rebate, turnover, cash-profit, and band reconciliation errors were zero. The same-engine, same-path full-lot control finished at `$998.4912` before rebate, showing that reserve was continuing XRP exposure rather than free value.
- Controlled 3%, 4%, and 5% exit comparisons found 69, 50, and 42 completed lots respectively. More frequent smaller exits paid more fees and did not improve final equity in this bounded falling window. These results do not establish strategy profitability.
- The bounded `$100` overflow experiment held the base trade fingerprint fixed. It added 18 completed and 20 end-open lots, raised maximum active cost from about `$165` to `$265`, raised gross fees from `$3.51` to `$5.18`, increased close-sampled maximum drawdown from `$11.91` to `$21.61`, and reduced pre-rebate final equity from `$998.1351` to `$993.4949`. The full-lot overflow control also finished about `$4.41` below fixed full-lot, so extra falling-market exposure—not reserve alone—caused the degradation. All 38 conservation and cap checks were zero.
- The v1.3 diagnostic replay of the fixed 5% profile used the same 28,223 candles and preserved both historical base decision fingerprints (`9cb35b...` low-first and `a0419d...` high-first), 42 completed lots, 33 end-open lots, and `$998.1351` pre-rebate final equity. The input declared a one-minute timeframe, had a 60-second maximum observed gap, and had zero diagnostic coverage gaps.
- Of 53 lots with a complete 7-day follow-up, 31 recovered within 7 days and 22 did not, a 58.49% observed-cohort recovery rate. Of 46 lots with complete 14-day follow-up, 40 recovered and 6 did not, an 86.96% rate. The run is shorter than 28 days, so it has no eligible 28-day cohort and cannot estimate that rate.
- Across all 75 fixed-profile lots, average assumed-path adverse excursion was about 487.18 basis points and average close-sampled adverse excursion was about 458.98 basis points. Observed unrecovered cash-cost-time totaled about 2,072.60 quote-currency days and reconciled exactly to the chronological event stream. These are diagnostics for one falling window, not evidence that the strategy is profitable or that the same recovery rates hold elsewhere.
- A new Coinbase-public XRP fallback file covers `2025-05-01T00:00:00Z` through `2025-09-20T04:36:00Z` with 204,755 of 204,757 expected one-minute candles, zero request errors, and two explicit missing August buckets. It is a price-only source and does not replace L2.
- The fixed 5% profile was validated over non-overlapping May, June, July, and August 2025 calendar windows, with each three-band range frozen from its first candle and overflow held at zero. Sixteen reserve/full-lot and low/high path runs completed with every conservation and cap error at zero.
- The July range `[2.00,2.60)` sold out before XRP reached `$3.6662`: it ended with zero active cost and `$1,010.48` pre-rebate equity, but missed most of the upside versus `$1,351.16` idealized fee-free buy-and-hold. A fixed range avoids chasing a breakout but does not solve higher-band handoff.
- A fresh August range `[2.80,3.40)` exposed the transition risk. The low-first reserve run ended with 59 open lots, `$295` unrecovered cost, `-$30.78` open-lot unrealized PnL, `$32.23` maximum close-sampled drawdown, and `$985.29` pre-rebate equity. Its 14-day eligible cohort recovered only 4 of 23 lots. Overflow was already disabled.
- Reserve-minus-full-lot pre-rebate equity was `-$0.67`, `+$0.33`, `+$1.86`, and `-$1.73` in May through August respectively. Reserve helped when retained XRP appreciated and hurt when it fell; it is not a downtrend defense.
- Overflow remains disabled by default. Do not proceed to borrowing quiet-band allocations from this result; the surplus-only version already failed this bounded window.
- V1.4 adds the optional `ema_cross` entry guard, with `none` as the default. The frozen experiment uses prior-close-only EMA360/EMA1440 state and exactly 1,440 signal-only pre-roll candles. It can block newly crossed empty buy slots, but cannot block exits, resize lots, change caps, release reserve, or affect guard-off output. Trading and pre-roll sources and selected-row hashes are recorded separately.
- Eight new guard-off May-August 2025 runs reproduced the corresponding v1.3 decision fingerprints and financial results exactly. The guarded development runs reduced cash-cost-time and drawdown in all four windows but also reduced marked equity by `$1.48` to `$7.81`, demonstrating opportunity cost rather than a universal improvement.
- The pre-registered June 9-July 9, 2026 holdout passed every frozen criterion under both low-first and high-first paths. The guard reduced end active cost from about `$115` to `$75`, cash-cost-time by about 33.6%, drawdown by about 35.5%, end-open lots from 23 to 15, and fees by about `$0.54`; marked equity improved by about `$0.31` and estimated-liquidation equity by about `$0.55`. All accounting, cap, diagnostic, and guard reconciliations were zero.
- The holdout remains price-only evidence and every variant finished below the original `$1,000` cash before rebate. The guard remains disabled by default, its spans must not be tuned on that holdout, and the protected/flexible allocator remains a later separate experiment.
- `python -m moneyman reconstruct-book` now replays every top-level Coinbase envelope in deterministic order while mutating only target `l2_data` envelopes atomically. It audits connection-global sequences, gaps, duplicates, reconnects, timestamps, snapshot recovery, product routing, full-book invariants, raw-source stability, and output provenance.
- The hardened frozen XRP run `20260721T235740933455Z-0760dcde` replayed two ordered legacy roll files plus a non-replayed right-boundary sentinel. It produced one strict eligible window and 19,051 book-state rows from 26,236 envelopes and 208,328 mutations. Sequence numbers were exactly 0 through 26234, with one preserved unsequenced authentication error and zero gaps, duplicates, regressions, malformed target L2 envelopes, empty books, locked books, or crossed books.
- Independent canonical full-book hashes matched at the initial snapshot (`0:6`), both sides of the file rollover (`0:13632` and `0:13633`), and the final L2 state (`0:26232`). Final BBO was 3 / 3.0004. The right-boundary file began at sequence 26235, all raw hashes were unchanged after replay, and the consumer audit returned `valid=true`, `strict_l2_eligible=true`, and no errors.
- The untouched second reconstruction `20260722T032803947186Z-9593d3b9` used a different legacy logger session. It replayed 35,435 envelopes with exact sequences 0 through 35433, verified the next roll at 35434, and produced one eligible window with 22,778 emitted states and 450,728 mutations. Raw, artifact, checkpoint, engine, and run fingerprints passed a fresh zero-error audit. The two eligible runs now contain 41,829 book states, but they remain two bounded prefixes rather than archive-wide coverage.
- The old `microstructure_features` output remains exploratory row-exploded update processing; it is not a strict reconstructed-book contract. Strict consumers use only audited `derived\v1\book_reconstruction\<RUN_ID>` manifests and rows.
- Minimal valid-window L2 reconstruction and the first isolated strict fill model are complete for two independent bounded windows. `moneyman/l2_fills.py` uses an explicit message or receive clock, deterministic latency, taker spread crossing on arrival, strict price-through for passive maker fills, persistent emitted-depth shadow accounting with delta-only replenishment, one-shot partial fill with remainder cancellation, dynamic maker/taker fees, inventory checks, and exact cash/base/fee/turnover reconciliation. It never consumes full-depth aggregates or crosses a validity-window boundary. The consumer rediscovers and audits rather than trusting a caller's stale report, binds manifest/artifact bytes across consumption, and fingerprints exact sources, config, selected rows, and outputs.
- The frozen strict run `20260722T024413Z` consumed 18,067 audited rows from `20260721T235740933455Z-0760dcde/window-000001` over `[2025-08-01T21:22:00Z, 2025-08-01T21:41:00Z)`. At 100 ms latency it completed 68 full maker orders, produced no real-window taker or partial fill, turned over `$1,700`, paid `$10.20` gross fees, rejected five sells for insufficient base, and ended at `$992.1998` marked equity after the simplified rebate. Quote, base, fee, and turnover errors were zero.
- Same-grid Coinbase-public price-only controls completed 72 low-first touches (`20260722T024444Z`) and 70 high-first touches (`20260722T024501Z`). All three modes independently selected grid index 14, and the candle controls share the exact selected-row hash. Their markedly different ending cash/XRP mixes show why candle path touches are not execution evidence. Every mode remained below the `$1,000` no-trade cash baseline. This first 100 ms slice did not exhibit taker crossing or visible-depth partials, so its absent paths remain fixture evidence.
- The pre-registered second strict replay `20260722T033005Z` consumed 21,613 rows over `[2025-08-02T15:59:00Z, 2025-08-02T16:18:00Z)`. It completed 45 full makers (23 buys and 22 sells), no takers or partials, turned over `$1,125`, paid `$6.75` gross fees, rejected seven sells for insufficient base, and ended with `$968.25` plus `8.905831` XRP. All four reconciliation errors were zero and every artifact fingerprint verified.
- Its identically configured controls completed only 22 low-first touches (`20260722T033049Z`) and 28 high-first touches (`20260722T033102Z`) from the same 19 candles and exact selected-row hash. All three again chose grid index 14. Because strict was below both controls in the first window but above both in the second, price-only touch replay is neither an upper nor lower execution bound. Both 100 ms comparison slices had zero taker and partial fills. Every mode again finished below no-trade cash; these are mechanics results, not profitability evidence.
- The pre-registered full-window clock A/B used the same 22,778 states and selected-row hash in `message_ts` run `20260722T120726Z` and `recv_ts` run `20260722T120807Z`; configs differed only at `l2_clock_source`. Clock choice moved 38 of 73 activation rows by -2 through +6 sequences, but all 52 fills occurred at identical sequences with identical depth, fees, balances, and equity. Both produced 27 buys, 25 sells, zero takers or partials, `$1,300` turnover, and exact zero reconciliation errors. This is one transport-clock robustness result, not proof clock choice never matters or profitability evidence.
- The sole pre-registered slower replay `20260722T213307Z` changed only `l2_latency_ms`, from 100 to 500, after commit `f0d5594` froze the value and mechanics-only rule. It used the identical 22,778 rows/hash and completed 39 full orders (20 buys/19 sells) plus one partial buy, with 39 maker and one taker execution. Independent replay verified all 60 first-eligible activations, all 40 visible-depth consumptions, the taker at sequence 32, exact-touch rejection and partial/cancellation at sequence 382, nonnegative balances, `$980.6686186690` turnover, `$5.8840117120` gross fees, ending `$963.4473696189` plus `10.9045911455` XRP, verified artifacts, and zero reconciliation errors. Both latency variants remained below cash; this is mechanics sensitivity, not evidence that slower is better.
- The post-stress suite passed all 116 tests unchanged, including spread crossing, shared depth, partial cancellation, latency, strict queue, inventory, fee, and conservation fixtures. No reserve, overflow, EMA, sizing, allocation, exit, compounding, handoff, logger, or live-trading source changed.
- Strict fill assumptions, run IDs, results, and the reproducible command are in `docs/L2_GRIDBOT_FILL_MODEL.md`; reserve-grid evidence remains in `docs/RESERVE_GRIDBOT_RESEARCH.md`.
- The L2/L3 boundary is explicit: current L2 supports visible-book features and the conservative fill model, but it cannot provide L3 market-by-order lifecycles, exact queue position, hidden liquidity, named participant identity, or proof of spoofing.
- `coinbase_ws_stable_logger.py` now supports local `config\logger.json`; `config\logger.example.json` is committed, and the real `config\logger.json` is ignored as machine-local configuration.
- No MoneyMan web GUI exists yet.

## Files Added For Agent Continuity

- `docs/PROJECT_PLAN.md`: human project plan and problem translation.
- `docs/PROJECT_STATE.md`: this current-state snapshot.
- `docs/RUNBOOK.md`: setup and operating commands.
- `docs/DATA_LAYOUT.md`: raw and derived data organization plan.
- `docs/L2_BOOK_RECONSTRUCTION.md`: current book-state contract, two frozen validations, exact commands, and consumer boundary.
- `docs/L2_GRIDBOT_FILL_MODEL.md`: frozen strict execution hypothesis, fixture contract, audited price-touch comparisons, clock sensitivity, and the 500 ms latency stress.
- `docs/CODEX_STUDY_HUB_INTEGRATION.md`: web GUI integration rule.
- `docs/ROADMAP.md`: recovered roadmap content from the Codex branch, reconciled with the main plan.
- `.env.example`: documented environment variables.
- `.gitignore`: protects raw data, derived data, secrets, local databases, and generated artifacts.
- `requirements.txt`: dependency anchor matching the known roadmap branch prototype.
- `moneyman/`: package and CLI skeleton.
- `moneyman/l2_fills.py`: audited-window-only conservative strict grid fill model.
- `coinbase_ws_stable_logger.py`: recovered and hardened raw WebSocket logger.
- `tests/`: tiny fake WebSocket fixtures and focused parser/normalizer tests.

## Non-Negotiable Boundaries

- Do not delete or rewrite raw L2 captures.
- Do not commit raw market data, secrets, local databases, generated Parquet/Feather/DuckDB files, or account exports.
- Do not create a separate visible MoneyMan web server or port. If a web GUI is needed, integrate it into Codex Word Game / Codex Study Hub.
- Do not wire strategies to live Coinbase orders until the user explicitly asks after the research, backtest, risk, and paper-trading path exists.
- Do not implement ClusterLOB-style ML clustering yet. Prepare the raw reader, normalized tables, streaming-friendly feature interfaces, and data-quality reports first.
- Do not claim that L2 data identifies named traders or proves spoofing. The near-term goal is behavior-pattern inference from observed order flow.

## Next Best Move

The next foundational goal is a clean-checkout portability rehearsal on the other computer, not another strict-L2 or strategy replay. From an isolated environment and explicit roots, run read-only inventory, one bounded observed `normalization.v2` slice, `audit-normalization`, and before/after raw hashes. Fix only defects that the rehearsal actually exposes.

After portability passes, do not infer the next research goal from chronology. Ask the user to choose among:

1. numeric semantic certification for normalized price/size/OHLC/BBO fields;
2. a read-only MoneyMan data-quality view inside Codex Study Hub; or
3. one separately pre-registered strategy/reconstruction experiment.

Another XRP reconstruction, another latency point, another EMA holdout, protected/flexible allocation, reserve release, compounding, and band handoffs are optional later experiments, not unfinished prerequisites.

## Open Questions

- What is the top-level path of the raw L2 archive on the other computer?
- Should derived data live next to the raw archive, on an external drive, or on this computer?
- Is the future Codex Study Hub integration expected to live in the existing `CodexWordGame` repository or eventually in a shared monorepo?
- Which product set matters first beyond `XRP-USD`, `BTC-USD`, and `ETH-USD`?
- For real-time monitoring, what rolling windows should matter first: seconds, minutes, or 30-minute research buckets?

## Agent Reminder

Before saying something is impossible or missing, check branches, local files, likely paths, environment variables, and safe samples. The point is to be useful first.
