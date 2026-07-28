from __future__ import annotations

import hashlib
import json
import heapq
from dataclasses import asdict, dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from .book import discover_audited_book_runs
from .coinbase import normalize_product_id, utc_now_id
from .fees import (
    FeeAccumulator,
    FeeProfile,
    fee_profile_to_report,
    manual_fee_profile,
    resolve_fee_profile,
    zero_fee_quote,
)
from .l2_fills import (
    AuditedBookSelectionError,
    StrictL2FillConfig,
    load_audited_book_window,
    simulate_gridbot_on_l2,
)
from .raw import iter_jsonl, write_jsonl


@dataclass(frozen=True)
class GridbotConfig:
    product_id: str
    lower: str
    upper: str
    grid_count: int
    quote_start: str
    base_start: str
    order_quote: str
    fee_rate: str
    include_fallback_candles: bool
    candle_path_assumption: str
    start: str | None = None
    end: str | None = None
    providers: tuple[str, ...] = ()
    max_rows: int | None = None
    fee_source: str = "manual"
    fee_source_status: str = "manual"
    maker_fee_rate: str | None = None
    taker_fee_rate: str | None = None
    liquidity_assumption: str = "maker"
    coinbase_one_advanced_rebate_rate: str = "0.25"
    coinbase_one_monthly_rebate_cap: str = "100"
    coinbase_one_monthly_rebate_used: str = "0"
    fee_profile_warnings: tuple[str, ...] = ()
    l2_run_id: str | None = None
    l2_window_id: str | None = None
    l2_latency_ms: int = 100
    l2_clock_source: str = "message_ts"
    l2_queue_policy: str = "strict_price_through"
    l2_partial_remainder_policy: str = "cancel"


@dataclass
class GridbotState:
    quote: Decimal
    base: Decimal
    fees_quote: Decimal = Decimal("0")
    fees_gross_quote: Decimal = Decimal("0")
    fee_rebates_quote: Decimal = Decimal("0")
    turnover_quote: Decimal = Decimal("0")
    filled_buys: int = 0
    filled_sells: int = 0
    missed_buys_quote: int = 0
    missed_sells_base: int = 0


def _decimal(value: Any, name: str) -> Decimal:
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"{name} must be a decimal number") from exc
    return parsed


def _decimal_str(value: Decimal) -> str:
    return format(value.normalize(), "f")


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _sha256_json(value: Any) -> str:
    encoded = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _output_artifact_report(path: Path, rows: int | None = None) -> dict[str, Any]:
    report: dict[str, Any] = {
        "path": path.name,
        "sha256": _sha256_file(path),
    }
    if rows is not None:
        report["rows"] = rows
    return report


def _candle_decimal(row: dict[str, Any], name: str) -> Decimal:
    return _decimal(row.get(name), name)


def _build_arithmetic_levels(lower: Decimal, upper: Decimal, grid_count: int) -> list[Decimal]:
    if grid_count < 1:
        raise ValueError("grid_count must be at least 1")
    if upper <= lower:
        raise ValueError("upper must be greater than lower")
    step = (upper - lower) / Decimal(grid_count)
    return [lower + (step * Decimal(index)) for index in range(grid_count + 1)]


def _nearest_level_index(levels: list[Decimal], price: Decimal) -> int:
    best_index = 0
    best_distance = abs(levels[0] - price)
    for index, level in enumerate(levels[1:], start=1):
        distance = abs(level - price)
        if distance < best_distance:
            best_index = index
            best_distance = distance
    return best_index


def _in_window(row: dict[str, Any], product_id: str, start: str | None, end: str | None, providers: set[str]) -> bool:
    if row.get("product_id") != product_id:
        return False
    if row.get("source_kind") != "price_only_fallback":
        return False
    start_ts = str(row.get("start_ts") or "")
    if not start_ts:
        return False
    if start and start_ts < start:
        return False
    if end and start_ts >= end:
        return False
    if providers and str(row.get("source_provider") or "") not in providers:
        return False
    return True


