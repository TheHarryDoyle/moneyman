from __future__ import annotations

import bisect
import hashlib
import json
import uuid
from collections import Counter
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from .coinbase import message_channel, normalize_product_id
from .raw import RawRecord, detect_compression, iter_jsonl, write_jsonl


ENGINE_VERSION = "coinbase_l2_book_reconstructor_v1"
RUN_SCHEMA = "moneyman.book_reconstruction_run.v1"
SNAPSHOT_SCHEMA = "moneyman.book_snapshot.v1"
WINDOW_SCHEMA = "moneyman.book_valid_window.v1"
QUALITY_EVENT_SCHEMA = "moneyman.book_quality_event.v1"
SUPPORTED_SOURCE_LAYOUT_INPUT_ORDER_PAIRS = {
    ("ordered_files", "file"),
    ("routed_shards", "receive_time"),
}


class BookDataError(ValueError):
    def __init__(
        self,
        reason: str,
        *,
        event_index: int | None = None,
        update_index: int | None = None,
    ) -> None:
        super().__init__(reason)
        self.reason = reason
        self.event_index = event_index
        self.update_index = update_index


@dataclass(frozen=True)
class BookReconstructionConfig:
    product_id: str
    capture_stream_id: str
    sequence_scope: str = "filtered"
    input_order: str = "file"
    source_layout: str = "ordered_files"
    depth_limit: int = 25
    emit_every_l2_messages: int = 1
    full_hash_sequences: tuple[int, ...] = ()
    max_envelope_gap_seconds: str | None = None
    ticker_tolerance: str = "0.0001"
    start: str | None = None
    end: str | None = None
    max_messages: int | None = None


@dataclass
class SourceOccurrence:
    source_path: str
    source_line: int
    file_ordinal: int


@dataclass
class Envelope:
    payload: dict[str, Any] | None
    raw_line: str
    error: str | None
    source_path: Path
    source_line: int
    file_ordinal: int
    load_ordinal: int
    channel: str
    sequence_num: int | None
    message_ts: str | None
    recv_ts: str | None
    order_ts: str | None
    order_dt: datetime | None
    payload_sha256: str
    occurrences: list[SourceOccurrence] = field(default_factory=list)


@dataclass
class ReplayResult:
    config: BookReconstructionConfig
    snapshots: list[dict[str, Any]]
    quality_events: list[dict[str, Any]]
    windows: list[dict[str, Any]]
    summary: dict[str, Any]


def _new_run_id() -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    return f"{stamp}-{uuid.uuid4().hex[:8]}"


def _json_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _parse_timestamp(value: Any) -> datetime | None:
    if value in {None, ""}:
        return None
    text = str(value)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _message_timestamp(payload: dict[str, Any]) -> str | None:
    for key in ("timestamp", "time", "event_time"):
        value = payload.get(key)
        if value not in {None, ""}:
            return str(value)
    return None


def _sequence_number(payload: dict[str, Any]) -> int | None:
    value = payload.get("sequence_num")
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return value
    return None


def _decimal(value: Any, *, field_name: str) -> Decimal:
    if value is None or isinstance(value, bool):
        raise BookDataError(f"missing_or_invalid_{field_name}")
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise BookDataError(f"missing_or_invalid_{field_name}") from None
    if not parsed.is_finite():
        raise BookDataError(f"missing_or_invalid_{field_name}")
    return parsed


def _decimal_str(value: Decimal) -> str:
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return "0" if text in {"", "-0"} else text


def _first_present(mapping: dict[str, Any], keys: tuple[str, ...]) -> Any:
    for key in keys:
        if key in mapping and mapping[key] is not None:
            return mapping[key]
    return None


def _canonical_side(value: Any) -> str:
    side = str(value or "").strip().lower()
    if side in {"bid", "buy"}:
        return "bid"
    if side in {"ask", "offer", "sell"}:
        return "ask"
    raise BookDataError("unknown_book_side")


def _event_product_id(
    payload: dict[str, Any],
    event: dict[str, Any],
    update: dict[str, Any] | None = None,
) -> str | None:
    value = None
    if update is not None:
        value = update.get("product_id")
    value = value or event.get("product_id") or payload.get("product_id")
    return normalize_product_id(str(value)) if value else None


def _event_timestamp(
    payload: dict[str, Any],
    event: dict[str, Any],
    update: dict[str, Any] | None = None,
) -> str | None:
    if update is not None:
        for key in ("event_time", "timestamp", "time"):
            if update.get(key) not in {None, ""}:
                return str(update[key])
    for key in ("event_time", "timestamp", "time"):
        if event.get(key) not in {None, ""}:
            return str(event[key])
    return _message_timestamp(payload)


def _source_fields(envelope: Envelope) -> dict[str, Any]:
    return {
        "source_path": str(envelope.source_path.resolve()),
        "source_line": envelope.source_line,
        "source_file_ordinal": envelope.file_ordinal,
        "source_occurrence_count": len(envelope.occurrences),
        "source_occurrences": [asdict(item) for item in envelope.occurrences],
        "envelope_sha256": envelope.payload_sha256,
    }


class PriceBook:
    def __init__(self) -> None:
        self.bids: dict[Decimal, Decimal] = {}
        self.asks: dict[Decimal, Decimal] = {}
        self.bid_prices: list[Decimal] = []
        self.ask_prices: list[Decimal] = []
        self.bid_depth = Decimal("0")
        self.ask_depth = Decimal("0")
        self._state_xor = 0

    def clear(self) -> None:
        self.bids.clear()
        self.asks.clear()
        self.bid_prices.clear()
        self.ask_prices.clear()
        self.bid_depth = Decimal("0")
        self.ask_depth = Decimal("0")
        self._state_xor = 0

    @staticmethod
    def _level_fingerprint(side: str, price: Decimal, quantity: Decimal) -> int:
        label = "bid" if side == "bid" else "offer"
        payload = (
            f"{label}\t{_decimal_str(price)}\t{_decimal_str(quantity)}\n".encode(
                "utf-8"
            )
        )
        return int.from_bytes(hashlib.sha256(payload).digest(), "big")

    def _set_level(self, side: str, price: Decimal, quantity: Decimal) -> str:
        if price <= 0:
            raise BookDataError("nonpositive_price")
        if quantity < 0:
            raise BookDataError("negative_quantity")
        book = self.bids if side == "bid" else self.asks
        prices = self.bid_prices if side == "bid" else self.ask_prices
        old_quantity = book.get(price)
        if quantity == 0:
            if old_quantity is None:
                return "missing_delete"
            self._state_xor ^= self._level_fingerprint(side, price, old_quantity)
            del book[price]
            index = bisect.bisect_left(prices, price)
            if index >= len(prices) or prices[index] != price:
                raise AssertionError("price index lost an existing level")
            prices.pop(index)
            if side == "bid":
                self.bid_depth -= old_quantity
            else:
                self.ask_depth -= old_quantity
            return "delete"
        if old_quantity is None:
            book[price] = quantity
            self._state_xor ^= self._level_fingerprint(side, price, quantity)
            bisect.insort(prices, price)
            if side == "bid":
                self.bid_depth += quantity
            else:
                self.ask_depth += quantity
            return "add"
        self._state_xor ^= self._level_fingerprint(side, price, old_quantity)
        book[price] = quantity
        self._state_xor ^= self._level_fingerprint(side, price, quantity)
        if side == "bid":
            self.bid_depth += quantity - old_quantity
        else:
            self.ask_depth += quantity - old_quantity
        return "replace"

    def apply_mutations(
        self,
        mutations: list[tuple[str, Decimal, Decimal, int, int]],
        counters: Counter[str],
    ) -> None:
        for side, price, quantity, _event_index, _update_index in mutations:
            action = self._set_level(side, price, quantity)
            counters[f"level_{action}s"] += 1
            if quantity == 0:
                counters["zero_quantity_deletes"] += 1

    def best_bid(self) -> Decimal | None:
        return self.bid_prices[-1] if self.bid_prices else None

    def best_ask(self) -> Decimal | None:
        return self.ask_prices[0] if self.ask_prices else None

    def invariant_error(self) -> str | None:
        if not self.bid_prices:
            return "empty_bid_book"
        if not self.ask_prices:
            return "empty_ask_book"
        best_bid = self.best_bid()
        best_ask = self.best_ask()
        if best_bid is None or best_ask is None:
            return "empty_book"
        if best_bid == best_ask:
            return "locked_book"
        if best_bid > best_ask:
            return "crossed_book"
        if len(self.bid_prices) != len(self.bids):
            return "bid_index_mismatch"
        if len(self.ask_prices) != len(self.asks):
            return "ask_index_mismatch"
        return None

    def levels(self, side: str, depth_limit: int | None = None) -> list[tuple[Decimal, Decimal]]:
        if side == "bid":
            prices = reversed(self.bid_prices)
            book = self.bids
        else:
            prices = iter(self.ask_prices)
            book = self.asks
        output: list[tuple[Decimal, Decimal]] = []
        for price in prices:
            output.append((price, book[price]))
            if depth_limit is not None and len(output) >= depth_limit:
                break
        return output

    def state_sha256(self, depth_limit: int | None = None) -> str:
        digest = hashlib.sha256()
        for side, label in (("bid", "bid"), ("ask", "offer")):
            for price, quantity in self.levels(side, depth_limit):
                digest.update(
                    f"{label}\t{_decimal_str(price)}\t{_decimal_str(quantity)}\n".encode(
                        "utf-8"
                    )
                )
        return digest.hexdigest()

    def state_fingerprint_sha256(self) -> str:
        payload = {
            "schema": "moneyman.book_state_set_fingerprint.v1",
            "level_hash_xor": self._state_xor.to_bytes(32, "big").hex(),
            "bid_level_count": len(self.bids),
            "ask_level_count": len(self.asks),
            "bid_depth": _decimal_str(self.bid_depth),
            "ask_depth": _decimal_str(self.ask_depth),
        }
        return _sha256_bytes(_json_bytes(payload))

    def snapshot_metrics(
        self,
        depth_limit: int,
        *,
        include_full_line_hash: bool,
    ) -> dict[str, Any]:
        bids = self.levels("bid", depth_limit)
        asks = self.levels("ask", depth_limit)
        best_bid = bids[0][0]
        best_ask = asks[0][0]
        midpoint = (best_bid + best_ask) / Decimal("2")
        spread = best_ask - best_bid
        relative_spread = spread / midpoint if midpoint else Decimal("0")
        emitted_bid_depth = sum((quantity for _, quantity in bids), Decimal("0"))
        emitted_ask_depth = sum((quantity for _, quantity in asks), Decimal("0"))
        emitted_total = emitted_bid_depth + emitted_ask_depth
        imbalance = (
            (emitted_bid_depth - emitted_ask_depth) / emitted_total
            if emitted_total
            else Decimal("0")
        )
        return {
            "best_bid": _decimal_str(best_bid),
            "best_ask": _decimal_str(best_ask),
            "midpoint": _decimal_str(midpoint),
            "spread": _decimal_str(spread),
            "relative_spread": _decimal_str(relative_spread),
            "bid_levels": [
                {"price": _decimal_str(price), "quantity": _decimal_str(quantity)}
                for price, quantity in bids
            ],
            "ask_levels": [
                {"price": _decimal_str(price), "quantity": _decimal_str(quantity)}
                for price, quantity in asks
            ],
            "full_bid_level_count": len(self.bids),
            "full_ask_level_count": len(self.asks),
            "emitted_bid_level_count": len(bids),
            "emitted_ask_level_count": len(asks),
            "depth_truncated": len(self.bids) > len(bids) or len(self.asks) > len(asks),
            "full_bid_depth": _decimal_str(self.bid_depth),
            "full_ask_depth": _decimal_str(self.ask_depth),
            "emitted_bid_depth": _decimal_str(emitted_bid_depth),
            "emitted_ask_depth": _decimal_str(emitted_ask_depth),
            "emitted_book_imbalance": _decimal_str(imbalance),
            "full_book_fingerprint_sha256": self.state_fingerprint_sha256(),
            "full_book_sha256": (
                self.state_sha256() if include_full_line_hash else None
            ),
            "visible_book_sha256": self.state_sha256(depth_limit),
        }


