from __future__ import annotations

import json
import os
import sys
from collections import defaultdict
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable

from .coinbase import (
    extract_event_timestamps,
    extract_product_ids,
    extract_recv_timestamps,
    guess_product_from_path,
    message_channel,
    session_id_from_path,
    utc_now_id,
)
from .raw import is_jsonl_path, open_text, write_jsonl
from .storage_audit import stem_without_known_suffix


@dataclass(frozen=True)
class RawFileBoundary:
    path: str
    size_bytes: int
    product: str | None
    session_id: str | None
    filename_time: str | None
    first_recv_ts: str | None = None
    last_recv_ts: str | None = None
    first_event_ts: str | None = None
    last_event_ts: str | None = None
    first_channel: str | None = None
    last_channel: str | None = None
    physical_lines: int | None = None
    payload_records: int | None = None
    parse_errors: int = 0
    read_error: str | None = None


@dataclass(frozen=True)
class CoverageInterval:
    product: str
    start: datetime
    end: datetime
    source_path: str
    session_id: str | None


@dataclass(frozen=True)
class Gap:
    product: str
    gap_start: str
    gap_end: str
    gap_seconds: float
    previous_path: str
    next_path: str
    previous_session_id: str | None
    next_session_id: str | None


def _iter_raw_files(
    roots: Iterable[Path],
    product_filter: set[str] | None,
    max_files: int | None,
    progress_every: int,
):
    emitted = 0
    seen = 0
    for root in roots:
        candidates: Iterable[Path]
        if root.is_file():
            candidates = [root]
        elif root.exists():
            candidates = (
                Path(dirpath) / filename
                for dirpath, _dirnames, filenames in os.walk(root)
                for filename in filenames
            )
        else:
            continue

        for path in candidates:
            seen += 1
            if progress_every > 0 and seen % progress_every == 0:
                print(f"raw gap scan: {seen:,} files seen", file=sys.stderr, flush=True)
            if not is_jsonl_path(path):
                continue
            guessed_product = guess_product_from_path(path)
            if product_filter and guessed_product not in product_filter:
                continue
            yield path
            emitted += 1
            if max_files is not None and emitted >= max_files:
                return


def _parse_filename_time(path: Path) -> datetime | None:
    stem = stem_without_known_suffix(path)
    marker = stem[-16:]
    try:
        return datetime.strptime(marker, "%Y-%m-%d_%H-%M").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _parse_timestamp(value: str | None) -> datetime | None:
    if not value:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        if text.replace(".", "", 1).isdigit():
            return datetime.fromtimestamp(float(text), tz=timezone.utc)
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except (ValueError, OSError, OverflowError):
        return None


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _min_dt(values: Iterable[datetime | None]) -> datetime | None:
    concrete = [value for value in values if value is not None]
    return min(concrete) if concrete else None


def _max_dt(values: Iterable[datetime | None]) -> datetime | None:
    concrete = [value for value in values if value is not None]
    return max(concrete) if concrete else None


def filename_boundary(path: Path) -> RawFileBoundary:
    filename_time = _parse_filename_time(path)
    return RawFileBoundary(
        path=str(path.resolve()),
        size_bytes=path.stat().st_size,
        product=guess_product_from_path(path),
        session_id=session_id_from_path(path),
        filename_time=_iso(filename_time),
    )


def content_boundary(path: Path) -> RawFileBoundary:
    filename_time = _parse_filename_time(path)
    recv_times: list[datetime] = []
    event_times: list[datetime] = []
    first_channel = None
    last_channel = None
    product = guess_product_from_path(path)
    products: set[str] = set()
    physical_lines = 0
    payload_records = 0
    parse_errors = 0
    read_error = None

    try:
        with open_text(path) as handle:
            for line_number, line in enumerate(handle, start=1):
                physical_lines = line_number
                if not line.strip():
                    continue
                try:
                    payload = json.loads(line)
                except json.JSONDecodeError:
                    parse_errors += 1
                    continue
                if not isinstance(payload, dict):
                    parse_errors += 1
                    continue

                payload_records += 1
                channel = message_channel(payload)
                if first_channel is None:
                    first_channel = channel
                last_channel = channel
                products.update(extract_product_ids(payload))
                recv_times.extend(
                    parsed
                    for parsed in (_parse_timestamp(value) for value in extract_recv_timestamps(payload))
                    if parsed is not None
                )
                event_times.extend(
                    parsed
                    for parsed in (_parse_timestamp(value) for value in extract_event_timestamps(payload))
                    if parsed is not None
                )
    except (EOFError, OSError) as exc:
        read_error = f"{type(exc).__name__}: {exc}"

    if products:
        product = sorted(products)[0]

    return RawFileBoundary(
        path=str(path.resolve()),
        size_bytes=path.stat().st_size,
        product=product,
        session_id=session_id_from_path(path),
        filename_time=_iso(filename_time),
        first_recv_ts=_iso(_min_dt(recv_times)),
        last_recv_ts=_iso(_max_dt(recv_times)),
        first_event_ts=_iso(_min_dt(event_times)),
        last_event_ts=_iso(_max_dt(event_times)),
        first_channel=first_channel,
        last_channel=last_channel,
        physical_lines=physical_lines,
        payload_records=payload_records,
        parse_errors=parse_errors,
        read_error=read_error,
    )


