from __future__ import annotations

import argparse
import json
from dataclasses import asdict
from pathlib import Path

from .book import run_book_reconstruction
from .candles import fetch_coinbase_exchange_candles, import_candle_csv_files, move_external_ohlcv_files
from .centralize import centralize_legacy_ws_data
from .cleanup import run_feather_cleanup
from .collector_audit import audit_collector_session
from .config import DataRoots
from .coverage import run_legacy_coverage
from .features import run_features
from .fees import (
    default_coinbase_one_rebate_cap,
    default_coinbase_one_rebate_rate,
    default_coinbase_one_rebate_used,
    default_liquidity_assumption,
    fee_profile_to_report,
    resolve_fee_profile,
)
from .gaps import run_raw_gaps
from .gridbot import run_gridbot_backtest
from .inventory import run_inventory
from .normalize import audit_normalization, normalize_roots
from .probe import run_read_check
from .relocate import move_stranded_coinbase_sessions
from .reserve_gridbot import run_reserve_gridbot_backtest
from .storage_audit import run_storage_audit


def _path(value: str) -> Path:
    return Path(value).expanduser()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="moneyman", description="MoneyMan data pipeline tools")
    subparsers = parser.add_subparsers(dest="command", required=True)

    inventory = subparsers.add_parser("inventory", help="Read-only raw data discovery and manifest")
    inventory.add_argument("--raw-root", action="append", type=_path, help="Raw root or file to scan")
    inventory.add_argument("--catalog-root", type=_path, help="Catalog output root")
    inventory.add_argument("--include-legacy-ws-folders", action="store_true")
    inventory.add_argument("--legacy-search-root", action="append", type=_path, help="Root to search for *_ws_data folders")
    inventory.add_argument("--sample-records", type=int, default=5)
    inventory.add_argument("--max-files", type=int)

    centralize = subparsers.add_parser(
        "centralize-legacy",
        help="Centralize legacy raw data under MONEYMAN_RAW_ROOT",
    )
    centralize.add_argument("--legacy-search-root", action="append", type=_path, required=True)
    centralize.add_argument("--raw-root", type=_path, help="Canonical raw output root")
    centralize.add_argument("--catalog-root", type=_path, help="Catalog output root")
    centralize.add_argument("--mode", choices=["hardlink", "copy", "move", "plan"], default="plan")
    centralize.add_argument("--max-files", type=int)
    centralize.add_argument("--progress-every", type=int, default=10_000)

    stranded = subparsers.add_parser(
        "move-stranded-sessions",
        help="Move closed repo-local Coinbase session folders into MONEYMAN_RAW_ROOT",
    )
    stranded.add_argument(
        "--source-raw-root",
        action="append",
        type=_path,
        help="Raw root to scan for coinbase_advanced_trade/session=* folders; defaults to ./data/raw",
    )
    stranded.add_argument("--raw-root", type=_path, help="Canonical raw output root")
    stranded.add_argument("--catalog-root", type=_path, help="Catalog output root")
    stranded.add_argument("--mode", choices=["plan", "move"], default="plan")
    stranded.add_argument(
        "--include-open-sessions",
        action="store_true",
        help="Allow moving sessions whose manifest has no end_ts. Use only after confirming the logger is stopped.",
    )

    normalize = subparsers.add_parser(
        "normalize",
        help="Normalize raw Coinbase JSONL into audited normalization.v2 partitions",
    )
    normalize.add_argument("--raw-root", action="append", type=_path, help="Raw root or file to scan")
    normalize.add_argument("--derived-root", type=_path, help="Derived output root")
    normalize.add_argument("--catalog-root", type=_path, help="Catalog output root")
    normalize.add_argument("--quarantine-root", type=_path, help="Quarantine output root")
    normalize.add_argument("--include-legacy-ws-folders", action="store_true")
    normalize.add_argument("--legacy-search-root", action="append", type=_path)
    normalize.add_argument("--limit-files", type=int)
    normalize.add_argument(
        "--input-order",
        choices=["file", "receive_time"],
        default="file",
        help="Read file-by-file or merge selected shards by exact receive timestamp",
    )
    normalize.add_argument(
        "--sequence-scope",
        choices=["observed", "complete"],
        default="observed",
        help=(
            "complete asserts connection-global coverage and requires receive_time ordering; "
            "bounded runs always report observed-only results"
        ),
    )
    normalize.add_argument(
        "--limit-records-per-file",
        type=int,
        help="Bound each selected file independently; recorded as an incomplete quality slice",
    )
    normalize.add_argument(
        "--max-records",
        type=int,
        help="Optional total record bound after ordering",
    )
    normalize.add_argument(
        "--max-open-partitions",
        type=int,
        default=32,
        help="Maximum simultaneously open partition files (default: 32)",
    )

    audit_normalize = subparsers.add_parser(
        "audit-normalization",
        help="Rehash a normalization.v2 manifest, raw inputs, session manifests, and artifacts",
    )
    audit_normalize.add_argument("--manifest", type=_path, required=True)

    audit_collector = subparsers.add_parser(
        "audit-collector-session",
        help="Rehash and reconcile one closed collector session manifest and its raw files",
    )
    audit_collector.add_argument("--manifest", type=_path, required=True)

    features = subparsers.add_parser("features", help="Calculate first non-ML microstructure features")
    features.add_argument("--derived-root", type=_path, help="Derived output root")
    features.add_argument("--catalog-root", type=_path, help="Catalog output root")
    features.add_argument(
        "--product",
        required=True,
        help="Required single product; prevents one order book from mixing products",
    )
    features.add_argument("--trade-window", type=int, default=100)
    features.add_argument(
        "--normalization-dataset-id",
        help="Read one authoritative normalization.v2 manifest instead of aggregating legacy v1 parts",
    )

    reconstruct_book = subparsers.add_parser(
        "reconstruct-book",
        help="Reconstruct and audit deterministic Coinbase L2 book windows",
    )
    reconstruct_book.add_argument(
        "--raw-file",
        action="append",
        type=_path,
        required=True,
        help="Ordered raw JSONL/JSONL.GZ input file; repeat for file rollovers",
    )
    reconstruct_book.add_argument("--product", required=True, help="Product id such as XRP-USD")
    reconstruct_book.add_argument(
        "--capture-stream-id",
        required=True,
        help="Stable identifier for the source WebSocket connection/capture stream",
    )
    reconstruct_book.add_argument(
        "--sequence-scope",
        choices=["complete", "filtered"],
        default="filtered",
        help="Use complete only when every envelope in the connection window is present",
    )
    reconstruct_book.add_argument(
        "--input-order",
        choices=["file", "receive_time"],
        default="file",
        help="Replay in supplied file/line order or canonical receive-time order",
    )
    reconstruct_book.add_argument(
        "--source-layout",
        choices=["ordered_files", "routed_shards"],
        default="ordered_files",
        help=(
            "Use ordered_files for one stream and its roll files; routed_shards requires "
            "receive_time so identical logger routing replicas can be collapsed"
        ),
    )
    reconstruct_book.add_argument("--depth-limit", type=int, default=25)
    reconstruct_book.add_argument("--emit-every-l2-messages", type=int, default=1)
    reconstruct_book.add_argument(
        "--full-hash-sequence",
        action="append",
        type=int,
        default=[],
        help=(
            "Sequence number at which to compute the expensive canonical full-book hash; "
            "repeat for checkpoints (fresh snapshots are always hashed)"
        ),
    )
    reconstruct_book.add_argument(
        "--max-envelope-gap-seconds",
        help="Optional maximum time gap between canonical envelopes",
    )
    reconstruct_book.add_argument("--ticker-tolerance", default="0.0001")
    reconstruct_book.add_argument("--start", help="UTC inclusive envelope-time filter")
    reconstruct_book.add_argument("--end", help="UTC exclusive envelope-time filter")
    reconstruct_book.add_argument("--max-messages", type=int)
    reconstruct_book.add_argument(
        "--right-boundary-file",
        type=_path,
        help=(
            "Optional next raw roll file whose first sequence must immediately follow the "
            "replayed window; it is hashed as boundary evidence but not replayed"
        ),
    )
    reconstruct_book.add_argument("--derived-root", type=_path, help="Derived output root")
    reconstruct_book.add_argument("--catalog-root", type=_path, help="Catalog output root")

    candles = subparsers.add_parser(
        "import-candles",
        help="Import one-minute OHLCV CSV files as price-only fallback candles",
    )
    candles.add_argument("--input", action="append", type=_path, required=True)
    candles.add_argument("--product", required=True, help="Product id such as XRP-USD")
    candles.add_argument("--provider", default="unknown")
    candles.add_argument("--derived-root", type=_path, help="Derived output root")
    candles.add_argument("--catalog-root", type=_path, help="Catalog output root")
    candles.add_argument("--max-rows", type=int)

    move_candles = subparsers.add_parser(
        "move-external-ohlcv",
        help="Move external OHLCV source files into raw/external_ohlcv",
    )
    move_candles.add_argument("--input", action="append", type=_path, required=True)
    move_candles.add_argument("--product", required=True, help="Product id such as XRP-USD")
    move_candles.add_argument("--provider", required=True, help="Provider/source label such as coinbase_ccxt")
    move_candles.add_argument("--raw-root", type=_path, help="Raw output root")
    move_candles.add_argument("--catalog-root", type=_path, help="Catalog output root")
    move_candles.add_argument("--mode", choices=["plan", "move"], default="plan")

    fetch_candles = subparsers.add_parser(
        "fetch-candles",
        help="Fetch Coinbase Exchange public OHLCV as labeled price-only fallback candles",
    )
    fetch_candles.add_argument("--product", action="append", required=True, help="Product id such as XRP-USD")
    fetch_candles.add_argument("--start", required=True, help="UTC start such as 2025-09-20T04:37:00Z")
    fetch_candles.add_argument("--end", required=True, help="UTC end such as 2025-10-09T19:00:00Z")
    fetch_candles.add_argument("--granularity-seconds", type=int, default=60)
    fetch_candles.add_argument("--raw-root", type=_path, help="Raw output root")
    fetch_candles.add_argument("--derived-root", type=_path, help="Derived output root")
    fetch_candles.add_argument("--catalog-root", type=_path, help="Catalog output root")
    fetch_candles.add_argument("--provider", default="coinbase_exchange_public")
    fetch_candles.add_argument("--sleep-seconds", type=float, default=0.0)
    fetch_candles.add_argument("--timeout-seconds", type=int, default=30)

    fee_profile = subparsers.add_parser(
        "fee-profile",
        help="Show the fee profile MoneyMan will use without placing trades",
    )
    fee_profile.add_argument("--source", choices=["auto", "manual", "coinbase"], default="auto")
    fee_profile.add_argument("--fee-rate", default="0.006", help="Manual fallback fee rate")
    fee_profile.add_argument("--maker-fee-rate", help="Manual maker fee rate override")
    fee_profile.add_argument("--taker-fee-rate", help="Manual taker fee rate override")
    fee_profile.add_argument(
        "--liquidity-assumption",
        choices=["maker", "taker"],
        default=default_liquidity_assumption(),
        help="Which side of the fee tier to use for grid fills",
    )
    fee_profile.add_argument(
        "--coinbase-one-advanced-rebate-rate",
        default=default_coinbase_one_rebate_rate(),
        help="Coinbase One Advanced spot-fee rebate rate; selected research scenario uses 0.25",
    )
    fee_profile.add_argument(
        "--coinbase-one-monthly-rebate-cap",
        default=default_coinbase_one_rebate_cap(),
        help="Monthly Coinbase One Advanced rebate cap in USDC; selected research scenario uses 100",
    )
    fee_profile.add_argument(
        "--coinbase-one-monthly-rebate-used",
        default=default_coinbase_one_rebate_used(),
        help="Rebate already used this membership month in USDC",
    )

    gridbot = subparsers.add_parser(
        "gridbot-backtest",
        help="Run the first inventory-aware gridbot backtest; fallback candles must be explicit",
    )
    gridbot.add_argument("--product", required=True, help="Product id such as XRP-USD")
    gridbot.add_argument("--lower", required=True, help="Lower grid price")
    gridbot.add_argument("--upper", required=True, help="Upper grid price")
    gridbot.add_argument("--grid-count", type=int, required=True, help="Number of grid intervals")
    gridbot.add_argument("--quote-start", required=True, help="Starting quote balance, such as USD")
    gridbot.add_argument("--base-start", default="0", help="Starting base asset balance")
    gridbot.add_argument("--order-quote", required=True, help="Quote value per grid order")
    gridbot.add_argument("--fee-rate", default="0.006", help="Per-fill fee rate as a decimal")
    gridbot.add_argument(
        "--fee-source",
        choices=["auto", "manual", "coinbase"],
        default="auto",
        help="auto pulls Coinbase fee tier when env credentials are available, then falls back to manual",
    )
    gridbot.add_argument("--maker-fee-rate", help="Manual maker fee rate override")
    gridbot.add_argument("--taker-fee-rate", help="Manual taker fee rate override")
    gridbot.add_argument(
        "--liquidity-assumption",
        choices=["maker", "taker"],
        default=default_liquidity_assumption(),
        help="Grid limit orders usually model maker fills; fallback candles cannot prove it",
    )
    gridbot.add_argument(
        "--coinbase-one-advanced-rebate-rate",
        default=default_coinbase_one_rebate_rate(),
        help="Coinbase One Advanced spot-fee rebate rate; selected research scenario uses 0.25",
    )
    gridbot.add_argument(
        "--coinbase-one-monthly-rebate-cap",
        default=default_coinbase_one_rebate_cap(),
        help="Monthly Coinbase One Advanced rebate cap in USDC; selected research scenario uses 100",
    )
    gridbot.add_argument(
        "--coinbase-one-monthly-rebate-used",
        default=default_coinbase_one_rebate_used(),
        help="Rebate already used this membership month in USDC",
    )
    gridbot.add_argument(
        "--include-fallback-candles",
        action="store_true",
        help="Use price_only_fallback candles. Without this, strict L2 mode requires book snapshots.",
    )
    gridbot.add_argument("--candle-path-assumption", choices=["low-first", "high-first"], default="low-first")
    gridbot.add_argument("--start", help="UTC inclusive start filter")
    gridbot.add_argument("--end", help="UTC exclusive end filter")
    gridbot.add_argument("--provider", action="append", help="Fallback candle provider filter")
    gridbot.add_argument("--max-rows", type=int, help="Maximum candle or audited book rows to load")
    gridbot.add_argument(
        "--l2-run-id",
        help="Explicit audited reconstruction run for strict L2 mode",
    )
    gridbot.add_argument(
        "--l2-window-id",
        help="Explicit strict-L2-eligible window inside the selected run",
    )
    gridbot.add_argument(
        "--l2-latency-ms",
        type=int,
        default=100,
        help="Deterministic decision-to-arrival latency for strict L2 orders (default: 100 ms)",
    )
    gridbot.add_argument(
        "--l2-clock-source",
        choices=["message_ts", "recv_ts"],
        default="message_ts",
        help="Single audited timestamp field used for strict L2 latency; fields are never mixed",
    )
    gridbot.add_argument("--derived-root", type=_path, help="Derived output root")
    gridbot.add_argument("--catalog-root", type=_path, help="Catalog output root")

    reserve_gridbot = subparsers.add_parser(
        "gridbot-reserve-backtest",
        help="Run the research-only banded lot gridbot with principal recovery and tagged reserve",
    )
    reserve_gridbot.add_argument("--product", required=True, help="XRP-USD only in reserve-grid v1")
    reserve_gridbot.add_argument("--lower", required=True, help="Inclusive master range lower price")
    reserve_gridbot.add_argument("--upper", required=True, help="Exclusive master range upper price")
    reserve_gridbot.add_argument("--band-width", required=True, help="Width of each contiguous half-open band")
    reserve_gridbot.add_argument("--levels-per-band", type=int, required=True)
    reserve_gridbot.add_argument(
        "--band-active-lot-budget-cap",
        required=True,
        help="Maximum all-in cash cost awaiting recovery in each band; not a separate deposit",
    )
    reserve_gridbot.add_argument("--quote-start", required=True, help="One real shared starting cash balance")
    reserve_gridbot.add_argument("--exit-move-pct", required=True, help="Exit movement as a decimal, such as 0.05")
    reserve_gridbot.add_argument(
        "--cash-profit-bps",
        required=True,
        help="Positive cash-flow profit target in basis points of each lot's all-in cost",
    )
    reserve_gridbot.add_argument(
        "--overflow-global-active-lot-budget-cap",
        default="0",
        help=(
            "Optional global all-in cap for one duplicate overflow tranche per level; "
            "uses the shared cash wallet and 0 disables it"
        ),
    )
    reserve_gridbot.add_argument(
        "--entry-guard",
        choices=["none", "ema_cross"],
        default="none",
        help=(
            "Optional causal EMA-cross buy-entry guard; it uses only "
            "completed prior one-minute closes and never blocks exits"
        ),
    )
    reserve_gridbot.add_argument(
        "--entry-guard-fast-ema-span-candles",
        type=int,
        default=360,
        help="Fast EMA span in one-minute candles for the optional entry guard",
    )
    reserve_gridbot.add_argument(
        "--entry-guard-slow-ema-span-candles",
        type=int,
        default=1440,
        help=(
            "Slow EMA span and required signal-only pre-roll observations for "
            "the optional entry guard"
        ),
    )
    reserve_gridbot.add_argument(
        "--exit-policy",
        choices=["principal_recovery", "full_lot"],
        default="principal_recovery",
        help="principal_recovery retains residual reserve; full_lot is the cash-exit control",
    )
    reserve_gridbot.add_argument("--base-increment", default="0.000001")
    reserve_gridbot.add_argument(
        "--quote-increment",
        default="0.01",
        help="Cash-allocation granularity for each slot; not a price or settlement increment",
    )
    reserve_gridbot.add_argument(
        "--price-increment",
        default="0.0001",
        help="Explicit research price increment; captured status rows do not prove historical price ticks",
    )
    reserve_gridbot.add_argument("--min-quote-notional", default="1")
    reserve_gridbot.add_argument("--fee-rate", default="0.006", help="Manual fallback gross fee rate")
    reserve_gridbot.add_argument(
        "--fee-source",
        choices=["auto", "manual", "coinbase"],
        default="manual",
        help="manual is the replay-stable default; auto/coinbase use a current tier, not a historical one",
    )
    reserve_gridbot.add_argument("--maker-fee-rate", help="Manual maker fee rate override")
    reserve_gridbot.add_argument("--taker-fee-rate", help="Manual taker fee rate override")
    reserve_gridbot.add_argument(
        "--liquidity-assumption",
        choices=["maker", "taker"],
        default=default_liquidity_assumption(),
    )
    reserve_gridbot.add_argument(
        "--coinbase-one-advanced-rebate-rate",
        default=default_coinbase_one_rebate_rate(),
    )
    reserve_gridbot.add_argument(
        "--coinbase-one-monthly-rebate-cap",
        default=default_coinbase_one_rebate_cap(),
    )
    reserve_gridbot.add_argument(
        "--coinbase-one-monthly-rebate-used",
        default=default_coinbase_one_rebate_used(),
    )
    reserve_gridbot.add_argument(
        "--include-fallback-candles",
        action="store_true",
        help="Required in v1; strict L2 waits for validated book reconstruction",
    )
    reserve_gridbot.add_argument(
        "--candle-path-assumption",
        choices=["low-first", "high-first"],
        default="low-first",
    )
    reserve_gridbot.add_argument("--start", help="UTC inclusive start filter")
    reserve_gridbot.add_argument("--end", help="UTC exclusive end filter")
    reserve_gridbot.add_argument("--provider", action="append", help="Fallback candle provider filter")
    reserve_gridbot.add_argument("--max-rows", type=int)
    reserve_gridbot.add_argument("--derived-root", type=_path, help="Derived output root")
    reserve_gridbot.add_argument("--catalog-root", type=_path, help="Catalog output root")

    storage = subparsers.add_parser(
        "storage-audit",
        help="Audit raw and derived legacy file types without deleting anything",
    )
    storage.add_argument("--root", action="append", type=_path, required=True)
    storage.add_argument("--catalog-root", type=_path, help="Catalog output root")
    storage.add_argument("--sample-missing", type=int, default=25)

    coverage = subparsers.add_parser(
        "legacy-coverage",
        help="Audit raw coverage for old Parquet/Feather files, including shifted roll windows",
    )
    coverage.add_argument("--root", action="append", type=_path, required=True)
    coverage.add_argument("--catalog-root", type=_path, help="Catalog output root")
    coverage.add_argument("--roll-seconds", type=int, default=600)
    coverage.add_argument("--sample-missing", type=int, default=25)
    coverage.add_argument("--progress-every", type=int, default=50_000)

    cleanup = subparsers.add_parser(
        "cleanup-feather",
        help="Plan or delete legacy Feather files after checking for raw candidates",
    )
    cleanup.add_argument("--root", action="append", type=_path, required=True)
    cleanup.add_argument("--catalog-root", type=_path, help="Catalog output root")
    cleanup.add_argument("--mode", choices=["plan", "delete"], default="plan")
    cleanup.add_argument(
        "--coverage-required",
        choices=["any-raw-candidate", "exact-or-previous", "none"],
        default="any-raw-candidate",
        help="Which Feather files are eligible for deletion",
    )
    cleanup.add_argument("--roll-seconds", type=int, default=600)
    cleanup.add_argument("--progress-every", type=int, default=50_000)

    gaps = subparsers.add_parser(
        "raw-gaps",
        help="Find raw capture gaps from filename windows or file content boundaries",
    )
    gaps.add_argument("--raw-root", action="append", type=_path, required=True)
    gaps.add_argument("--catalog-root", type=_path, help="Catalog output root")
    gaps.add_argument("--mode", choices=["filename", "content"], default="filename")
    gaps.add_argument("--product", action="append", help="Product filter such as BTC-USD")
    gaps.add_argument("--roll-seconds", type=int, default=600)
    gaps.add_argument("--tolerance-seconds", type=int, default=90)
    gaps.add_argument("--max-files", type=int)
    gaps.add_argument("--max-gaps", type=int, default=1000)
    gaps.add_argument("--progress-every", type=int, default=10_000)

    read_check = subparsers.add_parser(
        "read-check",
        help="Probe one or a few raw/legacy files without moving or converting them",
    )
    read_check.add_argument("paths", nargs="+", type=_path)
    read_check.add_argument("--sample-records", type=int, default=2)
    read_check.add_argument("--scan-all", action="store_true", help="Read the whole JSONL file to find its last record")
    read_check.add_argument("--max-records", type=int, help="Stop after this many physical JSONL lines")

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    roots = DataRoots.from_env()

    if args.command == "inventory":
        raw_roots = args.raw_root or [roots.raw_root]
        result = run_inventory(
            raw_roots=raw_roots,
            catalog_root=args.catalog_root or roots.catalog_root,
            include_legacy_ws_folders=args.include_legacy_ws_folders,
            legacy_search_roots=args.legacy_search_root,
            sample_records=args.sample_records,
            max_files=args.max_files,
        )
    elif args.command == "centralize-legacy":
        result = asdict(
            centralize_legacy_ws_data(
                legacy_search_roots=args.legacy_search_root,
                raw_root=args.raw_root or roots.raw_root,
                catalog_root=args.catalog_root or roots.catalog_root,
                mode=args.mode,
                max_files=args.max_files,
                progress_every=args.progress_every,
            )
        )
    elif args.command == "move-stranded-sessions":
        result = asdict(
            move_stranded_coinbase_sessions(
                source_raw_roots=args.source_raw_root or [Path("data/raw")],
                raw_root=args.raw_root or roots.raw_root,
                catalog_root=args.catalog_root or roots.catalog_root,
                mode=args.mode,
                include_open_sessions=args.include_open_sessions,
            )
        )
    elif args.command == "normalize":
        raw_roots = args.raw_root or [roots.raw_root]
        result = normalize_roots(
            raw_roots=raw_roots,
            derived_root=args.derived_root or roots.derived_root,
            quarantine_root=args.quarantine_root or roots.quarantine_root,
            catalog_root=args.catalog_root or roots.catalog_root,
            include_legacy_ws_folders=args.include_legacy_ws_folders,
            legacy_search_roots=args.legacy_search_root,
            limit_files=args.limit_files,
            input_order=args.input_order,
            sequence_scope=args.sequence_scope,
            limit_records_per_file=args.limit_records_per_file,
            max_records=args.max_records,
            max_open_files=args.max_open_partitions,
        )
    elif args.command == "audit-normalization":
        result = audit_normalization(args.manifest)
        print(json.dumps(result, indent=2, sort_keys=True, default=str))
        return 0 if result["valid"] else 1
    elif args.command == "audit-collector-session":
        result = audit_collector_session(args.manifest)
        print(json.dumps(result, indent=2, sort_keys=True, default=str))
        return 0 if result["valid"] else 1
    elif args.command == "features":
        result = run_features(
            derived_root=args.derived_root or roots.derived_root,
            catalog_root=args.catalog_root or roots.catalog_root,
            product=args.product,
            trade_window=args.trade_window,
            normalization_dataset_id=args.normalization_dataset_id,
        )
    elif args.command == "reconstruct-book":
        result = run_book_reconstruction(
            raw_files=args.raw_file,
            derived_root=args.derived_root or roots.derived_root,
            catalog_root=args.catalog_root or roots.catalog_root,
            product=args.product,
            capture_stream_id=args.capture_stream_id,
            sequence_scope=args.sequence_scope,
            input_order=args.input_order,
            source_layout=args.source_layout,
            depth_limit=args.depth_limit,
            emit_every_l2_messages=args.emit_every_l2_messages,
            full_hash_sequences=args.full_hash_sequence,
            max_envelope_gap_seconds=args.max_envelope_gap_seconds,
            ticker_tolerance=args.ticker_tolerance,
            start=args.start,
            end=args.end,
            max_messages=args.max_messages,
            right_boundary_file=args.right_boundary_file,
        )
    elif args.command == "import-candles":
        result = import_candle_csv_files(
            inputs=args.input,
            product=args.product,
            derived_root=args.derived_root or roots.derived_root,
            catalog_root=args.catalog_root or roots.catalog_root,
            provider=args.provider,
            max_rows=args.max_rows,
        )
    elif args.command == "move-external-ohlcv":
        result = asdict(
            move_external_ohlcv_files(
                inputs=args.input,
                product=args.product,
                provider=args.provider,
                raw_root=args.raw_root or roots.raw_root,
                catalog_root=args.catalog_root or roots.catalog_root,
                mode=args.mode,
            )
        )
    elif args.command == "fetch-candles":
        result = {
            "products": [
                fetch_coinbase_exchange_candles(
                    product=product,
                    start=args.start,
                    end=args.end,
                    raw_root=args.raw_root or roots.raw_root,
                    derived_root=args.derived_root or roots.derived_root,
                    catalog_root=args.catalog_root or roots.catalog_root,
                    granularity_seconds=args.granularity_seconds,
                    provider=args.provider,
                    sleep_seconds=args.sleep_seconds,
                    timeout_seconds=args.timeout_seconds,
                )
                for product in args.product
            ]
        }
    elif args.command == "fee-profile":
        result = fee_profile_to_report(
            resolve_fee_profile(
                source=args.source,
                fee_rate=args.fee_rate,
                maker_fee_rate=args.maker_fee_rate,
                taker_fee_rate=args.taker_fee_rate,
                liquidity_assumption=args.liquidity_assumption,
                coinbase_one_advanced_rebate_rate=args.coinbase_one_advanced_rebate_rate,
                coinbase_one_monthly_rebate_cap=args.coinbase_one_monthly_rebate_cap,
                coinbase_one_monthly_rebate_used=args.coinbase_one_monthly_rebate_used,
            )
        )
    elif args.command == "gridbot-backtest":
        result = run_gridbot_backtest(
            derived_root=args.derived_root or roots.derived_root,
            catalog_root=args.catalog_root or roots.catalog_root,
            product=args.product,
            lower=args.lower,
            upper=args.upper,
            grid_count=args.grid_count,
            quote_start=args.quote_start,
            base_start=args.base_start,
            order_quote=args.order_quote,
            fee_rate=args.fee_rate,
            fee_source=args.fee_source,
            maker_fee_rate=args.maker_fee_rate,
            taker_fee_rate=args.taker_fee_rate,
            liquidity_assumption=args.liquidity_assumption,
            coinbase_one_advanced_rebate_rate=args.coinbase_one_advanced_rebate_rate,
            coinbase_one_monthly_rebate_cap=args.coinbase_one_monthly_rebate_cap,
            coinbase_one_monthly_rebate_used=args.coinbase_one_monthly_rebate_used,
            include_fallback_candles=args.include_fallback_candles,
            candle_path_assumption=args.candle_path_assumption,
            start=args.start,
            end=args.end,
            providers=tuple(args.provider or ()),
            max_rows=args.max_rows,
            l2_run_id=args.l2_run_id,
            l2_window_id=args.l2_window_id,
            l2_latency_ms=args.l2_latency_ms,
            l2_clock_source=args.l2_clock_source,
        )
    elif args.command == "gridbot-reserve-backtest":
        result = run_reserve_gridbot_backtest(
            derived_root=args.derived_root or roots.derived_root,
            catalog_root=args.catalog_root or roots.catalog_root,
            product=args.product,
            lower=args.lower,
            upper=args.upper,
            band_width=args.band_width,
            levels_per_band=args.levels_per_band,
            band_active_lot_budget_cap=args.band_active_lot_budget_cap,
            quote_start=args.quote_start,
            exit_move_pct=args.exit_move_pct,
            cash_profit_bps=args.cash_profit_bps,
            overflow_global_active_lot_budget_cap=(
                args.overflow_global_active_lot_budget_cap
            ),
            entry_guard=args.entry_guard,
            entry_guard_fast_ema_span_candles=(
                args.entry_guard_fast_ema_span_candles
            ),
            entry_guard_slow_ema_span_candles=(
                args.entry_guard_slow_ema_span_candles
            ),
            exit_policy=args.exit_policy,
            base_increment=args.base_increment,
            quote_increment=args.quote_increment,
            price_increment=args.price_increment,
            min_quote_notional=args.min_quote_notional,
            fee_rate=args.fee_rate,
            fee_source=args.fee_source,
            maker_fee_rate=args.maker_fee_rate,
            taker_fee_rate=args.taker_fee_rate,
            liquidity_assumption=args.liquidity_assumption,
            coinbase_one_advanced_rebate_rate=args.coinbase_one_advanced_rebate_rate,
            coinbase_one_monthly_rebate_cap=args.coinbase_one_monthly_rebate_cap,
            coinbase_one_monthly_rebate_used=args.coinbase_one_monthly_rebate_used,
            include_fallback_candles=args.include_fallback_candles,
            candle_path_assumption=args.candle_path_assumption,
            start=args.start,
            end=args.end,
            providers=tuple(args.provider or ()),
            max_rows=args.max_rows,
        )
    elif args.command == "storage-audit":
        result = run_storage_audit(
            roots=args.root,
            catalog_root=args.catalog_root or roots.catalog_root,
            sample_missing=args.sample_missing,
        )
    elif args.command == "legacy-coverage":
        result = run_legacy_coverage(
            roots=args.root,
            catalog_root=args.catalog_root or roots.catalog_root,
            roll_seconds=args.roll_seconds,
            sample_missing=args.sample_missing,
            progress_every=args.progress_every,
        )
    elif args.command == "cleanup-feather":
        result = run_feather_cleanup(
            roots=args.root,
            catalog_root=args.catalog_root or roots.catalog_root,
            mode=args.mode,
            coverage_required=args.coverage_required,
            roll_seconds=args.roll_seconds,
            progress_every=args.progress_every,
        )
    elif args.command == "raw-gaps":
        result = run_raw_gaps(
            roots=args.raw_root,
            catalog_root=args.catalog_root or roots.catalog_root,
            mode=args.mode,
            products=args.product,
            roll_seconds=args.roll_seconds,
            tolerance_seconds=args.tolerance_seconds,
            max_files=args.max_files,
            max_gaps=args.max_gaps,
            progress_every=args.progress_every,
        )
    elif args.command == "read-check":
        result = run_read_check(
            paths=args.paths,
            sample_records=args.sample_records,
            scan_all=args.scan_all,
            max_records=args.max_records,
        )
    else:
        parser.error(f"Unknown command: {args.command}")
        return 2

    print(json.dumps(result, indent=2, sort_keys=True, default=str))
    return 0
