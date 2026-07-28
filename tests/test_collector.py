from __future__ import annotations

import asyncio
import gzip
import json
import sys
import tempfile
import types
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import Mock, patch

try:
    import websockets  # noqa: F401
except ModuleNotFoundError:
    websocket_stub = types.ModuleType("websockets")
    websocket_stub.exceptions = types.SimpleNamespace(ConnectionClosedError=RuntimeError)
    sys.modules["websockets"] = websocket_stub

import coinbase_ws_stable_logger as logger
from moneyman.cli import main as cli_main
from moneyman.collector_audit import (
    CollectorAuditState,
    audit_collector_session,
    canonical_sha256,
    capture_collector_provenance,
    closed_file_evidence,
    file_sha256,
)


def _provenance(
    raw_root: Path,
    *,
    products: list[str] | None = None,
    channels: list[str] | None = None,
) -> dict[str, object]:
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
        "products": products or ["XRP-USD", "BTC-USD"],
        "channels": channels or ["ticker", "heartbeats"],
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


def _message(
    sequence_num: int,
    recv_ts: str,
    *,
    channel: str = "ticker",
    latency_ms: float = 5.0,
) -> dict[str, object]:
    return {
        "channel": channel,
        "sequence_num": sequence_num,
        "timestamp": recv_ts,
        "_channel": channel,
        "_recv_ts": recv_ts,
        "_latency_ms": latency_ms,
        "events": [],
    }


