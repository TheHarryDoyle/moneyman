from __future__ import annotations

import bisect
import copy
import gzip
import hashlib
import json
import math
import os
import platform
import re
import sqlite3
import subprocess
import sys
from collections import OrderedDict
from datetime import datetime, timezone
from importlib.metadata import version as distribution_version
from pathlib import Path
from typing import Any, Iterable, TextIO

from .coinbase import extract_event_timestamps, extract_product_ids, message_channel


MANIFEST_SCHEMA = "moneyman.collector_session_manifest.v1"
COLLECTOR_VERSION = "2.0"
SEQUENCE_LOOKBACK = 4096
QUALITY_EVENT_LIMIT = 1000
INVALID_ROUTE_CHANNEL = "invalid_route"
_PRODUCT_ROUTE_RE = re.compile(r"^[A-Z0-9]+-[A-Z0-9]+$")
_CHANNEL_ROUTE_RE = re.compile(r"^[a-z0-9_]+$")
REQUIRED_EXECUTION_SOURCE_ROLES = (
    "collector",
    "collector_audit",
    "coinbase_helpers",
    "logger_config",
    "project_config",
)
LATENCY_BUCKET_UPPER_BOUNDS_MS = (
    -1000.0,
    -100.0,
    -10.0,
    0.0,
    1.0,
    2.0,
    5.0,
    10.0,
    20.0,
    50.0,
    100.0,
    200.0,
    500.0,
    1000.0,
    2000.0,
    5000.0,
    10000.0,
    60000.0,
)


AUDIT_CONTRACT = {
    "received_frame_count": "One count for every websocket recv result, including malformed JSON.",
    "received_envelope_count": "One count for every received frame parsed as a top-level JSON object.",
    "message_count": "Compatibility alias for received_envelope_count; routing replicas do not increase it.",
    "routed_write_count": "Number of JSONL rows written. One envelope may create identical rows in multiple product shards.",
    "sequence_scope": "Coinbase sequence_num is checked independently inside each successful websocket connection epoch.",
    "sequence_gap": (
        "Each connection epoch expects its first valid sequence to be 0. A later sequence above previous+1 is "
        "one gap event; missing_sequence_count is the numeric hole size."
    ),
    "sequence_duplicate": (
        "A repeated sequence found in the bounded recent-sequence lookback. Exact/conflicting compares the "
        "exchange payload after removing collector-added underscore fields."
    ),
    "sequence_regression": (
        "A sequence below the highest sequence already observed in the epoch. Repeats older than the "
        "lookback remain regressions but cannot be classified as exact/conflicting duplicates."
    ),
    "heartbeat_stale": "A heartbeat receive interval or runtime timeout strictly above heartbeat_dead_seconds.",
    "raw_derived_audit": (
        "Closed-session audit independently replays one canonical raw row per received frame. Sequence fields, "
        "heartbeat receive intervals/counters, and latency distributions are raw-derived; connection lifecycle "
        "reasons, retry delays, and runtime heartbeat timeouts remain writer-attested."
    ),
    "routing": (
        "The auditor independently derives exact destinations from the non-underscore exchange channel, "
        "extracted product IDs, and effective configured products. Unsafe network routes are confined to the "
        "fixed invalid_route review channel."
    ),
    "latency": (
        "_latency_ms is receive time minus the source envelope timestamp. Negative and invalid samples are "
        "counted, never coerced; percentile fields are fixed-histogram upper bounds."
    ),
    "closed_file": (
        "A file is inventoried only after flush and gzip close. Evidence binds path, byte size, SHA-256, row count, "
        "and first/last receive and event timestamp strings without modifying raw timestamps."
    ),
    "sequence_duplicate_lookback": SEQUENCE_LOOKBACK,
    "quality_event_limit": QUALITY_EVENT_LIMIT,
}


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")


def canonical_sha256(value: Any) -> str:
    return hashlib.sha256(_canonical_json_bytes(value)).hexdigest()


def _is_sha256(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value.lower())
    )


def file_sha256(path: Path, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(chunk_size):
            digest.update(chunk)
    return digest.hexdigest()


def atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    """Replace a JSON file atomically from a same-directory temporary file."""

    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    with temporary.open("wt", encoding="utf-8", newline="\n") as handle:
        json.dump(payload, handle, indent=2, sort_keys=True)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def _run_git(source_path: Path, *args: str) -> tuple[str | None, str | None]:
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=source_path.parent,
            capture_output=True,
            check=False,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return None, f"{type(exc).__name__}: {exc}"
    if result.returncode:
        return None, result.stderr.strip() or f"git exited {result.returncode}"
    return result.stdout.strip(), None


def capture_collector_provenance(
    collector_source_path: Path,
    effective_config: dict[str, Any],
    config_path: Path | None,
) -> dict[str, Any]:
    source_path = collector_source_path.resolve()
    git_commit, git_commit_error = _run_git(source_path, "rev-parse", "HEAD")
    git_status, git_status_error = _run_git(
        source_path,
        "status",
        "--porcelain",
    )
    config_file = config_path.resolve() if config_path else None
    audit_source_path = Path(__file__).resolve()
    package_root = audit_source_path.parent
    execution_sources = [
        {"role": "collector", "path": str(source_path), "sha256": file_sha256(source_path)},
        {
            "role": "collector_audit",
            "path": str(audit_source_path),
            "sha256": file_sha256(audit_source_path),
        },
        {
            "role": "coinbase_helpers",
            "path": str(package_root / "coinbase.py"),
            "sha256": file_sha256(package_root / "coinbase.py"),
        },
        {
            "role": "logger_config",
            "path": str(package_root / "logger_config.py"),
            "sha256": file_sha256(package_root / "logger_config.py"),
        },
        {
            "role": "project_config",
            "path": str(package_root / "config.py"),
            "sha256": file_sha256(package_root / "config.py"),
        },
    ]
    return {
        "collector_provenance": {
            "name": source_path.name,
            "version": COLLECTOR_VERSION,
            "source_path": str(source_path),
            "source_sha256": execution_sources[0]["sha256"],
            "execution_sources": execution_sources,
            "execution_source_bundle_sha256": canonical_sha256(execution_sources),
            "git_commit": git_commit,
            "git_worktree_dirty": bool(git_status) if git_status is not None else None,
            "git_probe_error": git_commit_error or git_status_error,
        },
        "host_provenance": {
            "hostname": platform.node(),
            "system": platform.system(),
            "release": platform.release(),
            "machine": platform.machine(),
            "python_version": platform.python_version(),
            "python_implementation": platform.python_implementation(),
            "python_executable": sys.executable,
            "websockets_version": distribution_version("websockets"),
        },
        "config_provenance": {
            "effective_config": effective_config,
            "effective_config_sha256": canonical_sha256(effective_config),
            "config_path": str(config_file) if config_file else None,
            "config_file_size_bytes": config_file.stat().st_size if config_file and config_file.exists() else None,
            "config_file_sha256": file_sha256(config_file) if config_file and config_file.exists() else None,
        },
    }


def transport_payload_sha256(payload: dict[str, Any]) -> str:
    exchange_payload = {key: value for key, value in payload.items() if not str(key).startswith("_")}
    return canonical_sha256(exchange_payload)


def primary_event_timestamp(payload: dict[str, Any]) -> str | None:
    for key in ("timestamp", "time", "event_time"):
        value = payload.get(key)
        if isinstance(value, str) and value:
            return value
    timestamps = extract_event_timestamps(payload)
    return timestamps[0] if timestamps else None


def _parse_timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def _heartbeat_counter(payload: dict[str, Any]) -> int | None:
    for event in payload.get("events", []) or []:
        if not isinstance(event, dict):
            continue
        value = event.get("heartbeat_counter")
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            return value
    return None


class LatencyDistribution:
    def __init__(self) -> None:
        self.count = 0
        self.missing_count = 0
        self.invalid_count = 0
        self.negative_count = 0
        self.minimum: float | None = None
        self.maximum: float | None = None
        self.total = 0.0
        self.bucket_counts = [0] * (len(LATENCY_BUCKET_UPPER_BOUNDS_MS) + 1)

    def observe(self, value: Any, has_error: bool = False) -> None:
        if value is None:
            if has_error:
                self.invalid_count += 1
            else:
                self.missing_count += 1
            return
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)):
            self.invalid_count += 1
            return
        sample = float(value)
        self.count += 1
        self.total += sample
        self.minimum = sample if self.minimum is None else min(self.minimum, sample)
        self.maximum = sample if self.maximum is None else max(self.maximum, sample)
        if sample < 0:
            self.negative_count += 1
        index = bisect.bisect_left(LATENCY_BUCKET_UPPER_BOUNDS_MS, sample)
        self.bucket_counts[index] += 1

    def _quantile_upper_bound(self, quantile: float) -> float | str | None:
        if not self.count:
            return None
        target = max(1, math.ceil(self.count * quantile))
        cumulative = 0
        for index, count in enumerate(self.bucket_counts):
            cumulative += count
            if cumulative >= target:
                if index == len(LATENCY_BUCKET_UPPER_BOUNDS_MS):
                    return "+inf"
                return LATENCY_BUCKET_UPPER_BOUNDS_MS[index]
        return "+inf"

    def snapshot(self) -> dict[str, Any]:
        labels = [f"<= {bound:g}" for bound in LATENCY_BUCKET_UPPER_BOUNDS_MS] + ["> 60000"]
        return {
            "sample_count": self.count,
            "missing_count": self.missing_count,
            "invalid_count": self.invalid_count,
            "negative_sample_count": self.negative_count,
            "min_ms": self.minimum,
            "max_ms": self.maximum,
            "mean_ms": self.total / self.count if self.count else None,
            "p50_upper_bound_ms": self._quantile_upper_bound(0.50),
            "p95_upper_bound_ms": self._quantile_upper_bound(0.95),
            "p99_upper_bound_ms": self._quantile_upper_bound(0.99),
            "histogram_bucket_upper_bounds_ms": list(LATENCY_BUCKET_UPPER_BOUNDS_MS) + ["+inf"],
            "histogram_counts": dict(zip(labels, self.bucket_counts, strict=True)),
        }


