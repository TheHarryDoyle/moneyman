from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .coinbase import (
    extract_event_timestamps,
    extract_product_ids,
    extract_recv_timestamps,
    guess_product_from_path,
    message_channel,
    safe_max,
    safe_min,
)
from .raw import detect_compression, is_jsonl_path, open_text


def _file_format(path: Path) -> str:
    name = path.name.lower()
    if name.endswith(".jsonl.gz"):
        return "jsonl.gz"
    if name.endswith(".jsonl"):
        return "jsonl"
    if name.endswith(".parquet"):
        return "parquet"
    if name.endswith(".feather"):
        return "feather"
    return path.suffix.lower().lstrip(".") or "unknown"


def _same_stem_key(path: Path) -> str:
    name = path.name
    for suffix in (".jsonl.gz", ".jsonl", ".parquet", ".feather"):
        if name.lower().endswith(suffix):
            name = name[: -len(suffix)]
            break
    return str(path.with_name(name).resolve())


def _json_safe(value: Any) -> Any:
    try:
        json.dumps(value)
    except TypeError:
        return str(value)
    return value


def _summarize_message(message: dict[str, Any]) -> dict[str, Any]:
    products = sorted(extract_product_ids(message))
    recv_ts = extract_recv_timestamps(message)
    event_ts = extract_event_timestamps(message)
    events = message.get("events") if isinstance(message.get("events"), list) else []
    return {
        "channel": message_channel(message),
        "products": products,
        "recv_ts": safe_min(recv_ts),
        "event_ts": safe_min(event_ts),
        "sequence_num": message.get("sequence_num"),
        "event_count": len(events),
        "keys": sorted(str(key) for key in message.keys())[:25],
    }


def _merge_summary(target: dict[str, Any], message: dict[str, Any]) -> None:
    target["channels"].add(message_channel(message))
    target["products"].update(extract_product_ids(message))
    target["recv_ts"].extend(extract_recv_timestamps(message))
    target["event_ts"].extend(extract_event_timestamps(message))


def _probe_jsonl(
    path: Path,
    sample_records: int,
    scan_all: bool,
    max_records: int | None,
) -> dict[str, Any]:
    accumulator: dict[str, Any] = {
        "channels": set(),
        "products": set(),
        "recv_ts": [],
        "event_ts": [],
    }
    first_records: list[dict[str, Any]] = []
    last_record: dict[str, Any] | None = None
    parse_errors: list[str] = []
    physical_lines = 0
    payload_records = 0
    read_error = None

    try:
        with open_text(path) as handle:
            for line_number, line in enumerate(handle, start=1):
                physical_lines = line_number
                if max_records is not None and line_number > max_records:
                    break
                raw_line = line.rstrip("\n")
                if not raw_line.strip():
                    continue
                try:
                    payload = json.loads(raw_line)
                except json.JSONDecodeError as exc:
                    if len(parse_errors) < 10:
                        parse_errors.append(f"line {line_number}: {exc}")
                    if not scan_all and len(first_records) >= sample_records:
                        break
                    continue
                if not isinstance(payload, dict):
                    if len(parse_errors) < 10:
                        parse_errors.append(f"line {line_number}: JSON value is not an object")
                    continue

                payload_records += 1
                _merge_summary(accumulator, payload)
                summary = _summarize_message(payload)
                if len(first_records) < sample_records:
                    first_records.append(summary)
                last_record = summary

                if not scan_all and len(first_records) >= sample_records:
                    break
    except (EOFError, OSError) as exc:
        read_error = f"{type(exc).__name__}: {exc}"

    products = sorted(accumulator["products"])
    guessed_product = guess_product_from_path(path)
    if guessed_product and guessed_product not in products:
        products.insert(0, guessed_product)

    return {
        "path": str(path.resolve()),
        "exists": path.exists(),
        "format": _file_format(path),
        "size_bytes": path.stat().st_size if path.exists() else None,
        "compression": detect_compression(path),
        "scan_all": scan_all,
        "max_records": max_records,
        "physical_lines_read": physical_lines,
        "payload_records_read": payload_records,
        "parse_error_count_sampled": len(parse_errors),
        "parse_errors": parse_errors,
        "read_error": read_error,
        "channels": sorted(accumulator["channels"]),
        "products": products,
        "first_recv_ts": safe_min(accumulator["recv_ts"]),
        "last_recv_ts": safe_max(accumulator["recv_ts"]),
        "first_event_ts": safe_min(accumulator["event_ts"]),
        "last_event_ts": safe_max(accumulator["event_ts"]),
        "first_records": first_records,
        "last_record": last_record if scan_all else None,
    }


