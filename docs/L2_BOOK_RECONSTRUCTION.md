# L2 Book Reconstruction Contract

Last updated: 2026-07-22

## Status

MoneyMan now has a deterministic, gap-aware Coinbase Advanced Trade L2 market-by-price reconstructor. It is a data-quality and book-state layer, not a fill simulator and not a trading bot. Two independent frozen XRP-USD connection prefixes have passed the hand-checked fixture suite and the consumer-side artifact audit.

The strict gridbot boundary has advanced again: `moneyman/l2_fills.py` now consumes one audited eligible run/window through a separately tested conservative execution model. This does not change the reconstruction contract or turn book states into ground-truth executions. Exact queue position, hidden liquidity, and L3 order priority remain unavailable; the explicit assumptions, comparisons, and sensitivities live in `docs/L2_GRIDBOT_FILL_MODEL.md`.

## Ordering And Atomicity

- Coinbase `sequence_num` is treated as connection-global across every top-level WebSocket envelope, not as an L2-only sequence. Ticker, trade, candle, subscription, and other envelopes must remain in the replay stream even though only `l2_data` mutates the book.
- `ordered_files` replays raw files in the exact order supplied and then in physical line order. Use it only with `input_order=file` for one stream and its sequential roll files.
- `routed_shards` requires `receive_time` ordering. Identical cross-shard routing copies are collapsed before transport-duplicate policy is applied.
- Every full L2 envelope is atomic. All relevant events and level mutations are applied before the book is validated or emitted. A temporary intra-envelope cross that resolves by the end of that envelope is not falsely reported as a crossed book.
- A `snapshot` clears the previous product book. An `update` uses absolute quantities. Zero quantity deletes a level; deleting an absent level is a counted safe no-op.
- `bid` and `buy` map to bid. `offer`, `ask`, and `sell` map to ask. Prices must be positive finite decimals; quantities must be finite and nonnegative.
- Sequence numbers must be nonnegative JSON integers. Strings, floats, booleans, negative values, and explicit nulls are malformed rather than silently coerced.

## Validity Rules

The book starts invalid and becomes valid only after a structurally valid target-product snapshot. A normal update can never recover an invalid window.

A valid window closes on a global sequence gap, exact transport duplicate, conflicting duplicate, sequence regression/reconnect, malformed sequenced timestamp, timestamp regression, configured stale-envelope gap, malformed target L2 payload, unknown side, invalid number, negative quantity, empty side, locked book, or crossed book. A fresh valid snapshot can start a new window; a sequence-regression snapshot starts a new connection epoch.

An ordinary unsequenced control message, such as the legacy authentication error in the frozen window, is preserved and reported. It is not treated as an L2 gap when the surrounding global sequence remains continuous.

`sequence_scope=complete` is an explicit completeness claim. Inside the supplied inputs, the runner proves global sequence continuity and invalidates at any observed defect. Raw files are hashed before and after reading and must remain stable. When a right-boundary file is supplied, it is hashed but not replayed, and its first sequenced envelope must immediately follow the final replayed sequence. A connection prefix beginning at sequence zero plus a contiguous right-boundary sentinel receives the stronger `verified_connection_prefix_with_contiguous_right_boundary` provenance label. Other complete windows retain an explicit operator attestation because no program can prove that an unsupplied file does not exist.

## Output Contract

Each run writes under `derived/v1/book_reconstruction/<RUN_ID>/`:

```text
config.json
book_snapshots.jsonl
book_quality_events.jsonl
book_windows.jsonl
manifest.json
```

A matching catalog report is written under `catalog/quality/`.

`book_snapshots.jsonl` contains only valid states. Each row records product, capture stream, connection epoch, validity window, global sequence, originating snapshot, raw source path/line/hash, top-N bid/offer levels, BBO, midpoint, spread, full and visible depth, imbalance, and state fingerprints. The full in-memory book is retained even when emitted depth is truncated.

Every emitted state receives an O(1) deterministic full-state set fingerprint. The exact canonical full-book line hash is more expensive, so it is calculated at every fresh snapshot and at explicitly requested global sequence checkpoints. Checkpoint keys include `connection_epoch:sequence_num` so a reconnect cannot overwrite an earlier hash. The visible top-N line hash is calculated on every emitted row.

The manifest binds the supported schemas, engine source hash, saved config hash, ordered raw-file sizes and hashes, source stability checks, optional right-boundary evidence, artifact hashes and row counts, semantic stream fingerprint, state streams, and provenance-aware run fingerprint. `audit_book_reconstruction_run` rechecks raw sources, all four artifacts, row/window origin relationships, BBO and visible-level ordering, state/checkpoint/final summaries, engine source, and both fingerprints. Strict consumers must use the audit result, not count filenames.

