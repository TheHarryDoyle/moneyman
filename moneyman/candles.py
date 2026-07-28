from __future__ import annotations

import csv
import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from .coinbase import normalize_product_id, utc_now_id


REQUIRED_CANDLE_COLUMNS = ("time", "open", "high", "low", "close", "volume")


@dataclass
class CandleImportCounts:
    rows_seen: int = 0
    rows_written: int = 0
    rows_failed: int = 0


@dataclass(frozen=True)
class ExternalOhlcvMoveSummary:
    run_id: str
    raw_root: str
    catalog_manifest_path: str
    mode: str
    files_seen: int
    files_planned: int
    files_moved: int
    files_missing: int
    files_skipped_existing: int
    files_skipped_same_root: int
    files_failed: int
    bytes_seen: int


COINBASE_EXCHANGE_CANDLES_URL = "https://api.exchange.coinbase.com/products/{product_id}/candles"
EXTERNAL_OHLCV_LIMITATION = "OHLCV candles do not contain L2 spread, depth, queue, or imbalance."


def _normalize_time(value: str) -> str:
    text = value.strip()
    if not text:
        raise ValueError("missing time")
    dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _iso_z(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_utc_datetime(value: str) -> datetime:
    text = value.strip()
    if not text:
        raise ValueError("missing datetime")
    if text.isdigit():
        return datetime.fromtimestamp(int(text), tz=timezone.utc)
    parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _timeframe(granularity_seconds: int) -> str:
    if granularity_seconds == 60:
        return "1m"
    if granularity_seconds % 3600 == 0:
        return f"{granularity_seconds // 3600}h"
    if granularity_seconds % 60 == 0:
        return f"{granularity_seconds // 60}m"
    return f"{granularity_seconds}s"


def _require(row: dict[str, str], name: str) -> str:
    value = row.get(name)
    if value is None or str(value).strip() == "":
        raise ValueError(f"missing {name}")
    return str(value).strip()


def _candle_row(
    row: dict[str, str],
    source_path: Path,
    source_line: int,
    product_id: str,
    provider: str,
) -> dict[str, Any]:
    return {
        "start_ts": _normalize_time(_require(row, "time")),
        "product_id": product_id,
        "open": _require(row, "open"),
        "high": _require(row, "high"),
        "low": _require(row, "low"),
        "close": _require(row, "close"),
        "volume": _require(row, "volume"),
        "timeframe": "1m",
        "source_kind": "price_only_fallback",
        "source_provider": provider,
        "source_path": str(source_path.resolve()),
        "source_line": source_line,
        "limitations": EXTERNAL_OHLCV_LIMITATION,
    }


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _is_relative_to(child: Path, parent: Path) -> bool:
    try:
        child.resolve().relative_to(parent.resolve())
    except ValueError:
        return False
    return True


def _same_drive(left: Path, right: Path) -> bool:
    return left.resolve().drive.lower() == right.resolve().drive.lower()


def move_external_ohlcv_files(
    inputs: list[Path],
    product: str,
    provider: str,
    raw_root: Path,
    catalog_root: Path,
    mode: str = "plan",
) -> ExternalOhlcvMoveSummary:
    if mode not in {"plan", "move"}:
        raise ValueError("mode must be one of: plan, move")
    product_id = normalize_product_id(product)
    if not product_id:
        raise ValueError("product must normalize to a product id such as XRP-USD")

    run_id = utc_now_id()
    destination_dir = raw_root / "external_ohlcv" / f"product={product_id}" / f"provider={provider}"
    manifest_dir = catalog_root / "external_ohlcv"
    manifest_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = manifest_dir / f"move_external_ohlcv_{run_id}.jsonl"

    files_seen = 0
    files_planned = 0
    files_moved = 0
    files_missing = 0
    files_skipped_existing = 0
    files_skipped_same_root = 0
    files_failed = 0
    bytes_seen = 0

    with manifest_path.open("wt", encoding="utf-8", newline="\n") as manifest:
        for source in inputs:
            files_seen += 1
            destination = destination_dir / source.name
            action = "planned"
            error = None
            size = None
            modified_time = None
            sha256 = None

            try:
                if not source.exists():
                    action = "missing"
                    files_missing += 1
                else:
                    stat = source.stat()
                    size = stat.st_size
                    bytes_seen += size
                    modified_time = _iso_z(datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc))
                    sha256 = _sha256(source)

                    if _is_relative_to(source, raw_root):
                        action = "skipped_same_root"
                        files_skipped_same_root += 1
                    elif destination.exists():
                        action = "skipped_existing"
                        files_skipped_existing += 1
                    elif mode == "plan":
                        action = "planned"
                        files_planned += 1
                    else:
                        if not _same_drive(source, destination):
                            raise OSError("move mode requires source and destination on the same drive")
                        destination.parent.mkdir(parents=True, exist_ok=True)
                        source.rename(destination)
                        action = "moved"
                        files_moved += 1
            except Exception as exc:  # noqa: BLE001 - record every file-level failure.
                action = "failed"
                error = str(exc)
                files_failed += 1

            manifest.write(
                json.dumps(
                    {
                        "run_id": run_id,
                        "source_path": str(source.resolve()),
                        "destination_path": str(destination.resolve()),
                        "product_id": product_id,
                        "provider": provider,
                        "mode": mode,
                        "action": action,
                        "size_bytes": size,
                        "modified_time": modified_time,
                        "sha256": sha256,
                        "error": error,
                    },
                    sort_keys=True,
                )
                + "\n"
            )

    return ExternalOhlcvMoveSummary(
        run_id=run_id,
        raw_root=str(raw_root.resolve()),
        catalog_manifest_path=str(manifest_path.resolve()),
        mode=mode,
        files_seen=files_seen,
        files_planned=files_planned,
        files_moved=files_moved,
        files_missing=files_missing,
        files_skipped_existing=files_skipped_existing,
        files_skipped_same_root=files_skipped_same_root,
        files_failed=files_failed,
        bytes_seen=bytes_seen,
    )