class CollectorAuditStateTests(unittest.TestCase):
    def test_capture_provenance_binds_project_config_and_websockets_version(self) -> None:
        source_path = Path(logger.__file__).resolve()
        with (
            patch(
                "moneyman.collector_audit.distribution_version",
                return_value="15.0.1",
            ),
            patch(
                "moneyman.collector_audit._run_git",
                side_effect=[("f" * 40, None), ("", None)],
            ),
        ):
            provenance = capture_collector_provenance(source_path, {}, None)

        sources = {
            source["role"]: source
            for source in provenance["collector_provenance"]["execution_sources"]
        }
        project_config = source_path.parent / "moneyman" / "config.py"
        self.assertEqual(sources["project_config"]["path"], str(project_config))
        self.assertEqual(sources["project_config"]["sha256"], file_sha256(project_config))
        self.assertEqual(
            provenance["host_provenance"]["websockets_version"],
            "15.0.1",
        )

    def test_busy_nonheartbeat_traffic_cannot_mask_a_stale_heartbeat(self) -> None:
        session = Mock()
        session.channels = ["heartbeats", "ticker"]

        logger.enforce_heartbeat_freshness(
            session,
            last_heartbeat_monotonic=100.0,
            observed_monotonic=100.0 + logger.HEARTBEAT_DEAD_SECS,
            last_heartbeat_recv_ts="2026-01-01T00:00:00+00:00",
        )
        session.record_heartbeat_timeout.assert_not_called()

        with self.assertRaisesRegex(RuntimeError, "Missed heartbeats"):
            logger.enforce_heartbeat_freshness(
                session,
                last_heartbeat_monotonic=100.0,
                observed_monotonic=100.001 + logger.HEARTBEAT_DEAD_SECS,
                last_heartbeat_recv_ts="2026-01-01T00:00:00+00:00",
            )
        session.record_heartbeat_timeout.assert_called_once()
        call = session.record_heartbeat_timeout.call_args.kwargs
        self.assertAlmostEqual(
            call["stale_for_seconds"],
            logger.HEARTBEAT_DEAD_SECS + 0.001,
        )
        self.assertEqual(
            call["last_heartbeat_recv_ts"],
            "2026-01-01T00:00:00+00:00",
        )

        no_heartbeat_session = Mock()
        no_heartbeat_session.channels = ["ticker"]
        logger.enforce_heartbeat_freshness(
            no_heartbeat_session,
            last_heartbeat_monotonic=0.0,
            observed_monotonic=1_000.0,
            last_heartbeat_recv_ts=None,
        )
        no_heartbeat_session.record_heartbeat_timeout.assert_not_called()

    def test_sequence_heartbeat_latency_and_reconnect_are_per_epoch(self) -> None:
        state = CollectorAuditState(
            started_at="2026-01-01T00:00:00+00:00",
            heartbeat_dead_seconds=15,
        )
        state.start_connection(connected_at="2026-01-01T00:00:00+00:00")

        first = _message(0, "2026-01-01T00:00:01+00:00")
        exact_repeat = dict(first)
        exact_repeat["_recv_ts"] = "2026-01-01T00:00:02+00:00"
        conflicting_repeat = dict(first)
        conflicting_repeat["_recv_ts"] = "2026-01-01T00:00:03+00:00"
        conflicting_repeat["timestamp"] = "2026-01-01T00:00:03.5+00:00"
        gap = _message(3, "2026-01-01T00:00:04+00:00", latency_ms=-2)
        regression = _message(1, "2026-01-01T00:00:05+00:00")
        for payload in (first, exact_repeat, conflicting_repeat, gap, regression):
            state.observe_envelope(payload, observed_at=str(payload["_recv_ts"]))

        heartbeat_one = _message(
            4,
            "2026-01-01T00:00:10+00:00",
            channel="heartbeats",
        )
        heartbeat_one["events"] = [{"heartbeat_counter": 10, "current_time": "unchanged raw text"}]
        heartbeat_two = _message(
            5,
            "2026-01-01T00:00:30+00:00",
            channel="heartbeats",
        )
        heartbeat_two["events"] = [{"heartbeat_counter": 13}]
        state.observe_envelope(heartbeat_one, observed_at=str(heartbeat_one["_recv_ts"]))
        state.observe_envelope(heartbeat_two, observed_at=str(heartbeat_two["_recv_ts"]))
        state.record_heartbeat_timeout(
            observed_at="2026-01-01T00:00:50+00:00",
            stale_for_seconds=20,
            last_heartbeat_recv_ts=str(heartbeat_two["_recv_ts"]),
        )
        state.end_connection(
            ended_at="2026-01-01T00:01:00+00:00",
            disconnect_kind="fixture",
            reason="fixture reconnect",
            retry_delay_seconds=1,
        )
        state.start_connection(connected_at="2026-01-01T00:01:01+00:00")
        state.observe_envelope(
            _message(0, "2026-01-01T00:01:02+00:00"),
            observed_at="2026-01-01T00:01:02+00:00",
        )

        snapshot = state.snapshot()
        self.assertEqual(snapshot["successful_connection_count"], 2)
        self.assertEqual(snapshot["reconnect_count"], 1)
        self.assertEqual(snapshot["sequence_summary"]["sequence_gap_count"], 1)
        self.assertEqual(snapshot["sequence_summary"]["missing_sequence_count"], 2)
        self.assertEqual(snapshot["sequence_summary"]["sequence_duplicate_count"], 2)
        self.assertEqual(snapshot["sequence_summary"]["exact_sequence_duplicate_count"], 1)
        self.assertEqual(snapshot["sequence_summary"]["conflicting_sequence_duplicate_count"], 1)
        self.assertEqual(snapshot["sequence_summary"]["sequence_regression_count"], 1)
        self.assertEqual(snapshot["heartbeat_summary"]["stale_heartbeat_interval_count"], 1)
        self.assertEqual(snapshot["heartbeat_summary"]["heartbeat_timeout_count"], 1)
        self.assertEqual(snapshot["heartbeat_summary"]["missed_heartbeat_counter_count"], 2)
        self.assertEqual(snapshot["latency_summary"]["negative_sample_count"], 1)
        self.assertEqual(snapshot["connection_history"][1]["first_sequence_num"], 0)