def _row_to_message(row: dict[str, Any]) -> dict[str, Any]:
    return {str(key): _json_safe(value) for key, value in row.items()}


def _probe_table(path: Path, sample_records: int) -> dict[str, Any]:
    try:
        import pandas as pd
    except Exception as exc:  # pragma: no cover - depends on local environment.
        return {
            "path": str(path.resolve()),
            "exists": path.exists(),
            "format": _file_format(path),
            "size_bytes": path.stat().st_size if path.exists() else None,
            "read_error": f"pandas import failed: {type(exc).__name__}: {exc}",
        }

    try:
        if path.name.lower().endswith(".parquet"):
            frame = pd.read_parquet(path)
        else:
            frame = pd.read_feather(path)
    except Exception as exc:
        return {
            "path": str(path.resolve()),
            "exists": path.exists(),
            "format": _file_format(path),
            "size_bytes": path.stat().st_size if path.exists() else None,
            "read_error": f"{type(exc).__name__}: {exc}",
        }

    first_records = []
    if len(frame) > 0:
        for _, row in frame.head(sample_records).iterrows():
            first_records.append(_summarize_message(_row_to_message(row.to_dict())))
        last_record = _summarize_message(_row_to_message(frame.tail(1).iloc[0].to_dict()))
    else:
        last_record = None

    return {
        "path": str(path.resolve()),
        "exists": True,
        "format": _file_format(path),
        "size_bytes": path.stat().st_size,
        "row_count": int(len(frame)),
        "columns": [str(column) for column in frame.columns],
        "first_records": first_records,
        "last_record": last_record,
        "read_error": None,
    }


def probe_file(
    path: Path,
    sample_records: int = 2,
    scan_all: bool = False,
    max_records: int | None = None,
) -> dict[str, Any]:
    path = path.expanduser()
    if not path.exists():
        return {
            "path": str(path.resolve()),
            "exists": False,
            "format": _file_format(path),
            "read_error": "path does not exist",
        }
    if is_jsonl_path(path):
        return _probe_jsonl(path, sample_records, scan_all, max_records)
    if path.name.lower().endswith((".parquet", ".feather")):
        return _probe_table(path, sample_records)
    return {
        "path": str(path.resolve()),
        "exists": True,
        "format": _file_format(path),
        "size_bytes": path.stat().st_size,
        "read_error": "unsupported file type for read-check",
    }


def _same_stem_groups(results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    groups: dict[str, list[dict[str, Any]]] = {}
    for result in results:
        groups.setdefault(_same_stem_key(Path(str(result["path"]))), []).append(result)

    summaries = []
    for stem, items in sorted(groups.items()):
        if len(items) < 2:
            continue
        summaries.append(
            {
                "stem": stem,
                "formats": sorted(str(item.get("format")) for item in items),
                "row_counts": {
                    str(item.get("format")): item.get("row_count")
                    or item.get("payload_records_read")
                    or item.get("physical_lines_read")
                    for item in items
                },
                "read_errors": {
                    str(item.get("format")): item.get("read_error")
                    for item in items
                    if item.get("read_error")
                },
                "first_event_ts": {
                    str(item.get("format")): item.get("first_event_ts")
                    for item in items
                    if item.get("first_event_ts")
                },
                "last_event_ts": {
                    str(item.get("format")): item.get("last_event_ts")
                    for item in items
                    if item.get("last_event_ts")
                },
            }
        )
    return summaries


def run_read_check(
    paths: list[Path],
    sample_records: int = 2,
    scan_all: bool = False,
    max_records: int | None = None,
) -> dict[str, Any]:
    results = [
        probe_file(path, sample_records=sample_records, scan_all=scan_all, max_records=max_records)
        for path in paths
    ]
    return {
        "file_count": len(results),
        "scan_all": scan_all,
        "results": results,
        "same_stem_groups": _same_stem_groups(results),
    }
