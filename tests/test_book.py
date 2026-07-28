from __future__ import annotations

import gzip
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from typing import Any

from moneyman.book import (
    audit_book_reconstruction_run,
    run_book_reconstruction,
)
from moneyman.gridbot import _strict_l2_contract_report, run_gridbot_backtest
from moneyman.l2_fills import (
    AuditedBookSelectionError,
    StrictL2FillConfig,
    load_audited_book_window,
)


def _timestamp(ordinal: int) -> str:
    return f"2025-08-01T21:21:{ordinal:02d}.000000Z"


def _update(
    side: str,
    price: str,
    quantity: str,
    *,
    product_id: str | None = None,
) -> dict[str, str]:
    row = {
        "side": side,
        "price_level": price,
        "new_quantity": quantity,
    }
    if product_id is not None:
        row["product_id"] = product_id
    return row


def _l2_event(
    event_type: str,
    updates: list[dict[str, str]],
    *,
    product_id: str = "XRP-USD",
    event_time: str | None = None,
) -> dict[str, Any]:
    row: dict[str, Any] = {
        "type": event_type,
        "product_id": product_id,
        "updates": updates,
    }
    if event_time is not None:
        row["event_time"] = event_time
    return row


def _envelope(
    sequence_num: int,
    channel: str,
    events: list[dict[str, Any]],
    *,
    ordinal: int | None = None,
    recv_ts: str | None = None,
) -> dict[str, Any]:
    timestamp = _timestamp(sequence_num if ordinal is None else ordinal)
    row: dict[str, Any] = {
        "channel": channel,
        "timestamp": timestamp,
        "sequence_num": sequence_num,
        "events": events,
    }
    if recv_ts is not None:
        row["_recv_ts"] = recv_ts
    return row


def _l2(
    sequence_num: int,
    event_type: str,
    updates: list[dict[str, str]],
    *,
    product_id: str = "XRP-USD",
    ordinal: int | None = None,
    recv_ts: str | None = None,
) -> dict[str, Any]:
    event_time = _timestamp(sequence_num if ordinal is None else ordinal)
    return _envelope(
        sequence_num,
        "l2_data",
        [
            _l2_event(
                event_type,
                updates,
                product_id=product_id,
                event_time=event_time,
            )
        ],
        ordinal=ordinal,
        recv_ts=recv_ts,
    )


def _ticker(
    sequence_num: int,
    best_bid: str,
    best_ask: str,
    *,
    ordinal: int | None = None,
) -> dict[str, Any]:
    return _envelope(
        sequence_num,
        "ticker",
        [
            {
                "type": "update",
                "tickers": [
                    {
                        "product_id": "XRP-USD",
                        "best_bid": best_bid,
                        "best_ask": best_ask,
                    }
                ],
            }
        ],
        ordinal=ordinal,
    )


def _basic_snapshot(sequence_num: int = 0, *, ordinal: int | None = None) -> dict[str, Any]:
    return _l2(
        sequence_num,
        "snapshot",
        [
            _update("bid", "100", "1"),
            _update("bid", "99", "2"),
            _update("offer", "101", "3"),
            _update("offer", "102", "4"),
        ],
        ordinal=ordinal,
    )


class BookReconstructionTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self._file_number = 0

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _write_rows(
        self,
        rows: list[dict[str, Any]],
        *,
        name: str | None = None,
        sort_keys: bool = True,
    ) -> Path:
        if name is None:
            self._file_number += 1
            name = f"raw-{self._file_number:02d}.jsonl"
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("wt", encoding="utf-8", newline="\n") as handle:
            for row in rows:
                handle.write(json.dumps(row, sort_keys=sort_keys) + "\n")
        return path

    def _write_gzip_rows(
        self,
        rows: list[dict[str, Any]],
        *,
        name: str,
    ) -> Path:
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        with gzip.open(path, "wt", encoding="utf-8", newline="\n") as handle:
            for row in rows:
                handle.write(json.dumps(row, sort_keys=True) + "\n")
        return path

    def _run(
        self,
        rows: list[dict[str, Any]],
        *,
        depth_limit: int = 25,
        name: str | None = None,
        capture_stream_id: str = "unit-test-stream",
        max_envelope_gap_seconds: str | None = None,
        emit_every_l2_messages: int = 1,
        full_hash_sequences: list[int] | None = None,
    ) -> dict[str, Any]:
        raw_path = self._write_rows(rows, name=name)
        return run_book_reconstruction(
            raw_files=[raw_path],
            derived_root=self.root / "derived",
            catalog_root=self.root / "catalog",
            product="XRP-USD",
            capture_stream_id=capture_stream_id,
            sequence_scope="complete",
            input_order="file",
            depth_limit=depth_limit,
            max_envelope_gap_seconds=max_envelope_gap_seconds,
            emit_every_l2_messages=emit_every_l2_messages,
            full_hash_sequences=full_hash_sequences,
        )

    def test_atomic_envelope_and_global_interleaved_sequence(self) -> None:
        snapshot = _l2(
            0,
            "snapshot",
            [
                _update("bid", "100", "1"),
                _update("bid", "99", "2"),
                _update("bid", "98", "3"),
                _update("offer", "101", "4"),
                _update("offer", "102", "5"),
                _update("offer", "103", "6"),
            ],
        )
        atomic_update = _envelope(
            2,
            "l2_data",
            [
                _l2_event(
                    "update",
                    [_update("buy", "102", "7")],
                    event_time=_timestamp(2),
                ),
                _l2_event(
                    "update",
                    [
                        _update("sell", "101", "0"),
                        _update("ask", "102", "0"),
                        _update("bid", "97", "0"),
                        _update("offer", "104", "8"),
                    ],
                    event_time=_timestamp(2),
                ),
            ],
        )

        result = self._run([snapshot, _ticker(1, "100", "101"), atomic_update])
        summary = result["summary"]

        self.assertEqual([row["sequence_num"] for row in summary_rows(result)], [0, 2])
        final = summary_rows(result)[-1]
        self.assertEqual(final["best_bid"], "102")
        self.assertEqual(final["best_ask"], "103")
        self.assertEqual(final["source_event_count"], 2)
        self.assertEqual(summary["counts"]["ticker_bbo_comparisons"], 1)
        self.assertEqual(summary["counts"]["ticker_bbo_exact_matches"], 1)
        self.assertEqual(summary["counts"]["zero_quantity_deletes"], 3)
        self.assertEqual(summary["counts"]["level_deletes"], 2)
        self.assertEqual(summary["counts"]["level_missing_deletes"], 1)
        self.assertEqual(summary["counts"].get("sequence_gap_events", 0), 0)
        self.assertNotIn("locked_book", summary["invalidation_reasons"])
        self.assertNotIn("crossed_book", summary["invalidation_reasons"])

    def test_gap_invalidity_is_sticky_until_a_fresh_snapshot(self) -> None:
        rows = [
            _basic_snapshot(0),
            _l2(1, "update", [_update("bid", "100", "2")]),
            _ticker(2, "100", "101"),
            _l2(4, "update", [_update("bid", "100", "3")], ordinal=3),
            _l2(5, "update", [_update("bid", "100", "4")], ordinal=4),
            _l2(
                6,
                "snapshot",
                [_update("bid", "90", "5"), _update("offer", "91", "6")],
                ordinal=5,
            ),
        ]

        result = self._run(rows)
        summary = result["summary"]
        snapshots = summary_rows(result)

        self.assertEqual([row["sequence_num"] for row in snapshots], [0, 1, 6])
        self.assertEqual(snapshots[-1]["best_bid"], "90")
        self.assertEqual(snapshots[-1]["best_ask"], "91")
        self.assertEqual(summary["invalidation_reasons"]["global_sequence_gap"], 1)
        self.assertEqual(summary["counts"]["ignored_updates_while_invalid"], 2)
        self.assertEqual(summary["valid_window_count"], 2)

    def test_unsequenced_l2_snapshot_cannot_validate(self) -> None:
        snapshot = _basic_snapshot(0)
        del snapshot["sequence_num"]

        result = self._run([snapshot])
        summary = result["summary"]

        self.assertEqual(summary["book_snapshot_rows"], 0)
        self.assertEqual(summary["valid_window_count"], 0)
        self.assertEqual(summary["counts"]["unsequenced_l2_envelopes"], 1)
        self.assertEqual(
            summary["invalidation_reasons"]["l2_envelope_missing_sequence"],
            1,
        )
        self.assertEqual(summary["counts"].get("validations", 0), 0)

    def test_missing_or_malformed_sequenced_timestamp_cannot_validate(self) -> None:
        missing = _basic_snapshot(0)
        del missing["timestamp"]
        malformed = _basic_snapshot(0)
        malformed["timestamp"] = "not-a-timestamp"

        for reason, row in (
            ("missing_envelope_timestamp", missing),
            ("malformed_envelope_timestamp", malformed),
        ):
            with self.subTest(reason=reason):
                result = self._run([row], name=f"{reason}.jsonl")
                summary = result["summary"]
                self.assertEqual(summary["book_snapshot_rows"], 0)
                self.assertEqual(summary["valid_window_count"], 0)
                self.assertEqual(summary["invalidation_reasons"][reason], 1)
                self.assertEqual(summary["counts"].get("validations", 0), 0)

    def test_stale_gap_can_recover_on_the_fresh_snapshot_that_exposes_it(self) -> None:
        rows = [
            _basic_snapshot(0, ordinal=0),
            _l2(
                1,
                "snapshot",
                [_update("bid", "90", "5"), _update("offer", "91", "6")],
                ordinal=3,
            ),
        ]

        result = self._run(rows, max_envelope_gap_seconds="1")
        summary = result["summary"]
        snapshots = summary_rows(result)

        self.assertEqual([row["sequence_num"] for row in snapshots], [0, 1])
        self.assertEqual(snapshots[-1]["best_bid"], "90")
        self.assertEqual(snapshots[-1]["best_ask"], "91")
        self.assertEqual(summary["counts"]["stale_envelope_gaps"], 1)
        self.assertEqual(summary["invalidation_reasons"]["stale_envelope_gap"], 1)
        self.assertEqual(summary["valid_window_count"], 2)

    def test_malformed_ticker_bbo_is_audited_without_invalidating_book(self) -> None:
        rows = [
            _basic_snapshot(0),
            _ticker(1, "not-a-number", "101"),
            _ticker(2, "102", "101"),
            _l2(3, "update", [_update("bid", "100", "2")]),
        ]

        result = self._run(rows)
        summary = result["summary"]
        rejected = [
            row
            for row in artifact_rows(result, "book_quality_events")
            if row["event"] == "ticker_bbo_rejected"
        ]

        self.assertEqual(summary["counts"]["malformed_ticker_bbo_rows"], 2)
        self.assertEqual(summary["counts"].get("invalidations", 0), 0)
        self.assertEqual(summary["valid_window_count"], 1)
        self.assertEqual([row["sequence_num"] for row in summary_rows(result)], [0, 3])
        self.assertEqual(len(rejected), 2)
        self.assertEqual(
            {row["reason"] for row in rejected},
            {
                "missing_or_invalid_ticker_bid",
                "nonpositive_locked_or_crossed_ticker_bbo",
            },
        )

    def test_update_before_any_snapshot_emits_nothing(self) -> None:
        result = self._run(
            [_l2(0, "update", [_update("bid", "100", "2")])]
        )
        summary = result["summary"]

        self.assertEqual(summary["book_snapshot_rows"], 0)
        self.assertEqual(summary["valid_window_count"], 0)
        self.assertEqual(summary["counts"]["ignored_updates_while_invalid"], 1)
        self.assertEqual(summary["counts"].get("validations", 0), 0)

    def test_normal_update_after_invalidation_cannot_recover(self) -> None:
        rows = [
            _basic_snapshot(0),
            _l2(1, "update", [_update("bid", "102", "1")]),
            _l2(2, "update", [_update("bid", "99", "5")]),
        ]

        result = self._run(rows)
        summary = result["summary"]

        self.assertEqual([row["sequence_num"] for row in summary_rows(result)], [0])
        self.assertEqual(summary["invalidation_reasons"]["crossed_book"], 1)
        self.assertEqual(summary["counts"]["ignored_updates_while_invalid"], 1)
        self.assertEqual(summary["counts"]["validations"], 1)
        self.assertEqual(summary["valid_window_count"], 1)

    def test_exact_transport_duplicate_invalidates_and_snapshot_recovers(self) -> None:
        first = _basic_snapshot(0)
        result = self._run([first, dict(first), _basic_snapshot(1, ordinal=1)])
        summary = result["summary"]

        self.assertEqual(summary["counts"]["exact_transport_duplicates"], 1)
        self.assertEqual(
            summary["invalidation_reasons"]["exact_transport_duplicate"],
            1,
        )
        self.assertEqual([row["sequence_num"] for row in summary_rows(result)], [0, 1])
        self.assertEqual(summary["valid_window_count"], 2)

    def test_conflicting_same_sequence_invalidates(self) -> None:
        conflict = _l2(
            0,
            "update",
            [_update("bid", "100", "99")],
            ordinal=0,
        )
        result = self._run([_basic_snapshot(0), conflict, _basic_snapshot(1, ordinal=1)])
        summary = result["summary"]

        self.assertEqual(summary["counts"]["sequence_conflicts"], 1)
        self.assertEqual(
            summary["invalidation_reasons"]["conflicting_sequence_duplicate"],
            1,
        )
        self.assertEqual([row["sequence_num"] for row in summary_rows(result)], [0, 1])

    def test_sequence_regression_starts_new_epoch_and_snapshot_recovers(self) -> None:
        rows = [
            _basic_snapshot(5, ordinal=0),
            _l2(6, "update", [_update("bid", "100", "2")], ordinal=1),
            _basic_snapshot(0, ordinal=2),
        ]
        result = self._run(rows)
        summary = result["summary"]
        snapshots = summary_rows(result)

        self.assertEqual(summary["counts"]["sequence_regressions"], 1)
        self.assertEqual(
            summary["invalidation_reasons"]["sequence_regression_or_reconnect"],
            1,
        )
        self.assertEqual([row["connection_epoch"] for row in snapshots], [0, 0, 1])
        self.assertEqual(summary["valid_window_count"], 2)

    def test_other_product_l2_envelope_preserves_target_book(self) -> None:
        rows = [
            _basic_snapshot(0),
            _l2(
                1,
                "snapshot",
                [_update("bid", "50000", "1"), _update("offer", "50001", "1")],
                product_id="BTC-USD",
            ),
            _l2(2, "update", [_update("bid", "100", "9")]),
        ]
        result = self._run(rows)
        summary = result["summary"]
        snapshots = summary_rows(result)

        self.assertEqual(summary["counts"]["l2_envelopes_for_other_products"], 1)
        self.assertEqual(summary["counts"]["target_l2_envelopes"], 2)
        self.assertEqual([row["sequence_num"] for row in snapshots], [0, 2])
        self.assertEqual(snapshots[-1]["best_bid"], "100")
        self.assertEqual(snapshots[-1]["bid_levels"][0]["quantity"], "9")

    def test_target_event_with_missing_or_non_list_updates_invalidates_atomically(self) -> None:
        malformed_events = {
            "missing": {
                "type": "update",
                "product_id": "XRP-USD",
            },
            "non_list": {
                "type": "update",
                "product_id": "XRP-USD",
                "updates": {"side": "bid"},
            },
        }
        for label, malformed_event in malformed_events.items():
            with self.subTest(label=label):
                envelope = _envelope(
                    1,
                    "l2_data",
                    [
                        _l2_event(
                            "update",
                            [_update("bid", "100", "9")],
                            event_time=_timestamp(1),
                        ),
                        malformed_event,
                    ],
                )
                result = self._run(
                    [_basic_snapshot(0), envelope],
                    name=f"target-{label}-updates.jsonl",
                )
                summary = result["summary"]

                self.assertEqual(
                    [row["sequence_num"] for row in summary_rows(result)],
                    [0],
                )
                self.assertEqual(
                    summary["invalidation_reasons"]["l2_updates_not_list"],
                    1,
                )
                self.assertEqual(summary["counts"]["malformed_l2_envelopes"], 1)
                self.assertEqual(summary["valid_window_count"], 1)

    def test_non_target_events_with_malformed_updates_are_ignored(self) -> None:
        other_products = _envelope(
            1,
            "l2_data",
            [
                {"type": "snapshot", "product_id": "BTC-USD"},
                {
                    "type": "update",
                    "product_id": "ETH-USD",
                    "updates": {"not": "a list"},
                },
            ],
        )
        result = self._run(
            [
                _basic_snapshot(0),
                other_products,
                _l2(2, "update", [_update("bid", "100", "9")]),
            ]
        )
        summary = result["summary"]

        self.assertEqual([row["sequence_num"] for row in summary_rows(result)], [0, 2])
        self.assertEqual(summary["counts"]["l2_envelopes_for_other_products"], 1)
        self.assertEqual(summary["counts"].get("malformed_l2_envelopes", 0), 0)
        self.assertEqual(summary["counts"].get("invalidations", 0), 0)

    def test_malformed_empty_locked_and_crossed_books_are_rejected(self) -> None:
        cases = {
            "unknown_book_side": [
                _l2(
                    0,
                    "snapshot",
                    [_update("mystery", "100", "1"), _update("offer", "101", "1")],
                )
            ],
            "negative_quantity": [
                _l2(
                    0,
                    "snapshot",
                    [_update("bid", "100", "-1"), _update("offer", "101", "1")],
                )
            ],
            "empty_ask_book": [
                _l2(0, "snapshot", [_update("bid", "100", "1")])
            ],
            "locked_book": [
                _l2(
                    0,
                    "snapshot",
                    [_update("bid", "100", "1"), _update("offer", "100", "1")],
                )
            ],
            "crossed_book": [
                _l2(
                    0,
                    "snapshot",
                    [_update("bid", "101", "1"), _update("offer", "100", "1")],
                )
            ],
        }

        for reason, rows in cases.items():
            with self.subTest(reason=reason):
                result = self._run(rows, name=f"{reason}.jsonl")
                summary = result["summary"]
                self.assertEqual(summary["book_snapshot_rows"], 0)
                self.assertEqual(summary["invalidation_reasons"][reason], 1)

    def test_full_depth_is_retained_when_emitted_depth_is_truncated(self) -> None:
        rows = [
            _l2(
                0,
                "snapshot",
                [
                    _update("bid", "100", "1"),
                    _update("bid", "99", "2"),
                    _update("bid", "98", "3"),
                    _update("offer", "101", "4"),
                    _update("offer", "102", "5"),
                    _update("offer", "103", "6"),
                ],
            ),
            _l2(1, "update", [_update("bid", "98", "30")]),
        ]
        result = self._run(rows, depth_limit=1)
        first, second = summary_rows(result)

        self.assertTrue(first["depth_truncated"])
        self.assertEqual(first["full_bid_level_count"], 3)
        self.assertEqual(first["emitted_bid_level_count"], 1)
        self.assertEqual(first["bid_levels"], [{"price": "100", "quantity": "1"}])
        self.assertEqual(first["visible_book_sha256"], second["visible_book_sha256"])
        self.assertNotEqual(
            first["full_book_fingerprint_sha256"],
            second["full_book_fingerprint_sha256"],
        )
        self.assertIsNone(second["full_book_sha256"])
        self.assertEqual(second["full_bid_depth"], "33")
        expected_full_state = (
            "bid\t100\t1\n"
            "bid\t99\t2\n"
            "bid\t98\t3\n"
            "offer\t101\t4\n"
            "offer\t102\t5\n"
            "offer\t103\t6\n"
        ).encode("utf-8")
        self.assertEqual(
            first["full_book_sha256"],
            hashlib.sha256(expected_full_state).hexdigest(),
        )

    def test_snapshots_and_requested_full_hash_sequences_bypass_emit_cadence(self) -> None:
        rows = [
            _basic_snapshot(0),
            _l2(1, "update", [_update("bid", "100", "2")]),
            _l2(2, "update", [_update("bid", "100", "3")]),
            _l2(
                3,
                "snapshot",
                [_update("bid", "90", "4"), _update("offer", "91", "5")],
            ),
        ]
        result = self._run(
            rows,
            emit_every_l2_messages=100,
            full_hash_sequences=[2],
        )
        snapshots = summary_rows(result)
        summary = result["summary"]

        self.assertEqual([row["sequence_num"] for row in snapshots], [0, 2, 3])
        self.assertEqual(
            [row["validity_reason"] for row in snapshots],
            ["fresh_snapshot", "continuous_update", "fresh_snapshot"],
        )
        self.assertTrue(all(row["full_book_sha256"] for row in snapshots))
        self.assertEqual(
            set(summary["full_book_sha256_checkpoints"]),
            {"0:0", "0:2", "0:3"},
        )

    def test_non_l2_full_hash_checkpoint_emits_the_current_book_state(self) -> None:
        result = self._run(
            [
                _basic_snapshot(0),
                _l2(1, "update", [_update("bid", "100", "2")]),
                _ticker(2, "100", "101"),
            ],
            emit_every_l2_messages=100,
            full_hash_sequences=[2],
        )
        snapshots = summary_rows(result)

        self.assertEqual([row["sequence_num"] for row in snapshots], [0, 2])
        self.assertEqual(
            snapshots[-1]["validity_reason"],
            "continuous_non_l2_checkpoint",
        )
        self.assertEqual(snapshots[-1]["source_channel"], "ticker")
        self.assertTrue(snapshots[-1]["full_book_sha256"])
        self.assertEqual(
            set(result["summary"]["full_book_sha256_checkpoints"]),
            {"0:0", "0:2"},
        )

    def test_sequence_numbers_must_be_nonnegative_json_integers(self) -> None:
        for index, invalid_sequence in enumerate(("0", 0.0, -1, True, None)):
            with self.subTest(sequence_num=invalid_sequence):
                row = _basic_snapshot(0)
                row["sequence_num"] = invalid_sequence
                result = self._run([row], name=f"invalid-sequence-{index}.jsonl")
                self.assertEqual(result["summary"]["book_snapshot_rows"], 0)
                self.assertEqual(
                    result["summary"]["invalidation_reasons"][
                        "malformed_sequence_num"
                    ],
                    1,
                )

    def test_full_hash_checkpoint_keys_include_connection_epoch(self) -> None:
        result = self._run(
            [_basic_snapshot(5, ordinal=0), _basic_snapshot(0, ordinal=1)]
        )

        self.assertEqual(
            set(result["summary"]["full_book_sha256_checkpoints"]),
            {"0:5", "1:0"},
        )

    def test_file_mode_preserves_supplied_rollover_order(self) -> None:
        first_path = self._write_rows([_basic_snapshot(0)], name="z-first.jsonl")
        second_path = self._write_rows(
            [_l2(1, "update", [_update("bid", "100", "2")])],
            name="a-second.jsonl",
        )

        result = run_book_reconstruction(
            raw_files=[first_path, second_path],
            derived_root=self.root / "derived-rollover",
            catalog_root=self.root / "catalog-rollover",
            product="XRP-USD",
            capture_stream_id="rollover-stream",
            sequence_scope="complete",
            input_order="file",
        )
        summary = result["summary"]

        self.assertEqual([row["sequence_num"] for row in summary_rows(result)], [0, 1])
        self.assertEqual(
            [Path(row["path"]).name for row in summary["input_files"]],
            ["z-first.jsonl", "a-second.jsonl"],
        )
        self.assertEqual(summary["counts"].get("sequence_gap_events", 0), 0)
        self.assertEqual(summary["counts"].get("sequence_regressions", 0), 0)

    def test_semantic_and_run_fingerprints_are_path_and_encoding_independent(self) -> None:
        rows = [_basic_snapshot(0), _l2(1, "update", [_update("bid", "100", "2")])]
        first_path = self._write_rows(rows, name="one/source.jsonl", sort_keys=True)
        second_path = self._write_rows(rows, name="two/source.jsonl", sort_keys=False)

        def run(path: Path, suffix: str) -> dict[str, Any]:
            return run_book_reconstruction(
                raw_files=[path],
                derived_root=self.root / f"derived-{suffix}",
                catalog_root=self.root / f"catalog-{suffix}",
                product="XRP-USD",
                capture_stream_id="same-logical-stream",
                sequence_scope="complete",
                input_order="file",
                depth_limit=2,
            )["summary"]

        first = run(first_path, "one")
        second = run(second_path, "two")

        self.assertNotEqual(first["input_files"][0]["path"], second["input_files"][0]["path"])
        self.assertNotEqual(first["input_files"][0]["sha256"], second["input_files"][0]["sha256"])
        self.assertEqual(
            first["semantic_message_stream_sha256"],
            second["semantic_message_stream_sha256"],
        )
        self.assertEqual(first["state_stream_sha256"], second["state_stream_sha256"])
        self.assertEqual(
            first["semantic_run_fingerprint_sha256"],
            second["semantic_run_fingerprint_sha256"],
        )
        self.assertNotEqual(
            first["run_fingerprint_sha256"],
            second["run_fingerprint_sha256"],
        )

    def test_gzip_and_plain_inputs_share_semantics_but_not_raw_provenance(self) -> None:
        rows = [_basic_snapshot(0), _l2(1, "update", [_update("bid", "100", "2")])]
        plain_path = self._write_rows(rows, name="plain/source.jsonl")
        gzip_path = self._write_gzip_rows(rows, name="gzip/source.jsonl.gz")

        def run(path: Path, suffix: str) -> dict[str, Any]:
            return run_book_reconstruction(
                raw_files=[path],
                derived_root=self.root / f"derived-compression-{suffix}",
                catalog_root=self.root / f"catalog-compression-{suffix}",
                product="XRP-USD",
                capture_stream_id="same-compressed-stream",
                sequence_scope="complete",
                input_order="file",
                depth_limit=2,
            )["summary"]

        plain = run(plain_path, "plain")
        compressed = run(gzip_path, "gzip")

        self.assertNotEqual(
            plain["input_files"][0]["sha256"],
            compressed["input_files"][0]["sha256"],
        )
        self.assertEqual(
            plain["semantic_message_stream_sha256"],
            compressed["semantic_message_stream_sha256"],
        )
        self.assertEqual(plain["state_stream_sha256"], compressed["state_stream_sha256"])
        self.assertEqual(
            plain["semantic_run_fingerprint_sha256"],
            compressed["semantic_run_fingerprint_sha256"],
        )
        self.assertNotEqual(
            plain["run_fingerprint_sha256"],
            compressed["run_fingerprint_sha256"],
        )

    def test_cross_file_receive_time_routing_replicas_are_collapsed(self) -> None:
        recv_zero = "2025-08-01T21:21:00.100000Z"
        recv_one = "2025-08-01T21:21:01.100000Z"
        rows = [
            _l2(0, "snapshot", _basic_snapshot(0)["events"][0]["updates"], recv_ts=recv_zero),
            _l2(1, "update", [_update("bid", "100", "2")], recv_ts=recv_one),
        ]
        first_path = self._write_rows(rows, name="shard-a.jsonl")
        second_path = self._write_rows(rows, name="shard-b.jsonl")

        result = run_book_reconstruction(
            raw_files=[first_path, second_path],
            derived_root=self.root / "derived-replicas",
            catalog_root=self.root / "catalog-replicas",
            product="XRP-USD",
            capture_stream_id="replicated-stream",
            sequence_scope="complete",
            input_order="receive_time",
            source_layout="routed_shards",
        )
        summary = result["summary"]

        self.assertEqual(summary["counts"]["routing_replicas_collapsed"], 2)
        self.assertEqual(summary["counts"].get("exact_transport_duplicates", 0), 0)
        self.assertEqual(summary["counts"]["canonical_envelopes"], 2)
        self.assertEqual([row["sequence_num"] for row in summary_rows(result)], [0, 1])

    def test_unsupported_source_layout_input_order_pairs_are_rejected(self) -> None:
        raw_path = self._write_rows([_basic_snapshot(0)], name="unsupported-pair.jsonl")

        for source_layout, input_order in (
            ("ordered_files", "receive_time"),
            ("routed_shards", "file"),
        ):
            with self.subTest(source_layout=source_layout, input_order=input_order):
                with self.assertRaisesRegex(ValueError, "supported source_layout/input_order"):
                    run_book_reconstruction(
                        raw_files=[raw_path],
                        derived_root=self.root / f"derived-{source_layout}-{input_order}",
                        catalog_root=self.root / f"catalog-{source_layout}-{input_order}",
                        product="XRP-USD",
                        capture_stream_id="unsupported-pair-stream",
                        sequence_scope="complete",
                        input_order=input_order,
                        source_layout=source_layout,
                    )

    def test_max_messages_is_applied_after_routing_replica_collapse(self) -> None:
        recv_times = [
            "2025-08-01T21:21:00.100000Z",
            "2025-08-01T21:21:01.100000Z",
            "2025-08-01T21:21:02.100000Z",
        ]
        rows = [
            _l2(
                0,
                "snapshot",
                _basic_snapshot(0)["events"][0]["updates"],
                recv_ts=recv_times[0],
            ),
            _l2(1, "update", [_update("bid", "100", "2")], recv_ts=recv_times[1]),
            _l2(2, "update", [_update("bid", "100", "3")], recv_ts=recv_times[2]),
        ]
        first_path = self._write_rows(rows, name="limited-shard-a.jsonl")
        second_path = self._write_rows(rows, name="limited-shard-b.jsonl")

        result = run_book_reconstruction(
            raw_files=[first_path, second_path],
            derived_root=self.root / "derived-limited-replicas",
            catalog_root=self.root / "catalog-limited-replicas",
            product="XRP-USD",
            capture_stream_id="limited-replicated-stream",
            sequence_scope="complete",
            input_order="receive_time",
            source_layout="routed_shards",
            max_messages=2,
        )
        summary = result["summary"]

        self.assertEqual(summary["counts"]["selected_raw_records"], 6)
        self.assertEqual(summary["counts"]["routing_replicas_collapsed"], 3)
        self.assertEqual(summary["counts"]["canonical_envelopes_before_limit"], 3)
        self.assertEqual(summary["counts"]["canonical_envelopes"], 2)
        self.assertEqual(summary["counts"]["max_messages_truncated"], 1)
        self.assertEqual([row["sequence_num"] for row in summary_rows(result)], [0, 1])

    def test_duplicate_raw_paths_are_rejected(self) -> None:
        raw_path = self._write_rows([_basic_snapshot(0)], name="duplicate-input.jsonl")

        with self.assertRaisesRegex(ValueError, "supplied more than once"):
            run_book_reconstruction(
                raw_files=[raw_path, raw_path],
                derived_root=self.root / "derived-duplicate-input",
                catalog_root=self.root / "catalog-duplicate-input",
                product="XRP-USD",
                capture_stream_id="duplicate-input-stream",
                sequence_scope="complete",
            )

    def test_source_file_provenance_records_stable_verified_inputs(self) -> None:
        raw_path = self._write_rows([_basic_snapshot(0)], name="stable-source.jsonl")
        result = run_book_reconstruction(
            raw_files=[raw_path],
            derived_root=self.root / "derived-stable-source",
            catalog_root=self.root / "catalog-stable-source",
            product="XRP-USD",
            capture_stream_id="stable-source-stream",
            sequence_scope="complete",
        )
        manifest = result["summary"]
        source = manifest["input_files"][0]

        self.assertEqual(source["file_ordinal"], 0)
        self.assertEqual(source["path"], str(raw_path.resolve()))
        self.assertEqual(source["size_bytes"], raw_path.stat().st_size)
        self.assertEqual(source["sha256"], hashlib.sha256(raw_path.read_bytes()).hexdigest())
        self.assertEqual(source["compression"], "none")
        self.assertTrue(source["modified_time_utc"])
        self.assertTrue(source["verified_stable_during_read"])
        self.assertTrue(
            manifest["strict_source_provenance"]["all_input_files_stable_during_read"]
        )

    def test_manifest_audit_accepts_intact_run_and_rejects_corruption(self) -> None:
        result = self._run([_basic_snapshot(0)])
        manifest_path = Path(result["manifest_path"])

        intact = audit_book_reconstruction_run(
            manifest_path,
            product_id="XRP-USD",
        )
        self.assertTrue(intact["valid"])
        self.assertTrue(intact["strict_l2_eligible"])
        self.assertEqual(intact["observed_rows"]["book_snapshots"], 1)
        self.assertEqual(intact["observed_rows"]["book_windows"], 1)

        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        snapshots_path = manifest_path.parent / manifest["artifacts"]["book_snapshots"]["path"]
        with snapshots_path.open("at", encoding="utf-8", newline="\n") as handle:
            handle.write("{}\n")

        corrupted = audit_book_reconstruction_run(
            manifest_path,
            product_id="XRP-USD",
        )
        self.assertFalse(corrupted["valid"])
        self.assertFalse(corrupted["strict_l2_eligible"])
        self.assertIn("book_snapshots_sha256_mismatch", corrupted["errors"])

    def test_strict_gridbot_discovery_allows_fill_model_or_rejects_failed_contract(self) -> None:
        result = self._run([_basic_snapshot(0)])
        report = _strict_l2_contract_report(self.root / "derived", "XRP-USD")

        self.assertEqual(report["status"], "audited_book_windows_available")
        self.assertEqual(report["book_contract_discovery"]["eligible_runs"], 1)

        manifest_path = Path(result["manifest_path"])
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        snapshots_path = manifest_path.parent / manifest["artifacts"]["book_snapshots"][
            "path"
        ]
        with snapshots_path.open("at", encoding="utf-8", newline="\n") as handle:
            handle.write("{}\n")

        with self.assertRaisesRegex(
            AuditedBookSelectionError,
            "failed its fresh strict-L2 audit",
        ):
            load_audited_book_window(
                derived_root=self.root / "derived",
                product_id="XRP-USD",
                config=StrictL2FillConfig(),
                run_id=result["run_id"],
                window_id="window-000001",
                discovery=report["book_contract_discovery"],
            )

        failed_report = _strict_l2_contract_report(
            self.root / "derived",
            "XRP-USD",
        )
        self.assertEqual(failed_report["status"], "requires_valid_book_snapshots")
        self.assertEqual(failed_report["book_contract_discovery"]["eligible_runs"], 0)

    def test_strict_gridbot_consumes_exactly_one_audited_window(self) -> None:
        reconstruction = self._run([_basic_snapshot(0)])

        result = run_gridbot_backtest(
            derived_root=self.root / "derived",
            catalog_root=self.root / "catalog-gridbot",
            product="XRP-USD",
            lower="90",
            upper="110",
            grid_count=2,
            quote_start="100",
            base_start="0",
            order_quote="10",
            fee_source="manual",
            fee_rate="0",
            include_fallback_candles=False,
            l2_run_id=reconstruction["run_id"],
            l2_window_id="window-000001",
            l2_latency_ms=0,
        )

        self.assertEqual(result["summary"]["status"], "completed")
        self.assertEqual(result["summary"]["book_rows_used"], 1)
        self.assertEqual(
            result["summary"]["audited_book_selection"]["run_id"],
            reconstruction["run_id"],
        )
        self.assertEqual(
            result["summary"]["audited_book_selection"]["window_id"],
            "window-000001",
        )
        self.assertRegex(
            result["summary"]["audited_book_selection"]["selected_rows_sha256"],
            r"^[0-9a-f]{64}$",
        )
        self.assertRegex(result["summary"]["fill_engine_source_sha256"], r"^[0-9a-f]{64}$")
        self.assertRegex(result["summary"]["fill_contract_config_sha256"], r"^[0-9a-f]{64}$")
        self.assertRegex(result["summary"]["gridbot_engine_source_sha256"], r"^[0-9a-f]{64}$")
        self.assertRegex(result["summary"]["config_sha256"], r"^[0-9a-f]{64}$")
        run_dir = Path(result["run_dir"])
        self.assertTrue((run_dir / "fills.jsonl").exists())
        self.assertTrue((run_dir / "order_events.jsonl").exists())
        self.assertTrue((run_dir / "equity_curve.jsonl").exists())
        self.assertEqual(result["summary"]["output_artifacts"]["fills"]["rows"], 0)
        for artifact in result["summary"]["output_artifacts"].values():
            self.assertRegex(artifact["sha256"], r"^[0-9a-f]{64}$")

    def test_audit_reconciles_provenance_and_all_fingerprint_contracts(self) -> None:
        result = self._run(
            [
                _basic_snapshot(0),
                _l2(1, "update", [_update("bid", "100", "2")]),
                _l2(2, "update", [_update("bid", "100", "3")]),
                _l2(
                    3,
                    "snapshot",
                    [_update("bid", "90", "4"), _update("offer", "91", "5")],
                ),
            ],
            emit_every_l2_messages=100,
            full_hash_sequences=[2],
            name="audit-fingerprints.jsonl",
        )
        manifest_path = Path(result["manifest_path"])
        original_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

        intact = audit_book_reconstruction_run(manifest_path, product_id="XRP-USD")
        self.assertTrue(intact["valid"], intact["errors"])
        self.assertTrue(intact["strict_l2_eligible"])
        self.assertTrue(original_manifest["engine_source_sha256"])
        self.assertTrue(original_manifest["input_files"][0]["verified_stable_during_read"])
        self.assertTrue(original_manifest["state_stream_sha256"])
        self.assertTrue(original_manifest["visible_state_stream_sha256"])
        self.assertEqual(
            set(original_manifest["full_book_sha256_checkpoints"]),
            {"0:0", "0:2", "0:3"},
        )
        self.assertTrue(original_manifest["final_full_book_fingerprint_sha256"])
        self.assertTrue(original_manifest["semantic_run_fingerprint_sha256"])
        self.assertTrue(original_manifest["run_fingerprint_sha256"])

        tamper_cases = (
            (
                "engine",
                "engine_source_sha256_mismatch",
                lambda row: row.__setitem__("engine_source_sha256", "0" * 64),
            ),
            (
                "input",
                "input_file_sha256_mismatch",
                lambda row: row["input_files"][0].__setitem__("sha256", "0" * 64),
            ),
            (
                "state",
                "state_stream_sha256_mismatch",
                lambda row: row.__setitem__("state_stream_sha256", "0" * 64),
            ),
            (
                "visible",
                "visible_state_stream_sha256_mismatch",
                lambda row: row.__setitem__("visible_state_stream_sha256", "0" * 64),
            ),
            (
                "checkpoint",
                "full_book_sha256_checkpoints_mismatch",
                lambda row: row.__setitem__("full_book_sha256_checkpoints", {}),
            ),
            (
                "final",
                "final_full_book_fingerprint_sha256_mismatch",
                lambda row: row.__setitem__(
                    "final_full_book_fingerprint_sha256",
                    "0" * 64,
                ),
            ),
            (
                "semantic_run",
                "semantic_run_fingerprint_sha256_mismatch",
                lambda row: row.__setitem__(
                    "semantic_run_fingerprint_sha256",
                    "0" * 64,
                ),
            ),
            (
                "provenance_run",
                "run_fingerprint_sha256_mismatch",
                lambda row: row.__setitem__("run_fingerprint_sha256", "0" * 64),
            ),
        )
        for label, expected_error, mutate in tamper_cases:
            with self.subTest(label=label):
                changed_manifest = json.loads(json.dumps(original_manifest))
                mutate(changed_manifest)
                manifest_path.write_text(
                    json.dumps(changed_manifest, indent=2, sort_keys=True),
                    encoding="utf-8",
                )
                audit = audit_book_reconstruction_run(
                    manifest_path,
                    product_id="XRP-USD",
                )
                self.assertFalse(audit["valid"])
                self.assertIn(expected_error, audit["errors"])

        snapshots_path = (
            manifest_path.parent
            / original_manifest["artifacts"]["book_snapshots"]["path"]
        )
        snapshot_rows = artifact_rows(result, "book_snapshots")
        snapshot_rows[0]["originating_snapshot_sequence_num"] = 999
        with snapshots_path.open("wt", encoding="utf-8", newline="\n") as handle:
            for row in snapshot_rows:
                handle.write(json.dumps(row, sort_keys=True) + "\n")
        origin_manifest = json.loads(json.dumps(original_manifest))
        origin_manifest["artifacts"]["book_snapshots"]["sha256"] = hashlib.sha256(
            snapshots_path.read_bytes()
        ).hexdigest()
        manifest_path.write_text(
            json.dumps(origin_manifest, indent=2, sort_keys=True),
            encoding="utf-8",
        )

        origin_audit = audit_book_reconstruction_run(
            manifest_path,
            product_id="XRP-USD",
        )
        self.assertFalse(origin_audit["valid"])
        self.assertNotIn("book_snapshots_sha256_mismatch", origin_audit["errors"])
        self.assertIn(
            "strict_window_snapshot_attribution_mismatch",
            origin_audit["errors"],
        )

    def test_manifest_audit_rejects_tampered_config_and_quality_artifacts(self) -> None:
        for artifact_name in ("config", "book_quality_events"):
            with self.subTest(artifact_name=artifact_name):
                result = self._run(
                    [_basic_snapshot(0)],
                    name=f"audit-{artifact_name}.jsonl",
                )
                manifest_path = Path(result["manifest_path"])
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                artifact_path = (
                    manifest_path.parent
                    / manifest["artifacts"][artifact_name]["path"]
                )
                with artifact_path.open("at", encoding="utf-8", newline="\n") as handle:
                    handle.write("{}\n" if artifact_name != "config" else "\n")

                audit = audit_book_reconstruction_run(
                    manifest_path,
                    product_id="XRP-USD",
                )
                self.assertFalse(audit["valid"])
                self.assertFalse(audit["strict_l2_eligible"])
                self.assertIn(f"{artifact_name}_sha256_mismatch", audit["errors"])

    def test_manifest_audit_rejects_unsupported_layout_order_pair(self) -> None:
        result = self._run([_basic_snapshot(0)], name="audit-layout-pair.jsonl")
        manifest_path = Path(result["manifest_path"])
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["source_layout"] = "ordered_files"
        manifest["input_order"] = "receive_time"
        manifest["config"]["source_layout"] = "ordered_files"
        manifest["config"]["input_order"] = "receive_time"
        manifest_path.write_text(
            json.dumps(manifest, indent=2, sort_keys=True),
            encoding="utf-8",
        )

        audit = audit_book_reconstruction_run(manifest_path, product_id="XRP-USD")

        self.assertFalse(audit["valid"])
        self.assertFalse(audit["strict_l2_eligible"])
        self.assertIn("unsupported_source_layout_input_order_pair", audit["errors"])


def artifact_rows(result: dict[str, Any], artifact_name: str) -> list[dict[str, Any]]:
    manifest_path = Path(result["manifest_path"])
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    snapshots_path = manifest_path.parent / manifest["artifacts"][artifact_name]["path"]
    rows: list[dict[str, Any]] = []
    with snapshots_path.open("rt", encoding="utf-8") as handle:
        for line in handle:
            rows.append(json.loads(line))
    return rows


def summary_rows(result: dict[str, Any]) -> list[dict[str, Any]]:
    return artifact_rows(result, "book_snapshots")


if __name__ == "__main__":
    unittest.main()