def _interval_from_boundary(
    boundary: RawFileBoundary,
    mode: str,
    roll_seconds: int,
) -> CoverageInterval | None:
    product = boundary.product
    if not product:
        return None

    if mode == "filename":
        start = _parse_timestamp(boundary.filename_time)
        if start is None:
            return None
        end = start + timedelta(seconds=roll_seconds)
    else:
        start = _parse_timestamp(boundary.first_recv_ts) or _parse_timestamp(boundary.first_event_ts)
        end = _parse_timestamp(boundary.last_recv_ts) or _parse_timestamp(boundary.last_event_ts)
        if start is None or end is None:
            return None
        if end < start:
            start, end = end, start

    return CoverageInterval(
        product=product,
        start=start,
        end=end,
        source_path=boundary.path,
        session_id=boundary.session_id,
    )


def _find_gaps(
    intervals: list[CoverageInterval],
    tolerance_seconds: int,
    max_gaps: int,
) -> tuple[dict[str, object], list[Gap]]:
    by_product: dict[str, list[CoverageInterval]] = defaultdict(list)
    for interval in intervals:
        by_product[interval.product].append(interval)

    summaries: dict[str, object] = {}
    gaps: list[Gap] = []
    tolerance = timedelta(seconds=tolerance_seconds)

    for product, product_intervals in sorted(by_product.items()):
        ordered = sorted(product_intervals, key=lambda item: (item.start, item.end, item.source_path))
        if not ordered:
            continue

        merged_count = 0
        gap_count = 0
        total_gap_seconds = 0.0
        largest_gap_seconds = 0.0
        current_start = ordered[0].start
        current_end = ordered[0].end
        current_path = ordered[0].source_path
        current_session = ordered[0].session_id

        for interval in ordered[1:]:
            if interval.start <= current_end + tolerance:
                if interval.end > current_end:
                    current_end = interval.end
                    current_path = interval.source_path
                    current_session = interval.session_id
                continue

            gap_seconds = (interval.start - current_end).total_seconds()
            gap_count += 1
            total_gap_seconds += gap_seconds
            largest_gap_seconds = max(largest_gap_seconds, gap_seconds)
            if len(gaps) < max_gaps:
                gaps.append(
                    Gap(
                        product=product,
                        gap_start=_iso(current_end) or "",
                        gap_end=_iso(interval.start) or "",
                        gap_seconds=gap_seconds,
                        previous_path=current_path,
                        next_path=interval.source_path,
                        previous_session_id=current_session,
                        next_session_id=interval.session_id,
                    )
                )

            merged_count += 1
            current_start = interval.start
            current_end = interval.end
            current_path = interval.source_path
            current_session = interval.session_id

        merged_count += 1
        summaries[product] = {
            "raw_intervals": len(ordered),
            "merged_coverage_intervals": merged_count,
            "gap_count": gap_count,
            "first_coverage_start": _iso(ordered[0].start),
            "last_coverage_end": _iso(max(item.end for item in ordered)),
            "total_gap_seconds": total_gap_seconds,
            "largest_gap_seconds": largest_gap_seconds,
        }

    gaps.sort(key=lambda gap: gap.gap_seconds, reverse=True)
    return summaries, gaps[:max_gaps]


def run_raw_gaps(
    roots: list[Path],
    catalog_root: Path,
    mode: str = "filename",
    products: list[str] | None = None,
    roll_seconds: int = 600,
    tolerance_seconds: int = 90,
    max_files: int | None = None,
    max_gaps: int = 1000,
    progress_every: int = 10_000,
) -> dict[str, object]:
    run_id = utc_now_id()
    product_filter = {product.upper() for product in products} if products else None
    boundary_func = content_boundary if mode == "content" else filename_boundary

    boundaries: list[RawFileBoundary] = []
    intervals: list[CoverageInterval] = []
    skipped_without_interval = 0

    for index, path in enumerate(
        _iter_raw_files(roots, product_filter, max_files=max_files, progress_every=progress_every),
        start=1,
    ):
        if progress_every > 0 and index % progress_every == 0:
            print(f"raw gap analyze: {index:,} raw files checked", file=sys.stderr, flush=True)
        boundary = boundary_func(path)
        boundaries.append(boundary)
        interval = _interval_from_boundary(boundary, mode, roll_seconds)
        if interval is None:
            skipped_without_interval += 1
        else:
            intervals.append(interval)

    product_summaries, gaps = _find_gaps(intervals, tolerance_seconds, max_gaps=max_gaps)

    report = {
        "run_id": run_id,
        "mode": mode,
        "roots": [str(root.resolve()) for root in roots],
        "products": sorted(product_filter) if product_filter else None,
        "roll_seconds": roll_seconds,
        "tolerance_seconds": tolerance_seconds,
        "raw_files_checked": len(boundaries),
        "intervals_used": len(intervals),
        "skipped_without_interval": skipped_without_interval,
        "product_summaries": product_summaries,
        "gaps": [asdict(gap) for gap in gaps],
        "gap_count_returned": len(gaps),
        "interpretation": {
            "filename_mode": (
                "Uses file timestamp windows only. It is fast and useful for outage gaps, "
                "but it does not prove every file contains records through its expected end."
            ),
            "content_mode": (
                "Reads each raw JSONL/JSONL.GZ file to extract first/last receive or event time. "
                "It is the stronger check, but it can take a long time on the full archive."
            ),
        },
    }

    output_dir = catalog_root / "gaps"
    output_dir.mkdir(parents=True, exist_ok=True)
    report_path = output_dir / f"raw_gaps_{mode}_{run_id}.json"
    report["output_path"] = str(report_path.resolve())

    if mode == "content":
        boundaries_path = output_dir / f"raw_file_boundaries_{run_id}.jsonl"
        write_jsonl(boundaries_path, [asdict(boundary) for boundary in boundaries])
        report["boundaries_path"] = str(boundaries_path.resolve())

    report_path.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
    return report
