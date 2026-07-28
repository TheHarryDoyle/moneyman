from __future__ import annotations

import json
import hashlib
import sys
import tempfile
import types
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch

try:
    import websockets  # noqa: F401
except ModuleNotFoundError:
    websocket_stub = types.ModuleType("websockets")
    websocket_stub.exceptions = types.SimpleNamespace(ConnectionClosedError=RuntimeError)
    sys.modules["websockets"] = websocket_stub

import coinbase_ws_stable_logger as logger
from moneyman.features import run_features
from moneyman.collector_audit import audit_collector_session, canonical_sha256
from moneyman.normalize import (
    SCHEMA_VERSION,
    audit_normalization,
    canonical_timestamp,
    normalize_envelope,
    normalize_files,
)
from moneyman.cli import main as cli_main
from moneyman.raw import RawRecord, read_jsonl_dicts


FIXTURE = Path(__file__).parent / "fixtures" / "coinbase_multichannel.jsonl"


def _fixture_payloads() -> dict[str, dict[str, object]]:
    payloads: dict[str, dict[str, object]] = {}
    for line in FIXTURE.read_text(encoding="utf-8").splitlines():
        payload = json.loads(line)
        payloads[str(payload["channel"])] = payload
    return payloads


def _write_jsonl(path: Path, payloads: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("wt", encoding="utf-8", newline="\n") as handle:
        for payload in payloads:
            handle.write(json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n")


def _collector_provenance(raw_root: Path) -> dict[str, object]:
    sources = [
        {"role": "collector", "path": "fixture_logger.py", "sha256": "a" * 64},
        {
            "role": "collector_audit",
            "path": "fixture_collector_audit.py",
            "sha256": "b" * 64,
        },
        {"role": "coinbase_helpers", "path": "fixture_coinbase.py", "sha256": "d" * 64},
        {"role": "logger_config", "path": "fixture_logger_config.py", "sha256": "e" * 64},
        {"role": "project_config", "path": "fixture_config.py", "sha256": "f" * 64},
    ]
    effective_config = {
        "ws_url": logger.WS_URL,
        "products": ["XRP-USD"],
        "channels": ["market_trades"],
        "raw_root": str(raw_root.resolve()),
        "heartbeat_dead_seconds": logger.HEARTBEAT_DEAD_SECS,
    }
    return {
        "collector_provenance": {
            "name": "fixture_logger.py",
            "version": "test",
            "source_path": "fixture_logger.py",
            "source_sha256": "a" * 64,
            "execution_sources": sources,
            "execution_source_bundle_sha256": canonical_sha256(sources),
            "git_commit": "c" * 40,
            "git_worktree_dirty": False,
            "git_probe_error": None,
        },
        "host_provenance": {
            "hostname": "fixture-host",
            "websockets_version": "fixture-websockets-1.0",
        },
        "config_provenance": {
            "effective_config": effective_config,
            "effective_config_sha256": canonical_sha256(effective_config),
            "config_path": None,
            "config_file_size_bytes": None,
            "config_file_sha256": None,
        },
    }


def _trade_message(
    sequence_num: int,
    recv_ts: str,
    *,
    trade_id: str,
    price: str,
) -> dict[str, object]:
    return {
        "channel": "market_trades",
        "sequence_num": sequence_num,
        "timestamp": recv_ts,
        "_channel": "market_trades",
        "_recv_ts": recv_ts,
        "_latency_ms": 0.0,
        "events": [
            {
                "trades": [
                    {
                        "trade_id": trade_id,
                        "product_id": "XRP-USD",
                        "price": price,
                        "size": "1",
                        "side": "BUY",
                        "time": recv_ts,
                    }
                ]
            }
        ],
    }


class TimestampTests(unittest.TestCase):
    def test_canonical_timestamp_preserves_nanoseconds_and_converts_offset(self) -> None:
        parsed = canonical_timestamp("2026-01-01T01:00:00.123456789+01:00")

        self.assertEqual(parsed.text, "2026-01-01T00:00:00.123456789Z")
        self.assertEqual(parsed.date, "2026-01-01")
        self.assertEqual(parsed.epoch_ns % 1_000_000_000, 123_456_789)

    def test_candle_epoch_seconds_are_canonical_utc(self) -> None:
        parsed = canonical_timestamp("1767225600", allow_epoch_seconds=True)

        self.assertEqual(parsed.text, "2026-01-01T00:00:00Z")
        self.assertEqual(parsed.epoch_ns, 1_767_225_600_000_000_000)


class EnvelopeNormalizationTests(unittest.TestCase):
    def test_malformed_sequence_values_are_quarantined(self) -> None:
        for sequence, expected_reason in (
            (True, "invalid_sequence_num_type"),
            (-1, "invalid_sequence_num_negative"),
            ("1", "invalid_sequence_num_type"),
        ):
            with self.subTest(sequence=sequence):
                payload = {
                    **_fixture_payloads()["market_trades"],
                    "sequence_num": sequence,
                }
                record = RawRecord(Path("fixture.jsonl"), 1, json.dumps(payload), payload, None)

                tables, quarantine, accounting = normalize_envelope(record, dataset_id="fixture")

                self.assertFalse(any(tables.values()))
                self.assertEqual([row["reason"] for row in quarantine], [expected_reason])
                self.assertEqual(accounting["semantic_items_seen"], 1)
                self.assertEqual(accounting["semantic_items_quarantined"], 1)

    def test_collector_parse_error_wrapper_is_preserved_in_quarantine(self) -> None:
        payload = {
            "_recv_ts": "2026-01-01T00:00:00Z",
            "_parsed_envelope": False,
            "_connection_epoch": 0,
            "_received_frame_ordinal": 1,
            "_routed_destinations": ["channel=malformed_json"],
            "parse_error": "bad json",
            "raw": "{bad",
        }
        record = RawRecord(Path("malformed.jsonl"), 1, json.dumps(payload), payload, None)

        tables, quarantine, accounting = normalize_envelope(record, dataset_id="fixture")

        self.assertFalse(any(tables.values()))
        self.assertEqual(quarantine[0]["reason"], "collector_parse_error_wrapper")
        self.assertEqual(quarantine[0]["item"], "{bad")
        self.assertEqual(accounting["collector_parse_error_items"], 1)

    def test_naive_trade_timestamp_is_quarantined_with_reconciliation(self) -> None:
        payload = {
            "channel": "market_trades",
            "sequence_num": 1,
            "_recv_ts": "2026-01-01T00:00:01Z",
            "events": [
                {
                    "trades": [
                        {
                            "trade_id": "bad-time",
                            "product_id": "BTC-USD",
                            "price": "100",
                            "size": "1",
                            "side": "BUY",
                            "time": "2026-01-01T00:00:00",
                        }
                    ]
                }
            ],
        }
        record = RawRecord(Path("fixture.jsonl"), 1, json.dumps(payload), payload, None)

        tables, quarantine, accounting = normalize_envelope(record, dataset_id="fixture")

        self.assertEqual(tables["trades"], [])
        self.assertEqual(len(quarantine), 1)
        self.assertIn("explicit offset", quarantine[0]["reason"])
        self.assertEqual(accounting["semantic_items_seen"], 1)
        self.assertEqual(accounting["semantic_items_quarantined"], 1)

    def test_top_level_error_without_events_or_event_time_is_control(self) -> None:
        payload = {
            "type": "error",
            "message": "authentication failure",
            "_recv_ts": "2025-08-02T16:00:00.123456789Z",
            "session_id": "2",
        }
        record = RawRecord(Path("xrp_legacy.jsonl"), 9, json.dumps(payload), payload, None)

        tables, quarantine, accounting = normalize_envelope(record, dataset_id="fixture")

        self.assertFalse(quarantine)
        self.assertEqual(len(tables["control"]), 1)
        self.assertIsNone(tables["control"][0]["event_ts"])
        self.assertEqual(tables["control"][0]["control_type"], "error")
        self.assertIn("authentication failure", tables["control"][0]["details_json"])
        self.assertEqual(accounting["semantic_items_seen"], accounting["semantic_items_emitted"])

    def test_falsy_nonlist_item_collections_are_quarantined(self) -> None:
        families = (
            ("market_trades", "trades", "trades_not_list"),
            ("l2_data", "updates", "l2_updates_not_list"),
            ("ticker", "tickers", "tickers_not_list"),
            ("candles", "candles", "candles_not_list"),
            ("status", "products", "status_products_not_list"),
        )
        for channel, collection, expected_reason in families:
            for malformed in (0, False, "", {}):
                with self.subTest(channel=channel, malformed=repr(malformed)):
                    payload = json.loads(json.dumps(_fixture_payloads()[channel]))
                    event = payload["events"][0]
                    event[collection] = malformed
                    payload["events"] = [event]
                    record = RawRecord(
                        Path("fixture.jsonl"),
                        1,
                        json.dumps(payload),
                        payload,
                        None,
                    )

                    tables, quarantine, accounting = normalize_envelope(
                        record,
                        dataset_id="fixture",
                    )

                    self.assertFalse(any(tables.values()))
                    self.assertEqual([row["reason"] for row in quarantine], [expected_reason])
                    self.assertEqual(accounting["semantic_items_seen"], 1)
                    self.assertEqual(accounting["semantic_items_quarantined"], 1)

    def test_present_falsy_item_product_ids_do_not_fall_back(self) -> None:
        families = (
            ("market_trades", "trades"),
            ("l2_data", "updates"),
            ("ticker", "tickers"),
            ("candles", "candles"),
            ("status", "products"),
        )
        for channel, collection in families:
            for malformed in (0, False, "", {}):
                with self.subTest(channel=channel, malformed=repr(malformed)):
                    payload = json.loads(json.dumps(_fixture_payloads()[channel]))
                    event = payload["events"][0]
                    item = event[collection][0]
                    if channel == "status":
                        item["id"] = malformed
                        item["product_id"] = "XRP-USD"
                    else:
                        item["product_id"] = malformed
                        event["product_id"] = "XRP-USD"
                    event[collection] = [item]
                    payload["events"] = [event]
                    record = RawRecord(
                        Path("fixture.jsonl"),
                        1,
                        json.dumps(payload),
                        payload,
                        None,
                    )

                    tables, quarantine, accounting = normalize_envelope(
                        record,
                        dataset_id="fixture",
                    )

                    self.assertFalse(any(tables.values()))
                    self.assertEqual([row["reason"] for row in quarantine], ["invalid_product_id"])
                    self.assertEqual(accounting["semantic_items_seen"], 1)
                    self.assertEqual(accounting["semantic_items_quarantined"], 1)
                    self.assertEqual(accounting["invalid_product_id_items"], 1)


class PartitionedNormalizationTests(unittest.TestCase):
    def _build_hardened_session(
        self,
        root: Path,
        *,
        session_id: str,
        payloads: list[dict[str, object]],
    ) -> tuple[list[Path], Path]:
        raw_root = root / "raw"
        with redirect_stdout(StringIO()):
            session = logger.CollectorSession(
                raw_root,
                ["XRP-USD"],
                ["market_trades"],
                session_id=session_id,
                provenance=_collector_provenance(raw_root),
            )
            session.start_connection("2026-01-01T00:00:00+00:00")
            for payload in payloads:
                session.record_received_frame()
                session.write_message(payload)
            session.close("fixture_complete")
        manifest_path = session.session_root / "manifest.json"
        collector_audit = audit_collector_session(manifest_path)
        self.assertTrue(collector_audit["valid"], collector_audit["errors"])
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        files = [Path(item["absolute_path"]) for item in manifest["closed_files"]]
        return files, manifest_path

    def _build_session(self, root: Path) -> tuple[list[Path], Path]:
        payloads = _fixture_payloads()
        session = root / "raw" / "coinbase_advanced_trade" / "session=fixture"
        btc = session / "product=BTC-USD" / "btc.jsonl"
        eth = session / "product=ETH-USD" / "eth.jsonl"
        subscriptions = session / "channel=subscriptions" / "subscriptions.jsonl"
        heartbeats = session / "channel=heartbeats" / "heartbeats.jsonl"
        status = session / "channel=status" / "status.jsonl"
        errors = session / "channel=error" / "error.jsonl"

        # The ticker envelope is an exact same-route duplicate in BTC and a routed
        # replica in ETH. It must emit its two semantic product rows exactly once.
        _write_jsonl(
            btc,
            [
                payloads["ticker"],
                payloads["ticker"],
                payloads["candles"],
                payloads["l2_data"],
                payloads["market_trades"],
            ],
        )
        _write_jsonl(eth, [payloads["ticker"], payloads["ticker_batch"]])
        _write_jsonl(subscriptions, [payloads["subscriptions"]])
        _write_jsonl(heartbeats, [payloads["heartbeats"]])
        _write_jsonl(status, [payloads["status"]])
        error_payload = {
            "type": "error",
            "message": "authentication failure",
            "_recv_ts": "2026-01-01T00:00:08.000000009Z",
            "sequence_num": 8,
        }
        _write_jsonl(errors, [error_payload])
        files = [btc, eth, subscriptions, heartbeats, status, errors]

        manifest = {
            "manifest_schema": "moneyman.collector_session_manifest.v1",
            "session_id": "fixture",
            "status": "closed",
            "collector": "coinbase_ws_stable_logger.py",
            "collector_provenance": {
                "name": "coinbase_ws_stable_logger.py",
                "version": "2.0",
                "git_commit": "abc123",
            },
            "host_provenance": {"hostname": "fixture-host", "system": "test"},
            "products": ["BTC-USD", "ETH-USD"],
            "channels": ["ticker", "ticker_batch", "candles", "l2_data", "market_trades", "heartbeats", "status"],
            "start_ts": "2026-01-01T00:00:00Z",
            "end_ts": "2026-01-01T00:01:00Z",
            "shutdown_reason": "normal_exit",
            "message_count": 9,
            "parse_error_count": 0,
            "sequence_summary": {
                "sequenced_envelope_count": 9,
                "unsequenced_envelope_count": 0,
                "malformed_sequence_count": 0,
                "sequence_gap_count": 0,
                "missing_sequence_count": 0,
                "sequence_duplicate_count": 0,
                "exact_sequence_duplicate_count": 0,
                "conflicting_sequence_duplicate_count": 0,
                "sequence_regression_count": 0,
            },
            "closed_files": [
                {
                    "absolute_path": str(path.resolve()),
                    "relative_path": path.relative_to(session).as_posix(),
                }
                for path in files
            ],
        }
        manifest_path = session / "manifest.json"
        manifest_path.write_text(json.dumps(manifest, sort_keys=True), encoding="utf-8")
        return files, manifest_path

    def test_streaming_partitions_quality_hashes_and_idempotent_reuse(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            files, manifest_path = self._build_session(root)
            raw_before = {path: path.read_bytes() for path in [*files, manifest_path]}
            derived = root / "derived"
            quarantine = root / "quarantine"
            catalog = root / "catalog"

            result = normalize_files(
                files,
                derived,
                quarantine,
                catalog,
                input_order="receive_time",
                sequence_scope="complete",
                max_open_files=2,
            )

            self.assertEqual(result["status"], "completed")
            quality = result["quality"]
            self.assertEqual(quality["schema_version"], SCHEMA_VERSION)
            self.assertEqual(quality["tables"]["trades"]["rows"], 2)
            self.assertEqual(quality["tables"]["l2_updates"]["rows"], 2)
            self.assertEqual(quality["tables"]["quotes"]["rows"], 3)
            self.assertEqual(quality["tables"]["candles"]["rows"], 2)
            self.assertEqual(quality["tables"]["heartbeats"]["rows"], 1)
            self.assertEqual(quality["tables"]["status"]["rows"], 2)
            self.assertEqual(quality["tables"]["control"]["rows"], 2)
            self.assertEqual(quality["tables"]["sessions"]["rows"], 1)
            self.assertEqual(quality["recognized_nonemitting"]["ticker_without_bbo"], 1)
            self.assertEqual(quality["duplicates"]["routing_replicas_collapsed"], 1)
            self.assertEqual(quality["duplicates"]["exact_transport_duplicates_collapsed"], 1)
            self.assertEqual(quality["reconciliation"]["input_records_error"], 0)
            self.assertEqual(quality["reconciliation"]["semantic_items_error"], 0)
            self.assertFalse(quality["ordering"]["connection_complete_claim"])
            self.assertIn(
                "collector_session_audit_not_valid",
                quality["ordering"]["complete_validation_failures"],
            )
            self.assertTrue(quality["source_coverage"]["raw_inputs_unchanged"])
            self.assertTrue(
                any("does not yet certify finiteness" in item for item in quality["limitations"])
            )
            self.assertEqual(
                quality["time_coverage"]["tables"]["candles"]["first_event_ts"],
                "2026-01-01T00:00:00Z",
            )
            self.assertEqual(
                quality["time_coverage"]["tables"]["candles"]["last_event_ts"],
                "2026-01-02T00:00:00Z",
            )

            candle_paths = [Path(item["path"]) for item in result["artifacts"] if item["table"] == "candles"]
            self.assertTrue(any("product=BTC-USD" in str(path) and "date=2026-01-01" in str(path) for path in candle_paths))
            self.assertTrue(any("product=ETH-USD" in str(path) and "date=2026-01-02" in str(path) for path in candle_paths))
            for artifact in result["artifacts"]:
                self.assertRegex(str(artifact["sha256"]), r"^[0-9a-f]{64}$")
                self.assertEqual(Path(artifact["path"]).stat().st_size, artifact["bytes"])

            session_artifact = next(item for item in result["artifacts"] if item["table"] == "sessions")
            session_row = read_jsonl_dicts(Path(session_artifact["path"]))[0]
            self.assertEqual(session_row["collector_version"], "2.0")
            self.assertEqual(session_row["git_commit"], "abc123")
            self.assertIn("fixture-host", session_row["host"])
            self.assertEqual(session_row["manifest_gap_count"], 0)
            self.assertEqual(session_row["manifest_duplicate_count"], 0)

            for path, expected in raw_before.items():
                self.assertEqual(path.read_bytes(), expected)

            part_paths_before = sorted(derived.rglob(f"part-{result['dataset_id']}.jsonl"))
            reused = normalize_files(
                files,
                derived,
                quarantine,
                catalog,
                input_order="receive_time",
                sequence_scope="complete",
                max_open_files=2,
            )
            self.assertEqual(reused["status"], "reused")
            self.assertEqual(sorted(derived.rglob(f"part-{result['dataset_id']}.jsonl")), part_paths_before)

    def test_execution_source_bundle_is_bound_and_dependency_tamper_invalidates_audit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            files, _ = self._build_session(root)
            result = normalize_files(
                files,
                root / "derived",
                root / "quarantine",
                root / "catalog",
                input_order="receive_time",
            )
            manifest_path = Path(result["manifest_path"])
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            bundle = manifest["normalizer_execution_source_bundle"]

            self.assertEqual(
                [source["module"] for source in bundle["sources"]],
                [
                    "moneyman.normalize",
                    "moneyman.collector_audit",
                    "moneyman.coinbase",
                    "moneyman.raw",
                    "moneyman.inventory",
                ],
            )
            self.assertTrue(audit_normalization(manifest_path)["valid"])

            for source in bundle["sources"]:
                source["path"] = f"D:/relocated/{source['module']}.py"
            manifest_path.write_text(json.dumps(manifest, sort_keys=True), encoding="utf-8")
            relocated = audit_normalization(manifest_path)
            self.assertTrue(relocated["valid"], relocated["errors"])
            self.assertEqual(
                len(
                    [
                        warning
                        for warning in relocated["warnings"]
                        if warning.startswith("normalizer_execution_source_path_drift:")
                    ]
                ),
                5,
            )

            collector_source = next(
                source
                for source in bundle["sources"]
                if source["module"] == "moneyman.collector_audit"
            )
            collector_source["sha256"] = "0" * 64
            portable_identity = {
                "schema": bundle["schema"],
                "sources": [
                    {
                        "module": source["module"],
                        "bytes": source["bytes"],
                        "sha256": source["sha256"],
                    }
                    for source in bundle["sources"]
                ],
            }
            bundle["bundle_sha256"] = hashlib.sha256(
                json.dumps(portable_identity, sort_keys=True, separators=(",", ":")).encode(
                    "utf-8"
                )
            ).hexdigest()
            manifest_path.write_text(json.dumps(manifest, sort_keys=True), encoding="utf-8")

            invalid = audit_normalization(manifest_path)
            self.assertFalse(invalid["valid"])
            self.assertIn("normalizer_execution_source_current_bundle_mismatch", invalid["errors"])
            self.assertIn("dataset_id_recalculation_mismatch", invalid["errors"])

    def test_complete_claim_is_reaudited_and_fails_if_collector_result_changes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            files, manifest_path = self._build_session(root)
            collector_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            for item in collector_manifest["closed_files"]:
                item["absolute_path"] = str(root / "old-location" / Path(item["relative_path"]).name)
            manifest_path.write_text(
                json.dumps(collector_manifest, sort_keys=True), encoding="utf-8"
            )
            collector_result = {
                "valid": True,
                "manifest_path": str(manifest_path.resolve()),
                "session_id": "fixture",
                "closed_files_verified": 6,
                "routed_rows_verified": 11,
                "received_envelopes_verified": 10,
                "errors": [],
                "warnings": [],
            }
            with patch(
                "moneyman.normalize.audit_collector_session", return_value=collector_result
            ):
                result = normalize_files(
                    files,
                    root / "derived",
                    root / "quarantine",
                    root / "catalog",
                    input_order="receive_time",
                    sequence_scope="complete",
                )
                valid = audit_normalization(Path(result["manifest_path"]))

            self.assertTrue(result["quality"]["ordering"]["connection_complete_claim"])
            self.assertTrue(valid["valid"], valid["errors"])
            self.assertTrue(valid["collector_reaudit_performed"])

            relocated_result = {
                **collector_result,
                "manifest_path": str(root / "relocated" / "manifest.json"),
                "warnings": ["closed_file_recorded_absolute_path_drift: fixture"],
            }
            with patch(
                "moneyman.normalize.audit_collector_session", return_value=relocated_result
            ):
                relocated = audit_normalization(Path(result["manifest_path"]))
            self.assertTrue(relocated["valid"], relocated["errors"])
            self.assertIn("complete_collector_manifest_path_drift", relocated["warnings"])
            self.assertTrue(
                any(
                    warning.startswith("complete_collector_reaudit_warning:")
                    for warning in relocated["warnings"]
                )
            )

            changed_result = {
                **collector_result,
                "valid": False,
                "errors": ["collector_contract_changed"],
            }
            with patch(
                "moneyman.normalize.audit_collector_session", return_value=changed_result
            ):
                invalid = audit_normalization(Path(result["manifest_path"]))

            self.assertFalse(invalid["valid"])
            self.assertIn("complete_collector_audit_summary_mismatch", invalid["errors"])
            self.assertIn(
                "complete_claim_ineligible: current_collector_audit_not_valid",
                invalid["errors"],
            )

    def test_conflicting_sequence_duplicate_cannot_claim_complete(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            files, _ = self._build_hardened_session(
                root,
                session_id="conflicting-sequence",
                payloads=[
                    _trade_message(
                        0,
                        "2026-01-01T00:00:00.100000+00:00",
                        trade_id="first",
                        price="2.00",
                    ),
                    _trade_message(
                        0,
                        "2026-01-01T00:00:00.200000+00:00",
                        trade_id="conflict",
                        price="2.01",
                    ),
                    _trade_message(
                        1,
                        "2026-01-01T00:00:00.300000+00:00",
                        trade_id="next",
                        price="2.02",
                    ),
                ],
            )
            result = normalize_files(
                files,
                root / "derived",
                root / "quarantine",
                root / "catalog",
                input_order="receive_time",
                sequence_scope="complete",
            )
            quality = result["quality"]
            failures = quality["ordering"]["complete_validation_failures"]

            self.assertFalse(quality["ordering"]["connection_complete_claim"])
            self.assertEqual(quality["counts"]["conflicting_sequence_duplicates"], 1)
            self.assertEqual(
                quality["duplicates"]["conflicting_sequence_duplicates_quarantined"],
                1,
            )
            self.assertEqual(quality["quarantine"]["rows"], 1)
            self.assertIn("conflicting_sequence_duplicate", failures)
            self.assertIn("collector_conflicting_sequence_duplicate", failures)
            self.assertTrue(
                quality["ordering"]["collector_session_audit"][
                    "normalizer_conflicting_sequence_duplicate_count_reconcile"
                ]
            )
            manifest_path = Path(result["manifest_path"])
            truthful_audit = audit_normalization(manifest_path)
            self.assertTrue(truthful_audit["valid"], truthful_audit["errors"])

            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            tampered_quality = manifest["quality"]
            tampered_quality["ordering"]["connection_complete_claim"] = True
            tampered_quality["ordering"]["complete_validation_failures"] = []
            tampered_quality["ordering"][
                "sequence_interpretation"
            ] = "connection_global_feed_continuity"
            tampered_quality["sequence"]["claim_scope"] = "connection_global"
            quality_path = Path(manifest["quality_path"])
            quality_path.write_text(
                json.dumps(tampered_quality, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
                newline="\n",
            )
            quality_artifact = next(
                artifact for artifact in manifest["artifacts"] if artifact["table"] == "_quality"
            )
            quality_artifact["bytes"] = quality_path.stat().st_size
            quality_artifact["sha256"] = hashlib.sha256(quality_path.read_bytes()).hexdigest()
            manifest_path.write_text(
                json.dumps(manifest, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
                newline="\n",
            )

            invalid = audit_normalization(manifest_path)
            self.assertFalse(invalid["valid"])
            self.assertIn(
                "complete_claim_ineligible: conflicting_sequence_duplicate",
                invalid["errors"],
            )
            self.assertIn(
                "complete_claim_ineligible: collector_conflicting_sequence_duplicate",
                invalid["errors"],
            )

    def test_collector_initial_sequence_gap_cannot_claim_complete(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            files, _ = self._build_hardened_session(
                root,
                session_id="initial-sequence-gap",
                payloads=[
                    _trade_message(
                        5,
                        "2026-01-01T00:00:00.100000+00:00",
                        trade_id="first",
                        price="2.00",
                    ),
                    _trade_message(
                        6,
                        "2026-01-01T00:00:00.200000+00:00",
                        trade_id="next",
                        price="2.01",
                    ),
                ],
            )
            result = normalize_files(
                files,
                root / "derived",
                root / "quarantine",
                root / "catalog",
                input_order="receive_time",
                sequence_scope="complete",
            )
            quality = result["quality"]
            failures = quality["ordering"]["complete_validation_failures"]
            collector_summary = quality["ordering"]["collector_session_audit"]

            self.assertEqual(quality["counts"]["sequence_gap_events"], 0)
            self.assertEqual(quality["counts"]["observed_missing_sequence_numbers"], 0)
            self.assertEqual(collector_summary["sequence_gap_count"], 1)
            self.assertEqual(collector_summary["missing_sequence_count"], 5)
            self.assertFalse(quality["ordering"]["connection_complete_claim"])
            self.assertIn("collector_sequence_gap", failures)
            self.assertIn("collector_missing_sequence", failures)
            public_audit = audit_normalization(Path(result["manifest_path"]))
            self.assertTrue(public_audit["valid"], public_audit["errors"])

    def test_bounded_slice_never_claims_complete_connection(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            files, _ = self._build_session(root)
            result = normalize_files(
                files,
                root / "derived",
                root / "quarantine",
                root / "catalog",
                input_order="receive_time",
                sequence_scope="complete",
                limit_records_per_file=1,
            )

            self.assertFalse(result["quality"]["ordering"]["connection_complete_claim"])
            self.assertIn("bounded_input", result["quality"]["ordering"]["complete_validation_failures"])

    def test_wrapper_and_malformed_sequences_reconcile_without_becoming_canonical(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            raw_path = root / "raw" / "coinbase_advanced_trade" / "session=bad" / "channel=malformed_json" / "bad.jsonl"
            wrapper = {
                "_recv_ts": "2026-01-01T00:00:00Z",
                "_parsed_envelope": False,
                "_connection_epoch": 0,
                "_received_frame_ordinal": 1,
                "_routed_destinations": ["channel=malformed_json"],
                "parse_error": "bad json",
                "raw": "{bad",
            }
            invalid_sequence = {
                **_fixture_payloads()["market_trades"],
                "sequence_num": False,
                "_recv_ts": "2026-01-01T00:00:01Z",
            }
            _write_jsonl(raw_path, [wrapper, invalid_sequence])

            result = normalize_files(
                [raw_path],
                root / "derived",
                root / "quarantine",
                root / "catalog",
                input_order="receive_time",
            )

            quality = result["quality"]
            self.assertEqual(quality["counts"]["collector_parse_error_wrappers"], 1)
            self.assertEqual(quality["counts"]["malformed_sequence_envelopes"], 1)
            self.assertEqual(quality["counts"]["canonical_envelopes"], 1)
            self.assertEqual(quality["quarantine"]["rows"], 2)
            self.assertTrue(all(value == 0 for value in quality["reconciliation"].values()))

    def test_v2_features_require_product_and_audited_single_product_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            files, _ = self._build_session(root)
            derived = root / "derived"
            catalog = root / "catalog"
            result = normalize_files(
                files,
                derived,
                root / "quarantine",
                catalog,
                input_order="receive_time",
            )

            with self.assertRaisesRegex(ValueError, "product is required"):
                run_features(
                    derived,
                    catalog,
                    normalization_dataset_id=result["dataset_id"],
                )

            features = run_features(
                derived,
                catalog,
                product="BTC-USD",
                normalization_dataset_id=result["dataset_id"],
            )
            self.assertTrue(features["report"]["normalization_audit"]["valid"])
            self.assertTrue(
                all("product=BTC-USD" in path for path in features["report"]["selected_sources"])
            )
            output_rows = read_jsonl_dicts(Path(features["output_path"]))
            self.assertLessEqual({row["product_id"] for row in output_rows}, {"BTC-USD"})

            artifact_path = next(
                Path(artifact["path"])
                for artifact in result["artifacts"]
                if artifact["table"] == "trades" and artifact["partition"]["product"] == "BTC-USD"
            )
            with artifact_path.open("at", encoding="utf-8") as handle:
                handle.write("tampered\n")
            with self.assertRaisesRegex(ValueError, "failed audit"):
                run_features(
                    derived,
                    catalog,
                    product="BTC-USD",
                    normalization_dataset_id=result["dataset_id"],
                )

    def test_public_audit_and_cli_reject_tampered_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            files, _ = self._build_session(root)
            result = normalize_files(
                files,
                root / "derived",
                root / "quarantine",
                root / "catalog",
                input_order="receive_time",
            )
            manifest_path = Path(result["manifest_path"])

            valid = audit_normalization(manifest_path)
            self.assertTrue(valid["valid"], valid["errors"])
            with redirect_stdout(StringIO()):
                self.assertEqual(
                    cli_main(["audit-normalization", "--manifest", str(manifest_path)]),
                    0,
                )

            artifact = next(
                Path(item["path"])
                for item in result["artifacts"]
                if item["table"] == "quotes"
            )
            with artifact.open("at", encoding="utf-8") as handle:
                handle.write("tampered\n")

            invalid = audit_normalization(manifest_path)
            self.assertFalse(invalid["valid"])
            self.assertTrue(
                any(error.startswith("artifact_sha256_mismatch") for error in invalid["errors"])
            )
            with redirect_stdout(StringIO()):
                self.assertEqual(
                    cli_main(["audit-normalization", "--manifest", str(manifest_path)]),
                    1,
                )

    def test_public_audit_returns_invalid_for_malformed_numeric_quality_fields(self) -> None:
        cases = (
            ("count", "quality_count_missing_or_invalid: input_records"),
            ("table_rows", "quality_table_rows_missing_or_invalid: trades"),
            ("quarantine_rows", "quality_quarantine_rows_missing_or_invalid"),
        )
        for case, expected_error in cases:
            with self.subTest(case=case), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                files, _ = self._build_session(root)
                result = normalize_files(
                    files,
                    root / "derived",
                    root / "quarantine",
                    root / "catalog",
                    input_order="receive_time",
                )
                manifest_path = Path(result["manifest_path"])
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                quality = manifest["quality"]
                if case == "count":
                    quality["counts"]["input_records"] = "not-an-integer"
                elif case == "table_rows":
                    quality["tables"]["trades"]["rows"] = "not-an-integer"
                else:
                    quality["quarantine"]["rows"] = "not-an-integer"

                quality_path = Path(manifest["quality_path"])
                quality_path.write_text(
                    json.dumps(quality, indent=2, sort_keys=True) + "\n",
                    encoding="utf-8",
                    newline="\n",
                )
                quality_artifact = next(
                    artifact
                    for artifact in manifest["artifacts"]
                    if artifact["table"] == "_quality"
                )
                quality_artifact["bytes"] = quality_path.stat().st_size
                quality_artifact["sha256"] = hashlib.sha256(
                    quality_path.read_bytes()
                ).hexdigest()
                manifest_path.write_text(
                    json.dumps(manifest, indent=2, sort_keys=True) + "\n",
                    encoding="utf-8",
                    newline="\n",
                )

                audit = audit_normalization(manifest_path)
                self.assertFalse(audit["valid"])
                self.assertIn(expected_error, audit["errors"])

    def test_public_audit_returns_invalid_for_manifest_paths_with_nul(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            files, _ = self._build_session(root)
            result = normalize_files(
                files,
                root / "derived",
                root / "quarantine",
                root / "catalog",
                input_order="receive_time",
            )
            manifest_path = Path(result["manifest_path"])
            original_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            cases = (
                ("artifact", "artifact_path_invalid:"),
                ("quality", "quality_path_invalid:"),
                ("session_manifest", "session_manifest_path_invalid:"),
            )
            for case, expected_error_prefix in cases:
                with self.subTest(case=case):
                    manifest = json.loads(json.dumps(original_manifest))
                    if case == "artifact":
                        manifest["artifacts"][0]["path"] = "invalid\x00artifact"
                    elif case == "quality":
                        manifest["quality_path"] = "invalid\x00quality"
                    else:
                        manifest["inputs"][0]["session_manifest"][
                            "path"
                        ] = "invalid\x00session-manifest"
                    manifest_path.write_text(
                        json.dumps(manifest, indent=2, sort_keys=True) + "\n",
                        encoding="utf-8",
                        newline="\n",
                    )

                    audit = audit_normalization(manifest_path)
                    self.assertFalse(audit["valid"])
                    self.assertTrue(
                        any(
                            error.startswith(expected_error_prefix)
                            for error in audit["errors"]
                        ),
                        audit["errors"],
                    )

    def test_legacy_sequence_reset_starts_new_inferred_epoch_before_dedup(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            session = root / "raw" / "legacy_ws_data" / "xrp-usd_ws_data" / "2"
            raw_path = session / "xrp.jsonl"
            first = {
                "channel": "market_trades",
                "timestamp": "2025-08-02T00:00:10Z",
                "_recv_ts": "2025-08-02T00:00:10.1Z",
                "sequence_num": 10,
                "events": [{"trades": [{"trade_id": "a", "product_id": "XRP-USD", "price": "3", "size": "1", "side": "BUY", "time": "2025-08-02T00:00:10Z"}]}],
            }
            reset = {
                "channel": "market_trades",
                "timestamp": "2025-08-02T00:00:11Z",
                "_recv_ts": "2025-08-02T00:00:11.1Z",
                "sequence_num": 0,
                "events": [{"trades": [{"trade_id": "b", "product_id": "XRP-USD", "price": "4", "size": "1", "side": "SELL", "time": "2025-08-02T00:00:11Z"}]}],
            }
            _write_jsonl(raw_path, [first, reset])

            result = normalize_files(
                [raw_path],
                root / "derived",
                root / "quarantine",
                root / "catalog",
                input_order="receive_time",
            )

            self.assertEqual(result["quality"]["tables"]["trades"]["rows"], 2)
            self.assertEqual(result["quality"]["sequence"]["regressions"], 1)
            self.assertEqual(result["quality"]["sequence"]["inferred_reconnect_boundaries"], 1)
            self.assertEqual(
                result["quality"]["duplicates"]["conflicting_sequence_duplicates_quarantined"],
                0,
            )

    def test_exchange_duplicate_conflict_and_embedded_epoch_are_distinct(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            session = root / "raw" / "coinbase_advanced_trade" / "session=duplicates"
            raw_path = session / "product=BTC-USD" / "btc.jsonl"

            def trade_payload(
                *, recv: str, frame: int, epoch: int, trade_id: str, price: str
            ) -> dict[str, object]:
                return {
                    "_connection_epoch": epoch,
                    "_received_frame_ordinal": frame,
                    "_recv_ts": recv,
                    "channel": "market_trades",
                    "timestamp": "2026-01-01T00:00:00Z",
                    "sequence_num": 5,
                    "events": [
                        {
                            "trades": [
                                {
                                    "trade_id": trade_id,
                                    "product_id": "BTC-USD",
                                    "price": price,
                                    "size": "1",
                                    "side": "BUY",
                                    "time": "2026-01-01T00:00:00Z",
                                }
                            ]
                        }
                    ],
                }

            first = trade_payload(
                recv="2026-01-01T00:00:00.100000001Z",
                frame=1,
                epoch=0,
                trade_id="same",
                price="100",
            )
            exact_later_receive = trade_payload(
                recv="2026-01-01T00:00:00.200000002Z",
                frame=2,
                epoch=0,
                trade_id="same",
                price="100",
            )
            conflict = trade_payload(
                recv="2026-01-01T00:00:00.300000003Z",
                frame=3,
                epoch=0,
                trade_id="conflict",
                price="101",
            )
            next_epoch = trade_payload(
                recv="2026-01-01T00:00:00.400000004Z",
                frame=4,
                epoch=1,
                trade_id="next-epoch",
                price="102",
            )
            _write_jsonl(raw_path, [first, exact_later_receive, conflict, next_epoch])

            result = normalize_files(
                [raw_path],
                root / "derived",
                root / "quarantine",
                root / "catalog",
                input_order="receive_time",
            )

            quality = result["quality"]
            self.assertEqual(quality["tables"]["trades"]["rows"], 2)
            self.assertEqual(quality["duplicates"]["exact_transport_duplicates_collapsed"], 1)
            self.assertEqual(quality["duplicates"]["conflicting_sequence_duplicates_quarantined"], 1)
            self.assertEqual(quality["quarantine"]["rows"], 1)
            self.assertEqual(quality["reconciliation"]["input_records_error"], 0)
            self.assertEqual(quality["reconciliation"]["semantic_items_error"], 0)

    def test_invalid_product_ids_are_quarantined_before_partitioning(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            raw_path = (
                root
                / "raw"
                / "coinbase_advanced_trade"
                / "session=invalid-products"
                / "channel=market_trades"
                / "trades.jsonl"
            )
            payload = {
                "channel": "market_trades",
                "sequence_num": 0,
                "timestamp": "2026-01-01T00:00:00Z",
                "_recv_ts": "2026-01-01T00:00:00.100000Z",
                "events": [
                    {
                        "trades": [
                            {
                                "trade_id": "slash",
                                "product_id": "A/B",
                                "price": "2.00",
                                "size": "1",
                                "side": "BUY",
                                "time": "2026-01-01T00:00:00Z",
                            },
                            {
                                "trade_id": "question",
                                "product_id": "A?B",
                                "price": "2.01",
                                "size": "1",
                                "side": "BUY",
                                "time": "2026-01-01T00:00:00Z",
                            },
                        ]
                    }
                ],
            }
            _write_jsonl(raw_path, [payload])

            result = normalize_files(
                [raw_path],
                root / "derived",
                root / "quarantine",
                root / "catalog",
                input_order="receive_time",
                sequence_scope="complete",
            )
            quality = result["quality"]

            self.assertEqual(quality["tables"]["trades"]["rows"], 0)
            self.assertEqual(quality["counts"]["invalid_product_id_items"], 2)
            self.assertEqual(quality["quarantine"]["reasons"], {"invalid_product_id": 2})
            self.assertIn(
                "invalid_product_id",
                quality["ordering"]["complete_validation_failures"],
            )
            self.assertFalse(any((root / "derived").rglob("product=A_B")))
            quarantine_rows = []
            for artifact in result["artifacts"]:
                if artifact["table"] == "_quarantine":
                    quarantine_rows.extend(read_jsonl_dicts(Path(artifact["path"])))
            self.assertEqual(
                {
                    json.loads(row["item_json"])["product_id"]
                    for row in quarantine_rows
                },
                {"A/B", "A?B"},
            )
            public_audit = audit_normalization(Path(result["manifest_path"]))
            self.assertTrue(public_audit["valid"], public_audit["errors"])

    def test_output_root_inside_raw_boundary_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            raw_path = (
                root
                / "raw"
                / "coinbase_advanced_trade"
                / "session=unsafe"
                / "product=BTC-USD"
                / "btc.jsonl"
            )
            _write_jsonl(raw_path, [_fixture_payloads()["market_trades"]])

            with self.assertRaisesRegex(ValueError, "raw session boundary"):
                normalize_files(
                    [raw_path],
                    root / "raw" / "derived",
                    root / "quarantine",
                    root / "catalog",
                )


if __name__ == "__main__":
    unittest.main()
