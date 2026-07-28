from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import DEFAULT_PRODUCTS, DEFAULT_WS_URL, default_data_root, products_from_env


DEFAULT_CHANNELS = ("ticker", "candles", "level2", "market_trades", "heartbeats", "status")


@dataclass(frozen=True)
class LoggerConfig:
    config_path: Path | None
    config_source: str
    ws_url: str
    products: list[str]
    channels: list[str]
    raw_root: Path
    raw_root_source: str
    roll_interval_seconds: int
    flush_interval_messages: int
    progress_interval_messages: int
    manifest_interval_messages: int


def _default_config_path() -> Path:
    return Path(os.environ.get("MONEYMAN_LOGGER_CONFIG", "config/logger.json")).expanduser()


def _read_config(path: Path | None) -> tuple[dict[str, Any], Path | None, str]:
    config_path = path or _default_config_path()
    if not config_path.exists():
        return {}, None, "defaults/env"
    payload = json.loads(config_path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"Logger config must be a JSON object: {config_path}")
    return payload, config_path, str(config_path.resolve())


def _csv_list(raw: str | None) -> list[str] | None:
    if not raw:
        return None
    return [item.strip() for item in raw.split(",") if item.strip()]


def _json_list(value: Any, name: str) -> list[str] | None:
    if value is None:
        return None
    if not isinstance(value, list):
        raise ValueError(f"{name} must be a JSON list")
    output = [str(item).strip() for item in value if str(item).strip()]
    return output or None


def _int_setting(payload: dict[str, Any], key: str, env_name: str, default: int) -> int:
    if os.environ.get(env_name):
        return int(os.environ[env_name])
    if payload.get(key) is not None:
        return int(payload[key])
    return default


def _resolve_raw_root(payload: dict[str, Any]) -> tuple[Path, str]:
    if os.environ.get("MONEYMAN_RAW_ROOT"):
        return Path(os.environ["MONEYMAN_RAW_ROOT"]).expanduser(), "MONEYMAN_RAW_ROOT"
    if payload.get("raw_root"):
        return Path(str(payload["raw_root"])).expanduser(), "logger_config.raw_root"

    downloads_default = Path.home() / "Downloads" / "MoneyManData" / "raw"
    if downloads_default.exists():
        return downloads_default, "existing ~/Downloads/MoneyManData/raw"

    data_root = default_data_root()
    return data_root / "raw", "fallback data/raw"


def load_logger_config(path: Path | None = None) -> LoggerConfig:
    payload, config_path, config_source = _read_config(path)
    products = (
        _csv_list(os.environ.get("MONEYMAN_PRODUCTS"))
        or _json_list(payload.get("products"), "products")
        or list(DEFAULT_PRODUCTS)
    )
    channels = (
        _csv_list(os.environ.get("MONEYMAN_COINBASE_CHANNELS"))
        or _json_list(payload.get("channels"), "channels")
        or list(DEFAULT_CHANNELS)
    )
    raw_root, raw_root_source = _resolve_raw_root(payload)
    return LoggerConfig(
        config_path=config_path,
        config_source=config_source,
        ws_url=os.environ.get("MONEYMAN_COINBASE_WS_URL", str(payload.get("ws_url") or DEFAULT_WS_URL)),
        products=[item.upper() for item in products],
        channels=channels,
        raw_root=raw_root,
        raw_root_source=raw_root_source,
        roll_interval_seconds=_int_setting(payload, "roll_interval_seconds", "MONEYMAN_ROLL_INTERVAL_SECONDS", 600),
        flush_interval_messages=_int_setting(payload, "flush_interval_messages", "MONEYMAN_FLUSH_INTERVAL_MESSAGES", 100),
        progress_interval_messages=_int_setting(payload, "progress_interval_messages", "MONEYMAN_PROGRESS_INTERVAL_MESSAGES", 5000),
        manifest_interval_messages=_int_setting(payload, "manifest_interval_messages", "MONEYMAN_MANIFEST_INTERVAL_MESSAGES", 5000),
    )