def load_fallback_candles(
    derived_root: Path,
    product_id: str,
    start: str | None = None,
    end: str | None = None,
    providers: tuple[str, ...] = (),
    max_rows: int | None = None,
    tail_rows: int | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    if tail_rows is not None and tail_rows < 1:
        raise ValueError("tail_rows must be at least 1")
    if tail_rows is not None and max_rows is not None:
        raise ValueError("tail_rows and max_rows cannot be combined")
    provider_set = set(providers)
    by_start: dict[str, dict[str, Any]] = {}
    derived_path_by_start: dict[str, str] = {}
    seen_start_timestamps: set[str] = set()
    tail_start_timestamps: list[str] = []
    duplicate_timestamps = 0
    files_seen = 0
    rows_seen = 0
    rows_matched = 0

    for path in sorted((derived_root / "v1" / "candles_fallback").glob("*.jsonl")):
        files_seen += 1
        for record in iter_jsonl(path):
            if record.payload is None:
                continue
            rows_seen += 1
            row = record.payload
            if not _in_window(row, product_id, start, end, provider_set):
                continue
            rows_matched += 1
            start_ts = str(row["start_ts"])
            if start_ts in seen_start_timestamps:
                duplicate_timestamps += 1
                continue
            seen_start_timestamps.add(start_ts)
            if tail_rows is None:
                by_start[start_ts] = row
                derived_path_by_start[start_ts] = str(path.resolve())
            elif len(tail_start_timestamps) < tail_rows:
                heapq.heappush(tail_start_timestamps, start_ts)
                by_start[start_ts] = row
                derived_path_by_start[start_ts] = str(path.resolve())
            elif start_ts > tail_start_timestamps[0]:
                removed_start_ts = heapq.heapreplace(
                    tail_start_timestamps,
                    start_ts,
                )
                del by_start[removed_start_ts]
                del derived_path_by_start[removed_start_ts]
                by_start[start_ts] = row
                derived_path_by_start[start_ts] = str(path.resolve())
            if max_rows is not None and len(seen_start_timestamps) >= max_rows:
                break
        if max_rows is not None and len(seen_start_timestamps) >= max_rows:
            break

    selected_start_timestamps = sorted(by_start)
    rows_selected_before_tail = len(seen_start_timestamps)
    candles = [by_start[key] for key in selected_start_timestamps]
    selected_derived_sources: dict[str, dict[str, Any]] = {}
    for start_ts in selected_start_timestamps:
        path_text = derived_path_by_start[start_ts]
        source = selected_derived_sources.setdefault(
            path_text,
            {
                "derived_path": path_text,
                "selected_rows": 0,
                "first_start_ts": start_ts,
                "last_start_ts": start_ts,
            },
        )
        source["selected_rows"] += 1
        source["last_start_ts"] = start_ts
    report = {
        "candle_files_seen": files_seen,
        "candle_rows_seen": rows_seen,
        "candle_rows_matched": rows_matched,
        "candle_rows_selected_before_tail": rows_selected_before_tail,
        "candle_rows_loaded": len(candles),
        "tail_rows": tail_rows,
        "duplicate_candle_timestamps_skipped": duplicate_timestamps,
        "selected_derived_sources": [
            selected_derived_sources[path_text]
            for path_text in sorted(selected_derived_sources)
        ],
        "providers": sorted({str(row.get("source_provider")) for row in candles if row.get("source_provider")}),
        "first_start_ts": candles[0].get("start_ts") if candles else None,
        "last_start_ts": candles[-1].get("start_ts") if candles else None,
    }
    return candles, report


def _fill_buy(
    state: GridbotState,
    level_index: int,
    level: Decimal,
    config: GridbotConfig,
    fees: FeeAccumulator,
    active_buys: set[int],
    active_sells: set[int],
    candle: dict[str, Any],
) -> dict[str, Any]:
    order_quote = _decimal(config.order_quote, "order_quote")
    gross_fee_estimate = fees.gross_fee_for(order_quote, config.liquidity_assumption)
    required_quote = order_quote + gross_fee_estimate
    status = "filled"
    base_delta = Decimal("0")
    if state.quote >= required_quote:
        fee_quote = fees.quote(order_quote, config.liquidity_assumption)
        base_delta = order_quote / level
        state.quote -= order_quote + fee_quote.gross_fee_quote
        state.base += base_delta
        state.fees_gross_quote += fee_quote.gross_fee_quote
        state.fee_rebates_quote += fee_quote.rebate_quote
        state.fees_quote += fee_quote.net_fee_quote
        state.turnover_quote += order_quote
        state.filled_buys += 1
        if level_index + 1 <= int(config.grid_count):
            active_sells.add(level_index + 1)
    else:
        fee_quote = zero_fee_quote(config.liquidity_assumption)
        state.missed_buys_quote += 1
        status = "missed_insufficient_quote"
    active_buys.discard(level_index)
    return _fill_row(candle, "buy", level_index, level, order_quote, base_delta, fee_quote, status, state)


def _fill_sell(
    state: GridbotState,
    level_index: int,
    level: Decimal,
    config: GridbotConfig,
    fees: FeeAccumulator,
    active_buys: set[int],
    active_sells: set[int],
    candle: dict[str, Any],
) -> dict[str, Any]:
    order_quote = _decimal(config.order_quote, "order_quote")
    base_delta = order_quote / level
    status = "filled"
    if state.base >= base_delta:
        fee_quote = fees.quote(order_quote, config.liquidity_assumption)
        state.base -= base_delta
        state.quote += order_quote - fee_quote.gross_fee_quote
        state.fees_gross_quote += fee_quote.gross_fee_quote
        state.fee_rebates_quote += fee_quote.rebate_quote
        state.fees_quote += fee_quote.net_fee_quote
        state.turnover_quote += order_quote
        state.filled_sells += 1
        if level_index - 1 >= 0:
            active_buys.add(level_index - 1)
    else:
        fee_quote = zero_fee_quote(config.liquidity_assumption)
        state.missed_sells_base += 1
        status = "missed_insufficient_base"
    active_sells.discard(level_index)
    return _fill_row(candle, "sell", level_index, level, order_quote, base_delta, fee_quote, status, state)


def _fill_row(
    candle: dict[str, Any],
    side: str,
    level_index: int,
    level: Decimal,
    order_quote: Decimal,
    base_delta: Decimal,
    fee_quote: Any,
    status: str,
    state: GridbotState,
) -> dict[str, Any]:
    return {
        "ts": candle.get("start_ts"),
        "side": side,
        "grid_level_index": level_index,
        "price": _decimal_str(level),
        "order_quote": _decimal_str(order_quote),
        "base_delta": _decimal_str(base_delta),
        "liquidity_assumption": fee_quote.liquidity,
        "fee_rate": _decimal_str(fee_quote.fee_rate),
        "fee_quote": _decimal_str(fee_quote.net_fee_quote),
        "fee_gross_quote": _decimal_str(fee_quote.gross_fee_quote),
        "fee_rebate_quote": _decimal_str(fee_quote.rebate_quote),
        "fee_net_quote": _decimal_str(fee_quote.net_fee_quote),
        "status": status,
        "quote_balance": _decimal_str(state.quote),
        "base_balance": _decimal_str(state.base),
        "rebate_balance_quote": _decimal_str(state.fee_rebates_quote),
        "source_kind": candle.get("source_kind"),
        "source_provider": candle.get("source_provider"),
        "source_path": candle.get("source_path"),
    }


def _process_buys(
    state: GridbotState,
    levels: list[Decimal],
    low: Decimal,
    anchor: Decimal,
    config: GridbotConfig,
    fees: FeeAccumulator,
    active_buys: set[int],
    active_sells: set[int],
    candle: dict[str, Any],
) -> list[dict[str, Any]]:
    fills: list[dict[str, Any]] = []
    for index in sorted(list(active_buys), reverse=True):
        level = levels[index]
        if low <= level <= anchor:
            fills.append(_fill_buy(state, index, level, config, fees, active_buys, active_sells, candle))
    return fills


def _process_sells(
    state: GridbotState,
    levels: list[Decimal],
    anchor: Decimal,
    high: Decimal,
    config: GridbotConfig,
    fees: FeeAccumulator,
    active_buys: set[int],
    active_sells: set[int],
    candle: dict[str, Any],
) -> list[dict[str, Any]]:
    fills: list[dict[str, Any]] = []
    for index in sorted(list(active_sells)):
        level = levels[index]
        if anchor <= level <= high:
            fills.append(_fill_sell(state, index, level, config, fees, active_buys, active_sells, candle))
    return fills


def _fee_profile_from_config(config: GridbotConfig) -> FeeProfile:
    return manual_fee_profile(
        fee_rate=config.fee_rate,
        maker_fee_rate=config.maker_fee_rate,
        taker_fee_rate=config.taker_fee_rate,
        liquidity_assumption=config.liquidity_assumption,
        coinbase_one_advanced_rebate_rate=config.coinbase_one_advanced_rebate_rate,
        coinbase_one_monthly_rebate_cap=config.coinbase_one_monthly_rebate_cap,
        coinbase_one_monthly_rebate_used=config.coinbase_one_monthly_rebate_used,
        source=config.fee_source,
        source_status=config.fee_source_status,
        warnings=config.fee_profile_warnings,
    )


def simulate_gridbot_on_candles(
    candles: list[dict[str, Any]],
    config: GridbotConfig,
    fee_profile: FeeProfile | None = None,
) -> dict[str, Any]:
    if not candles:
        raise ValueError("no candle rows available for backtest")

    resolved_fee_profile = fee_profile or _fee_profile_from_config(config)
    fees = FeeAccumulator(resolved_fee_profile)
    lower = _decimal(config.lower, "lower")
    upper = _decimal(config.upper, "upper")
    levels = _build_arithmetic_levels(lower, upper, config.grid_count)
    first_open = _candle_decimal(candles[0], "open")
    start_index = _nearest_level_index(levels, first_open)
    active_buys = set(range(0, start_index))
    active_sells = set(range(start_index + 1, len(levels)))
    state = GridbotState(quote=_decimal(config.quote_start, "quote_start"), base=_decimal(config.base_start, "base_start"))
    initial_equity = state.quote + (state.base * first_open)
    no_trade_initial_quote = state.quote
    no_trade_initial_base = state.base
    all_in_base = initial_equity / first_open if first_open != 0 else Decimal("0")
    previous_close = first_open
    fills: list[dict[str, Any]] = []
    equity_curve: list[dict[str, Any]] = []
    max_equity = initial_equity
    max_drawdown_quote = Decimal("0")

    for candle in candles:
        open_price = _candle_decimal(candle, "open")
        high = _candle_decimal(candle, "high")
        low = _candle_decimal(candle, "low")
        close = _candle_decimal(candle, "close")
        anchor = previous_close if previous_close else open_price
        if config.candle_path_assumption == "low-first":
            fills.extend(_process_buys(state, levels, low, anchor, config, fees, active_buys, active_sells, candle))
            fills.extend(_process_sells(state, levels, low, high, config, fees, active_buys, active_sells, candle))
        elif config.candle_path_assumption == "high-first":
            fills.extend(_process_sells(state, levels, anchor, high, config, fees, active_buys, active_sells, candle))
            fills.extend(_process_buys(state, levels, low, high, config, fees, active_buys, active_sells, candle))
        else:
            raise ValueError("candle_path_assumption must be low-first or high-first")

        equity = state.quote + state.fee_rebates_quote + (state.base * close)
        max_equity = max(max_equity, equity)
        drawdown = max_equity - equity
        max_drawdown_quote = max(max_drawdown_quote, drawdown)
        equity_curve.append(
            {
                "ts": candle.get("start_ts"),
                "close": _decimal_str(close),
                "quote_balance": _decimal_str(state.quote),
                "base_balance": _decimal_str(state.base),
                "rebate_balance_quote": _decimal_str(state.fee_rebates_quote),
                "equity_quote": _decimal_str(equity),
                "drawdown_quote": _decimal_str(drawdown),
            }
        )
        previous_close = close

    final_close = _candle_decimal(candles[-1], "close")
    final_equity = state.quote + state.fee_rebates_quote + (state.base * final_close)
    no_trade_final_equity = no_trade_initial_quote + (no_trade_initial_base * final_close)
    buy_hold_final_equity = all_in_base * final_close
    summary = {
        "status": "completed",
        "engine": "price_only_gridbot_v1",
        "product_id": config.product_id,
        "mode": "fallback_candles" if config.include_fallback_candles else "strict_l2",
        "candle_path_assumption": config.candle_path_assumption,
        "grid_type": "arithmetic",
        "grid_levels": [_decimal_str(level) for level in levels],
        "start_grid_level_index": start_index,
        "anchor_source": "first_candle_open",
        "anchor_value": _decimal_str(first_open),
        "first_ts": candles[0].get("start_ts"),
        "last_ts": candles[-1].get("start_ts"),
        "candles_used": len(candles),
        "fills": len([row for row in fills if row["status"] == "filled"]),
        "filled_buys": state.filled_buys,
        "filled_sells": state.filled_sells,
        "missed_buys_insufficient_quote": state.missed_buys_quote,
        "missed_sells_insufficient_base": state.missed_sells_base,
        "fees_quote": _decimal_str(state.fees_quote),
        "fees_gross_quote": _decimal_str(state.fees_gross_quote),
        "fee_rebates_quote": _decimal_str(state.fee_rebates_quote),
        "fees_net_quote": _decimal_str(state.fees_quote),
        "turnover_quote": _decimal_str(state.turnover_quote),
        "fee_profile": fee_profile_to_report(resolved_fee_profile),
        "initial_equity_quote": _decimal_str(initial_equity),
        "final_equity_quote": _decimal_str(final_equity),
        "net_pnl_quote": _decimal_str(final_equity - initial_equity),
        "max_drawdown_quote": _decimal_str(max_drawdown_quote),
        "final_quote_balance": _decimal_str(state.quote),
        "final_base_balance": _decimal_str(state.base),
        "final_rebate_balance_quote": _decimal_str(state.fee_rebates_quote),
        "no_trade_final_equity_quote": _decimal_str(no_trade_final_equity),
        "buy_hold_final_equity_quote": _decimal_str(buy_hold_final_equity),
        "limitations": [
            "Fallback-candle mode can test price touches but cannot prove L2 fill depth, spread, queue position, or exact intraminute order.",
            "Coinbase One Advanced rebates are modeled as USDC-equivalent accrued value; exact rebate timing is simplified.",
            "Strict L2 mode requires reconstructed valid book snapshots and should skip invalid gap windows.",
            "This backtester does not place live trades.",
        ],
    }
    return {"summary": summary, "fills": fills, "equity_curve": equity_curve}


def _strict_l2_contract_report(derived_root: Path, product_id: str) -> dict[str, Any]:
    discovery = discover_audited_book_runs(derived_root, product_id)
    matching_product_runs = discovery["matching_product_runs"]
    eligible_runs = discovery["eligible_runs"]
    snapshot_files_found = sum(
        1
        for audit in discovery["audits"]
        if audit.get("product_id") == product_id
        and audit.get("book_snapshot_rows") is not None
    )
    snapshot_rows_found = sum(
        int(audit.get("book_snapshot_rows") or 0)
        for audit in discovery["audits"]
        if audit.get("product_id") == product_id
    )
    report: dict[str, Any] = {
        "engine": "strict_l2_gridbot_v1",
        "product_id": product_id,
        "book_snapshot_files_found": snapshot_files_found,
        "book_snapshot_rows_found": snapshot_rows_found,
        "book_contract_discovery": discovery,
    }
    if matching_product_runs == 0:
        report.update(
            {
                "status": "requires_book_snapshots",
                "message": (
                    "Strict L2 backtesting requires an audited reconstructed-book contract, "
                    "and no reconstruction manifests were found for this product."
                ),
                "next_step": (
                    "Run reconstruct-book across a complete capture stream before adding "
                    "depth-aware fills."
                ),
            }
        )
    elif eligible_runs == 0:
        report.update(
            {
                "status": "requires_valid_book_snapshots",
                "message": (
                    "Book reconstruction manifests were found, but none for this product "
                    "passed the strict-L2 contract audit."
                ),
                "next_step": (
                    "Review the reported contract errors or reconstruct a complete, valid "
                    "sequence window."
                ),
            }
        )
    else:
        report.update(
            {
                "status": "audited_book_windows_available",
                "message": (
                    "Audited continuous L2 windows are available for the conservative "
                    "strict fill model."
                ),
                "next_step": (
                    "Select exactly one eligible run/window and consume only its audited rows."
                ),
            }
        )
    return report


def run_gridbot_backtest(
    derived_root: Path,
    catalog_root: Path,
    product: str,
    lower: str,
    upper: str,
    grid_count: int,
    quote_start: str,
    base_start: str,
    order_quote: str,
    fee_rate: str = "0.006",
    fee_source: str = "manual",
    maker_fee_rate: str | None = None,
    taker_fee_rate: str | None = None,
    liquidity_assumption: str = "maker",
    coinbase_one_advanced_rebate_rate: str = "0.25",
    coinbase_one_monthly_rebate_cap: str = "100",
    coinbase_one_monthly_rebate_used: str = "0",
    include_fallback_candles: bool = False,
    candle_path_assumption: str = "low-first",
    start: str | None = None,
    end: str | None = None,
    providers: tuple[str, ...] = (),
    max_rows: int | None = None,
    l2_run_id: str | None = None,
    l2_window_id: str | None = None,
    l2_latency_ms: int = 100,
    l2_clock_source: str = "message_ts",
) -> dict[str, Any]:
    gridbot_engine_source_sha256 = _sha256_file(Path(__file__))
    product_id = normalize_product_id(product)
    if not product_id:
        raise ValueError("product must normalize to a product id such as XRP-USD")

    fee_profile = resolve_fee_profile(
        source=fee_source,
        fee_rate=fee_rate,
        maker_fee_rate=maker_fee_rate,
        taker_fee_rate=taker_fee_rate,
        liquidity_assumption=liquidity_assumption,
        coinbase_one_advanced_rebate_rate=coinbase_one_advanced_rebate_rate,
        coinbase_one_monthly_rebate_cap=coinbase_one_monthly_rebate_cap,
        coinbase_one_monthly_rebate_used=coinbase_one_monthly_rebate_used,
    )
    run_id = utc_now_id()
    run_dir = derived_root / "v1" / "backtests" / "gridbot" / run_id
    report_path = catalog_root / "quality" / f"gridbot_backtest_{run_id}.json"
    run_dir.mkdir(parents=True, exist_ok=True)
    report_path.parent.mkdir(parents=True, exist_ok=True)

    config = GridbotConfig(
        product_id=product_id,
        lower=str(lower),
        upper=str(upper),
        grid_count=grid_count,
        quote_start=str(quote_start),
        base_start=str(base_start),
        order_quote=str(order_quote),
        fee_rate=str(fee_rate),
        include_fallback_candles=include_fallback_candles,
        candle_path_assumption=candle_path_assumption,
        start=start,
        end=end,
        providers=providers,
        max_rows=max_rows,
        fee_source=fee_profile.source,
        fee_source_status=fee_profile.source_status,
        maker_fee_rate=fee_profile.maker_fee_rate,
        taker_fee_rate=fee_profile.taker_fee_rate,
        liquidity_assumption=fee_profile.liquidity_assumption,
        coinbase_one_advanced_rebate_rate=fee_profile.coinbase_one_advanced_rebate_rate,
        coinbase_one_monthly_rebate_cap=fee_profile.coinbase_one_monthly_rebate_cap,
        coinbase_one_monthly_rebate_used=fee_profile.coinbase_one_monthly_rebate_used,
        fee_profile_warnings=fee_profile.warnings,
        l2_run_id=l2_run_id,
        l2_window_id=l2_window_id,
        l2_latency_ms=l2_latency_ms,
        l2_clock_source=l2_clock_source,
    )
    config_payload = asdict(config)
    config_sha256 = _sha256_json(config_payload)
    config_path = run_dir / "config.json"
    config_path.write_text(json.dumps(config_payload, indent=2, sort_keys=True), encoding="utf-8")

    if not include_fallback_candles:
        contract_report = _strict_l2_contract_report(derived_root, product_id)
        if contract_report["status"] != "audited_book_windows_available":
            contract_report["fee_profile"] = fee_profile_to_report(fee_profile)
            (run_dir / "summary.json").write_text(
                json.dumps(contract_report, indent=2, sort_keys=True),
                encoding="utf-8",
            )
            report_path.write_text(
                json.dumps(contract_report, indent=2, sort_keys=True),
                encoding="utf-8",
            )
            return {
                "run_id": run_id,
                "run_dir": str(run_dir),
                "report_path": str(report_path),
                "summary": contract_report,
            }

        fill_config = StrictL2FillConfig(
            latency_ms=l2_latency_ms,
            clock_source=l2_clock_source,
        )
        try:
            book_rows, selection_report = load_audited_book_window(
                derived_root=derived_root,
                product_id=product_id,
                config=fill_config,
                run_id=l2_run_id,
                window_id=l2_window_id,
                start=start,
                end=end,
                max_rows=max_rows,
                discovery=contract_report["book_contract_discovery"],
            )
        except AuditedBookSelectionError as exc:
            summary = dict(contract_report)
            summary.update(
                {
                    "status": exc.code,
                    "message": exc.message,
                    "fee_profile": fee_profile_to_report(fee_profile),
                }
            )
            (run_dir / "summary.json").write_text(
                json.dumps(summary, indent=2, sort_keys=True),
                encoding="utf-8",
            )
            report_path.write_text(
                json.dumps(summary, indent=2, sort_keys=True),
                encoding="utf-8",
            )
            return {
                "run_id": run_id,
                "run_dir": str(run_dir),
                "report_path": str(report_path),
                "summary": summary,
            }

        lower_decimal = _decimal(lower, "lower")
        upper_decimal = _decimal(upper, "upper")
        levels = _build_arithmetic_levels(lower_decimal, upper_decimal, grid_count)
        first_midpoint = _decimal(book_rows[0].get("midpoint"), "midpoint")
        final_midpoint = _decimal(book_rows[-1].get("midpoint"), "midpoint")
        start_index = _nearest_level_index(levels, first_midpoint)
        strict_result = simulate_gridbot_on_l2(
            rows=book_rows,
            levels=levels,
            start_index=start_index,
            product_id=product_id,
            quote_start=_decimal(quote_start, "quote_start"),
            base_start=_decimal(base_start, "base_start"),
            order_quote=_decimal(order_quote, "order_quote"),
            fee_profile=fee_profile,
            fill_config=fill_config,
            selection_report=selection_report,
        )
        if _sha256_file(Path(__file__)) != gridbot_engine_source_sha256:
            raise RuntimeError("gridbot engine source changed during the strict run")
        initial_equity = _decimal(quote_start, "quote_start") + (
            _decimal(base_start, "base_start") * first_midpoint
        )
        no_trade_final_equity = _decimal(quote_start, "quote_start") + (
            _decimal(base_start, "base_start") * final_midpoint
        )
        buy_hold_base = initial_equity / first_midpoint
        summary = strict_result["summary"]
        summary.update(
            {
                "run_id": run_id,
                "lower": _decimal_str(lower_decimal),
                "upper": _decimal_str(upper_decimal),
                "grid_count": grid_count,
                "order_quote": _decimal_str(_decimal(order_quote, "order_quote")),
                "first_midpoint": _decimal_str(first_midpoint),
                "final_midpoint": _decimal_str(final_midpoint),
                "anchor_source": "first_book_midpoint",
                "anchor_value": _decimal_str(first_midpoint),
                "no_trade_final_equity_quote": _decimal_str(no_trade_final_equity),
                "buy_hold_final_equity_quote": _decimal_str(
                    buy_hold_base * final_midpoint
                ),
                "book_contract_discovery": contract_report[
                    "book_contract_discovery"
                ],
                "gridbot_engine_source_sha256": gridbot_engine_source_sha256,
                "config_sha256": config_sha256,
            }
        )
        fills_path = run_dir / "fills.jsonl"
        order_events_path = run_dir / "order_events.jsonl"
        equity_curve_path = run_dir / "equity_curve.jsonl"
        write_jsonl(fills_path, strict_result["fills"])
        write_jsonl(order_events_path, strict_result["order_events"])
        write_jsonl(equity_curve_path, strict_result["equity_curve"])
        summary["output_artifacts"] = {
            "config": _output_artifact_report(config_path),
            "fills": _output_artifact_report(fills_path, len(strict_result["fills"])),
            "order_events": _output_artifact_report(
                order_events_path,
                len(strict_result["order_events"]),
            ),
            "equity_curve": _output_artifact_report(
                equity_curve_path,
                len(strict_result["equity_curve"]),
            ),
        }
        (run_dir / "summary.json").write_text(json.dumps(summary, indent=2, sort_keys=True), encoding="utf-8")
        report_path.write_text(json.dumps(summary, indent=2, sort_keys=True), encoding="utf-8")
        return {
            "run_id": run_id,
            "run_dir": str(run_dir),
            "report_path": str(report_path),
            "summary": summary,
        }

    candles, candle_report = load_fallback_candles(
        derived_root=derived_root,
        product_id=product_id,
        start=start,
        end=end,
        providers=providers,
        max_rows=max_rows,
    )
    result = simulate_gridbot_on_candles(candles, config, fee_profile=fee_profile)
    if _sha256_file(Path(__file__)) != gridbot_engine_source_sha256:
        raise RuntimeError("gridbot engine source changed during the candle run")
    result["summary"]["candle_input_report"] = candle_report
    result["summary"]["run_id"] = run_id
    result["summary"]["selected_candle_rows_sha256"] = _sha256_json(candles)
    result["summary"]["gridbot_engine_source_sha256"] = gridbot_engine_source_sha256
    result["summary"]["config_sha256"] = config_sha256
    fills_path = run_dir / "fills.jsonl"
    equity_curve_path = run_dir / "equity_curve.jsonl"
    write_jsonl(fills_path, result["fills"])
    write_jsonl(equity_curve_path, result["equity_curve"])
    result["summary"]["output_artifacts"] = {
        "config": _output_artifact_report(config_path),
        "fills": _output_artifact_report(fills_path, len(result["fills"])),
        "equity_curve": _output_artifact_report(
            equity_curve_path,
            len(result["equity_curve"]),
        ),
    }
    (run_dir / "summary.json").write_text(
        json.dumps(result["summary"], indent=2, sort_keys=True),
        encoding="utf-8",
    )
    report_path.write_text(json.dumps(result["summary"], indent=2, sort_keys=True), encoding="utf-8")
    return {
        "run_id": run_id,
        "run_dir": str(run_dir),
        "report_path": str(report_path),
        "summary": result["summary"],
    }
