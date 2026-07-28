# MoneyMan Project Plan

MoneyMan has two jobs at the start:

1. make a large raw Coinbase level 2 archive usable on this computer and another computer; and
2. build a research path toward gridbot-style trading without pretending the first version is ready for live money.

This plan translates the current state, the roadmap branch, and the user's concerns into concrete work.

## Reconciled Research-MVP Finish Line

This plan is the authority for build order and completion gates. `docs/PROJECT_STATE.md` is the concise current-state ledger, the specialist L2 and reserve documents hold experiment evidence, and `docs/RUNBOOK.md` holds commands. Historical experiment chronology must not silently become the next required goal.

The current research MVP is complete when, from a clean checkout and explicit data roots, MoneyMan can take one bounded representative Coinbase session through read-only inventory, independently auditable collector/session provenance, stable normalized product/date tables, quarantine and quality reporting, and an artifact manifest without modifying raw data. The local implementation and evidence gates now pass. The one remaining required exit check is to reproduce the bounded inventory/normalization path from a clean checkout on the other computer. The already validated reconstruction and gridbot engines remain downstream research consumers; paper trading and live execution are not part of this MVP.

| Phase | Current gate | Research-MVP role |
| --- | --- | --- |
| 0. Project recovery | Complete | Required and closed |
| 1. Local data roots/inventory | Locally complete; second-computer portability not yet witnessed | Required local gate closed |
| 2. Collector hardening | V1 contract and fresh independent public-session audit complete locally | Required local gate closed |
| 3. Normalization | Audited multi-channel `normalization.v2` contract complete locally | Required local gate closed |
| 4. Reconstruction | Minimum V1 complete on two independent XRP sessions | Required minimum closed; broader coverage is later validation |
| 5. Gridbot mechanics | Arithmetic price-only and conservative strict-L2 V1 complete | Required minimum closed; new strategy variants are optional research |
| 6. Study Hub dashboard | Not started | Post-MVP product work |
| 7. Paper trading | Not started | Later product stage before any live work |

The research-MVP exit checklist is therefore:

- [x] recover the project and protect raw/generated data;
- [x] centralize and inventory the local archive without rewriting raw captures;
- [x] close and independently audit a collector session with code/host/config provenance, immutable closed-file evidence, connection epochs, sequence/duplicate findings, heartbeat/reconnect findings, and latency summaries;
- [x] normalize bounded multi-channel slices with stable schemas, product/date partitions, explicit timestamp and duplicate policy, quarantine, source/artifact hashes, and count reconciliation;
- [x] verify the bounded artifacts and prove the selected raw files plus all strategy/execution sources are unchanged;
- [x] preserve the passed reconstruction, strict-fill, reserve, overflow, and EMA experiment boundaries.
- [ ] reproduce read-only inventory plus one bounded observed-scope normalization/audit from a clean checkout on the other computer, with before/after raw hashes.

Local proof ledger, frozen 2026-07-22 Chicago time:

- collector session `20260723T021818Z-d5a6ccbfc0a5`: 271 frames/envelopes/routed rows, four immutable files, one connection, 18 heartbeats, 225 preserved negative latency samples with a minimum of `-2.711` ms consistent with clock offset, and zero parse, sequence, duplicate, regression, reconnect, stale-heartbeat, or audit defects; its public audit returned `valid=true` with `warnings=[]`;
- complete-session normalization dataset `712a360e77c76b63686731d4`: 271 canonical envelopes, 16,351 semantic rows plus one session row, zero quarantine, zero sequence defects, all six reconciliation identities zero, exact collector-file claim and collector re-audit passed;
- bounded historical multi-product dataset `7b46883be85803f8c453d7ee`: 35,574 canonical envelopes, 344,664 semantic rows plus one session row, zero quarantine, all six reconciliations zero, and one observed 53-sequence gap correctly preventing a complete-session claim; and
- both public normalization audits returned valid with no warnings; selected raw files, sibling manifests, the normalizer execution bundle, and all protected strategy/book sources remained unchanged. The 160/160 test suite passed.

