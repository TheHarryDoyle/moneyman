from __future__ import annotations

import json
import os
import sys
from collections import defaultdict
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Iterable

from .coinbase import guess_product_from_path, session_id_from_path, utc_now_id
from .raw import is_jsonl_path
from .storage_audit import classify_suffix, stem_without_known_suffix


DERIVED_SUFFIXES = (".parquet", ".feather")
RAW_SUFFIXES = (".jsonl", ".jsonl.gz")


@dataclass
class TypeCoverage:
    files: int = 0
    bytes: int = 0
    exact_stem_raw_files: int = 0
    exact_stem_raw_bytes: int = 0
    previous_window_raw_files: int = 0
    previous_window_raw_bytes: int = 0
    exact_or_previous_raw_files: int = 0
    exact_or_previous_raw_bytes: int = 0
    same_directory_raw_files: int = 0
    same_directory_raw_bytes: int = 0
    no_raw_candidate_files: int = 0
    no_raw_candidate_bytes: int = 0


@dataclass
class ProductCoverage:
    raw_files: int = 0
    raw_bytes: int = 0
    derived_files: int = 0
    derived_bytes: int = 0
    first_raw_filename_time: str | None = None
    last_raw_filename_time: str | None = None
    first_derived_filename_time: str | None = None
    last_derived_filename_time: str | None = None


@dataclass(frozen=True)
class FileEntry:
    path: Path
    parent_key: str
    suffix: str
    size: int
    product: str | None
    session_id: str | None
    filename_time: datetime | None


def _iter_files(roots: Iterable[Path], progress_every: int = 50_000):
    seen = 0
    for root in roots:
        if root.is_file():
            seen += 1
            yield root
            continue
        if not root.exists():
            continue
        for dirpath, _dirnames, filenames in os.walk(root):
            base = Path(dirpath)
            for filename in filenames:
                seen += 1
                if progress_every > 0 and seen % progress_every == 0:
                    print(f"coverage scan: {seen:,} files seen", file=sys.stderr, flush=True)
                yield base / filename


def _parse_filename_time(path: Path) -> datetime | None:
    stem = stem_without_known_suffix(path)
    marker = stem[-16:]
    try:
        return datetime.strptime(marker, "%Y-%m-%d_%H-%M")
    except ValueError:
        return None


