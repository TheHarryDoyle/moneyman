from __future__ import annotations

import gzip
import unittest
from decimal import Decimal
from pathlib import Path

from moneyman.candles import (
    fetch_coinbase_exchange_candles,
    import_candle_csv_files,
    move_external_ohlcv_files,
)
from moneyman.centralize import centralize_legacy_ws_data
from moneyman.cleanup import run_feather_cleanup
from moneyman.coinbase import session_id_from_path
from moneyman.coverage import run_legacy_coverage
from moneyman.features import calculate_feature_rows
from moneyman.fees import (
    FeeAccumulator,
    fee_profile_from_coinbase_transaction_summary,
    manual_fee_profile,
    resolve_fee_profile,
)
from moneyman.gaps import run_raw_gaps
from moneyman.gridbot import (
    GridbotConfig,
    load_fallback_candles,
    run_gridbot_backtest,
    simulate_gridbot_on_candles,
)
from moneyman.inventory import inspect_file, run_inventory
from moneyman.logger_config import load_logger_config
from moneyman.normalize import normalize_record
from moneyman.relocate import move_stranded_coinbase_sessions
from moneyman.probe import probe_file, run_read_check
from moneyman.raw import iter_jsonl


FIXTURE = Path(__file__).parent / "fixtures" / "sample_coinbase.jsonl"


class RawReaderTests(unittest.TestCase):
    def test_iter_jsonl_reports_payloads_and_errors(self) -> None:
        records = list(iter_jsonl(FIXTURE))
        self.assertEqual(len(records), 5)
        self.assertEqual(sum(1 for record in records if record.payload), 4)
        self.assertEqual(sum(1 for record in records if record.error), 1)

    def test_iter_jsonl_reads_gzip(self) -> None:
        gz_path = FIXTURE.parent / "sample_tmp.jsonl.gz"
        try:
            with gzip.open(gz_path, "wt", encoding="utf-8") as handle:
                handle.write(FIXTURE.read_text(encoding="utf-8"))
            records = list(iter_jsonl(gz_path, limit=2))
            self.assertEqual(len(records), 2)
            self.assertEqual(records[0].payload["channel"], "market_trades")
        finally:
            if gz_path.exists():
                gz_path.unlink()

    def test_read_check_reports_first_and_last_jsonl_records(self) -> None:
        result = probe_file(FIXTURE, sample_records=1, scan_all=True)
        self.assertEqual(result["format"], "jsonl")
        self.assertEqual(result["physical_lines_read"], 5)
        self.assertEqual(result["payload_records_read"], 4)
        self.assertEqual(result["parse_error_count_sampled"], 1)
        self.assertEqual(result["first_records"][0]["channel"], "market_trades")
        self.assertEqual(result["last_record"]["channel"], "l2_data")

        grouped = run_read_check([FIXTURE], sample_records=1, scan_all=True)
        self.assertEqual(grouped["file_count"], 1)