class CollectorAuditState:
    """Bounded in-memory audit state for one collector session."""

    def __init__(self, *, started_at: str, heartbeat_dead_seconds: float) -> None:
        self.started_at = started_at
        self.heartbeat_dead_seconds = float(heartbeat_dead_seconds)
        self.received_frame_count = 0
        self.received_envelope_count = 0
        self.parse_error_count = 0
        self.connection_history: list[dict[str, Any]] = []
        self.connect_failures: list[dict[str, Any]] = []
        self.quality_events: list[dict[str, Any]] = []
        self.quality_events_truncated = 0
        self.latency = LatencyDistribution()
        self._active_connection: dict[str, Any] | None = None
        self._recent_sequences: OrderedDict[int, str] = OrderedDict()
        self._last_sequence: int | None = None
        self._last_heartbeat_recv: str | None = None
        self._last_heartbeat_counter: int | None = None

    @property
    def active_connection_epoch(self) -> int | None:
        return self._active_connection["connection_epoch"] if self._active_connection else None

    def _event(self, event_type: str, observed_at: str, **details: Any) -> None:
        if len(self.quality_events) < QUALITY_EVENT_LIMIT:
            self.quality_events.append(
                {
                    "event_type": event_type,
                    "observed_at": observed_at,
                    "connection_epoch": self.active_connection_epoch,
                    **details,
                }
            )
        else:
            self.quality_events_truncated += 1

    def record_received_frame(self) -> None:
        self.received_frame_count += 1

    def record_parse_error(self, *, observed_at: str, error: str) -> None:
        self.parse_error_count += 1
        self._event("parse_error", observed_at, error=error)

    def start_connection(self, *, connected_at: str) -> int:
        if self._active_connection is not None:
            self.end_connection(
                ended_at=connected_at,
                disconnect_kind="superseded",
                reason="new connection began before prior epoch was closed",
                retry_delay_seconds=None,
            )
        epoch = len(self.connection_history)
        record = {
            "connection_epoch": epoch,
            "connect_ts": connected_at,
            "disconnect_ts": None,
            "disconnect_kind": None,
            "disconnect_reason": None,
            "retry_delay_seconds": None,
            "received_envelope_count": 0,
            "first_sequence_num": None,
            "last_sequence_num": None,
            "sequenced_envelope_count": 0,
            "unsequenced_envelope_count": 0,
            "malformed_sequence_count": 0,
            "sequence_gap_count": 0,
            "missing_sequence_count": 0,
            "sequence_duplicate_count": 0,
            "exact_sequence_duplicate_count": 0,
            "conflicting_sequence_duplicate_count": 0,
            "sequence_regression_count": 0,
            "heartbeat_message_count": 0,
            "first_heartbeat_recv_ts": None,
            "last_heartbeat_recv_ts": None,
            "max_heartbeat_interval_seconds": None,
            "stale_heartbeat_interval_count": 0,
            "heartbeat_timeout_count": 0,
            "heartbeat_counter_gap_count": 0,
            "missed_heartbeat_counter_count": 0,
            "heartbeat_counter_duplicate_count": 0,
            "heartbeat_counter_regression_count": 0,
        }
        self.connection_history.append(record)
        self._active_connection = record
        self._recent_sequences.clear()
        self._last_sequence = None
        self._last_heartbeat_recv = None
        self._last_heartbeat_counter = None
        return epoch

    def end_connection(
        self,
        *,
        ended_at: str,
        disconnect_kind: str,
        reason: str,
        retry_delay_seconds: float | None,
    ) -> None:
        if self._active_connection is None:
            return
        self._active_connection["disconnect_ts"] = ended_at
        self._active_connection["disconnect_kind"] = disconnect_kind
        self._active_connection["disconnect_reason"] = reason
        self._active_connection["retry_delay_seconds"] = retry_delay_seconds
        self._active_connection = None
        self._recent_sequences.clear()
        self._last_sequence = None
        self._last_heartbeat_recv = None
        self._last_heartbeat_counter = None

    def record_connect_failure(
        self,
        *,
        observed_at: str,
        error_kind: str,
        error: str,
        retry_delay_seconds: float,
    ) -> None:
        self.connect_failures.append(
            {
                "observed_at": observed_at,
                "error_kind": error_kind,
                "error": error,
                "retry_delay_seconds": retry_delay_seconds,
            }
        )

    def observe_envelope(
        self,
        payload: dict[str, Any],
        *,
        observed_at: str,
        transport_hash: str | None = None,
    ) -> None:
        self.received_envelope_count += 1
        connection = self._active_connection
        if connection is None:
            self._event("envelope_without_connection", observed_at)
        else:
            connection["received_envelope_count"] += 1
            self._observe_sequence(connection, payload, observed_at, transport_hash=transport_hash)
            if message_channel(payload) == "heartbeats":
                self._observe_heartbeat(connection, payload, observed_at)
        self.latency.observe(payload.get("_latency_ms"), has_error=bool(payload.get("_latency_error")))

    def _observe_sequence(
        self,
        connection: dict[str, Any],
        payload: dict[str, Any],
        observed_at: str,
        *,
        transport_hash: str | None = None,
    ) -> None:
        if "sequence_num" not in payload:
            connection["unsequenced_envelope_count"] += 1
            return
        sequence = payload.get("sequence_num")
        if isinstance(sequence, bool) or not isinstance(sequence, int) or sequence < 0:
            connection["malformed_sequence_count"] += 1
            self._event("malformed_sequence", observed_at, raw_value=sequence)
            return

        connection["sequenced_envelope_count"] += 1
        if connection["first_sequence_num"] is None:
            connection["first_sequence_num"] = sequence
            if sequence > 0:
                connection["sequence_gap_count"] += 1
                connection["missing_sequence_count"] += sequence
                self._event(
                    "initial_sequence_gap",
                    observed_at,
                    expected_sequence_num=0,
                    observed_sequence_num=sequence,
                    missing_sequence_count=sequence,
                )
        payload_hash = transport_hash or transport_payload_sha256(payload)
        previous_hash = self._recent_sequences.get(sequence)
        if previous_hash is not None:
            connection["sequence_duplicate_count"] += 1
            duplicate_kind = "exact" if previous_hash == payload_hash else "conflicting"
            connection[f"{duplicate_kind}_sequence_duplicate_count"] += 1
            self._event(
                f"{duplicate_kind}_sequence_duplicate",
                observed_at,
                sequence_num=sequence,
            )

        if self._last_sequence is not None:
            if sequence > self._last_sequence + 1:
                missing = sequence - self._last_sequence - 1
                connection["sequence_gap_count"] += 1
                connection["missing_sequence_count"] += missing
                self._event(
                    "sequence_gap",
                    observed_at,
                    previous_sequence_num=self._last_sequence,
                    observed_sequence_num=sequence,
                    missing_sequence_count=missing,
                )
            elif sequence < self._last_sequence:
                connection["sequence_regression_count"] += 1
                self._event(
                    "sequence_regression",
                    observed_at,
                    previous_sequence_num=self._last_sequence,
                    observed_sequence_num=sequence,
                    duplicate_identity_known=previous_hash is not None,
                )

        self._recent_sequences[sequence] = payload_hash
        self._recent_sequences.move_to_end(sequence)
        while len(self._recent_sequences) > SEQUENCE_LOOKBACK:
            self._recent_sequences.popitem(last=False)
        if self._last_sequence is None or sequence > self._last_sequence:
            self._last_sequence = sequence
            connection["last_sequence_num"] = sequence

    def _observe_heartbeat(
        self,
        connection: dict[str, Any],
        payload: dict[str, Any],
        observed_at: str,
    ) -> None:
        connection["heartbeat_message_count"] += 1
        recv_ts = payload.get("_recv_ts")
        if isinstance(recv_ts, str) and recv_ts:
            if connection["first_heartbeat_recv_ts"] is None:
                connection["first_heartbeat_recv_ts"] = recv_ts
            connection["last_heartbeat_recv_ts"] = recv_ts
            current_dt = _parse_timestamp(recv_ts)
            previous_dt = _parse_timestamp(self._last_heartbeat_recv)
            if current_dt is None:
                self._event("heartbeat_recv_timestamp_invalid", observed_at, raw_value=recv_ts)
            elif self._last_heartbeat_recv is not None and previous_dt is None:
                self._event(
                    "heartbeat_recv_timestamp_invalid",
                    observed_at,
                    raw_value=self._last_heartbeat_recv,
                )
            elif previous_dt is not None:
                interval = (current_dt - previous_dt).total_seconds()
                maximum = connection["max_heartbeat_interval_seconds"]
                connection["max_heartbeat_interval_seconds"] = interval if maximum is None else max(maximum, interval)
                if interval > self.heartbeat_dead_seconds:
                    connection["stale_heartbeat_interval_count"] += 1
                    self._event(
                        "stale_heartbeat_interval",
                        observed_at,
                        previous_heartbeat_recv_ts=self._last_heartbeat_recv,
                        heartbeat_recv_ts=recv_ts,
                        interval_seconds=interval,
                        threshold_seconds=self.heartbeat_dead_seconds,
                    )
            self._last_heartbeat_recv = recv_ts

        counter = _heartbeat_counter(payload)
        if counter is not None and self._last_heartbeat_counter is not None:
            if counter > self._last_heartbeat_counter + 1:
                missed = counter - self._last_heartbeat_counter - 1
                connection["heartbeat_counter_gap_count"] += 1
                connection["missed_heartbeat_counter_count"] += missed
                self._event(
                    "heartbeat_counter_gap",
                    observed_at,
                    previous_counter=self._last_heartbeat_counter,
                    observed_counter=counter,
                    missed_counter_count=missed,
                )
            elif counter == self._last_heartbeat_counter:
                connection["heartbeat_counter_duplicate_count"] += 1
                self._event("heartbeat_counter_duplicate", observed_at, counter=counter)
            elif counter < self._last_heartbeat_counter:
                connection["heartbeat_counter_regression_count"] += 1
                self._event(
                    "heartbeat_counter_regression",
                    observed_at,
                    previous_counter=self._last_heartbeat_counter,
                    observed_counter=counter,
                )
        if counter is not None:
            self._last_heartbeat_counter = counter

    def record_heartbeat_timeout(
        self,
        *,
        observed_at: str,
        stale_for_seconds: float,
        last_heartbeat_recv_ts: str | None,
    ) -> None:
        if self._active_connection is not None:
            self._active_connection["heartbeat_timeout_count"] += 1
        self._event(
            "heartbeat_timeout",
            observed_at,
            stale_for_seconds=stale_for_seconds,
            threshold_seconds=self.heartbeat_dead_seconds,
            last_heartbeat_recv_ts=last_heartbeat_recv_ts,
        )

    def snapshot(self) -> dict[str, Any]:
        sequence_fields = (
            "sequenced_envelope_count",
            "unsequenced_envelope_count",
            "malformed_sequence_count",
            "sequence_gap_count",
            "missing_sequence_count",
            "sequence_duplicate_count",
            "exact_sequence_duplicate_count",
            "conflicting_sequence_duplicate_count",
            "sequence_regression_count",
        )
        heartbeat_fields = (
            "heartbeat_message_count",
            "stale_heartbeat_interval_count",
            "heartbeat_timeout_count",
            "heartbeat_counter_gap_count",
            "missed_heartbeat_counter_count",
            "heartbeat_counter_duplicate_count",
            "heartbeat_counter_regression_count",
        )
        sequence_summary = {
            field: sum(int(record[field]) for record in self.connection_history)
            for field in sequence_fields
        }
        heartbeat_summary = {
            field: sum(int(record[field]) for record in self.connection_history)
            for field in heartbeat_fields
        }
        intervals = [
            record["max_heartbeat_interval_seconds"]
            for record in self.connection_history
            if record["max_heartbeat_interval_seconds"] is not None
        ]
        heartbeat_summary["max_heartbeat_interval_seconds"] = max(intervals) if intervals else None
        heartbeat_summary["heartbeat_dead_seconds"] = self.heartbeat_dead_seconds
        return {
            "received_frame_count": self.received_frame_count,
            "received_envelope_count": self.received_envelope_count,
            "message_count": self.received_envelope_count,
            "parse_error_count": self.parse_error_count,
            "successful_connection_count": len(self.connection_history),
            "reconnect_count": max(0, len(self.connection_history) - 1),
            "failed_connect_attempt_count": len(self.connect_failures),
            "connection_history": copy.deepcopy(self.connection_history),
            "connect_failures": copy.deepcopy(self.connect_failures),
            "sequence_summary": sequence_summary,
            "heartbeat_summary": heartbeat_summary,
            "latency_summary": self.latency.snapshot(),
            "quality_events": copy.deepcopy(self.quality_events),
            "quality_events_truncated": self.quality_events_truncated,
        }