def _record_to_envelope(
    record: RawRecord,
    *,
    file_ordinal: int,
    load_ordinal: int,
) -> Envelope:
    payload = record.payload
    if payload is None:
        payload_sha = _sha256_bytes(record.raw_line.encode("utf-8"))
        return Envelope(
            payload=None,
            raw_line=record.raw_line,
            error=record.error or "missing_payload",
            source_path=record.source_path,
            source_line=record.line_number,
            file_ordinal=file_ordinal,
            load_ordinal=load_ordinal,
            channel="malformed_json",
            sequence_num=None,
            message_ts=None,
            recv_ts=None,
            order_ts=None,
            order_dt=None,
            payload_sha256=payload_sha,
            occurrences=[
                SourceOccurrence(
                    source_path=str(record.source_path.resolve()),
                    source_line=record.line_number,
                    file_ordinal=file_ordinal,
                )
            ],
        )
    recv_ts = payload.get("_recv_ts") or payload.get("recv_ts")
    message_ts = _message_timestamp(payload)
    order_ts = str(recv_ts) if recv_ts not in {None, ""} else message_ts
    payload_sha = _sha256_bytes(_json_bytes(payload))
    return Envelope(
        payload=payload,
        raw_line=record.raw_line,
        error=None,
        source_path=record.source_path,
        source_line=record.line_number,
        file_ordinal=file_ordinal,
        load_ordinal=load_ordinal,
        channel=message_channel(payload).strip().lower(),
        sequence_num=_sequence_number(payload),
        message_ts=message_ts,
        recv_ts=str(recv_ts) if recv_ts not in {None, ""} else None,
        order_ts=order_ts,
        order_dt=_parse_timestamp(order_ts),
        payload_sha256=payload_sha,
        occurrences=[
            SourceOccurrence(
                source_path=str(record.source_path.resolve()),
                source_line=record.line_number,
                file_ordinal=file_ordinal,
            )
        ],
    )


def _validate_reconstruction_config(
    config: BookReconstructionConfig,
) -> tuple[Decimal | None, Decimal]:
    if config.sequence_scope not in {"complete", "filtered"}:
        raise ValueError("sequence_scope must be complete or filtered")
    if config.depth_limit < 1:
        raise ValueError("depth_limit must be at least 1")
    if config.emit_every_l2_messages < 1:
        raise ValueError("emit_every_l2_messages must be at least 1")
    if any(
        isinstance(sequence, bool) or not isinstance(sequence, int) or sequence < 0
        for sequence in config.full_hash_sequences
    ):
        raise ValueError("full_hash_sequences must contain nonnegative integers")
    if config.input_order not in {"file", "receive_time"}:
        raise ValueError("input_order must be file or receive_time")
    if config.source_layout not in {"ordered_files", "routed_shards"}:
        raise ValueError("source_layout must be ordered_files or routed_shards")
    if (
        config.source_layout,
        config.input_order,
    ) not in SUPPORTED_SOURCE_LAYOUT_INPUT_ORDER_PAIRS:
        raise ValueError(
            "supported source_layout/input_order pairs are "
            "ordered_files/file and routed_shards/receive_time"
        )
    if not config.capture_stream_id.strip():
        raise ValueError("capture_stream_id must not be empty")
    if config.max_messages is not None and config.max_messages < 1:
        raise ValueError("max_messages must be at least 1")
    start_dt = _parse_timestamp(config.start)
    end_dt = _parse_timestamp(config.end)
    if config.start is not None and start_dt is None:
        raise ValueError("start must be an ISO-8601 timestamp")
    if config.end is not None and end_dt is None:
        raise ValueError("end must be an ISO-8601 timestamp")
    if start_dt is not None and end_dt is not None and start_dt >= end_dt:
        raise ValueError("start must be earlier than end")
    max_gap = (
        _decimal(config.max_envelope_gap_seconds, field_name="max_envelope_gap_seconds")
        if config.max_envelope_gap_seconds is not None
        else None
    )
    if max_gap is not None and max_gap <= 0:
        raise ValueError("max_envelope_gap_seconds must be positive")
    ticker_tolerance = _decimal(config.ticker_tolerance, field_name="ticker_tolerance")
    if ticker_tolerance < 0:
        raise ValueError("ticker_tolerance must not be negative")
    return max_gap, ticker_tolerance


def _load_envelopes(
    raw_files: list[Path],
    config: BookReconstructionConfig,
) -> tuple[list[Envelope], list[dict[str, Any]], Counter[str]]:
    _validate_reconstruction_config(config)
    if not raw_files:
        raise ValueError("at least one ordered raw file is required")
    counters: Counter[str] = Counter()
    envelopes: list[Envelope] = []
    input_files: list[dict[str, Any]] = []
    start_dt = _parse_timestamp(config.start)
    end_dt = _parse_timestamp(config.end)
    load_ordinal = 0
    resolved_paths: set[Path] = set()

    for file_ordinal, raw_path in enumerate(raw_files):
        path = raw_path.expanduser().resolve()
        if not path.is_file():
            raise FileNotFoundError(path)
        if path in resolved_paths:
            raise ValueError(f"raw file was supplied more than once: {path}")
        resolved_paths.add(path)
        stat = path.stat()
        initial_sha256 = _sha256_file(path)
        file_info: dict[str, Any] = {
            "file_ordinal": file_ordinal,
            "path": str(path),
            "size_bytes": stat.st_size,
            "sha256": initial_sha256,
            "compression": detect_compression(path),
            "modified_time_utc": datetime.fromtimestamp(
                stat.st_mtime,
                tz=timezone.utc,
            ).isoformat(),
            "physical_records": 0,
            "selected_records": 0,
            "first_selected_sequence_num": None,
            "last_selected_sequence_num": None,
            "first_selected_order_ts": None,
            "last_selected_order_ts": None,
        }
        for record in iter_jsonl(path):
            file_info["physical_records"] += 1
            envelope = _record_to_envelope(
                record,
                file_ordinal=file_ordinal,
                load_ordinal=load_ordinal,
            )
            load_ordinal += 1
            if start_dt is not None and envelope.order_dt is not None and envelope.order_dt < start_dt:
                continue
            if end_dt is not None and envelope.order_dt is not None and envelope.order_dt >= end_dt:
                continue
            envelopes.append(envelope)
            file_info["selected_records"] += 1
            if file_info["first_selected_sequence_num"] is None:
                file_info["first_selected_sequence_num"] = envelope.sequence_num
                file_info["first_selected_order_ts"] = envelope.order_ts
            file_info["last_selected_sequence_num"] = envelope.sequence_num
            file_info["last_selected_order_ts"] = envelope.order_ts
        input_files.append(file_info)
        final_stat = path.stat()
        final_sha256 = _sha256_file(path)
        stable = (
            final_stat.st_size == stat.st_size
            and final_stat.st_mtime_ns == stat.st_mtime_ns
            and final_sha256 == initial_sha256
        )
        file_info["verified_stable_during_read"] = stable
        if not stable:
            raise RuntimeError(f"raw file changed while it was being read: {path}")

    counters["raw_records"] = sum(int(item["physical_records"]) for item in input_files)
    counters["selected_raw_records"] = len(envelopes)
    if config.input_order == "receive_time":
        missing_receive = [item for item in envelopes if item.recv_ts is None]
        if missing_receive:
            raise ValueError(
                "input_order=receive_time requires _recv_ts or recv_ts on every selected record"
            )
        envelopes.sort(
            key=lambda item: (
                item.order_dt or datetime.max.replace(tzinfo=timezone.utc),
                item.sequence_num if item.sequence_num is not None else -1,
                item.payload_sha256,
                str(item.source_path.resolve()),
                item.source_line,
            )
        )
    elif config.input_order != "file":
        raise ValueError("input_order must be file or receive_time")
    return envelopes, input_files, counters


