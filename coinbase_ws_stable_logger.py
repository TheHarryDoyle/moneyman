"""Coinbase Advanced Trade WebSocket raw logger.

This is recovered from the roadmap branch and hardened for MoneyMan's current
layout. It writes immutable compressed JSONL under MONEYMAN_RAW_ROOT instead of
creating product folders in the current working directory.
"""

from __future__ import annotations

import asyncio
import gzip
import json
import random
import re
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, TextIO

import websockets

from moneyman.coinbase import extract_product_ids, message_channel, utc_now_id
from moneyman.collector_audit import (
    AUDIT_CONTRACT,
    MANIFEST_SCHEMA,
    CollectorAuditState,
    atomic_write_json,
    capture_collector_provenance,
    closed_file_evidence,
    primary_event_timestamp,
)
from moneyman.logger_config import load_logger_config


LOGGER_CONFIG = load_logger_config()
WS_URL = LOGGER_CONFIG.ws_url
PRODUCT_IDS = LOGGER_CONFIG.products
CHANNELS = LOGGER_CONFIG.channels
RAW_ROOT = LOGGER_CONFIG.raw_root
RAW_ROOT_SOURCE = LOGGER_CONFIG.raw_root_source
ROLL_INTERVAL = LOGGER_CONFIG.roll_interval_seconds
FLUSH_INTERVAL = LOGGER_CONFIG.flush_interval_messages
PROGRESS_INTERVAL = LOGGER_CONFIG.progress_interval_messages
MANIFEST_INTERVAL = LOGGER_CONFIG.manifest_interval_messages
BASE_BACKOFF = 1.0
MAX_BACKOFF = 30.0
HEARTBEAT_DEAD_SECS = 15
INVALID_ROUTE_CHANNEL = "invalid_route"
_PRODUCT_ROUTE_RE = re.compile(r"^[A-Z0-9]+-[A-Z0-9]+$")
_CHANNEL_ROUTE_RE = re.compile(r"^[a-z0-9_]+$")
_FILE_COMPONENT_RE = re.compile(r"^[A-Za-z0-9_-]+$")


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso_now() -> str:
    return utc_now().isoformat()


def log(message: str) -> None:
    timestamp = utc_now().strftime("%Y-%m-%d %H:%M:%S UTC")
    print(f"[{timestamp}] {message}", flush=True)


def safe_name(value: str) -> str:
    """Return one collision-checkable filename component for a validated route."""

    if not isinstance(value, str) or not _FILE_COMPONENT_RE.fullmatch(value):
        raise ValueError(f"unsafe route component: {value!r}")
    safe = value.lower().replace("-", "_")
    if safe in {"", ".", ".."} or Path(safe).name != safe:
        raise ValueError(f"unsafe route component: {value!r}")
    return safe


def _is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True


def _confined_path(path: Path, session_root: Path) -> Path:
    resolved_root = session_root.resolve()
    resolved_path = path.resolve()
    if not _is_relative_to(resolved_path, resolved_root):
        raise ValueError(f"collector writer path escapes session root: {resolved_path}")
    return resolved_path