def _fetch_json(url: str, timeout_seconds: int) -> Any:
    request = Request(url, headers={"User-Agent": "MoneyMan/0.1"})
    with urlopen(request, timeout=timeout_seconds) as response:  # noqa: S310 - URL is fixed by caller.
        return json.loads(response.read().decode("utf-8"))


def _request_windows(
    start_dt: datetime,
    end_dt: datetime,
    granularity_seconds: int,
    max_candles_per_request: int,
) -> list[tuple[datetime, datetime]]:
    windows: list[tuple[datetime, datetime]] = []
    cursor = start_dt
    chunk = timedelta(seconds=granularity_seconds * max_candles_per_request)
    while cursor < end_dt:
        chunk_end = min(cursor + chunk, end_dt)
        windows.append((cursor, chunk_end))
        cursor = chunk_end
    return windows


def _exchange_candles_url(product_id: str, start_dt: datetime, end_dt: datetime, granularity_seconds: int) -> str:
    query = urlencode(
        {
            "start": _iso_z(start_dt),
            "end": _iso_z(end_dt),
            "granularity": str(granularity_seconds),
        }
    )
    return f"{COINBASE_EXCHANGE_CANDLES_URL.format(product_id=product_id)}?{query}"


def _raw_exchange_candle_row(
    candle: list[Any],
    product_id: str,
    provider: str,
    granularity_seconds: int,
    retrieved_at: str,
    request_start: str,
    request_end: str,
) -> dict[str, Any]:
    if len(candle) < 6:
        raise ValueError(f"expected 6 candle fields, got {len(candle)}")
    start_epoch = int(candle[0])
    return {
        "source_kind": "external_ohlcv_raw",
        "source_provider": provider,
        "product_id": product_id,
        "granularity_seconds": granularity_seconds,
        "retrieved_at": retrieved_at,
        "request_start": request_start,
        "request_end": request_end,
        "start_epoch": start_epoch,
        "start_ts": _iso_z(datetime.fromtimestamp(start_epoch, tz=timezone.utc)),
        "low": str(candle[1]),
        "high": str(candle[2]),
        "open": str(candle[3]),
        "close": str(candle[4]),
        "volume": str(candle[5]),
    }


def _fallback_row_from_raw(raw_row: dict[str, Any], raw_output_path: Path, source_line: int) -> dict[str, Any]:
    return {
        "start_ts": raw_row["start_ts"],
        "product_id": raw_row["product_id"],
        "open": raw_row["open"],
        "high": raw_row["high"],
        "low": raw_row["low"],
        "close": raw_row["close"],
        "volume": raw_row["volume"],
        "timeframe": _timeframe(int(raw_row["granularity_seconds"])),
        "source_kind": "price_only_fallback",
        "source_provider": raw_row["source_provider"],
        "source_path": str(raw_output_path.resolve()),
        "source_line": source_line,
        "limitations": EXTERNAL_OHLCV_LIMITATION,
    }


