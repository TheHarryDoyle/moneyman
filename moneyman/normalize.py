from __future__ import annotations

import hashlib
import heapq
import json
import os
import re
import sqlite3
from collections import Counter, OrderedDict, defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Iterable, Iterator, TextIO

from .coinbase import PRODUCT_RE, message_channel, normalize_product_id, session_id_from_path
from .collector_audit import audit_collector_session
from .inventory import discover_raw_files
from .raw import RawRecord, iter_jsonl


SCHEMA_VERSION = "normalization.v2"
DATASET_VERSION = "v2"
EXECUTION_SOURCE_BUNDLE_SCHEMA = "moneyman.normalization_execution_sources.v1"
EXECUTION_SOURCE_MODULES = (
    "moneyman.normalize",
    "moneyman.collector_audit",
    "moneyman.coinbase",
    "moneyman.raw",
    "moneyman.inventory",
)
NORMALIZER_COMPLETE_ZERO_FIELDS = (
    "per_file_missing_recv_ts",
    "per_file_invalid_recv_ts",
    "per_file_recv_ts_regressions",
    "sequence_gap_events",
    "observed_missing_sequence_numbers",
    "sequence_regressions",
    "malformed_sequence_envelopes",
    "unsequenced_envelopes",
    "conflicting_sequence_duplicates",
    "collector_parse_error_wrappers",
    "invalid_product_id_items",
)
NORMALIZER_COMPLETE_FAILURES = {
    "per_file_missing_recv_ts": "missing_recv_ts",
    "per_file_invalid_recv_ts": "invalid_recv_ts",
    "per_file_recv_ts_regressions": "per_file_recv_ts_regression",
    "sequence_gap_events": "sequence_gap",
    "observed_missing_sequence_numbers": "observed_missing_sequence_numbers",
    "sequence_regressions": "sequence_regression",
    "malformed_sequence_envelopes": "malformed_sequence",
    "unsequenced_envelopes": "unsequenced_envelope",
    "conflicting_sequence_duplicates": "conflicting_sequence_duplicate",
    "collector_parse_error_wrappers": "collector_parse_errors_present",
    "invalid_product_id_items": "invalid_product_id",
}
COLLECTOR_COMPLETE_SEQUENCE_FIELDS = (
    "unsequenced_envelope_count",
    "malformed_sequence_count",
    "sequence_gap_count",
    "missing_sequence_count",
    "sequence_regression_count",
    "conflicting_sequence_duplicate_count",
)
COLLECTOR_COMPLETE_SEQUENCE_FAILURES = {
    "unsequenced_envelope_count": "collector_unsequenced_envelope",
    "malformed_sequence_count": "collector_malformed_sequence",
    "sequence_gap_count": "collector_sequence_gap",
    "missing_sequence_count": "collector_missing_sequence",
    "sequence_regression_count": "collector_sequence_regression",
    "conflicting_sequence_duplicate_count": "collector_conflicting_sequence_duplicate",
}
TABLE_NAMES = (
    "trades",
    "l2_updates",
    "quotes",
    "candles",
    "heartbeats",
    "status",
    "control",
    "sessions",
)

COMMON_FIELDS = (
    "schema_version",
    "dataset_id",
    "recv_ts",
    "recv_ts_epoch_ns",
    "recv_ts_raw",
    "sequence_num",
    "connection_epoch",
    "received_frame_ordinal",
    "channel",
    "source_path",
    "source_line",
    "session_id",
    "envelope_sha256",
    "exchange_payload_sha256",
    "row_ordinal",
    "row_id",
)

TABLE_SCHEMAS: dict[str, tuple[str, ...]] = {
    "trades": COMMON_FIELDS
    + (
        "event_ts",
        "event_ts_epoch_ns",
        "event_ts_raw",
        "product_id",
        "trade_id",
        "side",
        "price",
        "size",
    ),
    "l2_updates": COMMON_FIELDS
    + (
        "event_ts",
        "event_ts_epoch_ns",
        "event_ts_raw",
        "product_id",
        "side",
        "price_level",
        "new_quantity",
        "event_type",
    ),
    "quotes": COMMON_FIELDS
    + (
        "event_ts",
        "event_ts_epoch_ns",
        "event_ts_raw",
        "product_id",
        "best_bid",
        "best_ask",
        "best_bid_quantity",
        "best_ask_quantity",
        "midpoint",
        "spread",
        "relative_spread",
        "last_price",
        "quote_source",
    ),
    "candles": COMMON_FIELDS
    + (
        "start_ts",
        "start_ts_epoch_ns",
        "start_ts_raw",
        "product_id",
        "open",
        "high",
        "low",
        "close",
        "volume",
        "candle_source",
    ),
    "heartbeats": COMMON_FIELDS
    + (
        "event_ts",
        "event_ts_epoch_ns",
        "event_ts_raw",
        "heartbeat_counter",
        "current_time_raw",
    ),
    "status": COMMON_FIELDS
    + (
        "event_ts",
        "event_ts_epoch_ns",
        "event_ts_raw",
        "product_id",
        "status",
        "status_message",
        "base_currency",
        "quote_currency",
        "base_increment",
        "quote_increment",
        "min_market_funds",
        "product_type",
        "display_name",
        "event_type",
    ),
    "control": COMMON_FIELDS
    + (
        "event_ts",
        "event_ts_epoch_ns",
        "event_ts_raw",
        "control_type",
        "event_type",
        "details_json",
    ),
    "sessions": (
        "schema_version",
        "dataset_id",
        "session_key",
        "session_id",
        "manifest_provenance",
        "manifest_path",
        "manifest_sha256",
        "collector",
        "collector_version",
        "git_commit",
        "host",
        "products_json",
        "channels_json",
        "start_ts",
        "start_ts_epoch_ns",
        "start_ts_raw",
        "end_ts",
        "end_ts_epoch_ns",
        "end_ts_raw",
        "raw_root",
        "shutdown_reason",
        "manifest_message_count",
        "manifest_gap_count",
        "manifest_duplicate_count",
        "selected_raw_file_count",
        "selected_input_records",
        "selected_canonical_envelopes",
        "selected_sequence_gap_count",
        "selected_sequence_regression_count",
        "selected_malformed_sequence_count",
        "selected_collector_parse_error_count",
        "selected_exact_duplicate_count",
        "selected_conflicting_duplicate_count",
        "selected_routing_replica_count",
    ),
}

QUARANTINE_FIELDS = (
    "schema_version",
    "dataset_id",
    "quarantine_id",
    "reason",
    "recv_ts_raw",
    "channel",
    "source_path",
    "source_line",
    "session_id",
    "envelope_sha256",
    "raw_line",
    "item_json",
)

_ISO_RE = re.compile(
    r"^(?P<base>\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2})"
    r"(?:\.(?P<fraction>\d+))?(?P<zone>Z|[+-]\d{2}:\d{2})$"
)
_EPOCH_RE = re.compile(r"^-?\d+(?:\.\d+)?$")
_UTC_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


class TimestampError(ValueError):
    pass


@dataclass(frozen=True)
class CanonicalTimestamp:
    text: str
    epoch_ns: int
    date: str
    raw: str


def canonical_timestamp(value: Any, *, allow_epoch_seconds: bool = False) -> CanonicalTimestamp:
    """Parse an aware timestamp without losing Coinbase's nanosecond text precision."""

    if value is None:
        raise TimestampError("timestamp is missing")
    raw = str(value).strip()
    if not raw:
        raise TimestampError("timestamp is empty")

    if allow_epoch_seconds and _EPOCH_RE.fullmatch(raw):
        try:
            epoch_decimal = Decimal(raw)
        except InvalidOperation as exc:
            raise TimestampError(f"invalid epoch seconds: {raw}") from exc
        epoch_ns_decimal = epoch_decimal * Decimal(1_000_000_000)
        if epoch_ns_decimal != epoch_ns_decimal.to_integral_value():
            raise TimestampError("epoch seconds exceed nanosecond precision")
        epoch_ns = int(epoch_ns_decimal)
        seconds, fraction_ns = divmod(epoch_ns, 1_000_000_000)
        try:
            utc_base = _UTC_EPOCH + timedelta(seconds=seconds)
        except (OverflowError, ValueError) as exc:
            raise TimestampError(f"epoch seconds out of range: {raw}") from exc
        fraction = f".{fraction_ns:09d}" if fraction_ns else ""
        text = utc_base.strftime("%Y-%m-%dT%H:%M:%S") + fraction + "Z"
        return CanonicalTimestamp(text=text, epoch_ns=epoch_ns, date=text[:10], raw=raw)

    match = _ISO_RE.fullmatch(raw)
    if not match:
        raise TimestampError("timestamp must be ISO-8601 with Z or an explicit offset")
    fraction_raw = match.group("fraction") or ""
    if len(fraction_raw) > 9:
        raise TimestampError("timestamp exceeds nanosecond precision")
    fraction_ns = int(fraction_raw.ljust(9, "0")) if fraction_raw else 0
    try:
        naive = datetime.strptime(match.group("base").replace(" ", "T"), "%Y-%m-%dT%H:%M:%S")
    except ValueError as exc:
        raise TimestampError(f"invalid calendar timestamp: {raw}") from exc

    zone = match.group("zone")
    if zone == "Z":
        offset = timedelta(0)
    else:
        sign = 1 if zone[0] == "+" else -1
        hours = int(zone[1:3])
        minutes = int(zone[4:6])
        if hours > 23 or minutes > 59:
            raise TimestampError(f"invalid UTC offset: {zone}")
        offset = sign * timedelta(hours=hours, minutes=minutes)
    aware = naive.replace(tzinfo=timezone(offset))
    utc_base = aware.astimezone(timezone.utc).replace(microsecond=0)
    delta = utc_base - _UTC_EPOCH
    epoch_seconds = delta.days * 86_400 + delta.seconds
    epoch_ns = epoch_seconds * 1_000_000_000 + fraction_ns
    fraction = f".{fraction_ns:09d}" if fraction_raw else ""
    text = utc_base.strftime("%Y-%m-%dT%H:%M:%S") + fraction + "Z"
    return CanonicalTimestamp(text=text, epoch_ns=epoch_ns, date=text[:10], raw=raw)


def _payload_sha256(payload: dict[str, Any]) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _exchange_payload_sha256(payload: dict[str, Any]) -> str:
    exchange_payload = {
        key: value for key, value in payload.items() if not str(key).startswith("_")
    }
    return _payload_sha256(exchange_payload)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _manifest_path_value(value: Any) -> Path:
    if not isinstance(value, str) or not value:
        raise ValueError("path must be a nonempty string")
    if "\x00" in value:
        raise ValueError("embedded null character in path")
    return Path(value)


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    if temporary.exists():
        raise RuntimeError(f"stale atomic-write temporary exists: {temporary}")
    with temporary.open("xt", encoding="utf-8", newline="\n") as handle:
        json.dump(payload, handle, indent=2, sort_keys=True)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def _source_route(path: Path) -> str:
    for part in reversed(path.parts):
        if part.startswith("product=") or part.startswith("channel="):
            return part
    return str(path.resolve())


def _session_root(path: Path) -> Path | None:
    current = path.parent
    while current != current.parent:
        if current.name.startswith("session="):
            return current
        current = current.parent
    return None


def _raw_source_boundary(path: Path) -> Path:
    session_root = _session_root(path)
    if session_root is not None and session_root.parent.name == "coinbase_advanced_trade":
        return session_root.parent.parent
    parts = path.resolve().parts
    for index, part in enumerate(parts):
        if part.lower() == "legacy_ws_data" and index > 0:
            return Path(*parts[:index])
    return session_root or path.parent


def _event_ts_raw(
    payload: dict[str, Any],
    event: dict[str, Any] | None = None,
    item: dict[str, Any] | None = None,
) -> Any:
    if item:
        for key in ("time", "event_time", "timestamp"):
            if item.get(key) is not None:
                return item.get(key)
    if event:
        for key in ("time", "event_time", "timestamp"):
            if event.get(key) is not None:
                return event.get(key)
    for key in ("timestamp", "time", "event_time"):
        if payload.get(key) is not None:
            return payload.get(key)
    return None


def _quarantine(
    record: RawRecord,
    reason: str,
    payload: dict[str, Any] | None = None,
    *,
    item: Any = None,
) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "reason": reason,
        "source_path": str(record.source_path.resolve()),
        "source_line": record.line_number,
        "session_id": session_id_from_path(record.source_path),
        "channel": message_channel(payload) if payload else None,
        "recv_ts_raw": (
            payload.get("_recv_ts") or payload.get("recv_ts") if payload else None
        ),
        "envelope_sha256": _payload_sha256(payload) if payload else None,
        "raw_line": record.raw_line,
        "item": item,
    }


def _finish_normalization(
    tables: dict[str, list[dict[str, Any]]],
    quarantine: list[dict[str, Any]],
    accounting: Counter[str],
) -> tuple[dict[str, list[dict[str, Any]]], list[dict[str, Any]], Counter[str]]:
    for table, rows in tables.items():
        for row_ordinal, row in enumerate(rows):
            row["row_ordinal"] = row_ordinal
            row["row_id"] = hashlib.sha256(
                (
                    f"{row.get('envelope_sha256')}|{table}|{row_ordinal}"
                ).encode("utf-8")
            ).hexdigest()
    return tables, quarantine, accounting


def _canonical_or_quarantine(
    raw_value: Any,
    record: RawRecord,
    payload: dict[str, Any],
    reason_prefix: str,
    quarantine: list[dict[str, Any]],
    *,
    item: Any,
    allow_epoch_seconds: bool = False,
) -> CanonicalTimestamp | None:
    try:
        return canonical_timestamp(raw_value, allow_epoch_seconds=allow_epoch_seconds)
    except TimestampError as exc:
        quarantine.append(
            _quarantine(record, f"{reason_prefix}: {exc}", payload, item=item)
        )
        return None


