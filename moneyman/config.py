from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


DEFAULT_PRODUCTS = ("XRP-USD", "BTC-USD", "ETH-USD")
DEFAULT_WS_URL = "wss://advanced-trade-ws.coinbase.com"


def default_data_root() -> Path:
    downloads_root = Path.home() / "Downloads" / "MoneyManData"
    if downloads_root.exists():
        return downloads_root
    return Path("data")


def _path_from_env(name: str, fallback: str) -> Path:
    return Path(os.environ.get(name, fallback)).expanduser()


def products_from_env() -> list[str]:
    raw = os.environ.get("MONEYMAN_PRODUCTS")
    if not raw:
        return list(DEFAULT_PRODUCTS)
    return [item.strip().upper() for item in raw.split(",") if item.strip()]


@dataclass(frozen=True)
class DataRoots:
    data_root: Path
    raw_root: Path
    derived_root: Path
    catalog_root: Path
    quarantine_root: Path

    @classmethod
    def from_env(cls) -> "DataRoots":
        data_root = _path_from_env("MONEYMAN_DATA_ROOT", str(default_data_root()))
        return cls(
            data_root=data_root,
            raw_root=_path_from_env("MONEYMAN_RAW_ROOT", str(data_root / "raw")),
            derived_root=_path_from_env("MONEYMAN_DERIVED_ROOT", str(data_root / "derived")),
            catalog_root=_path_from_env("MONEYMAN_CATALOG_ROOT", str(data_root / "catalog")),
            quarantine_root=_path_from_env(
                "MONEYMAN_QUARANTINE_ROOT", str(data_root / "quarantine")
            ),
        )
