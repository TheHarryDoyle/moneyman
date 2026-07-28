from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable

from .coinbase import utc_now_id


@dataclass(frozen=True)
class SessionMoveSummary:
    run_id: str
    raw_root: str
    catalog_manifest_path: str
    mode: str
    sessions_seen: int
    sessions_planned: int
    sessions_moved: int
    sessions_skipped_open: int
    sessions_skipped_existing: int
    sessions_skipped_same_root: int
    sessions_failed: int
    files_seen: int
    bytes_seen: int


def _same_drive(left: Path, right: Path) -> bool:
    return left.resolve().drive.lower() == right.resolve().drive.lower()


def _is_relative_to(child: Path, parent: Path) -> bool:
    try:
        child.resolve().relative_to(parent.resolve())
    except ValueError:
        return False
    return True


def _session_state(session_dir: Path) -> str:
    manifest_path = session_dir / "manifest.json"
    if not manifest_path.exists():
        return "no_manifest"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return "invalid_manifest"
    status = manifest.get("status")
    if status is not None:
        clean_close = (
            status == "closed"
            and bool(manifest.get("end_ts"))
            and bool(manifest.get("session_end", {}).get("all_writers_closed"))
        )
        return "closed" if clean_close else "open"
    # Legacy manifests predate the explicit close contract. Preserve their
    # original end_ts rule while new audited sessions fail closed.
    return "closed" if manifest.get("end_ts") else "open"


def _session_size(session_dir: Path) -> tuple[int, int]:
    files = 0
    bytes_seen = 0
    for path in session_dir.rglob("*"):
        if path.is_file():
            files += 1
            bytes_seen += path.stat().st_size
    return files, bytes_seen


def discover_stranded_sessions(source_raw_roots: Iterable[Path]) -> list[Path]:
    sessions: list[Path] = []
    seen: set[Path] = set()
    for source_raw_root in source_raw_roots:
        source_base = source_raw_root / "coinbase_advanced_trade"
        if not source_base.exists():
            continue
        for session_dir in sorted(source_base.glob("session=*")):
            resolved = session_dir.resolve()
            if session_dir.is_dir() and resolved not in seen:
                sessions.append(session_dir)
                seen.add(resolved)
    return sessions


def move_stranded_coinbase_sessions(
    source_raw_roots: list[Path],
    raw_root: Path,
    catalog_root: Path,
    mode: str = "plan",
    include_open_sessions: bool = False,
) -> SessionMoveSummary:
    if mode not in {"plan", "move"}:
        raise ValueError("mode must be one of: plan, move")

    run_id = utc_now_id()
    manifest_dir = catalog_root / "relocate"
    manifest_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = manifest_dir / f"move_stranded_sessions_{run_id}.jsonl"

    sessions_seen = 0
    sessions_planned = 0
    sessions_moved = 0
    sessions_skipped_open = 0
    sessions_skipped_existing = 0
    sessions_skipped_same_root = 0
    sessions_failed = 0
    files_seen = 0
    bytes_seen = 0

    raw_root.mkdir(parents=True, exist_ok=True)
    sessions = discover_stranded_sessions(source_raw_roots)

    with manifest_path.open("wt", encoding="utf-8", newline="\n") as manifest:
        for session_dir in sessions:
            sessions_seen += 1
            file_count, byte_count = _session_size(session_dir)
            files_seen += file_count
            bytes_seen += byte_count

            destination = raw_root / "coinbase_advanced_trade" / session_dir.name
            state = _session_state(session_dir)
            action = "planned"
            error = None

            try:
                if _is_relative_to(session_dir, raw_root):
                    action = "skipped_same_root"
                    sessions_skipped_same_root += 1
                elif destination.exists():
                    action = "skipped_existing"
                    sessions_skipped_existing += 1
                elif state == "open" and not include_open_sessions:
                    action = "skipped_open"
                    sessions_skipped_open += 1
                elif mode == "plan":
                    action = "planned"
                    sessions_planned += 1
                else:
                    if not _same_drive(session_dir, destination):
                        raise OSError("move mode requires source and destination on the same drive")
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    session_dir.rename(destination)
                    action = "moved"
                    sessions_moved += 1
            except Exception as exc:  # noqa: BLE001 - record every session-level failure.
                action = "failed"
                error = str(exc)
                sessions_failed += 1

            manifest.write(
                json.dumps(
                    {
                        "run_id": run_id,
                        "source_session": str(session_dir.resolve()),
                        "destination_session": str(destination.resolve()),
                        "session_state": state,
                        "file_count": file_count,
                        "bytes_seen": byte_count,
                        "mode": mode,
                        "action": action,
                        "error": error,
                    },
                    sort_keys=True,
                )
                + "\n"
            )

    return SessionMoveSummary(
        run_id=run_id,
        raw_root=str(raw_root.resolve()),
        catalog_manifest_path=str(manifest_path.resolve()),
        mode=mode,
        sessions_seen=sessions_seen,
        sessions_planned=sessions_planned,
        sessions_moved=sessions_moved,
        sessions_skipped_open=sessions_skipped_open,
        sessions_skipped_existing=sessions_skipped_existing,
        sessions_skipped_same_root=sessions_skipped_same_root,
        sessions_failed=sessions_failed,
        files_seen=files_seen,
        bytes_seen=bytes_seen,
    )