class InventoryTests(unittest.TestCase):
    def test_session_id_skips_canonical_legacy_container(self) -> None:
        path = Path("raw") / "legacy_ws_data" / "btc-usd_ws_data" / "11" / "btc_usd.jsonl.gz"
        self.assertEqual(session_id_from_path(path), "11")

    def test_inspect_file_samples_product_channel_and_parse_errors(self) -> None:
        entry = inspect_file(FIXTURE, sample_records=10)
        self.assertEqual(entry.likely_product, "BTC-USD")
        self.assertIn("market_trades", entry.channel)
        self.assertIn("l2_data", entry.channel)
        self.assertEqual(entry.sample_parse_errors, 1)
        self.assertIsNotNone(entry.estimated_rows)

    def test_run_inventory_writes_manifest(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            result = run_inventory([FIXTURE], Path(tmp), sample_records=10)
            self.assertEqual(result["file_count"], 1)
            self.assertTrue(Path(result["manifest_path"]).exists())


class CentralizeTests(unittest.TestCase):
    def test_centralize_legacy_moves_into_canonical_root(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            legacy = root / "btc-usd_ws_data" / "1"
            legacy.mkdir(parents=True)
            source = legacy / "btc_usd_test.jsonl"
            source.write_text(FIXTURE.read_text(encoding="utf-8"), encoding="utf-8")

            raw_root = root / "MoneyManData" / "raw"
            catalog_root = root / "MoneyManData" / "catalog"
            summary = centralize_legacy_ws_data(
                legacy_search_roots=[root],
                raw_root=raw_root,
                catalog_root=catalog_root,
                mode="move",
            )

            dest = raw_root / "legacy_ws_data" / "btc-usd_ws_data" / "1" / source.name
            self.assertEqual(summary.files_seen, 1)
            self.assertEqual(summary.files_moved, 1)
            self.assertFalse(source.exists())
            self.assertTrue(dest.exists())
            self.assertIn("market_trades", dest.read_text(encoding="utf-8"))
            self.assertTrue(Path(summary.catalog_manifest_path).exists())


class StrandedSessionMoveTests(unittest.TestCase):
    def test_move_stranded_closed_session_into_canonical_raw_root(self) -> None:
        import json
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source_raw = root / "repo" / "data" / "raw"
            session = source_raw / "coinbase_advanced_trade" / "session=20260709T010203Z"
            product = session / "product=BTC-USD"
            product.mkdir(parents=True)
            (session / "manifest.json").write_text(
                json.dumps({"session_id": "20260709T010203Z", "end_ts": "2026-07-09T01:12:03Z"}),
                encoding="utf-8",
            )
            (product / "btc_usd_2026-07-09_01-02.jsonl.gz").write_bytes(b"fake")

            raw_root = root / "MoneyManData" / "raw"
            catalog_root = root / "MoneyManData" / "catalog"
            summary = move_stranded_coinbase_sessions(
                source_raw_roots=[source_raw],
                raw_root=raw_root,
                catalog_root=catalog_root,
                mode="move",
            )

            dest = raw_root / "coinbase_advanced_trade" / session.name
            self.assertEqual(summary.sessions_seen, 1)
            self.assertEqual(summary.sessions_moved, 1)
            self.assertFalse(session.exists())
            self.assertTrue((dest / "manifest.json").exists())
            self.assertTrue((dest / "product=BTC-USD" / "btc_usd_2026-07-09_01-02.jsonl.gz").exists())
            self.assertTrue(Path(summary.catalog_manifest_path).exists())

    def test_move_stranded_open_session_is_skipped_by_default(self) -> None:
        import json
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source_raw = root / "repo" / "data" / "raw"
            session = source_raw / "coinbase_advanced_trade" / "session=20260709T010203Z"
            session.mkdir(parents=True)
            (session / "manifest.json").write_text(
                json.dumps({"session_id": "20260709T010203Z", "end_ts": None}),
                encoding="utf-8",
            )

            summary = move_stranded_coinbase_sessions(
                source_raw_roots=[source_raw],
                raw_root=root / "MoneyManData" / "raw",
                catalog_root=root / "MoneyManData" / "catalog",
                mode="move",
            )

            self.assertEqual(summary.sessions_seen, 1)
            self.assertEqual(summary.sessions_moved, 0)
            self.assertEqual(summary.sessions_skipped_open, 1)
            self.assertTrue(session.exists())

    def test_move_stranded_audited_close_failure_is_skipped(self) -> None:
        import json
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source_raw = root / "repo" / "data" / "raw"
            session = source_raw / "coinbase_advanced_trade" / "session=failed-close"
            session.mkdir(parents=True)
            (session / "manifest.json").write_text(
                json.dumps(
                    {
                        "manifest_schema": "moneyman.collector_session_manifest.v1",
                        "session_id": "failed-close",
                        "status": "close_failed",
                        "end_ts": "2026-07-09T01:12:03Z",
                        "session_end": {"all_writers_closed": False},
                    }
                ),
                encoding="utf-8",
            )

            summary = move_stranded_coinbase_sessions(
                source_raw_roots=[source_raw],
                raw_root=root / "MoneyManData" / "raw",
                catalog_root=root / "MoneyManData" / "catalog",
                mode="move",
            )

            self.assertEqual(summary.sessions_seen, 1)
            self.assertEqual(summary.sessions_moved, 0)
            self.assertEqual(summary.sessions_skipped_open, 1)
            self.assertTrue(session.exists())


class CoverageTests(unittest.TestCase):
    def test_legacy_coverage_detects_previous_window_raw_candidate(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            session = root / "btc-usd_ws_data" / "1"
            session.mkdir(parents=True)
            raw = session / "btc_usd_2025-01-01_00-00.jsonl.gz"
            derived = session / "btc_usd_2025-01-01_00-10.feather"
            raw.write_bytes(b"")
            derived.write_bytes(b"fake")

            result = run_legacy_coverage(
                roots=[root],
                catalog_root=root / "catalog",
                roll_seconds=600,
                progress_every=0,
            )

            feather = result["coverage_by_derived_type"][".feather"]
            self.assertEqual(feather["files"], 1)
            self.assertEqual(feather["exact_stem_raw_files"], 0)
            self.assertEqual(feather["previous_window_raw_files"], 1)
            self.assertEqual(feather["exact_or_previous_raw_files"], 1)
            self.assertEqual(feather["no_raw_candidate_files"], 0)


class CleanupTests(unittest.TestCase):
    def test_feather_cleanup_deletes_only_eligible_feather_files(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            session = root / "btc-usd_ws_data" / "1"
            session.mkdir(parents=True)
            raw = session / "btc_usd_2025-01-01_00-00.jsonl.gz"
            eligible_feather = session / "btc_usd_2025-01-01_00-10.feather"
            orphan_feather = root / "orphan.feather"
            parquet = session / "btc_usd_2025-01-01_00-10.parquet"
            raw.write_bytes(b"raw")
            eligible_feather.write_bytes(b"feather")
            orphan_feather.write_bytes(b"orphan")
            parquet.write_bytes(b"parquet")

            result = run_feather_cleanup(
                roots=[root],
                catalog_root=root / "catalog",
                mode="delete",
                coverage_required="any-raw-candidate",
                progress_every=0,
            )

            self.assertEqual(result["feather_files_seen"], 2)
            self.assertEqual(result["eligible_files"], 1)
            self.assertEqual(result["deleted_files"], 1)
            self.assertFalse(eligible_feather.exists())
            self.assertTrue(orphan_feather.exists())
            self.assertTrue(raw.exists())
            self.assertTrue(parquet.exists())
            self.assertTrue(Path(result["catalog_manifest_path"]).exists())


class GapTests(unittest.TestCase):
    def test_raw_gaps_detects_missing_filename_window(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            session = root / "btc-usd_ws_data" / "1"
            session.mkdir(parents=True)
            for timestamp in ("2025-01-01_00-00", "2025-01-01_00-10", "2025-01-01_00-40"):
                (session / f"btc_usd_{timestamp}.jsonl.gz").write_bytes(b"")

            result = run_raw_gaps(
                roots=[root],
                catalog_root=root / "catalog",
                mode="filename",
                roll_seconds=600,
                tolerance_seconds=0,
                progress_every=0,
            )

            btc = result["product_summaries"]["BTC-USD"]
            self.assertEqual(btc["gap_count"], 1)
            self.assertEqual(result["gaps"][0]["gap_seconds"], 1200.0)
            self.assertEqual(result["gaps"][0]["gap_start"], "2025-01-01T00:20:00Z")
            self.assertEqual(result["gaps"][0]["gap_end"], "2025-01-01T00:40:00Z")


class LoggerConfigTests(unittest.TestCase):
    def test_logger_config_reads_json_file(self) -> None:
        import json
        import os
        import tempfile
        from unittest.mock import patch

        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "logger.json"
            config_path.write_text(
                json.dumps(
                    {
                        "raw_root": str(Path(tmp) / "raw"),
                        "products": ["xrp-usd", "btc-usd"],
                        "channels": ["level2", "market_trades"],
                        "roll_interval_seconds": 30,
                        "progress_interval_messages": 10,
                    }
                ),
                encoding="utf-8",
            )

            with patch.dict(os.environ, {}, clear=True):
                config = load_logger_config(config_path)
            self.assertEqual(config.products, ["XRP-USD", "BTC-USD"])
            self.assertEqual(config.channels, ["level2", "market_trades"])
            self.assertEqual(config.roll_interval_seconds, 30)
            self.assertEqual(config.progress_interval_messages, 10)
            self.assertEqual(config.raw_root, Path(tmp) / "raw")


class CandleFallbackTests(unittest.TestCase):
    def test_import_candle_csv_labels_rows_as_price_only_fallback(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "xrp.csv"
            source.write_text(
                "time,open,high,low,close,volume\n"
                "2025-01-01 00:00:00+00:00,2.10,2.20,2.00,2.15,100\n",
                encoding="utf-8",
            )

            result = import_candle_csv_files(
                inputs=[source],
                product="XRP-USD",
                derived_root=root / "derived",
                catalog_root=root / "catalog",
                provider="fixture",
            )

            output = Path(result["output_path"])
            rows = list(iter_jsonl(output))
            self.assertEqual(result["report"]["rows_written"], 1)
            self.assertEqual(rows[0].payload["source_kind"], "price_only_fallback")
            self.assertEqual(rows[0].payload["product_id"], "XRP-USD")
            self.assertIn("cannot replace missing L2", result["report"]["limitations"][1])

    def test_move_external_ohlcv_moves_original_source_file(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "Downloads" / "XRP_1m.csv"
            source.parent.mkdir()
            source.write_text(
                "time,open,high,low,close,volume\n"
                "2025-01-01T00:00:00Z,2.10,2.20,2.00,2.15,100\n",
                encoding="utf-8",
            )

            summary = move_external_ohlcv_files(
                inputs=[source],
                product="XRP-USD",
                provider="fixture",
                raw_root=root / "MoneyManData" / "raw",
                catalog_root=root / "MoneyManData" / "catalog",
                mode="move",
            )

            destination = (
                root
                / "MoneyManData"
                / "raw"
                / "external_ohlcv"
                / "product=XRP-USD"
                / "provider=fixture"
                / source.name
            )
            self.assertEqual(summary.files_moved, 1)
            self.assertFalse(source.exists())
            self.assertTrue(destination.exists())
            self.assertTrue(Path(summary.catalog_manifest_path).exists())

    def test_fetch_coinbase_exchange_candles_writes_raw_and_fallback_rows(self) -> None:
        import tempfile

        calls = []

        def fake_fetch(url: str, timeout_seconds: int):
            calls.append((url, timeout_seconds))
            if len(calls) == 1:
                return [
                    [1735689600, "2.00", "2.20", "2.10", "2.15", "100"],
                    [1735689660, "2.14", "2.30", "2.15", "2.25", "110"],
                ]
            return [[1735689720, "2.24", "2.40", "2.25", "2.35", "120"]]

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            result = fetch_coinbase_exchange_candles(
                product="XRP-USD",
                start="2025-01-01T00:00:00Z",
                end="2025-01-01T00:03:00Z",
                raw_root=root / "raw",
                derived_root=root / "derived",
                catalog_root=root / "catalog",
                max_candles_per_request=2,
                fetch_json=fake_fetch,
            )

            raw_rows = list(iter_jsonl(Path(result["raw_output_path"])))
            fallback_rows = list(iter_jsonl(Path(result["derived_output_path"])))
            self.assertEqual(len(calls), 2)
            self.assertEqual(result["report"]["rows_written"], 3)
            self.assertEqual(result["report"]["missing_candles"], 0)
            self.assertEqual(raw_rows[0].payload["source_kind"], "external_ohlcv_raw")
            self.assertEqual(fallback_rows[0].payload["source_kind"], "price_only_fallback")
            self.assertEqual(fallback_rows[0].payload["source_path"], str(Path(result["raw_output_path"]).resolve()))
            self.assertTrue(Path(result["report_path"]).exists())


class FeeModelTests(unittest.TestCase):
    def test_coinbase_transaction_summary_sets_maker_and_taker_rates(self) -> None:
        profile = fee_profile_from_coinbase_transaction_summary(
            {
                "fee_tier": {
                    "pricing_tier": "$10k-$50k",
                    "maker_fee_rate": "0.004",
                    "taker_fee_rate": "0.006",
                }
            },
            liquidity_assumption="maker",
            coinbase_one_advanced_rebate_rate="0.25",
            coinbase_one_monthly_rebate_cap="100",
        )

        self.assertEqual(profile.source, "coinbase_transaction_summary")
        self.assertEqual(profile.maker_fee_rate, "0.004")
        self.assertEqual(profile.taker_fee_rate, "0.006")
        self.assertEqual(profile.pricing_tier, "$10k-$50k")

    def test_coinbase_one_rebate_caps_net_fees(self) -> None:
        profile = manual_fee_profile(
            fee_rate="0.01",
            liquidity_assumption="maker",
            coinbase_one_advanced_rebate_rate="0.25",
            coinbase_one_monthly_rebate_cap="1",
        )
        fees = FeeAccumulator(profile)

        first = fees.quote(Decimal("200"), "maker")
        second = fees.quote(Decimal("300"), "maker")

        self.assertEqual(str(first.gross_fee_quote), "2.00")
        self.assertEqual(str(first.rebate_quote), "0.5000")
        self.assertEqual(str(second.gross_fee_quote), "3.00")
        self.assertEqual(str(second.rebate_quote), "0.5000")
        self.assertEqual(str(fees.rebates_quote), "1.0000")

    def test_auto_fee_profile_falls_back_without_credentials(self) -> None:
        profile = resolve_fee_profile(
            source="auto",
            fee_rate="0.006",
            liquidity_assumption="maker",
            fetch_summary=lambda: (_ for _ in ()).throw(RuntimeError("offline")),
        )

        self.assertEqual(profile.source, "manual_fallback")
        self.assertEqual(profile.source_status, "auto_pull_failed")
        self.assertTrue(profile.warnings)


class NormalizationTests(unittest.TestCase):
    def test_normalization_quarantine_and_features(self) -> None:
        trades = []
        l2_updates = []
        quarantine = []
        for record in iter_jsonl(FIXTURE):
            record_trades, record_l2, record_quarantine = normalize_record(record)
            trades.extend(record_trades)
            l2_updates.extend(record_l2)
            quarantine.extend(record_quarantine)

        self.assertEqual(len(trades), 2)
        self.assertEqual(len(l2_updates), 3)
        self.assertEqual(len(quarantine), 1)
        self.assertEqual(trades[0]["session_id"], None)
        self.assertEqual(l2_updates[0]["source_path"], str(FIXTURE.resolve()))

        features = calculate_feature_rows(trades, l2_updates, trade_window=10)
        self.assertGreaterEqual(len(features), 2)
        self.assertEqual(features[0]["midpoint"], "100.00")
        self.assertEqual(features[0]["spread"], "2.00")
        self.assertEqual(features[-1]["midpoint"], "100.50")
        self.assertEqual(features[-1]["spread"], "1.00")
        self.assertEqual(features[-1]["quality_status"], "ok")


class GridbotTests(unittest.TestCase):
    def test_fallback_tail_loader_evicts_old_rows_and_preserves_first_duplicate(self) -> None:
        import json
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            candle_dir = root / "derived" / "v1" / "candles_fallback"
            candle_dir.mkdir(parents=True)

            def row(minute: int, close: str) -> dict[str, str]:
                return {
                    "start_ts": f"2025-01-01T00:0{minute}:00Z",
                    "product_id": "XRP-USD",
                    "open": close,
                    "high": close,
                    "low": close,
                    "close": close,
                    "volume": "1",
                    "source_kind": "price_only_fallback",
                    "source_provider": "fixture",
                    "source_path": "raw-fixture.jsonl",
                }

            first_path = candle_dir / "part_a.jsonl"
            second_path = candle_dir / "part_b.jsonl"
            with first_path.open("wt", encoding="utf-8", newline="\n") as handle:
                for candle in [row(0, "1.00"), row(1, "1.01"), row(2, "1.02"), row(3, "1.03")]:
                    handle.write(json.dumps(candle, sort_keys=True) + "\n")
            with second_path.open("wt", encoding="utf-8", newline="\n") as handle:
                for candle in [row(2, "9.99"), row(4, "1.04")]:
                    handle.write(json.dumps(candle, sort_keys=True) + "\n")

            candles, report = load_fallback_candles(
                derived_root=root / "derived",
                product_id="XRP-USD",
                providers=("fixture",),
                tail_rows=3,
            )

            self.assertEqual(
                [candle["start_ts"] for candle in candles],
                [
                    "2025-01-01T00:02:00Z",
                    "2025-01-01T00:03:00Z",
                    "2025-01-01T00:04:00Z",
                ],
            )
            self.assertEqual(candles[0]["close"], "1.02")
            self.assertEqual(report["candle_rows_selected_before_tail"], 5)
            self.assertEqual(report["duplicate_candle_timestamps_skipped"], 1)
            self.assertEqual(
                [source["selected_rows"] for source in report["selected_derived_sources"]],
                [2, 1],
            )

    def test_fallback_gridbot_reports_missed_sells_when_base_runs_out(self) -> None:
        candles = [
            {
                "start_ts": "2025-01-01T00:00:00Z",
                "product_id": "XRP-USD",
                "open": "3",
                "high": "6",
                "low": "1",
                "close": "5",
                "volume": "1000",
                "source_kind": "price_only_fallback",
                "source_provider": "fixture",
                "source_path": "fixture.jsonl",
            }
        ]
        config = GridbotConfig(
            product_id="XRP-USD",
            lower="1",
            upper="6",
            grid_count=5,
            quote_start="15",
            base_start="0",
            order_quote="10",
            fee_rate="0",
            include_fallback_candles=True,
            candle_path_assumption="low-first",
        )

        result = simulate_gridbot_on_candles(candles, config)

        self.assertEqual(result["summary"]["filled_buys"], 1)
        self.assertGreater(result["summary"]["missed_sells_insufficient_base"], 0)
        self.assertGreaterEqual(len(result["fills"]), 3)
        self.assertIn("Fallback-candle mode", result["summary"]["limitations"][0])

    def test_gridbot_reports_gross_rebate_and_net_fees(self) -> None:
        candles = [
            {
                "start_ts": "2025-01-01T00:00:00Z",
                "product_id": "XRP-USD",
                "open": "3",
                "high": "4",
                "low": "2",
                "close": "3.5",
                "volume": "1000",
                "source_kind": "price_only_fallback",
                "source_provider": "fixture",
                "source_path": "fixture.jsonl",
            }
        ]
        config = GridbotConfig(
            product_id="XRP-USD",
            lower="1",
            upper="5",
            grid_count=4,
            quote_start="100",
            base_start="1",
            order_quote="10",
            fee_rate="0.01",
            include_fallback_candles=True,
            candle_path_assumption="low-first",
            coinbase_one_advanced_rebate_rate="0.25",
            coinbase_one_monthly_rebate_cap="100",
        )

        result = simulate_gridbot_on_candles(candles, config)

        self.assertEqual(result["summary"]["fees_gross_quote"], "0.3")
        self.assertEqual(result["summary"]["fee_rebates_quote"], "0.075")
        self.assertEqual(result["summary"]["fees_net_quote"], "0.225")
        self.assertEqual(result["fills"][0]["fee_gross_quote"], "0.1")
        self.assertEqual(result["fills"][0]["fee_rebate_quote"], "0.025")

    def test_strict_l2_gridbot_requires_book_snapshots(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            result = run_gridbot_backtest(
                derived_root=root / "derived",
                catalog_root=root / "catalog",
                product="XRP-USD",
                lower="1",
                upper="2",
                grid_count=2,
                quote_start="100",
                base_start="0",
                order_quote="10",
                include_fallback_candles=False,
            )

            self.assertEqual(result["summary"]["status"], "requires_book_snapshots")
            self.assertEqual(result["summary"]["book_snapshot_files_found"], 0)
            self.assertTrue((Path(result["run_dir"]) / "summary.json").exists())

    def test_gridbot_backtest_loads_fallback_candles_and_writes_outputs(self) -> None:
        import json
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            candles_dir = root / "derived" / "v1" / "candles_fallback"
            candles_dir.mkdir(parents=True)
            candle_rows = [
                {
                    "start_ts": "2025-01-01T00:00:00Z",
                    "product_id": "XRP-USD",
                    "open": "3",
                    "high": "4",
                    "low": "2",
                    "close": "3.5",
                    "volume": "100",
                    "source_kind": "price_only_fallback",
                    "source_provider": "fixture",
                    "source_path": "fixture.jsonl",
                },
                {
                    "start_ts": "2025-01-01T00:01:00Z",
                    "product_id": "XRP-USD",
                    "open": "3.5",
                    "high": "5",
                    "low": "3",
                    "close": "4.5",
                    "volume": "110",
                    "source_kind": "price_only_fallback",
                    "source_provider": "fixture",
                    "source_path": "fixture.jsonl",
                },
            ]
            with (candles_dir / "part_fixture.jsonl").open("wt", encoding="utf-8", newline="\n") as handle:
                for row in candle_rows:
                    handle.write(json.dumps(row, sort_keys=True) + "\n")

            result = run_gridbot_backtest(
                derived_root=root / "derived",
                catalog_root=root / "catalog",
                product="XRP-USD",
                lower="1",
                upper="5",
                grid_count=4,
                quote_start="100",
                base_start="1",
                order_quote="10",
                fee_rate="0.001",
                include_fallback_candles=True,
                providers=("fixture",),
            )

            run_dir = Path(result["run_dir"])
            self.assertEqual(result["summary"]["status"], "completed")
            self.assertEqual(result["summary"]["run_id"], result["run_id"])
            self.assertEqual(result["summary"]["candle_input_report"]["candle_rows_loaded"], 2)
            self.assertEqual(
                result["summary"]["output_artifacts"]["fills"]["rows"],
                len(
                    [
                        line
                        for line in (run_dir / "fills.jsonl").read_text(
                            encoding="utf-8"
                        ).splitlines()
                        if line
                    ]
                ),
            )
            for artifact in result["summary"]["output_artifacts"].values():
                self.assertRegex(artifact["sha256"], r"^[0-9a-f]{64}$")
            self.assertTrue((run_dir / "config.json").exists())
            self.assertTrue((run_dir / "fills.jsonl").exists())
            self.assertTrue((run_dir / "equity_curve.jsonl").exists())
            self.assertTrue(Path(result["report_path"]).exists())


if __name__ == "__main__":
    unittest.main()