## Frozen First XRP-USD Validation

Run ID: `20260721T235740933455Z-0760dcde`

This run supersedes the pre-hardening draft run `20260721T234008921166Z-9af5b4eb`. The draft output remains locally for comparison but fails the current exact-engine audit, so strict discovery excludes it from eligible results.

Replay inputs, in order:

1. `xrp_2025-08-01_21-21.jsonl`, SHA-256 `a351238627fb340f50e931e4596c755099a3a0cafd6e2a5ee0e4807a31811f0b`
2. `xrp_2025-08-01_21-31.jsonl`, SHA-256 `b23e24329b99e3a48bc9765d9f58982a098715c84d8bd57c574ff6b23cfa8d91`

Right-boundary sentinel, not replayed: `xrp_2025-08-01_21-41.jsonl`, SHA-256 `e7e9b88833704b51ad8dca7d39130c634bed01883d7bc518dbf9f42fd7dee5df`; its first envelope is sequence 26235.

Verified results:

- 26,236 top-level envelopes; 26,235 sequenced exactly once from 0 through 26234; one preserved unsequenced authentication error.
- 19,051 target L2 envelopes: one snapshot and 19,050 updates.
- 208,328 level mutations: 41,021 adds, 144,057 replacements, 21,638 existing-level deletes, and 1,612 absent-level delete no-ops.
- 23,250 total zero-quantity deletes.
- One valid strict window with 19,051 emitted atomic book states.
- Zero sequence gaps, duplicates, conflicts, regressions, stale gaps over one second, malformed target L2 envelopes, empty books, locked books, or crossed books.
- Maximum observed global envelope gap: 0.601337 seconds.
- Initial BBO at sequence 6: bid 3.0066, ask 3.007.
- Final L2 BBO at sequence 26232: bid 3, ask 3.0004.
- Ticker cross-checks: 4,003 of 4,840 exact; 4,452 within 0.0001 on both sides. Ticker comparison is auxiliary because ticker and L2 envelopes are asynchronous.

Canonical full-book SHA-256 checkpoints:

- `0:6`: `fde58738e8a198c79991fcf2dfa46541df5279df075b1f0accea62e022ba69dd`
- `0:13632`: `66e334d350c4058583a2aeb1eb18732920d83cd3943ba3c17cf6e6867c8129e0`
- `0:13633`: `dfb93e15b64525de62320ed0911a27084ebb5a2a2dd4ca0f6390f3dce2a5a162`
- `0:26232`: `184267a5ff19a0ae559d0625fda113e1f29d643264329fe3265f9213e95dbecf`

The pre-run and post-run hashes of all three raw files were identical. The consumer audit returned `valid=true`, `strict_l2_eligible=true`, one eligible window, 19,051 snapshot rows, and no errors.

## First Reproducible Command

```powershell
python -m moneyman reconstruct-book `
  --raw-file "C:\Users\doyle\Downloads\MoneyManData\raw\legacy_ws_data\xrp_ws_data\xrp_2025-08-01_21-21.jsonl" `
  --raw-file "C:\Users\doyle\Downloads\MoneyManData\raw\legacy_ws_data\xrp_ws_data\xrp_2025-08-01_21-31.jsonl" `
  --product XRP-USD `
  --capture-stream-id legacy-xrp-ws-2025-08-01-session `
  --sequence-scope complete `
  --input-order file `
  --source-layout ordered_files `
  --depth-limit 10 `
  --emit-every-l2-messages 1 `
  --full-hash-sequence 13632 `
  --full-hash-sequence 13633 `
  --full-hash-sequence 26232 `
  --max-envelope-gap-seconds 1 `
  --ticker-tolerance 0.0001 `
  --right-boundary-file "C:\Users\doyle\Downloads\MoneyManData\raw\legacy_ws_data\xrp_ws_data\xrp_2025-08-01_21-41.jsonl" `
  --derived-root "C:\Users\doyle\Downloads\MoneyManData\derived" `
  --catalog-root "C:\Users\doyle\Downloads\MoneyManData\catalog"
