from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable

from .coinbase import (
    extract_event_timestamps,
    extract_product_ids,
    extract_recv_timestamps,
    guess_product_from_path,
    message_channel,
    safe_max,
    safe_min,
    session_id_from_path,
    utc_now_id,
)
from .raw import detect_compression, is_jsonl_path, iter_jsonl, write_jsonl


@dataclass(frozen=True)
class InventoryEntry:
    source_path: str
    source_size_bytes: int
    modified_time: str
    compression: str
    likely_product: str | None
    channel: str | None
    session_id: str | None
    first_recv_ts: str | None
    last_recv_ts: str | None
    first_event_ts: str | None
    last_event_ts: str | None
    estimated_rows: int | None
    estimate_method: str
    sample_parse_errors: int
    sample_error_messages: list[str]
    sample_records: int


def discover_legacy_ws_folders(search_roots: Iterable[Path]) -> list[Path]:
    folders: list[Path] = []
    seen: set[Path] = set()
    for root in search_roots:
        if not root.exists():
            continue
        candidates = [root] if root.name.lower().endswith("_ws_data") else root.rglob("*_ws_data")
        for folder in candidates:
            if folder.is_dir() and folder not in seen:
                folders.append(folder)
                seen.add(folder)
    return sorted(folders)


def discover_legacy_session_folders(search_roots: Iterable[Path]) -> list[Path]:
    sessions: list[Path] = []
    for folder in discover_legacy_ws_folders(search_roots):
        for child in sorted(folder.iterdir()):
            if child.is_dir():
                sessions.append(child)
    return sessions


def discover_raw_files(
    raw_roots: Iterable[Path],
    include_legacy_ws_folders: bool = False,
    legacy_search_roots: Iterable[Path] | None = None,
    max_files: int | None = None,
) -> list[Path]:
    roots = [root for root in raw_roots if root]
    files: list[Path] = []
    seen: set[Path] = set()

    def add(path: Path) -> bool:
        resolved = path.resolve()
        if resolved not in seen and path.is_file() and is_jsonl_path(path):
            seen.add(resolved)
            files.append(path)
            return max_files is not None and len(files) >= max_files
        return False

    for root in roots:
        if root.is_file():
            if add(root):
                return sorted(files)
        elif root.exists():
            for path in root.rglob("*"):
                if add(path):
                    return sorted(files)

    if include_legacy_ws_folders:
        search_roots = list(legacy_search_roots or roots)
        for session in discover_legacy_session_folders(search_roots):
            for path in session.rglob("*"):
                if add(path):
                    return sorted(files)

    return sorted(files)


def _iso_mtime(path: Path) -> str:
    from datetime import datetime, timezone

    return datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc).isoformat()


def _estimate_rows(path: Path, raw_line_lengths: list[int], sample_count: int) -> tuple[int | None, str]:
    if sample_count == 0:
        return 0, "empty_sample"
    if not raw_line_lengths:
        return None, "unavailable"
    avg_line_bytes = max(sum(raw_line_lengths) / len(raw_line_lengths), 1)
    estimate = max(sample_count, int(path.stat().st_size / avg_line_bytes))
    method = "sampled_line_size"
    if detect_compression(path) == "gzip":
        method = "sampled_line_size_compressed_rough"
    return estimate, method


def inspect_file(path: Path, sample_records: int = 5) -> InventoryEntry:
    products: set[str] = set()
    channels: set[str] = set()
    recv_ts: list[str] = []
    event_ts: list[str] = []
    parse_errors: list[str] = []
    raw_line_lengths: list[int] = []
    seen_records = 0

    guessed_product = guess_product_from_path(path)
    if guessed_product:
        products.add(guessed_product)

    for record in iter_jsonl(path, limit=sample_records):
        seen_records += 1
        raw_line_lengths.append(len(record.raw_line.encode("utf-8")) + 1)
        if record.error:
            parse_errors.append(f"line {record.line_number}: {record.error}")
            continue
        assert record.payload is not None
        channels.add(message_channel(record.payload))
        products.update(extract_product_ids(record.payload))
        recv_ts.extend(extract_recv_timestamps(record.payload))
        event_ts.extend(extract_event_timestamps(record.payload))

    estimated_rows, estimate_method = _estimate_rows(path, raw_line_lengths, seen_records)
    likely_product = sorted(products)[0] if products else None
    channel = sorted(channels)[0] if len(channels) == 1 else (",".join(sorted(channels)) or None)

    return InventoryEntry(
        source_path=str(path.resolve()),
        source_size_bytes=path.stat().st_size,
        modified_time=_iso_mtime(path),
        compression=detect_compression(path),
        likely_product=likely_product,
        channel=channel,
        session_id=session_id_from_path(path),
        first_recv_ts=safe_min(recv_ts),
        last_recv_ts=safe_max(recv_ts),
        first_event_ts=safe_min(event_ts),
        last_event_ts=safe_max(event_ts),
        estimated_rows=estimated_rows,
        estimate_method=estimate_method,
        sample_parse_errors=len(parse_errors),
        sample_error_messages=parse_errors[:5],
        sample_records=seen_records,
    )


def run_inventory(
    raw_roots: Iterable[Path],
    catalog_root: Path,
    include_legacy_ws_folders: bool = False,
    legacy_search_roots: Iterable[Path] | None = None,
    sample_records: int = 5,
    max_files: int | None = None,
) -> dict[str, object]:
    run_id = utc_now_id()
    files = discover_raw_files(raw_roots, include_legacy_ws_folders, legacy_search_roots, max_files=max_files)

    entries = [inspect_file(path, sample_records=sample_records) for path in files]
    inventory_dir = catalog_root / "inventory"
    inventory_dir.mkdir(parents=True, exist_ok=True)
    json_path = inventory_dir / f"inventory_{run_id}.json"
    jsonl_path = inventory_dir / f"inventory_{run_id}.jsonl"

    entry_dicts = [asdict(entry) for entry in entries]
    payload: dict[str, object] = {
        "inventory_run_id": run_id,
        "raw_roots": [str(root.resolve()) for root in raw_roots if root],
        "include_legacy_ws_folders": include_legacy_ws_folders,
        "file_count": len(entries),
        "entries": entry_dicts,
    }
    json_path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    write_jsonl(jsonl_path, entry_dicts)
    payload["manifest_path"] = str(json_path)
    payload["jsonl_path"] = str(jsonl_path)
    return payload