def _base_row(
    record: RawRecord,
    payload: dict[str, Any],
    channel: str,
    dataset_id: str,
    envelope_sha256: str,
    exchange_payload_sha256: str,
    recv_ts: CanonicalTimestamp | None,
) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "dataset_id": dataset_id,
        "recv_ts": recv_ts.text if recv_ts else None,
        "recv_ts_epoch_ns": recv_ts.epoch_ns if recv_ts else None,
        "recv_ts_raw": recv_ts.raw if recv_ts else None,
        "sequence_num": payload.get("sequence_num"),
        "connection_epoch": payload.get("_connection_epoch"),
        "received_frame_ordinal": payload.get("_received_frame_ordinal"),
        "channel": channel,
        "source_path": str(record.source_path.resolve()),
        "source_line": record.line_number,
        "session_id": session_id_from_path(record.source_path),
        "envelope_sha256": envelope_sha256,
        "exchange_payload_sha256": exchange_payload_sha256,
    }


def _event_fields(ts: CanonicalTimestamp) -> dict[str, Any]:
    return {
        "event_ts": ts.text,
        "event_ts_epoch_ns": ts.epoch_ns,
        "event_ts_raw": ts.raw,
    }


def _decimal_text(value: Any) -> str | None:
    if value is None:
        return None
    try:
        return str(Decimal(str(value)))
    except (InvalidOperation, ValueError):
        return None


def _sequence_number_error(payload: dict[str, Any]) -> str | None:
    """Return a quarantine reason for a present malformed Coinbase sequence."""

    if "sequence_num" not in payload or payload.get("sequence_num") is None:
        return None
    value = payload.get("sequence_num")
    if isinstance(value, bool) or not isinstance(value, int):
        return "invalid_sequence_num_type"
    if value < 0:
        return "invalid_sequence_num_negative"
    return None


def _valid_sequence_number(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _count_or_none(counts: dict[str, Any] | Counter[str], field: str) -> int | None:
    value = counts.get(field, 0)
    return value if _valid_sequence_number(value) else None


def _strict_product_id(value: Any) -> tuple[str | None, bool]:
    """Return a canonical Coinbase product or mark a present malformed value invalid."""

    if value is None:
        return None, False
    if not isinstance(value, str) or not value.strip():
        return None, True
    product_id = normalize_product_id(value)
    if product_id is None or PRODUCT_RE.fullmatch(product_id) is None:
        return None, True
    return product_id, False


def _quote_values(ticker: dict[str, Any]) -> dict[str, str | None] | None:
    bid = _decimal_text(ticker.get("best_bid"))
    ask = _decimal_text(ticker.get("best_ask"))
    if bid is None or ask is None:
        return None
    bid_decimal = Decimal(bid)
    ask_decimal = Decimal(ask)
    midpoint = (bid_decimal + ask_decimal) / Decimal(2)
    spread = ask_decimal - bid_decimal
    relative = spread / midpoint if midpoint else None
    return {
        "best_bid": bid,
        "best_ask": ask,
        "best_bid_quantity": _decimal_text(ticker.get("best_bid_quantity")),
        "best_ask_quantity": _decimal_text(ticker.get("best_ask_quantity")),
        "midpoint": str(midpoint),
        "spread": str(spread),
        "relative_spread": str(relative) if relative is not None else None,
        "last_price": _decimal_text(ticker.get("price")),
    }


def normalize_envelope(
    record: RawRecord,
    *,
    dataset_id: str,
) -> tuple[dict[str, list[dict[str, Any]]], list[dict[str, Any]], Counter[str]]:
    """Normalize one canonical envelope. The caller owns cross-shard replica handling."""

    tables = {name: [] for name in TABLE_NAMES if name != "sessions"}
    quarantine: list[dict[str, Any]] = []
    accounting: Counter[str] = Counter()
    if record.error:
        quarantine.append(_quarantine(record, f"malformed_json: {record.error}"))
        accounting["semantic_items_seen"] += 1
        accounting["semantic_items_quarantined"] += 1
        return _finish_normalization(tables, quarantine, accounting)
    if record.payload is None:
        quarantine.append(_quarantine(record, "missing_payload"))
        accounting["semantic_items_seen"] += 1
        accounting["semantic_items_quarantined"] += 1
        return _finish_normalization(tables, quarantine, accounting)

    payload = record.payload
    if payload.get("_parsed_envelope") is False:
        quarantine.append(
            _quarantine(record, "collector_parse_error_wrapper", payload, item=payload.get("raw"))
        )
        accounting["semantic_items_seen"] += 1
        accounting["semantic_items_quarantined"] += 1
        accounting["collector_parse_error_items"] += 1
        return _finish_normalization(tables, quarantine, accounting)

    sequence_error = _sequence_number_error(payload)
    if sequence_error is not None:
        quarantine.append(_quarantine(record, sequence_error, payload, item=payload.get("sequence_num")))
        accounting["semantic_items_seen"] += 1
        accounting["semantic_items_quarantined"] += 1
        accounting["malformed_sequence_items"] += 1
        return _finish_normalization(tables, quarantine, accounting)

    channel = message_channel(payload)
    envelope_sha256 = _payload_sha256(payload)
    exchange_payload_sha256 = _exchange_payload_sha256(payload)
    recv_raw = payload.get("_recv_ts") or payload.get("recv_ts")
    recv_ts: CanonicalTimestamp | None = None
    if recv_raw is not None:
        recv_ts = _canonical_or_quarantine(
            recv_raw,
            record,
            payload,
            "invalid_recv_ts",
            quarantine,
            item=None,
        )
        if recv_ts is None:
            accounting["semantic_items_seen"] += 1
            accounting["semantic_items_quarantined"] += 1
            return _finish_normalization(tables, quarantine, accounting)

    base = _base_row(
        record,
        payload,
        channel,
        dataset_id,
        envelope_sha256,
        exchange_payload_sha256,
        recv_ts,
    )
    events = payload.get("events")
    if channel == "error" and not isinstance(events, list):
        accounting["semantic_items_seen"] += 1
        event_raw = _event_ts_raw(payload)
        if event_raw is None:
            event_fields = {
                "event_ts": None,
                "event_ts_epoch_ns": None,
                "event_ts_raw": None,
            }
            accounting["control_missing_event_ts"] += 1
        else:
            event_ts = _canonical_or_quarantine(
                event_raw,
                record,
                payload,
                "invalid_control_event_ts",
                quarantine,
                item=payload,
            )
            if event_ts is None:
                accounting["semantic_items_quarantined"] += 1
                return _finish_normalization(tables, quarantine, accounting)
            event_fields = _event_fields(event_ts)
        if not quarantine:
            details = {
                key: value for key, value in payload.items() if not str(key).startswith("_")
            }
            tables["control"].append(
                {
                    **base,
                    **event_fields,
                    "control_type": "error",
                    "event_type": payload.get("type") or "error",
                    "details_json": json.dumps(details, sort_keys=True, separators=(",", ":")),
                }
            )
            accounting["semantic_items_emitted"] += 1
        return _finish_normalization(tables, quarantine, accounting)
    if not isinstance(events, list):
        quarantine.append(_quarantine(record, "events_not_list", payload))
        accounting["semantic_items_seen"] += 1
        accounting["semantic_items_quarantined"] += 1
        return _finish_normalization(tables, quarantine, accounting)

    if channel == "market_trades":
        for event in events:
            if not isinstance(event, dict):
                quarantine.append(_quarantine(record, "trade_event_not_object", payload, item=event))
                accounting["semantic_items_seen"] += 1
                accounting["semantic_items_quarantined"] += 1
                continue
            items = event.get("trades")
            if not isinstance(items, list):
                quarantine.append(_quarantine(record, "trades_not_list", payload, item=event))
                accounting["semantic_items_seen"] += 1
                accounting["semantic_items_quarantined"] += 1
                continue
            for trade in items:
                accounting["semantic_items_seen"] += 1
                if not isinstance(trade, dict):
                    quarantine.append(_quarantine(record, "trade_not_object", payload, item=trade))
                    accounting["semantic_items_quarantined"] += 1
                    continue
                raw_product_id = trade.get("product_id")
                if raw_product_id is None:
                    raw_product_id = event.get("product_id")
                product_id, invalid_product_id = _strict_product_id(raw_product_id)
                if invalid_product_id:
                    quarantine.append(
                        _quarantine(record, "invalid_product_id", payload, item=trade)
                    )
                    accounting["semantic_items_quarantined"] += 1
                    accounting["invalid_product_id_items"] += 1
                    continue
                if not product_id or trade.get("price") is None or trade.get("size") is None:
                    quarantine.append(_quarantine(record, "trade_missing_required_field", payload, item=trade))
                    accounting["semantic_items_quarantined"] += 1
                    continue
                event_ts = _canonical_or_quarantine(
                    _event_ts_raw(payload, event, trade),
                    record,
                    payload,
                    "invalid_trade_event_ts",
                    quarantine,
                    item=trade,
                )
                if event_ts is None:
                    accounting["semantic_items_quarantined"] += 1
                    continue
                tables["trades"].append(
                    {
                        **base,
                        **_event_fields(event_ts),
                        "product_id": product_id,
                        "trade_id": trade.get("trade_id"),
                        "side": str(trade.get("side")).lower() if trade.get("side") else None,
                        "price": str(trade.get("price")),
                        "size": str(trade.get("size")),
                    }
                )
                accounting["semantic_items_emitted"] += 1
        return _finish_normalization(tables, quarantine, accounting)

    if channel in {"l2_data", "level2"}:
        for event in events:
            if not isinstance(event, dict):
                quarantine.append(_quarantine(record, "l2_event_not_object", payload, item=event))
                accounting["semantic_items_seen"] += 1
                accounting["semantic_items_quarantined"] += 1
                continue
            items = event.get("updates")
            if not isinstance(items, list):
                quarantine.append(_quarantine(record, "l2_updates_not_list", payload, item=event))
                accounting["semantic_items_seen"] += 1
                accounting["semantic_items_quarantined"] += 1
                continue
            for update in items:
                accounting["semantic_items_seen"] += 1
                if not isinstance(update, dict):
                    quarantine.append(_quarantine(record, "l2_update_not_object", payload, item=update))
                    accounting["semantic_items_quarantined"] += 1
                    continue
                raw_product_id = update.get("product_id")
                if raw_product_id is None:
                    raw_product_id = event.get("product_id")
                product_id, invalid_product_id = _strict_product_id(raw_product_id)
                side = update.get("side")
                price = update.get("price_level") if update.get("price_level") is not None else update.get("price")
                quantity = update.get("new_quantity")
                if quantity is None:
                    quantity = update.get("quantity") if update.get("quantity") is not None else update.get("size")
                if invalid_product_id:
                    quarantine.append(
                        _quarantine(record, "invalid_product_id", payload, item=update)
                    )
                    accounting["semantic_items_quarantined"] += 1
                    accounting["invalid_product_id_items"] += 1
                    continue
                if not product_id or not side or price is None or quantity is None:
                    quarantine.append(_quarantine(record, "l2_update_missing_required_field", payload, item=update))
                    accounting["semantic_items_quarantined"] += 1
                    continue
                event_ts = _canonical_or_quarantine(
                    _event_ts_raw(payload, event, update),
                    record,
                    payload,
                    "invalid_l2_event_ts",
                    quarantine,
                    item=update,
                )
                if event_ts is None:
                    accounting["semantic_items_quarantined"] += 1
                    continue
                tables["l2_updates"].append(
                    {
                        **base,
                        **_event_fields(event_ts),
                        "product_id": product_id,
                        "side": str(side).lower(),
                        "price_level": str(price),
                        "new_quantity": str(quantity),
                        "event_type": event.get("type"),
                    }
                )
                accounting["semantic_items_emitted"] += 1
        return _finish_normalization(tables, quarantine, accounting)

    if channel in {"ticker", "ticker_batch"}:
        for event in events:
            if not isinstance(event, dict):
                quarantine.append(_quarantine(record, "ticker_event_not_object", payload, item=event))
                accounting["semantic_items_seen"] += 1
                accounting["semantic_items_quarantined"] += 1
                continue
            items = event.get("tickers")
            if not isinstance(items, list):
                quarantine.append(_quarantine(record, "tickers_not_list", payload, item=event))
                accounting["semantic_items_seen"] += 1
                accounting["semantic_items_quarantined"] += 1
                continue
            for ticker in items:
                accounting["semantic_items_seen"] += 1
                if not isinstance(ticker, dict):
                    quarantine.append(_quarantine(record, "ticker_not_object", payload, item=ticker))
                    accounting["semantic_items_quarantined"] += 1
                    continue
                raw_product_id = ticker.get("product_id")
                if raw_product_id is None:
                    raw_product_id = event.get("product_id")
                product_id, invalid_product_id = _strict_product_id(raw_product_id)
                if invalid_product_id:
                    quarantine.append(
                        _quarantine(record, "invalid_product_id", payload, item=ticker)
                    )
                    accounting["semantic_items_quarantined"] += 1
                    accounting["invalid_product_id_items"] += 1
                    continue
                if not product_id:
                    quarantine.append(_quarantine(record, "ticker_missing_product", payload, item=ticker))
                    accounting["semantic_items_quarantined"] += 1
                    continue
                quote_values = _quote_values(ticker)
                if quote_values is None:
                    accounting["semantic_items_recognized_nonemitting"] += 1
                    accounting["ticker_without_bbo"] += 1
                    continue
                event_ts = _canonical_or_quarantine(
                    _event_ts_raw(payload, event, ticker),
                    record,
                    payload,
                    "invalid_quote_event_ts",
                    quarantine,
                    item=ticker,
                )
                if event_ts is None:
                    accounting["semantic_items_quarantined"] += 1
                    continue
                tables["quotes"].append(
                    {
                        **base,
                        **_event_fields(event_ts),
                        "product_id": product_id,
                        **quote_values,
                        "quote_source": channel,
                    }
                )
                accounting["semantic_items_emitted"] += 1
        return _finish_normalization(tables, quarantine, accounting)

    if channel == "candles":
        for event in events:
            if not isinstance(event, dict):
                quarantine.append(_quarantine(record, "candle_event_not_object", payload, item=event))
                accounting["semantic_items_seen"] += 1
                accounting["semantic_items_quarantined"] += 1
                continue
            items = event.get("candles")
            if not isinstance(items, list):
                quarantine.append(_quarantine(record, "candles_not_list", payload, item=event))
                accounting["semantic_items_seen"] += 1
                accounting["semantic_items_quarantined"] += 1
                continue
            for candle in items:
                accounting["semantic_items_seen"] += 1
                if not isinstance(candle, dict):
                    quarantine.append(_quarantine(record, "candle_not_object", payload, item=candle))
                    accounting["semantic_items_quarantined"] += 1
                    continue
                raw_product_id = candle.get("product_id")
                if raw_product_id is None:
                    raw_product_id = event.get("product_id")
                product_id, invalid_product_id = _strict_product_id(raw_product_id)
                required = ("open", "high", "low", "close", "volume", "start")
                if invalid_product_id:
                    quarantine.append(
                        _quarantine(record, "invalid_product_id", payload, item=candle)
                    )
                    accounting["semantic_items_quarantined"] += 1
                    accounting["invalid_product_id_items"] += 1
                    continue
                if not product_id or any(candle.get(key) is None for key in required):
                    quarantine.append(_quarantine(record, "candle_missing_required_field", payload, item=candle))
                    accounting["semantic_items_quarantined"] += 1
                    continue
                start_ts = _canonical_or_quarantine(
                    candle.get("start"),
                    record,
                    payload,
                    "invalid_candle_start_ts",
                    quarantine,
                    item=candle,
                    allow_epoch_seconds=True,
                )
                if start_ts is None:
                    accounting["semantic_items_quarantined"] += 1
                    continue
                tables["candles"].append(
                    {
                        **base,
                        "start_ts": start_ts.text,
                        "start_ts_epoch_ns": start_ts.epoch_ns,
                        "start_ts_raw": start_ts.raw,
                        "product_id": product_id,
                        "open": str(candle.get("open")),
                        "high": str(candle.get("high")),
                        "low": str(candle.get("low")),
                        "close": str(candle.get("close")),
                        "volume": str(candle.get("volume")),
                        "candle_source": "coinbase_advanced_trade_ws",
                    }
                )
                accounting["semantic_items_emitted"] += 1
        return _finish_normalization(tables, quarantine, accounting)

    if channel == "heartbeats":
        for event in events:
            accounting["semantic_items_seen"] += 1
            if not isinstance(event, dict):
                quarantine.append(_quarantine(record, "heartbeat_event_not_object", payload, item=event))
                accounting["semantic_items_quarantined"] += 1
                continue
            event_ts = _canonical_or_quarantine(
                _event_ts_raw(payload, event),
                record,
                payload,
                "invalid_heartbeat_event_ts",
                quarantine,
                item=event,
            )
            if event_ts is None:
                accounting["semantic_items_quarantined"] += 1
                continue
            tables["heartbeats"].append(
                {
                    **base,
                    **_event_fields(event_ts),
                    "heartbeat_counter": event.get("heartbeat_counter"),
                    "current_time_raw": event.get("current_time"),
                }
            )
            accounting["semantic_items_emitted"] += 1
        return _finish_normalization(tables, quarantine, accounting)

    if channel == "status":
        for event in events:
            if not isinstance(event, dict):
                quarantine.append(_quarantine(record, "status_event_not_object", payload, item=event))
                accounting["semantic_items_seen"] += 1
                accounting["semantic_items_quarantined"] += 1
                continue
            products = event.get("products")
            if not isinstance(products, list):
                quarantine.append(_quarantine(record, "status_products_not_list", payload, item=event))
                accounting["semantic_items_seen"] += 1
                accounting["semantic_items_quarantined"] += 1
                continue
            for product in products:
                accounting["semantic_items_seen"] += 1
                if not isinstance(product, dict):
                    quarantine.append(_quarantine(record, "status_product_not_object", payload, item=product))
                    accounting["semantic_items_quarantined"] += 1
                    continue
                raw_product_id = product.get("id")
                if raw_product_id is None:
                    raw_product_id = product.get("product_id")
                product_id, invalid_product_id = _strict_product_id(raw_product_id)
                if invalid_product_id:
                    quarantine.append(
                        _quarantine(record, "invalid_product_id", payload, item=product)
                    )
                    accounting["semantic_items_quarantined"] += 1
                    accounting["invalid_product_id_items"] += 1
                    continue
                if not product_id:
                    quarantine.append(_quarantine(record, "status_missing_product", payload, item=product))
                    accounting["semantic_items_quarantined"] += 1
                    continue
                event_ts = _canonical_or_quarantine(
                    _event_ts_raw(payload, event),
                    record,
                    payload,
                    "invalid_status_event_ts",
                    quarantine,
                    item=product,
                )
                if event_ts is None:
                    accounting["semantic_items_quarantined"] += 1
                    continue
                tables["status"].append(
                    {
                        **base,
                        **_event_fields(event_ts),
                        "product_id": product_id,
                        "status": product.get("status"),
                        "status_message": product.get("status_message"),
                        "base_currency": product.get("base_currency"),
                        "quote_currency": product.get("quote_currency"),
                        "base_increment": product.get("base_increment"),
                        "quote_increment": product.get("quote_increment"),
                        "min_market_funds": product.get("min_market_funds"),
                        "product_type": product.get("product_type"),
                        "display_name": product.get("display_name"),
                        "event_type": event.get("type"),
                    }
                )
                accounting["semantic_items_emitted"] += 1
        return _finish_normalization(tables, quarantine, accounting)

    if channel in {"subscriptions", "subscription", "error"}:
        for event in events or [{}]:
            accounting["semantic_items_seen"] += 1
            if not isinstance(event, dict):
                quarantine.append(_quarantine(record, "control_event_not_object", payload, item=event))
                accounting["semantic_items_quarantined"] += 1
                continue
            event_ts = _canonical_or_quarantine(
                _event_ts_raw(payload, event),
                record,
                payload,
                "invalid_control_event_ts",
                quarantine,
                item=event,
            )
            if event_ts is None:
                accounting["semantic_items_quarantined"] += 1
                continue
            details = event.get("subscriptions") if channel in {"subscriptions", "subscription"} else event
            tables["control"].append(
                {
                    **base,
                    **_event_fields(event_ts),
                    "control_type": channel,
                    "event_type": event.get("type"),
                    "details_json": json.dumps(details, sort_keys=True, separators=(",", ":")),
                }
            )
            accounting["semantic_items_emitted"] += 1
        return _finish_normalization(tables, quarantine, accounting)

    accounting["semantic_items_seen"] += 1
    accounting["semantic_items_recognized_nonemitting"] += 1
    accounting[f"ignored_channel:{channel}"] += 1
    return _finish_normalization(tables, quarantine, accounting)


def normalize_record(record: RawRecord) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    """Compatibility API for the v1 feature fixture; new runs use normalize_envelope."""

    tables, quarantine, _ = normalize_envelope(record, dataset_id="compatibility")
    return tables["trades"], tables["l2_updates"], quarantine


class PartitionWriter:
    def __init__(self, root: Path, dataset_id: str, *, max_open_files: int = 32) -> None:
        self.root = root
        self.dataset_id = dataset_id
        self.max_open_files = max_open_files
        self._handles: OrderedDict[Path, TextIO] = OrderedDict()
        self._created: set[Path] = set()
        self._rows: Counter[Path] = Counter()
        self._table_for_path: dict[Path, str] = {}
        self._partition_for_path: dict[Path, dict[str, str]] = {}

    @staticmethod
    def _safe_partition(value: str | None) -> str:
        if not value:
            return "__none__"
        return re.sub(r"[^A-Za-z0-9_.-]", "_", value)

    def _path_for(self, table: str, row: dict[str, Any]) -> tuple[Path, dict[str, str]]:
        if table == "candles":
            date = str(row.get("start_ts") or "unknown")[:10]
        elif table == "sessions":
            date = str(row.get("start_ts") or "unknown")[:10]
        else:
            date = str(row.get("event_ts") or row.get("recv_ts") or "unknown")[:10]
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date):
            date = "unknown"
        raw_product = row.get("product_id")
        if raw_product is None:
            product = "__none__"
        elif not isinstance(raw_product, str) or PRODUCT_RE.fullmatch(raw_product) is None:
            raise ValueError(f"invalid product_id reached partition writer: {raw_product!r}")
        else:
            product = raw_product
        partition = {"product": product, "date": date}
        path = (
            self.root
            / DATASET_VERSION
            / table
            / f"product={product}"
            / f"date={date}"
            / f"part-{self.dataset_id}.jsonl"
        )
        return path, partition

    def _handle(self, path: Path) -> TextIO:
        if path in self._handles:
            handle = self._handles.pop(path)
            self._handles[path] = handle
            return handle
        if len(self._handles) >= self.max_open_files:
            _, oldest = self._handles.popitem(last=False)
            oldest.close()
        path.parent.mkdir(parents=True, exist_ok=True)
        mode = "at" if path in self._created else "xt"
        handle = path.open(mode, encoding="utf-8", newline="\n")
        self._created.add(path)
        self._handles[path] = handle
        return handle

    def write(self, table: str, row: dict[str, Any]) -> None:
        expected = TABLE_SCHEMAS[table]
        normalized = {field: row.get(field) for field in expected}
        extras = sorted(set(row) - set(expected))
        if extras:
            raise ValueError(f"{table} row has fields outside {SCHEMA_VERSION}: {extras}")
        path, partition = self._path_for(table, normalized)
        self._handle(path).write(json.dumps(normalized, sort_keys=True, separators=(",", ":")) + "\n")
        self._rows[path] += 1
        self._table_for_path[path] = table
        self._partition_for_path[path] = partition

    def write_quarantine(self, row: dict[str, Any]) -> None:
        normalized = {field: row.get(field) for field in QUARANTINE_FIELDS}
        extras = sorted(set(row) - set(QUARANTINE_FIELDS))
        if extras:
            raise ValueError(f"quarantine row has fields outside {SCHEMA_VERSION}: {extras}")
        reason = self._safe_partition(str(normalized.get("reason") or "unknown"))
        date = "unknown"
        recv_raw = normalized.get("recv_ts_raw")
        if recv_raw is not None:
            try:
                date = canonical_timestamp(recv_raw).date
            except TimestampError:
                pass
        partition = {"reason": reason, "date": date}
        path = (
            self.root
            / DATASET_VERSION
            / "records"
            / f"reason={reason}"
            / f"date={date}"
            / f"part-{self.dataset_id}.jsonl"
        )
        self._handle(path).write(json.dumps(normalized, sort_keys=True, separators=(",", ":")) + "\n")
        self._rows[path] += 1
        self._table_for_path[path] = "_quarantine"
        self._partition_for_path[path] = partition

    def close(self) -> None:
        while self._handles:
            _, handle = self._handles.popitem(last=False)
            handle.close()

    def artifacts(self) -> list[dict[str, Any]]:
        self.close()
        artifacts: list[dict[str, Any]] = []
        for path in sorted(self._rows, key=lambda item: str(item)):
            artifacts.append(
                {
                    "table": self._table_for_path[path],
                    "partition": self._partition_for_path[path],
                    "path": str(path.resolve()),
                    "rows": self._rows[path],
                    "bytes": path.stat().st_size,
                    "sha256": _sha256_file(path),
                }
            )
        return artifacts