```

## Frozen Second XRP-USD Validation

Run ID: `20260722T032803947186Z-9593d3b9`

This is a separate logger session under `xrp_ws_data\2`, not an extension or re-slice of the first run. Replay inputs, in order:

1. `xrp_2025-08-02_15-58.jsonl`, SHA-256 `d833b07fed12ff698248b3786bd7ca46b60d53f4921bd2f367d9e4a495a97667`
2. `xrp_2025-08-02_16-08.jsonl`, SHA-256 `fa38101815fac17c53d0fbb5096386550032a22f0cd3ffd46d14f52240ad9cff`

Right-boundary sentinel, not replayed: `xrp_2025-08-02_16-18.jsonl`, SHA-256 `723344bef49aa2c9f0be9155fb5adc58317d1db90775163442af8ceafdbe4c5a`; its first envelope is sequence 35434.

Verified results:

- 35,435 top-level envelopes; 35,434 sequenced exactly once from 0 through 35433; one preserved unsequenced authentication error.
- 22,777 target L2 envelopes: one snapshot and 22,776 updates.
- 450,728 level mutations: 52,389 adds, 359,947 replacements, 33,004 existing-level deletes, and 5,388 absent-level delete no-ops.
- One valid strict window with 22,778 emitted states. The extra state is the explicitly requested non-L2 rollover checkpoint at sequence 17891; it records the unchanged current book and does not mutate it.
- Zero sequence gaps, duplicates, conflicts, regressions, stale gaps over one second, malformed target L2 envelopes, empty books, locked books, or crossed books.
- Maximum observed global envelope gap: 0.86642 seconds.
- Initial BBO at sequence 6: bid 2.8297, ask 2.83.
- Final L2 BBO at sequence 35432: bid 2.8282, ask 2.8285.
- Raw inputs and sentinel remained stable during replay, the reconstruction artifacts matched their declared hashes, and the independent audit returned `valid=true`, `strict_l2_eligible=true`, one eligible window, and no errors.

Canonical full-book SHA-256 checkpoints:

- `0:6`: `70eeb96475987e8de709774f4842809bab2e61b3bfdb0b69cfdfc39efe25bc1f`
- `0:17890`: `edc77b71829c1affddbe6efbac285fe47360ebf112cd9513550d0fe98cc801d1`
- `0:17891`: `edc77b71829c1affddbe6efbac285fe47360ebf112cd9513550d0fe98cc801d1`
- `0:35432`: `07377fe36fb4ce19cb83444928fb4b577c441d03642db7fc90bf2d058a1c5c1e`

The equal hashes at 17890 and 17891 are expected: sequence 17891 is the first file-two `market_trades` envelope, which must remain in the global stream but must not alter the L2 book. The engine source hash is `b47131434318d8d14592854faf6741cf8f6d7a5333f9eee996feae12b5aa5493`. The manifest hash is `2a6f157b442a3053efe4579a3e61c7c0b20624eda0a72b16643ca6ba56d7e10f`, and its catalog copy is byte-identical.

## Second Reproducible Command

```powershell
python -m moneyman reconstruct-book `
  --raw-file "C:\Users\doyle\Downloads\MoneyManData\raw\legacy_ws_data\xrp_ws_data\2\xrp_2025-08-02_15-58.jsonl" `
  --raw-file "C:\Users\doyle\Downloads\MoneyManData\raw\legacy_ws_data\xrp_ws_data\2\xrp_2025-08-02_16-08.jsonl" `
  --product XRP-USD `
  --capture-stream-id legacy-xrp-ws-2025-08-02-session-2 `
  --sequence-scope complete `
  --input-order file `
  --source-layout ordered_files `
  --depth-limit 10 `
  --emit-every-l2-messages 1 `
  --full-hash-sequence 17890 `
  --full-hash-sequence 17891 `
  --full-hash-sequence 35432 `
  --max-envelope-gap-seconds 1 `
  --ticker-tolerance 0.0001 `
  --right-boundary-file "C:\Users\doyle\Downloads\MoneyManData\raw\legacy_ws_data\xrp_ws_data\2\xrp_2025-08-02_16-18.jsonl" `
  --derived-root "C:\Users\doyle\Downloads\MoneyManData\derived" `
  --catalog-root "C:\Users\doyle\Downloads\MoneyManData\catalog"
```

## What This Does Not Prove

These are two bounded valid L2 price-level windows from independent legacy logger sessions. They prove deterministic reconstruction and contract integrity for those prefixes, not archive-wide continuity, gridbot profitability, or ground-truth execution. Coinbase L2 is not L3/MBO: exact queue position, individual-order lifecycles, hidden liquidity, participant identity, and spoofing claims remain unavailable.

The first isolated fill model, its second untouched-window validation, the full-window `message_ts`/`recv_ts` sensitivity, and the single 500 ms `recv_ts` stress are complete and leave this reconstruction engine unchanged. Both clock loaders and both latency cases selected the same 22,778 ordered states. Clock choice moved activation timing without changing fills; the slower latency changed the fill path and exercised one taker and one partial while all reconstruction and conservation audits still passed. If future strict-execution generalization is selected, it must use a new untouched audited window rather than tune this one; that replication is not the next required project milestone. Do not combine it with overflow, EMA changes, protected/flexible allocation, reserve release, compounding, band handoffs, or live behavior.
