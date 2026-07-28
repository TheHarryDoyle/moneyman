from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


PRODUCT_RE = re.compile(r"^[A-Z0-9]+-[A-Z0-9]+$")
LEGACY_WS_RE = re.compile(r"^(?P<base>[a-z0-9]+(?:-[a-z0-9]+)?)_ws_data$", re.IGNORECASE)


def utc_now_id() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def message_channel(message: dict[str, Any]) -> str:
    return str(
        message.get("_channel")
        or message.get("channel")
        or message.get("type")
        or "unknown"
    )


def normalize_product_id(value: str | None) -> str | None:
    if not value:
        return None
    product = value.strip().upper().replace("_", "-")
    if "-" not in product and len(product) > 3:
        product = f"{product[:-3]}-{product[-3:]}"
    if PRODUCT_RE.match(product):
        return product
    return product if product else None


def product_from_legacy_folder(name: str) -> str | None:
    match = LEGACY_WS_RE.match(name)
    if not match:
        return None
    base = match.group("base")
    if "-" not in base and len(base) > 3:
        base = f"{base[:-3]}-{base[-3:]}"
    return normalize_product_id(base)


def guess_product_from_path(path: Path) -> str | None:
    for part in reversed(path.parts):
        if part.startswith("product="):
            return normalize_product_id(part.split("=", 1)[1])
        legacy = product_from_legacy_folder(part)
        if legacy:
            return legacy

    stem = path.name.lower().replace(".jsonl", "").replace(".gz", "")
    match = re.search(r"([a-z0-9]+)[_-]([a-z0-9]+)", stem)
    if match:
        return normalize_product_id(f"{match.group(1)}-{match.group(2)}")
    return None


def session_id_from_path(path: Path) -> str | None:
    parts = list(path.parts)
    for part in reversed(parts):
        if part.startswith("session="):
            return part.split("=", 1)[1]

    for index, part in enumerate(parts[:-1]):
        if part.lower() == "legacy_ws_data":
            continue
        if LEGACY_WS_RE.match(part) and index + 1 < len(parts):
            return parts[index + 1]
    return None


def extract_product_ids(message: dict[str, Any]) -> set[str]:
    products: set[str] = set()

    def add(value: str | None) -> None:
        normalized = normalize_product_id(value)
        if normalized:
            products.add(normalized)

    add(message.get("product_id"))
    for event in message.get("events", []) or []:
        if not isinstance(event, dict):
            continue
        add(event.get("product_id"))
        for field in ("trades", "updates", "tickers", "candles"):
            for item in event.get(field, []) or []:
                if isinstance(item, dict):
                    add(item.get("product_id"))
    return products


def _collect_ts(value: Any, output: list[str]) -> None:
    if isinstance(value, str) and value:
        output.append(value)


def extract_recv_timestamps(message: dict[str, Any]) -> list[str]:
    timestamps: list[str] = []
    _collect_ts(message.get("_recv_ts"), timestamps)
    _collect_ts(message.get("recv_ts"), timestamps)
    return timestamps


def extract_event_timestamps(message: dict[str, Any]) -> list[str]:
    timestamps: list[str] = []
    for key in ("timestamp", "time", "event_time"):
        _collect_ts(message.get(key), timestamps)
    for event in message.get("events", []) or []:
        if not isinstance(event, dict):
            continue
        for key in ("timestamp", "time", "event_time"):
            _collect_ts(event.get(key), timestamps)
        for field in ("trades", "updates", "tickers", "candles"):
            for item in event.get(field, []) or []:
                if not isinstance(item, dict):
                    continue
                for key in ("timestamp", "time", "event_time", "start"):
                    _collect_ts(item.get(key), timestamps)
    return timestamps


def safe_min(values: list[str]) -> str | None:
    return min(values) if values else None


def safe_max(values: list[str]) -> str | None:
    return max(values) if values else None