def _collapse_routing_replicas(
    envelopes: list[Envelope],
    *,
    source_layout: str,
    counters: Counter[str],
) -> list[Envelope]:
    if source_layout != "routed_shards":
        return envelopes
    output: list[Envelope] = []
    by_identity: dict[tuple[str | None, int | None, str], Envelope] = {}
    for envelope in envelopes:
        identity = (envelope.recv_ts, envelope.sequence_num, envelope.payload_sha256)
        previous = by_identity.get(identity)
        if previous is None:
            by_identity[identity] = envelope
            output.append(envelope)
            continue
        if previous.source_path.resolve() == envelope.source_path.resolve():
            output.append(envelope)
            continue
        previous.occurrences.extend(envelope.occurrences)
        counters["routing_replicas_collapsed"] += 1
    return output


def _quality_event(
    event: str,
    reason: str,
    envelope: Envelope | None,
    *,
    envelope_ordinal: int | None,
    connection_epoch: int,
    product_id: str,
    details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    row: dict[str, Any] = {
        "schema": QUALITY_EVENT_SCHEMA,
        "event": event,
        "reason": reason,
        "product_id": product_id,
        "connection_epoch": connection_epoch,
        "envelope_ordinal": envelope_ordinal,
        "sequence_num": envelope.sequence_num if envelope else None,
        "message_ts": envelope.message_ts if envelope else None,
        "recv_ts": envelope.recv_ts if envelope else None,
    }
    if envelope is not None:
        row.update(_source_fields(envelope))
    if details:
        row["details"] = details
    return row


def _parse_target_l2_events(
    envelope: Envelope,
    product_id: str,
) -> tuple[list[tuple[str, list[tuple[str, Decimal, Decimal, int, int]], str | None]], int]:
    payload = envelope.payload
    if payload is None:
        return [], 0
    events = payload.get("events")
    if not isinstance(events, list):
        raise BookDataError("l2_events_not_list")
    parsed_events: list[
        tuple[str, list[tuple[str, Decimal, Decimal, int, int]], str | None]
    ] = []
    mutation_count = 0
    for event_index, event in enumerate(events):
        if not isinstance(event, dict):
            raise BookDataError("l2_event_not_object", event_index=event_index)
        relevant_product = _event_product_id(payload, event)
        updates = event.get("updates")
        if not isinstance(updates, list):
            if relevant_product in {None, product_id}:
                raise BookDataError("l2_updates_not_list", event_index=event_index)
            continue
        relevant_updates: list[tuple[str, Decimal, Decimal, int, int]] = []
        for update_index, update in enumerate(updates):
            if not isinstance(update, dict):
                raise BookDataError(
                    "l2_update_not_object",
                    event_index=event_index,
                    update_index=update_index,
                )
            update_product = _event_product_id(payload, event, update)
            if update_product != product_id:
                continue
            relevant_product = update_product
            try:
                side = _canonical_side(update.get("side"))
                price = _decimal(
                    _first_present(update, ("price_level", "price")),
                    field_name="price",
                )
                quantity = _decimal(
                    _first_present(update, ("new_quantity", "quantity", "size")),
                    field_name="quantity",
                )
            except BookDataError as exc:
                raise BookDataError(
                    exc.reason,
                    event_index=event_index,
                    update_index=update_index,
                ) from None
            relevant_updates.append((side, price, quantity, event_index, update_index))
        if relevant_product != product_id:
            continue
        event_type = str(event.get("type") or "").lower()
        if event_type not in {"snapshot", "update"}:
            raise BookDataError("unknown_l2_event_type", event_index=event_index)
        event_ts = _event_timestamp(payload, event)
        parsed_events.append((event_type, relevant_updates, event_ts))
        mutation_count += len(relevant_updates)
    return parsed_events, mutation_count


def _ticker_rows(payload: dict[str, Any], product_id: str) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    for event in payload.get("events", []) or []:
        if not isinstance(event, dict):
            continue
        for ticker in event.get("tickers", []) or []:
            if not isinstance(ticker, dict):
                continue
            ticker_product = normalize_product_id(str(ticker.get("product_id") or ""))
            if ticker_product == product_id:
                output.append(ticker)
    return output


def reconstruct_envelopes(
    envelopes: list[Envelope],
    config: BookReconstructionConfig,
    *,
    initial_counters: Counter[str] | None = None,
) -> ReplayResult:
    max_gap, ticker_tolerance = _validate_reconstruction_config(config)

    counters = Counter(initial_counters or {})
    snapshots: list[dict[str, Any]] = []
    quality_events: list[dict[str, Any]] = []
    windows: list[dict[str, Any]] = []
    channel_counts: Counter[str] = Counter()
    invalidation_reasons: Counter[str] = Counter()
    book = PriceBook()
    valid = False
    current_window: dict[str, Any] | None = None
    window_number = 0
    connection_epoch = 0
    snapshot_origin_sequence: int | None = None
    previous_sequence: int | None = None
    previous_clock: datetime | None = None
    seen_sequences: dict[int, tuple[str, Envelope]] = {}
    valid_l2_since_emit = 0
    last_valid_envelope: Envelope | None = None
    last_valid_ordinal: int | None = None
    semantic_stream_digest = hashlib.sha256()
    state_stream_digest = hashlib.sha256()
    visible_stream_digest = hashlib.sha256()
    first_order_ts: str | None = None
    last_order_ts: str | None = None
    first_sequence: int | None = None
    last_sequence: int | None = None
    max_observed_gap: Decimal | None = None
    ticker_mismatch_samples: list[dict[str, Any]] = []
    full_hash_sequences = set(config.full_hash_sequences)

    def close_window(reason: str) -> None:
        nonlocal current_window
        if current_window is None:
            return
        current_window["last_envelope_ordinal"] = last_valid_ordinal
        current_window["last_sequence_num"] = (
            last_valid_envelope.sequence_num if last_valid_envelope else None
        )
        current_window["last_message_ts"] = (
            last_valid_envelope.message_ts if last_valid_envelope else None
        )
        current_window["last_recv_ts"] = (
            last_valid_envelope.recv_ts if last_valid_envelope else None
        )
        current_window["closed_by_reason"] = reason
        windows.append(current_window)
        current_window = None

    def invalidate(
        reason: str,
        envelope: Envelope | None,
        envelope_ordinal: int | None,
        details: dict[str, Any] | None = None,
    ) -> None:
        nonlocal valid, snapshot_origin_sequence, valid_l2_since_emit
        if valid:
            close_window(reason)
        valid = False
        snapshot_origin_sequence = None
        valid_l2_since_emit = 0
        book.clear()
        counters["invalidations"] += 1
        invalidation_reasons[reason] += 1
        quality_events.append(
            _quality_event(
                "book_invalidated",
                reason,
                envelope,
                envelope_ordinal=envelope_ordinal,
                connection_epoch=connection_epoch,
                product_id=config.product_id,
                details=details,
            )
        )

    def validate_from_snapshot(
        envelope: Envelope,
        envelope_ordinal: int,
        event_ts: str | None,
    ) -> None:
        nonlocal valid, current_window, window_number, snapshot_origin_sequence
        nonlocal valid_l2_since_emit
        if valid:
            close_window("fresh_snapshot")
        valid = True
        valid_l2_since_emit = 0
        window_number += 1
        snapshot_origin_sequence = envelope.sequence_num
        current_window = {
            "schema": WINDOW_SCHEMA,
            "window_id": f"window-{window_number:06d}",
            "product_id": config.product_id,
            "capture_stream_id": config.capture_stream_id,
            "connection_epoch": connection_epoch,
            "validity_status": "valid",
            "strict_l2_eligible": config.sequence_scope == "complete",
            "first_envelope_ordinal": envelope_ordinal,
            "first_sequence_num": envelope.sequence_num,
            "first_message_ts": envelope.message_ts,
            "first_event_ts": event_ts,
            "first_recv_ts": envelope.recv_ts,
            "originating_snapshot_sequence_num": envelope.sequence_num,
            "snapshot_rows": 0,
        }
        counters["validations"] += 1
        quality_events.append(
            _quality_event(
                "book_validated",
                "fresh_snapshot",
                envelope,
                envelope_ordinal=envelope_ordinal,
                connection_epoch=connection_epoch,
                product_id=config.product_id,
                details={"window_id": current_window["window_id"]},
            )
        )

    def emit_book_state(
        envelope: Envelope,
        envelope_ordinal: int,
        *,
        event_ts: str | None,
        validity_reason: str,
        source_event_count: int,
        source_mutation_count: int,
        include_full_line_hash: bool,
    ) -> None:
        metrics = book.snapshot_metrics(
            config.depth_limit,
            include_full_line_hash=include_full_line_hash,
        )
        sequence = envelope.sequence_num
        window_id = current_window["window_id"] if current_window else None
        row = {
            "schema": SNAPSHOT_SCHEMA,
            "product_id": config.product_id,
            "capture_stream_id": config.capture_stream_id,
            "connection_epoch": connection_epoch,
            "window_id": window_id,
            "validity_status": "valid",
            "validity_reason": validity_reason,
            "strict_l2_eligible": config.sequence_scope == "complete",
            "envelope_ordinal": envelope_ordinal,
            "sequence_num": sequence,
            "originating_snapshot_sequence_num": snapshot_origin_sequence,
            "source_channel": envelope.channel,
            "message_ts": envelope.message_ts,
            "event_ts": event_ts,
            "recv_ts": envelope.recv_ts,
            "depth_limit": config.depth_limit,
            "source_event_count": source_event_count,
            "source_mutation_count": source_mutation_count,
            **_source_fields(envelope),
            **metrics,
        }
        snapshots.append(row)
        if current_window is not None:
            current_window["snapshot_rows"] += 1
        state_stream_digest.update(
            f"{sequence}\t{metrics['full_book_fingerprint_sha256']}\n".encode(
                "utf-8"
            )
        )
        visible_stream_digest.update(
            f"{sequence}\t{metrics['visible_book_sha256']}\n".encode("utf-8")
        )

    for envelope_ordinal, envelope in enumerate(envelopes):
        counters["replayed_envelopes"] += 1
        channel_counts[envelope.channel] += 1
        if first_order_ts is None:
            first_order_ts = envelope.order_ts
        last_order_ts = envelope.order_ts
        if envelope.payload is None:
            semantic_stream_digest.update(b"ERROR\t")
            semantic_stream_digest.update(envelope.raw_line.encode("utf-8"))
            semantic_stream_digest.update(b"\n")
            counters["parse_errors"] += 1
            invalidate(
                "malformed_raw_json",
                envelope,
                envelope_ordinal,
                {"error": envelope.error},
            )
            continue

        semantic_stream_digest.update(_json_bytes(envelope.payload))
        semantic_stream_digest.update(b"\n")
        sequence = envelope.sequence_num
        sequence_field_malformed = (
            "sequence_num" in envelope.payload and sequence is None
        )
        if sequence is not None:
            counters["sequenced_envelopes"] += 1
            if first_sequence is None:
                first_sequence = sequence
            last_sequence = sequence
        else:
            counters["unsequenced_envelopes"] += 1

        clock = envelope.order_dt
        envelope_usable = True
        if sequence_field_malformed:
            counters["malformed_sequence_numbers"] += 1
            invalidate(
                "malformed_sequence_num",
                envelope,
                envelope_ordinal,
                {"raw_value": envelope.payload.get("sequence_num")},
            )
            envelope_usable = False
        if sequence is not None and clock is None:
            reason = (
                "missing_envelope_timestamp"
                if envelope.order_ts is None
                else "malformed_envelope_timestamp"
            )
            counters[reason + "s"] += 1
            invalidate(reason, envelope, envelope_ordinal)
            envelope_usable = False
        if previous_clock is not None and clock is not None:
            delta = Decimal(str((clock - previous_clock).total_seconds()))
            if delta < 0:
                counters["timestamp_regressions"] += 1
                invalidate(
                    "timestamp_regression",
                    envelope,
                    envelope_ordinal,
                    {"delta_seconds": _decimal_str(delta)},
                )
                envelope_usable = False
            else:
                if max_observed_gap is None or delta > max_observed_gap:
                    max_observed_gap = delta
                if max_gap is not None and delta > max_gap:
                    counters["stale_envelope_gaps"] += 1
                    invalidate(
                        "stale_envelope_gap",
                        envelope,
                        envelope_ordinal,
                        {"gap_seconds": _decimal_str(delta)},
                    )
        if clock is not None:
            previous_clock = clock
        else:
            counters["envelopes_without_order_timestamp"] += 1

        duplicate_envelope = False
        if sequence is not None:
            seen = seen_sequences.get(sequence)
            if seen is not None and seen[0] == envelope.payload_sha256:
                counters["exact_transport_duplicates"] += 1
                quality_events.append(
                    _quality_event(
                        "duplicate_envelope",
                        "exact_transport_duplicate",
                        envelope,
                        envelope_ordinal=envelope_ordinal,
                        connection_epoch=connection_epoch,
                        product_id=config.product_id,
                        details={"first_source": _source_fields(seen[1])},
                    )
                )
                invalidate(
                    "exact_transport_duplicate",
                    envelope,
                    envelope_ordinal,
                )
                duplicate_envelope = True
            elif previous_sequence is not None and sequence < previous_sequence:
                counters["sequence_regressions"] += 1
                connection_epoch += 1
                seen_sequences.clear()
                previous_sequence = None
                invalidate(
                    "sequence_regression_or_reconnect",
                    envelope,
                    envelope_ordinal,
                )
            elif seen is not None:
                counters["sequence_conflicts"] += 1
                invalidate(
                    "conflicting_sequence_duplicate",
                    envelope,
                    envelope_ordinal,
                    {"first_envelope_sha256": seen[0]},
                )
                duplicate_envelope = True

            if not duplicate_envelope:
                if previous_sequence is not None and sequence > previous_sequence + 1:
                    missing = sequence - previous_sequence - 1
                    counters["sequence_gap_events"] += 1
                    counters["missing_sequence_numbers"] += missing
                    quality_events.append(
                        _quality_event(
                            "sequence_gap",
                            (
                                "global_sequence_gap"
                                if config.sequence_scope == "complete"
                                else "ambiguous_filtered_sequence_gap"
                            ),
                            envelope,
                            envelope_ordinal=envelope_ordinal,
                            connection_epoch=connection_epoch,
                            product_id=config.product_id,
                            details={
                                "previous_sequence_num": previous_sequence,
                                "observed_sequence_num": sequence,
                                "missing_count": missing,
                            },
                        )
                    )
                    if config.sequence_scope == "complete":
                        invalidate(
                            "global_sequence_gap",
                            envelope,
                            envelope_ordinal,
                            {"missing_count": missing},
                        )
                previous_sequence = sequence
                seen_sequences[sequence] = (envelope.payload_sha256, envelope)
        elif sequence_field_malformed:
            pass
        elif envelope.channel in {"l2_data", "level2"}:
            counters["unsequenced_l2_envelopes"] += 1
            invalidate(
                "l2_envelope_missing_sequence",
                envelope,
                envelope_ordinal,
            )
            envelope_usable = False
        else:
            counters["unsequenced_control_envelopes"] += 1
            quality_events.append(
                _quality_event(
                    "unsequenced_control_envelope",
                    "preserved_unsequenced_control",
                    envelope,
                    envelope_ordinal=envelope_ordinal,
                    connection_epoch=connection_epoch,
                    product_id=config.product_id,
                    details={
                        "message_type": envelope.payload.get("type"),
                        "message": envelope.payload.get("message"),
                    },
                )
            )

        if duplicate_envelope or not envelope_usable:
            continue

        if envelope.channel in {"ticker", "ticker_batch"}:
            for ticker in _ticker_rows(envelope.payload, config.product_id):
                if not valid:
                    counters["ticker_rows_without_valid_book"] += 1
                    continue
                bid_value = ticker.get("best_bid")
                ask_value = ticker.get("best_ask")
                if bid_value in {None, ""} or ask_value in {None, ""}:
                    counters["ticker_rows_without_bbo"] += 1
                    continue
                try:
                    ticker_bid = _decimal(bid_value, field_name="ticker_bid")
                    ticker_ask = _decimal(ask_value, field_name="ticker_ask")
                except BookDataError as exc:
                    counters["malformed_ticker_bbo_rows"] += 1
                    quality_events.append(
                        _quality_event(
                            "ticker_bbo_rejected",
                            exc.reason,
                            envelope,
                            envelope_ordinal=envelope_ordinal,
                            connection_epoch=connection_epoch,
                            product_id=config.product_id,
                        )
                    )
                    continue
                if ticker_bid <= 0 or ticker_ask <= 0 or ticker_bid >= ticker_ask:
                    counters["malformed_ticker_bbo_rows"] += 1
                    quality_events.append(
                        _quality_event(
                            "ticker_bbo_rejected",
                            "nonpositive_locked_or_crossed_ticker_bbo",
                            envelope,
                            envelope_ordinal=envelope_ordinal,
                            connection_epoch=connection_epoch,
                            product_id=config.product_id,
                        )
                    )
                    continue
                book_bid = book.best_bid()
                book_ask = book.best_ask()
                if book_bid is None or book_ask is None:
                    continue
                counters["ticker_bbo_comparisons"] += 1
                bid_delta = abs(book_bid - ticker_bid)
                ask_delta = abs(book_ask - ticker_ask)
                if bid_delta == 0 and ask_delta == 0:
                    counters["ticker_bbo_exact_matches"] += 1
                if bid_delta <= ticker_tolerance and ask_delta <= ticker_tolerance:
                    counters["ticker_bbo_within_tolerance"] += 1
                elif len(ticker_mismatch_samples) < 20:
                    ticker_mismatch_samples.append(
                        {
                            "sequence_num": sequence,
                            "message_ts": envelope.message_ts,
                            "book_best_bid": _decimal_str(book_bid),
                            "book_best_ask": _decimal_str(book_ask),
                            "ticker_best_bid": _decimal_str(ticker_bid),
                            "ticker_best_ask": _decimal_str(ticker_ask),
                            "bid_delta": _decimal_str(bid_delta),
                            "ask_delta": _decimal_str(ask_delta),
                        }
                    )

        if envelope.channel not in {"l2_data", "level2"}:
            if valid:
                last_valid_envelope = envelope
                last_valid_ordinal = envelope_ordinal
                if sequence is not None and sequence in full_hash_sequences:
                    source_events = envelope.payload.get("events")
                    emit_book_state(
                        envelope,
                        envelope_ordinal,
                        event_ts=envelope.message_ts,
                        validity_reason="continuous_non_l2_checkpoint",
                        source_event_count=(
                            len(source_events) if isinstance(source_events, list) else 0
                        ),
                        source_mutation_count=0,
                        include_full_line_hash=True,
                    )
            continue

        counters["l2_envelopes_seen"] += 1
        try:
            parsed_events, mutation_count = _parse_target_l2_events(
                envelope,
                config.product_id,
            )
        except BookDataError as exc:
            counters["malformed_l2_envelopes"] += 1
            invalidate(
                exc.reason,
                envelope,
                envelope_ordinal,
                {
                    "event_index": exc.event_index,
                    "update_index": exc.update_index,
                },
            )
            continue
        if not parsed_events:
            counters["l2_envelopes_for_other_products"] += 1
            if valid:
                last_valid_envelope = envelope
                last_valid_ordinal = envelope_ordinal
            continue
        counters["target_l2_envelopes"] += 1
        counters["level_mutations"] += mutation_count
        has_snapshot = any(event_type == "snapshot" for event_type, _, _ in parsed_events)
        if not valid and not has_snapshot:
            counters["ignored_updates_while_invalid"] += 1
            continue

        last_event_ts: str | None = None
        snapshot_event_ts: str | None = None
        try:
            for event_type, mutations, event_ts in parsed_events:
                last_event_ts = event_ts or last_event_ts
                if event_type == "snapshot":
                    counters["l2_snapshot_events"] += 1
                    book.clear()
                    snapshot_event_ts = event_ts
                else:
                    counters["l2_update_events"] += 1
                book.apply_mutations(mutations, counters)
        except BookDataError as exc:
            counters["malformed_l2_envelopes"] += 1
            invalidate(
                exc.reason,
                envelope,
                envelope_ordinal,
                {
                    "event_index": exc.event_index,
                    "update_index": exc.update_index,
                },
            )
            continue

        invariant_error = book.invariant_error()
        if invariant_error is not None:
            counters["book_invariant_failures"] += 1
            invalidate(
                invariant_error,
                envelope,
                envelope_ordinal,
            )
            continue
        if has_snapshot:
            validate_from_snapshot(
                envelope,
                envelope_ordinal,
                snapshot_event_ts or last_event_ts,
            )
        if not valid:
            continue

        last_valid_envelope = envelope
        last_valid_ordinal = envelope_ordinal
        valid_l2_since_emit += 1
        is_full_hash_checkpoint = sequence is not None and sequence in full_hash_sequences
        if (
            not has_snapshot
            and not is_full_hash_checkpoint
            and valid_l2_since_emit < config.emit_every_l2_messages
        ):
            continue
        valid_l2_since_emit = 0
        emit_book_state(
            envelope,
            envelope_ordinal,
            event_ts=last_event_ts,
            validity_reason=("fresh_snapshot" if has_snapshot else "continuous_update"),
            source_event_count=len(parsed_events),
            source_mutation_count=mutation_count,
            include_full_line_hash=(has_snapshot or is_full_hash_checkpoint),
        )

    if valid:
        close_window("input_end")

    ticker_comparisons = counters["ticker_bbo_comparisons"]
    exact_rate = (
        Decimal(counters["ticker_bbo_exact_matches"]) / Decimal(ticker_comparisons)
        if ticker_comparisons
        else None
    )
    tolerance_rate = (
        Decimal(counters["ticker_bbo_within_tolerance"]) / Decimal(ticker_comparisons)
        if ticker_comparisons
        else None
    )
    strict_windows = sum(
        1
        for row in windows
        if row["strict_l2_eligible"] and int(row.get("snapshot_rows") or 0) > 0
    )
    summary = {
        "engine": ENGINE_VERSION,
        "run_schema": RUN_SCHEMA,
        "snapshot_schema": SNAPSHOT_SCHEMA,
        "window_schema": WINDOW_SCHEMA,
        "quality_event_schema": QUALITY_EVENT_SCHEMA,
        "product_id": config.product_id,
        "capture_stream_id": config.capture_stream_id,
        "sequence_scope": config.sequence_scope,
        "input_order": config.input_order,
        "source_layout": config.source_layout,
        "status": "completed",
        "strict_l2_eligible": strict_windows > 0,
        "strict_l2_eligible_window_count": strict_windows,
        "book_snapshot_rows": len(snapshots),
        "valid_window_count": len(windows),
        "first_order_ts": first_order_ts,
        "last_order_ts": last_order_ts,
        "first_sequence_num": first_sequence,
        "last_sequence_num": last_sequence,
        "max_observed_envelope_gap_seconds": (
            _decimal_str(max_observed_gap) if max_observed_gap is not None else None
        ),
        "channel_counts": dict(sorted(channel_counts.items())),
        "counts": dict(sorted(counters.items())),
        "invalidation_reasons": dict(sorted(invalidation_reasons.items())),
        "semantic_message_stream_sha256": semantic_stream_digest.hexdigest(),
        "state_stream_sha256": state_stream_digest.hexdigest(),
        "visible_state_stream_sha256": visible_stream_digest.hexdigest(),
        "final_full_book_sha256": snapshots[-1]["full_book_sha256"] if snapshots else None,
        "final_full_book_fingerprint_sha256": (
            snapshots[-1]["full_book_fingerprint_sha256"] if snapshots else None
        ),
        "final_visible_book_sha256": snapshots[-1]["visible_book_sha256"] if snapshots else None,
        "final_best_bid": snapshots[-1]["best_bid"] if snapshots else None,
        "final_best_ask": snapshots[-1]["best_ask"] if snapshots else None,
        "ticker_bbo_exact_match_rate": _decimal_str(exact_rate) if exact_rate is not None else None,
        "ticker_bbo_within_tolerance_rate": (
            _decimal_str(tolerance_rate) if tolerance_rate is not None else None
        ),
        "ticker_mismatch_samples": ticker_mismatch_samples,
        "full_book_sha256_checkpoints": {
            f"{row['connection_epoch']}:{row['sequence_num']}": row[
                "full_book_sha256"
            ]
            for row in snapshots
            if row.get("full_book_sha256") is not None
        },
        "limitations": [
            "This is Coinbase L2 market-by-price, not L3 market-by-order; queue position and hidden liquidity remain unknown.",
            "Only windows explicitly marked valid and strict_l2_eligible may feed a strict-L2 consumer.",
            "Emitted depth is truncated to depth_limit; absence beyond emitted levels means unknown, not zero liquidity.",
            "A complete sequence scope must contain every top-level WebSocket envelope from the selected connection window.",
        ],
    }
    run_fingerprint_payload = {
        "engine": ENGINE_VERSION,
        "config": asdict(config),
        "semantic_message_stream_sha256": summary["semantic_message_stream_sha256"],
        "state_stream_sha256": summary["state_stream_sha256"],
        "visible_state_stream_sha256": summary["visible_state_stream_sha256"],
    }
    summary["semantic_run_fingerprint_sha256"] = _sha256_bytes(
        _json_bytes(run_fingerprint_payload)
    )
    return ReplayResult(
        config=config,
        snapshots=snapshots,
        quality_events=quality_events,
        windows=windows,
        summary=summary,
    )


def _inspect_right_boundary_file(
    path_value: Path,
    *,
    expected_sequence_num: int,
) -> dict[str, Any]:
    path = path_value.expanduser().resolve()
    if not path.is_file():
        raise FileNotFoundError(path)
    initial_stat = path.stat()
    initial_sha256 = _sha256_file(path)
    first_sequenced: Envelope | None = None
    for load_ordinal, record in enumerate(iter_jsonl(path)):
        envelope = _record_to_envelope(
            record,
            file_ordinal=0,
            load_ordinal=load_ordinal,
        )
        if envelope.sequence_num is not None:
            first_sequenced = envelope
            break
    final_stat = path.stat()
    final_sha256 = _sha256_file(path)
    stable = (
        initial_stat.st_size == final_stat.st_size
        and initial_stat.st_mtime_ns == final_stat.st_mtime_ns
        and initial_sha256 == final_sha256
    )
    if not stable:
        raise RuntimeError(f"right-boundary file changed while inspected: {path}")
    if first_sequenced is None:
        raise ValueError(f"right-boundary file has no sequenced envelope: {path}")
    if first_sequenced.sequence_num != expected_sequence_num:
        raise ValueError(
            "right-boundary sequence is not contiguous: "
            f"expected {expected_sequence_num}, observed {first_sequenced.sequence_num}"
        )
    return {
        "role": "right_boundary_continuity_sentinel",
        "path": str(path),
        "size_bytes": initial_stat.st_size,
        "sha256": initial_sha256,
        "compression": detect_compression(path),
        "modified_time_utc": datetime.fromtimestamp(
            initial_stat.st_mtime,
            tz=timezone.utc,
        ).isoformat(),
        "verified_stable_during_read": True,
        "first_sequenced_source_line": first_sequenced.source_line,
        "first_sequence_num": first_sequenced.sequence_num,
        "first_channel": first_sequenced.channel,
        "first_message_ts": first_sequenced.message_ts,
        "first_envelope_sha256": first_sequenced.payload_sha256,
    }


def run_book_reconstruction(
    raw_files: list[Path],
    derived_root: Path,
    catalog_root: Path,
    product: str,
    *,
    capture_stream_id: str,
    sequence_scope: str = "filtered",
    input_order: str = "file",
    source_layout: str = "ordered_files",
    depth_limit: int = 25,
    emit_every_l2_messages: int = 1,
    full_hash_sequences: list[int] | tuple[int, ...] | None = None,
    max_envelope_gap_seconds: str | None = None,
    ticker_tolerance: str = "0.0001",
    start: str | None = None,
    end: str | None = None,
    max_messages: int | None = None,
    right_boundary_file: Path | None = None,
) -> dict[str, Any]:
    product_id = normalize_product_id(product)
    if not product_id:
        raise ValueError("product must normalize to a product id such as XRP-USD")
    config = BookReconstructionConfig(
        product_id=product_id,
        capture_stream_id=capture_stream_id,
        sequence_scope=sequence_scope,
        input_order=input_order,
        source_layout=source_layout,
        depth_limit=depth_limit,
        emit_every_l2_messages=emit_every_l2_messages,
        full_hash_sequences=tuple(sorted(set(full_hash_sequences or []))),
        max_envelope_gap_seconds=max_envelope_gap_seconds,
        ticker_tolerance=ticker_tolerance,
        start=start,
        end=end,
        max_messages=max_messages,
    )
    envelopes, input_files, counters = _load_envelopes(raw_files, config)
    envelopes = _collapse_routing_replicas(
        envelopes,
        source_layout=source_layout,
        counters=counters,
    )
    counters["canonical_envelopes_before_limit"] = len(envelopes)
    if config.max_messages is not None and len(envelopes) > config.max_messages:
        envelopes = envelopes[: config.max_messages]
        counters["max_messages_truncated"] = 1
    counters["canonical_envelopes"] = len(envelopes)
    replay = reconstruct_envelopes(envelopes, config, initial_counters=counters)
    right_boundary: dict[str, Any] | None = None
    if right_boundary_file is not None:
        right_boundary_path = right_boundary_file.expanduser().resolve()
        input_paths = {Path(item["path"]).resolve() for item in input_files}
        if right_boundary_path in input_paths:
            raise ValueError("right_boundary_file must not also be a replay input")
        last_sequence = replay.summary.get("last_sequence_num")
        if not isinstance(last_sequence, int):
            raise ValueError("cannot verify a right boundary without a final sequence number")
        right_boundary = _inspect_right_boundary_file(
            right_boundary_path,
            expected_sequence_num=last_sequence + 1,
        )

    run_id = _new_run_id()
    run_dir = derived_root / "v1" / "book_reconstruction" / run_id
    run_dir.mkdir(parents=True, exist_ok=False)
    config_path = run_dir / "config.json"
    snapshots_path = run_dir / "book_snapshots.jsonl"
    quality_events_path = run_dir / "book_quality_events.jsonl"
    windows_path = run_dir / "book_windows.jsonl"
    manifest_path = run_dir / "manifest.json"
    report_path = catalog_root / "quality" / f"book_reconstruction_{run_id}.json"
    report_path.parent.mkdir(parents=True, exist_ok=True)

    config_path.write_text(
        json.dumps(asdict(config), indent=2, sort_keys=True),
        encoding="utf-8",
    )
    write_jsonl(snapshots_path, replay.snapshots)
    write_jsonl(quality_events_path, replay.quality_events)
    write_jsonl(windows_path, replay.windows)
    artifacts = {
        "config": {
            "path": config_path.name,
            "sha256": _sha256_file(config_path),
        },
        "book_snapshots": {
            "path": snapshots_path.name,
            "sha256": _sha256_file(snapshots_path),
            "rows": len(replay.snapshots),
        },
        "book_quality_events": {
            "path": quality_events_path.name,
            "sha256": _sha256_file(quality_events_path),
            "rows": len(replay.quality_events),
        },
        "book_windows": {
            "path": windows_path.name,
            "sha256": _sha256_file(windows_path),
            "rows": len(replay.windows),
        },
    }
    engine_source_sha256 = _sha256_file(Path(__file__))
    manifest = {
        **replay.summary,
        "run_id": run_id,
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "run_dir": str(run_dir.resolve()),
        "engine_source_sha256": engine_source_sha256,
        "config": asdict(config),
        "input_files": input_files,
        "right_boundary": right_boundary,
        "artifacts": artifacts,
    }
    manifest["strict_source_provenance"] = {
        "completeness_claim": (
            "verified_connection_prefix_with_contiguous_right_boundary"
            if (
                config.sequence_scope == "complete"
                and replay.summary.get("first_sequence_num") == 0
                and right_boundary is not None
            )
            else "operator_attested_complete_envelope_window"
            if config.sequence_scope == "complete"
            else "filtered_or_incomplete_envelope_window"
        ),
        "source_layout": config.source_layout,
        "ordering_method": (
            "supplied_file_then_line_order"
            if config.input_order == "file"
            else "receive_timestamp_then_sequence_canonical_merge"
        ),
        "all_input_files_stable_during_read": all(
            bool(item["verified_stable_during_read"]) for item in input_files
        ),
        "duplicate_input_paths_rejected": True,
        "global_sequence_policy": (
            "every sequenced top-level envelope is audited; a gap invalidates the book"
        ),
        "boundary_limitation": (
            "The runner proves continuity inside supplied inputs and records any supplied "
            "right-boundary sentinel; absent complete boundary proof, the operator attests "
            "that the bounded inputs contain every required envelope."
        ),
    }
    provenance_fingerprint_payload = {
        "engine": ENGINE_VERSION,
        "engine_source_sha256": engine_source_sha256,
        "config_sha256": artifacts["config"]["sha256"],
        "ordered_input_files": [
            {
                "file_ordinal": item["file_ordinal"],
                "size_bytes": item["size_bytes"],
                "sha256": item["sha256"],
                "compression": item["compression"],
                "verified_stable_during_read": item["verified_stable_during_read"],
            }
            for item in input_files
        ],
        "right_boundary": right_boundary,
        "semantic_run_fingerprint_sha256": replay.summary[
            "semantic_run_fingerprint_sha256"
        ],
        "state_stream_sha256": replay.summary["state_stream_sha256"],
        "visible_state_stream_sha256": replay.summary[
            "visible_state_stream_sha256"
        ],
    }
    manifest["run_fingerprint_sha256"] = _sha256_bytes(
        _json_bytes(provenance_fingerprint_payload)
    )
    manifest_path.write_text(
        json.dumps(manifest, indent=2, sort_keys=True, default=str),
        encoding="utf-8",
    )
    report_path.write_text(
        json.dumps(manifest, indent=2, sort_keys=True, default=str),
        encoding="utf-8",
    )
    return {
        "run_id": run_id,
        "run_dir": str(run_dir),
        "manifest_path": str(manifest_path),
        "report_path": str(report_path),
        "summary": manifest,
    }


def _artifact_path(run_dir: Path, artifact: dict[str, Any]) -> Path:
    path = Path(str(artifact.get("path") or ""))
    return path if path.is_absolute() else run_dir / path


def audit_book_reconstruction_run(
    manifest_path: Path,
    *,
    product_id: str | None = None,
) -> dict[str, Any]:
    manifest_file = manifest_path.resolve()
    errors: list[str] = []
    try:
        manifest = json.loads(manifest_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return {
            "manifest_path": str(manifest_file),
            "valid": False,
            "strict_l2_eligible": False,
            "errors": [f"manifest_read_error: {exc}"],
        }
    if not isinstance(manifest, dict):
        return {
            "manifest_path": str(manifest_file),
            "valid": False,
            "strict_l2_eligible": False,
            "errors": ["manifest_not_object"],
        }
    normalized_product = normalize_product_id(product_id) if product_id else None
    if manifest.get("engine") != ENGINE_VERSION:
        errors.append("unsupported_engine")
    if manifest.get("run_schema") != RUN_SCHEMA:
        errors.append("unsupported_run_schema")
    if manifest.get("snapshot_schema") != SNAPSHOT_SCHEMA:
        errors.append("unsupported_snapshot_schema")
    if manifest.get("window_schema") != WINDOW_SCHEMA:
        errors.append("unsupported_window_schema")
    if manifest.get("quality_event_schema") != QUALITY_EVENT_SCHEMA:
        errors.append("unsupported_quality_event_schema")
    if manifest.get("status") != "completed":
        errors.append("run_not_completed")
    if normalized_product and manifest.get("product_id") != normalized_product:
        errors.append("product_mismatch")
    if manifest.get("sequence_scope") != "complete":
        errors.append("sequence_scope_not_complete")
    engine_source_sha256 = manifest.get("engine_source_sha256")
    if engine_source_sha256 != _sha256_file(Path(__file__)):
        errors.append("engine_source_sha256_mismatch")
    capture_stream_id = manifest.get("capture_stream_id")
    if not isinstance(capture_stream_id, str) or not capture_stream_id.strip():
        errors.append("missing_capture_stream_id")
    manifest_config = manifest.get("config")
    if not isinstance(manifest_config, dict):
        errors.append("missing_config")
        manifest_config = {}
    elif manifest_config.get("product_id") != manifest.get("product_id"):
        errors.append("config_product_mismatch")
    if manifest_config.get("sequence_scope") != manifest.get("sequence_scope"):
        errors.append("config_sequence_scope_mismatch")
    if manifest_config.get("capture_stream_id") != capture_stream_id:
        errors.append("config_capture_stream_mismatch")
    if manifest_config.get("input_order") != manifest.get("input_order"):
        errors.append("config_input_order_mismatch")
    if manifest_config.get("source_layout") != manifest.get("source_layout"):
        errors.append("config_source_layout_mismatch")
    if (
        manifest.get("source_layout"),
        manifest.get("input_order"),
    ) not in SUPPORTED_SOURCE_LAYOUT_INPUT_ORDER_PAIRS:
        errors.append("unsupported_source_layout_input_order_pair")
    source_provenance = manifest.get("strict_source_provenance")
    if not isinstance(source_provenance, dict):
        errors.append("missing_strict_source_provenance")
    else:
        completeness_claim = source_provenance.get("completeness_claim")
        if completeness_claim not in {
            "operator_attested_complete_envelope_window",
            "verified_connection_prefix_with_contiguous_right_boundary",
        }:
            errors.append("missing_complete_window_attestation")
        if source_provenance.get("source_layout") != manifest_config.get(
            "source_layout"
        ):
            errors.append("source_provenance_layout_mismatch")
        if not source_provenance.get("all_input_files_stable_during_read"):
            errors.append("source_files_not_verified_stable")
    right_boundary = manifest.get("right_boundary")
    if right_boundary is not None:
        if not isinstance(right_boundary, dict):
            errors.append("right_boundary_not_object")
            right_boundary = None
        else:
            boundary_path = Path(str(right_boundary.get("path") or "")).resolve()
            if not boundary_path.is_file():
                errors.append("right_boundary_file_missing")
            else:
                if right_boundary.get("size_bytes") != boundary_path.stat().st_size:
                    errors.append("right_boundary_size_mismatch")
                if right_boundary.get("sha256") != _sha256_file(boundary_path):
                    errors.append("right_boundary_sha256_mismatch")
                if right_boundary.get("compression") != detect_compression(
                    boundary_path
                ):
                    errors.append("right_boundary_compression_mismatch")
            last_sequence_num = manifest.get("last_sequence_num")
            if (
                not isinstance(last_sequence_num, int)
                or right_boundary.get("first_sequence_num") != last_sequence_num + 1
            ):
                errors.append("right_boundary_sequence_mismatch")
            if not right_boundary.get("verified_stable_during_read"):
                errors.append("right_boundary_not_verified_stable")
    if (
        isinstance(source_provenance, dict)
        and source_provenance.get("completeness_claim")
        == "verified_connection_prefix_with_contiguous_right_boundary"
        and (right_boundary is None or manifest.get("first_sequence_num") != 0)
    ):
        errors.append("verified_boundary_claim_not_supported")
    input_files = manifest.get("input_files")
    if not isinstance(input_files, list) or not input_files:
        errors.append("missing_input_files")
        input_files = []
    seen_input_paths: set[Path] = set()
    for input_ordinal, item in enumerate(input_files):
        if not isinstance(item, dict):
            errors.append("input_file_entry_not_object")
            continue
        if item.get("file_ordinal") != input_ordinal:
            errors.append("input_file_ordinal_mismatch")
        if not item.get("verified_stable_during_read"):
            errors.append("input_file_not_verified_stable")
        input_path = Path(str(item.get("path") or "")).resolve()
        if input_path in seen_input_paths:
            errors.append("duplicate_input_file_path")
        seen_input_paths.add(input_path)
        if not input_path.is_file():
            errors.append("input_file_missing")
            continue
        size_bytes = item.get("size_bytes")
        if (
            isinstance(size_bytes, bool)
            or not isinstance(size_bytes, int)
            or size_bytes < 0
            or input_path.stat().st_size != size_bytes
        ):
            errors.append("input_file_size_mismatch")
        if item.get("compression") != detect_compression(input_path):
            errors.append("input_file_compression_mismatch")
        if item.get("sha256") != _sha256_file(input_path):
            errors.append("input_file_sha256_mismatch")
    artifacts = manifest.get("artifacts")
    if not isinstance(artifacts, dict):
        errors.append("missing_artifacts")
        artifacts = {}
    required = (
        "config",
        "book_windows",
        "book_snapshots",
        "book_quality_events",
    )
    observed_rows: dict[str, int] = {}
    window_ids: set[str] = set()
    strict_window_ids: set[str] = set()
    snapshot_rows_by_window: Counter[str] = Counter()
    declared_snapshot_rows_by_window: dict[str, int] = {}
    window_rows_by_id: dict[str, dict[str, Any]] = {}
    first_snapshot_by_window: dict[str, dict[str, Any]] = {}
    state_stream_digest = hashlib.sha256()
    visible_state_stream_digest = hashlib.sha256()
    observed_full_hash_checkpoints: dict[str, str] = {}
    last_snapshot: dict[str, Any] | None = None
    last_snapshot_sequence_by_epoch: dict[int, int] = {}
    config_artifact_sha256: str | None = None

    def expected_count(value: Any, label: str) -> int | None:
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            errors.append(f"{label}_not_nonnegative_integer")
            return None
        return value

    def valid_sha256(value: Any) -> bool:
        if not isinstance(value, str) or len(value) != 64:
            return False
        try:
            int(value, 16)
        except ValueError:
            return False
        return True

    def visible_hash_from_row(row: dict[str, Any]) -> str | None:
        digest = hashlib.sha256()
        for field_name, label, descending in (
            ("bid_levels", "bid", True),
            ("ask_levels", "offer", False),
        ):
            levels = row.get(field_name)
            if not isinstance(levels, list) or not levels:
                errors.append("snapshot_row_missing_visible_levels")
                return None
            previous_price: Decimal | None = None
            for level in levels:
                if not isinstance(level, dict):
                    errors.append("snapshot_row_invalid_visible_level")
                    return None
                try:
                    price = _decimal(level.get("price"), field_name="visible_price")
                    quantity = _decimal(
                        level.get("quantity"),
                        field_name="visible_quantity",
                    )
                except BookDataError:
                    errors.append("snapshot_row_invalid_visible_level")
                    return None
                if price <= 0 or quantity <= 0:
                    errors.append("snapshot_row_nonpositive_visible_level")
                    return None
                if previous_price is not None:
                    wrongly_ordered = (
                        price >= previous_price if descending else price <= previous_price
                    )
                    if wrongly_ordered:
                        errors.append("snapshot_row_visible_levels_not_sorted")
                        return None
                previous_price = price
                digest.update(
                    f"{label}\t{_decimal_str(price)}\t{_decimal_str(quantity)}\n".encode(
                        "utf-8"
                    )
                )
        return digest.hexdigest()

    for name in required:
        artifact = artifacts.get(name)
        if not isinstance(artifact, dict):
            errors.append(f"missing_{name}_artifact")
            continue
        path = _artifact_path(manifest_file.parent, artifact).resolve()
        try:
            path.relative_to(manifest_file.parent)
        except ValueError:
            errors.append(f"{name}_path_outside_run")
            continue
        if not path.is_file():
            errors.append(f"missing_{name}_file")
            continue
        expected_hash = artifact.get("sha256")
        if expected_hash != _sha256_file(path):
            errors.append(f"{name}_sha256_mismatch")
            continue
        if name == "config":
            config_artifact_sha256 = str(expected_hash)
            try:
                artifact_config = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                errors.append("config_parse_error")
            else:
                if artifact_config != manifest_config:
                    errors.append("config_artifact_mismatch")
            continue
        rows = 0
        for record in iter_jsonl(path):
            if record.payload is None:
                errors.append(f"{name}_parse_error")
                continue
            row = record.payload
            rows += 1
            if name == "book_snapshots":
                if row.get("schema") != SNAPSHOT_SCHEMA:
                    errors.append("snapshot_row_schema_mismatch")
                if row.get("product_id") != manifest.get("product_id"):
                    errors.append("snapshot_row_product_mismatch")
                if row.get("capture_stream_id") != capture_stream_id:
                    errors.append("snapshot_row_capture_stream_mismatch")
                if row.get("validity_status") != "valid":
                    errors.append("snapshot_row_not_valid")
                if not row.get("strict_l2_eligible"):
                    errors.append("snapshot_row_not_strict_l2_eligible")
                window_id = row.get("window_id")
                if not isinstance(window_id, str) or not window_id:
                    errors.append("snapshot_row_missing_window_id")
                else:
                    snapshot_rows_by_window[window_id] += 1
                    first_snapshot_by_window.setdefault(window_id, row)
                sequence_num = row.get("sequence_num")
                connection_epoch = row.get("connection_epoch")
                if (
                    isinstance(sequence_num, bool)
                    or not isinstance(sequence_num, int)
                    or isinstance(connection_epoch, bool)
                    or not isinstance(connection_epoch, int)
                    or connection_epoch < 0
                ):
                    errors.append("snapshot_row_invalid_sequence_or_epoch")
                else:
                    previous_snapshot_sequence = last_snapshot_sequence_by_epoch.get(
                        connection_epoch
                    )
                    if (
                        previous_snapshot_sequence is not None
                        and sequence_num <= previous_snapshot_sequence
                    ):
                        errors.append("snapshot_rows_not_sequence_monotonic")
                    last_snapshot_sequence_by_epoch[connection_epoch] = sequence_num
                full_fingerprint = row.get("full_book_fingerprint_sha256")
                visible_hash = row.get("visible_book_sha256")
                if not valid_sha256(full_fingerprint):
                    errors.append("snapshot_row_invalid_full_book_fingerprint")
                else:
                    state_stream_digest.update(
                        f"{sequence_num}\t{full_fingerprint}\n".encode("utf-8")
                    )
                if not valid_sha256(visible_hash):
                    errors.append("snapshot_row_invalid_visible_book_hash")
                else:
                    visible_state_stream_digest.update(
                        f"{sequence_num}\t{visible_hash}\n".encode("utf-8")
                    )
                    recomputed_visible_hash = visible_hash_from_row(row)
                    if recomputed_visible_hash != visible_hash:
                        errors.append("snapshot_row_visible_book_hash_mismatch")
                full_line_hash = row.get("full_book_sha256")
                if full_line_hash is not None:
                    if not valid_sha256(full_line_hash):
                        errors.append("snapshot_row_invalid_full_book_hash")
                    else:
                        observed_full_hash_checkpoints[
                            f"{connection_epoch}:{sequence_num}"
                        ] = full_line_hash
                try:
                    best_bid = _decimal(row.get("best_bid"), field_name="best_bid")
                    best_ask = _decimal(row.get("best_ask"), field_name="best_ask")
                except BookDataError:
                    errors.append("snapshot_row_invalid_bbo")
                else:
                    if best_bid >= best_ask:
                        errors.append("snapshot_row_locked_or_crossed")
                    bid_levels = row.get("bid_levels")
                    ask_levels = row.get("ask_levels")
                    try:
                        first_bid = _decimal(
                            bid_levels[0].get("price"),
                            field_name="first_bid_price",
                        )
                        first_ask = _decimal(
                            ask_levels[0].get("price"),
                            field_name="first_ask_price",
                        )
                    except (AttributeError, BookDataError, IndexError, TypeError):
                        errors.append("snapshot_row_invalid_visible_bbo")
                    else:
                        if first_bid != best_bid:
                            errors.append("snapshot_row_best_bid_mismatch")
                        if first_ask != best_ask:
                            errors.append("snapshot_row_best_ask_mismatch")
                last_snapshot = row
            elif name == "book_windows":
                if row.get("schema") != WINDOW_SCHEMA:
                    errors.append("window_row_schema_mismatch")
                if row.get("product_id") != manifest.get("product_id"):
                    errors.append("window_row_product_mismatch")
                if row.get("capture_stream_id") != capture_stream_id:
                    errors.append("window_row_capture_stream_mismatch")
                if row.get("validity_status") != "valid":
                    errors.append("window_row_not_valid")
                if not row.get("strict_l2_eligible"):
                    errors.append("window_row_not_strict_l2_eligible")
                window_id = row.get("window_id")
                if not isinstance(window_id, str) or not window_id:
                    errors.append("window_row_missing_window_id")
                elif window_id in window_ids:
                    errors.append("duplicate_window_id")
                else:
                    window_ids.add(window_id)
                    window_rows_by_id[window_id] = row
                    if row.get("strict_l2_eligible"):
                        strict_window_ids.add(window_id)
                    declared_rows = expected_count(
                        row.get("snapshot_rows"),
                        "window_snapshot_rows",
                    )
                    if declared_rows is not None:
                        declared_snapshot_rows_by_window[window_id] = declared_rows
            else:
                if row.get("schema") != QUALITY_EVENT_SCHEMA:
                    errors.append("quality_event_row_schema_mismatch")
                if row.get("product_id") != manifest.get("product_id"):
                    errors.append("quality_event_row_product_mismatch")
        observed_rows[name] = rows
        expected_rows = expected_count(artifact.get("rows"), f"{name}_artifact_rows")
        if expected_rows is not None and rows != expected_rows:
            errors.append(f"{name}_row_count_mismatch")
    for window_id in snapshot_rows_by_window:
        if window_id not in strict_window_ids:
            errors.append("snapshot_window_not_in_strict_windows")
    for window_id, declared_rows in declared_snapshot_rows_by_window.items():
        if snapshot_rows_by_window.get(window_id, 0) != declared_rows:
            errors.append("window_snapshot_row_count_mismatch")
    for window_id in strict_window_ids:
        window_row = window_rows_by_id.get(window_id)
        first_snapshot = first_snapshot_by_window.get(window_id)
        if first_snapshot is None or window_row is None:
            errors.append("strict_window_missing_origin_snapshot")
            continue
        if first_snapshot.get("validity_reason") != "fresh_snapshot":
            errors.append("strict_window_first_row_not_fresh_snapshot")
        if first_snapshot.get("sequence_num") != window_row.get("first_sequence_num"):
            errors.append("strict_window_origin_sequence_mismatch")
        if first_snapshot.get("originating_snapshot_sequence_num") != window_row.get(
            "originating_snapshot_sequence_num"
        ):
            errors.append("strict_window_snapshot_attribution_mismatch")
        if first_snapshot.get("connection_epoch") != window_row.get("connection_epoch"):
            errors.append("strict_window_connection_epoch_mismatch")
    if manifest.get("state_stream_sha256") != state_stream_digest.hexdigest():
        errors.append("state_stream_sha256_mismatch")
    if (
        manifest.get("visible_state_stream_sha256")
        != visible_state_stream_digest.hexdigest()
    ):
        errors.append("visible_state_stream_sha256_mismatch")
    if manifest.get("full_book_sha256_checkpoints") != observed_full_hash_checkpoints:
        errors.append("full_book_sha256_checkpoints_mismatch")
    if last_snapshot is not None:
        final_pairs = (
            ("final_full_book_sha256", "full_book_sha256"),
            (
                "final_full_book_fingerprint_sha256",
                "full_book_fingerprint_sha256",
            ),
            ("final_visible_book_sha256", "visible_book_sha256"),
            ("final_best_bid", "best_bid"),
            ("final_best_ask", "best_ask"),
        )
        for manifest_field, row_field in final_pairs:
            if manifest.get(manifest_field) != last_snapshot.get(row_field):
                errors.append(f"{manifest_field}_mismatch")
    semantic_fingerprint_payload = {
        "engine": ENGINE_VERSION,
        "config": manifest_config,
        "semantic_message_stream_sha256": manifest.get(
            "semantic_message_stream_sha256"
        ),
        "state_stream_sha256": manifest.get("state_stream_sha256"),
        "visible_state_stream_sha256": manifest.get(
            "visible_state_stream_sha256"
        ),
    }
    expected_semantic_fingerprint = _sha256_bytes(
        _json_bytes(semantic_fingerprint_payload)
    )
    if (
        manifest.get("semantic_run_fingerprint_sha256")
        != expected_semantic_fingerprint
    ):
        errors.append("semantic_run_fingerprint_sha256_mismatch")
    if config_artifact_sha256 is None:
        errors.append("missing_config_artifact_sha256")
    else:
        provenance_fingerprint_payload = {
            "engine": ENGINE_VERSION,
            "engine_source_sha256": engine_source_sha256,
            "config_sha256": config_artifact_sha256,
            "ordered_input_files": [
                {
                    "file_ordinal": item.get("file_ordinal"),
                    "size_bytes": item.get("size_bytes"),
                    "sha256": item.get("sha256"),
                    "compression": item.get("compression"),
                    "verified_stable_during_read": item.get(
                        "verified_stable_during_read"
                    ),
                }
                for item in input_files
                if isinstance(item, dict)
            ],
            "right_boundary": right_boundary,
            "semantic_run_fingerprint_sha256": manifest.get(
                "semantic_run_fingerprint_sha256"
            ),
            "state_stream_sha256": manifest.get("state_stream_sha256"),
            "visible_state_stream_sha256": manifest.get(
                "visible_state_stream_sha256"
            ),
        }
        expected_run_fingerprint = _sha256_bytes(
            _json_bytes(provenance_fingerprint_payload)
        )
        if manifest.get("run_fingerprint_sha256") != expected_run_fingerprint:
            errors.append("run_fingerprint_sha256_mismatch")
    eligible_window_ids = {
        window_id
        for window_id in strict_window_ids
        if snapshot_rows_by_window.get(window_id, 0) > 0
    }
    manifest_snapshot_rows = expected_count(
        manifest.get("book_snapshot_rows"),
        "manifest_book_snapshot_rows",
    )
    if (
        manifest_snapshot_rows is not None
        and observed_rows.get("book_snapshots", 0) != manifest_snapshot_rows
    ):
        errors.append("manifest_snapshot_row_count_mismatch")
    manifest_window_rows = expected_count(
        manifest.get("valid_window_count"),
        "manifest_valid_window_count",
    )
    if (
        manifest_window_rows is not None
        and observed_rows.get("book_windows", 0) != manifest_window_rows
    ):
        errors.append("manifest_window_row_count_mismatch")
    manifest_strict_windows = expected_count(
        manifest.get("strict_l2_eligible_window_count"),
        "manifest_strict_l2_eligible_window_count",
    )
    if (
        manifest_strict_windows is not None
        and len(eligible_window_ids) != manifest_strict_windows
    ):
        errors.append("manifest_strict_window_count_mismatch")
    derived_eligibility = bool(
        eligible_window_ids and observed_rows.get("book_snapshots", 0) > 0
    )
    if bool(manifest.get("strict_l2_eligible")) != derived_eligibility:
        errors.append("manifest_strict_l2_eligibility_mismatch")
    valid = not errors
    return {
        "manifest_path": str(manifest_file),
        "run_id": manifest.get("run_id"),
        "product_id": manifest.get("product_id"),
        "valid": valid,
        "strict_l2_eligible": bool(valid and derived_eligibility),
        "valid_window_count": observed_rows.get("book_windows", 0),
        "strict_l2_eligible_window_count": len(eligible_window_ids),
        "book_snapshot_rows": observed_rows.get("book_snapshots", 0),
        "first_order_ts": manifest.get("first_order_ts"),
        "last_order_ts": manifest.get("last_order_ts"),
        "observed_rows": observed_rows,
        "errors": sorted(set(errors)),
    }


def discover_audited_book_runs(
    derived_root: Path,
    product_id: str,
) -> dict[str, Any]:
    root = derived_root / "v1" / "book_reconstruction"
    manifest_paths = sorted(root.glob("*/manifest.json")) if root.exists() else []
    audits = [
        audit_book_reconstruction_run(path, product_id=product_id)
        for path in manifest_paths
    ]
    matching = [row for row in audits if row.get("product_id") == product_id]
    eligible = [row for row in matching if row.get("strict_l2_eligible")]
    return {
        "contract_root": str(root),
        "manifests_found": len(manifest_paths),
        "matching_product_runs": len(matching),
        "eligible_runs": len(eligible),
        "audits": audits,
        "eligible": eligible,
    }