class RollingJsonlWriter:
    def __init__(
        self,
        folder: Path,
        stem: str,
        *,
        session_root: Path,
        on_file_closed: Callable[[dict[str, Any]], None],
    ) -> None:
        self.session_root = session_root.resolve()
        self.folder = _confined_path(folder, self.session_root)
        self.stem = safe_name(stem)
        self.on_file_closed = on_file_closed
        self.roll_at = utc_now()
        self.handle: TextIO | None = None
        self.current_path: Path | None = None
        self.opened_at: str | None = None
        self.current_file_message_count = 0
        self.first_recv_ts: str | None = None
        self.last_recv_ts: str | None = None
        self.first_event_ts: str | None = None
        self.last_event_ts: str | None = None
        self.part_index = 0
        self.flush_counter = 0
        self.message_count = 0
        self.folder.mkdir(parents=True, exist_ok=True)
        self._open_next_file()

    def _open_next_file(self) -> None:
        previous_path = self.current_path
        previous_count = self.current_file_message_count
        self.close()
        if previous_path is not None:
            log(f"Rolled {self.stem}: {previous_count:,} messages -> {previous_path}")
        timestamp = utc_now().strftime("%Y-%m-%d_%H-%M")
        path = _confined_path(
            self.folder / f"{self.stem}_part-{self.part_index:06d}_{timestamp}.jsonl.gz",
            self.session_root,
        )
        self.part_index += 1
        self.handle = gzip.open(path, "xt", encoding="utf-8")
        self.current_path = path
        self.opened_at = iso_now()
        self.current_file_message_count = 0
        self.first_recv_ts = None
        self.last_recv_ts = None
        self.first_event_ts = None
        self.last_event_ts = None
        self.roll_at = utc_now() + timedelta(seconds=ROLL_INTERVAL)
        log(f"Logging {self.stem} -> {path}")

    def write(self, payload: dict[str, Any]) -> None:
        if utc_now() >= self.roll_at:
            self._open_next_file()
        assert self.handle is not None
        self.handle.write(json.dumps(payload, sort_keys=True) + "\n")
        self.message_count += 1
        self.current_file_message_count += 1
        recv_ts = payload.get("_recv_ts")
        event_ts = primary_event_timestamp(payload)
        if isinstance(recv_ts, str) and recv_ts:
            self.first_recv_ts = self.first_recv_ts or recv_ts
            self.last_recv_ts = recv_ts
        if event_ts:
            self.first_event_ts = self.first_event_ts or event_ts
            self.last_event_ts = event_ts
        self.flush_counter += 1
        if self.flush_counter >= FLUSH_INTERVAL:
            self.handle.flush()
            self.flush_counter = 0

    def close(self) -> None:
        if self.handle is None:
            return
        handle = self.handle
        path = self.current_path
        opened_at = self.opened_at
        self.handle = None
        handle.flush()
        handle.close()
        if path is None or opened_at is None:
            raise RuntimeError("Closed writer was missing its file identity")
        evidence = closed_file_evidence(
            path=path,
            session_root=self.session_root,
            row_count=self.current_file_message_count,
            opened_at=opened_at,
            closed_at=iso_now(),
            first_recv_ts=self.first_recv_ts,
            last_recv_ts=self.last_recv_ts,
            first_event_ts=self.first_event_ts,
            last_event_ts=self.last_event_ts,
        )
        self.on_file_closed(evidence)
        self.current_path = None
        self.opened_at = None

    def open_file_evidence(self) -> dict[str, Any] | None:
        if self.handle is None or self.current_path is None:
            return None
        return {
            "relative_path": self.current_path.resolve().relative_to(self.session_root.resolve()).as_posix(),
            "absolute_path": str(self.current_path.resolve()),
            "row_count_so_far": self.current_file_message_count,
            "opened_at": self.opened_at,
            "first_recv_ts": self.first_recv_ts,
            "last_recv_ts": self.last_recv_ts,
            "first_event_ts": self.first_event_ts,
            "last_event_ts": self.last_event_ts,
            "close_status": "open",
        }