def closed_file_evidence(
    *,
    path: Path,
    session_root: Path,
    row_count: int,
    opened_at: str,
    closed_at: str,
    first_recv_ts: str | None,
    last_recv_ts: str | None,
    first_event_ts: str | None,
    last_event_ts: str | None,
) -> dict[str, Any]:
    resolved_path = path.resolve()
    relative_path = resolved_path.relative_to(session_root.resolve())
    stat = resolved_path.stat()
    return {
        "relative_path": relative_path.as_posix(),
        "absolute_path": str(resolved_path),
        "size_bytes": stat.st_size,
        "modified_time_ns": stat.st_mtime_ns,
        "modified_time_utc": datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat(),
        "sha256": file_sha256(resolved_path),
        "row_count": row_count,
        "opened_at": opened_at,
        "closed_at": closed_at,
        "first_recv_ts": first_recv_ts,
        "last_recv_ts": last_recv_ts,
        "first_event_ts": first_event_ts,
        "last_event_ts": last_event_ts,
        "close_status": "closed",
    }


def _open_raw_text(path: Path) -> TextIO:
    if path.name.lower().endswith(".gz"):
        return gzip.open(path, "rt", encoding="utf-8")
    return path.open("rt", encoding="utf-8")


def _derive_envelope_latency(payload: dict[str, Any]) -> tuple[float | None, bool]:
    """Return receive-minus-source latency and whether timestamp parsing failed."""

    source_ts = payload.get("time") or payload.get("timestamp")
    if not source_ts:
        return None, False
    try:
        source_dt = datetime.fromisoformat(str(source_ts).replace("Z", "+00:00"))
        recv_dt = datetime.fromisoformat(str(payload.get("_recv_ts")))
        return (recv_dt - source_dt).total_seconds() * 1000, False
    except Exception:
        return None, True


