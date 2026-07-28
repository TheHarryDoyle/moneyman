from __future__ import annotations

import json
import os
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from .coinbase import utc_now_id
from .inventory import discover_legacy_ws_folders


RAW_SUFFIXES = (".jsonl", ".jsonl.gz")
METADATA_NAMES = {"session_start.txt", "manifest.json"}


@dataclass(frozen=True)
class CentralizeSummary:
    run_id: str
    raw_root: str
    catalog_manifest_path: str
    mode: str
    files_seen: int
    files_linked: int
    files_copied: int
    files_moved: int
    files_skipped_existing: int
    files_failed: int
    bytes_seen: int


def is_raw_or_session_metadata(path: Path) -> bool:
    name = path.name.lower()
    return name.endswith(RAW_SUFFIXES) or name in METADATA_NAMES


def iter_legacy_files(legacy_folders: Iterable[Path]) -> Iterable[tuple[Path, Path]]:
    for folder in legacy_folders:
        for path in folder.rglob("*"):
            if path.is_file() and is_raw_or_session_metadata(path):
                yield folder, path


def destination_for(raw_root: Path, folder: Path, source: Path) -> Path:
    relative = source.relative_to(folder)
    return raw_root / "legacy_ws_data" / folder.name / relative


def _same_drive(left: Path, right: Path) -> bool:
    return left.resolve().drive.lower() == right.resolve().drive.lower()


def _same_file_content_identity(source: Path, dest: Path) -> bool:
    if not dest.exists():
        return False
    try:
        return os.path.samefile(source, dest)
    except OSError:
        return source.stat().st_size == dest.stat().st_size


def centralize_legacy_ws_data(
    legacy_search_roots: list[Path],
    raw_root: Path,
    catalog_root: Path,
    mode: str = "plan",
    max_files: int | None = None,
    progress_every: int = 10_000,
) -> CentralizeSummary:
    if mode not in {"hardlink", "copy", "move", "plan"}:
        raise ValueError("mode must be one of: hardlink, copy, move, plan")

    run_id = utc_now_id()
    manifest_dir = catalog_root / "centralize"
    manifest_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = manifest_dir / f"centralize_{run_id}.jsonl"
    legacy_folders = discover_legacy_ws_folders(legacy_search_roots)

    files_seen = 0
    files_linked = 0
    files_copied = 0
    files_moved = 0
    files_skipped_existing = 0
    files_failed = 0
    bytes_seen = 0

    raw_root.mkdir(parents=True, exist_ok=True)
    with manifest_path.open("wt", encoding="utf-8", newline="\n") as manifest:
        for folder, source in iter_legacy_files(legacy_folders):
            files_seen += 1
            if max_files is not None and files_seen > max_files:
                break

            size = source.stat().st_size
            bytes_seen += size
            dest = destination_for(raw_root, folder, source)
            action = "planned"
            error = None

            try:
                if dest.exists():
                    action = "skipped_existing"
                    files_skipped_existing += 1
                elif mode == "plan":
                    action = "planned"
                else:
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    if mode == "hardlink":
                        if not _same_drive(source, dest):
                            raise OSError("hardlink requires source and destination on the same drive")
                        os.link(source, dest)
                        if not _same_file_content_identity(source, dest):
                            raise OSError("created hardlink could not be verified")
                        action = "hardlinked"
                        files_linked += 1
                    elif mode == "move":
                        if not _same_drive(source, dest):
                            raise OSError("move mode requires source and destination on the same drive")
                        source.rename(dest)
                        action = "moved"
                        files_moved += 1
                    else:
                        shutil.copy2(source, dest)
                        action = "copied"
                        files_copied += 1
            except Exception as exc:  # noqa: BLE001 - manifest should record any file-level issue.
                action = "failed"
                error = str(exc)
                files_failed += 1

            manifest.write(
                json.dumps(
                    {
                        "run_id": run_id,
                        "source_path": str(source.resolve()),
                        "destination_path": str(dest.resolve()),
                        "source_size_bytes": size,
                        "legacy_folder": str(folder.resolve()),
                        "mode": mode,
                        "action": action,
                        "error": error,
                    },
                    sort_keys=True,
                )
                + "\n"
            )

            if progress_every and files_seen % progress_every == 0:
                print(
                    f"centralize: {files_seen} files seen, {files_moved} moved, "
                    f"{files_linked} linked, {files_copied} copied, "
                    f"{files_skipped_existing} existing, {files_failed} failed",
                    file=sys.stderr,
                    flush=True,
                )

    return CentralizeSummary(
        run_id=run_id,
        raw_root=str(raw_root.resolve()),
        catalog_manifest_path=str(manifest_path.resolve()),
        mode=mode,
        files_seen=files_seen if max_files is None else min(files_seen, max_files),
        files_linked=files_linked,
        files_copied=files_copied,
        files_moved=files_moved,
        files_skipped_existing=files_skipped_existing,
        files_failed=files_failed,
        bytes_seen=bytes_seen,
    )
