from __future__ import annotations

import json
import os
from collections import defaultdict
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable

from .coinbase import utc_now_id


RAW_SUFFIXES = (".jsonl", ".jsonl.gz")
DERIVED_SUFFIXES = (".parquet", ".feather")


@dataclass
class TypeSummary:
    files: int = 0
    bytes: int = 0


@dataclass
class DerivedCoverage:
    files: int = 0
    bytes: int = 0
    with_same_stem_raw: int = 0
    with_same_stem_raw_bytes: int = 0
    without_same_stem_raw: int = 0
    without_same_stem_raw_bytes: int = 0


def classify_suffix(path: Path) -> str:
    name = path.name.lower()
    if name.endswith(".jsonl.gz"):
        return ".jsonl.gz"
    for suffix in (".jsonl", ".parquet", ".feather", ".csv", ".py", ".txt"):
        if name.endswith(suffix):
            return suffix
    return path.suffix.lower() or "<none>"


def stem_without_known_suffix(path: Path) -> str:
    name = path.name
    lower = name.lower()
    for suffix in (".jsonl.gz", ".jsonl", ".parquet", ".feather"):
        if lower.endswith(suffix):
            return name[: -len(suffix)]
    return path.stem


def iter_files(roots: Iterable[Path]):
    for root in roots:
        if root.is_file():
            yield root
        elif root.exists():
            for dirpath, _dirnames, filenames in os.walk(root):
                base = Path(dirpath)
                for filename in filenames:
                    yield base / filename


def run_storage_audit(
    roots: list[Path],
    catalog_root: Path,
    sample_missing: int = 25,
) -> dict[str, object]:
    run_id = utc_now_id()
    by_type: dict[str, TypeSummary] = defaultdict(TypeSummary)
    raw_keys: set[str] = set()
    derived: list[tuple[str, str, int, str]] = []

    for path in iter_files(roots):
        suffix = classify_suffix(path)
        size = path.stat().st_size
        by_type[suffix].files += 1
        by_type[suffix].bytes += size
        if suffix in RAW_SUFFIXES:
            raw_keys.add(f"{path.parent}|{stem_without_known_suffix(path)}")
        elif suffix in DERIVED_SUFFIXES:
            derived.append((suffix, f"{path.parent}|{stem_without_known_suffix(path)}", size, str(path.resolve())))

    coverage: dict[str, DerivedCoverage] = defaultdict(DerivedCoverage)
    missing_samples: dict[str, list[str]] = {".parquet": [], ".feather": []}
    for suffix, key, size, source_path in derived:
        bucket = coverage[suffix]
        bucket.files += 1
        bucket.bytes += size
        if key in raw_keys:
            bucket.with_same_stem_raw += 1
            bucket.with_same_stem_raw_bytes += size
        else:
            bucket.without_same_stem_raw += 1
            bucket.without_same_stem_raw_bytes += size
            if len(missing_samples[suffix]) < sample_missing:
                missing_samples[suffix].append(source_path)

    report = {
        "run_id": run_id,
        "roots": [str(root.resolve()) for root in roots],
        "by_type": {key: asdict(value) for key, value in sorted(by_type.items())},
        "derived_same_stem_raw_coverage": {
            key: asdict(value) for key, value in sorted(coverage.items())
        },
        "missing_same_stem_raw_samples": missing_samples,
        "interpretation": {
            "raw_source_of_truth": [".jsonl", ".jsonl.gz"],
            "candidate_rebuildable_derived": [".parquet", ".feather"],
            "note": (
                "Same-stem raw coverage is a filename-level audit, not a row-level checksum. "
                "The old stable logger can write Parquet/Feather with the roll timestamp, "
                "so a derived file may correspond to the previous raw JSONL window rather than "
                "the same filename stem."
            ),
        },
    }

    output_dir = catalog_root / "storage"
    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / f"storage_audit_{run_id}.json"
    output_path.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
    report["output_path"] = str(output_path.resolve())
    return report