class LatencyDistribution:
    BOUNDS_MS = (-1000, -100, -10, 0, 1, 2, 5, 10, 20, 50, 100, 200, 500, 1000, 5000, 10000)

    def __init__(self) -> None:
        self.count = 0
        self.total_ns = 0
        self.minimum_ns: int | None = None
        self.maximum_ns: int | None = None
        self.bins: Counter[str] = Counter()

    def add(self, latency_ns: int) -> None:
        self.count += 1
        self.total_ns += latency_ns
        self.minimum_ns = latency_ns if self.minimum_ns is None else min(self.minimum_ns, latency_ns)
        self.maximum_ns = latency_ns if self.maximum_ns is None else max(self.maximum_ns, latency_ns)
        latency_ms = Decimal(latency_ns) / Decimal(1_000_000)
        lower: int | None = None
        for upper in self.BOUNDS_MS:
            if latency_ms < upper:
                label = f"[{lower if lower is not None else '-inf'},{upper})"
                self.bins[label] += 1
                return
            lower = upper
        self.bins[f"[{lower},inf)"] += 1

    def report(self) -> dict[str, Any]:
        return {
            "count": self.count,
            "min_ms": str(Decimal(self.minimum_ns) / Decimal(1_000_000)) if self.minimum_ns is not None else None,
            "max_ms": str(Decimal(self.maximum_ns) / Decimal(1_000_000)) if self.maximum_ns is not None else None,
            "mean_ms": str(Decimal(self.total_ns) / Decimal(self.count) / Decimal(1_000_000)) if self.count else None,
            "histogram_ms": dict(sorted(self.bins.items())),
        }


class TimeCoverage:
    def __init__(self) -> None:
        self._values: dict[str, dict[str, list[Any]]] = defaultdict(dict)

    def add(self, group: str, key: str, row: dict[str, Any]) -> None:
        event_text = row.get("event_ts") or row.get("start_ts") or row.get("start_ts")
        event_ns = row.get("event_ts_epoch_ns") or row.get("start_ts_epoch_ns")
        recv_text = row.get("recv_ts")
        recv_ns = row.get("recv_ts_epoch_ns")
        bucket = self._values[group].setdefault(
            key,
            [None, None, None, None, None, None, None, None],
        )
        if isinstance(event_ns, int) and isinstance(event_text, str):
            if bucket[0] is None or event_ns < bucket[0]:
                bucket[0], bucket[1] = event_ns, event_text
            if bucket[2] is None or event_ns > bucket[2]:
                bucket[2], bucket[3] = event_ns, event_text
        if isinstance(recv_ns, int) and isinstance(recv_text, str):
            if bucket[4] is None or recv_ns < bucket[4]:
                bucket[4], bucket[5] = recv_ns, recv_text
            if bucket[6] is None or recv_ns > bucket[6]:
                bucket[6], bucket[7] = recv_ns, recv_text

    def report(self) -> dict[str, Any]:
        output: dict[str, Any] = {}
        for group, entries in sorted(self._values.items()):
            output[group] = {}
            for key, values in sorted(entries.items()):
                output[group][key] = {
                    "first_event_ts_epoch_ns": values[0],
                    "first_event_ts": values[1],
                    "last_event_ts_epoch_ns": values[2],
                    "last_event_ts": values[3],
                    "first_recv_ts_epoch_ns": values[4],
                    "first_recv_ts": values[5],
                    "last_recv_ts_epoch_ns": values[6],
                    "last_recv_ts": values[7],
                }
        return output