Another XRP reconstruction, another latency replay, another EMA holdout, protected/flexible allocation, reserve release, compounding, and band handoffs are deliberately later experiments. They are not unfinished prerequisites for this MVP.

Numeric finiteness, sign, OHLC-relationship, and BBO-sanity certification is also an optional later quality lane. It does not replace the one remaining required exit check: the clean-checkout portability rehearsal on the other computer.

## What Is Actually Going On

The project started split across two realities:

- `main` had planning and guardrail files.
- `origin/codex/add-coinbase-logger-roadmap` had the useful early implementation work: a Coinbase WebSocket logger, dependency list, README, and roadmap.

The useful branch work has now been selectively recovered onto `main` without deleting the planning docs.

The data problem is separate from Git. A large raw level 2 archive is now centralized locally under `C:\Users\doyle\Downloads\MoneyManData\raw`; another computer may also hold captures. None of that data belongs in Git. The project needs portable code, docs, and commands that can locate, inventory, normalize, and analyze an explicit archive root wherever it lives.

The trading idea is also separate from the data cleanup. The first serious product target is a 3Commas-like gridbot research lab, but a gridbot needs trustworthy historical data and a backtester before it can be useful.

The web experience should not become a second local app. If MoneyMan needs a GUI, it should live inside Codex Word Game / Codex Study Hub so the user still opens one hub URL and one visible port.

Another research inspiration is ClusterLOB: a limit-order-book paper about clustering individual market-by-order events into behavior groups. The verified reference is `https://arxiv.org/abs/2504.20349`. LOB means limit order book; "Lima Oscar Bravo" is the user's mnemonic for LOB, not an author list. MoneyMan should prepare for that style of research by building clean readers, normalized event tables, market microstructure features, and quality reports. Do not implement clustering yet.

The MoneyMan version should be more real-time oriented than a pure offline paper reproduction. Historical data should train, calibrate, test, and replay ideas. The same readers, normalizers, and feature functions should also be able to run on live Coinbase WebSocket messages in small rolling windows. The near-term goal is not trader identification; it is to infer reusable behavior patterns from observed order flow and then test whether those patterns are useful.

## Problems And Practical Solutions

### 1. The useful branch work has been recovered

Problem: a future agent could still inspect the old branch, switch to it, and think the planning docs disappeared.

Solution:

- Keep `main` as the working source of truth.
- Use `origin/codex/add-coinbase-logger-roadmap` as reference history.
- Preserve the planning docs while continuing to evolve the recovered logger, roadmap, and CLI.

Success check: a fresh clone can find the logger, roadmap, project plan, package CLI, and setup commands from the default branch.

### 2. The raw L2 archive is local, large, and must stay portable

Problem: the data is too large and too important to treat casually. The local captures have been centralized, but they remain hard to query and may be irreplaceable; portability to another computer still matters.

Solution:

- Use explicit environment variables such as `MONEYMAN_DATA_ROOT`, `MONEYMAN_RAW_ROOT`, and `MONEYMAN_DERIVED_ROOT`.
- Build a read-only inventory command before any converter.
- Search for current logger-created folders such as `btc-usd_ws_data/<session>/`, `eth-usd_ws_data/<session>/`, `xrp-usd_ws_data/<session>/`, and the general pattern `*_ws_data/<session>/`.
- Prefer copy-and-index workflows when no storage instruction has been given. If the user explicitly says move, use a move workflow and verify source/destination paths first.
- Define one canonical raw data root and update the logger to write there going forward.
- Write manifests with path, size, modified time, hash when practical, product, channel, session, parse status, and time range.
- Keep raw data out of Git.

Current result: the local archive can be inventoried without changing files. The remaining portability check is to run the same explicit-root workflow on the other computer when that is needed.

### 3. Raw WebSocket messages are not research tables

Problem: Coinbase WebSocket messages are nested event blobs. They are good as a source of truth but painful for analysis. A year of raw JSONL is not automatically usable for gridbots, charts, or statistics.

Solution:

- Keep raw JSONL compressed and immutable.
- Normalize into separate tables:
  - `trades`
  - `quotes`
  - `candles`
  - `l2_updates`
  - `book_snapshots`
  - `heartbeats`
  - `status`
  - `sessions`
- Store the first stable contract as versioned, partitioned JSONL by table, product, and date so it runs in the bundled environment and stays easy to audit. Parquet is a later format optimization after schemas settle and `pyarrow` is available.
- Quarantine malformed rows instead of silently dropping them.

Success check: counts, products, time ranges, null rates, duplicates, and rejected rows are explainable after each run.

### 4. Level 2 data is harder than ticker data

Problem: full L2 means order-book updates, not just prices. If updates arrive out of order, if a sequence gap happens, or if the starting snapshot is wrong, the reconstructed book can become false.

Solution:

- Build book reconstruction as its own tested module.
- Track snapshots, updates, sequence numbers, gaps, duplicates, reconnect windows, and depth limits.
- Start with tiny hand-checked fixtures before processing the full archive.
- Mark book windows invalid when gaps make reconstruction untrustworthy.

Current implementation: `reconstruct-book` now treats Coinbase sequence numbers as connection-global across all channels, applies each complete target L2 envelope atomically, retains the full book behind depth-limited output, and allows only a fresh valid snapshot to recover an invalid window. It hashes stable raw inputs, optional rollover-boundary evidence, every derived artifact, state streams, exact requested full-book checkpoints, and the engine source. Two frozen XRP connection prefixes from independent logger sessions passed the hand-checked contract/integration suite and the consumer audit. See `docs/L2_BOOK_RECONSTRUCTION.md`.

Success check: the system can say "this book state is valid" or "this window is invalid because messages are missing" instead of producing fake precision.

### 5. The collector has a complete local audit contract

Problem addressed: older manifests did not fully prove which code/host/config ran, what happened in each connection epoch, whether sequences or heartbeats failed, or exactly which immutable files closed.

Solution:

- Preserve the existing explicit root, canonical layout, routing, shutdown, and subscription behavior.
- Bind manifests to collector source, Git revision when available, host/runtime, and effective configuration.
- Record connection epochs, reconnect reasons, heartbeats, global sequence gaps/duplicates/regressions, parse errors, and bounded latency distributions.
- Inventory every closed raw file with path confinement, size, SHA-256, row count, and timestamp coverage.
- Provide a read-only independent session-manifest audit instead of trusting the writer's own claim.

Current result: the V1 manifest binds the collector, collector auditor, Coinbase helpers, logger configuration, and project configuration, plus effective config, Git/host/runtime provenance including the installed `websockets` version, connection history, independently replayable sequence/heartbeat/latency summaries, and exact closed-file evidence. Session and roll paths are confined under strict routes. `audit-collector-session` independently derives expected routing destinations, rehashes the files, and re-derives observable claims instead of trusting writer metadata; malformed manifest counts or paths fail closed. The fresh public proof session above passed with no warnings. A deliberately interrupted longer reconnect soak remains useful operational validation, not an unfinished contract requirement.

### 6. The project needs to run on another computer

Problem: even good code is not useful if the other machine cannot reproduce the environment or find the archive.

Solution:

- Keep a simple Python setup path:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
```

- Keep the committed `.env.example` as the portable data-root template.
- Add commands that accept explicit paths instead of assuming local folder names.
- Make the first command a dry-run inventory, not a full conversion.

Success check: the other computer can run inventory, normalization on a tiny slice, and a data-quality report without hand editing code.

### 7. The gridbot concern is real: inventory runs out

Problem: a naive gridbot is often described as "buy when it goes down, sell when it goes up." But that hides the inventory problem. If price drops three grid levels and only buys three units, then rallies eight grid levels, the bot may not own enough base asset to sell at every higher level. It can miss upside sells, or it can only sell what it owns.

Solution:

- Treat gridbots as inventory management, not just signal generation.
- Track base inventory, quote cash, reserved cash, reserved base, open orders, filled orders, and maximum possible future sells.
- Test different launch allocations:
  - all quote, only buying dips;
  - mixed base/quote so the bot can sell immediately if price rises;
  - reserve cash for deeper drawdowns;
  - reserve base for upside moves;
  - asymmetric grids that buy more aggressively than they sell or vice versa.
- Add rebalance rules:
  - refill base after too many sells;
  - refill quote after too many buys;
  - pause when price leaves the range;
  - shift the grid only under explicit rules.

Success check: every backtest reports not just profit, but inventory, missed sells due to no base, missed buys due to no quote, drawdown, fees, and exposure.

### 8. Gridbots need realistic fills

Problem: a grid backtest that fills every touched price level for free is fantasy. L2 data lets us do better, but only if we include spread, fees, depth, and latency.

Solution:

- Use Coinbase fee assumptions.
- Model bid/ask spread.
- Use available depth from reconstructed L2 where possible.
- Add latency assumptions.
- Support partial and missed fills.
- Compare against simple baselines such as buy-and-hold and do-nothing.

Success check: doubling fees or adding latency changes results in an understandable way.

### 8a. Missing data needs labels, not pretending

Problem: the archive has real capture gaps, including a large BTC/ETH/XRP-USD hole from `2025-09-20T04:37:00Z` to `2025-10-09T19:00:00Z`. Some gaps are likely from the computer being powered off. Missing L2 data cannot be recreated from candles.

Solution:

- Treat gaps as explicit invalid windows for L2 reconstruction and depth-aware backtests.
- Use existing raw L2 where available.
- Use one-minute OHLCV files only for price-only baselines, not for L2 depth, spread, or realistic fills.
- Allow a clearly labeled temporary candle fallback dataset for contiguous price-path tests. This can answer rough questions such as "did price touch these grid levels?" but it cannot answer "was there enough book depth at the bid/ask?".
- If external data is bought or imported later, tag it by provider, product, schema, and quality level.
- Allow backtests to skip missing windows or run separate "price-only" fallback tests with weaker assumptions.
- Backtest commands should eventually expose this as an explicit option such as `--include-fallback-candles`; without that option, L2-dependent backtests should skip missing L2 windows.
- Do not blend external candles into L2 tables as if they were captured order-book data.
- Current implementation note: MoneyMan can now fetch Coinbase Exchange public candles into `raw\external_ohlcv` and `derived\v1\candles_fallback`. The large BTC/ETH/XRP gap from `2025-09-20T04:37:00Z` to `2025-10-09T19:00:00Z` has a complete one-minute candle fallback patch with zero missing buckets in the fetch reports.
- Current implementation note: `python -m moneyman gridbot-backtest` supports explicit fallback-candle mode for price-path tests. Strict L2 mode audits reconstructed contracts, selects one eligible run/window, and applies the conservative V1 fill contract documented in `docs/L2_GRIDBOT_FILL_MODEL.md`. It fails closed on missing, corrupt, or ambiguous contracts.

Success check: every backtest report states which windows were excluded, which windows used L2, and whether any fallback candle data was used.

### 9. Statistical arbitrage is a later lane

Problem: stat arb can sound like the "smart" thing to do, but it is easy to fool yourself with noisy crypto data, leakage, and overfit models.

Solution:

- Defer stat arb until normalized data, quality reports, and a baseline backtester exist.
- Start with explainable reports:
  - correlations by window;
  - spread behavior;
  - lead-lag checks;
  - mean reversion tests;
  - out-of-sample splits.
- Keep every model chronological. Do not random-shuffle time series.

Success check: a stat-arb idea has a written hypothesis, a cost-aware backtest, and an out-of-sample result before it becomes a strategy candidate.

### 9a. ClusterLOB-style research needs preparation, not clustering yet

Problem: ClusterLOB-style research tries to learn from order-flow behavior by decomposing events in a limit order book. The paper is offline research over market-by-order data, while MoneyMan's current Coinbase captures are likely market-by-price L2 updates. L2 can support useful microstructure features, but it cannot identify individual order lifecycles the same way L3/MBO data can.

Solution:

- Build raw JSONL readers.
- Normalize trades and book updates.
- Preserve source path, session, product, channel, event time, receive time, and sequence metadata.
- Build feature interfaces for midpoint, spread, relative spread, book imbalance, trade imbalance, and data-quality metrics.
- Make feature interfaces streaming-friendly so they can run over archived files now and live WebSocket windows later.
- Document whether a dataset is L2 market-by-price or L3 market-by-order.
- Do not add K-means, ML clustering, participant labels, spoofing claims, or strategy claims yet.

Success check: MoneyMan has clean event tables and feature outputs that can run on historical data and are shaped for future real-time use, but no clustering model is implemented.

### 9b. Real-time inference is a target, live trading is not

Problem: an offline notebook can discover a pattern after the fact, but MoneyMan eventually needs to observe the book as it changes. That requires incremental readers and rolling features, not just one big batch job.

Solution:

- Build pure functions that accept normalized events and emit feature rows.
- Keep stateful rolling windows small and explicit.
- Support replaying historical JSONL through the same interface used by live WebSocket messages.
- Emit append-only feature events and data-quality warnings.
- Keep execution disconnected until paper trading and risk controls exist.

Success check: a historical file can be replayed through the same feature path planned for real-time messages.

### 10. Live trading must stay far away at first

Problem: wiring an unfinished research idea to live orders is the fastest way to turn a coding project into an expensive lesson.

Solution:

- Research first.
- Backtest second.
- Paper trade third.
- Live execution only after explicit approval, small limits, environment-based secrets, idempotent orders, kill switches, and independent risk checks.

Success check: no strategy module can directly place a live order.

### 10a. Existing exchange and 3Commas activity can be tracked read-only

Problem: the user's account may already have active 3Commas bots or manual trades. MoneyMan should understand account history and current exposure, but it should not place orders.

Solution:

- Start with read-only exports or API permissions only.
- Ingest account fills, deposits, withdrawals, fees, bot IDs/names if exported, and balances into separate account-ledger tables.
- Keep market-data research separate from account-ledger facts.
- Use 3Commas/gridbot exports to calibrate backtests and compare actual fills against simulated fills.
- Clearly label all account integrations as read-only until the user explicitly asks for paper or live execution later.

Success check: MoneyMan can say what happened in the account and compare it to a backtest, without being able to place a live trade.

### 11. A future web GUI should not create another port

Problem: the user already has Codex Word Game / Codex Study Hub as the local web surface. Adding another MoneyMan URL or port would make the system more annoying to run and remember.

Solution:

- Keep MoneyMan's ingestion, normalization, backtesting, and reporting code in this repository.
- Put any future web UI inside Codex Word Game / Codex Study Hub.
- Use routes such as `/moneyman`, `/moneyman/data`, `/moneyman/gridbots`, and `/moneyman/reports`.
- Reuse the existing hub server, navigation, dark visual language, and port.
- Start with read-only inventory and research views, not trading buttons.

Success check: the user opens one Codex Study Hub URL and sees MoneyMan there.

## Build Phases

### Phase 0: Recover The Project Shape

Goal: make the default branch tell the truth about the project.

Status: complete.

Completed:

- brought over the useful logger direction, requirements, README details, and roadmap from `origin/codex/add-coinbase-logger-roadmap`;
- kept the planning and operating documentation;
- added `.gitignore` rules for raw data, derived data, local databases, logs, exports, and secrets;
- documented the branch history so future work does not restart from zero;
- added a small package CLI with inventory, normalization, and feature commands.

Done when: a new Codex run sees the logger, roadmap, and this plan without needing oral history.

### Phase 1: Portable Data Roots And Inventory

Goal: safely understand an explicitly selected raw archive on either computer.

Status: locally complete. The canonical local archive, legacy discovery, read-only inventory, storage audit, gap/coverage tools, and relocation manifests exist and have been exercised. Running the same workflow on the second computer remains a portability proof, not a missing local implementation.

Completed locally:

- add config for `MONEYMAN_DATA_ROOT`, `MONEYMAN_RAW_ROOT`, and `MONEYMAN_DERIVED_ROOT`;
- add `moneyman inventory --raw-root <path> --catalog-root <path> --sample-records 2 --max-files 6` for a bounded read-only scan;
- add a legacy-folder discovery option for `*_ws_data/<session>/`;
- scan files without changing them;
- write a manifest in a derived/catalog folder;
- sample only the first few records per file for product/channel/time detection.

Done when: the other computer can produce a manifest without moving or rewriting raw files, including captures made by the old logger folder pattern.

### Phase 2: Collector Hardening

Goal: make future captures reliable and auditable.

Tasks:

- explicit output root;
- one canonical folder tree under `MONEYMAN_RAW_ROOT`;
- session manifests;
- graceful shutdown;
- product routing fixes;
- sequence, duplicate, heartbeat, reconnect, and latency reports;
- clear choice between `ticker` and `ticker_batch`.

Done when: a collector session can be restarted, audited, and trusted as raw input.

Status: complete locally. The fresh session `20260723T021818Z-d5a6ccbfc0a5` closed cleanly and passed an independent raw-derived audit after strict path confinement, independent route derivation, expanded provenance, and malformed-manifest fail-closed tests. Longer uptime and deliberately induced reconnects are later operational validation.

### Phase 3: Normalization

Goal: turn raw JSONL into usable research tables.

Tasks:

- define schemas for every table;
- convert raw records into versioned table/product/date JSONL partitions;
- quarantine malformed records;
- add tiny fixtures from real messages;
- test row counts and timestamp behavior;
- bind raw inputs, session manifests, output artifacts, quality counts, and reconciliation in an independently auditable dataset manifest;
- compute first features: midpoint, spread, relative spread, book imbalance, and trade imbalance.
- keep normalizer and feature code usable for both batch files and future streaming events.

Done when: a bounded product/date slice can be independently audited as clean trades, quotes, candles, L2 updates, heartbeats, status/control, and session provenance without changing raw inputs.

Status: complete locally. Dataset `712a360e77c76b63686731d4` proved the complete-session path against the exact fresh collector files, and `7b46883be85803f8c453d7ee` proved a larger observed-only historical multi-product path while correctly preserving its gap finding. Complete claims now fail closed on the normalizer's own or the bound collector's sequence evidence, malformed product IDs are quarantined before partitioning, and malformed manifest counts or paths are rejected.

### Phase 4: Data Quality And Book Reconstruction

Goal: know which windows are trustworthy.

Tasks:

- generate data-quality reports;
- reconstruct order books for small windows;
- mark invalid windows when gaps exist;
- emit depth, spread, midpoint, imbalance, and liquidity features.

Status: minimum V1 complete. Two frozen XRP-USD connection prefixes are strict eligible reconstruction contracts. Together they contain 41,829 emitted valid states from 61,671 envelopes, with exact global sequence continuity inside each prefix, contiguous right-boundary sentinels, stable raw hashes, and zero consumer-audit errors. Archive-wide and cross-product coverage remain later validation, not a prerequisite for completing collector and normalization foundations.

Done when: the project can explain where the data is complete enough for gridbot simulation.

### Phase 5: Gridbot Backtester

Goal: test the 3Commas-like gridbot idea honestly.

Minimum V1 completed:

- implement the arithmetic grid baseline; keep geometric and volatility grids as later extensions;
- model base/quote allocation;
- model fees, spread, depth, latency, partial fills, and missed fills;
- track inventory and reserved funds;
- report realized PnL, unrealized PnL, drawdown, turnover, exposure, missed buys, and missed sells;
- compare against buy-and-hold and no-trade baselines.

Status: arithmetic and strict-execution V1 complete. The fixed-quote price-only fallback reports missed sells due to insufficient base inventory and missed buys due to insufficient quote and compares against no-trade and buy-and-hold baselines. Strict mode consumes one explicitly selected audited contract at a time and models explicit latency, arrival spread crossing, maker/taker fees, strict price-through queue uncertainty, persistent emitted shadow depth with delta-only replenishment, partial-fill cancellation, inventory limits, and exact conservation. It deliberately does not infer hidden depth, exact queue position, L3 lifecycles, or fills from same-level depletion.

The first unchanged-grid comparison used 18,067 audited XRP rows and the same 19-minute Coinbase-public candle interval. Strict 100 ms execution completed 68 full maker orders versus 72 low-first and 70 high-first candle touches. Strict turnover was `$1,700` versus `$1,800` and `$1,750`; all strict reconciliation errors were zero. The real strict slice had enough visible depth for every eligible order and no arrival crossing, so taker and partial behavior remain fixture-validated rather than claimed from that run. Every mode finished below the no-trade cash baseline.

The pre-registered second comparison used a different logger session and 21,613 audited rows over another untouched 19-minute interval. The unchanged 100 ms message-clock model completed 45 full makers versus only 22 low-first and 28 high-first price touches. Strict turnover was `$1,125` versus `$550` and `$700`; all strict reconciliation errors and artifact checks were zero. Both 100 ms comparison slices had no taker or partial fill. Because strict fell below both candle controls once and exceeded both once, price-only touch replay is neither an upper nor a lower execution bound. These are mechanics validations, not profitability evidence.

A later pre-registered clock-only sensitivity used all 22,778 states from the second eligible window in paired 100 ms `message_ts` and `recv_ts` runs. The row set and every non-clock config field matched. Thirty-eight of 73 activation rows changed by -2 through +6 sequences, yet all 52 maker fills retained the same order and execution sequences, depth, fees, inventory, non-time equity values, and zero-error conservation. This is evidence that this one result is robust to the recorded clock choice, not proof that clocks never matter or that 100 ms is calibrated.

The sole isolated latency sensitivity was frozen at one 500 ms `recv_ts` point before replay. Against the full-window 100 ms control, the saved config changed only latency and retained the same 22,778 rows/hash. It completed 39 full orders plus one partial instead of 52 full orders, produced one arrival taker, reduced turnover to `$980.6686`, and ended with `$963.4474` plus `10.904591` XRP. Independent replay proved every activation, all 40 depth consumptions, partial cancellation, inventory, fees, artifacts, and exact conservation. Both cases remained below cash, so the result is execution-mechanics sensitivity rather than a reason to prefer slower latency.

Reserve-grid status: a separate XRP-only banded-lot v1.4 engine now exists without replacing the pooled historical engine. It uses one real shared cash balance, half-open accounting bands, tick-aligned fixed all-in level budgets, per-band caps on base-tranche unrecovered cash, fee-aware principal recovery, positive cash-flow profit, and residual reserve coin tagged to its lot, tranche, and originating band. An optional duplicate overflow tranche has a separate portfolio-wide active-cost ceiling but no separate wallet. The engine reports total/base/overflow cost, lots, reserve, profit, fees, turnover, exposure, and conservation/cap checks. V1.3 added a diagnostic-only sidecar for exact-horizon close markouts, recovery cohorts, adverse excursion, right-censored duration, and cash-cost-time. V1.4 adds a default-off, prior-close-only EMA entry guard with signal-only pre-roll and separate provenance. The same-engine `full_lot` policy remains the causal no-reserve control; the old engine remains only a historical compatibility reference.

The first bounded XRP comparison showed that the accounting works but did not show a profitable strategy. A 5% exit retained about 3.42% of each completed lot as reserve, completed 42 lots, left 33 open, and finished below holding cash before rebate. Smaller 3% and 4% exits completed more cycles but incurred more fees and finished lower in the same period. The completed `$100` surplus-only overflow experiment preserved the base trade fingerprint but added 20 end-open lots, almost doubled maximum drawdown, and reduced pre-rebate equity by about `$4.64`. V1.3 replayed the fixed profile without changing its trade fingerprint or financial result and exposed how recovery and capital lock should be measured.

The four-window validation covered May through August 2025 with overflow disabled and ranges anchored from the first candle of each calendar month. July's `[2.00,2.60)` grid recovered every active lot before XRP ran to `$3.6662`, while a fresh August `[2.80,3.40)` grid ended with 59 open lots and `$295` unrecovered after price fell below the range. A frozen EMA360/EMA1440 new-entry guard was then tested without changing sizing, exits, reserve, or allocation. It reduced capital lock and drawdown in all four development windows but reduced marked equity in all four. On a pre-registered June-July 2026 holdout, it reduced end active cost by `$40`, cash-cost-time by about 33.6%, and drawdown by about 35.5% on both paths while slightly improving marked and liquidation equity. This earns optional further validation, not default activation: every holdout variant still lost versus cash, development opportunity cost was material, and candles cannot test execution. Keep overflow and the guard off by default. Minimal valid-window L2 reconstruction, the strict fill model, its clock sensitivity, and the one-point 500 ms stress are closed mechanics experiments across two frozen windows. A future untouched-window replication is required only before broader generalization claims, not as the immediate project task. Every strategy setting remains unchanged, and protected/flexible allocation, reserve release, compounding, handoffs, and optimization stay separate.

Current fee status: `fee-profile` and `gridbot-backtest --fee-source auto` can use Coinbase's authenticated transaction summary endpoint to pull current Advanced maker/taker fee rates when local credentials are available. The committed example Coinbase One Advanced scenario models 25% of spot fees, capped at 100 USDC per month, and can be overridden or disabled. Fallback-candle grid fills still assume maker fees by default. The separately validated strict-L2 model can distinguish arrival takers from conservative resting makers, but it remains a pooled research intent model rather than literal exchange placement or queue proof.

Minimum V1 gate: complete. The specific "down three grids, up eight grids" problem is measurable as inventory, funding, missed-order, and conservation output in both fallback-candle and strict-L2 modes. Geometric/volatility grids and new allocation policies are separate research extensions.

### Phase 6: Research Dashboard

Goal: make the data and simulations readable inside Codex Word Game / Codex Study Hub.

Tasks:

- dark grimdark research UI;
- integration into the existing Study Hub server and navigation;
- data inventory page;
- session quality page;
- product/date coverage page;
- order-book and spread views;
- gridbot simulation runs;
- risk and inventory warnings.

Done when: the dashboard helps decide what to test next from the existing hub URL instead of asking the user to open another port.

### Phase 7: Paper Trading

Goal: run the same signal and risk path without live money.

Tasks:

- paper broker;
- append-only decision log;
- risk checks outside strategies;
- stale-data checks;
- duplicate-order protection;
- kill switch.

Done when: paper results can be compared to backtests and differences can be explained.

## Milestone Ledger

| Milestone | Status |
| --- | --- |
| Project recovery, data-root config, `.gitignore`, and portable setup | Complete |
| Legacy discovery/centralization plus read-only inventory, storage, coverage, gap, and cleanup tools | Complete locally |
| Multi-channel `normalization.v2` contract and exploratory streaming-friendly features | Complete locally; clean-checkout portability remains |
| Collector root/layout, session folder, routing, graceful close, and single `ticker` default | Complete |
| Collector provenance/integrity manifest plus independent audit | Complete locally; fresh proof passed |
| Stable bounded normalization for trades, L2, quotes, candles, heartbeats, status/control, and sessions | Complete locally; complete and observed-only proofs passed |
| Clean-checkout rehearsal on the other computer | Next required milestone |
| Minimal reconstruction on two independent XRP windows | Complete |
| Conservative strict-L2 fills, two price controls, clock A/B, and one 500 ms stress | Complete mechanics evidence |
| Arithmetic inventory-aware gridbot, fee profile, reserve accounting, overflow test, diagnostics, and first EMA holdout | Complete research baselines; no profitability claim |
| Read-only account/3Commas ledger import | Later, not started |
| Codex Study Hub read-only integration | Post-MVP, not started |
| Paper broker/risk path | Later product stage, not started |

## What To Ask The User For

When it is time to verify portability on the other computer, ask for only the minimum needed:

- the top-level folder path containing the raw captures;
- whether the data should stay on that computer, be copied to an external drive, or be copied to this machine;
- whether filenames/session folders follow the logger pattern from the roadmap branch;
- a tiny sample folder or file if the full archive is not locally accessible.

Do not ask the user to summarize a year of L2 data by hand. Build tools that inspect it safely.