def _exchange_channel(payload: dict[str, Any]) -> str:
    """Derive the exchange channel without trusting collector-added fields."""

    return str(payload.get("channel") or payload.get("type") or "unknown")


def _expected_routing(
    payload: dict[str, Any],
    configured_products: set[str],
) -> tuple[tuple[str, ...], dict[str, Any] | None]:
    """Independently derive the writer's exact route and review annotation."""

    try:
        product_ids = extract_product_ids(payload)
    except (AttributeError, TypeError, ValueError):
        return (
            (f"channel={INVALID_ROUTE_CHANNEL}",),
            {"reason": "invalid_product_id_type"},
        )
    invalid_product_ids = sorted(
        product_id
        for product_id in product_ids
        if not _PRODUCT_ROUTE_RE.fullmatch(product_id)
    )
    if invalid_product_ids:
        return (
            (f"channel={INVALID_ROUTE_CHANNEL}",),
            {"reason": "invalid_product_id", "values": invalid_product_ids},
        )
    requested_products = sorted(product_ids & configured_products)
    if requested_products:
        return tuple(f"product={product_id}" for product_id in requested_products), None

    channel = _exchange_channel(payload)
    if not _CHANNEL_ROUTE_RE.fullmatch(channel):
        return (
            (f"channel={INVALID_ROUTE_CHANNEL}",),
            {"reason": "invalid_channel_route", "value": channel},
        )
    if channel == INVALID_ROUTE_CHANNEL:
        return (
            (f"channel={INVALID_ROUTE_CHANNEL}",),
            {"reason": "reserved_channel_route", "value": channel},
        )
    return (f"channel={channel}",), None


def _routing_error_matches(
    payload: dict[str, Any],
    expected_error: dict[str, Any] | None,
) -> bool:
    if expected_error is None:
        return "_routing_error" not in payload
    actual = payload.get("_routing_error")
    if not isinstance(actual, dict) or actual.get("reason") != expected_error["reason"]:
        return False
    if expected_error["reason"] == "invalid_product_id_type":
        return set(actual) == {"reason", "detail"} and isinstance(actual.get("detail"), str) and bool(
            actual["detail"]
        )
    return actual == expected_error