class CollectorSession:
    def __init__(
        self,
        raw_root: Path,
        products: list[str],
        channels: list[str],
        *,
        session_id: str | None = None,
        provenance: dict[str, Any] | None = None,
    ) -> None:
        products = list(products)
        channels = list(channels)
        invalid_products = [
            product_id
            for product_id in products
            if not isinstance(product_id, str) or not _PRODUCT_ROUTE_RE.fullmatch(product_id)
        ]
        invalid_channels = [
            channel
            for channel in channels
            if (
                not isinstance(channel, str)
                or not _CHANNEL_ROUTE_RE.fullmatch(channel)
                or channel == INVALID_ROUTE_CHANNEL
            )
        ]
        if invalid_products:
            raise ValueError(f"invalid configured product route(s): {invalid_products!r}")
        if invalid_channels:
            raise ValueError(f"invalid configured channel route(s): {invalid_channels!r}")
        duplicate_products = sorted(
            product_id for product_id in set(products) if products.count(product_id) > 1
        )
        duplicate_channels = sorted(
            channel for channel in set(channels) if channels.count(channel) > 1
        )
        if duplicate_products:
            raise ValueError(
                f"duplicate configured product route(s): {duplicate_products!r}"
            )
        if duplicate_channels:
            raise ValueError(
                f"duplicate configured channel route(s): {duplicate_channels!r}"
            )
        self.raw_root = raw_root
        self.products = products
        self.channels = channels
        self.session_id = session_id or f"{utc_now_id()}-{uuid.uuid4().hex[:12]}"
        session_component = f"session={self.session_id}"
        if Path(session_component).name != session_component:
            raise ValueError("session_id must be one path component")
        sessions_root = (raw_root / "coinbase_advanced_trade").resolve()
        self.session_root = _confined_path(
            sessions_root / session_component,
            sessions_root,
        )
        self.product_writers: dict[str, RollingJsonlWriter] = {}
        self.channel_writers: dict[str, RollingJsonlWriter] = {}
        self.started_at = iso_now()
        self.audit = CollectorAuditState(
            started_at=self.started_at,
            heartbeat_dead_seconds=HEARTBEAT_DEAD_SECS,
        )
        self.closed_files: list[dict[str, Any]] = []
        self.routed_write_count = 0
        self.product_message_counts = {product_id: 0 for product_id in products}
        self.channel_message_counts: dict[str, int] = {}
        effective_config = {
            "ws_url": WS_URL,
            "products": products,
            "channels": channels,
            "raw_root": str(raw_root.resolve()),
            "raw_root_source": RAW_ROOT_SOURCE,
            "roll_interval_seconds": ROLL_INTERVAL,
            "flush_interval_messages": FLUSH_INTERVAL,
            "progress_interval_messages": PROGRESS_INTERVAL,
            "manifest_interval_messages": MANIFEST_INTERVAL,
            "heartbeat_dead_seconds": HEARTBEAT_DEAD_SECS,
            "base_backoff_seconds": BASE_BACKOFF,
            "max_backoff_seconds": MAX_BACKOFF,
        }
        self.provenance = provenance or capture_collector_provenance(
            Path(__file__),
            effective_config,
            LOGGER_CONFIG.config_path,
        )
        sessions_root.mkdir(parents=True, exist_ok=True)
        self.session_root.mkdir(exist_ok=False)
        self._write_manifest(shutdown_reason=None)
        log(f"Logger config: {LOGGER_CONFIG.config_source}")
        log(f"Raw root: {self.raw_root.resolve()} ({RAW_ROOT_SOURCE})")
        log(f"Session root: {self.session_root.resolve()}")

    @property
    def message_count(self) -> int:
        return self.audit.received_envelope_count

    @property
    def parse_error_count(self) -> int:
        return self.audit.parse_error_count

    @property
    def active_connection_epoch(self) -> int | None:
        return self.audit.active_connection_epoch

    def _on_file_closed(self, evidence: dict[str, Any]) -> None:
        self.closed_files.append(evidence)

    def _open_files(self) -> list[dict[str, Any]]:
        rows = []
        for writer in [*self.product_writers.values(), *self.channel_writers.values()]:
            evidence = writer.open_file_evidence()
            if evidence is not None:
                rows.append(evidence)
        return sorted(rows, key=lambda row: row["relative_path"])

    def _write_manifest(
        self,
        shutdown_reason: str | None,
        *,
        close_errors: list[str] | None = None,
    ) -> None:
        audit = self.audit.snapshot()
        open_files = self._open_files()
        close_errors = close_errors or []
        ended = shutdown_reason is not None
        all_writers_closed = ended and not open_files and not close_errors
        manifest = {
            "manifest_schema": MANIFEST_SCHEMA,
            "audit_contract": AUDIT_CONTRACT,
            "session_id": self.session_id,
            "collector": "coinbase_ws_stable_logger.py",
            "config_source": LOGGER_CONFIG.config_source,
            "ws_url": WS_URL,
            "products": self.products,
            "channels": self.channels,
            "raw_root": str(self.raw_root.resolve()),
            "session_root": str(self.session_root.resolve()),
            "start_ts": self.started_at,
            "end_ts": iso_now() if ended else None,
            "shutdown_reason": shutdown_reason,
            "status": "closed" if all_writers_closed else ("close_failed" if ended else "open"),
            **audit,
            "routed_write_count": self.routed_write_count,
            "product_message_counts": self.product_message_counts,
            "channel_message_counts": self.channel_message_counts,
            "closed_file_count": len(self.closed_files),
            "closed_files": sorted(self.closed_files, key=lambda row: row["relative_path"]),
            "open_files": open_files,
            "session_end": {
                "all_writers_closed": all_writers_closed,
                "open_writer_count": len(open_files),
                "close_errors": close_errors,
                "final_manifest_written_after_file_close": all_writers_closed,
            },
            "layout": (
                "coinbase_advanced_trade/session=<SESSION>/"
                "{product=<PRODUCT>|channel=<CHANNEL>}/*.jsonl.gz"
            ),
            **self.provenance,
        }
        atomic_write_json(self.session_root / "manifest.json", manifest)

    def _product_writer(self, product_id: str) -> RollingJsonlWriter:
        if not _PRODUCT_ROUTE_RE.fullmatch(product_id):
            raise ValueError(f"invalid product route: {product_id!r}")
        if product_id not in self.product_writers:
            folder = _confined_path(self.session_root / f"product={product_id}", self.session_root)
            self.product_writers[product_id] = RollingJsonlWriter(
                folder,
                safe_name(product_id),
                session_root=self.session_root,
                on_file_closed=self._on_file_closed,
            )
        return self.product_writers[product_id]

    def _channel_writer(self, channel_route: str) -> RollingJsonlWriter:
        safe_route = safe_name(channel_route)
        if safe_route != channel_route:
            raise ValueError(f"channel route must already be canonical: {channel_route!r}")
        if channel_route not in self.channel_writers:
            folder = _confined_path(
                self.session_root / f"channel={channel_route}", self.session_root
            )
            self.channel_writers[channel_route] = RollingJsonlWriter(
                folder,
                channel_route,
                session_root=self.session_root,
                on_file_closed=self._on_file_closed,
            )
        return self.channel_writers[channel_route]

    def _validated_channel_route(self, channel: str) -> tuple[str, str | None]:
        if not _CHANNEL_ROUTE_RE.fullmatch(channel):
            return INVALID_ROUTE_CHANNEL, "invalid_channel_route"
        if channel == INVALID_ROUTE_CHANNEL:
            return INVALID_ROUTE_CHANNEL, "reserved_channel_route"
        return channel, None

    def record_received_frame(self) -> None:
        self.audit.record_received_frame()

    def start_connection(self, connected_at: str) -> int:
        epoch = self.audit.start_connection(connected_at=connected_at)
        self._write_manifest(shutdown_reason=None)
        return epoch

    def end_connection(
        self,
        *,
        disconnect_kind: str,
        reason: str,
        retry_delay_seconds: float | None,
    ) -> None:
        self.audit.end_connection(
            ended_at=iso_now(),
            disconnect_kind=disconnect_kind,
            reason=reason,
            retry_delay_seconds=retry_delay_seconds,
        )
        self._write_manifest(shutdown_reason=None)

    def record_connect_failure(
        self,
        *,
        error_kind: str,
        error: str,
        retry_delay_seconds: float,
    ) -> None:
        self.audit.record_connect_failure(
            observed_at=iso_now(),
            error_kind=error_kind,
            error=error,
            retry_delay_seconds=retry_delay_seconds,
        )

    def record_heartbeat_timeout(
        self,
        *,
        stale_for_seconds: float,
        last_heartbeat_recv_ts: str | None,
    ) -> None:
        self.audit.record_heartbeat_timeout(
            observed_at=iso_now(),
            stale_for_seconds=stale_for_seconds,
            last_heartbeat_recv_ts=last_heartbeat_recv_ts,
        )

    def write_message(self, payload: dict[str, Any]) -> None:
        observed_at = str(payload.get("_recv_ts") or iso_now())
        self.audit.observe_envelope(payload, observed_at=observed_at)
        channel = str(payload.get("channel") or payload.get("type") or "unknown")
        route_error: dict[str, Any] | None = None
        try:
            product_ids = extract_product_ids(payload)
        except (AttributeError, TypeError, ValueError) as exc:
            product_ids = set()
            route_error = {
                "reason": "invalid_product_id_type",
                "detail": f"{type(exc).__name__}: {exc}",
            }
        invalid_product_ids = sorted(
            product_id
            for product_id in product_ids
            if not _PRODUCT_ROUTE_RE.fullmatch(product_id)
        )
        if invalid_product_ids:
            route_error = {
                "reason": "invalid_product_id",
                "values": invalid_product_ids,
            }

        requested_products = (
            [pid for pid in sorted(product_ids) if pid in self.products]
            if route_error is None
            else []
        )
        channel_route: str | None = None
        if requested_products:
            destinations = [f"product={product_id}" for product_id in requested_products]
        else:
            if route_error is None:
                channel_route, channel_error = self._validated_channel_route(channel)
                if channel_error is not None:
                    route_error = {"reason": channel_error, "value": channel}
            else:
                channel_route = INVALID_ROUTE_CHANNEL
            assert channel_route is not None
            destinations = [f"channel={channel_route}"]

        payload["_collector_session_id"] = self.session_id
        payload["_connection_epoch"] = self.active_connection_epoch
        payload["_received_frame_ordinal"] = self.audit.received_frame_count
        payload["_parsed_envelope"] = True
        payload["_routed_destinations"] = destinations
        if route_error is not None:
            payload["_routing_error"] = route_error
        if requested_products:
            for product_id in requested_products:
                self.product_message_counts[product_id] += 1
                self._product_writer(product_id).write(payload)
        else:
            assert channel_route is not None
            self.channel_message_counts[channel_route] = (
                self.channel_message_counts.get(channel_route, 0) + 1
            )
            self._channel_writer(channel_route).write(payload)
        self.routed_write_count += len(destinations)
        if MANIFEST_INTERVAL > 0 and self.message_count % MANIFEST_INTERVAL == 0:
            self._write_manifest(shutdown_reason=None)
        if PROGRESS_INTERVAL > 0 and self.message_count % PROGRESS_INTERVAL == 0:
            product_counts = ", ".join(
                f"{product_id}={self.product_message_counts.get(product_id, 0):,}"
                for product_id in self.products
            )
            channel_counts = ", ".join(
                f"{channel}={count:,}" for channel, count in sorted(self.channel_message_counts.items())
            )
            detail = product_counts if not channel_counts else f"{product_counts}; {channel_counts}"
            log(f"Logged {self.message_count:,} messages ({detail})")

    def write_malformed_frame(self, *, raw: str, recv_ts: str, error: str) -> None:
        self.audit.record_parse_error(observed_at=recv_ts, error=error)
        channel = "malformed_json"
        destination = f"channel={safe_name(channel)}"
        wrapper = {
            "_recv_ts": recv_ts,
            "_collector_session_id": self.session_id,
            "_connection_epoch": self.active_connection_epoch,
            "_received_frame_ordinal": self.audit.received_frame_count,
            "_parsed_envelope": False,
            "_routed_destinations": [destination],
            "parse_error": error,
            "raw": raw,
        }
        self.channel_message_counts[channel] = self.channel_message_counts.get(channel, 0) + 1
        self._channel_writer(channel).write(wrapper)
        self.routed_write_count += 1

    def close(self, shutdown_reason: str) -> None:
        if self.active_connection_epoch is not None:
            self.audit.end_connection(
                ended_at=iso_now(),
                disconnect_kind="session_shutdown",
                reason=shutdown_reason,
                retry_delay_seconds=None,
            )
        close_errors: list[str] = []
        for writer in [*self.product_writers.values(), *self.channel_writers.values()]:
            try:
                writer.close()
            except Exception as exc:  # File-close failures belong in the final audit manifest.
                close_errors.append(f"{type(exc).__name__}: {exc}")
        self._write_manifest(shutdown_reason=shutdown_reason, close_errors=close_errors)


