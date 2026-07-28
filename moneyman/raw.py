from __future__ import annotations

import gzip
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator, TextIO


@dataclass(frozen=True)
class RawRecord:
    source_path: Path
    line_number: int
    raw_line: str
    payload: dict[str, Any] | None = None
    error: str | None = None


def detect_compression(path: Path) -> str:
    return "gzip" if path.name.lower().endswith(".gz") else "none"


def is_jsonl_path(path: Path) -> bool:
    name = path.name.lower()
    return name.endswith(".jsonl") or name.endswith(".jsonl.gz")


def open_text(path: Path) -> TextIO:
    if detect_compression(path) == "gzip":
        return gzip.open(path, "rt", encoding="utf-8")
    return path.open("rt", encoding="utf-8")


def iter_jsonl(path: Path, limit: int | None = None) -> Iterator[RawRecord]:
    emitted = 0
    with open_text(path) as handle:
        for line_number, line in enumerate(handle, start=1):
            if limit is not None and emitted >= limit:
                break
            raw_line = line.rstrip("\n")
            if not raw_line.strip():
                continue
            try:
                payload = json.loads(raw_line)
            except json.JSONDecodeError as exc:
                yield RawRecord(path, line_number, raw_line, None, str(exc))
            else:
                if isinstance(payload, dict):
                    yield RawRecord(path, line_number, raw_line, payload, None)
                else:
                    yield RawRecord(path, line_number, raw_line, None, "JSON value is not an object")
            emitted += 1


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("wt", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(json.dumps(row, sort_keys=True, default=str) + "\n")


def append_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("at", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(json.dumps(row, sort_keys=True, default=str) + "\n")


def read_jsonl_dicts(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for record in iter_jsonl(path):
        if record.payload is not None:
            rows.append(record.payload)
    return rows
