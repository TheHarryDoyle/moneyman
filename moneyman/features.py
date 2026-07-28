from __future__ import annotations

import json
import heapq
from collections import deque
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from .coinbase import normalize_product_id, utc_now_id
from .normalize import audit_normalization
from .raw import read_jsonl_dicts, write_jsonl


def _decimal(value: Any) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def _decimal_str(value: Decimal | None) -> str | None:
    return str(value) if value is not None else None


class RollingMicrostructureCalculator:
    def __init__(self, trade_window: int = 100) -> None:
        self.bids: dict[Decimal, Decimal] = {}
        self.asks: dict[Decimal, Decimal] = {}
        self.bid_heap: list[Decimal] = []
        self.ask_heap: list[Decimal] = []
        self.bid_depth = Decimal("0")
        self.ask_depth = Decimal("0")
        self.trades: deque[tuple[str, Decimal]] = deque(maxlen=trade_window)
        self.trade_window = trade_window

    def update_trade(self, row: dict[str, Any]) -> None:
        size = _decimal(row.get("size"))
        if size is None:
            return
        side = str(row.get("side") or "").lower()
        self.trades.append((side, size))

    def update_l2(self, row: dict[str, Any]) -> None:
        price = _decimal(row.get("price_level"))
        quantity = _decimal(row.get("new_quantity"))
        if price is None or quantity is None:
            return
        side = str(row.get("side") or "").lower()
        book = self.bids if side in {"bid", "buy"} else self.asks
        old_quantity = book.get(price, Decimal("0"))
        if quantity <= 0:
            if price in book:
                del book[price]
                if book is self.bids:
                    self.bid_depth -= old_quantity
                else:
                    self.ask_depth -= old_quantity
        else:
            book[price] = quantity
            if book is self.bids:
                self.bid_depth += quantity - old_quantity
                if old_quantity == 0:
                    heapq.heappush(self.bid_heap, -price)
            else:
                self.ask_depth += quantity - old_quantity
                if old_quantity == 0:
                    heapq.heappush(self.ask_heap, price)

    def _best_bid(self) -> Decimal | None:
        while self.bid_heap:
            price = -self.bid_heap[0]
            if price in self.bids:
                return price
            heapq.heappop(self.bid_heap)
        return None

    def _best_ask(self) -> Decimal | None:
        while self.ask_heap:
            price = self.ask_heap[0]
            if price in self.asks:
                return price
            heapq.heappop(self.ask_heap)
        return None

    def _trade_imbalance(self) -> Decimal | None:
        buy = Decimal("0")
        sell = Decimal("0")
        for side, size in self.trades:
            if side in {"buy", "bid"}:
                buy += size
            elif side in {"sell", "ask", "offer"}:
                sell += size
        total = buy + sell
        if total == 0:
            return None
        return (buy - sell) / total

    def current_feature(self, row: dict[str, Any]) -> dict[str, Any] | None:
        best_bid = self._best_bid()
        best_ask = self._best_ask()
        if best_bid is None or best_ask is None:
            return None
        midpoint = (best_bid + best_ask) / Decimal("2")
        spread = best_ask - best_bid
        relative_spread = spread / midpoint if midpoint != 0 else None
        depth_total = self.bid_depth + self.ask_depth
        book_imbalance = (self.bid_depth - self.ask_depth) / depth_total if depth_total != 0 else None
        quality_status = "ok" if spread >= 0 else "crossed_book"
        return {
            "event_ts": row.get("event_ts"),
            "recv_ts": row.get("recv_ts"),
            "product_id": row.get("product_id"),
            "midpoint": _decimal_str(midpoint),
            "spread": _decimal_str(spread),
            "relative_spread": _decimal_str(relative_spread),
            "book_imbalance": _decimal_str(book_imbalance),
            "trade_imbalance": _decimal_str(self._trade_imbalance()),
            "window": f"last_{self.trade_window}_trades",
            "source_tables": "trades,l2_updates",
            "quality_status": quality_status,
        }