def _natural_count(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _require_natural_count(
    value: Any,
    field: str,
    errors: list[str],
) -> int | None:
    count = _natural_count(value)
    if count is None:
        errors.append(f"{field}_not_nonnegative_integer")
    return count


def _require_object(value: Any, field: str, errors: list[str]) -> dict[str, Any]:
    if not isinstance(value, dict):
        errors.append(f"{field}_not_object")
        return {}
    return value


def _require_list(value: Any, field: str, errors: list[str]) -> list[Any]:
    if not isinstance(value, list):
        errors.append(f"{field}_not_list")
        return []
    return value


def _require_count_map(
    value: Any,
    field: str,
    errors: list[str],
) -> dict[str, int]:
    if not isinstance(value, dict):
        errors.append(f"{field}_not_object")
        return {}
    output: dict[str, int] = {}
    for key, raw_count in value.items():
        if not isinstance(key, str) or not key:
            errors.append(f"{field}_key_invalid: {key}")
            continue
        count = _natural_count(raw_count)
        if count is None:
            errors.append(f"{field}_value_not_nonnegative_integer: {key}")
            continue
        output[key] = count
    return output


def _sum_history_count(
    connection_history: list[dict[str, Any]],
    field: str,
    errors: list[str],
) -> int | None:
    total = 0
    valid = True
    for index, row in enumerate(connection_history):
        count = _natural_count(row.get(field))
        if count is None:
            errors.append(
                f"connection_history_{index}_{field}_not_nonnegative_integer"
            )
            valid = False
            continue
        total += count
    return total if valid else None


def _scan_raw_file(
    path: Path,
    routing_streams: dict[tuple[tuple[str, ...], str], dict[str, Any]],
    canonical_frames: sqlite3.Connection,
    *,
    expected_session_id: Any,
    configured_products: set[str],
) -> dict[str, Any]:
    row_count = 0
    parse_errors = 0
    first_recv_ts = None
    last_recv_ts = None
    first_event_ts = None
    last_event_ts = None
    primary_envelope_count = 0
    primary_malformed_count = 0
    routing_metadata_error_count = 0
    expected_routing_error_count = 0
    latency_annotation_error_count = 0
    canonical_frame_error_count = 0
    route_name = path.parent.name
    with _open_raw_text(path) as handle:
        for line in handle:
            if not line.strip():
                continue
            row_count += 1
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                parse_errors += 1
                continue
            if not isinstance(payload, dict):
                parse_errors += 1
                continue
            recv_ts = payload.get("_recv_ts")
            event_ts = primary_event_timestamp(payload)
            if isinstance(recv_ts, str) and recv_ts:
                first_recv_ts = first_recv_ts or recv_ts
                last_recv_ts = recv_ts
            if event_ts:
                first_event_ts = first_event_ts or event_ts
                last_event_ts = event_ts
            destinations = payload.get("_routed_destinations")
            if not isinstance(destinations, list) or not destinations or not all(
                isinstance(item, str) and item for item in destinations
            ):
                routing_metadata_error_count += 1
                continue
            destination_tuple = tuple(destinations)
            if len(set(destination_tuple)) != len(destination_tuple) or route_name not in destination_tuple:
                routing_metadata_error_count += 1
                continue
            parsed_envelope = payload.get("_parsed_envelope")
            connection_epoch = payload.get("_connection_epoch")
            frame_ordinal = payload.get("_received_frame_ordinal")
            if (
                not isinstance(parsed_envelope, bool)
                or isinstance(connection_epoch, bool)
                or not isinstance(connection_epoch, int)
                or connection_epoch < 0
                or isinstance(frame_ordinal, bool)
                or not isinstance(frame_ordinal, int)
                or frame_ordinal <= 0
                or payload.get("_collector_session_id") != expected_session_id
                or _parse_timestamp(payload.get("_recv_ts")) is None
            ):
                routing_metadata_error_count += 1
                continue
            stream_key = (destination_tuple, route_name)
            stream = routing_streams.setdefault(
                stream_key,
                {"count": 0, "digest": hashlib.sha256()},
            )
            stream["digest"].update(_canonical_json_bytes(payload) + b"\n")
            stream["count"] += 1
            if parsed_envelope:
                expected_destinations, expected_route_error = _expected_routing(
                    payload,
                    configured_products,
                )
                if (
                    destination_tuple != expected_destinations
                    or route_name not in expected_destinations
                    or not _routing_error_matches(payload, expected_route_error)
                ):
                    expected_routing_error_count += 1
            if destinations[0] != route_name:
                continue

            replay_payload: dict[str, Any] = {}
            transport_hash = ""
            if parsed_envelope:
                primary_envelope_count += 1
                exchange_channel = _exchange_channel(payload)
                if payload.get("_channel") != exchange_channel:
                    routing_metadata_error_count += 1
                replay_payload = {
                    "_channel": exchange_channel,
                    "_recv_ts": payload.get("_recv_ts"),
                }
                if "sequence_num" in payload:
                    replay_payload["sequence_num"] = payload.get("sequence_num")
                counter = _heartbeat_counter(payload)
                if counter is not None:
                    replay_payload["events"] = [{"heartbeat_counter": counter}]
                derived_latency, latency_error = _derive_envelope_latency(payload)
                if latency_error:
                    replay_payload["_latency_ms"] = None
                    replay_payload["_latency_error"] = True
                    if payload.get("_latency_ms") is not None or not payload.get("_latency_error"):
                        latency_annotation_error_count += 1
                elif derived_latency is None:
                    if payload.get("_latency_ms") is not None or payload.get("_latency_error"):
                        latency_annotation_error_count += 1
                else:
                    replay_payload["_latency_ms"] = derived_latency
                    recorded_latency = payload.get("_latency_ms")
                    if (
                        isinstance(recorded_latency, bool)
                        or not isinstance(recorded_latency, (int, float))
                        or not math.isfinite(float(recorded_latency))
                        or abs(float(recorded_latency) - derived_latency) > 1e-6
                        or payload.get("_latency_error")
                    ):
                        latency_annotation_error_count += 1
                transport_hash = transport_payload_sha256(payload)
            else:
                primary_malformed_count += 1
                if (
                    destination_tuple != ("channel=malformed_json",)
                    or not isinstance(payload.get("parse_error"), str)
                    or not isinstance(payload.get("raw"), str)
                ):
                    routing_metadata_error_count += 1

            try:
                canonical_frames.execute(
                    """
                    INSERT INTO canonical_frames(
                        frame_ordinal, connection_epoch, parsed_envelope,
                        replay_payload_json, transport_payload_sha256
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        frame_ordinal,
                        connection_epoch,
                        int(parsed_envelope),
                        json.dumps(replay_payload, sort_keys=True, separators=(",", ":"), default=str),
                        transport_hash,
                    ),
                )
            except sqlite3.IntegrityError:
                canonical_frame_error_count += 1
    return {
        "row_count": row_count,
        "parse_error_count": parse_errors,
        "first_recv_ts": first_recv_ts,
        "last_recv_ts": last_recv_ts,
        "first_event_ts": first_event_ts,
        "last_event_ts": last_event_ts,
        "primary_envelope_count": primary_envelope_count,
        "primary_malformed_count": primary_malformed_count,
        "routing_metadata_error_count": routing_metadata_error_count,
        "expected_routing_error_count": expected_routing_error_count,
        "latency_annotation_error_count": latency_annotation_error_count,
        "canonical_frame_error_count": canonical_frame_error_count,
    }


RAW_DERIVED_SEQUENCE_FIELDS = (
    "first_sequence_num",
    "last_sequence_num",
    "sequenced_envelope_count",
    "unsequenced_envelope_count",
    "malformed_sequence_count",
    "sequence_gap_count",
    "missing_sequence_count",
    "sequence_duplicate_count",
    "exact_sequence_duplicate_count",
    "conflicting_sequence_duplicate_count",
    "sequence_regression_count",
)
RAW_DERIVED_HEARTBEAT_FIELDS = (
    "heartbeat_message_count",
    "first_heartbeat_recv_ts",
    "last_heartbeat_recv_ts",
    "max_heartbeat_interval_seconds",
    "stale_heartbeat_interval_count",
    "heartbeat_counter_gap_count",
    "missed_heartbeat_counter_count",
    "heartbeat_counter_duplicate_count",
    "heartbeat_counter_regression_count",
)


def _replay_raw_audit(
    canonical_frames: sqlite3.Connection,
    manifest: dict[str, Any],
    connection_history: list[dict[str, Any]],
    errors: list[str],
) -> dict[str, Any]:
    heartbeat_summary = manifest.get("heartbeat_summary")
    threshold = heartbeat_summary.get("heartbeat_dead_seconds") if isinstance(heartbeat_summary, dict) else None
    if (
        isinstance(threshold, bool)
        or not isinstance(threshold, (int, float))
        or not math.isfinite(float(threshold))
        or float(threshold) < 0
    ):
        errors.append("heartbeat_dead_seconds_invalid")
        threshold = 0.0

    replay = CollectorAuditState(
        started_at=str(manifest.get("start_ts") or ""),
        heartbeat_dead_seconds=float(threshold),
    )
    active_epoch = -1
    previous_ordinal = 0
    previous_row_epoch = -1

    def close_active() -> None:
        if replay.active_connection_epoch is None:
            return
        declared = connection_history[active_epoch]
        replay.end_connection(
            ended_at=str(declared.get("disconnect_ts") or ""),
            disconnect_kind=str(declared.get("disconnect_kind") or "writer_attested"),
            reason=str(declared.get("disconnect_reason") or "writer_attested"),
            retry_delay_seconds=declared.get("retry_delay_seconds"),
        )

    def advance_to(epoch: int) -> None:
        nonlocal active_epoch
        while active_epoch < epoch:
            close_active()
            active_epoch += 1
            declared = connection_history[active_epoch]
            replay.start_connection(connected_at=str(declared.get("connect_ts") or ""))

    for ordinal, epoch, parsed, replay_json, transport_hash in canonical_frames.execute(
        """
        SELECT frame_ordinal, connection_epoch, parsed_envelope,
               replay_payload_json, transport_payload_sha256
        FROM canonical_frames
        ORDER BY frame_ordinal
        """
    ):
        replay.record_received_frame()
        if ordinal != previous_ordinal + 1:
            errors.append(
                f"raw_frame_ordinal_not_contiguous: expected={previous_ordinal + 1} observed={ordinal}"
            )
        previous_ordinal = ordinal
        if epoch < previous_row_epoch:
            errors.append(f"raw_connection_epoch_regression: previous={previous_row_epoch} observed={epoch}")
            continue
        previous_row_epoch = epoch
        if epoch >= len(connection_history):
            errors.append(f"raw_connection_epoch_out_of_range: {epoch}")
            continue
        advance_to(epoch)
        if parsed:
            payload = json.loads(replay_json)
            replay.observe_envelope(
                payload,
                observed_at=str(payload.get("_recv_ts") or ""),
                transport_hash=transport_hash,
            )

    while active_epoch + 1 < len(connection_history):
        advance_to(active_epoch + 1)
    close_active()
    raw_snapshot = replay.snapshot()

    if raw_snapshot["received_frame_count"] != manifest.get("received_frame_count"):
        errors.append("raw_received_frame_count_mismatch")
    if raw_snapshot["received_envelope_count"] != manifest.get("received_envelope_count"):
        errors.append("raw_received_envelope_count_mismatch")
    if len(raw_snapshot["connection_history"]) != len(connection_history):
        errors.append("raw_connection_history_length_mismatch")
    for index, (raw_row, declared_row) in enumerate(
        zip(raw_snapshot["connection_history"], connection_history)
    ):
        for field in (
            "received_envelope_count",
            *RAW_DERIVED_SEQUENCE_FIELDS,
            *RAW_DERIVED_HEARTBEAT_FIELDS,
        ):
            if raw_row.get(field) != declared_row.get(field):
                errors.append(f"raw_connection_metric_mismatch: epoch={index} field={field}")

    declared_sequence = manifest.get("sequence_summary")
    raw_sequence = raw_snapshot["sequence_summary"]
    if not isinstance(declared_sequence, dict):
        errors.append("sequence_summary_not_object")
    else:
        for field, value in raw_sequence.items():
            if declared_sequence.get(field) != value:
                errors.append(f"raw_sequence_summary_mismatch: {field}")

    if not isinstance(heartbeat_summary, dict):
        errors.append("heartbeat_summary_not_object")
    else:
        for field in (
            "heartbeat_message_count",
            "stale_heartbeat_interval_count",
            "heartbeat_counter_gap_count",
            "missed_heartbeat_counter_count",
            "heartbeat_counter_duplicate_count",
            "heartbeat_counter_regression_count",
            "max_heartbeat_interval_seconds",
        ):
            if heartbeat_summary.get(field) != raw_snapshot["heartbeat_summary"].get(field):
                errors.append(f"raw_heartbeat_summary_mismatch: {field}")

    if manifest.get("latency_summary") != raw_snapshot["latency_summary"]:
        errors.append("raw_latency_summary_mismatch")
    return raw_snapshot


def audit_collector_session(manifest_path: Path) -> dict[str, Any]:
    """Read-only verification of a closed collector manifest and every bound raw file."""

    errors: list[str] = []
    warnings: list[str] = []
    configured_products: set[str] = set()
    try:
        manifest_file = manifest_path.resolve()
    except (OSError, ValueError) as exc:
        return {
            "valid": False,
            "manifest_path": str(manifest_path),
            "errors": [f"manifest_path_invalid: {type(exc).__name__}: {exc}"],
        }
    try:
        manifest = json.loads(manifest_file.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        return {"valid": False, "manifest_path": str(manifest_file), "errors": [f"manifest_read_error: {exc}"]}
    if not isinstance(manifest, dict):
        return {"valid": False, "manifest_path": str(manifest_file), "errors": ["manifest_not_object"]}
    if manifest.get("manifest_schema") != MANIFEST_SCHEMA:
        errors.append("manifest_schema_mismatch")
    if manifest.get("status") != "closed" or not manifest.get("end_ts"):
        errors.append("session_not_closed")
    count_fields = (
        "received_frame_count",
        "received_envelope_count",
        "message_count",
        "parse_error_count",
        "successful_connection_count",
        "reconnect_count",
        "failed_connect_attempt_count",
        "routed_write_count",
        "closed_file_count",
    )
    manifest_counts = {
        field: _require_natural_count(manifest.get(field), field, errors)
        for field in count_fields
    }
    session_end = _require_object(manifest.get("session_end"), "session_end", errors)
    open_files = _require_list(manifest.get("open_files"), "open_files", errors)
    close_errors = _require_list(
        session_end.get("close_errors"),
        "session_end_close_errors",
        errors,
    )
    open_writer_count = _require_natural_count(
        session_end.get("open_writer_count"),
        "session_end_open_writer_count",
        errors,
    )
    product_message_counts = _require_count_map(
        manifest.get("product_message_counts"),
        "product_message_counts",
        errors,
    )
    channel_message_counts = _require_count_map(
        manifest.get("channel_message_counts"),
        "channel_message_counts",
        errors,
    )
    connect_failures = _require_list(
        manifest.get("connect_failures"),
        "connect_failures",
        errors,
    )
    sequence_summary = _require_object(
        manifest.get("sequence_summary"),
        "sequence_summary",
        errors,
    )
    heartbeat_summary = _require_object(
        manifest.get("heartbeat_summary"),
        "heartbeat_summary",
        errors,
    )
    latency_summary = _require_object(
        manifest.get("latency_summary"),
        "latency_summary",
        errors,
    )
    closed_files = _require_list(manifest.get("closed_files"), "closed_files", errors)

    if session_end.get("all_writers_closed") is not True:
        errors.append("writers_not_all_closed")
    if session_end.get("final_manifest_written_after_file_close") is not True:
        errors.append("final_manifest_not_after_file_close")
    if open_writer_count != 0 or open_files:
        errors.append("final_manifest_has_open_files")
    if close_errors:
        errors.append("final_manifest_has_close_errors")

    config_provenance = manifest.get("config_provenance")
    if not isinstance(config_provenance, dict):
        errors.append("config_provenance_missing")
    else:
        effective_config = config_provenance.get("effective_config")
        if not isinstance(effective_config, dict):
            errors.append("effective_config_not_object")
        else:
            if canonical_sha256(effective_config) != config_provenance.get("effective_config_sha256"):
                errors.append("effective_config_sha256_mismatch")
            for field in ("ws_url", "products", "channels", "raw_root"):
                if effective_config.get(field) != manifest.get(field):
                    errors.append(f"effective_config_manifest_mismatch: {field}")
            if effective_config.get("heartbeat_dead_seconds") != heartbeat_summary.get(
                "heartbeat_dead_seconds"
            ):
                errors.append("effective_config_manifest_mismatch: heartbeat_dead_seconds")
            effective_products = effective_config.get("products")
            if not isinstance(effective_products, list) or not all(
                isinstance(product_id, str)
                and bool(_PRODUCT_ROUTE_RE.fullmatch(product_id))
                for product_id in effective_products
            ):
                errors.append("effective_config_products_invalid")
            else:
                configured_products = set(effective_products)
                if len(configured_products) != len(effective_products):
                    errors.append("effective_config_products_duplicate")
            effective_channels = effective_config.get("channels")
            if not isinstance(effective_channels, list) or not all(
                isinstance(channel, str)
                and bool(_CHANNEL_ROUTE_RE.fullmatch(channel))
                and channel != INVALID_ROUTE_CHANNEL
                for channel in effective_channels
            ):
                errors.append("effective_config_channels_invalid")
            elif len(set(effective_channels)) != len(effective_channels):
                errors.append("effective_config_channels_duplicate")
    collector_provenance = manifest.get("collector_provenance")
    if not isinstance(collector_provenance, dict) or not collector_provenance.get("source_sha256"):
        errors.append("collector_provenance_missing")
    else:
        execution_sources = collector_provenance.get("execution_sources")
        if not isinstance(execution_sources, list):
            execution_sources = []
            errors.append("execution_sources_not_list")
        if canonical_sha256(execution_sources) != collector_provenance.get("execution_source_bundle_sha256"):
            errors.append("execution_source_bundle_sha256_mismatch")
        role_sources: dict[str, dict[str, Any]] = {}
        for source in execution_sources:
            if not isinstance(source, dict):
                errors.append("execution_source_not_object")
                continue
            role = source.get("role")
            if not isinstance(role, str) or not role or role in role_sources:
                errors.append(f"execution_source_role_invalid: {role}")
                continue
            role_sources[role] = source
            if not isinstance(source.get("path"), str) or not source.get("path"):
                errors.append(f"execution_source_path_invalid: {role}")
            if not _is_sha256(source.get("sha256")):
                errors.append(f"execution_source_sha256_invalid: {role}")
            try:
                current_path = Path(str(source.get("path", "")))
                if current_path.is_file() and file_sha256(current_path) != source.get("sha256"):
                    warnings.append(f"execution_source_current_sha256_drift: {current_path}")
            except (OSError, ValueError) as exc:
                errors.append(
                    f"execution_source_path_unusable: {role}: {type(exc).__name__}"
                )
        for role in REQUIRED_EXECUTION_SOURCE_ROLES:
            if role not in role_sources:
                errors.append(f"execution_source_role_missing: {role}")
        collector_source = role_sources.get("collector", {})
        if collector_source.get("sha256") != collector_provenance.get("source_sha256"):
            errors.append("collector_source_sha256_mismatch")
    host_provenance = manifest.get("host_provenance")
    if not isinstance(host_provenance, dict):
        errors.append("host_provenance_missing")
    elif not isinstance(host_provenance.get("websockets_version"), str) or not host_provenance[
        "websockets_version"
    ].strip():
        errors.append("host_provenance_websockets_version_missing")
    if isinstance(config_provenance, dict) and config_provenance.get("config_path"):
        try:
            current_config = Path(str(config_provenance["config_path"]))
            if current_config.is_file() and file_sha256(current_config) != config_provenance.get(
                "config_file_sha256"
            ):
                warnings.append(f"config_file_current_sha256_drift: {current_config}")
        except (OSError, ValueError) as exc:
            errors.append(f"config_file_path_unusable: {type(exc).__name__}")

    session_root = manifest_file.parent
    observed_rows = 0
    observed_primary_envelopes = 0
    observed_primary_malformed = 0
    observed_files = 0
    observed_route_counts: dict[str, int] = {}
    declared_paths: set[Path] = set()
    routing_streams: dict[tuple[tuple[str, ...], str], dict[str, Any]] = {}
    canonical_frames = sqlite3.connect("")
    canonical_frames.execute(
        """
        CREATE TABLE canonical_frames(
            frame_ordinal INTEGER PRIMARY KEY,
            connection_epoch INTEGER NOT NULL,
            parsed_envelope INTEGER NOT NULL,
            replay_payload_json TEXT NOT NULL,
            transport_payload_sha256 TEXT NOT NULL
        )
        """
    )
    ordered_evidence = sorted(
        closed_files,
        key=lambda row: str(row.get("relative_path", "")) if isinstance(row, dict) else "",
    )
    for index, evidence in enumerate(ordered_evidence):
        if not isinstance(evidence, dict):
            errors.append(f"closed_file_{index}_not_object")
            continue
        relative = evidence.get("relative_path")
        if not isinstance(relative, str) or not relative:
            errors.append(f"closed_file_{index}_relative_path_missing")
            continue
        try:
            candidate = (session_root / Path(relative)).resolve()
        except (OSError, ValueError) as exc:
            errors.append(
                f"closed_file_{index}_relative_path_invalid: {type(exc).__name__}"
            )
            continue
        try:
            candidate.relative_to(session_root.resolve())
        except ValueError:
            errors.append(f"closed_file_{index}_outside_session")
            continue
        if candidate in declared_paths:
            errors.append(f"closed_file_{index}_duplicate_path")
            continue
        declared_paths.add(candidate)
        recorded_absolute = evidence.get("absolute_path")
        try:
            recorded_path = (
                Path(recorded_absolute)
                if isinstance(recorded_absolute, str) and "\x00" not in recorded_absolute
                else None
            )
        except (OSError, ValueError):
            recorded_path = None
        if recorded_path is None or not recorded_path.is_absolute():
            errors.append(f"closed_file_{index}_absolute_path_invalid")
        elif recorded_path != candidate:
            warnings.append(f"closed_file_recorded_absolute_path_drift: {relative}")
        try:
            if not candidate.is_file():
                errors.append(f"closed_file_{index}_missing")
                continue
            stat = candidate.stat()
            digest = file_sha256(candidate)
        except (OSError, ValueError) as exc:
            errors.append(f"closed_file_{index}_path_io_error: {type(exc).__name__}")
            continue
        observed_files += 1
        if evidence.get("close_status") != "closed":
            errors.append(f"closed_file_{index}_status_mismatch")
        _require_natural_count(
            evidence.get("row_count"),
            f"closed_file_{index}_row_count",
            errors,
        )
        _require_natural_count(
            evidence.get("size_bytes"),
            f"closed_file_{index}_size_bytes",
            errors,
        )
        _require_natural_count(
            evidence.get("modified_time_ns"),
            f"closed_file_{index}_modified_time_ns",
            errors,
        )
        if stat.st_size != evidence.get("size_bytes"):
            errors.append(f"closed_file_{index}_size_mismatch")
        if stat.st_mtime_ns != evidence.get("modified_time_ns"):
            errors.append(f"closed_file_{index}_modified_time_mismatch")
        expected_modified_utc = datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat()
        if expected_modified_utc != evidence.get("modified_time_utc"):
            errors.append(f"closed_file_{index}_modified_time_utc_mismatch")
        if digest != evidence.get("sha256"):
            errors.append(f"closed_file_{index}_sha256_mismatch")
        try:
            scan = _scan_raw_file(
                candidate,
                routing_streams,
                canonical_frames,
                expected_session_id=manifest.get("session_id"),
                configured_products=configured_products,
            )
        except (OSError, EOFError, UnicodeError, ValueError) as exc:
            errors.append(f"closed_file_{index}_read_error: {exc}")
            continue
        observed_rows += scan["row_count"]
        observed_primary_envelopes += scan["primary_envelope_count"]
        observed_primary_malformed += scan["primary_malformed_count"]
        route_name = candidate.parent.name
        observed_route_counts[route_name] = observed_route_counts.get(route_name, 0) + scan["row_count"]
        for field in (
            "row_count",
            "first_recv_ts",
            "last_recv_ts",
            "first_event_ts",
            "last_event_ts",
        ):
            if scan[field] != evidence.get(field):
                errors.append(f"closed_file_{index}_{field}_mismatch")
        if scan["parse_error_count"]:
            errors.append(f"closed_file_{index}_contains_invalid_json")
        if scan["routing_metadata_error_count"]:
            errors.append(f"closed_file_{index}_routing_metadata_invalid")
        if scan["expected_routing_error_count"]:
            errors.append(f"closed_file_{index}_expected_routing_invalid")
        if scan["latency_annotation_error_count"]:
            errors.append(f"closed_file_{index}_latency_annotation_invalid")
        if scan["canonical_frame_error_count"]:
            errors.append(f"closed_file_{index}_canonical_frame_duplicate")

    on_disk_paths = {
        path.resolve()
        for path in session_root.rglob("*")
        if path.is_file()
        and (path.name.lower().endswith(".jsonl") or path.name.lower().endswith(".jsonl.gz"))
    }
    for path in sorted(on_disk_paths - declared_paths):
        errors.append(f"unlisted_raw_file: {path.relative_to(session_root).as_posix()}")
    for path in sorted(declared_paths - on_disk_paths):
        errors.append(f"declared_raw_file_missing: {path.relative_to(session_root).as_posix()}")

    destination_groups = {destinations for destinations, _route in routing_streams}
    for destinations in destination_groups:
        expected: tuple[int, str] | None = None
        for route in destinations:
            stream = routing_streams.get((destinations, route))
            if stream is None:
                errors.append(f"routing_replica_stream_missing: {route}")
                continue
            observed = (int(stream["count"]), stream["digest"].hexdigest())
            if expected is None:
                expected = observed
            elif observed != expected:
                errors.append(f"routing_replica_stream_mismatch: {route}")

    if observed_files != manifest_counts["closed_file_count"]:
        errors.append("closed_file_count_mismatch")
    if observed_rows != manifest_counts["routed_write_count"]:
        errors.append("routed_write_count_mismatch")
    route_total = sum(product_message_counts.values()) + sum(channel_message_counts.values())
    if route_total != manifest_counts["routed_write_count"]:
        errors.append("route_counter_total_mismatch")
    for product, count in product_message_counts.items():
        if observed_route_counts.get(f"product={product}", 0) != count:
            errors.append(f"product_route_count_mismatch: {product}")
    for channel, count in channel_message_counts.items():
        if observed_route_counts.get(f"channel={channel}", 0) != count:
            errors.append(f"channel_route_count_mismatch: {channel}")
    if observed_primary_envelopes != manifest_counts["received_envelope_count"]:
        errors.append("received_envelope_count_mismatch")
    if manifest_counts["message_count"] != manifest_counts["received_envelope_count"]:
        errors.append("message_count_semantics_mismatch")
    if (
        manifest_counts["received_frame_count"] is not None
        and manifest_counts["received_envelope_count"] is not None
        and manifest_counts["parse_error_count"] is not None
        and manifest_counts["received_frame_count"]
        != manifest_counts["received_envelope_count"] + manifest_counts["parse_error_count"]
    ):
        errors.append("received_frame_reconciliation_mismatch")
    if manifest_counts["parse_error_count"] != channel_message_counts.get(
        "malformed_json", 0
    ):
        errors.append("malformed_route_parse_error_mismatch")
    if observed_primary_malformed != manifest_counts["parse_error_count"]:
        errors.append("raw_malformed_frame_count_mismatch")

    raw_connection_history = _require_list(
        manifest.get("connection_history"),
        "connection_history",
        errors,
    )
    connection_history: list[dict[str, Any]] = []
    for index, row in enumerate(raw_connection_history):
        if not isinstance(row, dict):
            errors.append(f"connection_history_{index}_not_object")
            connection_history.append({})
        else:
            connection_history.append(row)
    if len(connection_history) != manifest_counts["successful_connection_count"]:
        errors.append("successful_connection_count_mismatch")
    if max(0, len(connection_history) - 1) != manifest_counts["reconnect_count"]:
        errors.append("reconnect_count_mismatch")
    if len(connect_failures) != manifest_counts["failed_connect_attempt_count"]:
        errors.append("failed_connect_attempt_count_mismatch")
    connection_envelopes = _sum_history_count(
        connection_history,
        "received_envelope_count",
        errors,
    )
    if connection_envelopes != manifest_counts["received_envelope_count"]:
        errors.append("connection_envelope_count_mismatch")
    raw_snapshot = _replay_raw_audit(canonical_frames, manifest, connection_history, errors)
    canonical_frames.close()
    for field in (
        "sequenced_envelope_count",
        "unsequenced_envelope_count",
        "malformed_sequence_count",
        "sequence_gap_count",
        "missing_sequence_count",
        "sequence_duplicate_count",
        "exact_sequence_duplicate_count",
        "conflicting_sequence_duplicate_count",
        "sequence_regression_count",
    ):
        history_total = _sum_history_count(connection_history, field, errors)
        summary_count = _require_natural_count(
            sequence_summary.get(field),
            f"sequence_summary_{field}",
            errors,
        )
        if history_total != summary_count:
            errors.append(f"sequence_summary_mismatch: {field}")
    for field in (
        "heartbeat_message_count",
        "stale_heartbeat_interval_count",
        "heartbeat_timeout_count",
        "heartbeat_counter_gap_count",
        "missed_heartbeat_counter_count",
        "heartbeat_counter_duplicate_count",
        "heartbeat_counter_regression_count",
    ):
        history_total = _sum_history_count(connection_history, field, errors)
        summary_count = _require_natural_count(
            heartbeat_summary.get(field),
            f"heartbeat_summary_{field}",
            errors,
        )
        if history_total != summary_count:
            errors.append(f"heartbeat_summary_mismatch: {field}")
    latency_counts = [
        _require_natural_count(
            latency_summary.get(field),
            f"latency_summary_{field}",
            errors,
        )
        for field in ("sample_count", "missing_count", "invalid_count")
    ]
    latency_accounted = (
        sum(count for count in latency_counts if count is not None)
        if all(count is not None for count in latency_counts)
        else None
    )
    if latency_accounted != manifest_counts["received_envelope_count"]:
        errors.append("latency_sample_reconciliation_mismatch")

    return {
        "valid": not errors,
        "manifest_path": str(manifest_file),
        "session_id": manifest.get("session_id"),
        "closed_files_verified": observed_files,
        "routed_rows_verified": observed_rows,
        "received_envelopes_verified": observed_primary_envelopes,
        "malformed_frames_verified": observed_primary_malformed,
        "raw_derived_fields_verified": {
            "sequence": True,
            "heartbeat_receive_intervals_and_counters": True,
            "latency": True,
        },
        "writer_attested_fields": [
            "connect_ts",
            "disconnect_ts",
            "disconnect_kind",
            "disconnect_reason",
            "retry_delay_seconds",
            "heartbeat_timeout_count",
            "connect_failures",
        ],
        "errors": errors,
        "warnings": warnings,
    }