def fetch_coinbase_exchange_candles(
    product: str,
    start: str,
    end: str,
    raw_root: Path,
    derived_root: Path,
    catalog_root: Path,
    granularity_seconds: int = 60,
    provider: str = "coinbase_exchange_public",
    max_candles_per_request: int = 300,
    timeout_seconds: int = 30,
    sleep_seconds: float = 0.0,
    fetch_json: Callable[[str, int], Any] | None = None,
) -> dict[str, Any]:
    product_id = normalize_product_id(product)
    if not product_id:
        raise ValueError("product must normalize to a product id such as XRP-USD")
    if granularity_seconds not in {60, 300, 900, 3600, 21600, 86400}:
        raise ValueError("granularity_seconds must be one of 60, 300, 900, 3600, 21600, 86400")
    if max_candles_per_request < 1 or max_candles_per_request > 300:
        raise ValueError("max_candles_per_request must be between 1 and 300")

    start_dt = _parse_utc_datetime(start)
    end_dt = _parse_utc_datetime(end)
    if end_dt <= start_dt:
        raise ValueError("end must be after start")

    run_id = utc_now_id()
    raw_output_path = (
        raw_root
        / "external_ohlcv"
        / f"product={product_id}"
        / f"provider={provider}"
        / f"granularity={granularity_seconds}"
        / f"part_{run_id}.jsonl"
    )
    derived_output_path = derived_root / "v1" / "candles_fallback" / f"part_{run_id}.jsonl"
    report_path = catalog_root / "quality" / f"candle_fallback_fetch_{run_id}.json"
    raw_output_path.parent.mkdir(parents=True, exist_ok=True)
    derived_output_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.parent.mkdir(parents=True, exist_ok=True)

    fetcher = fetch_json or _fetch_json
    retrieved_at = _iso_z(datetime.now(timezone.utc))
    raw_rows_by_start: dict[int, dict[str, Any]] = {}
    request_errors: list[dict[str, Any]] = []
    duplicate_candles = 0
    rows_received = 0

    windows = _request_windows(start_dt, end_dt, granularity_seconds, max_candles_per_request)
    for index, (request_start_dt, request_end_dt) in enumerate(windows):
        request_start = _iso_z(request_start_dt)
        request_end = _iso_z(request_end_dt)
        url = _exchange_candles_url(product_id, request_start_dt, request_end_dt, granularity_seconds)
        try:
            payload = fetcher(url, timeout_seconds)
            if not isinstance(payload, list):
                raise ValueError(f"expected list response, got {type(payload).__name__}")
            for candle in payload:
                if not isinstance(candle, list):
                    raise ValueError(f"expected candle array, got {type(candle).__name__}")
                raw_row = _raw_exchange_candle_row(
                    candle,
                    product_id=product_id,
                    provider=provider,
                    granularity_seconds=granularity_seconds,
                    retrieved_at=retrieved_at,
                    request_start=request_start,
                    request_end=request_end,
                )
                rows_received += 1
                start_epoch = int(raw_row["start_epoch"])
                candle_dt = datetime.fromtimestamp(start_epoch, tz=timezone.utc)
                if candle_dt < start_dt or candle_dt >= end_dt:
                    continue
                if start_epoch in raw_rows_by_start:
                    duplicate_candles += 1
                    continue
                raw_rows_by_start[start_epoch] = raw_row
        except Exception as exc:  # noqa: BLE001 - include request context in the report.
            request_errors.append(
                {
                    "request_index": index,
                    "request_start": request_start,
                    "request_end": request_end,
                    "url": url,
                    "error": str(exc),
                }
            )
        if sleep_seconds > 0 and index + 1 < len(windows):
            import time

            time.sleep(sleep_seconds)

    raw_rows = [raw_rows_by_start[start_epoch] for start_epoch in sorted(raw_rows_by_start)]
    with raw_output_path.open("wt", encoding="utf-8", newline="\n") as raw_output:
        with derived_output_path.open("wt", encoding="utf-8", newline="\n") as derived_output:
            for source_line, raw_row in enumerate(raw_rows, start=1):
                raw_output.write(json.dumps(raw_row, sort_keys=True) + "\n")
                derived_output.write(
                    json.dumps(_fallback_row_from_raw(raw_row, raw_output_path, source_line), sort_keys=True) + "\n"
                )

    expected_epochs = set(
        range(
            int(start_dt.timestamp()),
            int(end_dt.timestamp()),
            granularity_seconds,
        )
    )
    actual_epochs = set(raw_rows_by_start)
    missing_epochs = sorted(expected_epochs - actual_epochs)
    missing_samples = [
        _iso_z(datetime.fromtimestamp(epoch, tz=timezone.utc)) for epoch in missing_epochs[:25]
    ]

    first_start_ts = raw_rows[0]["start_ts"] if raw_rows else None
    last_start_ts = raw_rows[-1]["start_ts"] if raw_rows else None
    report = {
        "run_id": run_id,
        "product_id": product_id,
        "provider": provider,
        "source_kind": "price_only_fallback",
        "raw_output_path": str(raw_output_path.resolve()),
        "derived_output_path": str(derived_output_path.resolve()),
        "request_start": _iso_z(start_dt),
        "request_end": _iso_z(end_dt),
        "granularity_seconds": granularity_seconds,
        "request_count": len(windows),
        "rows_received": rows_received,
        "rows_written": len(raw_rows),
        "duplicate_candles": duplicate_candles,
        "expected_candles": len(expected_epochs),
        "missing_candles": len(missing_epochs),
        "missing_start_ts_samples": missing_samples,
        "first_start_ts": first_start_ts,
        "last_start_ts": last_start_ts,
        "request_errors": request_errors,
        "limitations": [
            "These rows can bridge price-path gaps for candle-only backtests.",
            "These rows cannot replace missing L2 spread, depth, queue, or imbalance.",
            "Coinbase Exchange notes historical rate data may be incomplete when no ticks exist.",
        ],
    }
    report_path.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
    return {
        "run_id": run_id,
        "raw_output_path": str(raw_output_path),
        "derived_output_path": str(derived_output_path),
        "report_path": str(report_path),
        "report": report,
    }


