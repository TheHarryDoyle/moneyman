# MoneyMan Learning And Build Roadmap

This roadmap was recovered from `origin/codex/add-coinbase-logger-roadmap` and reconciled with the fuller project plan on `main`.

The safest way to build MoneyMan is as a research pipeline, not as one large trading bot. First prove that the data is correct. Then prove that a hypothesis survives realistic backtesting. Only after that should paper trading enter the picture.

## 1. Collect Trustworthy Raw Data

Purpose: record exactly what Coinbase sent and when this computer received it.

The recovered logger writes compressed JSONL as the immutable source of truth. It now writes future sessions under:

```text
$env:MONEYMAN_RAW_ROOT\coinbase_advanced_trade\session=<SESSION_ID>\
```

Current V1 contract:

- keep the output root explicit through `MONEYMAN_RAW_ROOT`;
- exclusively create and confine each session and roll path beneath the resolved raw root, using strict route names and an explicit review route for invalid network routes;
- save atomic manifests with effective config, execution sources including project configuration, host/runtime including the installed `websockets` version, connection epochs, sequence/duplicate/heartbeat/reconnect/latency findings, and exact closed-file evidence;
- flush and close every writer before the final manifest;
- route every product in a multi-product event;
- independently derive expected routing destinations and observable quality findings from the bound raw rows, failing closed on malformed manifest counts or paths; and
- choose `ticker` or `ticker_batch` unless both are intentionally measured.

The V1 gate passed on public session `20260723T021818Z-d5a6ccbfc0a5`: 271 frames/envelopes/routed rows across four immutable files, one connection, 18 heartbeats, zero parse/sequence/duplicate/regression/reconnect/stale-heartbeat/audit defects, and an independent audit with `valid=true` and `warnings=[]`. Its latency histogram retained 225 negative samples with a minimum of `-2.711` ms as clock-offset evidence. A deliberately interrupted multi-hour reconnect soak remains later operational validation, not a reason to keep rebuilding the same contract.

## 2. Normalize Messages

Purpose: convert nested messages into stable tables with one kind of observation per row.

`normalization.v2` creates versioned JSONL partitions for:

| Table | Useful columns |
| --- | --- |
| trades | event time, receive time, product, price, size, side, trade id, source path, session |
| l2_updates | event time, receive time, product, side, price level, new quantity, source path, session |
| quotes | event/receive time, product, best bid/ask and sizes, midpoint, spread, source |
| candles | start/receive time, product, OHLCV, source |
| heartbeats | event/receive time, counter, connection and source provenance |
| status | event/receive time, product status and increments |
| control | subscriptions, errors, and other recognized non-market envelopes |
| sessions | collector/session manifest provenance and selected-slice quality counts |
| quarantine | malformed or incomplete records with source path, line, reason, and raw payload |

Dataset manifests bind raw inputs, sibling session manifests, the execution-source bundle, configuration, quality report, partitions, and count reconciliation. Complete claims require the exact file set from a currently valid collector audit and fail closed on either normalizer- or collector-derived sequence defects or malformed manifest counts and paths. Malformed product IDs are quarantined before partitioning; historical or bounded selections stay observed-only. Later these tables can be converted to Parquet after schemas settle. Numeric text is preserved; finiteness/sign/OHLC/BBO semantic certification remains an optional later quality layer.

Success check: `audit-normalization` rehashes and recounts the dataset with zero reconciliation errors while all selected raw inputs remain byte-for-byte unchanged.

The local gate passed twice: complete-session dataset `712a360e77c76b63686731d4` emitted 16,351 semantic rows plus one session row from 271 canonical envelopes, with zero quarantine or sequence defects, six zero reconciliations, and a successful collector re-audit. Historical observed-only dataset `7b46883be85803f8c453d7ee` emitted 344,664 semantic rows plus one session row from 35,574 canonical envelopes and preserved its 53-number sequence gap instead of overstating completeness. Both audits were valid with no warnings, zero quarantine, zero reconciliation errors, and unchanged raw/source evidence. The one next required foundation task is a clean-checkout portability rehearsal on the other computer, not another L2 or strategy replay.

## 3. Exploratory Core Features (Implemented, Not Model-Ready)

`python -m moneyman features` now calculates these measurements from one audited V2 dataset and one explicitly selected product using small streaming-friendly functions:

- midpoint: `(best_bid + best_ask) / 2`;
- spread: `best_ask - best_bid`;
- relative spread: `spread / midpoint`;
- book imbalance: `(bid_depth - ask_depth) / (bid_depth + ask_depth)`;
- trade imbalance: `(buy_size - sell_size) / (buy_size + sell_size)`.

Always shift or window rolling features so a decision at time `t` uses only information available by time `t`.

These rows remain exploratory. They do not replace an audited reconstructed book, and the underlying normalized numeric fields still need the separately optional, later finiteness/sign/OHLC/BBO certification before model-ready claims.

## 4. ClusterLOB Preparation, Not Clustering Yet

ClusterLOB is an inspiration for later order-flow behavior research. The arXiv page identifies it as an offline market-by-order approach that augments individual events with time-dependent features and clusters them with K-means++. MoneyMan's current Coinbase captures are likely L2 market-by-price, not L3/MBO market-by-order.

The local foundation now provides:

- clean raw JSONL readers;
- normalized trades and L2 book-update tables;
- source/session metadata;
- first non-ML microstructure features;
- quality reports; and
- feature interfaces that can run on archived files and future live WebSocket windows.

This closes preparation at the structural/provenance level, not the research question. Do not add K-means, ML clustering, participant labels, spoofing claims, strategy claims, or live trading merely because the tables exist.

## 5. Honest Backtest Baselines (Minimum V1 Complete)

Arithmetic price-only, conservative strict-L2, reserve accounting, overflow, diagnostics, and the first frozen EMA holdout now provide minimum research baselines. They validate mechanics and accounting, not profitability: every cited strict comparison and EMA holdout remains bounded, and the strategy variants remained below cash in the relevant evidence.

Any future backtest must continue to include:

- Coinbase fees;
- bid/ask spread;
- slippage and latency assumptions;
- order size and available liquidity;
- missed or partial fills; and
- chronological, out-of-sample evaluation.

Useful metrics include total/net return, drawdown, volatility, Sharpe ratio, turnover, hit rate, average win/loss, inventory, missed buys due to no quote, and missed sells due to no base.

## 6. Risk And Paper Trading Stay Separate

Strategy code should not place orders. Risk checks, paper execution, and any eventual live execution must be separate layers.

Start with:

- maximum position per product;
- maximum total exposure;
- maximum order size;
- stale-data and disconnected-feed checks;
- duplicate-order protection; and
- a manual kill switch that defaults to no new orders.

Live execution is intentionally not implemented.
