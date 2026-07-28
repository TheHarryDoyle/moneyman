"""Research-only banded lot gridbot with a protected, traceable reserve ledger.

The engine uses one shared cash balance and per-band ceilings on cash currently
awaiting recovery. Completed residual base stays tagged to its purchase lot and
originating band. It is marked to market but never reused as cash in v1.
Diagnostic-only recovery, markout, adverse-excursion, and cash-cost-time state
is isolated from trading decisions and finalized only after replay.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from bisect import bisect_left
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_CEILING, ROUND_FLOOR
from pathlib import Path
from typing import Any

from .coinbase import normalize_product_id, utc_now_id
from .fees import FeeAccumulator, FeeProfile, fee_profile_to_report, manual_fee_profile, resolve_fee_profile
from .gridbot import load_fallback_candles
from .raw import write_jsonl


ENGINE_VERSION = "banded_lot_reserve_gridbot_v1.4"
SELECTED_CANDLE_HASH_SCHEMA = "canonical-json-lines-sort-keys-v1"
LOT_DIAGNOSTIC_SCHEMA = "price-only-lot-recovery-v1"
MARKOUT_HORIZONS_SECONDS = (
    ("1h", 60 * 60),
    ("6h", 6 * 60 * 60),
    ("24h", 24 * 60 * 60),
    ("7d", 7 * 24 * 60 * 60),
)
RECOVERY_WINDOWS_SECONDS = (
    ("7d", 7 * 24 * 60 * 60),
    ("14d", 14 * 24 * 60 * 60),
    ("28d", 28 * 24 * 60 * 60),
)
CANDLE_GAP_TOLERANCE_MULTIPLIER = Decimal("1.5")


@dataclass(frozen=True)
class ReserveGridConfig:
    product_id: str
    lower: str
    upper: str
    band_width: str
    levels_per_band: int
    band_active_lot_budget_cap: str
    quote_start: str
    exit_move_pct: str
    cash_profit_bps: str
    base_increment: str
    quote_increment: str
    price_increment: str
    min_quote_notional: str
    fee_rate: str
    include_fallback_candles: bool
    candle_path_assumption: str
    exit_policy: str = "principal_recovery"
    overflow_global_active_lot_budget_cap: str = "0"
    entry_guard: str = "none"
    entry_guard_fast_ema_span_candles: int = 360
    entry_guard_slow_ema_span_candles: int = 1440
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


@dataclass(frozen=True)
class EntryGuardSnapshot:
    status: str
    allows_new_buys: bool
    signal_as_of_ts: str | None
    prior_close_count: int
    fast_ema: Decimal | None
    slow_ema: Decimal | None
    gap_seconds: Decimal | None


@dataclass(frozen=True)
class BandDefinition:
    band_id: str
    index: int
    lower: Decimal
    upper: Decimal
    active_lot_budget_cap: Decimal


@dataclass
class BandLedger:
    definition: BandDefinition
    active_cash_cost: Decimal = Decimal("0")
    base_active_cash_cost: Decimal = Decimal("0")
    overflow_active_cash_cost: Decimal = Decimal("0")
    max_active_cash_cost: Decimal = Decimal("0")
    max_base_active_cash_cost: Decimal = Decimal("0")
    max_overflow_active_cash_cost: Decimal = Decimal("0")
    realized_cash_profit: Decimal = Decimal("0")
    base_realized_cash_profit: Decimal = Decimal("0")
    overflow_realized_cash_profit: Decimal = Decimal("0")
    reserve_base: Decimal = Decimal("0")
    base_tranche_reserve_base: Decimal = Decimal("0")
    overflow_tranche_reserve_base: Decimal = Decimal("0")
    completed_lots: int = 0
    base_completed_lots: int = 0
    overflow_completed_lots: int = 0
    open_lots: int = 0
    filled_buys: int = 0
    base_filled_buys: int = 0
    overflow_filled_buys: int = 0
    missed_global_cash: int = 0
    missed_band_cap: int = 0
    missed_overflow_global_cap: int = 0
    missed_entry_guard: int = 0
    base_missed_entry_guard: int = 0
    overflow_missed_entry_guard: int = 0
    missed_entry_guard_downtrend: int = 0
    missed_entry_guard_warmup: int = 0
    missed_entry_guard_stale: int = 0
    disabled_infeasible_slots: int = 0


@dataclass
class SlotState:
    slot_id: str
    band_id: str
    level_index: int
    entry_price: Decimal
    cash_budget: Decimal
    tranche: str = "base"
    armed: bool = False
    disabled: bool = False
    disabled_reason: str | None = None
    open_lot_id: str | None = None
    cycles: int = 0


@dataclass
class LotRecord:
    lot_id: str
    slot_id: str
    band_id: str
    level_index: int
    tranche: str
    entry_ts: str
    entry_price: Decimal
    cash_budget: Decimal
    buy_notional: Decimal
    gross_buy_fee: Decimal
    buy_rebate: Decimal
    cash_cost: Decimal
    base_quantity: Decimal
    target_exit_price: Decimal
    target_cash_profit: Decimal
    planned_sell_quantity: Decimal
    planned_reserve_quantity: Decimal
    status: str = "open"
    exit_ts: str | None = None
    sell_quantity: Decimal = Decimal("0")
    gross_sell_notional: Decimal = Decimal("0")
    gross_sell_fee: Decimal = Decimal("0")
    sell_rebate: Decimal = Decimal("0")
    net_sell_proceeds: Decimal = Decimal("0")
    actual_cash_profit: Decimal = Decimal("0")
    reserve_quantity: Decimal = Decimal("0")
    sold_cost_basis: Decimal = Decimal("0")
    reserve_cost_basis: Decimal = Decimal("0")
    realized_pnl_sold_portion: Decimal = Decimal("0")
    holding_seconds: Decimal | None = None


@dataclass
class ReservePortfolioState:
    quote_cash: Decimal
    initial_quote_cash: Decimal
    active_cash_cost: Decimal = Decimal("0")
    base_active_cash_cost: Decimal = Decimal("0")
    overflow_active_cash_cost: Decimal = Decimal("0")
    open_base: Decimal = Decimal("0")
    reserve_base: Decimal = Decimal("0")
    base_tranche_reserve_base: Decimal = Decimal("0")
    overflow_tranche_reserve_base: Decimal = Decimal("0")
    event_sequence: int = 0
    lots_created: int = 0
    completed_lots: int = 0
    base_completed_lots: int = 0
    overflow_completed_lots: int = 0
    filled_buys: int = 0
    base_filled_buys: int = 0
    overflow_filled_buys: int = 0
    filled_sells: int = 0
    base_filled_sells: int = 0
    overflow_filled_sells: int = 0
    missed_global_cash: int = 0
    missed_band_cap: int = 0
    missed_overflow_global_cap: int = 0
    missed_entry_guard: int = 0
    base_missed_entry_guard: int = 0
    overflow_missed_entry_guard: int = 0
    missed_entry_guard_downtrend: int = 0
    missed_entry_guard_warmup: int = 0
    missed_entry_guard_stale: int = 0
    disabled_infeasible_slots: int = 0
    realized_cash_profit: Decimal = Decimal("0")
    base_realized_cash_profit: Decimal = Decimal("0")
    overflow_realized_cash_profit: Decimal = Decimal("0")
    turnover_quote: Decimal = Decimal("0")
    max_active_cash_cost: Decimal = Decimal("0")
    max_base_active_cash_cost: Decimal = Decimal("0")
    max_overflow_active_cash_cost: Decimal = Decimal("0")
    max_reserve_value_quote: Decimal = Decimal("0")
    utilization_sum: Decimal = Decimal("0")
    overflow_utilization_sum: Decimal = Decimal("0")
    utilization_observations: int = 0
    events: list[dict[str, Any]] = field(default_factory=list)


def _decimal(value: Any, name: str) -> Decimal:
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"{name} must be a decimal number") from exc
    if not parsed.is_finite():
        raise ValueError(f"{name} must be finite")
    return parsed


def _decimal_str(value: Decimal) -> str:
    return format(value.normalize(), "f")


def _floor_to_increment(value: Decimal, increment: Decimal) -> Decimal:
    if increment <= 0:
        raise ValueError("increment must be positive")
    return (value / increment).to_integral_value(rounding=ROUND_FLOOR) * increment


def _ceil_to_increment(value: Decimal, increment: Decimal) -> Decimal:
    if increment <= 0:
        raise ValueError("increment must be positive")
    return (value / increment).to_integral_value(rounding=ROUND_CEILING) * increment


def _is_increment_aligned(value: Decimal, increment: Decimal) -> bool:
    if increment <= 0:
        raise ValueError("increment must be positive")
    return value % increment == 0


def _parse_ts(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _seconds_between(start: str, end: str) -> Decimal:
    return Decimal(str((_parse_ts(end) - _parse_ts(start)).total_seconds()))


def _format_ts(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _timeframe_seconds(value: str) -> Decimal:
    text = value.strip().lower()
    if not text:
        raise ValueError("candle timeframe cannot be empty")
    unit = text[-1]
    multipliers = {
        "s": Decimal("1"),
        "m": Decimal("60"),
        "h": Decimal("3600"),
        "d": Decimal("86400"),
    }
    if unit not in multipliers:
        raise ValueError(f"unsupported candle timeframe: {value}")
    amount = _decimal(text[:-1], "candle timeframe")
    seconds = amount * multipliers[unit]
    if seconds <= 0:
        raise ValueError("candle timeframe must be positive")
    return seconds


def _median_decimal(values: list[Decimal]) -> Decimal:
    if not values:
        return Decimal("0")
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) / Decimal("2")


def _decimal_statistics(values: list[Decimal]) -> dict[str, Any]:
    if not values:
        return {
            "count": 0,
            "average": None,
            "median": None,
            "minimum": None,
            "maximum": None,
        }
    return {
        "count": len(values),
        "average": _decimal_str(sum(values, Decimal("0")) / Decimal(len(values))),
        "median": _decimal_str(_median_decimal(values)),
        "minimum": _decimal_str(min(values)),
        "maximum": _decimal_str(max(values)),
    }


def _interval_has_coverage_gap(
    start: datetime,
    end: datetime,
    coverage_gaps: list[tuple[datetime, datetime, Decimal]],
) -> bool:
    if end <= start:
        return False
    return any(gap_start < end and gap_end > start for gap_start, gap_end, _ in coverage_gaps)


def _active_cash_cost_time_from_events(
    events: list[dict[str, Any]],
    start_ts: str,
    end_ts: str,
) -> Decimal:
    current_ts = _parse_ts(start_ts)
    final_ts = _parse_ts(end_ts)
    active_cash_cost = Decimal("0")
    quote_seconds = Decimal("0")
    for event in events:
        event_ts = _parse_ts(str(event["ts"]))
        if event_ts < current_ts:
            raise AssertionError("diagnostic events are not chronological")
        quote_seconds += active_cash_cost * Decimal(
            str((event_ts - current_ts).total_seconds())
        )
        active_cash_cost = _decimal(event["active_cash_cost"], "event active_cash_cost")
        current_ts = event_ts
    if final_ts < current_ts:
        raise AssertionError("diagnostic end timestamp precedes the event stream")
    quote_seconds += active_cash_cost * Decimal(
        str((final_ts - current_ts).total_seconds())
    )
    return quote_seconds


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _selected_derived_sources_with_hashes(
    candle_report: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    if candle_report is None:
        return []
    rows: list[dict[str, Any]] = []
    for source in candle_report.get("selected_derived_sources", []):
        source_path = Path(str(source["derived_path"]))
        rows.append(
            {
                **source,
                "size_bytes": (
                    source_path.stat().st_size if source_path.exists() else None
                ),
                "sha256": _sha256(source_path) if source_path.is_file() else None,
            }
        )
    return rows


def _selected_candle_rows_sha256(candles: list[dict[str, Any]]) -> str:
    """Fingerprint the exact ordered candle rows consumed by a backtest."""

    digest = hashlib.sha256()
    for candle in candles:
        encoded = json.dumps(
            candle,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            default=str,
        ).encode("utf-8")
        digest.update(encoded)
        digest.update(b"\n")
    return digest.hexdigest()


def _new_run_id() -> str:
    return f"{utc_now_id()}-{uuid.uuid4().hex[:8]}"


def _trade_decision_fingerprint(events: list[dict[str, Any]], tranche: str) -> str:
    decision_events = []
    for event in events:
        if event.get("tranche") != tranche:
            continue
        if event.get("event") not in {"buy_filled", "exit_filled", "buy_missed", "slot_disabled"}:
            continue
        decision_events.append(
            {
                "ts": event.get("ts"),
                "event": event.get("event"),
                "reason": event.get("reason"),
                "slot_id": event.get("slot_id"),
                "band_id": event.get("band_id"),
                "tranche": event.get("tranche"),
                "entry_price": event.get("entry_price"),
                "exit_price": event.get("exit_price"),
            }
        )
    encoded = json.dumps(
        decision_events,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _fee_profile_from_config(config: ReserveGridConfig) -> FeeProfile:
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


def build_bands_and_slots(config: ReserveGridConfig) -> tuple[list[BandDefinition], list[SlotState]]:
    lower = _decimal(config.lower, "lower")
    upper = _decimal(config.upper, "upper")
    band_width = _decimal(config.band_width, "band_width")
    band_cap = _decimal(config.band_active_lot_budget_cap, "band_active_lot_budget_cap")
    overflow_cap = _decimal(
        config.overflow_global_active_lot_budget_cap,
        "overflow_global_active_lot_budget_cap",
    )
    quote_increment = _decimal(config.quote_increment, "quote_increment")
    price_increment = _decimal(config.price_increment, "price_increment")
    if lower <= 0 or upper <= lower:
        raise ValueError("reserve grid requires 0 < lower < upper")
    if band_width <= 0:
        raise ValueError("band_width must be positive")
    if band_cap <= 0:
        raise ValueError("band_active_lot_budget_cap must be positive")
    if overflow_cap < 0:
        raise ValueError("overflow_global_active_lot_budget_cap cannot be negative")
    if config.levels_per_band < 1:
        raise ValueError("levels_per_band must be at least 1")
    if quote_increment <= 0:
        raise ValueError("quote_increment must be positive")
    if price_increment <= 0:
        raise ValueError("price_increment must be positive")
    if not all(_is_increment_aligned(value, price_increment) for value in (lower, upper, band_width)):
        raise ValueError("lower, upper, and band_width must align exactly to price_increment")
    band_count_decimal = (upper - lower) / band_width
    band_count = int(band_count_decimal)
    if Decimal(band_count) != band_count_decimal:
        raise ValueError("upper - lower must be exactly divisible by band_width")

    band_width_ticks = int(band_width / price_increment)
    if band_width_ticks % config.levels_per_band != 0:
        raise ValueError("band_width / levels_per_band must align exactly to price_increment")
    level_step = price_increment * Decimal(band_width_ticks // config.levels_per_band)

    cash_budget = _floor_to_increment(band_cap / Decimal(config.levels_per_band), quote_increment)
    if cash_budget <= 0:
        raise ValueError("band cap is too small for levels_per_band and quote_increment")
    bands: list[BandDefinition] = []
    slots: list[SlotState] = []
    global_level = 0
    for band_index in range(band_count):
        band_lower = lower + (band_width * Decimal(band_index))
        band_upper = band_lower + band_width
        band_id = f"band-{band_index:03d}"
        definition = BandDefinition(
            band_id=band_id,
            index=band_index,
            lower=band_lower,
            upper=band_upper,
            active_lot_budget_cap=band_cap,
        )
        bands.append(definition)
        for level_in_band in range(config.levels_per_band):
            price = band_lower + (level_step * Decimal(level_in_band))
            if not _is_increment_aligned(price, price_increment):
                raise AssertionError("generated entry price did not align to price_increment")
            slots.append(
                SlotState(
                    slot_id=f"slot-{global_level:04d}",
                    band_id=band_id,
                    level_index=global_level,
                    entry_price=price,
                    cash_budget=cash_budget,
                    tranche="base",
                )
            )
            if overflow_cap > 0:
                slots.append(
                    SlotState(
                        slot_id=f"overflow-slot-{global_level:04d}",
                        band_id=band_id,
                        level_index=global_level,
                        entry_price=price,
                        cash_budget=cash_budget,
                        tranche="overflow",
                    )
                )
            global_level += 1
    return bands, slots


def band_for_price(price: Decimal, bands: list[BandDefinition]) -> BandDefinition | None:
    for band in bands:
        if band.lower <= price < band.upper:
            return band
    return None


def plan_buy(
    entry_price: Decimal,
    cash_budget: Decimal,
    gross_buy_fee_rate: Decimal,
    base_increment: Decimal,
    min_quote_notional: Decimal,
) -> dict[str, Decimal]:
    if gross_buy_fee_rate < 0 or gross_buy_fee_rate >= 1:
        raise ValueError("gross_buy_fee_rate must be at least 0 and less than 1")
    maximum_notional = cash_budget / (Decimal("1") + gross_buy_fee_rate)
    base_quantity = _floor_to_increment(maximum_notional / entry_price, base_increment)
    buy_notional = base_quantity * entry_price
    gross_buy_fee = buy_notional * gross_buy_fee_rate
    cash_cost = buy_notional + gross_buy_fee
    if base_quantity <= 0:
        raise ValueError("cash budget buys less than one base increment")
    if buy_notional < min_quote_notional:
        raise ValueError("cash budget is below min_quote_notional after rounding")
    if cash_cost > cash_budget:
        raise AssertionError("rounded buy cash cost exceeded its all-in cash budget")
    return {
        "base_quantity": base_quantity,
        "buy_notional": buy_notional,
        "gross_buy_fee": gross_buy_fee,
        "cash_cost": cash_cost,
    }


def plan_exit(
    *,
    cash_cost: Decimal,
    base_quantity: Decimal,
    target_exit_price: Decimal,
    cash_profit_bps: Decimal,
    gross_sell_fee_rate: Decimal,
    base_increment: Decimal,
    exit_policy: str,
) -> dict[str, Decimal | bool | str]:
    if gross_sell_fee_rate < 0 or gross_sell_fee_rate >= 1:
        raise ValueError("gross_sell_fee_rate must be at least 0 and less than 1")
    target_cash_profit = cash_cost * cash_profit_bps / Decimal("10000")
    if exit_policy == "full_lot":
        sell_quantity = base_quantity
    elif exit_policy == "principal_recovery":
        required_net_cash = cash_cost + target_cash_profit
        raw_sell_quantity = required_net_cash / (
            target_exit_price * (Decimal("1") - gross_sell_fee_rate)
        )
        sell_quantity = _ceil_to_increment(raw_sell_quantity, base_increment)
    else:
        raise ValueError("exit_policy must be principal_recovery or full_lot")

    gross_sell_notional = sell_quantity * target_exit_price
    gross_sell_fee = gross_sell_notional * gross_sell_fee_rate
    net_sell_proceeds = gross_sell_notional - gross_sell_fee
    reserve_quantity = base_quantity - sell_quantity
    feasible = sell_quantity <= base_quantity and reserve_quantity >= 0
    reason = "ok"
    if not feasible:
        reason = "sell_quantity_exceeds_bought_base"
    elif exit_policy == "principal_recovery" and reserve_quantity < base_increment:
        feasible = False
        reason = "reserve_below_one_base_increment"
    elif exit_policy == "principal_recovery" and net_sell_proceeds < cash_cost + target_cash_profit:
        feasible = False
        reason = "rounded_exit_misses_cash_target"
    return {
        "target_cash_profit": target_cash_profit,
        "sell_quantity": sell_quantity,
        "gross_sell_notional": gross_sell_notional,
        "gross_sell_fee": gross_sell_fee,
        "net_sell_proceeds": net_sell_proceeds,
        "reserve_quantity": reserve_quantity,
        "feasible": feasible,
        "reason": reason,
    }


def _lot_to_row(lot: LotRecord) -> dict[str, Any]:
    row = asdict(lot)
    for key, value in list(row.items()):
        if isinstance(value, Decimal):
            row[key] = _decimal_str(value)
    return row


def _build_lot_recovery_diagnostics(
    *,
    lots: dict[str, LotRecord],
    minimum_path_prices: dict[str, Decimal],
    close_observations: dict[str, tuple[Decimal, int]],
    validated_candles: list[
        tuple[dict[str, Any], str, Decimal, Decimal, Decimal, Decimal]
    ],
    events: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    candle_times = [_parse_ts(row[1]) for row in validated_candles]
    candle_closes = [row[5] for row in validated_candles]
    first_ts = validated_candles[0][1]
    final_ts = validated_candles[-1][1]
    final_time = candle_times[-1]
    candle_intervals = [
        Decimal(str((right - left).total_seconds()))
        for left, right in zip(candle_times, candle_times[1:])
    ]
    declared_timeframes = sorted(
        {
            str(row[0].get("timeframe")).strip().lower()
            for row in validated_candles
            if row[0].get("timeframe")
        }
    )
    if len(declared_timeframes) > 1:
        raise ValueError("reserve-grid diagnostic candles must share one timeframe")
    if declared_timeframes:
        declared_timeframe = declared_timeframes[0]
        expected_interval = _timeframe_seconds(declared_timeframe)
        timeframe_source = "candle.timeframe"
    else:
        declared_timeframe = "1m"
        expected_interval = Decimal("60")
        timeframe_source = "reserve_grid_fallback_default"
    observed_median_interval = _median_decimal(candle_intervals)
    gap_tolerance = expected_interval * CANDLE_GAP_TOLERANCE_MULTIPLIER
    coverage_gaps: list[tuple[datetime, datetime, Decimal]] = []
    if gap_tolerance > 0:
        for left, right, interval in zip(
            candle_times,
            candle_times[1:],
            candle_intervals,
        ):
            if interval > gap_tolerance:
                coverage_gaps.append((left, right, interval))

    diagnostics: list[dict[str, Any]] = []
    total_observed_seconds = Decimal("0")
    total_cash_cost_time = Decimal("0")
    path_mae_values: list[Decimal] = []
    close_mae_values: list[Decimal] = []
    coverage_complete_lots = 0
    coverage_incomplete_lots = 0
    markout_values: dict[str, list[Decimal]] = {
        label: [] for label, _ in MARKOUT_HORIZONS_SECONDS
    }
    markout_status_counts: dict[str, dict[str, int]] = {
        label: {
            "observed": 0,
            "delayed_by_data_gap": 0,
            "right_censored": 0,
            "negative": 0,
        }
        for label, _ in MARKOUT_HORIZONS_SECONDS
    }
    recovery_counts: dict[str, dict[str, int]] = {
        label: {
            "eligible": 0,
            "recovered": 0,
            "known_failure": 0,
            "right_censored": 0,
            "data_gap_unknown": 0,
            "recovered_but_ineligible": 0,
        }
        for label, _ in RECOVERY_WINDOWS_SECONDS
    }

    for lot in sorted(lots.values(), key=lambda item: item.lot_id):
        entry_time = _parse_ts(lot.entry_ts)
        entry_timestamp_is_gap_right_boundary = any(
            gap_end == entry_time for _, gap_end, _ in coverage_gaps
        )
        completed = lot.status == "completed"
        observation_end_ts = lot.exit_ts if completed else final_ts
        if observation_end_ts is None:
            raise AssertionError("completed diagnostic lot is missing exit_ts")
        observation_end = _parse_ts(observation_end_ts)
        observation_seconds = Decimal(
            str((observation_end - entry_time).total_seconds())
        )
        if observation_seconds < 0:
            raise AssertionError("lot diagnostic observation duration went negative")
        recovery_seconds = lot.holding_seconds if completed else None
        minimum_path_price = minimum_path_prices.get(lot.lot_id, lot.entry_price)
        if minimum_path_price > lot.entry_price:
            raise AssertionError("lot diagnostic minimum exceeded its entry price")
        path_mae_bps = max(
            Decimal("0"),
            (lot.entry_price - minimum_path_price)
            / lot.entry_price
            * Decimal("10000"),
        )
        close_observation = close_observations.get(lot.lot_id)
        minimum_close = close_observation[0] if close_observation else None
        close_sample_count = close_observation[1] if close_observation else 0
        close_mae_bps = (
            max(
                Decimal("0"),
                (lot.entry_price - minimum_close)
                / lot.entry_price
                * Decimal("10000"),
            )
            if minimum_close is not None
            else None
        )
        path_coverage_complete = (
            not entry_timestamp_is_gap_right_boundary
            and not _interval_has_coverage_gap(
                entry_time,
                observation_end,
                coverage_gaps,
            )
        )
        if path_coverage_complete:
            coverage_complete_lots += 1
            path_mae_values.append(path_mae_bps)
            if close_mae_bps is not None:
                close_mae_values.append(close_mae_bps)
        else:
            coverage_incomplete_lots += 1

        recovery_windows: dict[str, dict[str, Any]] = {}
        for label, horizon_seconds in RECOVERY_WINDOWS_SECONDS:
            deadline = entry_time + timedelta(seconds=horizon_seconds)
            run_reaches_deadline = final_time >= deadline
            deadline_has_gap = (
                entry_timestamp_is_gap_right_boundary
                or _interval_has_coverage_gap(
                    entry_time,
                    deadline,
                    coverage_gaps,
                )
            )
            recovered_by_deadline = bool(
                completed
                and lot.exit_ts is not None
                and _parse_ts(lot.exit_ts) <= deadline
            )
            full_followup_eligible = run_reaches_deadline and not deadline_has_gap
            if recovered_by_deadline:
                status = "recovered"
                recovered: bool | None = True
                if full_followup_eligible:
                    recovery_counts[label]["eligible"] += 1
                    recovery_counts[label]["recovered"] += 1
                else:
                    recovery_counts[label]["recovered_but_ineligible"] += 1
            elif not run_reaches_deadline:
                status = "right_censored"
                recovered = None
                recovery_counts[label]["right_censored"] += 1
            elif deadline_has_gap:
                status = "data_gap_unknown"
                recovered = None
                recovery_counts[label]["data_gap_unknown"] += 1
            else:
                status = "not_recovered"
                recovered = False
                recovery_counts[label]["eligible"] += 1
                recovery_counts[label]["known_failure"] += 1
            recovery_windows[label] = {
                "horizon_seconds": str(horizon_seconds),
                "deadline_ts": _format_ts(deadline),
                "status": status,
                "recovered": recovered,
                "full_followup_eligible": full_followup_eligible,
                "coverage_complete_through_deadline": (
                    run_reaches_deadline and not deadline_has_gap
                ),
            }

        markouts: dict[str, dict[str, Any]] = {}
        for label, horizon_seconds in MARKOUT_HORIZONS_SECONDS:
            target_time = entry_time + timedelta(seconds=horizon_seconds)
            sample_index = bisect_left(candle_times, target_time)
            if sample_index >= len(candle_times):
                status = "right_censored"
                observation_ts = None
                observation_lag_seconds = None
                close_value = None
                price_change_bps = None
            else:
                sample_time = candle_times[sample_index]
                observation_ts = validated_candles[sample_index][1]
                observation_lag = Decimal(
                    str((sample_time - target_time).total_seconds())
                )
                observation_lag_seconds = _decimal_str(observation_lag)
                close_value = candle_closes[sample_index]
                if observation_lag == 0:
                    status = "observed"
                    price_change_bps = (
                        (close_value / lot.entry_price) - Decimal("1")
                    ) * Decimal("10000")
                    markout_values[label].append(price_change_bps)
                    markout_status_counts[label]["observed"] += 1
                    if price_change_bps < 0:
                        markout_status_counts[label]["negative"] += 1
                else:
                    status = "delayed_by_data_gap"
                    price_change_bps = None
                    markout_status_counts[label]["delayed_by_data_gap"] += 1
            if status == "right_censored":
                markout_status_counts[label]["right_censored"] += 1
            markouts[label] = {
                "horizon_seconds": str(horizon_seconds),
                "target_ts": _format_ts(target_time),
                "status": status,
                "observation_ts": observation_ts,
                "observation_lag_seconds": observation_lag_seconds,
                "close": _decimal_str(close_value) if close_value is not None else None,
                "price_change_bps": (
                    _decimal_str(price_change_bps)
                    if price_change_bps is not None
                    else None
                ),
            }

        cash_cost_time = lot.cash_cost * observation_seconds
        total_observed_seconds += observation_seconds
        total_cash_cost_time += cash_cost_time
        diagnostics.append(
            {
                "schema": LOT_DIAGNOSTIC_SCHEMA,
                "lot_id": lot.lot_id,
                "slot_id": lot.slot_id,
                "band_id": lot.band_id,
                "tranche": lot.tranche,
                "status": (
                    "completed_recovered" if completed else "open_right_censored"
                ),
                "entry_ts": lot.entry_ts,
                "entry_price": _decimal_str(lot.entry_price),
                "target_exit_price": _decimal_str(lot.target_exit_price),
                "cash_cost": _decimal_str(lot.cash_cost),
                "exit_ts": lot.exit_ts,
                "recovery_seconds": (
                    _decimal_str(recovery_seconds)
                    if recovery_seconds is not None
                    else None
                ),
                "diagnostic_observation_end_ts": observation_end_ts,
                "diagnostic_observation_seconds": _decimal_str(
                    observation_seconds
                ),
                "open_duration_is_right_censored_lower_bound": not completed,
                "cash_cost_time_quote_seconds": _decimal_str(cash_cost_time),
                "cash_cost_time_quote_hours": _decimal_str(
                    cash_cost_time / Decimal("3600")
                ),
                "cash_cost_time_quote_days": _decimal_str(
                    cash_cost_time / Decimal("86400")
                ),
                "minimum_assumed_path_price_while_open": _decimal_str(
                    minimum_path_price
                ),
                "path_assumed_maximum_adverse_excursion_bps": _decimal_str(
                    path_mae_bps
                ),
                "minimum_close_while_open": (
                    _decimal_str(minimum_close)
                    if minimum_close is not None
                    else None
                ),
                "close_sample_count_while_open": close_sample_count,
                "close_sampled_maximum_adverse_excursion_bps": (
                    _decimal_str(close_mae_bps)
                    if close_mae_bps is not None
                    else None
                ),
                "candle_coverage_complete_to_observation_end": path_coverage_complete,
                "entry_timestamp_is_gap_right_boundary": (
                    entry_timestamp_is_gap_right_boundary
                ),
                "recovery_windows": recovery_windows,
                "price_only_close_markouts": markouts,
            }
        )

    active_cost_time_from_events = _active_cash_cost_time_from_events(
        events,
        first_ts,
        final_ts,
    )
    active_cost_time_error = total_cash_cost_time - active_cost_time_from_events
    completed_diagnostics = sum(
        1 for diagnostic in diagnostics if diagnostic["status"] == "completed_recovered"
    )
    if len(diagnostics) != len(lots):
        raise AssertionError("lot diagnostic row count disagreed with lot count")
    if completed_diagnostics != sum(
        1 for lot in lots.values() if lot.status == "completed"
    ):
        raise AssertionError("lot diagnostic recovery count disagreed with lots")
    if active_cost_time_error != 0:
        raise AssertionError("lot diagnostic active-cash time reconciliation failed")

    recovery_summary: dict[str, dict[str, Any]] = {}
    for label, horizon_seconds in RECOVERY_WINDOWS_SECONDS:
        counts = recovery_counts[label]
        eligible = counts["eligible"]
        recovery_summary[label] = {
            "horizon_seconds": str(horizon_seconds),
            "eligible_lots": eligible,
            "recovered_lots": counts["recovered"],
            "known_failure_lots": counts["known_failure"],
            "right_censored_lots": counts["right_censored"],
            "data_gap_unknown_lots": counts["data_gap_unknown"],
            "recovered_but_ineligible_lots": counts["recovered_but_ineligible"],
            "recovery_rate_among_eligible": (
                _decimal_str(Decimal(counts["recovered"]) / Decimal(eligible))
                if eligible
                else None
            ),
        }

    markout_summary: dict[str, dict[str, Any]] = {}
    for label, horizon_seconds in MARKOUT_HORIZONS_SECONDS:
        counts = markout_status_counts[label]
        observed = counts["observed"]
        markout_summary[label] = {
            "horizon_seconds": str(horizon_seconds),
            "observed_lots": observed,
            "delayed_by_data_gap_lots": counts["delayed_by_data_gap"],
            "right_censored_lots": counts["right_censored"],
            "negative_markout_lots": counts["negative"],
            "negative_rate_among_observed": (
                _decimal_str(Decimal(counts["negative"]) / Decimal(observed))
                if observed
                else None
            ),
            "price_change_bps": _decimal_statistics(markout_values[label]),
        }

    summary = {
        "schema": LOT_DIAGNOSTIC_SCHEMA,
        "diagnostic_only": True,
        "used_by_trade_decisions": False,
        "lots_observed": len(diagnostics),
        "completed_recovered_lots": completed_diagnostics,
        "open_right_censored_lots": len(diagnostics) - completed_diagnostics,
        "total_diagnostic_observation_seconds": _decimal_str(
            total_observed_seconds
        ),
        "total_observed_lot_days": _decimal_str(
            total_observed_seconds / Decimal("86400")
        ),
        "cash_cost_time_quote_seconds": _decimal_str(total_cash_cost_time),
        "cash_cost_time_quote_hours": _decimal_str(
            total_cash_cost_time / Decimal("3600")
        ),
        "cash_cost_time_quote_days": _decimal_str(
            total_cash_cost_time / Decimal("86400")
        ),
        "active_cash_cost_time_from_events_quote_seconds": _decimal_str(
            active_cost_time_from_events
        ),
        "active_cash_cost_time_reconciliation_error_quote_seconds": _decimal_str(
            active_cost_time_error
        ),
        "adverse_excursion_coverage_complete_lots": coverage_complete_lots,
        "adverse_excursion_coverage_incomplete_lots": coverage_incomplete_lots,
        "path_assumed_maximum_adverse_excursion_bps_complete_coverage": (
            _decimal_statistics(path_mae_values)
        ),
        "close_sampled_maximum_adverse_excursion_bps_complete_coverage": (
            _decimal_statistics(close_mae_values)
        ),
        "recovery_windows": recovery_summary,
        "price_only_close_markouts": markout_summary,
        "candle_coverage": {
            "declared_timeframe": declared_timeframe,
            "timeframe_source": timeframe_source,
            "expected_interval_seconds": _decimal_str(expected_interval),
            "observed_median_interval_seconds": _decimal_str(
                observed_median_interval
            ),
            "gap_tolerance_multiplier": _decimal_str(
                CANDLE_GAP_TOLERANCE_MULTIPLIER
            ),
            "gap_tolerance_seconds": _decimal_str(gap_tolerance),
            "maximum_gap_seconds": _decimal_str(
                max(candle_intervals, default=Decimal("0"))
            ),
            "gaps_exceeding_tolerance": len(coverage_gaps),
        },
        "sampling_notes": [
            "Diagnostics never feed entry, exit, allocation, cap, or reserve decisions.",
            "Fill and recovery timestamps use candle start_ts; same-candle recovery is zero seconds.",
            "Open-lot duration ends at the final candle start_ts and is a right-censored lower bound.",
            "Path adverse excursion follows the configured assumed candle path, including close-to-open gaps.",
            "Close adverse excursion uses available closes from entry through, but not including, a completed lot's exit candle; incomplete-coverage lots are excluded from aggregate excursion statistics.",
            "Buy markouts require the exact horizon candle; a later close across a gap is reported but not scored.",
            "Recovery rates use only lots with full horizon follow-up and adequate candle coverage.",
            "A lot stamped at a gap's right boundary is conservatively coverage-incomplete because candle timestamps cannot prove whether the fill occurred on the gap leg or later inside that candle.",
        ],
    }
    return diagnostics, summary


def _band_to_row(
    ledger: BandLedger,
    final_mark: Decimal,
    lots: dict[str, LotRecord],
) -> dict[str, Any]:
    definition = ledger.definition
    band_lots = [lot for lot in lots.values() if lot.band_id == definition.band_id]
    open_lots = [lot for lot in band_lots if lot.status == "open"]
    completed_lots = [lot for lot in band_lots if lot.status == "completed"]
    base_open_lots = [lot for lot in open_lots if lot.tranche == "base"]
    overflow_open_lots = [lot for lot in open_lots if lot.tranche == "overflow"]
    base_completed_lots = [lot for lot in completed_lots if lot.tranche == "base"]
    overflow_completed_lots = [lot for lot in completed_lots if lot.tranche == "overflow"]
    open_base = sum((lot.base_quantity for lot in open_lots), Decimal("0"))
    open_cost_basis = sum((lot.cash_cost for lot in open_lots), Decimal("0"))
    reserve_cost_basis = sum(
        (lot.reserve_cost_basis for lot in completed_lots), Decimal("0")
    )
    realized_pnl = sum(
        (lot.realized_pnl_sold_portion for lot in completed_lots), Decimal("0")
    )
    return {
        "band_id": definition.band_id,
        "index": definition.index,
        "lower_inclusive": _decimal_str(definition.lower),
        "upper_exclusive": _decimal_str(definition.upper),
        "active_lot_budget_cap": _decimal_str(definition.active_lot_budget_cap),
        "base_active_lot_budget_cap": _decimal_str(definition.active_lot_budget_cap),
        "final_active_cash_cost": _decimal_str(ledger.active_cash_cost),
        "final_base_active_cash_cost": _decimal_str(ledger.base_active_cash_cost),
        "final_overflow_active_cash_cost": _decimal_str(ledger.overflow_active_cash_cost),
        "max_active_cash_cost": _decimal_str(ledger.max_active_cash_cost),
        "max_base_active_cash_cost": _decimal_str(ledger.max_base_active_cash_cost),
        "max_overflow_active_cash_cost": _decimal_str(ledger.max_overflow_active_cash_cost),
        "realized_cash_profit": _decimal_str(ledger.realized_cash_profit),
        "base_realized_cash_profit": _decimal_str(ledger.base_realized_cash_profit),
        "overflow_realized_cash_profit": _decimal_str(ledger.overflow_realized_cash_profit),
        "reserve_base": _decimal_str(ledger.reserve_base),
        "base_tranche_reserve_base": _decimal_str(ledger.base_tranche_reserve_base),
        "overflow_tranche_reserve_base": _decimal_str(ledger.overflow_tranche_reserve_base),
        "reserve_value_quote": _decimal_str(ledger.reserve_base * final_mark),
        "reserve_cost_basis": _decimal_str(reserve_cost_basis),
        "reserve_unrealized_pnl": _decimal_str((ledger.reserve_base * final_mark) - reserve_cost_basis),
        "open_base": _decimal_str(open_base),
        "base_tranche_open_base": _decimal_str(
            sum((lot.base_quantity for lot in base_open_lots), Decimal("0"))
        ),
        "overflow_tranche_open_base": _decimal_str(
            sum((lot.base_quantity for lot in overflow_open_lots), Decimal("0"))
        ),
        "open_base_value_quote": _decimal_str(open_base * final_mark),
        "open_cost_basis": _decimal_str(open_cost_basis),
        "open_unrealized_pnl": _decimal_str((open_base * final_mark) - open_cost_basis),
        "realized_pnl_sold_portion": _decimal_str(realized_pnl),
        "completed_lots": ledger.completed_lots,
        "base_completed_lots": len(base_completed_lots),
        "overflow_completed_lots": len(overflow_completed_lots),
        "open_lots": ledger.open_lots,
        "base_open_lots": len(base_open_lots),
        "overflow_open_lots": len(overflow_open_lots),
        "filled_buys": ledger.filled_buys,
        "base_filled_buys": ledger.base_filled_buys,
        "overflow_filled_buys": ledger.overflow_filled_buys,
        "missed_global_cash": ledger.missed_global_cash,
        "missed_band_cap": ledger.missed_band_cap,
        "missed_overflow_global_cap": ledger.missed_overflow_global_cap,
        "missed_entry_guard": ledger.missed_entry_guard,
        "base_missed_entry_guard": ledger.base_missed_entry_guard,
        "overflow_missed_entry_guard": ledger.overflow_missed_entry_guard,
        "missed_entry_guard_downtrend": ledger.missed_entry_guard_downtrend,
        "missed_entry_guard_warmup": ledger.missed_entry_guard_warmup,
        "missed_entry_guard_stale": ledger.missed_entry_guard_stale,
        "disabled_infeasible_slots": ledger.disabled_infeasible_slots,
    }


def _record_event(
    state: ReservePortfolioState,
    event: dict[str, Any],
    lots: dict[str, LotRecord],
    fees: FeeAccumulator,
) -> None:
    state.event_sequence += 1
    event.update(
        {
            "event_sequence": state.event_sequence,
            "quote_cash": _decimal_str(state.quote_cash),
            "active_cash_cost": _decimal_str(state.active_cash_cost),
            "base_active_cash_cost": _decimal_str(state.base_active_cash_cost),
            "overflow_active_cash_cost": _decimal_str(state.overflow_active_cash_cost),
            "open_base": _decimal_str(state.open_base),
            "reserve_base": _decimal_str(state.reserve_base),
            "base_tranche_reserve_base": _decimal_str(state.base_tranche_reserve_base),
            "overflow_tranche_reserve_base": _decimal_str(
                state.overflow_tranche_reserve_base
            ),
            "gross_fees_quote": _decimal_str(fees.gross_fees_quote),
            "modeled_rebate_receivable_quote": _decimal_str(fees.rebates_quote),
        }
    )
    state.events.append(event)


def _buy_slot(
    *,
    slot: SlotState,
    ts: str,
    state: ReservePortfolioState,
    band_ledgers: dict[str, BandLedger],
    lots: dict[str, LotRecord],
    config: ReserveGridConfig,
    fees: FeeAccumulator,
) -> None:
    base_increment = _decimal(config.base_increment, "base_increment")
    price_increment = _decimal(config.price_increment, "price_increment")
    min_quote_notional = _decimal(config.min_quote_notional, "min_quote_notional")
    cash_profit_bps = _decimal(config.cash_profit_bps, "cash_profit_bps")
    buy_rate = fees.rate_for(config.liquidity_assumption)
    sell_rate = fees.rate_for(config.liquidity_assumption)
    band = band_ledgers[slot.band_id]
    try:
        terms = plan_buy(
            entry_price=slot.entry_price,
            cash_budget=slot.cash_budget,
            gross_buy_fee_rate=buy_rate,
            base_increment=base_increment,
            min_quote_notional=min_quote_notional,
        )
    except ValueError as exc:
        reason = f"buy_plan_infeasible: {exc}"
        state.disabled_infeasible_slots += 1
        band.disabled_infeasible_slots += 1
        slot.armed = False
        slot.disabled = True
        slot.disabled_reason = reason
        _record_event(
            state,
            {
                "ts": ts,
                "event": "slot_disabled",
                "reason": reason,
                "slot_id": slot.slot_id,
                "band_id": slot.band_id,
                "tranche": slot.tranche,
                "entry_price": _decimal_str(slot.entry_price),
            },
            lots,
            fees,
        )
        return
    target_exit_price = _ceil_to_increment(
        slot.entry_price * (Decimal("1") + _decimal(config.exit_move_pct, "exit_move_pct")),
        price_increment,
    )
    planned_exit = plan_exit(
        cash_cost=terms["cash_cost"],
        base_quantity=terms["base_quantity"],
        target_exit_price=target_exit_price,
        cash_profit_bps=cash_profit_bps,
        gross_sell_fee_rate=sell_rate,
        base_increment=base_increment,
        exit_policy=config.exit_policy,
    )
    if not bool(planned_exit["feasible"]):
        state.disabled_infeasible_slots += 1
        band.disabled_infeasible_slots += 1
        slot.armed = False
        slot.disabled = True
        slot.disabled_reason = str(planned_exit["reason"])
        _record_event(
            state,
            {
                "ts": ts,
                "event": "slot_disabled",
                "reason": str(planned_exit["reason"]),
                "slot_id": slot.slot_id,
                "band_id": slot.band_id,
                "tranche": slot.tranche,
                "entry_price": _decimal_str(slot.entry_price),
            },
            lots,
            fees,
        )
        return
    cash_cost = terms["cash_cost"]
    if slot.tranche == "base" and (
        band.base_active_cash_cost + cash_cost > band.definition.active_lot_budget_cap
    ):
        state.missed_band_cap += 1
        band.missed_band_cap += 1
        slot.armed = False
        _record_event(
            state,
            {
                "ts": ts,
                "event": "buy_missed",
                "reason": "band_active_lot_budget_cap",
                "slot_id": slot.slot_id,
                "band_id": slot.band_id,
                "tranche": slot.tranche,
                "entry_price": _decimal_str(slot.entry_price),
                "required_cash": _decimal_str(cash_cost),
            },
            lots,
            fees,
        )
        return
    if slot.tranche == "overflow":
        overflow_cap = _decimal(
            config.overflow_global_active_lot_budget_cap,
            "overflow_global_active_lot_budget_cap",
        )
        if state.overflow_active_cash_cost + cash_cost > overflow_cap:
            state.missed_overflow_global_cap += 1
            band.missed_overflow_global_cap += 1
            slot.armed = False
            _record_event(
                state,
                {
                    "ts": ts,
                    "event": "buy_missed",
                    "reason": "overflow_global_active_lot_budget_cap",
                    "slot_id": slot.slot_id,
                    "band_id": slot.band_id,
                    "tranche": slot.tranche,
                    "entry_price": _decimal_str(slot.entry_price),
                    "required_cash": _decimal_str(cash_cost),
                },
                lots,
                fees,
            )
            return
    elif slot.tranche != "base":
        raise AssertionError(f"unknown slot tranche: {slot.tranche}")
    if state.quote_cash < cash_cost:
        state.missed_global_cash += 1
        band.missed_global_cash += 1
        slot.armed = False
        _record_event(
            state,
            {
                "ts": ts,
                "event": "buy_missed",
                "reason": "insufficient_shared_cash",
                "slot_id": slot.slot_id,
                "band_id": slot.band_id,
                "tranche": slot.tranche,
                "entry_price": _decimal_str(slot.entry_price),
                "required_cash": _decimal_str(cash_cost),
            },
            lots,
            fees,
        )
        return

    fee_quote = fees.quote(terms["buy_notional"], config.liquidity_assumption)
    if fee_quote.gross_fee_quote != terms["gross_buy_fee"]:
        raise AssertionError("buy fee planning and fee accumulator disagreed")
    state.lots_created += 1
    lot_id = f"lot-{state.lots_created:06d}"
    lot = LotRecord(
        lot_id=lot_id,
        slot_id=slot.slot_id,
        band_id=slot.band_id,
        level_index=slot.level_index,
        tranche=slot.tranche,
        entry_ts=ts,
        entry_price=slot.entry_price,
        cash_budget=slot.cash_budget,
        buy_notional=terms["buy_notional"],
        gross_buy_fee=fee_quote.gross_fee_quote,
        buy_rebate=fee_quote.rebate_quote,
        cash_cost=cash_cost,
        base_quantity=terms["base_quantity"],
        target_exit_price=target_exit_price,
        target_cash_profit=planned_exit["target_cash_profit"],
        planned_sell_quantity=planned_exit["sell_quantity"],
        planned_reserve_quantity=planned_exit["reserve_quantity"],
    )
    lots[lot_id] = lot
    slot.open_lot_id = lot_id
    slot.armed = False
    state.quote_cash -= cash_cost
    state.active_cash_cost += cash_cost
    if slot.tranche == "base":
        state.base_active_cash_cost += cash_cost
        state.base_filled_buys += 1
        band.base_active_cash_cost += cash_cost
        band.base_filled_buys += 1
    else:
        state.overflow_active_cash_cost += cash_cost
        state.overflow_filled_buys += 1
        band.overflow_active_cash_cost += cash_cost
        band.overflow_filled_buys += 1
    state.open_base += terms["base_quantity"]
    state.filled_buys += 1
    state.turnover_quote += terms["buy_notional"]
    band.active_cash_cost += cash_cost
    band.max_active_cash_cost = max(band.max_active_cash_cost, band.active_cash_cost)
    band.max_base_active_cash_cost = max(
        band.max_base_active_cash_cost,
        band.base_active_cash_cost,
    )
    band.max_overflow_active_cash_cost = max(
        band.max_overflow_active_cash_cost,
        band.overflow_active_cash_cost,
    )
    band.open_lots += 1
    band.filled_buys += 1
    state.max_active_cash_cost = max(state.max_active_cash_cost, state.active_cash_cost)
    state.max_base_active_cash_cost = max(
        state.max_base_active_cash_cost,
        state.base_active_cash_cost,
    )
    state.max_overflow_active_cash_cost = max(
        state.max_overflow_active_cash_cost,
        state.overflow_active_cash_cost,
    )
    _record_event(
        state,
        {
            "ts": ts,
            "event": "buy_filled",
            "lot_id": lot_id,
            "slot_id": slot.slot_id,
            "band_id": slot.band_id,
            "tranche": slot.tranche,
            "entry_price": _decimal_str(slot.entry_price),
            "cash_budget": _decimal_str(slot.cash_budget),
            "buy_notional": _decimal_str(terms["buy_notional"]),
            "gross_buy_fee": _decimal_str(fee_quote.gross_fee_quote),
            "cash_cost": _decimal_str(cash_cost),
            "base_quantity": _decimal_str(terms["base_quantity"]),
            "target_exit_price": _decimal_str(target_exit_price),
            "planned_sell_quantity": _decimal_str(planned_exit["sell_quantity"]),
            "planned_reserve_quantity": _decimal_str(planned_exit["reserve_quantity"]),
        },
        lots,
        fees,
    )


def _exit_lot(
    *,
    lot: LotRecord,
    slot: SlotState,
    ts: str,
    state: ReservePortfolioState,
    band_ledgers: dict[str, BandLedger],
    lots: dict[str, LotRecord],
    config: ReserveGridConfig,
    fees: FeeAccumulator,
) -> None:
    if lot.tranche != slot.tranche:
        raise AssertionError("lot and slot tranche disagreed")
    fee_quote = fees.quote(lot.planned_sell_quantity * lot.target_exit_price, config.liquidity_assumption)
    net_proceeds = (lot.planned_sell_quantity * lot.target_exit_price) - fee_quote.gross_fee_quote
    actual_cash_profit = net_proceeds - lot.cash_cost
    reserve_quantity = lot.base_quantity - lot.planned_sell_quantity
    if config.exit_policy == "principal_recovery" and actual_cash_profit < lot.target_cash_profit:
        raise AssertionError("completed principal-recovery exit missed its cash-profit target")
    if reserve_quantity < 0:
        raise AssertionError("completed lot sold more base than it bought")

    lot.status = "completed"
    lot.exit_ts = ts
    lot.sell_quantity = lot.planned_sell_quantity
    lot.gross_sell_notional = lot.planned_sell_quantity * lot.target_exit_price
    lot.gross_sell_fee = fee_quote.gross_fee_quote
    lot.sell_rebate = fee_quote.rebate_quote
    lot.net_sell_proceeds = net_proceeds
    lot.actual_cash_profit = actual_cash_profit
    lot.reserve_quantity = reserve_quantity
    lot.reserve_cost_basis = (
        lot.cash_cost * reserve_quantity / lot.base_quantity if lot.base_quantity else Decimal("0")
    )
    lot.sold_cost_basis = lot.cash_cost - lot.reserve_cost_basis
    lot.realized_pnl_sold_portion = net_proceeds - lot.sold_cost_basis
    lot.holding_seconds = _seconds_between(lot.entry_ts, ts)
    slot.open_lot_id = None
    slot.armed = True
    slot.cycles += 1
    state.quote_cash += net_proceeds
    state.active_cash_cost -= lot.cash_cost
    if lot.tranche == "base":
        state.base_active_cash_cost -= lot.cash_cost
        state.base_completed_lots += 1
        state.base_filled_sells += 1
        state.base_realized_cash_profit += actual_cash_profit
        state.base_tranche_reserve_base += reserve_quantity
    elif lot.tranche == "overflow":
        state.overflow_active_cash_cost -= lot.cash_cost
        state.overflow_completed_lots += 1
        state.overflow_filled_sells += 1
        state.overflow_realized_cash_profit += actual_cash_profit
        state.overflow_tranche_reserve_base += reserve_quantity
    else:
        raise AssertionError(f"unknown lot tranche: {lot.tranche}")
    state.open_base -= lot.base_quantity
    state.reserve_base += reserve_quantity
    if (
        state.active_cash_cost < 0
        or state.base_active_cash_cost < 0
        or state.overflow_active_cash_cost < 0
        or state.open_base < 0
        or state.reserve_base < 0
    ):
        raise AssertionError("global reserve-grid balances went negative")
    state.completed_lots += 1
    state.filled_sells += 1
    state.realized_cash_profit += actual_cash_profit
    state.turnover_quote += lot.gross_sell_notional
    band = band_ledgers[lot.band_id]
    band.active_cash_cost -= lot.cash_cost
    if lot.tranche == "base":
        band.base_active_cash_cost -= lot.cash_cost
        band.base_realized_cash_profit += actual_cash_profit
        band.base_tranche_reserve_base += reserve_quantity
        band.base_completed_lots += 1
    else:
        band.overflow_active_cash_cost -= lot.cash_cost
        band.overflow_realized_cash_profit += actual_cash_profit
        band.overflow_tranche_reserve_base += reserve_quantity
        band.overflow_completed_lots += 1
    if (
        band.active_cash_cost < 0
        or band.base_active_cash_cost < 0
        or band.overflow_active_cash_cost < 0
    ):
        raise AssertionError("band active cash cost went negative")
    band.realized_cash_profit += actual_cash_profit
    band.reserve_base += reserve_quantity
    band.completed_lots += 1
    band.open_lots -= 1
    _record_event(
        state,
        {
            "ts": ts,
            "event": "exit_filled",
            "lot_id": lot.lot_id,
            "slot_id": lot.slot_id,
            "band_id": lot.band_id,
            "tranche": lot.tranche,
            "exit_price": _decimal_str(lot.target_exit_price),
            "sell_quantity": _decimal_str(lot.sell_quantity),
            "gross_sell_notional": _decimal_str(lot.gross_sell_notional),
            "gross_sell_fee": _decimal_str(lot.gross_sell_fee),
            "net_sell_proceeds": _decimal_str(net_proceeds),
            "actual_cash_profit": _decimal_str(actual_cash_profit),
            "reserve_quantity": _decimal_str(reserve_quantity),
            "sold_cost_basis": _decimal_str(lot.sold_cost_basis),
            "reserve_cost_basis": _decimal_str(lot.reserve_cost_basis),
            "realized_pnl_sold_portion": _decimal_str(lot.realized_pnl_sold_portion),
            "holding_seconds": _decimal_str(lot.holding_seconds),
        },
        lots,
        fees,
    )


def _process_downward_leg(
    start_price: Decimal,
    end_price: Decimal,
    ts: str,
    slots: list[SlotState],
    state: ReservePortfolioState,
    band_ledgers: dict[str, BandLedger],
    lots: dict[str, LotRecord],
    config: ReserveGridConfig,
    fees: FeeAccumulator,
    entry_guard: EntryGuardSnapshot,
) -> None:
    candidates = [
        slot
        for slot in slots
        if slot.armed
        and not slot.disabled
        and slot.open_lot_id is None
        and end_price <= slot.entry_price <= start_price
    ]
    for slot in sorted(
        candidates,
        key=lambda item: (
            -item.entry_price,
            item.level_index,
            0 if item.tranche == "base" else 1,
        ),
    ):
        if not entry_guard.allows_new_buys:
            band = band_ledgers[slot.band_id]
            state.missed_entry_guard += 1
            band.missed_entry_guard += 1
            if slot.tranche == "base":
                state.base_missed_entry_guard += 1
                band.base_missed_entry_guard += 1
            elif slot.tranche == "overflow":
                state.overflow_missed_entry_guard += 1
                band.overflow_missed_entry_guard += 1
            else:
                raise AssertionError(f"unknown slot tranche: {slot.tranche}")
            reason = f"entry_guard_{entry_guard.status}"
            if entry_guard.status == "downtrend":
                state.missed_entry_guard_downtrend += 1
                band.missed_entry_guard_downtrend += 1
                reason = "entry_guard_fast_ema_below_slow_ema"
            elif entry_guard.status == "warmup":
                state.missed_entry_guard_warmup += 1
                band.missed_entry_guard_warmup += 1
            elif entry_guard.status == "stale":
                state.missed_entry_guard_stale += 1
                band.missed_entry_guard_stale += 1
                reason = "entry_guard_stale_signal"
            slot.armed = False
            _record_event(
                state,
                {
                    "ts": ts,
                    "event": "buy_missed",
                    "reason": reason,
                    "slot_id": slot.slot_id,
                    "band_id": slot.band_id,
                    "tranche": slot.tranche,
                    "entry_price": _decimal_str(slot.entry_price),
                    "cash_budget": _decimal_str(slot.cash_budget),
                    "entry_guard": config.entry_guard,
                    "entry_guard_signal_as_of_ts": entry_guard.signal_as_of_ts,
                    "entry_guard_prior_close_count": entry_guard.prior_close_count,
                    "entry_guard_fast_ema": (
                        _decimal_str(entry_guard.fast_ema)
                        if entry_guard.fast_ema is not None
                        else None
                    ),
                    "entry_guard_slow_ema": (
                        _decimal_str(entry_guard.slow_ema)
                        if entry_guard.slow_ema is not None
                        else None
                    ),
                    "entry_guard_gap_seconds": (
                        _decimal_str(entry_guard.gap_seconds)
                        if entry_guard.gap_seconds is not None
                        else None
                    ),
                },
                lots,
                fees,
            )
            continue
        _buy_slot(
            slot=slot,
            ts=ts,
            state=state,
            band_ledgers=band_ledgers,
            lots=lots,
            config=config,
            fees=fees,
        )


def _process_upward_leg(
    start_price: Decimal,
    end_price: Decimal,
    ts: str,
    slots: list[SlotState],
    state: ReservePortfolioState,
    band_ledgers: dict[str, BandLedger],
    lots: dict[str, LotRecord],
    config: ReserveGridConfig,
    fees: FeeAccumulator,
) -> None:
    slot_by_id = {slot.slot_id: slot for slot in slots}
    open_lots = [
        lots[slot.open_lot_id]
        for slot in slots
        if slot.open_lot_id is not None
        and start_price <= lots[slot.open_lot_id].target_exit_price <= end_price
    ]
    for lot in sorted(
        open_lots,
        key=lambda item: (
            item.target_exit_price,
            item.level_index,
            0 if item.tranche == "base" else 1,
            item.lot_id,
        ),
    ):
        _exit_lot(
            lot=lot,
            slot=slot_by_id[lot.slot_id],
            ts=ts,
            state=state,
            band_ledgers=band_ledgers,
            lots=lots,
            config=config,
            fees=fees,
        )
    for slot in slots:
        if (
            slot.open_lot_id is None
            and not slot.armed
            and not slot.disabled
            and start_price <= slot.entry_price <= end_price
        ):
            slot.armed = True


def _validate_candle(candle: dict[str, Any]) -> tuple[Decimal, Decimal, Decimal, Decimal]:
    open_price = _decimal(candle.get("open"), "candle open")
    high = _decimal(candle.get("high"), "candle high")
    low = _decimal(candle.get("low"), "candle low")
    close = _decimal(candle.get("close"), "candle close")
    if low > high or not (low <= open_price <= high) or not (low <= close <= high):
        raise ValueError(f"invalid OHLC candle at {candle.get('start_ts')}")
    return open_price, high, low, close


def _path_points(
    previous_close: Decimal,
    open_price: Decimal,
    high: Decimal,
    low: Decimal,
    close: Decimal,
    assumption: str,
) -> list[Decimal]:
    if assumption == "low-first":
        return [previous_close, open_price, low, high, close]
    if assumption == "high-first":
        return [previous_close, open_price, high, low, close]
    raise ValueError("candle_path_assumption must be low-first or high-first")


def _ema_alpha(span_candles: int) -> Decimal:
    return Decimal("2") / Decimal(span_candles + 1)


def _update_ema(
    current_ema: Decimal | None,
    close: Decimal,
    span_candles: int,
) -> Decimal:
    if current_ema is None:
        return close
    alpha = _ema_alpha(span_candles)
    return (alpha * close) + ((Decimal("1") - alpha) * current_ema)


def _entry_guard_snapshot(
    *,
    config: ReserveGridConfig,
    current_ts: str,
    signal_as_of_ts: str | None,
    prior_close_count: int,
    fast_ema: Decimal | None,
    slow_ema: Decimal | None,
) -> EntryGuardSnapshot:
    if config.entry_guard == "none":
        return EntryGuardSnapshot(
            status="disabled",
            allows_new_buys=True,
            signal_as_of_ts=signal_as_of_ts,
            prior_close_count=prior_close_count,
            fast_ema=fast_ema,
            slow_ema=slow_ema,
            gap_seconds=None,
        )

    gap_seconds = (
        _seconds_between(signal_as_of_ts, current_ts)
        if signal_as_of_ts is not None
        else None
    )
    if (
        prior_close_count < config.entry_guard_slow_ema_span_candles
        or fast_ema is None
        or slow_ema is None
    ):
        status = "warmup"
        allows_new_buys = False
    elif gap_seconds is None or gap_seconds > Decimal("60"):
        status = "stale"
        allows_new_buys = False
    elif fast_ema >= slow_ema:
        status = "allowed"
        allows_new_buys = True
    else:
        status = "downtrend"
        allows_new_buys = False
    return EntryGuardSnapshot(
        status=status,
        allows_new_buys=allows_new_buys,
        signal_as_of_ts=signal_as_of_ts,
        prior_close_count=prior_close_count,
        fast_ema=fast_ema,
        slow_ema=slow_ema,
        gap_seconds=gap_seconds,
    )


def _advance_entry_guard_emas(
    *,
    config: ReserveGridConfig,
    ts: str,
    close: Decimal,
    signal_as_of_ts: str | None,
    prior_close_count: int,
    fast_ema: Decimal | None,
    slow_ema: Decimal | None,
) -> tuple[str, int, Decimal, Decimal, bool]:
    reset = False
    if signal_as_of_ts is not None:
        gap_seconds = _seconds_between(signal_as_of_ts, ts)
        reset_threshold = Decimal(
            config.entry_guard_slow_ema_span_candles * 60
        )
        if gap_seconds >= reset_threshold:
            prior_close_count = 0
            fast_ema = None
            slow_ema = None
            reset = True
    return (
        ts,
        prior_close_count + 1,
        _update_ema(
            fast_ema,
            close,
            config.entry_guard_fast_ema_span_candles,
        ),
        _update_ema(
            slow_ema,
            close,
            config.entry_guard_slow_ema_span_candles,
        ),
        reset,
    )


def simulate_reserve_gridbot_on_candles(
    candles: list[dict[str, Any]],
    config: ReserveGridConfig,
    fee_profile: FeeProfile | None = None,
) -> dict[str, Any]:
    if not candles:
        raise ValueError("no candle rows available for reserve-grid backtest")
    if not config.include_fallback_candles:
        raise ValueError("reserve-grid v1 supports explicit fallback-candle research only")
    if normalize_product_id(config.product_id) != "XRP-USD":
        raise ValueError("reserve-grid v1 is limited to XRP-USD until increments are product-derived")
    if config.exit_policy not in {"principal_recovery", "full_lot"}:
        raise ValueError("exit_policy must be principal_recovery or full_lot")
    if config.entry_guard not in {"none", "ema_cross"}:
        raise ValueError("entry_guard must be none or ema_cross")
    if config.entry_guard_fast_ema_span_candles < 2:
        raise ValueError("entry_guard_fast_ema_span_candles must be at least 2")
    if (
        config.entry_guard_slow_ema_span_candles
        <= config.entry_guard_fast_ema_span_candles
    ):
        raise ValueError(
            "entry_guard_slow_ema_span_candles must exceed the fast span"
        )
    if _decimal(config.quote_start, "quote_start") <= 0:
        raise ValueError("quote_start must be positive")
    if _decimal(config.exit_move_pct, "exit_move_pct") <= 0:
        raise ValueError("exit_move_pct must be positive")
    if _decimal(config.cash_profit_bps, "cash_profit_bps") <= 0:
        raise ValueError("cash_profit_bps must be positive")

    validated_candles: list[
        tuple[dict[str, Any], str, Decimal, Decimal, Decimal, Decimal]
    ] = []
    previous_ts: datetime | None = None
    trade_start = _parse_ts(config.start) if config.start else None
    for candle in candles:
        if normalize_product_id(candle.get("product_id")) != "XRP-USD":
            raise ValueError("every reserve-grid candle must have product_id XRP-USD")
        ts = str(candle.get("start_ts") or "")
        if not ts:
            raise ValueError("candle row is missing start_ts")
        parsed_ts = _parse_ts(ts)
        if previous_ts is not None and parsed_ts <= previous_ts:
            raise ValueError("reserve-grid candles must have strictly increasing unique start_ts values")
        if (
            config.entry_guard != "none"
            and _timeframe_seconds(str(candle.get("timeframe") or ""))
            != Decimal("60")
        ):
            raise ValueError("ema_cross entry guard requires one-minute candles")
        open_price, high, low, close = _validate_candle(candle)
        validated_candles.append((candle, ts, open_price, high, low, close))
        previous_ts = parsed_ts

    if config.entry_guard != "none" and trade_start is not None:
        signal_preroll_candles = [
            row for row in validated_candles if _parse_ts(row[1]) < trade_start
        ][-config.entry_guard_slow_ema_span_candles:]
        trading_candles = [
            row for row in validated_candles if _parse_ts(row[1]) >= trade_start
        ]
    else:
        signal_preroll_candles = []
        trading_candles = validated_candles
    if not trading_candles:
        raise ValueError("no trading candle rows remain at or after start")

    bands, slots = build_bands_and_slots(config)
    band_ledgers = {band.band_id: BandLedger(definition=band) for band in bands}
    resolved_fee_profile = fee_profile or _fee_profile_from_config(config)
    fees = FeeAccumulator(resolved_fee_profile)
    quote_start = _decimal(config.quote_start, "quote_start")
    state = ReservePortfolioState(quote_cash=quote_start, initial_quote_cash=quote_start)
    lots: dict[str, LotRecord] = {}
    minimum_path_prices: dict[str, Decimal] = {}
    close_observations: dict[str, tuple[Decimal, int]] = {}
    first_open = trading_candles[0][2]
    for slot in slots:
        slot.armed = slot.entry_price <= first_open
    previous_close = first_open
    max_equity_before_rebate = quote_start
    max_drawdown_before_rebate = Decimal("0")
    equity_curve: list[dict[str, Any]] = []
    signal_as_of_ts: str | None = None
    signal_prior_close_count = 0
    signal_fast_ema: Decimal | None = None
    signal_slow_ema: Decimal | None = None
    entry_guard_large_gap_resets = 0
    entry_guard_candle_counts = {
        "disabled": 0,
        "warmup": 0,
        "stale": 0,
        "allowed": 0,
        "downtrend": 0,
    }
    signal_transition_count = 0
    previous_signal_allows: bool | None = None

    if config.entry_guard != "none":
        for _, ts, _, _, _, close in signal_preroll_candles:
            (
                signal_as_of_ts,
                signal_prior_close_count,
                signal_fast_ema,
                signal_slow_ema,
                reset,
            ) = _advance_entry_guard_emas(
                config=config,
                ts=ts,
                close=close,
                signal_as_of_ts=signal_as_of_ts,
                prior_close_count=signal_prior_close_count,
                fast_ema=signal_fast_ema,
                slow_ema=signal_slow_ema,
            )
            entry_guard_large_gap_resets += int(reset)

    for candle, ts, open_price, high, low, close in trading_candles:
        if config.entry_guard != "none" and signal_as_of_ts is not None:
            gap_seconds = _seconds_between(signal_as_of_ts, ts)
            reset_threshold = Decimal(
                config.entry_guard_slow_ema_span_candles * 60
            )
            if gap_seconds >= reset_threshold:
                signal_prior_close_count = 0
                signal_fast_ema = None
                signal_slow_ema = None
                signal_as_of_ts = None
                entry_guard_large_gap_resets += 1
        entry_guard = _entry_guard_snapshot(
            config=config,
            current_ts=ts,
            signal_as_of_ts=signal_as_of_ts,
            prior_close_count=signal_prior_close_count,
            fast_ema=signal_fast_ema,
            slow_ema=signal_slow_ema,
        )
        entry_guard_candle_counts[entry_guard.status] += 1
        if entry_guard.status in {"allowed", "downtrend"}:
            if (
                previous_signal_allows is not None
                and previous_signal_allows != entry_guard.allows_new_buys
            ):
                signal_transition_count += 1
            previous_signal_allows = entry_guard.allows_new_buys
        points = _path_points(
            previous_close,
            open_price,
            high,
            low,
            close,
            config.candle_path_assumption,
        )
        for start_price, end_price in zip(points, points[1:]):
            if end_price < start_price:
                _process_downward_leg(
                    start_price,
                    end_price,
                    ts,
                    slots,
                    state,
                    band_ledgers,
                    lots,
                    config,
                    fees,
                    entry_guard,
                )
                for slot in slots:
                    if slot.open_lot_id is None:
                        continue
                    lot = lots[slot.open_lot_id]
                    prior_minimum = minimum_path_prices.get(
                        lot.lot_id,
                        lot.entry_price,
                    )
                    minimum_path_prices[lot.lot_id] = min(
                        prior_minimum,
                        end_price,
                    )
            elif end_price > start_price:
                _process_upward_leg(
                    start_price,
                    end_price,
                    ts,
                    slots,
                    state,
                    band_ledgers,
                    lots,
                    config,
                    fees,
                )
        for slot in slots:
            if slot.open_lot_id is None:
                continue
            lot_id = slot.open_lot_id
            prior_close_observation = close_observations.get(lot_id)
            if prior_close_observation is None:
                close_observations[lot_id] = (close, 1)
            else:
                close_observations[lot_id] = (
                    min(prior_close_observation[0], close),
                    prior_close_observation[1] + 1,
                )
        open_base = state.open_base
        reserve_base = state.reserve_base
        base_value = (open_base + reserve_base) * close
        equity_before_rebate = state.quote_cash + base_value
        equity_after_rebate = equity_before_rebate + fees.rebates_quote
        max_equity_before_rebate = max(max_equity_before_rebate, equity_before_rebate)
        drawdown = max_equity_before_rebate - equity_before_rebate
        max_drawdown_before_rebate = max(max_drawdown_before_rebate, drawdown)
        active_cash = state.active_cash_cost
        utilization = active_cash / quote_start
        overflow_utilization = state.overflow_active_cash_cost / quote_start
        state.utilization_sum += utilization
        state.overflow_utilization_sum += overflow_utilization
        state.utilization_observations += 1
        reserve_value = reserve_base * close
        state.max_reserve_value_quote = max(state.max_reserve_value_quote, reserve_value)
        equity_row = {
                "ts": ts,
                "close": _decimal_str(close),
                "quote_cash": _decimal_str(state.quote_cash),
                "active_cash_cost": _decimal_str(active_cash),
                "base_active_cash_cost": _decimal_str(state.base_active_cash_cost),
                "overflow_active_cash_cost": _decimal_str(state.overflow_active_cash_cost),
                "open_base": _decimal_str(open_base),
                "reserve_base": _decimal_str(reserve_base),
                "base_tranche_reserve_base": _decimal_str(
                    state.base_tranche_reserve_base
                ),
                "overflow_tranche_reserve_base": _decimal_str(
                    state.overflow_tranche_reserve_base
                ),
                "reserve_value_quote": _decimal_str(reserve_value),
                "equity_before_rebate": _decimal_str(equity_before_rebate),
                "modeled_rebate_receivable_quote": _decimal_str(fees.rebates_quote),
                "equity_after_rebate": _decimal_str(equity_after_rebate),
                "close_sampled_drawdown_before_rebate": _decimal_str(drawdown),
                "active_unrecovered_cost_fraction_of_starting_cash": _decimal_str(utilization),
                "overflow_active_unrecovered_cost_fraction_of_starting_cash": _decimal_str(
                    overflow_utilization
                ),
        }
        if config.entry_guard != "none":
            equity_row.update(
                {
                "entry_guard_status": entry_guard.status,
                "entry_guard_allows_new_buys": entry_guard.allows_new_buys,
                "entry_guard_signal_as_of_ts": entry_guard.signal_as_of_ts,
                "entry_guard_prior_close_count": entry_guard.prior_close_count,
                "entry_guard_fast_ema": (
                    _decimal_str(entry_guard.fast_ema)
                    if entry_guard.fast_ema is not None
                    else None
                ),
                "entry_guard_slow_ema": (
                    _decimal_str(entry_guard.slow_ema)
                    if entry_guard.slow_ema is not None
                    else None
                ),
                }
            )
        equity_curve.append(equity_row)
        if config.entry_guard != "none":
            (
                signal_as_of_ts,
                signal_prior_close_count,
                signal_fast_ema,
                signal_slow_ema,
                reset,
            ) = _advance_entry_guard_emas(
                config=config,
                ts=ts,
                close=close,
                signal_as_of_ts=signal_as_of_ts,
                prior_close_count=signal_prior_close_count,
                fast_ema=signal_fast_ema,
                slow_ema=signal_slow_ema,
            )
            if reset:
                raise AssertionError("large signal gap should reset before candle replay")
        previous_close = close

    final_close = trading_candles[-1][5]
    open_base = state.open_base
    reserve_base = state.reserve_base
    total_base = open_base + reserve_base
    final_base_value = total_base * final_close
    final_equity_before_rebate = state.quote_cash + final_base_value
    final_equity_after_rebate = final_equity_before_rebate + fees.rebates_quote
    final_liquidation_fee = final_base_value * fees.rate_for(config.liquidity_assumption)
    final_liquidation_equity_before_rebate = final_equity_before_rebate - final_liquidation_fee
    base_lots = [lot for lot in lots.values() if lot.tranche == "base"]
    overflow_lots = [lot for lot in lots.values() if lot.tranche == "overflow"]
    base_open_lots = [lot for lot in base_lots if lot.status == "open"]
    overflow_open_lots = [lot for lot in overflow_lots if lot.status == "open"]
    base_completed_lots = [lot for lot in base_lots if lot.status == "completed"]
    overflow_completed_lots = [lot for lot in overflow_lots if lot.status == "completed"]
    open_lots = base_open_lots + overflow_open_lots
    completed_lots = base_completed_lots + overflow_completed_lots
    base_open_quantity = sum((lot.base_quantity for lot in base_open_lots), Decimal("0"))
    overflow_open_quantity = sum(
        (lot.base_quantity for lot in overflow_open_lots), Decimal("0")
    )
    base_reserve_quantity = sum(
        (lot.reserve_quantity for lot in base_completed_lots), Decimal("0")
    )
    overflow_reserve_quantity = sum(
        (lot.reserve_quantity for lot in overflow_completed_lots), Decimal("0")
    )
    base_open_cost_basis = sum((lot.cash_cost for lot in base_open_lots), Decimal("0"))
    overflow_open_cost_basis = sum(
        (lot.cash_cost for lot in overflow_open_lots), Decimal("0")
    )
    open_cost_basis = base_open_cost_basis + overflow_open_cost_basis
    base_reserve_cost_basis = sum(
        (lot.reserve_cost_basis for lot in base_completed_lots), Decimal("0")
    )
    overflow_reserve_cost_basis = sum(
        (lot.reserve_cost_basis for lot in overflow_completed_lots), Decimal("0")
    )
    reserve_cost_basis = base_reserve_cost_basis + overflow_reserve_cost_basis
    base_realized_pnl_sold_portion = sum(
        (lot.realized_pnl_sold_portion for lot in base_completed_lots), Decimal("0")
    )
    overflow_realized_pnl_sold_portion = sum(
        (lot.realized_pnl_sold_portion for lot in overflow_completed_lots), Decimal("0")
    )
    realized_pnl_sold_portion = (
        base_realized_pnl_sold_portion + overflow_realized_pnl_sold_portion
    )
    open_unrealized_pnl = (open_base * final_close) - open_cost_basis
    reserve_unrealized_pnl = (reserve_base * final_close) - reserve_cost_basis
    pnl_components = realized_pnl_sold_portion + open_unrealized_pnl + reserve_unrealized_pnl
    pnl_reconciliation_error = (final_equity_before_rebate - quote_start) - pnl_components
    completed_holds = [lot.holding_seconds for lot in completed_lots if lot.holding_seconds is not None]
    open_holds = [
        _seconds_between(lot.entry_ts, trading_candles[-1][1])
        for lot in open_lots
    ]
    average_holding = (
        sum(completed_holds, Decimal("0")) / Decimal(len(completed_holds))
        if completed_holds
        else Decimal("0")
    )
    longest_end_open = max(open_holds, default=Decimal("0"))
    for ledger in band_ledgers.values():
        band_open_lots = [lot for lot in open_lots if lot.band_id == ledger.definition.band_id]
        band_completed_lots = [
            lot for lot in completed_lots if lot.band_id == ledger.definition.band_id
        ]
        ledger.base_active_cash_cost = sum(
            (lot.cash_cost for lot in band_open_lots if lot.tranche == "base"), Decimal("0")
        )
        ledger.overflow_active_cash_cost = sum(
            (lot.cash_cost for lot in band_open_lots if lot.tranche == "overflow"), Decimal("0")
        )
        ledger.active_cash_cost = (
            ledger.base_active_cash_cost + ledger.overflow_active_cash_cost
        )
        ledger.open_lots = len(band_open_lots)
        ledger.base_tranche_reserve_base = sum(
            (
                lot.reserve_quantity
                for lot in band_completed_lots
                if lot.tranche == "base"
            ),
            Decimal("0"),
        )
        ledger.overflow_tranche_reserve_base = sum(
            (
                lot.reserve_quantity
                for lot in band_completed_lots
                if lot.tranche == "overflow"
            ),
            Decimal("0"),
        )
        ledger.reserve_base = (
            ledger.base_tranche_reserve_base + ledger.overflow_tranche_reserve_base
        )

    bought_base = sum((lot.base_quantity for lot in lots.values()), Decimal("0"))
    sold_base = sum((lot.sell_quantity for lot in lots.values()), Decimal("0"))
    base_reconciliation_error = bought_base - sold_base - open_base - reserve_base
    expected_cash = quote_start - sum((lot.cash_cost for lot in lots.values()), Decimal("0")) + sum(
        (lot.net_sell_proceeds for lot in lots.values()), Decimal("0")
    )
    cash_reconciliation_error = state.quote_cash - expected_cash
    state_active_error = state.active_cash_cost - open_cost_basis
    state_open_base_error = state.open_base - sum(
        (lot.base_quantity for lot in lots.values() if lot.status == "open"), Decimal("0")
    )
    state_reserve_base_error = state.reserve_base - sum(
        (lot.reserve_quantity for lot in completed_lots), Decimal("0")
    )
    state_base_active_cash_reconciliation_error = (
        state.base_active_cash_cost - base_open_cost_basis
    )
    state_overflow_active_cash_reconciliation_error = (
        state.overflow_active_cash_cost - overflow_open_cost_basis
    )
    active_cash_tranche_reconciliation_error = state.active_cash_cost - (
        state.base_active_cash_cost + state.overflow_active_cash_cost
    )
    state_base_reserve_reconciliation_error = (
        state.base_tranche_reserve_base - base_reserve_quantity
    )
    state_overflow_reserve_reconciliation_error = (
        state.overflow_tranche_reserve_base - overflow_reserve_quantity
    )
    reserve_tranche_reconciliation_error = state.reserve_base - (
        state.base_tranche_reserve_base + state.overflow_tranche_reserve_base
    )
    band_active_cash_reconciliation_error = state.active_cash_cost - sum(
        (ledger.active_cash_cost for ledger in band_ledgers.values()), Decimal("0")
    )
    band_base_active_cash_reconciliation_error = state.base_active_cash_cost - sum(
        (ledger.base_active_cash_cost for ledger in band_ledgers.values()), Decimal("0")
    )
    band_overflow_active_cash_reconciliation_error = state.overflow_active_cash_cost - sum(
        (ledger.overflow_active_cash_cost for ledger in band_ledgers.values()), Decimal("0")
    )
    band_reserve_base_reconciliation_error = state.reserve_base - sum(
        (ledger.reserve_base for ledger in band_ledgers.values()), Decimal("0")
    )
    band_cash_profit_reconciliation_error = state.realized_cash_profit - sum(
        (ledger.realized_cash_profit for ledger in band_ledgers.values()), Decimal("0")
    )
    band_base_cash_profit_reconciliation_error = state.base_realized_cash_profit - sum(
        (ledger.base_realized_cash_profit for ledger in band_ledgers.values()), Decimal("0")
    )
    band_overflow_cash_profit_reconciliation_error = (
        state.overflow_realized_cash_profit
        - sum(
            (ledger.overflow_realized_cash_profit for ledger in band_ledgers.values()),
            Decimal("0"),
        )
    )
    lot_gross_fees = sum(
        (lot.gross_buy_fee + lot.gross_sell_fee for lot in lots.values()), Decimal("0")
    )
    gross_fee_reconciliation_error = fees.gross_fees_quote - lot_gross_fees
    lot_rebates = sum((lot.buy_rebate + lot.sell_rebate for lot in lots.values()), Decimal("0"))
    rebate_reconciliation_error = fees.rebates_quote - lot_rebates
    net_fee_reconciliation_error = fees.net_fees_quote - (lot_gross_fees - lot_rebates)
    lot_turnover = sum(
        (lot.buy_notional + lot.gross_sell_notional for lot in lots.values()), Decimal("0")
    )
    turnover_reconciliation_error = state.turnover_quote - lot_turnover
    base_gross_fees = sum(
        (lot.gross_buy_fee + lot.gross_sell_fee for lot in base_lots), Decimal("0")
    )
    overflow_gross_fees = sum(
        (lot.gross_buy_fee + lot.gross_sell_fee for lot in overflow_lots), Decimal("0")
    )
    base_rebates = sum(
        (lot.buy_rebate + lot.sell_rebate for lot in base_lots), Decimal("0")
    )
    overflow_rebates = sum(
        (lot.buy_rebate + lot.sell_rebate for lot in overflow_lots), Decimal("0")
    )
    base_turnover = sum(
        (lot.buy_notional + lot.gross_sell_notional for lot in base_lots), Decimal("0")
    )
    overflow_turnover = sum(
        (lot.buy_notional + lot.gross_sell_notional for lot in overflow_lots), Decimal("0")
    )
    gross_fee_tranche_reconciliation_error = fees.gross_fees_quote - (
        base_gross_fees + overflow_gross_fees
    )
    rebate_tranche_reconciliation_error = fees.rebates_quote - (
        base_rebates + overflow_rebates
    )
    turnover_tranche_reconciliation_error = state.turnover_quote - (
        base_turnover + overflow_turnover
    )
    lot_count_tranche_reconciliation_error = state.lots_created - (
        len(base_lots) + len(overflow_lots)
    )
    completed_count_tranche_reconciliation_error = state.completed_lots - (
        state.base_completed_lots + state.overflow_completed_lots
    )
    filled_buy_count_tranche_reconciliation_error = state.filled_buys - (
        state.base_filled_buys + state.overflow_filled_buys
    )
    filled_sell_count_tranche_reconciliation_error = state.filled_sells - (
        state.base_filled_sells + state.overflow_filled_sells
    )
    lot_cash_profit = sum((lot.actual_cash_profit for lot in lots.values()), Decimal("0"))
    cash_profit_reconciliation_error = state.realized_cash_profit - lot_cash_profit
    base_cash_profit_reconciliation_error = state.base_realized_cash_profit - sum(
        (lot.actual_cash_profit for lot in base_lots), Decimal("0")
    )
    overflow_cash_profit_reconciliation_error = state.overflow_realized_cash_profit - sum(
        (lot.actual_cash_profit for lot in overflow_lots), Decimal("0")
    )
    cash_profit_tranche_reconciliation_error = state.realized_cash_profit - (
        state.base_realized_cash_profit + state.overflow_realized_cash_profit
    )
    base_bought = sum((lot.base_quantity for lot in base_lots), Decimal("0"))
    base_sold = sum((lot.sell_quantity for lot in base_lots), Decimal("0"))
    overflow_bought = sum((lot.base_quantity for lot in overflow_lots), Decimal("0"))
    overflow_sold = sum((lot.sell_quantity for lot in overflow_lots), Decimal("0"))
    base_tranche_base_reconciliation_error = (
        base_bought - base_sold - base_open_quantity - base_reserve_quantity
    )
    overflow_tranche_base_reconciliation_error = (
        overflow_bought
        - overflow_sold
        - overflow_open_quantity
        - overflow_reserve_quantity
    )
    overflow_cap = _decimal(
        config.overflow_global_active_lot_budget_cap,
        "overflow_global_active_lot_budget_cap",
    )
    overflow_cap_excess_error = max(
        Decimal("0"),
        state.max_overflow_active_cash_cost - overflow_cap,
    )
    base_band_cap_excess_error = max(
        (
            max(
                Decimal("0"),
                ledger.max_base_active_cash_cost - ledger.definition.active_lot_budget_cap,
            )
            for ledger in band_ledgers.values()
        ),
        default=Decimal("0"),
    )
    entry_guard_band_reconciliation_error = state.missed_entry_guard - sum(
        (ledger.missed_entry_guard for ledger in band_ledgers.values()),
        0,
    )
    entry_guard_tranche_reconciliation_error = state.missed_entry_guard - (
        state.base_missed_entry_guard + state.overflow_missed_entry_guard
    )
    entry_guard_reason_reconciliation_error = state.missed_entry_guard - (
        state.missed_entry_guard_downtrend
        + state.missed_entry_guard_warmup
        + state.missed_entry_guard_stale
    )
    if any(
        value != 0
        for value in (
            base_reconciliation_error,
            cash_reconciliation_error,
            pnl_reconciliation_error,
            state_active_error,
            state_base_active_cash_reconciliation_error,
            state_overflow_active_cash_reconciliation_error,
            active_cash_tranche_reconciliation_error,
            state_open_base_error,
            state_reserve_base_error,
            state_base_reserve_reconciliation_error,
            state_overflow_reserve_reconciliation_error,
            reserve_tranche_reconciliation_error,
            band_active_cash_reconciliation_error,
            band_base_active_cash_reconciliation_error,
            band_overflow_active_cash_reconciliation_error,
            band_reserve_base_reconciliation_error,
            band_cash_profit_reconciliation_error,
            band_base_cash_profit_reconciliation_error,
            band_overflow_cash_profit_reconciliation_error,
            gross_fee_reconciliation_error,
            rebate_reconciliation_error,
            net_fee_reconciliation_error,
            turnover_reconciliation_error,
            gross_fee_tranche_reconciliation_error,
            rebate_tranche_reconciliation_error,
            turnover_tranche_reconciliation_error,
            lot_count_tranche_reconciliation_error,
            completed_count_tranche_reconciliation_error,
            filled_buy_count_tranche_reconciliation_error,
            filled_sell_count_tranche_reconciliation_error,
            cash_profit_reconciliation_error,
            base_cash_profit_reconciliation_error,
            overflow_cash_profit_reconciliation_error,
            cash_profit_tranche_reconciliation_error,
            base_tranche_base_reconciliation_error,
            overflow_tranche_base_reconciliation_error,
            overflow_cap_excess_error,
            base_band_cap_excess_error,
            entry_guard_band_reconciliation_error,
            entry_guard_tranche_reconciliation_error,
            entry_guard_reason_reconciliation_error,
        )
    ):
        raise AssertionError("reserve-grid ledger conservation failed")

    total_band_headroom = sum(
        (
            ledger.definition.active_lot_budget_cap - ledger.base_active_cash_cost
            for ledger in band_ledgers.values()
        ),
        Decimal("0"),
    )
    total_band_active_caps = sum(
        (ledger.definition.active_lot_budget_cap for ledger in band_ledgers.values()), Decimal("0")
    )
    base_slots = [slot for slot in slots if slot.tranche == "base"]
    overflow_slots = [slot for slot in slots if slot.tranche == "overflow"]
    total_base_slot_cash_budgets = sum(
        (slot.cash_budget for slot in base_slots), Decimal("0")
    )
    total_overflow_slot_cash_budgets = sum(
        (slot.cash_budget for slot in overflow_slots), Decimal("0")
    )
    total_slot_cash_budgets = (
        total_base_slot_cash_budgets + total_overflow_slot_cash_budgets
    )
    average_utilization = (
        state.utilization_sum / Decimal(state.utilization_observations)
        if state.utilization_observations
        else Decimal("0")
    )
    average_overflow_utilization = (
        state.overflow_utilization_sum / Decimal(state.utilization_observations)
        if state.utilization_observations
        else Decimal("0")
    )
    initial_cash_above_total_base_caps = max(
        Decimal("0"), quote_start - total_band_active_caps
    )
    base_open_unrealized_pnl = (base_open_quantity * final_close) - base_open_cost_basis
    overflow_open_unrealized_pnl = (
        overflow_open_quantity * final_close
    ) - overflow_open_cost_basis
    base_reserve_unrealized_pnl = (
        base_reserve_quantity * final_close
    ) - base_reserve_cost_basis
    overflow_reserve_unrealized_pnl = (
        overflow_reserve_quantity * final_close
    ) - overflow_reserve_cost_basis
    buy_hold_base = quote_start / first_open
    lot_diagnostic_rows, lot_diagnostic_summary = _build_lot_recovery_diagnostics(
        lots=lots,
        minimum_path_prices=minimum_path_prices,
        close_observations=close_observations,
        validated_candles=trading_candles,
        events=state.events,
    )
    entry_guard_events = [
        event
        for event in state.events
        if str(event.get("reason") or "").startswith("entry_guard_")
    ]
    summary = {
        "status": "completed",
        "engine": ENGINE_VERSION,
        "product_id": config.product_id,
        "mode": "fallback_candles",
        "exit_policy": config.exit_policy,
        "candle_path_assumption": config.candle_path_assumption,
        "entry_guard": config.entry_guard,
        "entry_guard_enabled": config.entry_guard != "none",
        "entry_guard_fast_ema_span_candles": (
            config.entry_guard_fast_ema_span_candles
        ),
        "entry_guard_slow_ema_span_candles": (
            config.entry_guard_slow_ema_span_candles
        ),
        "entry_guard_fast_ema_alpha": _decimal_str(
            _ema_alpha(config.entry_guard_fast_ema_span_candles)
        ),
        "entry_guard_slow_ema_alpha": _decimal_str(
            _ema_alpha(config.entry_guard_slow_ema_span_candles)
        ),
        "entry_guard_decision_timing": "completed_closes_through_previous_candle_only",
        "entry_guard_warmup_policy": "fail_closed_until_slow_span_observed_closes",
        "entry_guard_small_gap_policy": "block_only_the_first_candle_after_a_missing_minute",
        "entry_guard_large_gap_reset_candles": (
            config.entry_guard_slow_ema_span_candles
        ),
        "entry_guard_signal_preroll_candles_used": len(signal_preroll_candles),
        "entry_guard_signal_preroll_first_ts": (
            signal_preroll_candles[0][1] if signal_preroll_candles else None
        ),
        "entry_guard_signal_preroll_last_ts": (
            signal_preroll_candles[-1][1] if signal_preroll_candles else None
        ),
        "entry_guard_candle_counts": entry_guard_candle_counts,
        "entry_guard_signal_transition_count": signal_transition_count,
        "entry_guard_large_gap_reset_count": entry_guard_large_gap_resets,
        "entry_guard_blocked_crossings": state.missed_entry_guard,
        "entry_guard_blocked_unique_slots": len(
            {str(event.get("slot_id")) for event in entry_guard_events}
        ),
        "entry_guard_blocked_unique_bands": len(
            {str(event.get("band_id")) for event in entry_guard_events}
        ),
        "entry_guard_blocked_planned_cash_budget": _decimal_str(
            sum(
                (
                    _decimal(event.get("cash_budget"), "guard cash_budget")
                    for event in entry_guard_events
                ),
                Decimal("0"),
            )
        ),
        "first_ts": trading_candles[0][1],
        "last_ts": trading_candles[-1][1],
        "candles_used": len(trading_candles),
        "band_count": len(bands),
        "entry_level_count": len(base_slots),
        "trading_slot_count": len(slots),
        "base_tranche_slot_count": len(base_slots),
        "overflow_tranche_slot_count": len(overflow_slots),
        "overflow_enabled": overflow_cap > 0,
        "overflow_global_active_lot_budget_cap": _decimal_str(overflow_cap),
        "overflow_funding_mode": "shared_quote_cash_with_separate_active_cost_ceiling",
        "slot_cash_budget_all_in": _decimal_str(slots[0].cash_budget),
        "total_slot_cash_budgets": _decimal_str(total_slot_cash_budgets),
        "total_base_slot_cash_budgets": _decimal_str(total_base_slot_cash_budgets),
        "total_overflow_slot_cash_budgets": _decimal_str(
            total_overflow_slot_cash_budgets
        ),
        "total_band_active_lot_budget_caps": _decimal_str(total_band_active_caps),
        "band_caps_exceed_starting_cash": total_band_active_caps > quote_start,
        "base_plus_overflow_caps_exceed_starting_cash": (
            total_band_active_caps + overflow_cap > quote_start
        ),
        "initial_cash_above_total_base_caps": _decimal_str(
            initial_cash_above_total_base_caps
        ),
        "overflow_cap_fully_covered_by_initial_cash_above_base_caps": (
            overflow_cap <= initial_cash_above_total_base_caps
        ),
        "starting_quote_cash": _decimal_str(quote_start),
        "final_quote_cash": _decimal_str(state.quote_cash),
        "shared_idle_cash": _decimal_str(state.quote_cash),
        "end_theoretical_cash_bounded_by_total_base_band_cap_headroom": _decimal_str(
            min(state.quote_cash, total_band_headroom)
        ),
        "end_theoretical_cash_bounded_by_overflow_cap_headroom": _decimal_str(
            min(
                state.quote_cash,
                max(Decimal("0"), overflow_cap - state.overflow_active_cash_cost),
            )
        ),
        "final_active_cash_cost": _decimal_str(state.active_cash_cost),
        "final_base_active_cash_cost": _decimal_str(state.base_active_cash_cost),
        "final_overflow_active_cash_cost": _decimal_str(
            state.overflow_active_cash_cost
        ),
        "maximum_active_cash_cost": _decimal_str(state.max_active_cash_cost),
        "maximum_base_active_cash_cost": _decimal_str(state.max_base_active_cash_cost),
        "maximum_overflow_active_cash_cost": _decimal_str(
            state.max_overflow_active_cash_cost
        ),
        "average_close_sampled_active_unrecovered_cost_fraction_of_starting_cash": _decimal_str(
            average_utilization
        ),
        "average_close_sampled_overflow_active_cost_fraction_of_starting_cash": _decimal_str(
            average_overflow_utilization
        ),
        "lots_created": state.lots_created,
        "base_lots_created": len(base_lots),
        "overflow_lots_created": len(overflow_lots),
        "completed_lots": state.completed_lots,
        "base_completed_lots": state.base_completed_lots,
        "overflow_completed_lots": state.overflow_completed_lots,
        "end_open_unrecovered_lots": len([lot for lot in lots.values() if lot.status == "open"]),
        "base_end_open_unrecovered_lots": len(base_open_lots),
        "overflow_end_open_unrecovered_lots": len(overflow_open_lots),
        "filled_buys": state.filled_buys,
        "base_filled_buys": state.base_filled_buys,
        "overflow_filled_buys": state.overflow_filled_buys,
        "filled_sells": state.filled_sells,
        "base_filled_sells": state.base_filled_sells,
        "overflow_filled_sells": state.overflow_filled_sells,
        "missed_buys_insufficient_shared_cash": state.missed_global_cash,
        "missed_buys_band_cap": state.missed_band_cap,
        "missed_buys_overflow_global_cap": state.missed_overflow_global_cap,
        "missed_buys_entry_guard": state.missed_entry_guard,
        "base_missed_buys_entry_guard": state.base_missed_entry_guard,
        "overflow_missed_buys_entry_guard": state.overflow_missed_entry_guard,
        "missed_buys_entry_guard_downtrend": (
            state.missed_entry_guard_downtrend
        ),
        "missed_buys_entry_guard_warmup": state.missed_entry_guard_warmup,
        "missed_buys_entry_guard_stale": state.missed_entry_guard_stale,
        "disabled_infeasible_slots": state.disabled_infeasible_slots,
        "realized_cash_profit": _decimal_str(state.realized_cash_profit),
        "base_realized_cash_profit": _decimal_str(state.base_realized_cash_profit),
        "overflow_realized_cash_profit": _decimal_str(
            state.overflow_realized_cash_profit
        ),
        "realized_pnl_sold_portion": _decimal_str(realized_pnl_sold_portion),
        "base_realized_pnl_sold_portion": _decimal_str(
            base_realized_pnl_sold_portion
        ),
        "overflow_realized_pnl_sold_portion": _decimal_str(
            overflow_realized_pnl_sold_portion
        ),
        "open_base": _decimal_str(open_base),
        "open_base_value_quote": _decimal_str(open_base * final_close),
        "open_cost_basis": _decimal_str(open_cost_basis),
        "open_unrealized_pnl": _decimal_str(open_unrealized_pnl),
        "base_tranche_open_base": _decimal_str(base_open_quantity),
        "base_tranche_open_base_value_quote": _decimal_str(
            base_open_quantity * final_close
        ),
        "base_tranche_open_cost_basis": _decimal_str(base_open_cost_basis),
        "base_tranche_open_unrealized_pnl": _decimal_str(
            base_open_unrealized_pnl
        ),
        "overflow_tranche_open_base": _decimal_str(overflow_open_quantity),
        "overflow_tranche_open_base_value_quote": _decimal_str(
            overflow_open_quantity * final_close
        ),
        "overflow_tranche_open_cost_basis": _decimal_str(
            overflow_open_cost_basis
        ),
        "overflow_tranche_open_unrealized_pnl": _decimal_str(
            overflow_open_unrealized_pnl
        ),
        "reserve_base": _decimal_str(reserve_base),
        "reserve_value_quote": _decimal_str(reserve_base * final_close),
        "reserve_cost_basis": _decimal_str(reserve_cost_basis),
        "reserve_unrealized_pnl": _decimal_str(reserve_unrealized_pnl),
        "base_tranche_reserve_base": _decimal_str(base_reserve_quantity),
        "base_tranche_reserve_value_quote": _decimal_str(
            base_reserve_quantity * final_close
        ),
        "base_tranche_reserve_cost_basis": _decimal_str(base_reserve_cost_basis),
        "base_tranche_reserve_unrealized_pnl": _decimal_str(
            base_reserve_unrealized_pnl
        ),
        "overflow_tranche_reserve_base": _decimal_str(overflow_reserve_quantity),
        "overflow_tranche_reserve_value_quote": _decimal_str(
            overflow_reserve_quantity * final_close
        ),
        "overflow_tranche_reserve_cost_basis": _decimal_str(
            overflow_reserve_cost_basis
        ),
        "overflow_tranche_reserve_unrealized_pnl": _decimal_str(
            overflow_reserve_unrealized_pnl
        ),
        "maximum_close_sampled_reserve_value_quote": _decimal_str(state.max_reserve_value_quote),
        "final_total_base": _decimal_str(total_base),
        "final_total_base_value_quote": _decimal_str(final_base_value),
        "gross_fees_quote": _decimal_str(fees.gross_fees_quote),
        "base_gross_fees_quote": _decimal_str(base_gross_fees),
        "overflow_gross_fees_quote": _decimal_str(overflow_gross_fees),
        "modeled_rebate_receivable_quote": _decimal_str(fees.rebates_quote),
        "base_modeled_rebate_receivable_quote": _decimal_str(base_rebates),
        "overflow_modeled_rebate_receivable_quote": _decimal_str(overflow_rebates),
        "net_fees_after_modeled_rebate_quote": _decimal_str(fees.net_fees_quote),
        "turnover_quote": _decimal_str(state.turnover_quote),
        "base_turnover_quote": _decimal_str(base_turnover),
        "overflow_turnover_quote": _decimal_str(overflow_turnover),
        "base_trade_decision_fingerprint_sha256": _trade_decision_fingerprint(
            state.events, "base"
        ),
        "overflow_trade_decision_fingerprint_sha256": _trade_decision_fingerprint(
            state.events, "overflow"
        ),
        "final_equity_before_rebate": _decimal_str(final_equity_before_rebate),
        "final_equity_after_modeled_rebate": _decimal_str(final_equity_after_rebate),
        "final_liquidation_fee_estimate": _decimal_str(final_liquidation_fee),
        "final_liquidation_equity_before_rebate": _decimal_str(final_liquidation_equity_before_rebate),
        "net_pnl_before_rebate": _decimal_str(final_equity_before_rebate - quote_start),
        "net_pnl_after_modeled_rebate": _decimal_str(final_equity_after_rebate - quote_start),
        "maximum_close_sampled_drawdown_before_rebate": _decimal_str(max_drawdown_before_rebate),
        "average_completed_holding_seconds": _decimal_str(average_holding),
        "longest_end_open_unrecovered_lot_seconds": _decimal_str(longest_end_open),
        "lot_recovery_diagnostics": lot_diagnostic_summary,
        "no_trade_final_equity": _decimal_str(quote_start),
        "idealized_fee_free_fractional_buy_hold_final_equity": _decimal_str(
            buy_hold_base * final_close
        ),
        "cash_reconciliation_error": _decimal_str(cash_reconciliation_error),
        "base_reconciliation_error": _decimal_str(base_reconciliation_error),
        "pnl_reconciliation_error": _decimal_str(pnl_reconciliation_error),
        "state_active_cash_reconciliation_error": _decimal_str(state_active_error),
        "state_base_active_cash_reconciliation_error": _decimal_str(
            state_base_active_cash_reconciliation_error
        ),
        "state_overflow_active_cash_reconciliation_error": _decimal_str(
            state_overflow_active_cash_reconciliation_error
        ),
        "active_cash_tranche_reconciliation_error": _decimal_str(
            active_cash_tranche_reconciliation_error
        ),
        "state_open_base_reconciliation_error": _decimal_str(state_open_base_error),
        "state_reserve_base_reconciliation_error": _decimal_str(state_reserve_base_error),
        "state_base_reserve_reconciliation_error": _decimal_str(
            state_base_reserve_reconciliation_error
        ),
        "state_overflow_reserve_reconciliation_error": _decimal_str(
            state_overflow_reserve_reconciliation_error
        ),
        "reserve_tranche_reconciliation_error": _decimal_str(
            reserve_tranche_reconciliation_error
        ),
        "band_active_cash_reconciliation_error": _decimal_str(
            band_active_cash_reconciliation_error
        ),
        "band_reserve_base_reconciliation_error": _decimal_str(
            band_reserve_base_reconciliation_error
        ),
        "band_base_active_cash_reconciliation_error": _decimal_str(
            band_base_active_cash_reconciliation_error
        ),
        "band_overflow_active_cash_reconciliation_error": _decimal_str(
            band_overflow_active_cash_reconciliation_error
        ),
        "band_cash_profit_reconciliation_error": _decimal_str(
            band_cash_profit_reconciliation_error
        ),
        "band_base_cash_profit_reconciliation_error": _decimal_str(
            band_base_cash_profit_reconciliation_error
        ),
        "band_overflow_cash_profit_reconciliation_error": _decimal_str(
            band_overflow_cash_profit_reconciliation_error
        ),
        "gross_fee_reconciliation_error": _decimal_str(gross_fee_reconciliation_error),
        "gross_fee_tranche_reconciliation_error": _decimal_str(
            gross_fee_tranche_reconciliation_error
        ),
        "rebate_reconciliation_error": _decimal_str(rebate_reconciliation_error),
        "rebate_tranche_reconciliation_error": _decimal_str(
            rebate_tranche_reconciliation_error
        ),
        "net_fee_reconciliation_error": _decimal_str(net_fee_reconciliation_error),
        "turnover_reconciliation_error": _decimal_str(turnover_reconciliation_error),
        "turnover_tranche_reconciliation_error": _decimal_str(
            turnover_tranche_reconciliation_error
        ),
        "lot_count_tranche_reconciliation_error": lot_count_tranche_reconciliation_error,
        "completed_count_tranche_reconciliation_error": (
            completed_count_tranche_reconciliation_error
        ),
        "filled_buy_count_tranche_reconciliation_error": (
            filled_buy_count_tranche_reconciliation_error
        ),
        "filled_sell_count_tranche_reconciliation_error": (
            filled_sell_count_tranche_reconciliation_error
        ),
        "cash_profit_reconciliation_error": _decimal_str(cash_profit_reconciliation_error),
        "base_cash_profit_reconciliation_error": _decimal_str(
            base_cash_profit_reconciliation_error
        ),
        "overflow_cash_profit_reconciliation_error": _decimal_str(
            overflow_cash_profit_reconciliation_error
        ),
        "cash_profit_tranche_reconciliation_error": _decimal_str(
            cash_profit_tranche_reconciliation_error
        ),
        "base_tranche_base_reconciliation_error": _decimal_str(
            base_tranche_base_reconciliation_error
        ),
        "overflow_tranche_base_reconciliation_error": _decimal_str(
            overflow_tranche_base_reconciliation_error
        ),
        "overflow_cap_excess_error": _decimal_str(overflow_cap_excess_error),
        "base_band_cap_excess_error": _decimal_str(base_band_cap_excess_error),
        "entry_guard_band_reconciliation_error": (
            entry_guard_band_reconciliation_error
        ),
        "entry_guard_tranche_reconciliation_error": (
            entry_guard_tranche_reconciliation_error
        ),
        "entry_guard_reason_reconciliation_error": (
            entry_guard_reason_reconciliation_error
        ),
        "fee_profile": fee_profile_to_report(resolved_fee_profile),
        "limitations": [
            "Candle paths are deterministic assumptions, not observed intraminute order.",
            "Each path traverses previous close to recorded open before the assumed intraminute extremes.",
            "Per-band active-lot caps apply only to base-tranche unrecovered cash, not overflow or reserve exposure.",
            "Base-band and overflow cap headroom is theoretical; slot eligibility can make less cash deployable.",
            "Overflow is a ceiling on shared-cash deployment, not a second wallet or guaranteed allocation.",
            "Overflow uses one duplicate tranche per price level and favors the first eligible levels in path order.",
            "Utilization, drawdown, and maximum reserve value are sampled at candle closes.",
            "quote_increment rounds the per-slot cash allocation; it is not price or settlement rounding.",
            "Modeled Coinbase One rebates are nonspendable receivables; pre-rebate equity is authoritative.",
            "The rebate cap is simplified across the full run and does not reset at a membership-month boundary.",
            "Fallback candles cannot prove maker status, spread, depth, queue position, or partial fills.",
            "Reserve remains exposed to the product price and is not risk-free or reusable cash.",
            "The optional EMA entry guard is a price-only prior-close signal; it cannot protect existing lots or reserve and does not prove executable fills.",
            "The EMA guard counts observed one-minute closes, does not impute missing candles, and blocks only the first decision after a sub-24-hour gap.",
            "Lot recovery diagnostics are post-trade labels and never affect simulated decisions.",
            "Path adverse excursion is assumption-dependent; close markouts are price-only and not executable L2 markouts.",
            "No live, paper, or exchange order placement is present.",
        ],
    }
    band_rows = [_band_to_row(band_ledgers[band.band_id], final_close, lots) for band in bands]
    lot_rows = [_lot_to_row(lot) for lot in sorted(lots.values(), key=lambda item: item.lot_id)]
    return {
        "summary": summary,
        "bands": band_rows,
        "lots": lot_rows,
        "lot_diagnostics": lot_diagnostic_rows,
        "events": state.events,
        "equity_curve": equity_curve,
    }


def run_reserve_gridbot_backtest(
    *,
    derived_root: Path,
    catalog_root: Path,
    product: str,
    lower: str,
    upper: str,
    band_width: str,
    levels_per_band: int,
    band_active_lot_budget_cap: str,
    quote_start: str,
    exit_move_pct: str,
    cash_profit_bps: str,
    overflow_global_active_lot_budget_cap: str = "0",
    entry_guard: str = "none",
    entry_guard_fast_ema_span_candles: int = 360,
    entry_guard_slow_ema_span_candles: int = 1440,
    base_increment: str = "0.000001",
    quote_increment: str = "0.01",
    price_increment: str = "0.0001",
    min_quote_notional: str = "1",
    exit_policy: str = "principal_recovery",
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
) -> dict[str, Any]:
    product_id = normalize_product_id(product)
    if not product_id:
        raise ValueError("product must normalize to a product id such as XRP-USD")
    if product_id != "XRP-USD":
        raise ValueError("gridbot-reserve-backtest v1 supports XRP-USD only")
    if not include_fallback_candles:
        raise ValueError("gridbot-reserve-backtest v1 requires --include-fallback-candles")
    if entry_guard not in {"none", "ema_cross"}:
        raise ValueError("entry_guard must be none or ema_cross")
    if entry_guard_fast_ema_span_candles < 2:
        raise ValueError("entry_guard_fast_ema_span_candles must be at least 2")
    if entry_guard_slow_ema_span_candles <= entry_guard_fast_ema_span_candles:
        raise ValueError(
            "entry_guard_slow_ema_span_candles must exceed the fast span"
        )
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
    config = ReserveGridConfig(
        product_id=product_id,
        lower=str(lower),
        upper=str(upper),
        band_width=str(band_width),
        levels_per_band=levels_per_band,
        band_active_lot_budget_cap=str(band_active_lot_budget_cap),
        quote_start=str(quote_start),
        exit_move_pct=str(exit_move_pct),
        cash_profit_bps=str(cash_profit_bps),
        base_increment=str(base_increment),
        quote_increment=str(quote_increment),
        price_increment=str(price_increment),
        min_quote_notional=str(min_quote_notional),
        fee_rate=str(fee_rate),
        include_fallback_candles=include_fallback_candles,
        candle_path_assumption=candle_path_assumption,
        exit_policy=exit_policy,
        overflow_global_active_lot_budget_cap=str(
            overflow_global_active_lot_budget_cap
        ),
        entry_guard=entry_guard,
        entry_guard_fast_ema_span_candles=entry_guard_fast_ema_span_candles,
        entry_guard_slow_ema_span_candles=entry_guard_slow_ema_span_candles,
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
    )
    trading_candles, candle_report = load_fallback_candles(
        derived_root=derived_root,
        product_id=product_id,
        start=start,
        end=end,
        providers=providers,
        max_rows=max_rows,
    )
    signal_preroll_candles: list[dict[str, Any]] = []
    signal_preroll_report: dict[str, Any] | None = None
    if entry_guard != "none" and start:
        signal_candidates, signal_preroll_report = load_fallback_candles(
            derived_root=derived_root,
            product_id=product_id,
            end=start,
            providers=providers,
            tail_rows=entry_guard_slow_ema_span_candles,
        )
        signal_preroll_candles = signal_candidates
    candles = signal_preroll_candles + trading_candles
    input_sources: list[dict[str, Any]] = []
    for source_text in sorted({str(row.get("source_path")) for row in candles if row.get("source_path")}):
        source_path = Path(source_text)
        input_sources.append(
            {
                "source_path": str(source_path),
                "size_bytes": source_path.stat().st_size if source_path.exists() else None,
                "sha256": _sha256(source_path) if source_path.is_file() else None,
            }
        )
    candle_report["input_sources"] = input_sources
    candle_report["trading_derived_sources"] = (
        _selected_derived_sources_with_hashes(candle_report)
    )
    candle_report["signal_preroll_derived_sources"] = (
        _selected_derived_sources_with_hashes(signal_preroll_report)
    )
    candle_report["selected_candle_rows_sha256"] = _selected_candle_rows_sha256(
        trading_candles
    )
    candle_report["selected_trading_candle_rows_sha256"] = (
        candle_report["selected_candle_rows_sha256"]
    )
    candle_report["selected_signal_preroll_rows_sha256"] = (
        _selected_candle_rows_sha256(signal_preroll_candles)
        if signal_preroll_candles
        else None
    )
    candle_report["signal_preroll_rows_loaded"] = len(signal_preroll_candles)
    candle_report["signal_preroll_first_start_ts"] = (
        signal_preroll_candles[0].get("start_ts")
        if signal_preroll_candles
        else None
    )
    candle_report["signal_preroll_last_start_ts"] = (
        signal_preroll_candles[-1].get("start_ts")
        if signal_preroll_candles
        else None
    )
    candle_report["signal_preroll_loader_report"] = signal_preroll_report
    candle_report["selected_candle_hash_schema"] = SELECTED_CANDLE_HASH_SCHEMA
    result = simulate_reserve_gridbot_on_candles(candles, config, fee_profile=fee_profile)
    result["summary"]["candle_input_report"] = candle_report
    result["summary"]["engine_source_sha256"] = _sha256(Path(__file__))
    result["summary"]["candle_loader_source_sha256"] = _sha256(
        Path(load_fallback_candles.__code__.co_filename)
    )
    run_id = _new_run_id()
    config_payload = asdict(config)
    config_sha256 = hashlib.sha256(
        json.dumps(
            config_payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            default=str,
        ).encode("utf-8")
    ).hexdigest()
    result["summary"]["run_id"] = run_id
    result["summary"]["generated_at_utc"] = (
        datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    )
    result["summary"]["config_sha256"] = config_sha256
    run_dir = derived_root / "v1" / "backtests" / "gridbot_reserve" / run_id
    report_path = catalog_root / "quality" / f"gridbot_reserve_backtest_{run_id}.json"
    run_dir.mkdir(parents=True, exist_ok=False)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    (run_dir / "config.json").write_text(
        json.dumps(config_payload, indent=2, sort_keys=True), encoding="utf-8"
    )
    write_jsonl(run_dir / "events.jsonl", result["events"])
    write_jsonl(run_dir / "lots.jsonl", result["lots"])
    write_jsonl(run_dir / "lot_diagnostics.jsonl", result["lot_diagnostics"])
    write_jsonl(run_dir / "bands.jsonl", result["bands"])
    write_jsonl(run_dir / "equity_curve.jsonl", result["equity_curve"])
    (run_dir / "summary.json").write_text(
        json.dumps(result["summary"], indent=2, sort_keys=True), encoding="utf-8"
    )
    with report_path.open("x", encoding="utf-8") as handle:
        handle.write(json.dumps(result["summary"], indent=2, sort_keys=True))
    return {
        "run_id": run_id,
        "run_dir": str(run_dir),
        "report_path": str(report_path),
        "summary": result["summary"],
    }