def import_candle_csv_files(
    inputs: list[Path],
    product: str,
    derived_root: Path,
    catalog_root: Path,
    provider: str = "unknown",
    max_rows: int | None = None,
) -> dict[str, Any]:
    product_id = normalize_product_id(product)
    if not product_id:
        raise ValueError("product must normalize to a product id such as XRP-USD")

    run_id = utc_now_id()
    output_path = derived_root / "v1" / "candles_fallback" / f"part_{run_id}.jsonl"
    report_path = catalog_root / "quality" / f"candle_fallback_import_{run_id}.json"
    output_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.parent.mkdir(parents=True, exist_ok=True)

    counts_by_source: dict[str, CandleImportCounts] = {}
    parse_error_samples: list[dict[str, Any]] = []
    first_start_ts: str | None = None
    last_start_ts: str | None = None
    total_seen = 0
    total_written = 0
    total_failed = 0

    with output_path.open("wt", encoding="utf-8", newline="\n") as output:
        for input_path in inputs:
            counts = CandleImportCounts()
            counts_by_source[str(input_path.resolve())] = counts
            with input_path.open("rt", encoding="utf-8-sig", newline="") as handle:
                reader = csv.DictReader(handle)
                missing = [column for column in REQUIRED_CANDLE_COLUMNS if column not in (reader.fieldnames or [])]
                if missing:
                    raise ValueError(f"{input_path} is missing required columns: {', '.join(missing)}")
                for source_line, row in enumerate(reader, start=2):
                    if max_rows is not None and total_seen >= max_rows:
                        break
                    counts.rows_seen += 1
                    total_seen += 1
                    try:
                        candle = _candle_row(row, input_path, source_line, product_id, provider)
                    except Exception as exc:  # noqa: BLE001 - report bad rows with context.
                        counts.rows_failed += 1
                        total_failed += 1
                        if len(parse_error_samples) < 25:
                            parse_error_samples.append(
                                {
                                    "source_path": str(input_path.resolve()),
                                    "source_line": source_line,
                                    "error": str(exc),
                                    "row": row,
                                }
                            )
                        continue

                    output.write(json.dumps(candle, sort_keys=True) + "\n")
                    counts.rows_written += 1
                    total_written += 1
                    start_ts = str(candle["start_ts"])
                    if first_start_ts is None or start_ts < first_start_ts:
                        first_start_ts = start_ts
                    if last_start_ts is None or start_ts > last_start_ts:
                        last_start_ts = start_ts
                if max_rows is not None and total_seen >= max_rows:
                    break

    report = {
        "run_id": run_id,
        "product_id": product_id,
        "provider": provider,
        "source_kind": "price_only_fallback",
        "input_paths": [str(path.resolve()) for path in inputs],
        "output_path": str(output_path.resolve()),
        "rows_seen": total_seen,
        "rows_written": total_written,
        "rows_failed": total_failed,
        "first_start_ts": first_start_ts,
        "last_start_ts": last_start_ts,
        "counts_by_source": {
            source: {
                "rows_seen": counts.rows_seen,
                "rows_written": counts.rows_written,
                "rows_failed": counts.rows_failed,
            }
            for source, counts in sorted(counts_by_source.items())
        },
        "parse_error_samples": parse_error_samples,
        "limitations": [
            "These rows can bridge price-path gaps for candle-only backtests.",
            "These rows cannot replace missing L2 spread, depth, queue, or imbalance.",
        ],
    }
    report_path.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
    return {"run_id": run_id, "output_path": str(output_path), "report_path": str(report_path), "report": report}