def build_subscriptions() -> list[dict[str, Any]]:
    subscriptions: list[dict[str, Any]] = []
    for channel in CHANNELS:
        if channel == "heartbeats":
            subscriptions.append({"type": "subscribe", "channel": channel})
        else:
            subscriptions.append({"type": "subscribe", "channel": channel, "product_ids": PRODUCT_IDS})
    return subscriptions


def annotate_message(payload: dict[str, Any], recv_ts: str) -> dict[str, Any]:
    payload["_channel"] = message_channel(payload)
    payload["_recv_ts"] = recv_ts
    try:
        source_ts = payload.get("time") or payload.get("timestamp")
        if source_ts:
            source_dt = datetime.fromisoformat(str(source_ts).replace("Z", "+00:00"))
            recv_dt = datetime.fromisoformat(recv_ts)
            payload["_latency_ms"] = (recv_dt - source_dt).total_seconds() * 1000
    except Exception as exc:
        payload["_latency_ms"] = None
        payload["_latency_error"] = str(exc)
    return payload


def enforce_heartbeat_freshness(
    session: CollectorSession,
    *,
    last_heartbeat_monotonic: float,
    observed_monotonic: float,
    last_heartbeat_recv_ts: str | None,
) -> None:
    """Reconnect on stale heartbeats even when other channels remain busy."""

    if "heartbeats" not in session.channels:
        return
    heartbeat_age = max(0.0, observed_monotonic - last_heartbeat_monotonic)
    if heartbeat_age <= HEARTBEAT_DEAD_SECS:
        return
    session.record_heartbeat_timeout(
        stale_for_seconds=heartbeat_age,
        last_heartbeat_recv_ts=last_heartbeat_recv_ts,
    )
    raise RuntimeError("Missed heartbeats; reconnecting")