def _record_recv_epoch_ns(record: RawRecord) -> int | None:
    if record.payload is None:
        return None
    raw = record.payload.get("_recv_ts") or record.payload.get("recv_ts")
    if raw is None:
        return None
    try:
        return canonical_timestamp(raw).epoch_ns
    except TimestampError:
        return None


def _iter_file_order(raw_files: list[Path], limit_records_per_file: int | None) -> Iterator[RawRecord]:
    for path in raw_files:
        yield from iter_jsonl(path, limit=limit_records_per_file)


def _iter_receive_order(raw_files: list[Path], limit_records_per_file: int | None) -> Iterator[RawRecord]:
    iterators = [iter(iter_jsonl(path, limit=limit_records_per_file)) for path in raw_files]
    heap: list[tuple[tuple[Any, ...], int, RawRecord]] = []

    def key(record: RawRecord, index: int) -> tuple[Any, ...]:
        epoch_ns = _record_recv_epoch_ns(record)
        sequence = record.payload.get("sequence_num") if record.payload else None
        sequence_key = sequence if _valid_sequence_number(sequence) else 2**63 - 1
        if epoch_ns is None:
            return (1, index, record.line_number)
        return (0, epoch_ns, sequence_key, index, record.line_number)

    for index, iterator in enumerate(iterators):
        try:
            record = next(iterator)
        except StopIteration:
            continue
        heapq.heappush(heap, (key(record, index), index, record))
    while heap:
        _, index, record = heapq.heappop(heap)
        yield record
        try:
            next_record = next(iterators[index])
        except StopIteration:
            continue
        heapq.heappush(heap, (key(next_record, index), index, next_record))


def _manifest_for_file(path: Path) -> tuple[Path | None, dict[str, Any] | None, str | None]:
    root = _session_root(path)
    if root is None:
        return None, None, None
    manifest_path = root / "manifest.json"
    if not manifest_path.exists():
        return None, None, None
    digest = _sha256_file(manifest_path)
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return manifest_path, None, digest
    return manifest_path, payload if isinstance(payload, dict) else None, digest


def _collector_attested_paths(
    manifest_path: Path,
    manifest_payload: dict[str, Any],
) -> set[str]:
    attested_paths: set[str] = set()
    closed_files = manifest_payload.get("closed_files")
    if not isinstance(closed_files, list):
        return attested_paths
    session_root = manifest_path.resolve().parent
    for item in closed_files:
        if not isinstance(item, dict):
            continue
        candidate = None
        if isinstance(item.get("relative_path"), str) and item["relative_path"]:
            candidate = str((session_root / item["relative_path"]).resolve())
        elif item.get("absolute_path"):
            candidate = item.get("absolute_path")
        if candidate:
            try:
                attested_paths.add(str(_manifest_path_value(candidate).resolve()))
            except (OSError, TypeError, ValueError):
                continue
    return attested_paths


def _collector_audit_quality_summary(
    collector_audit: dict[str, Any],
    counts: dict[str, Any] | Counter[str],
    manifest_payload: dict[str, Any],
    *,
    performed: bool,
) -> dict[str, Any]:
    raw_parse_error_count = manifest_payload.get("parse_error_count")
    manifest_parse_error_count = (
        raw_parse_error_count if _valid_sequence_number(raw_parse_error_count) else None
    )
    audit_valid = collector_audit.get("valid") is True
    input_records = _count_or_none(counts, "input_records")
    canonical_envelopes = _count_or_none(counts, "canonical_envelopes")
    exact_transport_duplicates = _count_or_none(counts, "exact_transport_duplicate")
    routing_replicas = _count_or_none(counts, "routing_replica")
    malformed_records = _count_or_none(counts, "malformed_or_nonobject_records")
    parse_error_wrappers = _count_or_none(counts, "collector_parse_error_wrappers")
    routed_rows_reconcile = (
        audit_valid
        and input_records is not None
        and collector_audit.get("routed_rows_verified") == input_records
    )
    normalized_received_envelopes = (
        canonical_envelopes + exact_transport_duplicates
        if canonical_envelopes is not None and exact_transport_duplicates is not None
        else None
    )
    received_envelopes_reconcile = (
        audit_valid
        and input_records is not None
        and routing_replicas is not None
        and malformed_records is not None
        and parse_error_wrappers is not None
        and normalized_received_envelopes is not None
        and collector_audit.get("received_envelopes_verified")
        == normalized_received_envelopes
        == input_records - routing_replicas - malformed_records - parse_error_wrappers
    )
    parse_error_count_reconcile = (
        audit_valid
        and parse_error_wrappers is not None
        and manifest_parse_error_count == parse_error_wrappers
    )
    raw_sequence_summary = manifest_payload.get("sequence_summary")
    sequence_summary = raw_sequence_summary if isinstance(raw_sequence_summary, dict) else {}
    collector_sequence_counts = {
        field: (
            sequence_summary.get(field)
            if _valid_sequence_number(sequence_summary.get(field))
            else None
        )
        for field in COLLECTOR_COMPLETE_SEQUENCE_FIELDS
    }
    normalizer_conflicting_count = _count_or_none(counts, "conflicting_sequence_duplicates")
    conflicting_count_reconcile = (
        audit_valid
        and normalizer_conflicting_count is not None
        and collector_sequence_counts["conflicting_sequence_duplicate_count"]
        == normalizer_conflicting_count
    )
    return {
        "performed": performed,
        "valid": audit_valid,
        "manifest_path": collector_audit.get("manifest_path"),
        "closed_files_verified": collector_audit.get("closed_files_verified"),
        "routed_rows_verified": collector_audit.get("routed_rows_verified"),
        "received_envelopes_verified": collector_audit.get("received_envelopes_verified"),
        "errors": collector_audit.get("errors", []),
        "warnings": collector_audit.get("warnings", []),
        "normalizer_routed_rows_reconcile": routed_rows_reconcile,
        "normalizer_received_envelopes_reconcile": received_envelopes_reconcile,
        "manifest_parse_error_count": manifest_parse_error_count,
        "normalizer_parse_error_wrappers": parse_error_wrappers,
        "normalizer_parse_error_count_reconcile": parse_error_count_reconcile,
        **collector_sequence_counts,
        "normalizer_conflicting_sequence_duplicates": normalizer_conflicting_count,
        "normalizer_conflicting_sequence_duplicate_count_reconcile": (
            conflicting_count_reconcile
        ),
    }


def _stable_collector_audit_quality_summary(summary: dict[str, Any]) -> dict[str, Any]:
    """Fields whose equality survives an otherwise valid session relocation."""

    return {
        key: summary.get(key)
        for key in (
            "performed",
            "valid",
            "closed_files_verified",
            "routed_rows_verified",
            "received_envelopes_verified",
            "errors",
            "normalizer_routed_rows_reconcile",
            "normalizer_received_envelopes_reconcile",
            "manifest_parse_error_count",
            "normalizer_parse_error_wrappers",
            "normalizer_parse_error_count_reconcile",
            *COLLECTOR_COMPLETE_SEQUENCE_FIELDS,
            "normalizer_conflicting_sequence_duplicates",
            "normalizer_conflicting_sequence_duplicate_count_reconcile",
        )
    }


def _collector_complete_sequence_failures(summary: dict[str, Any]) -> list[str]:
    failures: list[str] = []
    for field in COLLECTOR_COMPLETE_SEQUENCE_FIELDS:
        value = summary.get(field)
        if not _valid_sequence_number(value):
            failures.append(f"collector_{field}_missing_or_invalid")
        elif value:
            failures.append(COLLECTOR_COMPLETE_SEQUENCE_FAILURES[field])
    return failures


def _normalizer_complete_failures(counts: dict[str, Any] | Counter[str]) -> list[str]:
    failures: list[str] = []
    for field in NORMALIZER_COMPLETE_ZERO_FIELDS:
        value = counts.get(field)
        if not _valid_sequence_number(value):
            failures.append(f"normalizer_{field}_missing_or_invalid")
        elif value:
            failures.append(NORMALIZER_COMPLETE_FAILURES[field])
    return failures


def _execution_source_bundle_identity(bundle: dict[str, Any]) -> dict[str, Any]:
    sources = bundle.get("sources")
    if not isinstance(sources, list):
        raise ValueError("normalizer execution-source bundle sources must be a list")
    identity_sources: list[dict[str, Any]] = []
    for source in sources:
        if not isinstance(source, dict):
            raise ValueError("normalizer execution-source entry must be an object")
        module = source.get("module")
        size = source.get("bytes")
        sha256 = source.get("sha256")
        if (
            not isinstance(module, str)
            or not module
            or isinstance(size, bool)
            or not isinstance(size, int)
            or size < 0
            or not isinstance(sha256, str)
            or not re.fullmatch(r"[0-9a-f]{64}", sha256)
        ):
            raise ValueError("invalid normalizer execution-source entry")
        identity_sources.append({"module": module, "bytes": size, "sha256": sha256})
    return {
        "schema": bundle.get("schema"),
        "sources": identity_sources,
    }