def calculate_feature_rows(
    trades: list[dict[str, Any]],
    l2_updates: list[dict[str, Any]],
    trade_window: int = 100,
) -> list[dict[str, Any]]:
    calculator = RollingMicrostructureCalculator(trade_window=trade_window)
    combined: list[tuple[str, dict[str, Any]]] = [
        *[("trade", row) for row in trades],
        *[("l2", row) for row in l2_updates],
    ]
    combined.sort(key=lambda item: (item[1].get("event_ts") or "", item[1].get("source_line") or 0, item[0]))

    features: list[dict[str, Any]] = []
    for kind, row in combined:
        if kind == "trade":
            calculator.update_trade(row)
            feature = calculator.current_feature(row)
        else:
            calculator.update_l2(row)
            feature = calculator.current_feature(row)
        if feature:
            features.append(feature)
    return features


def run_features(
    derived_root: Path,
    catalog_root: Path,
    product: str | None = None,
    trade_window: int = 100,
    normalization_dataset_id: str | None = None,
) -> dict[str, Any]:
    product = normalize_product_id(product)
    if not product:
        raise ValueError("product is required so one order book cannot mix multiple products")

    trades: list[dict[str, Any]] = []
    l2_updates: list[dict[str, Any]] = []
    selected_sources: list[str] = []
    normalization_audit: dict[str, Any] | None = None
    if normalization_dataset_id:
        manifest_path = (
            derived_root
            / "v2"
            / "normalization_datasets"
            / normalization_dataset_id
            / "manifest.json"
        )
        if not manifest_path.exists():
            raise FileNotFoundError(f"normalization dataset manifest not found: {manifest_path}")
        normalization_audit = audit_normalization(manifest_path)
        if normalization_audit.get("valid") is not True:
            raise ValueError(
                f"normalization dataset failed audit: {manifest_path}: "
                + "; ".join(str(error) for error in normalization_audit.get("errors", []))
            )
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if (
            manifest.get("status") != "completed"
            or manifest.get("dataset_id") != normalization_dataset_id
            or manifest.get("schema_version") != "normalization.v2"
        ):
            raise ValueError(f"normalization dataset manifest is not eligible: {manifest_path}")
        for artifact in manifest.get("artifacts", []):
            table = artifact.get("table")
            if table not in {"trades", "l2_updates"}:
                continue
            partition = artifact.get("partition")
            if not isinstance(partition, dict) or partition.get("product") != product:
                continue
            path = Path(artifact["path"])
            selected_sources.append(str(path.resolve()))
            rows = read_jsonl_dicts(path)
            if any(
                row.get("dataset_id") != normalization_dataset_id
                or row.get("product_id") != product
                for row in rows
            ):
                raise ValueError(f"normalization artifact contains out-of-scope rows: {path}")
            if table == "trades":
                trades.extend(rows)
            else:
                l2_updates.extend(rows)
    else:
        for path in sorted((derived_root / "v1" / "trades").glob("*.jsonl")):
            selected_sources.append(str(path.resolve()))
            trades.extend(read_jsonl_dicts(path))
        for path in sorted((derived_root / "v1" / "l2_updates").glob("*.jsonl")):
            selected_sources.append(str(path.resolve()))
            l2_updates.extend(read_jsonl_dicts(path))
    trades = [row for row in trades if row.get("product_id") == product]
    l2_updates = [row for row in l2_updates if row.get("product_id") == product]

    run_id = utc_now_id()
    features = calculate_feature_rows(trades, l2_updates, trade_window=trade_window)
    output_path = derived_root / "v1" / "microstructure_features" / f"features_{run_id}.jsonl"
    report_path = catalog_root / "quality" / f"features_quality_{run_id}.json"
    write_jsonl(output_path, features)
    report = {
        "feature_rows": len(features),
        "products": sorted({row.get("product_id") for row in features if row.get("product_id")}),
        "quality_statuses": sorted({row.get("quality_status") for row in features if row.get("quality_status")}),
        "output_path": str(output_path),
        "normalization_dataset_id": normalization_dataset_id,
        "normalization_audit": normalization_audit,
        "selected_sources": selected_sources,
    }
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
    return {"run_id": run_id, "output_path": str(output_path), "report_path": str(report_path), "report": report}