async def subscribe_and_collect() -> None:
    session = CollectorSession(RAW_ROOT, PRODUCT_IDS, CHANNELS)
    attempt = 0
    shutdown_reason = "normal_exit"

    try:
        while True:
            try:
                log(f"Connecting to {WS_URL}")
                async with websockets.connect(
                    WS_URL,
                    ping_interval=20,
                    ping_timeout=15,
                    close_timeout=10,
                    max_size=None,
                ) as ws:
                    session.start_connection(iso_now())
                    attempt = 0
                    for subscription in build_subscriptions():
                        await ws.send(json.dumps(subscription))
                    log(f"Subscribed: {CHANNELS} products: {PRODUCT_IDS}")

                    last_heartbeat = time.monotonic()
                    last_heartbeat_recv_ts: str | None = None
                    while True:
                        try:
                            message = await asyncio.wait_for(ws.recv(), timeout=5)
                        except asyncio.TimeoutError:
                            pong_waiter = await ws.ping()
                            await asyncio.wait_for(pong_waiter, timeout=10)
                            heartbeat_age = time.monotonic() - last_heartbeat
                            if heartbeat_age > HEARTBEAT_DEAD_SECS:
                                session.record_heartbeat_timeout(
                                    stale_for_seconds=heartbeat_age,
                                    last_heartbeat_recv_ts=last_heartbeat_recv_ts,
                                )
                                raise RuntimeError("Missed heartbeats; reconnecting")
                            continue

                        recv_ts = iso_now()
                        frame_received_monotonic = time.monotonic()
                        session.record_received_frame()
                        try:
                            payload = json.loads(message)
                        except json.JSONDecodeError as exc:
                            session.write_malformed_frame(
                                raw=message,
                                recv_ts=recv_ts,
                                error=f"JSONDecodeError: {exc}",
                            )
                            enforce_heartbeat_freshness(
                                session,
                                last_heartbeat_monotonic=last_heartbeat,
                                observed_monotonic=frame_received_monotonic,
                                last_heartbeat_recv_ts=last_heartbeat_recv_ts,
                            )
                            continue
                        if not isinstance(payload, dict):
                            session.write_malformed_frame(
                                raw=message,
                                recv_ts=recv_ts,
                                error="JSON value is not an object",
                            )
                            enforce_heartbeat_freshness(
                                session,
                                last_heartbeat_monotonic=last_heartbeat,
                                observed_monotonic=frame_received_monotonic,
                                last_heartbeat_recv_ts=last_heartbeat_recv_ts,
                            )
                            continue

                        payload = annotate_message(payload, recv_ts)
                        if payload["_channel"] == "heartbeats":
                            last_heartbeat = frame_received_monotonic
                            last_heartbeat_recv_ts = recv_ts
                        session.write_message(payload)
                        enforce_heartbeat_freshness(
                            session,
                            last_heartbeat_monotonic=last_heartbeat,
                            observed_monotonic=frame_received_monotonic,
                            last_heartbeat_recv_ts=last_heartbeat_recv_ts,
                        )
                        attempt = 0

            except websockets.exceptions.ConnectionClosedError as exc:
                sleep_for = random.uniform(0, min(MAX_BACKOFF, BASE_BACKOFF * (2**attempt)))
                if session.active_connection_epoch is not None:
                    session.end_connection(
                        disconnect_kind="connection_closed_error",
                        reason=str(exc),
                        retry_delay_seconds=sleep_for,
                    )
                else:
                    session.record_connect_failure(
                        error_kind="connection_closed_error",
                        error=str(exc),
                        retry_delay_seconds=sleep_for,
                    )
                log(f"Soft disconnect: {exc}. Retrying in {sleep_for:.2f}s")
                attempt += 1
                await asyncio.sleep(sleep_for)
            except Exception as exc:
                sleep_for = random.uniform(0, min(MAX_BACKOFF, BASE_BACKOFF * (2**attempt)))
                if session.active_connection_epoch is not None:
                    session.end_connection(
                        disconnect_kind="collector_error",
                        reason=f"{type(exc).__name__}: {exc}",
                        retry_delay_seconds=sleep_for,
                    )
                else:
                    session.record_connect_failure(
                        error_kind=type(exc).__name__,
                        error=str(exc),
                        retry_delay_seconds=sleep_for,
                    )
                log(f"Unexpected collector error: {exc}. Retrying in {sleep_for:.2f}s")
                attempt += 1
                await asyncio.sleep(sleep_for)
    except asyncio.CancelledError:
        shutdown_reason = "cancelled"
        raise
    except KeyboardInterrupt:
        shutdown_reason = "keyboard_interrupt"
    finally:
        session.close(shutdown_reason=shutdown_reason)


if __name__ == "__main__":
    try:
        asyncio.run(subscribe_and_collect())
    except KeyboardInterrupt:
        log("KeyboardInterrupt received. Exiting cleanly.")
