from __future__ import annotations

import json
import os
import sys
from collections import Counter
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Iterable

from .coinbase import guess_product_from_path, session_id_from_path, utc_now_id
from .raw import is_jsonl_path
from .storage_audit import classify_suffix, stem_without_known_suffix


@dataclass(frozen=True)
class CleanupEntry:
    path: Path
    size: int
    parent_key: str
    product: str | None
    session_id: str | None
    filename_time: datetime | None


@dataclass(frozen=True)
class FeatherCleanupSummary:
    run_id: str
    mode: str
    roots: list[str]
    catalog_manifest_path: str
    coverage_required: str
    feather_files_seen: int
    eligible_files: int
    eligible_bytes: int
    deleted_files: int
    deleted_bytes: int
    skipped_files: int
    skipped_bytes: int
    failed_files: int
    failed_bytes: int
    coverage_counts: dict[str, int]
    coverage_bytes: dict[str, int]


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
                    print(f"cleanup scan: {seen:,} files seen", file=sys.stderr, flush=True)
                yield base / filename


def _parse_filename_time(path: Path) -> datetime | None:
    marker = stem_without_known_suffix(path)[-16:]
    try:
        return datetime.strptime(marker, "%Y-%m-%d_%H-%M")
    except ValueError:
        return None


def _time_text(value: datetime | None) -> str | None:
    return value.strftime("%Y-%m-%d_%H-%M") if value else None


def _entry(path: Path) -> CleanupEntry:
    return CleanupEntry(
        path=path,
        size=path.stat().st_size,
        parent_key=str(path.parent.resolve()),
        product=guess_product_from_path(path),
        session_id=session_id_from_path(path),
        filename_time=_parse_filename_time(path),
    )


def _raw_key(entry: CleanupEntry, timestamp: datetime | None = None) -> tuple[str, str | None]:
    return entry.parent_key, _time_text(timestamp or entry.filename_time)


def _is_under_roots(path: Path, roots: list[Path]) -> bool:
    resolved = path.resolve()
    for root in roots:
        try:
            resolved.relative_to(root.resolve())
            return True
        except ValueError:
            continue
    return False


def _is_eligible(kind: str, coverage_required: str) -> bool:
    if coverage_required == "none":
        return True
    if coverage_required == "exact-or-previous":
        return kind in {"exact_stem_raw", "previous_window_raw"}
    if coverage_required == "any-raw-candidate":
        return kind != "no_raw_candidate"
    raise ValueError("coverage_required must be none, exact-or-previous, or any-raw-candidate")


def run_feather_cleanup(
    roots: list[Path],
    catalog_root: Path,
    mode: str = "plan",
    coverage_required: str = "any-raw-candidate",
    roll_seconds: int = 600,
    progress_every: int = 50_000,
) -> dict[str, object]:
    if mode not in {"plan", "delete"}:
        raise ValueError("mode must be one of: plan, delete")
    if coverage_required not in {"none", "exact-or-previous", "any-raw-candidate"}:
        raise ValueError("coverage_required must be none, exact-or-previous, or any-raw-candidate")

    run_id = utc_now_id()
    manifest_dir = catalog_root / "cleanup"
    manifest_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = manifest_dir / f"feather_cleanup_{run_id}.jsonl"
    root_resolved = [root.resolve() for root in roots]

    raw_by_time: dict[tuple[str, str | None], CleanupEntry] = {}
    raw_dirs: set[str] = set()
    feathers: list[CleanupEntry] = []

    for path in _iter_files(roots, progress_every=progress_every):
        suffix = classify_suffix(path)
        if is_jsonl_path(path):
            raw = _entry(path)
            raw_by_time[_raw_key(raw)] = raw
            raw_dirs.add(raw.parent_key)
        elif suffix == ".feather":
            feathers.append(_entry(path))

    previous_delta = timedelta(seconds=roll_seconds)
    coverage_counts: Counter[str] = Counter()
    coverage_bytes: Counter[str] = Counter()
    eligible_files = 0
    eligible_bytes = 0
    deleted_files = 0
    deleted_bytes = 0
    skipped_files = 0
    skipped_bytes = 0
    failed_files = 0
    failed_bytes = 0

    with manifest_path.open("wt", encoding="utf-8", newline="\n") as manifest:
        for index, feather in enumerate(feathers, start=1):
            if progress_every > 0 and index % progress_every == 0:
                print(f"cleanup classify: {index:,} feather files checked", file=sys.stderr, flush=True)
            exact = raw_by_time.get(_raw_key(feather))
            previous = None
            if feather.filename_time is not None:
                previous = raw_by_time.get(_raw_key(feather, feather.filename_time - previous_delta))
            if previous:
                coverage_kind = "previous_window_raw"
                raw_candidate = previous.path
            elif exact:
                coverage_kind = "exact_stem_raw"
                raw_candidate = exact.path
            elif feather.parent_key in raw_dirs:
                coverage_kind = "same_directory_raw"
                raw_candidate = None
            else:
                coverage_kind = "no_raw_candidate"
                raw_candidate = None

            eligible = _is_eligible(coverage_kind, coverage_required)
            action = "planned_delete" if eligible else "skipped_coverage"
            error = None
            if eligible:
                eligible_files += 1
                eligible_bytes += feather.size
            else:
                skipped_files += 1
                skipped_bytes += feather.size

            if mode == "delete" and eligible:
                try:
                    if classify_suffix(feather.path) != ".feather":
                        raise OSError("refusing to delete a non-Feather path")
                    if not _is_under_roots(feather.path, root_resolved):
                        raise OSError("refusing to delete outside requested cleanup roots")
                    feather.path.unlink()
                    action = "deleted"
                    deleted_files += 1
                    deleted_bytes += feather.size
                except Exception as exc:  # noqa: BLE001 - manifest needs the file-level reason.
                    action = "failed"
                    error = str(exc)
                    failed_files += 1
                    failed_bytes += feather.size

            coverage_counts[coverage_kind] += 1
            coverage_bytes[coverage_kind] += feather.size
            manifest.write(
                json.dumps(
                    {
                        "run_id": run_id,
                        "mode": mode,
                        "path": str(feather.path.resolve()),
                        "size_bytes": feather.size,
                        "product": feather.product,
                        "session_id": feather.session_id,
                        "filename_time": _time_text(feather.filename_time),
                        "coverage_kind": coverage_kind,
                        "coverage_required": coverage_required,
                        "eligible": eligible,
                        "raw_candidate_path": str(raw_candidate.resolve()) if raw_candidate else None,
                        "action": action,
                        "error": error,
                    },
                    sort_keys=True,
                )
                + "\n"
            )

    summary = FeatherCleanupSummary(
        run_id=run_id,
        mode=mode,
        roots=[str(root) for root in root_resolved],
        catalog_manifest_path=str(manifest_path.resolve()),
        coverage_required=coverage_required,
        feather_files_seen=len(feathers),
        eligible_files=eligible_files,
        eligible_bytes=eligible_bytes,
        deleted_files=deleted_files,
        deleted_bytes=deleted_bytes,
        skipped_files=skipped_files,
        skipped_bytes=skipped_bytes,
        failed_files=failed_files,
        failed_bytes=failed_bytes,
        coverage_counts=dict(sorted(coverage_counts.items())),
        coverage_bytes=dict(sorted(coverage_bytes.items())),
    )
    return asdict(summary)