def _time_text(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.strftime("%Y-%m-%d_%H-%M")


def _update_range(product: ProductCoverage, timestamp: datetime | None, raw: bool) -> None:
    if timestamp is None:
        return
    text = _time_text(timestamp)
    if raw:
        if product.first_raw_filename_time is None or text < product.first_raw_filename_time:
            product.first_raw_filename_time = text
        if product.last_raw_filename_time is None or text > product.last_raw_filename_time:
            product.last_raw_filename_time = text
    else:
        if product.first_derived_filename_time is None or text < product.first_derived_filename_time:
            product.first_derived_filename_time = text
        if product.last_derived_filename_time is None or text > product.last_derived_filename_time:
            product.last_derived_filename_time = text


def _raw_key(entry: FileEntry, timestamp: datetime | None = None) -> tuple[str, str | None]:
    return entry.parent_key, _time_text(timestamp or entry.filename_time)


def _entry_for(path: Path) -> FileEntry:
    suffix = classify_suffix(path)
    return FileEntry(
        path=path,
        parent_key=str(path.parent.resolve()),
        suffix=suffix,
        size=path.stat().st_size,
        product=guess_product_from_path(path),
        session_id=session_id_from_path(path),
        filename_time=_parse_filename_time(path),
    )


def run_legacy_coverage(
    roots: list[Path],
    catalog_root: Path,
    roll_seconds: int = 600,
    sample_missing: int = 25,
    progress_every: int = 50_000,
) -> dict[str, object]:
    run_id = utc_now_id()
    raw_entries: list[FileEntry] = []
    derived_entries: list[FileEntry] = []
    by_type: dict[str, TypeCoverage] = defaultdict(TypeCoverage)
    by_product: dict[str, ProductCoverage] = defaultdict(ProductCoverage)
    raw_by_time: dict[tuple[str, str | None], FileEntry] = {}
    raw_dirs: set[str] = set()

    for path in _iter_files(roots, progress_every=progress_every):
        suffix = classify_suffix(path)
        if suffix not in (*RAW_SUFFIXES, *DERIVED_SUFFIXES):
            continue
        entry = _entry_for(path)
        product_key = entry.product or "<unknown>"
        product_summary = by_product[product_key]

        if is_jsonl_path(path):
            raw_entries.append(entry)
            raw_by_time[_raw_key(entry)] = entry
            raw_dirs.add(entry.parent_key)
            product_summary.raw_files += 1
            product_summary.raw_bytes += entry.size
            _update_range(product_summary, entry.filename_time, raw=True)
        elif suffix in DERIVED_SUFFIXES:
            derived_entries.append(entry)
            product_summary.derived_files += 1
            product_summary.derived_bytes += entry.size
            _update_range(product_summary, entry.filename_time, raw=False)

    previous_delta = timedelta(seconds=roll_seconds)
    missing_samples: dict[str, list[str]] = {suffix: [] for suffix in DERIVED_SUFFIXES}
    previous_window_samples: dict[str, list[dict[str, str | None]]] = {
        suffix: [] for suffix in DERIVED_SUFFIXES
    }

    for index, entry in enumerate(derived_entries, start=1):
        if progress_every > 0 and index % progress_every == 0:
            print(
                f"coverage classify: {index:,} derived files checked",
                file=sys.stderr,
                flush=True,
            )
        bucket = by_type[entry.suffix]
        bucket.files += 1
        bucket.bytes += entry.size
        exact = raw_by_time.get(_raw_key(entry))
        previous = None
        if entry.filename_time is not None:
            previous = raw_by_time.get(_raw_key(entry, entry.filename_time - previous_delta))
        has_same_dir_raw = entry.parent_key in raw_dirs

        if exact:
            bucket.exact_stem_raw_files += 1
            bucket.exact_stem_raw_bytes += entry.size
        if previous:
            bucket.previous_window_raw_files += 1
            bucket.previous_window_raw_bytes += entry.size
            if len(previous_window_samples[entry.suffix]) < sample_missing:
                previous_window_samples[entry.suffix].append(
                    {
                        "derived_path": str(entry.path.resolve()),
                        "previous_raw_path": str(previous.path.resolve()),
                    }
                )
        if exact or previous:
            bucket.exact_or_previous_raw_files += 1
            bucket.exact_or_previous_raw_bytes += entry.size
        elif has_same_dir_raw:
            bucket.same_directory_raw_files += 1
            bucket.same_directory_raw_bytes += entry.size
        else:
            bucket.no_raw_candidate_files += 1
            bucket.no_raw_candidate_bytes += entry.size
            if len(missing_samples[entry.suffix]) < sample_missing:
                missing_samples[entry.suffix].append(str(entry.path.resolve()))

    report = {
        "run_id": run_id,
        "roots": [str(root.resolve()) for root in roots],
        "roll_seconds": roll_seconds,
        "raw_files": len(raw_entries),
        "derived_files": len(derived_entries),
        "coverage_by_derived_type": {
            key: asdict(value) for key, value in sorted(by_type.items())
        },
        "coverage_by_product": {
            key: asdict(value) for key, value in sorted(by_product.items())
        },
        "previous_window_samples": previous_window_samples,
        "missing_raw_candidate_samples": missing_samples,
        "interpretation": {
            "source_of_truth": "Raw .jsonl and .jsonl.gz files remain the source of truth.",
            "old_logger_shift": (
                "For the stable gzip logger, a derived Parquet/Feather file named with time T "
                "often corresponds to the raw JSONL window at T minus the roll interval."
            ),
            "cleanup_rule": (
                "Do not delete derived files from this report alone. Use previous_window_raw_files "
                "as a stronger cleanup signal than exact_stem_raw_files, then verify samples."
            ),
        },
    }

    output_dir = catalog_root / "coverage"
    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / f"legacy_coverage_{run_id}.json"
    output_path.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
    report["output_path"] = str(output_path.resolve())
    return report