class CollectorSessionTests(unittest.TestCase):
    def _closed_session(self, root: Path) -> Path:
        session = logger.CollectorSession(
            root,
            ["XRP-USD", "BTC-USD"],
            ["ticker", "heartbeats"],
            session_id="fixture-session",
            provenance=_provenance(root),
        )
        session.start_connection("2026-01-01T00:00:00+00:00")

        multi_product = {
            "channel": "ticker",
            "sequence_num": 0,
            "timestamp": "2026-01-01T00:00:01.123456789Z",
            "_channel": "ticker",
            "_recv_ts": "2026-01-01T00:00:01.200000+00:00",
            "_latency_ms": 76.544,
            "events": [
                {
                    "tickers": [
                        {"product_id": "XRP-USD", "price": "2.00"},
                        {"product_id": "BTC-USD", "price": "100000"},
                    ]
                }
            ],
        }
        session.record_received_frame()
        session.write_message(multi_product)

        heartbeat = {
            "channel": "heartbeats",
            "sequence_num": 2,
            "timestamp": "2026-01-01T00:00:02.123456789Z",
            "_channel": "heartbeats",
            "_recv_ts": "2026-01-01T00:00:02.200000+00:00",
            "_latency_ms": 76.544,
            "events": [{"heartbeat_counter": 100, "current_time": "raw-value"}],
        }
        session.record_received_frame()
        session.write_message(heartbeat)

        session.record_received_frame()
        session.write_malformed_frame(
            raw="{bad json",
            recv_ts="2026-01-01T00:00:03.200000+00:00",
            error="JSONDecodeError: fixture",
        )
        session.close("fixture_complete")
        return session.session_root / "manifest.json"

    def test_final_manifest_binds_identical_routes_and_audits_closed_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            manifest_path = self._closed_session(Path(tmp))
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

            self.assertEqual(manifest["status"], "closed")
            self.assertEqual(manifest["received_frame_count"], 3)
            self.assertEqual(manifest["received_envelope_count"], 2)
            self.assertEqual(manifest["message_count"], 2)
            self.assertEqual(manifest["parse_error_count"], 1)
            self.assertEqual(manifest["routed_write_count"], 4)
            self.assertEqual(manifest["sequence_summary"]["missing_sequence_count"], 1)
            self.assertTrue(manifest["session_end"]["all_writers_closed"])
            self.assertFalse((manifest_path.parent / ".manifest.json.tmp").exists())

            product_rows = []
            for product in ("BTC-USD", "XRP-USD"):
                raw_path = next((manifest_path.parent / f"product={product}").glob("*.jsonl.gz"))
                with gzip.open(raw_path, "rt", encoding="utf-8") as handle:
                    product_rows.append(handle.readline())
            self.assertEqual(product_rows[0], product_rows[1])
            routed_payload = json.loads(product_rows[0])
            self.assertEqual(
                routed_payload["_routed_destinations"],
                ["product=BTC-USD", "product=XRP-USD"],
            )
            self.assertNotIn("_routing_error", routed_payload)
            self.assertEqual(routed_payload["timestamp"], "2026-01-01T00:00:01.123456789Z")

            audit = audit_collector_session(manifest_path)
            self.assertTrue(audit["valid"], audit["errors"])
            self.assertEqual(audit["closed_files_verified"], 4)
            self.assertEqual(audit["routed_rows_verified"], 4)
            self.assertEqual(audit["received_envelopes_verified"], 2)
            with redirect_stdout(StringIO()):
                self.assertEqual(
                    cli_main(["audit-collector-session", "--manifest", str(manifest_path)]),
                    0,
                )

    def test_unsafe_network_routes_are_confined_to_fixed_review_route(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            session = logger.CollectorSession(
                root,
                ["XRP-USD", "BTC-USD"],
                ["ticker", "heartbeats"],
                session_id="unsafe-route-fixture",
                provenance=_provenance(root),
            )
            session.start_connection("2026-01-01T00:00:00+00:00")
            payloads = [
                _message(
                    0,
                    "2026-01-01T00:00:01+00:00",
                    channel="bad/../../escape",
                    latency_ms=0.0,
                ),
                _message(
                    1,
                    "2026-01-01T00:00:02+00:00",
                    channel=r"bad\..\..\escape",
                    latency_ms=0.0,
                ),
                _message(
                    2,
                    "2026-01-01T00:00:03+00:00",
                    channel="..",
                    latency_ms=0.0,
                ),
                {
                    **_message(
                        3,
                        "2026-01-01T00:00:04+00:00",
                        channel="ticker",
                        latency_ms=0.0,
                    ),
                    "events": [
                        {
                            "tickers": [
                                {
                                    "product_id": "../XRP-USD",
                                    "best_bid": "2.00",
                                    "best_ask": "2.01",
                                }
                            ]
                        }
                    ],
                },
            ]
            for payload in payloads:
                session.record_received_frame()
                session.write_message(payload)
            session.close("fixture_complete")

            manifest_path = session.session_root / "manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            self.assertEqual(manifest["status"], "closed")
            self.assertEqual(
                manifest["channel_message_counts"],
                {logger.INVALID_ROUTE_CHANNEL: len(payloads)},
            )
            raw_paths = list(root.rglob("*.jsonl.gz"))
            self.assertEqual(len(raw_paths), 1)
            for path in raw_paths:
                path.resolve().relative_to(session.session_root.resolve())
            self.assertEqual(raw_paths[0].parent.name, "channel=invalid_route")

            with gzip.open(raw_paths[0], "rt", encoding="utf-8") as handle:
                rows = [json.loads(line) for line in handle if line.strip()]
            self.assertEqual(len(rows), len(payloads))
            self.assertTrue(
                all(row["_routed_destinations"] == ["channel=invalid_route"] for row in rows)
            )
            self.assertEqual(
                [row["_routing_error"]["reason"] for row in rows],
                [
                    "invalid_channel_route",
                    "invalid_channel_route",
                    "invalid_channel_route",
                    "invalid_product_id",
                ],
            )
            self.assertEqual(rows[0]["channel"], "bad/../../escape")
            self.assertEqual(rows[1]["channel"], r"bad\..\..\escape")
            self.assertEqual(rows[2]["channel"], "..")
            self.assertEqual(rows[3]["events"][0]["tickers"][0]["product_id"], "../XRP-USD")

            audit = audit_collector_session(manifest_path)
            self.assertTrue(audit["valid"], audit["errors"])

    def test_strict_channel_routes_do_not_canonicalize_collisions(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            session = logger.CollectorSession(
                root,
                ["XRP-USD"],
                ["ticker"],
                session_id="route-collision-fixture",
                provenance=_provenance(
                    root,
                    products=["XRP-USD"],
                    channels=["ticker"],
                ),
            )
            session.start_connection("2026-01-01T00:00:00+00:00")
            payloads = [
                _message(
                    0,
                    "2026-01-01T00:00:01+00:00",
                    channel="foo_bar",
                    latency_ms=0.0,
                ),
                _message(
                    1,
                    "2026-01-01T00:00:02+00:00",
                    channel="foo-bar",
                    latency_ms=0.0,
                ),
            ]
            for payload in payloads:
                session.record_received_frame()
                session.write_message(payload)
            session.close("fixture_complete")

            manifest_path = session.session_root / "manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            self.assertEqual(
                manifest["channel_message_counts"],
                {"foo_bar": 1, logger.INVALID_ROUTE_CHANNEL: 1},
            )
            raw_paths = sorted(session.session_root.rglob("*.jsonl.gz"))
            self.assertEqual(
                [path.parent.name for path in raw_paths],
                ["channel=foo_bar", "channel=invalid_route"],
            )
            rows = {}
            for path in raw_paths:
                with gzip.open(path, "rt", encoding="utf-8") as handle:
                    rows[path.parent.name] = json.loads(handle.readline())
            self.assertEqual(rows["channel=foo_bar"]["channel"], "foo_bar")
            self.assertNotIn("_routing_error", rows["channel=foo_bar"])
            self.assertEqual(rows["channel=invalid_route"]["channel"], "foo-bar")
            self.assertEqual(
                rows["channel=invalid_route"]["_routing_error"]["reason"],
                "invalid_channel_route",
            )
            audit = audit_collector_session(manifest_path)
            self.assertTrue(audit["valid"], audit["errors"])

    def test_auditor_rejects_wrong_but_internally_consistent_destination(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            session = logger.CollectorSession(
                root,
                ["XRP-USD"],
                ["ticker"],
                session_id="wrong-route-fixture",
                provenance=_provenance(
                    root,
                    products=["XRP-USD"],
                    channels=["ticker"],
                ),
            )
            session.start_connection("2026-01-01T00:00:00+00:00")
            payload = {
                **_message(
                    0,
                    "2026-01-01T00:00:01+00:00",
                    channel="ticker",
                    latency_ms=0.0,
                ),
                "events": [
                    {
                        "tickers": [
                            {
                                "product_id": "XRP-USD",
                                "best_bid": "2.00",
                                "best_ask": "2.01",
                            }
                        ]
                    }
                ],
            }
            session.record_received_frame()
            session.write_message(payload)
            session.close("fixture_complete")

            manifest_path = session.session_root / "manifest.json"
            self.assertTrue(audit_collector_session(manifest_path)["valid"])
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            old_evidence = manifest["closed_files"][0]
            old_path = session.session_root / old_evidence["relative_path"]
            with gzip.open(old_path, "rt", encoding="utf-8") as handle:
                row = json.loads(handle.readline())
            row["_routed_destinations"] = ["channel=ticker"]

            new_path = session.session_root / "channel=ticker" / old_path.name
            new_path.parent.mkdir(parents=True)
            with gzip.open(new_path, "wt", encoding="utf-8", newline="\n") as handle:
                handle.write(json.dumps(row, sort_keys=True) + "\n")
            old_path.unlink()
            manifest["closed_files"] = [
                closed_file_evidence(
                    path=new_path,
                    session_root=session.session_root,
                    row_count=1,
                    opened_at=old_evidence["opened_at"],
                    closed_at=old_evidence["closed_at"],
                    first_recv_ts=old_evidence["first_recv_ts"],
                    last_recv_ts=old_evidence["last_recv_ts"],
                    first_event_ts=old_evidence["first_event_ts"],
                    last_event_ts=old_evidence["last_event_ts"],
                )
            ]
            manifest["product_message_counts"] = {"XRP-USD": 0}
            manifest["channel_message_counts"] = {"ticker": 1}
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

            audit = audit_collector_session(manifest_path)
            self.assertFalse(audit["valid"])
            self.assertIn("closed_file_0_expected_routing_invalid", audit["errors"])

    def test_invalid_configured_routes_fail_before_creating_a_session(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with self.assertRaisesRegex(ValueError, "invalid configured product route"):
                logger.CollectorSession(
                    root,
                    ["../XRP-USD"],
                    ["ticker"],
                    session_id="invalid-product",
                )
            with self.assertRaisesRegex(ValueError, "invalid configured channel route"):
                logger.CollectorSession(
                    root,
                    ["XRP-USD"],
                    [r"bad\..\channel"],
                    session_id="invalid-channel",
                )
            with self.assertRaisesRegex(ValueError, "duplicate configured product route"):
                logger.CollectorSession(
                    root,
                    ["XRP-USD", "XRP-USD"],
                    ["ticker"],
                    session_id="duplicate-product",
                )
            with self.assertRaisesRegex(ValueError, "duplicate configured channel route"):
                logger.CollectorSession(
                    root,
                    ["XRP-USD"],
                    ["ticker", "ticker"],
                    session_id="duplicate-channel",
                )
            with self.assertRaisesRegex(ValueError, "session_id must be one path component"):
                logger.CollectorSession(
                    root,
                    ["XRP-USD"],
                    ["ticker"],
                    session_id="../../escape",
                    provenance=_provenance(
                        root,
                        products=["XRP-USD"],
                        channels=["ticker"],
                    ),
                )
            self.assertFalse((root / "coinbase_advanced_trade").exists())

    def test_existing_session_symlink_cannot_escape_coinbase_root(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "raw"
            coinbase_root = root / "coinbase_advanced_trade"
            outside = Path(tmp) / "outside"
            coinbase_root.mkdir(parents=True)
            outside.mkdir()
            session_link = coinbase_root / "session=linked"
            try:
                session_link.symlink_to(outside, target_is_directory=True)
            except OSError as exc:
                self.skipTest(f"directory symlink unavailable: {exc}")

            with self.assertRaisesRegex(ValueError, "escapes session root"):
                logger.CollectorSession(
                    root,
                    ["XRP-USD"],
                    ["ticker"],
                    session_id="linked",
                    provenance=_provenance(
                        root,
                        products=["XRP-USD"],
                        channels=["ticker"],
                    ),
                )
            self.assertEqual(list(outside.iterdir()), [])

    def test_duplicate_session_id_fails_without_mutating_closed_session(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest_path = self._closed_session(root)
            before_manifest = manifest_path.read_bytes()
            before_raw = {
                path.relative_to(manifest_path.parent).as_posix(): path.read_bytes()
                for path in manifest_path.parent.rglob("*.jsonl.gz")
            }

            with self.assertRaises(FileExistsError):
                logger.CollectorSession(
                    root,
                    ["XRP-USD", "BTC-USD"],
                    ["ticker", "heartbeats"],
                    session_id="fixture-session",
                    provenance=_provenance(root),
                )

            self.assertEqual(manifest_path.read_bytes(), before_manifest)
            self.assertEqual(
                {
                    path.relative_to(manifest_path.parent).as_posix(): path.read_bytes()
                    for path in manifest_path.parent.rglob("*.jsonl.gz")
                },
                before_raw,
            )

    def test_auditor_rejects_missing_required_execution_source(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            manifest_path = self._closed_session(Path(tmp))
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            provenance = manifest["collector_provenance"]
            sources = [
                source
                for source in provenance["execution_sources"]
                if source["role"] != "project_config"
            ]
            provenance["execution_sources"] = sources
            provenance["execution_source_bundle_sha256"] = canonical_sha256(sources)
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

            audit = audit_collector_session(manifest_path)
            self.assertFalse(audit["valid"])
            self.assertIn(
                "execution_source_role_missing: project_config",
                audit["errors"],
            )

    def test_auditor_rejects_project_config_source_hash_tampering(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            manifest_path = self._closed_session(Path(tmp))
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            sources = manifest["collector_provenance"]["execution_sources"]
            project_config = next(
                source for source in sources if source["role"] == "project_config"
            )
            project_config["sha256"] = "0" * 64
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

            audit = audit_collector_session(manifest_path)
            self.assertFalse(audit["valid"])
            self.assertIn("execution_source_bundle_sha256_mismatch", audit["errors"])

    def test_auditor_requires_websockets_distribution_version(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            manifest_path = self._closed_session(Path(tmp))
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["host_provenance"]["websockets_version"] = ""
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

            audit = audit_collector_session(manifest_path)
            self.assertFalse(audit["valid"])
            self.assertIn(
                "host_provenance_websockets_version_missing",
                audit["errors"],
            )

    def test_public_auditor_fails_closed_on_malformed_manifest_shapes(self) -> None:
        def set_text_route_count(manifest: dict[str, object]) -> None:
            manifest["product_message_counts"]["XRP-USD"] = "many"

        def set_nul_relative_path(manifest: dict[str, object]) -> None:
            manifest["closed_files"][0]["relative_path"] = "bad\x00path.jsonl.gz"

        cases = (
            ("session_end_list", lambda manifest: manifest.__setitem__("session_end", []), "session_end_not_object"),
            (
                "product_count_map_list",
                lambda manifest: manifest.__setitem__("product_message_counts", []),
                "product_message_counts_not_object",
            ),
            (
                "text_route_count",
                set_text_route_count,
                "product_message_counts_value_not_nonnegative_integer: XRP-USD",
            ),
            (
                "connect_failures_none",
                lambda manifest: manifest.__setitem__("connect_failures", None),
                "connect_failures_not_list",
            ),
            (
                "sequence_summary_list",
                lambda manifest: manifest.__setitem__("sequence_summary", []),
                "sequence_summary_not_object",
            ),
            (
                "heartbeat_summary_list",
                lambda manifest: manifest.__setitem__("heartbeat_summary", []),
                "heartbeat_summary_not_object",
            ),
            (
                "latency_summary_list",
                lambda manifest: manifest.__setitem__("latency_summary", []),
                "latency_summary_not_object",
            ),
            (
                "text_received_count",
                lambda manifest: manifest.__setitem__("received_envelope_count", "two"),
                "received_envelope_count_not_nonnegative_integer",
            ),
            (
                "boolean_received_count",
                lambda manifest: manifest.__setitem__("received_frame_count", True),
                "received_frame_count_not_nonnegative_integer",
            ),
            (
                "nul_relative_path",
                set_nul_relative_path,
                "closed_file_0_relative_path_invalid: ValueError",
            ),
        )
        for name, mutate, expected_error in cases:
            with self.subTest(name=name), tempfile.TemporaryDirectory() as tmp:
                manifest_path = self._closed_session(Path(tmp))
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                mutate(manifest)
                manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

                audit = audit_collector_session(manifest_path)

                self.assertFalse(audit["valid"])
                self.assertIn(expected_error, audit["errors"])

    def test_auditor_rejects_coordinated_manifest_metric_tampering(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            manifest_path = self._closed_session(Path(tmp))
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["connection_history"][0]["missing_sequence_count"] = 999
            manifest["sequence_summary"]["missing_sequence_count"] = 999
            manifest["latency_summary"]["mean_ms"] = 999999.0
            manifest["latency_summary"]["p99_upper_bound_ms"] = -1000.0
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

            audit = audit_collector_session(manifest_path)
            self.assertFalse(audit["valid"])
            self.assertIn(
                "raw_connection_metric_mismatch: epoch=0 field=missing_sequence_count",
                audit["errors"],
            )
            self.assertIn("raw_latency_summary_mismatch", audit["errors"])
            with redirect_stdout(StringIO()):
                self.assertEqual(
                    cli_main(
                        ["audit-collector-session", "--manifest", str(manifest_path)]
                    ),
                    1,
                )

    def test_auditor_rejects_non_object_connection_history_row(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            manifest_path = self._closed_session(Path(tmp))
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["connection_history"][0] = "not-an-object"
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

            audit = audit_collector_session(manifest_path)
            self.assertFalse(audit["valid"])
            self.assertIn("connection_history_0_not_object", audit["errors"])

    def test_auditor_rejects_tampered_file_and_path_traversal(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            manifest_path = self._closed_session(Path(tmp))
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            first_file = manifest_path.parent / manifest["closed_files"][0]["relative_path"]
            first_file.write_bytes(first_file.read_bytes() + b"tamper")
            tampered = audit_collector_session(manifest_path)
            self.assertFalse(tampered["valid"])
            self.assertTrue(any("size_mismatch" in error for error in tampered["errors"]))
            with redirect_stdout(StringIO()):
                self.assertEqual(
                    cli_main(
                        ["audit-collector-session", "--manifest", str(manifest_path)]
                    ),
                    1,
                )

        with tempfile.TemporaryDirectory() as tmp:
            manifest_path = self._closed_session(Path(tmp))
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["closed_files"][0]["relative_path"] = "../outside.jsonl.gz"
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            traversal = audit_collector_session(manifest_path)
            self.assertFalse(traversal["valid"])
            self.assertTrue(any("outside_session" in error for error in traversal["errors"]))

    def test_auditor_rejects_unlisted_raw_file_and_counter_drift(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            manifest_path = self._closed_session(Path(tmp))
            extra = manifest_path.parent / "channel=extra" / "extra.jsonl"
            extra.parent.mkdir()
            extra.write_text("{}\n", encoding="utf-8")
            unlisted = audit_collector_session(manifest_path)
            self.assertFalse(unlisted["valid"])
            self.assertTrue(any("unlisted_raw_file" in error for error in unlisted["errors"]))

        with tempfile.TemporaryDirectory() as tmp:
            manifest_path = self._closed_session(Path(tmp))
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["channel_message_counts"]["malformed_json"] = 2
            manifest["routed_write_count"] += 1
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            drift = audit_collector_session(manifest_path)
            self.assertFalse(drift["valid"])
            self.assertIn("malformed_route_parse_error_mismatch", drift["errors"])

    def test_stale_busy_frame_is_written_before_reconnect(self) -> None:
        class FakeConnection:
            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, traceback):
                return False

            async def send(self, message: str) -> None:
                self.subscription = message

            async def recv(self) -> str:
                return json.dumps(
                    {
                        "channel": "ticker",
                        "sequence_num": 0,
                        "timestamp": "2026-01-01T00:00:00Z",
                        "events": [
                            {
                                "tickers": [
                                    {"product_id": "XRP-USD", "price": "2.00"}
                                ]
                            }
                        ],
                    }
                )

        async def cancel_retry(_seconds: float) -> None:
            raise asyncio.CancelledError

        def force_stale_reconnect(session, **_kwargs) -> None:
            session.record_heartbeat_timeout(
                stale_for_seconds=logger.HEARTBEAT_DEAD_SECS + 1,
                last_heartbeat_recv_ts=None,
            )
            raise RuntimeError("Missed heartbeats; reconnecting")

        with tempfile.TemporaryDirectory() as tmp:
            fake = FakeConnection()
            with (
                patch.object(logger, "RAW_ROOT", Path(tmp)),
                patch.object(logger, "PRODUCT_IDS", ["XRP-USD"]),
                patch.object(logger, "CHANNELS", ["ticker", "heartbeats"]),
                patch.object(
                    logger,
                    "enforce_heartbeat_freshness",
                    side_effect=force_stale_reconnect,
                ),
                patch.object(logger.random, "uniform", return_value=0.0),
                patch.object(logger.asyncio, "sleep", side_effect=cancel_retry),
                patch.object(logger.websockets, "connect", return_value=fake, create=True),
                patch.object(
                    logger,
                    "capture_collector_provenance",
                    return_value=_provenance(
                        Path(tmp),
                        products=["XRP-USD"],
                        channels=["ticker", "heartbeats"],
                    ),
                ),
            ):
                with self.assertRaises(asyncio.CancelledError):
                    asyncio.run(logger.subscribe_and_collect())

            manifest_path = next(Path(tmp).rglob("manifest.json"))
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            self.assertEqual(manifest["received_frame_count"], 1)
            self.assertEqual(manifest["received_envelope_count"], 1)
            self.assertEqual(manifest["message_count"], 1)
            self.assertEqual(manifest["routed_write_count"], 1)
            self.assertEqual(
                manifest["heartbeat_summary"]["heartbeat_timeout_count"],
                1,
            )
            audit = audit_collector_session(manifest_path)
            self.assertTrue(audit["valid"], audit["errors"])


class RollingWriterTests(unittest.TestCase):
    def test_multiple_same_minute_rolls_use_exclusive_unique_paths(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            session_root = Path(tmp) / "session=fixture"
            closed: list[dict[str, object]] = []
            with patch.object(logger, "ROLL_INTERVAL", 0):
                writer = logger.RollingJsonlWriter(
                    session_root / "channel=fixture",
                    "fixture",
                    session_root=session_root,
                    on_file_closed=closed.append,
                )
                writer.write({"_recv_ts": "2026-01-01T00:00:00+00:00"})
                writer.write({"_recv_ts": "2026-01-01T00:00:01+00:00"})
                writer.close()

            paths = [str(row["relative_path"]) for row in closed]
            self.assertEqual(len(paths), 3)
            self.assertEqual(len(set(paths)), 3)
            self.assertTrue(all("part-" in path for path in paths))
            self.assertEqual([int(row["row_count"]) for row in closed], [0, 1, 1])


if __name__ == "__main__":
    unittest.main()