def _execution_source_bundle_digest(bundle: dict[str, Any]) -> str:
    identity = _execution_source_bundle_identity(bundle)
    return hashlib.sha256(
        json.dumps(identity, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _normalizer_execution_source_bundle() -> dict[str, Any]:
    package_root = Path(__file__).resolve().parent
    sources: list[dict[str, Any]] = []
    for module in EXECUTION_SOURCE_MODULES:
        path = package_root / f"{module.rsplit('.', 1)[-1]}.py"
        stat = path.stat()
        sources.append(
            {
                "module": module,
                "path": str(path.resolve()),
                "bytes": stat.st_size,
                "sha256": _sha256_file(path),
            }
        )
    bundle: dict[str, Any] = {
        "schema": EXECUTION_SOURCE_BUNDLE_SCHEMA,
        "sources": sources,
    }
    bundle["bundle_sha256"] = _execution_source_bundle_digest(bundle)
    return bundle


def _input_metadata(raw_files: list[Path]) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    for path in raw_files:
        stat = path.stat()
        manifest_path, _, manifest_sha256 = _manifest_for_file(path)
        manifest_entry: dict[str, Any] | None = None
        if manifest_path is not None:
            manifest_stat = manifest_path.stat()
            manifest_entry = {
                "path": str(manifest_path.resolve()),
                "bytes_before": manifest_stat.st_size,
                "mtime_ns_before": manifest_stat.st_mtime_ns,
                "sha256_before": manifest_sha256,
            }
        entries.append(
            {
                "path": str(path.resolve()),
                "bytes_before": stat.st_size,
                "mtime_ns_before": stat.st_mtime_ns,
                "sha256_before": _sha256_file(path),
                "session_manifest": manifest_entry,
            }
        )
    return entries


def _is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True


def _validate_output_roots(
    raw_files: list[Path],
    derived_root: Path,
    quarantine_root: Path,
    catalog_root: Path,
) -> None:
    outputs = [root.resolve() for root in (derived_root, quarantine_root, catalog_root)]
    for index, left in enumerate(outputs):
        for right in outputs[index + 1 :]:
            if _is_relative_to(left, right) or _is_relative_to(right, left):
                raise ValueError(f"output roots must not overlap: {left} and {right}")
    source_boundaries: set[Path] = set()
    for source in raw_files:
        source = source.resolve()
        source_boundaries.add(_raw_source_boundary(source).resolve())
        for output in outputs:
            if _is_relative_to(source, output):
                raise ValueError(
                    f"output root must not contain or be contained by a raw source location: {output} vs {source}"
                )
    for boundary in source_boundaries:
        for output in outputs:
            if _is_relative_to(boundary, output) or _is_relative_to(output, boundary):
                raise ValueError(
                    f"output root must not overlap a canonical/legacy raw session boundary: {output} vs {boundary}"
                )


def _validate_raw_roots(
    raw_roots: Iterable[Path],
    derived_root: Path,
    quarantine_root: Path,
    catalog_root: Path,
) -> None:
    outputs = [root.resolve() for root in (derived_root, quarantine_root, catalog_root)]
    for raw_root in raw_roots:
        resolved = raw_root.resolve()
        boundary = resolved.parent if resolved.is_file() else resolved
        for output in outputs:
            if _is_relative_to(boundary, output) or _is_relative_to(output, boundary):
                raise ValueError(f"output root overlaps configured raw root: {output} vs {boundary}")


def _dataset_id(
    inputs: list[dict[str, Any]],
    config: dict[str, Any],
    execution_source_bundle: dict[str, Any],
) -> str:
    payload = {
        "schema_version": SCHEMA_VERSION,
        "normalizer_execution_source_bundle": {
            **_execution_source_bundle_identity(execution_source_bundle),
            "bundle_sha256": execution_source_bundle.get("bundle_sha256"),
        },
        "inputs": inputs,
        "config": config,
    }
    digest = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return digest[:24]


def _audit_existing_dataset(
    manifest_path: Path,
    inputs: list[dict[str, Any]],
    *,
    dataset_id: str,
    config: dict[str, Any],
    execution_source_bundle: dict[str, Any],
) -> dict[str, Any] | None:
    if not manifest_path.exists():
        return None
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    normalize_source = next(
        (
            source
            for source in execution_source_bundle.get("sources", [])
            if source.get("module") == "moneyman.normalize"
        ),
        {},
    )
    stored_execution_source_bundle = manifest.get("normalizer_execution_source_bundle")
    try:
        stored_execution_source_identity = _execution_source_bundle_identity(
            stored_execution_source_bundle or {}
        )
    except ValueError:
        return None
    if (
        manifest.get("status") != "completed"
        or manifest.get("schema_version") != SCHEMA_VERSION
        or manifest.get("dataset_id") != dataset_id
        or manifest.get("normalizer_source_sha256") != normalize_source.get("sha256")
        or stored_execution_source_identity
        != _execution_source_bundle_identity(execution_source_bundle)
        or (stored_execution_source_bundle or {}).get("bundle_sha256")
        != execution_source_bundle.get("bundle_sha256")
        or manifest.get("config") != config
        or manifest.get("inputs") != inputs
    ):
        return None
    return manifest if audit_normalization(manifest_path).get("valid") is True else None


def _jsonl_row_count(path: Path) -> int:
    with path.open("rt", encoding="utf-8") as handle:
        return sum(1 for line in handle if line.strip())


def audit_normalization(manifest_path: Path) -> dict[str, Any]:
    """Rehash a completed normalization dataset and every recorded raw input."""

    try:
        manifest_path = Path(manifest_path).resolve()
        if "\x00" in str(manifest_path):
            raise ValueError("embedded null character in path")
    except (OSError, TypeError, ValueError) as exc:
        return {
            "valid": False,
            "manifest_path": str(manifest_path),
            "dataset_id": None,
            "errors": [f"manifest_path_invalid: {exc}"],
            "warnings": [],
        }
    errors: list[str] = []
    warnings: list[str] = []
    if not manifest_path.exists():
        return {
            "valid": False,
            "manifest_path": str(manifest_path),
            "dataset_id": None,
            "errors": ["manifest_missing"],
            "warnings": [],
        }
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return {
            "valid": False,
            "manifest_path": str(manifest_path),
            "dataset_id": None,
            "errors": [f"manifest_unreadable: {exc}"],
            "warnings": [],
        }
    if not isinstance(manifest, dict):
        errors.append("manifest_not_object")
        manifest = {}

    dataset_id = manifest.get("dataset_id")
    config = manifest.get("config")
    inputs = manifest.get("inputs")
    source_sha256 = manifest.get("normalizer_source_sha256")
    execution_source_bundle = manifest.get("normalizer_execution_source_bundle")
    if manifest.get("status") != "completed":
        errors.append("status_not_completed")
    if manifest.get("schema_version") != SCHEMA_VERSION:
        errors.append("schema_version_mismatch")
    if not isinstance(dataset_id, str) or not re.fullmatch(r"[0-9a-f]{24}", dataset_id):
        errors.append("invalid_dataset_id")
    if not isinstance(config, dict):
        errors.append("config_not_object")
    if not isinstance(inputs, list) or not inputs:
        errors.append("inputs_missing_or_empty")
    if not isinstance(source_sha256, str) or not re.fullmatch(r"[0-9a-f]{64}", source_sha256):
        errors.append("invalid_normalizer_source_sha256")
    bundle_is_valid = True
    if not isinstance(execution_source_bundle, dict):
        errors.append("normalizer_execution_source_bundle_missing_or_invalid")
        bundle_is_valid = False
    else:
        if execution_source_bundle.get("schema") != EXECUTION_SOURCE_BUNDLE_SCHEMA:
            errors.append("normalizer_execution_source_bundle_schema_mismatch")
            bundle_is_valid = False
        try:
            calculated_bundle_sha256 = _execution_source_bundle_digest(execution_source_bundle)
        except ValueError as exc:
            errors.append(f"normalizer_execution_source_bundle_invalid: {exc}")
            bundle_is_valid = False
        else:
            if calculated_bundle_sha256 != execution_source_bundle.get("bundle_sha256"):
                errors.append("normalizer_execution_source_bundle_sha256_mismatch")
                bundle_is_valid = False
        stored_modules = [
            source.get("module")
            for source in execution_source_bundle.get("sources", [])
            if isinstance(source, dict)
        ]
        if stored_modules != list(EXECUTION_SOURCE_MODULES):
            errors.append("normalizer_execution_source_module_set_mismatch")
            bundle_is_valid = False
        if bundle_is_valid:
            current_bundle = _normalizer_execution_source_bundle()
            if _execution_source_bundle_identity(
                execution_source_bundle
            ) != _execution_source_bundle_identity(current_bundle):
                errors.append("normalizer_execution_source_current_bundle_mismatch")
            stored_paths = {
                source.get("module"): source.get("path")
                for source in execution_source_bundle["sources"]
            }
            current_paths = {
                source.get("module"): source.get("path") for source in current_bundle["sources"]
            }
            for module in EXECUTION_SOURCE_MODULES:
                if stored_paths.get(module) != current_paths.get(module):
                    warnings.append(f"normalizer_execution_source_path_drift: {module}")
            normalize_source = next(
                (
                    source
                    for source in execution_source_bundle["sources"]
                    if source.get("module") == "moneyman.normalize"
                ),
                {},
            )
            if source_sha256 != normalize_source.get("sha256"):
                errors.append("normalizer_source_sha256_bundle_mismatch")
    if (
        isinstance(dataset_id, str)
        and isinstance(config, dict)
        and isinstance(inputs, list)
        and isinstance(execution_source_bundle, dict)
        and bundle_is_valid
    ):
        if _dataset_id(inputs, config, execution_source_bundle) != dataset_id:
            errors.append("dataset_id_recalculation_mismatch")
        if manifest_path.parent.name != dataset_id:
            errors.append("manifest_parent_dataset_id_mismatch")

    input_files_verified = 0
    session_manifests_verified: set[str] = set()
    collector_reaudit_performed = False
    for index, entry in enumerate(inputs if isinstance(inputs, list) else []):
        if not isinstance(entry, dict):
            errors.append(f"input_not_object: {index}")
            continue
        try:
            path = _manifest_path_value(entry["path"])
        except KeyError:
            errors.append(f"input_path_missing: {index}")
            continue
        except (OSError, TypeError, ValueError) as exc:
            errors.append(f"input_path_invalid: {index}: {exc}")
            continue
        try:
            if not path.exists():
                errors.append(f"input_missing: {path}")
                continue
            stat = path.stat()
            if stat.st_size != entry.get("bytes_before"):
                errors.append(f"input_size_mismatch: {path}")
            if stat.st_mtime_ns != entry.get("mtime_ns_before"):
                errors.append(f"input_mtime_mismatch: {path}")
            if _sha256_file(path) != entry.get("sha256_before"):
                errors.append(f"input_sha256_mismatch: {path}")
        except (OSError, TypeError, ValueError) as exc:
            errors.append(f"input_unreadable: {index}: {exc}")
            continue
        input_files_verified += 1

        manifest_entry = entry.get("session_manifest")
        if manifest_entry is None:
            continue
        if not isinstance(manifest_entry, dict) or not manifest_entry.get("path"):
            errors.append(f"session_manifest_entry_invalid: {index}")
            continue
        try:
            session_manifest_path = _manifest_path_value(manifest_entry["path"])
            session_manifest_text = str(session_manifest_path.resolve())
        except (OSError, TypeError, ValueError) as exc:
            errors.append(f"session_manifest_path_invalid: {index}: {exc}")
            continue
        if session_manifest_text in session_manifests_verified:
            continue
        try:
            if not session_manifest_path.exists():
                errors.append(f"session_manifest_missing: {session_manifest_path}")
                continue
            manifest_stat = session_manifest_path.stat()
            if manifest_stat.st_size != manifest_entry.get("bytes_before"):
                errors.append(f"session_manifest_size_mismatch: {session_manifest_path}")
            if manifest_stat.st_mtime_ns != manifest_entry.get("mtime_ns_before"):
                errors.append(f"session_manifest_mtime_mismatch: {session_manifest_path}")
            if _sha256_file(session_manifest_path) != manifest_entry.get("sha256_before"):
                errors.append(f"session_manifest_sha256_mismatch: {session_manifest_path}")
        except (OSError, TypeError, ValueError) as exc:
            errors.append(f"session_manifest_unreadable: {index}: {exc}")
            continue
        session_manifests_verified.add(session_manifest_text)

    artifacts = manifest.get("artifacts")
    artifact_paths: set[str] = set()
    artifacts_verified = 0
    artifact_rows_by_table: Counter[str] = Counter()
    if not isinstance(artifacts, list) or not artifacts:
        errors.append("artifacts_missing_or_empty")
        artifacts = []
    for index, artifact in enumerate(artifacts):
        if not isinstance(artifact, dict):
            errors.append(f"artifact_not_object: {index}")
            continue
        try:
            path = _manifest_path_value(artifact["path"])
            path_text = str(path.resolve())
        except KeyError:
            errors.append(f"artifact_path_missing: {index}")
            continue
        except (OSError, TypeError, ValueError) as exc:
            errors.append(f"artifact_path_invalid: {index}: {exc}")
            continue
        if path_text in artifact_paths:
            errors.append(f"duplicate_artifact_path: {path}")
            continue
        artifact_paths.add(path_text)
        try:
            if not path.exists():
                errors.append(f"artifact_missing: {path}")
                continue
            artifact_stat = path.stat()
            if artifact_stat.st_size != artifact.get("bytes"):
                errors.append(f"artifact_size_mismatch: {path}")
            if _sha256_file(path) != artifact.get("sha256"):
                errors.append(f"artifact_sha256_mismatch: {path}")
        except (OSError, TypeError, ValueError) as exc:
            errors.append(f"artifact_unreadable: {index}: {exc}")
            continue
        expected_rows = artifact.get("rows")
        if path.name.endswith(".jsonl"):
            if not _valid_sequence_number(expected_rows):
                errors.append(f"artifact_rows_missing_or_invalid: {path}")
            else:
                try:
                    actual_rows = _jsonl_row_count(path)
                except (OSError, UnicodeError, ValueError) as exc:
                    errors.append(f"artifact_rows_unreadable: {path}: {exc}")
                    actual_rows = None
                if actual_rows is not None and actual_rows != expected_rows:
                    errors.append(f"artifact_row_count_mismatch: {path}")
                table = artifact.get("table")
                if isinstance(table, str):
                    artifact_rows_by_table[table] += expected_rows
        elif expected_rows is not None and not _valid_sequence_number(expected_rows):
            errors.append(f"artifact_rows_invalid: {path}")
        artifacts_verified += 1

    quality = manifest.get("quality")
    if not isinstance(quality, dict):
        errors.append("quality_missing_or_not_object")
    else:
        reconciliation = quality.get("reconciliation") or {}
        required_reconciliation = (
            "input_records_error",
            "semantic_items_error",
            "emitted_rows_error",
            "quarantine_rows_error",
            "table_artifact_rows_error",
            "quarantine_artifact_rows_error",
        )
        if not isinstance(reconciliation, dict):
            errors.append("quality_reconciliation_not_object")
        else:
            for field in required_reconciliation:
                if field not in reconciliation:
                    errors.append(f"quality_{field}_missing")
                elif reconciliation.get(field) != 0:
                    errors.append(f"quality_{field}_nonzero")
        source_coverage = quality.get("source_coverage")
        if not isinstance(source_coverage, dict):
            errors.append("quality_source_coverage_not_object")
        else:
            if source_coverage.get("raw_inputs_unchanged") is not True:
                errors.append("quality_raw_inputs_not_unchanged")
            if source_coverage.get("execution_sources_unchanged") is not True:
                errors.append("quality_execution_sources_not_unchanged")
            if isinstance(execution_source_bundle, dict) and source_coverage.get(
                "execution_source_bundle_sha256"
            ) != execution_source_bundle.get("bundle_sha256"):
                errors.append("quality_execution_source_bundle_sha256_mismatch")
        if quality.get("dataset_id") != dataset_id:
            errors.append("quality_dataset_id_mismatch")
        quality_path = manifest.get("quality_path")
        if quality_path is not None:
            try:
                resolved_quality_path = _manifest_path_value(quality_path).resolve()
            except (OSError, TypeError, ValueError) as exc:
                errors.append(f"quality_path_invalid: {exc}")
            else:
                if str(resolved_quality_path) not in artifact_paths:
                    errors.append("quality_path_not_bound_as_artifact")
                try:
                    quality_file_payload = json.loads(
                        resolved_quality_path.read_text(encoding="utf-8")
                    )
                except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
                    errors.append(f"quality_file_unreadable: {exc}")
                else:
                    if quality_file_payload != quality:
                        errors.append("quality_file_manifest_payload_mismatch")

        quality_tables = quality.get("tables")
        quality_table_rows_valid = isinstance(quality_tables, dict)
        if not isinstance(quality_tables, dict):
            errors.append("quality_tables_not_object")
        else:
            for table in TABLE_NAMES:
                table_quality = quality_tables.get(table)
                if not isinstance(table_quality, dict):
                    errors.append(f"quality_table_missing: {table}")
                    quality_table_rows_valid = False
                    continue
                table_rows = table_quality.get("rows")
                if not _valid_sequence_number(table_rows):
                    errors.append(f"quality_table_rows_missing_or_invalid: {table}")
                    quality_table_rows_valid = False
                elif table_rows != artifact_rows_by_table[table]:
                    errors.append(f"quality_table_artifact_rows_mismatch: {table}")
        quarantine_quality = quality.get("quarantine")
        quarantine_rows_valid = False
        if not isinstance(quarantine_quality, dict):
            errors.append("quality_quarantine_not_object")
        else:
            quarantine_rows = quarantine_quality.get("rows")
            if not _valid_sequence_number(quarantine_rows):
                errors.append("quality_quarantine_rows_missing_or_invalid")
            else:
                quarantine_rows_valid = True
                if quarantine_rows != artifact_rows_by_table["_quarantine"]:
                    errors.append("quality_quarantine_artifact_rows_mismatch")

        counts = quality.get("counts")
        if not isinstance(counts, dict):
            errors.append("quality_counts_not_object")
            count_values_valid = False
        else:
            invalid_count_fields = sorted(
                str(field)
                for field, value in counts.items()
                if not _valid_sequence_number(value)
            )
            count_values_valid = not invalid_count_fields
            for field in invalid_count_fields:
                errors.append(f"quality_count_missing_or_invalid: {field}")

        reconciliation_count_fields = (
            "input_records",
            "canonical_envelopes",
            "collector_parse_error_wrappers",
            "routing_replica",
            "exact_transport_duplicate",
            "malformed_or_nonobject_records",
            "semantic_items_seen",
            "semantic_items_emitted",
            "semantic_items_quarantined",
            "semantic_items_recognized_nonemitting",
        )
        reconciliation_counts_valid = isinstance(counts, dict) and all(
            _valid_sequence_number(counts.get(field, 0))
            for field in reconciliation_count_fields
        )
        if (
            isinstance(counts, dict)
            and count_values_valid
            and reconciliation_counts_valid
            and quality_table_rows_valid
            and quarantine_rows_valid
            and isinstance(quality_tables, dict)
            and isinstance(quarantine_quality, dict)
        ):
            recalculated = {
                "input_records_error": counts.get("input_records", 0)
                - counts.get("canonical_envelopes", 0)
                - counts.get("collector_parse_error_wrappers", 0)
                - counts.get("routing_replica", 0)
                - counts.get("exact_transport_duplicate", 0)
                - counts.get("malformed_or_nonobject_records", 0),
                "semantic_items_error": counts.get("semantic_items_seen", 0)
                - counts.get("semantic_items_emitted", 0)
                - counts.get("semantic_items_quarantined", 0)
                - counts.get("semantic_items_recognized_nonemitting", 0),
                "emitted_rows_error": sum(
                    quality_tables[table]["rows"]
                    for table in TABLE_NAMES
                    if table != "sessions"
                )
                - counts.get("semantic_items_emitted", 0),
                "quarantine_rows_error": quarantine_quality.get("rows", 0)
                - counts.get("semantic_items_quarantined", 0),
                "table_artifact_rows_error": sum(
                    quality_tables[table]["rows"] for table in TABLE_NAMES
                )
                - sum(artifact_rows_by_table[table] for table in TABLE_NAMES),
                "quarantine_artifact_rows_error": quarantine_quality.get("rows", 0)
                - artifact_rows_by_table["_quarantine"],
            }
            if isinstance(reconciliation, dict) and reconciliation != recalculated:
                errors.append("quality_reconciliation_recalculation_mismatch")

        duplicate_quality = quality.get("duplicates")
        if not isinstance(duplicate_quality, dict):
            errors.append("quality_duplicates_not_object")
        elif isinstance(counts, dict):
            conflicting_count = counts.get("conflicting_sequence_duplicates")
            quarantined_conflicting_count = duplicate_quality.get(
                "conflicting_sequence_duplicates_quarantined"
            )
            if not _valid_sequence_number(conflicting_count):
                errors.append("quality_conflicting_sequence_duplicates_missing_or_invalid")
            if not _valid_sequence_number(quarantined_conflicting_count):
                errors.append(
                    "quality_conflicting_sequence_duplicates_quarantined_missing_or_invalid"
                )
            if (
                _valid_sequence_number(conflicting_count)
                and _valid_sequence_number(quarantined_conflicting_count)
                and conflicting_count != quarantined_conflicting_count
            ):
                errors.append("quality_conflicting_sequence_duplicate_count_mismatch")

        ordering = quality.get("ordering")
        if not isinstance(ordering, dict):
            errors.append("quality_ordering_not_object")
        else:
            complete_claim = ordering.get("connection_complete_claim")
            if not isinstance(complete_claim, bool):
                errors.append("quality_connection_complete_claim_not_boolean")
            elif complete_claim:
                complete_gate_errors: list[str] = []
                if not isinstance(config, dict):
                    complete_gate_errors.append("config_invalid")
                else:
                    if config.get("input_order") != "receive_time":
                        complete_gate_errors.append("input_order_not_receive_time")
                    if config.get("sequence_scope_requested") != "complete":
                        complete_gate_errors.append("sequence_scope_not_complete")
                    if config.get("limit_records_per_file") is not None or config.get(
                        "max_records"
                    ) is not None:
                        complete_gate_errors.append("bounded_input")
                if ordering.get("input_order") != "receive_time":
                    complete_gate_errors.append("quality_input_order_not_receive_time")
                if ordering.get("sequence_scope_requested") != "complete":
                    complete_gate_errors.append("quality_sequence_scope_not_complete")
                if ordering.get("complete_validation_failures") != []:
                    complete_gate_errors.append("stored_complete_validation_failures_not_empty")
                if ordering.get("collector_manifest_attests_exact_closed_file_set") is not True:
                    complete_gate_errors.append("stored_exact_closed_file_attestation_false")
                if ordering.get("sequence_interpretation") != "connection_global_feed_continuity":
                    complete_gate_errors.append("stored_sequence_interpretation_not_global")
                if quality.get("bounded_run") is not False:
                    complete_gate_errors.append("stored_bounded_run_not_false")

                if not isinstance(counts, dict):
                    complete_gate_errors.append("quality_counts_invalid")
                else:
                    input_record_count = counts.get("input_records")
                    if not _valid_sequence_number(input_record_count):
                        complete_gate_errors.append("input_records_missing_or_invalid")
                    elif input_record_count < 1:
                        complete_gate_errors.append("empty_slice")
                    complete_gate_errors.extend(_normalizer_complete_failures(counts))

                sequence_quality = quality.get("sequence")
                if not isinstance(sequence_quality, dict):
                    complete_gate_errors.append("quality_sequence_invalid")
                elif sequence_quality.get("claim_scope") != "connection_global":
                    complete_gate_errors.append("quality_sequence_scope_not_global")

                if len(session_manifests_verified) != 1:
                    complete_gate_errors.append("not_exactly_one_session_manifest")
                elif isinstance(inputs, list) and isinstance(counts, dict):
                    collector_manifest_path = Path(next(iter(session_manifests_verified)))
                    try:
                        current_collector_manifest = json.loads(
                            collector_manifest_path.read_text(encoding="utf-8")
                        )
                    except (OSError, json.JSONDecodeError) as exc:
                        errors.append(f"complete_collector_manifest_unreadable: {exc}")
                    else:
                        if not isinstance(current_collector_manifest, dict):
                            errors.append("complete_collector_manifest_not_object")
                        else:
                            selected_input_paths: set[str] = set()
                            for entry in inputs:
                                if not isinstance(entry, dict) or "path" not in entry:
                                    continue
                                try:
                                    selected_input_paths.add(
                                        str(_manifest_path_value(entry["path"]).resolve())
                                    )
                                except (OSError, TypeError, ValueError):
                                    complete_gate_errors.append("current_input_path_invalid")
                            attested_paths = _collector_attested_paths(
                                collector_manifest_path, current_collector_manifest
                            )
                            exact_file_set = selected_input_paths == attested_paths
                            if not exact_file_set:
                                complete_gate_errors.append("current_exact_closed_file_set_mismatch")
                            collector_reaudit_performed = True
                            try:
                                current_collector_audit = audit_collector_session(
                                    collector_manifest_path
                                )
                            except Exception as exc:
                                current_collector_audit = {
                                    "valid": False,
                                    "manifest_path": str(collector_manifest_path.resolve()),
                                    "errors": [
                                        f"collector_session_audit_error: {type(exc).__name__}: {exc}"
                                    ],
                                    "warnings": [],
                                }
                            expected_collector_summary = _collector_audit_quality_summary(
                                current_collector_audit,
                                counts,
                                current_collector_manifest,
                                performed=True,
                            )
                            stored_collector_summary = ordering.get("collector_session_audit")
                            if not isinstance(stored_collector_summary, dict) or (
                                _stable_collector_audit_quality_summary(stored_collector_summary)
                                != _stable_collector_audit_quality_summary(
                                    expected_collector_summary
                                )
                            ):
                                errors.append("complete_collector_audit_summary_mismatch")
                            else:
                                if stored_collector_summary.get("manifest_path") != (
                                    expected_collector_summary.get("manifest_path")
                                ):
                                    warnings.append("complete_collector_manifest_path_drift")
                                for warning in expected_collector_summary.get("warnings", []):
                                    warnings.append(f"complete_collector_reaudit_warning: {warning}")
                            if current_collector_audit.get("valid") is not True:
                                complete_gate_errors.append("current_collector_audit_not_valid")
                            if expected_collector_summary[
                                "normalizer_routed_rows_reconcile"
                            ] is not True:
                                complete_gate_errors.append("current_collector_routed_rows_mismatch")
                            if expected_collector_summary[
                                "normalizer_received_envelopes_reconcile"
                            ] is not True:
                                complete_gate_errors.append(
                                    "current_collector_received_envelopes_mismatch"
                                )
                            if expected_collector_summary[
                                "normalizer_parse_error_count_reconcile"
                            ] is not True:
                                complete_gate_errors.append("current_collector_parse_error_mismatch")
                            if expected_collector_summary["manifest_parse_error_count"] != 0:
                                complete_gate_errors.append("current_collector_parse_errors_present")
                            complete_gate_errors.extend(
                                _collector_complete_sequence_failures(
                                    expected_collector_summary
                                )
                            )
                            if expected_collector_summary[
                                "normalizer_conflicting_sequence_duplicate_count_reconcile"
                            ] is not True:
                                complete_gate_errors.append(
                                    "current_collector_conflicting_sequence_duplicate_count_mismatch"
                                )

                for gate_error in complete_gate_errors:
                    errors.append(f"complete_claim_ineligible: {gate_error}")

    if manifest.get("normalizer_source_path") is None:
        warnings.append("normalizer_source_path_not_recorded")

    return {
        "valid": not errors,
        "manifest_path": str(manifest_path),
        "dataset_id": dataset_id,
        "schema_version": manifest.get("schema_version"),
        "execution_source_bundle_sha256": (
            execution_source_bundle.get("bundle_sha256")
            if isinstance(execution_source_bundle, dict)
            else None
        ),
        "input_files_verified": input_files_verified,
        "session_manifests_verified": len(session_manifests_verified),
        "artifacts_verified": artifacts_verified,
        "collector_reaudit_performed": collector_reaudit_performed,
        "errors": errors,
        "warnings": warnings,
    }


def _session_key(path: Path, session_id: str | None) -> str:
    root = _session_root(path)
    seed = str(root.resolve()) if root else f"{path.parent.resolve()}|{session_id or 'unknown'}"
    return hashlib.sha256(seed.encode("utf-8")).hexdigest()[:24]


def _json_list(value: Any) -> str:
    if isinstance(value, (list, tuple, set)):
        return json.dumps(sorted(str(item) for item in value), separators=(",", ":"))
    return "[]"


def _optional_manifest_ts(value: Any) -> CanonicalTimestamp | None:
    if value is None:
        return None
    try:
        return canonical_timestamp(value)
    except TimestampError:
        return None


def normalize_files(
    raw_files: list[Path],
    derived_root: Path,
    quarantine_root: Path,
    catalog_root: Path,
    *,
    input_order: str = "file",
    sequence_scope: str = "observed",
    limit_records_per_file: int | None = None,
    max_records: int | None = None,
    max_open_files: int = 32,
) -> dict[str, Any]:
    if not raw_files:
        raise ValueError("at least one raw JSONL/JSONL.GZ input file is required")
    if input_order not in {"file", "receive_time"}:
        raise ValueError("input_order must be file or receive_time")
    if sequence_scope not in {"observed", "complete"}:
        raise ValueError("sequence_scope must be observed or complete")
    if sequence_scope == "complete" and input_order != "receive_time":
        raise ValueError("complete sequence scope requires receive_time input order")
    if limit_records_per_file is not None and limit_records_per_file < 1:
        raise ValueError("limit_records_per_file must be positive")
    if max_records is not None and max_records < 1:
        raise ValueError("max_records must be positive")
    if max_open_files < 1:
        raise ValueError("max_open_files must be positive")

    raw_files = sorted({path.resolve() for path in raw_files}, key=str)
    _validate_output_roots(raw_files, derived_root, quarantine_root, catalog_root)
    execution_source_bundle = _normalizer_execution_source_bundle()
    normalize_source = next(
        source
        for source in execution_source_bundle["sources"]
        if source["module"] == "moneyman.normalize"
    )
    source_sha256 = normalize_source["sha256"]
    inputs = _input_metadata(raw_files)
    config = {
        "input_order": input_order,
        "sequence_scope_requested": sequence_scope,
        "limit_records_per_file": limit_records_per_file,
        "max_records": max_records,
        "max_open_files": max_open_files,
    }
    dataset_id = _dataset_id(inputs, config, execution_source_bundle)
    metadata_dir = derived_root / DATASET_VERSION / "normalization_datasets" / dataset_id
    manifest_path = metadata_dir / "manifest.json"
    existing = _audit_existing_dataset(
        manifest_path,
        inputs,
        dataset_id=dataset_id,
        config=config,
        execution_source_bundle=execution_source_bundle,
    )
    if existing is not None:
        return {
            "status": "reused",
            "dataset_id": dataset_id,
            "run_id": dataset_id,
            "manifest_path": str(manifest_path.resolve()),
            "quality_path": existing.get("quality_path"),
            "quality": existing.get("quality"),
            "artifacts": existing.get("artifacts", []),
        }

    if metadata_dir.exists():
        raise RuntimeError(
            "incomplete or corrupted normalization dataset already exists; inspect before retrying: "
            f"{metadata_dir}"
        )

    writer = PartitionWriter(derived_root, dataset_id, max_open_files=max_open_files)
    quarantine_writer = PartitionWriter(quarantine_root, dataset_id, max_open_files=max_open_files)
    state_path = metadata_dir / "dedup_state.sqlite3.tmp"
    metadata_dir.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(state_path)
    connection.execute(
        "CREATE TABLE seen_envelopes (signature TEXT PRIMARY KEY, route TEXT, path TEXT, line INTEGER)"
    )
    connection.execute(
        "CREATE TABLE seen_sequences (stream TEXT, epoch INTEGER, sequence_num INTEGER, fingerprint TEXT, PRIMARY KEY(stream, epoch, sequence_num))"
    )

    counts: Counter[str] = Counter()
    for field in NORMALIZER_COMPLETE_ZERO_FIELDS:
        counts[field] = 0
    table_rows: Counter[str] = Counter()
    table_nulls: dict[str, Counter[str]] = defaultdict(Counter)
    channels: Counter[str] = Counter()
    products: Counter[str] = Counter()
    quarantine_reasons: Counter[str] = Counter()
    duplicate_samples: list[dict[str, Any]] = []
    source_file_counts: Counter[str] = Counter()
    session_stats: dict[str, Counter[str]] = defaultdict(Counter)
    session_paths: dict[str, list[Path]] = defaultdict(list)
    last_sequence: dict[str, int] = {}
    connection_epoch: Counter[str] = Counter()
    per_file_last_recv_ns: dict[str, int] = {}
    latency = LatencyDistribution()
    coverage = TimeCoverage()

    iterator: Iterable[RawRecord]
    if input_order == "receive_time":
        iterator = _iter_receive_order(raw_files, limit_records_per_file)
    else:
        iterator = _iter_file_order(raw_files, limit_records_per_file)

    try:
        for record in iterator:
            if max_records is not None and counts["input_records"] >= max_records:
                counts["stopped_at_max_records"] = 1
                break
            counts["input_records"] += 1
            source_text = str(record.source_path.resolve())
            source_file_counts[source_text] += 1
            session_id = session_id_from_path(record.source_path) or "unknown"
            session_key = _session_key(record.source_path, session_id)
            if record.source_path not in session_paths[session_key]:
                session_paths[session_key].append(record.source_path)
            session_stats[session_key]["selected_input_records"] += 1

            if record.error or record.payload is None:
                tables, quarantine, accounting = normalize_envelope(record, dataset_id=dataset_id)
                counts["malformed_or_nonobject_records"] += 1
            else:
                payload = record.payload
                fingerprint = _payload_sha256(payload)
                exchange_fingerprint = _exchange_payload_sha256(payload)
                channel = message_channel(payload)
                route = _source_route(record.source_path)
                recv_raw = payload.get("_recv_ts") or payload.get("recv_ts")
                if recv_raw is None:
                    counts["per_file_missing_recv_ts"] += 1
                else:
                    try:
                        recv_epoch_ns = canonical_timestamp(recv_raw).epoch_ns
                    except TimestampError:
                        counts["per_file_invalid_recv_ts"] += 1
                    else:
                        previous_recv_ns = per_file_last_recv_ns.get(source_text)
                        if previous_recv_ns is not None and recv_epoch_ns < previous_recv_ns:
                            counts["per_file_recv_ts_regressions"] += 1
                        per_file_last_recv_ns[source_text] = recv_epoch_ns

                embedded_epoch = payload.get("_connection_epoch")
                frame_ordinal = payload.get("_received_frame_ordinal")
                routing_frame_identity = (
                    f"frame:{frame_ordinal}"
                    if _valid_sequence_number(frame_ordinal)
                    else f"sequence:{payload.get('sequence_num')}|recv:{recv_raw}"
                )
                signature = hashlib.sha256(
                    f"{session_key}|epoch:{embedded_epoch}|{routing_frame_identity}|{fingerprint}".encode("utf-8")
                ).hexdigest()
                seen = connection.execute(
                    "SELECT route, path, line FROM seen_envelopes WHERE signature = ?", (signature,)
                ).fetchone()
                if seen is not None:
                    classification = "routing_replica" if seen[0] != route else "exact_transport_duplicate"
                    counts[classification] += 1
                    session_stats[session_key][classification] += 1
                    if len(duplicate_samples) < 25:
                        duplicate_samples.append(
                            {
                                "classification": classification,
                                "sequence_num": payload.get("sequence_num"),
                                "first_source": {"path": seen[1], "line": seen[2], "route": seen[0]},
                                "duplicate_source": {"path": source_text, "line": record.line_number, "route": route},
                                "envelope_sha256": fingerprint,
                            }
                        )
                    continue
                connection.execute(
                    "INSERT INTO seen_envelopes(signature, route, path, line) VALUES (?, ?, ?, ?)",
                    (signature, route, source_text, record.line_number),
                )

                if payload.get("_parsed_envelope") is False:
                    counts["collector_parse_error_wrappers"] += 1
                    session_stats[session_key]["selected_collector_parse_error_count"] += 1
                    tables, quarantine, accounting = normalize_envelope(record, dataset_id=dataset_id)
                    channels["malformed_json"] += 1
                else:
                    sequence = payload.get("sequence_num")
                    stream = session_key if input_order == "receive_time" else source_text
                    embedded_epoch_is_valid = _valid_sequence_number(embedded_epoch)
                    epoch = embedded_epoch if embedded_epoch_is_valid else connection_epoch[stream]
                    sequence_stream = f"{stream}|epoch={epoch}"
                    previous = last_sequence.get(sequence_stream)
                    sequence_error = _sequence_number_error(payload)
                    if sequence_error is not None:
                        counts["malformed_sequence_envelopes"] += 1
                        session_stats[session_key]["selected_malformed_sequence_count"] += 1
                        tables, quarantine, accounting = normalize_envelope(record, dataset_id=dataset_id)
                    elif _valid_sequence_number(sequence):
                        if previous is not None and sequence < previous:
                            counts["sequence_regressions"] += 1
                            session_stats[session_key]["selected_sequence_regression_count"] += 1
                            if not embedded_epoch_is_valid:
                                connection_epoch[stream] += 1
                                epoch = connection_epoch[stream]
                                sequence_stream = f"{stream}|epoch={epoch}"
                                previous = last_sequence.get(sequence_stream)
                                counts["inferred_reconnect_boundaries"] += 1
                        prior = connection.execute(
                            "SELECT fingerprint FROM seen_sequences WHERE stream = ? AND epoch = ? AND sequence_num = ?",
                            (stream, epoch, sequence),
                        ).fetchone()
                        if prior is not None and prior[0] == exchange_fingerprint:
                            counts["exact_transport_duplicate"] += 1
                            session_stats[session_key]["exact_transport_duplicate"] += 1
                            if len(duplicate_samples) < 25:
                                duplicate_samples.append(
                                    {
                                        "classification": "exact_transport_duplicate",
                                        "sequence_num": sequence,
                                        "duplicate_source": {
                                            "path": source_text,
                                            "line": record.line_number,
                                            "route": route,
                                        },
                                        "exchange_payload_sha256": exchange_fingerprint,
                                    }
                                )
                            continue
                        if prior is not None and prior[0] != exchange_fingerprint:
                            counts["conflicting_sequence_duplicates"] += 1
                            session_stats[session_key]["conflicting_sequence_duplicate"] += 1
                            quarantine = [
                                _quarantine(record, "conflicting_sequence_duplicate", payload, item=None)
                            ]
                            tables = {name: [] for name in TABLE_NAMES if name != "sessions"}
                            accounting = Counter(
                                {"semantic_items_seen": 1, "semantic_items_quarantined": 1}
                            )
                            accounting = _finish_normalization(tables, quarantine, accounting)[2]
                        else:
                            if previous is not None:
                                if sequence == previous:
                                    counts["same_sequence_adjacent"] += 1
                                elif sequence > previous + 1:
                                    gap = sequence - previous - 1
                                    counts["observed_missing_sequence_numbers"] += gap
                                    counts["sequence_gap_events"] += 1
                                    session_stats[session_key]["selected_sequence_gap_count"] += 1
                            last_sequence[sequence_stream] = sequence
                            connection.execute(
                                "INSERT OR IGNORE INTO seen_sequences(stream, epoch, sequence_num, fingerprint) VALUES (?, ?, ?, ?)",
                                (stream, epoch, sequence, exchange_fingerprint),
                            )
                            tables, quarantine, accounting = normalize_envelope(
                                record, dataset_id=dataset_id
                            )
                    else:
                        tables, quarantine, accounting = normalize_envelope(record, dataset_id=dataset_id)
                        counts["unsequenced_envelopes"] += 1

                    counts["canonical_envelopes"] += 1
                    session_stats[session_key]["selected_canonical_envelopes"] += 1
                    channels[channel] += 1

            counts.update(accounting)
            emitted_this_envelope = 0
            for table, rows in tables.items():
                for row in rows:
                    writer.write(table, row)
                    emitted_this_envelope += 1
                    table_rows[table] += 1
                    product = row.get("product_id")
                    if product:
                        products[str(product)] += 1
                    coverage.add("tables", table, row)
                    coverage.add("products", str(product or "__none__"), row)
                    coverage.add("channels", str(row.get("channel") or "__none__"), row)
                    for field in TABLE_SCHEMAS[table]:
                        if row.get(field) is None:
                            table_nulls[table][field] += 1
                    event_ns = row.get("event_ts_epoch_ns") or row.get("start_ts_epoch_ns")
                    recv_ns = row.get("recv_ts_epoch_ns")
                    if isinstance(event_ns, int) and isinstance(recv_ns, int):
                        latency.add(recv_ns - event_ns)
            if emitted_this_envelope:
                counts["envelopes_with_rows"] += 1
            else:
                counts["envelopes_without_rows"] += 1

            for row in quarantine:
                reason = str(row["reason"])
                quarantine_reasons[reason] += 1
                quarantine_id = hashlib.sha256(
                    (
                        f"{row.get('source_path')}|{row.get('source_line')}|{reason}|"
                        f"{counts['quarantine_rows']}"
                    ).encode("utf-8")
                ).hexdigest()
                review_row = {
                    "schema_version": SCHEMA_VERSION,
                    "dataset_id": dataset_id,
                    "quarantine_id": quarantine_id,
                    "reason": reason,
                    "recv_ts_raw": row.get("recv_ts_raw"),
                    "channel": row.get("channel"),
                    "source_path": row.get("source_path"),
                    "source_line": row.get("source_line"),
                    "session_id": row.get("session_id"),
                    "envelope_sha256": row.get("envelope_sha256"),
                    "raw_line": row.get("raw_line"),
                    "item_json": json.dumps(
                        row.get("item"), sort_keys=True, separators=(",", ":"), default=str
                    ),
                }
                quarantine_writer.write_quarantine(review_row)
                counts["quarantine_rows"] += 1
            if counts["input_records"] % 10_000 == 0:
                connection.commit()
        connection.commit()

        # Emit exactly one session row per canonical session/legacy provenance root.
        for session_key, paths in sorted(session_paths.items()):
            first_path = sorted(paths, key=str)[0]
            session_id = session_id_from_path(first_path)
            manifest_file, manifest, manifest_sha = _manifest_for_file(first_path)
            manifest = manifest or {}
            collector_provenance = manifest.get("collector_provenance") or {}
            host_provenance = manifest.get("host_provenance")
            sequence_summary = manifest.get("sequence_summary") or {}
            start = _optional_manifest_ts(manifest.get("start_ts"))
            end = _optional_manifest_ts(manifest.get("end_ts"))
            stats = session_stats[session_key]
            session_row = {
                "schema_version": SCHEMA_VERSION,
                "dataset_id": dataset_id,
                "session_key": session_key,
                "session_id": session_id,
                "manifest_provenance": "manifest" if manifest_file and manifest else "inferred_legacy_or_missing_manifest",
                "manifest_path": str(manifest_file.resolve()) if manifest_file else None,
                "manifest_sha256": manifest_sha,
                "collector": manifest.get("collector") or collector_provenance.get("name"),
                "collector_version": manifest.get("collector_version") or collector_provenance.get("version"),
                "git_commit": manifest.get("git_commit") or collector_provenance.get("git_commit"),
                "host": (
                    json.dumps(host_provenance, sort_keys=True, separators=(",", ":"))
                    if isinstance(host_provenance, dict)
                    else manifest.get("host")
                ),
                "products_json": _json_list(manifest.get("products")),
                "channels_json": _json_list(manifest.get("channels")),
                "start_ts": start.text if start else None,
                "start_ts_epoch_ns": start.epoch_ns if start else None,
                "start_ts_raw": start.raw if start else manifest.get("start_ts"),
                "end_ts": end.text if end else None,
                "end_ts_epoch_ns": end.epoch_ns if end else None,
                "end_ts_raw": end.raw if end else manifest.get("end_ts"),
                "raw_root": manifest.get("raw_root"),
                "shutdown_reason": manifest.get("shutdown_reason"),
                "manifest_message_count": manifest.get("message_count"),
                "manifest_gap_count": manifest.get("gap_count", sequence_summary.get("sequence_gap_count")),
                "manifest_duplicate_count": manifest.get(
                    "duplicate_count", sequence_summary.get("sequence_duplicate_count")
                ),
                "selected_raw_file_count": len(paths),
                "selected_input_records": stats["selected_input_records"],
                "selected_canonical_envelopes": stats["selected_canonical_envelopes"],
                "selected_sequence_gap_count": stats["selected_sequence_gap_count"],
                "selected_sequence_regression_count": stats["selected_sequence_regression_count"],
                "selected_malformed_sequence_count": stats["selected_malformed_sequence_count"],
                "selected_collector_parse_error_count": stats[
                    "selected_collector_parse_error_count"
                ],
                "selected_exact_duplicate_count": stats["exact_transport_duplicate"],
                "selected_conflicting_duplicate_count": stats["conflicting_sequence_duplicate"],
                "selected_routing_replica_count": stats["routing_replica"],
            }
            writer.write("sessions", session_row)
            coverage.add("tables", "sessions", session_row)
            coverage.add("products", "__none__", session_row)
            coverage.add("channels", "sessions", session_row)
            table_rows["sessions"] += 1
            for field in TABLE_SCHEMAS["sessions"]:
                if session_row.get(field) is None:
                    table_nulls["sessions"][field] += 1

        artifacts = writer.artifacts()
        quarantine_artifacts = quarantine_writer.artifacts()

        inputs_after: list[dict[str, Any]] = []
        raw_unchanged = True
        for before in inputs:
            path = Path(before["path"])
            path_stat = path.stat()
            after = {
                **before,
                "bytes_after": path_stat.st_size,
                "mtime_ns_after": path_stat.st_mtime_ns,
                "sha256_after": _sha256_file(path),
            }
            after["unchanged"] = (
                after["bytes_before"] == after["bytes_after"]
                and after["mtime_ns_before"] == after["mtime_ns_after"]
                and after["sha256_before"] == after["sha256_after"]
            )
            manifest_before = before.get("session_manifest")
            if manifest_before:
                manifest_path_after = Path(manifest_before["path"])
                manifest_stat_after = manifest_path_after.stat()
                manifest_after = {
                    **manifest_before,
                    "bytes_after": manifest_stat_after.st_size,
                    "mtime_ns_after": manifest_stat_after.st_mtime_ns,
                    "sha256_after": _sha256_file(manifest_path_after),
                }
                manifest_after["unchanged"] = (
                    manifest_after["bytes_before"] == manifest_after["bytes_after"]
                    and manifest_after["mtime_ns_before"] == manifest_after["mtime_ns_after"]
                    and manifest_after["sha256_before"] == manifest_after["sha256_after"]
                )
                after["session_manifest"] = manifest_after
                after["unchanged"] = after["unchanged"] and manifest_after["unchanged"]
            raw_unchanged = raw_unchanged and bool(after["unchanged"])
            inputs_after.append(after)

        if not raw_unchanged:
            raise RuntimeError("raw input or sibling session manifest changed during normalization")
        execution_source_bundle_after = _normalizer_execution_source_bundle()
        if _execution_source_bundle_identity(
            execution_source_bundle_after
        ) != _execution_source_bundle_identity(execution_source_bundle):
            raise RuntimeError("normalizer execution source changed during normalization")

        semantic_reconciliation_error = (
            counts["semantic_items_seen"]
            - counts["semantic_items_emitted"]
            - counts["semantic_items_quarantined"]
            - counts["semantic_items_recognized_nonemitting"]
        )
        input_reconciliation_error = (
            counts["input_records"]
            - counts["canonical_envelopes"]
            - counts["collector_parse_error_wrappers"]
            - counts["routing_replica"]
            - counts["exact_transport_duplicate"]
            - counts["malformed_or_nonobject_records"]
        )
        emitted_data_rows = sum(
            table_rows[table] for table in TABLE_NAMES if table != "sessions"
        )
        artifact_table_rows = sum(
            int(artifact["rows"])
            for artifact in artifacts
            if artifact.get("table") in TABLE_NAMES and isinstance(artifact.get("rows"), int)
        )
        artifact_quarantine_rows = sum(
            int(artifact["rows"])
            for artifact in quarantine_artifacts
            if artifact.get("table") == "_quarantine" and isinstance(artifact.get("rows"), int)
        )
        emitted_rows_error = emitted_data_rows - counts["semantic_items_emitted"]
        quarantine_rows_error = counts["quarantine_rows"] - counts["semantic_items_quarantined"]
        table_artifact_rows_error = sum(table_rows.values()) - artifact_table_rows
        quarantine_artifact_rows_error = counts["quarantine_rows"] - artifact_quarantine_rows
        reconciliation_errors = {
            "input_records_error": input_reconciliation_error,
            "semantic_items_error": semantic_reconciliation_error,
            "emitted_rows_error": emitted_rows_error,
            "quarantine_rows_error": quarantine_rows_error,
            "table_artifact_rows_error": table_artifact_rows_error,
            "quarantine_artifact_rows_error": quarantine_artifact_rows_error,
        }
        if any(value != 0 for value in reconciliation_errors.values()):
            raise RuntimeError(
                "normalization reconciliation failed closed: "
                + json.dumps(reconciliation_errors, sort_keys=True)
            )
        duplicate_denominator = max(
            counts["input_records"] - counts["malformed_or_nonobject_records"], 0
        )
        complete_validation_failures: list[str] = []
        if sequence_scope != "complete":
            complete_validation_failures.append("sequence_scope_not_requested_complete")
        if input_order != "receive_time":
            complete_validation_failures.append("input_order_not_receive_time")
        if limit_records_per_file is not None or max_records is not None:
            complete_validation_failures.append("bounded_input")
        if counts["input_records"] == 0:
            complete_validation_failures.append("empty_slice")
        if len(session_paths) != 1:
            complete_validation_failures.append("not_exactly_one_session")
        complete_validation_failures.extend(_normalizer_complete_failures(counts))

        manifest_attests_complete_files = False
        collector_audit_performed = False
        collector_session_audit: dict[str, Any] = {
            "valid": False,
            "errors": ["collector_session_audit_not_run"],
            "warnings": [],
        }
        collector_row_count_reconciles = False
        collector_envelope_count_reconciles = False
        collector_parse_error_count_reconciles = False
        collector_manifest_parse_error_count: int | None = None
        if len(session_paths) == 1:
            only_paths = next(iter(session_paths.values()))
            manifest_file, manifest_payload, _ = _manifest_for_file(only_paths[0])
            manifest_payload = manifest_payload or {}
            raw_manifest_parse_error_count = manifest_payload.get("parse_error_count")
            if _valid_sequence_number(raw_manifest_parse_error_count):
                collector_manifest_parse_error_count = raw_manifest_parse_error_count
            closed_files = manifest_payload.get("closed_files") or []
            session_root = _session_root(only_paths[0])
            if manifest_file and session_root and closed_files and manifest_payload.get("status") == "closed":
                attested_paths = _collector_attested_paths(manifest_file, manifest_payload)
                selected_paths = {str(path.resolve()) for path in raw_files}
                manifest_attests_complete_files = selected_paths == attested_paths
            collector_audit_candidate = (
                sequence_scope == "complete"
                and input_order == "receive_time"
                and limit_records_per_file is None
                and max_records is None
                and manifest_file is not None
            )
            if collector_audit_candidate:
                collector_audit_performed = True
                try:
                    collector_session_audit = audit_collector_session(manifest_file)
                except Exception as exc:  # Eligibility must fail closed on any malformed audit input.
                    collector_session_audit = {
                        "valid": False,
                        "manifest_path": str(manifest_file.resolve()),
                        "errors": [f"collector_session_audit_error: {type(exc).__name__}: {exc}"],
                        "warnings": [],
                    }
        collector_quality_summary = _collector_audit_quality_summary(
            collector_session_audit,
            counts,
            manifest_payload if len(session_paths) == 1 else {},
            performed=collector_audit_performed,
        )
        collector_row_count_reconciles = collector_quality_summary[
            "normalizer_routed_rows_reconcile"
        ]
        collector_envelope_count_reconciles = collector_quality_summary[
            "normalizer_received_envelopes_reconcile"
        ]
        collector_parse_error_count_reconciles = collector_quality_summary[
            "normalizer_parse_error_count_reconcile"
        ]
        collector_manifest_parse_error_count = collector_quality_summary[
            "manifest_parse_error_count"
        ]
        if not manifest_attests_complete_files:
            complete_validation_failures.append("collector_manifest_does_not_attest_exact_closed_file_set")
        if collector_session_audit.get("valid") is not True:
            complete_validation_failures.append("collector_session_audit_not_valid")
        if not collector_row_count_reconciles:
            complete_validation_failures.append("collector_routed_row_count_mismatch")
        if not collector_envelope_count_reconciles:
            complete_validation_failures.append("collector_received_envelope_count_mismatch")
        if not collector_parse_error_count_reconciles:
            complete_validation_failures.append("collector_parse_error_count_mismatch")
        if (
            collector_manifest_parse_error_count is not None
            and collector_manifest_parse_error_count > 0
            and "collector_parse_errors_present" not in complete_validation_failures
        ):
            complete_validation_failures.append("collector_parse_errors_present")
        complete_validation_failures.extend(
            _collector_complete_sequence_failures(collector_quality_summary)
        )
        if collector_quality_summary[
            "normalizer_conflicting_sequence_duplicate_count_reconcile"
        ] is not True:
            complete_validation_failures.append(
                "collector_conflicting_sequence_duplicate_count_mismatch"
            )
        effective_complete = not complete_validation_failures

        quality = {
            "schema_version": SCHEMA_VERSION,
            "dataset_id": dataset_id,
            "bounded_run": limit_records_per_file is not None or max_records is not None,
            "limits": {
                "limit_records_per_file": limit_records_per_file,
                "max_records": max_records,
                "max_records_reached": bool(counts["stopped_at_max_records"]),
            },
            "ordering": {
                "input_order": input_order,
                "sequence_scope_requested": sequence_scope,
                "connection_complete_claim": effective_complete,
                "complete_validation_failures": complete_validation_failures,
                "collector_manifest_attests_exact_closed_file_set": manifest_attests_complete_files,
                "collector_session_audit": collector_quality_summary,
                "sequence_interpretation": (
                    "connection_global_feed_continuity"
                    if effective_complete
                    else "observed_only_not_connection_complete"
                ),
            },
            "counts": dict(sorted(counts.items())),
            "tables": {
                table: {
                    "rows": table_rows[table],
                    "null_counts": dict(sorted(table_nulls[table].items())),
                    "null_rates": {
                        field: (
                            table_nulls[table][field] / table_rows[table]
                            if table_rows[table]
                            else None
                        )
                        for field in TABLE_SCHEMAS[table]
                    },
                    "schema_fields": list(TABLE_SCHEMAS[table]),
                }
                for table in TABLE_NAMES
            },
            "channels": dict(sorted(channels.items())),
            "products": dict(sorted(products.items())),
            "time_coverage": coverage.report(),
            "source_coverage": {
                "selected_files": len(raw_files),
                "records_by_source": dict(sorted(source_file_counts.items())),
                "inputs": inputs_after,
                "raw_inputs_unchanged": raw_unchanged,
                "execution_sources_unchanged": True,
                "execution_source_bundle_sha256": execution_source_bundle[
                    "bundle_sha256"
                ],
            },
            "duplicates": {
                "routing_replicas_collapsed": counts["routing_replica"],
                "exact_transport_duplicates_collapsed": counts["exact_transport_duplicate"],
                "conflicting_sequence_duplicates_quarantined": counts["conflicting_sequence_duplicates"],
                "denominator_valid_input_envelopes": duplicate_denominator,
                "routing_replica_rate": (
                    counts["routing_replica"] / duplicate_denominator
                    if duplicate_denominator
                    else None
                ),
                "exact_transport_duplicate_rate": (
                    counts["exact_transport_duplicate"] / duplicate_denominator
                    if duplicate_denominator
                    else None
                ),
                "conflicting_sequence_duplicate_rate": (
                    counts["conflicting_sequence_duplicates"] / duplicate_denominator
                    if duplicate_denominator
                    else None
                ),
                "samples": duplicate_samples,
                "policy": (
                    "Exact cross-shard routing replicas and exact same-route transport duplicates emit one canonical "
                    "semantic envelope and remain counted here. Conflicting same-epoch sequence payloads are quarantined."
                ),
            },
            "sequence": {
                "gap_events": counts["sequence_gap_events"],
                "observed_missing_sequence_numbers": counts["observed_missing_sequence_numbers"],
                "regressions": counts["sequence_regressions"],
                "inferred_reconnect_boundaries": counts["inferred_reconnect_boundaries"],
                "unsequenced_envelopes": counts["unsequenced_envelopes"],
                "malformed_sequence_envelopes": counts["malformed_sequence_envelopes"],
                "collector_parse_error_wrappers": counts["collector_parse_error_wrappers"],
                "claim_scope": "connection_global" if effective_complete else "observed_only",
            },
            "latency": latency.report(),
            "quarantine": {
                "rows": counts["quarantine_rows"],
                "reasons": dict(sorted(quarantine_reasons.items())),
            },
            "recognized_nonemitting": {
                "ticker_without_bbo": counts["ticker_without_bbo"],
                "ignored_channels": {
                    key.split(":", 1)[1]: value
                    for key, value in sorted(counts.items())
                    if key.startswith("ignored_channel:")
                },
            },
            "reconciliation": reconciliation_errors,
            "limitations": [
                "Observed-only or bounded sequence gaps are not proof of Coinbase feed loss.",
                "Collector parse-error wrappers are preserved in quarantine and can never support a complete-connection claim.",
                "Ticker and ticker_batch items without both BBO sides are counted but do not emit quote rows.",
                "Numeric price, size, quantity, and OHLC text is preserved with shape/required-field checks; normalization.v2 does not yet certify finiteness, sign, OHLC relationships, or BBO sanity.",
                "Session rows are manifest-backed when available and provenance-only when inferred from legacy paths.",
                "JSONL is retained for this schema slice because the bundled runtime has no pyarrow; each part is partitioned, hashed, immutable, and rebuildable.",
            ],
        }

        schema_path = metadata_dir / "schema.json"
        quality_path = metadata_dir / "quality.json"
        schema_payload = {
            "schema_version": SCHEMA_VERSION,
            "tables": {table: list(fields) for table, fields in TABLE_SCHEMAS.items()},
            "quarantine": list(QUARANTINE_FIELDS),
        }
        _atomic_write_json(schema_path, schema_payload)
        _atomic_write_json(quality_path, quality)
        metadata_artifacts = [
            {
                "table": "_schema",
                "partition": {},
                "path": str(schema_path.resolve()),
                "rows": None,
                "bytes": schema_path.stat().st_size,
                "sha256": _sha256_file(schema_path),
            },
            {
                "table": "_quality",
                "partition": {},
                "path": str(quality_path.resolve()),
                "rows": None,
                "bytes": quality_path.stat().st_size,
                "sha256": _sha256_file(quality_path),
            },
        ]
        all_artifacts = artifacts + quarantine_artifacts + metadata_artifacts
        manifest = {
            "status": "completed",
            "schema_version": SCHEMA_VERSION,
            "dataset_id": dataset_id,
            "normalizer_source_path": str(Path(__file__).resolve()),
            "normalizer_source_sha256": source_sha256,
            "normalizer_execution_source_bundle": execution_source_bundle,
            "config": config,
            "inputs": inputs,
            "quality_path": str(quality_path.resolve()),
            "quality": quality,
            "artifacts": all_artifacts,
        }
        _atomic_write_json(manifest_path, manifest)

        catalog_quality_path = catalog_root / "quality" / f"normalization_quality_{dataset_id}.json"
        catalog_quality_path.parent.mkdir(parents=True, exist_ok=True)
        if catalog_quality_path.exists():
            existing_quality = json.loads(catalog_quality_path.read_text(encoding="utf-8"))
            if existing_quality != quality:
                raise RuntimeError(f"catalog quality collision: {catalog_quality_path}")
        else:
            _atomic_write_json(catalog_quality_path, quality)

        return {
            "status": "completed",
            "dataset_id": dataset_id,
            "run_id": dataset_id,
            "raw_file_count": len(raw_files),
            "manifest_path": str(manifest_path.resolve()),
            "quality_path": str(quality_path.resolve()),
            "catalog_quality_path": str(catalog_quality_path.resolve()),
            "quality": quality,
            "artifacts": all_artifacts,
        }
    finally:
        writer.close()
        quarantine_writer.close()
        connection.close()
        if state_path.exists():
            state_path.unlink()


def normalize_roots(
    raw_roots: list[Path],
    derived_root: Path,
    quarantine_root: Path,
    catalog_root: Path,
    include_legacy_ws_folders: bool = False,
    legacy_search_roots: list[Path] | None = None,
    limit_files: int | None = None,
    *,
    input_order: str = "file",
    sequence_scope: str = "observed",
    limit_records_per_file: int | None = None,
    max_records: int | None = None,
    max_open_files: int = 32,
) -> dict[str, Any]:
    _validate_raw_roots(raw_roots, derived_root, quarantine_root, catalog_root)
    files = discover_raw_files(
        raw_roots,
        include_legacy_ws_folders,
        legacy_search_roots,
        max_files=limit_files,
    )
    return normalize_files(
        files,
        derived_root,
        quarantine_root,
        catalog_root,
        input_order=input_order,
        sequence_scope=sequence_scope,
        limit_records_per_file=limit_records_per_file,
        max_records=max_records,
        max_open_files=max_open_files,
    )
