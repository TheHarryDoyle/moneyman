from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from .book import audit_book_reconstruction_run, discover_audited_book_runs
from .fees import FeeAccumulator, FeeProfile, fee_profile_to_report
from .raw import iter_jsonl


STRICT_L2_FILL_ENGINE = "strict_l2_gridbot_v1"
STRICT_QUEUE_POLICY = "strict_price_through"
STRICT_PARTIAL_REMAINDER_POLICY = "cancel"
STRICT_VISIBLE_DEPTH_POLICY = "observed_delta_shadow_v1"
STRICT_EVENT_COHORT_POLICY = "resting_before_arrival_price_priority"
_TIMESTAMP_RE = re.compile(
    r"^(?P<whole>\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})"
    r"(?:\.(?P<fraction>\d{1,9}))?"
    r"(?P<offset>Z|[+-]\d{2}:\d{2})$"
)
_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


class AuditedBookSelectionError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class StrictL2FillConfig:
    latency_ms: int = 100
    clock_source: str = "message_ts"
    queue_policy: str = STRICT_QUEUE_POLICY
    partial_remainder_policy: str = STRICT_PARTIAL_REMAINDER_POLICY

    def __post_init__(self) -> None:
        if isinstance(self.latency_ms, bool) or not isinstance(self.latency_ms, int):
            raise ValueError("latency_ms must be an integer")
        if self.latency_ms < 0:
            raise ValueError("latency_ms must be nonnegative")
        if self.clock_source not in {"message_ts", "recv_ts"}:
            raise ValueError("clock_source must be message_ts or recv_ts")
        if self.queue_policy != STRICT_QUEUE_POLICY:
            raise ValueError(f"queue_policy must be {STRICT_QUEUE_POLICY}")
        if self.partial_remainder_policy != STRICT_PARTIAL_REMAINDER_POLICY:
            raise ValueError(
                f"partial_remainder_policy must be {STRICT_PARTIAL_REMAINDER_POLICY}"
            )


@dataclass
class StrictL2Order:
    order_id: str
    side: str
    level_index: int
    limit_price: Decimal
    target_base: Decimal
    decision_ts: str
    decision_ns: int
    decision_sequence_num: int
    eligible_after_ns: int
    allow_same_row_activation: bool
    status: str = "pending_latency"
    arrival_ts: str | None = None
    arrival_ns: int | None = None
    arrival_sequence_num: int | None = None
    filled_base: Decimal = Decimal("0")
    canceled_base: Decimal = Decimal("0")


@dataclass
class StrictL2Portfolio:
    quote: Decimal
    base: Decimal
    rebate_receivable_quote: Decimal = Decimal("0")
    buy_base: Decimal = Decimal("0")
    sell_base: Decimal = Decimal("0")
    buy_notional_quote: Decimal = Decimal("0")
    sell_notional_quote: Decimal = Decimal("0")
    buy_gross_fees_quote: Decimal = Decimal("0")
    sell_gross_fees_quote: Decimal = Decimal("0")
    fees_gross_quote: Decimal = Decimal("0")
    fees_net_quote: Decimal = Decimal("0")
    turnover_quote: Decimal = Decimal("0")
    filled_orders: int = 0
    filled_buys: int = 0
    filled_sells: int = 0
    partial_fills: int = 0
    partial_buys: int = 0
    partial_sells: int = 0
    maker_fills: int = 0
    taker_fills: int = 0
    missed_buys_quote: int = 0
    missed_sells_base: int = 0
    canceled_no_visible_depth: int = 0
    canceled_window_end: int = 0


def _decimal(value: Any, name: str) -> Decimal:
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"{name} must be a decimal number") from exc
    if not parsed.is_finite():
        raise ValueError(f"{name} must be finite")
    return parsed


def _decimal_str(value: Decimal) -> str:
    if value == 0:
        return "0"
    return format(value.normalize(), "f")


def _sha256_json(value: Any) -> str:
    encoded = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _sha256_rows(rows: list[dict[str, Any]]) -> str:
    digest = hashlib.sha256()
    for row in rows:
        digest.update(
            json.dumps(
                row,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
            ).encode("utf-8")
        )
        digest.update(b"\n")
    return digest.hexdigest()


def _fill_contract_config(config: StrictL2FillConfig) -> dict[str, Any]:
    return {
        **asdict(config),
        "visible_depth_policy": STRICT_VISIBLE_DEPTH_POLICY,
        "event_cohort_policy": STRICT_EVENT_COHORT_POLICY,
        "maker_execution_price": "limit_price",
        "taker_execution_price": "visible_book_vwap",
    }


def _timestamp_ns(value: Any, name: str) -> int:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{name} must be a nonempty UTC timestamp")
    match = _TIMESTAMP_RE.fullmatch(value)
    if match is None:
        raise ValueError(f"{name} must be an ISO-8601 timestamp with a timezone")
    offset = match.group("offset")
    offset_text = "+00:00" if offset == "Z" else offset
    parsed = datetime.fromisoformat(f"{match.group('whole')}{offset_text}")
    parsed_utc = parsed.astimezone(timezone.utc)
    delta = parsed_utc - _EPOCH
    whole_seconds = (delta.days * 86_400) + delta.seconds
    fraction = (match.group("fraction") or "").ljust(9, "0")
    return (whole_seconds * 1_000_000_000) + int(fraction or "0")


def _format_timestamp_ns(value: int) -> str:
    seconds, nanoseconds = divmod(value, 1_000_000_000)
    whole = datetime.fromtimestamp(seconds, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
    if nanoseconds == 0:
        return f"{whole}Z"
    return f"{whole}.{nanoseconds:09d}Z"


def _row_clock(row: dict[str, Any], clock_source: str) -> tuple[str, int]:
    value = row.get(clock_source)
    return str(value or ""), _timestamp_ns(value, clock_source)


def _sequence_num(row: dict[str, Any]) -> int:
    value = row.get("sequence_num")
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError("book row sequence_num must be a nonnegative integer")
    return value


def _level_rows(
    row: dict[str, Any],
    name: str,
    reverse: bool,
) -> list[list[Decimal]]:
    raw_levels = row.get(name)
    if not isinstance(raw_levels, list) or not raw_levels:
        raise ValueError(f"book row {name} must be a nonempty list")
    parsed: list[list[Decimal]] = []
    for index, raw in enumerate(raw_levels):
        if not isinstance(raw, dict):
            raise ValueError(f"{name}[{index}] must be an object")
        price = _decimal(raw.get("price"), f"{name}[{index}].price")
        quantity = _decimal(raw.get("quantity"), f"{name}[{index}].quantity")
        if price <= 0 or quantity <= 0:
            raise ValueError(f"{name} prices and quantities must be positive")
        parsed.append([price, quantity])
    expected = sorted(parsed, key=lambda level: level[0], reverse=reverse)
    if parsed != expected:
        raise ValueError(f"book row {name} is not in price priority")
    return parsed


class _VisibleDepthLedger:
    """Persist conservative simulated consumption across emitted book rows.

    Coinbase snapshots are counterfactual to this simulation: a later row cannot
    reflect quantity that our hypothetical order consumed.  For each visible
    side/price, the ledger therefore carries a shadow remaining quantity.  Only
    a positive observed absolute-quantity delta replenishes it; an observed
    decrease removes shadow quantity, and an unchanged row never restores it.
    Prices that leave the emitted top-N retain their last observation so that
    re-entry is not mistaken for wholly new depth.
    """

    def __init__(self) -> None:
        self.bids: list[list[Decimal]] = []
        self.asks: list[list[Decimal]] = []
        self._observed: dict[tuple[str, Decimal], Decimal] = {}
        self._remaining: dict[tuple[str, Decimal], Decimal] = {}

    def update(self, row: dict[str, Any]) -> None:
        parsed = {
            "bid": _level_rows(row, "bid_levels", reverse=True),
            "ask": _level_rows(row, "ask_levels", reverse=False),
        }
        visible: dict[str, list[list[Decimal]]] = {"bid": [], "ask": []}
        for book_side, levels in parsed.items():
            for price, observed_quantity in levels:
                key = (book_side, price)
                previous_observed = self._observed.get(key)
                if previous_observed is None:
                    remaining = observed_quantity
                else:
                    observed_delta = observed_quantity - previous_observed
                    remaining = max(
                        Decimal("0"),
                        self._remaining[key] + observed_delta,
                    )
                self._observed[key] = observed_quantity
                self._remaining[key] = remaining
                visible[book_side].append([price, remaining])
        self.bids = visible["bid"]
        self.asks = visible["ask"]

    def best_ask(self) -> Decimal | None:
        return next((price for price, quantity in self.asks if quantity > 0), None)

    def best_bid(self) -> Decimal | None:
        return next((price for price, quantity in self.bids if quantity > 0), None)

    def consume(
        self,
        side: str,
        limit_price: Decimal,
        target_base: Decimal,
        strict: bool,
    ) -> tuple[Decimal, Decimal, list[dict[str, str]]]:
        book_side = "ask" if side == "buy" else "bid"
        levels = self.asks if side == "buy" else self.bids
        remaining = target_base
        filled = Decimal("0")
        book_notional = Decimal("0")
        consumed: list[dict[str, str]] = []
        for level in levels:
            price, available = level
            if remaining <= 0:
                break
            if side == "buy":
                eligible = price < limit_price if strict else price <= limit_price
            else:
                eligible = price > limit_price if strict else price >= limit_price
            if not eligible:
                continue
            take = min(available, remaining)
            if take <= 0:
                continue
            level[1] -= take
            self._remaining[(book_side, price)] -= take
            remaining -= take
            filled += take
            book_notional += price * take
            consumed.append(
                {
                    "book_price": _decimal_str(price),
                    "base_quantity": _decimal_str(take),
                }
            )
        return filled, book_notional, consumed


class StrictL2FillEngine:
    def __init__(
        self,
        levels: list[Decimal],
        order_quote: Decimal,
        quote_start: Decimal,
        base_start: Decimal,
        fee_profile: FeeProfile,
        config: StrictL2FillConfig,
        product_id: str = "XRP-USD",
        window_id: str = "window-1",
    ) -> None:
        if not levels:
            raise ValueError("levels must not be empty")
        self.levels = [_decimal(level, "grid level") for level in levels]
        if any(level <= 0 for level in self.levels):
            raise ValueError("grid levels must be positive")
        if self.levels != sorted(set(self.levels)):
            raise ValueError("grid levels must be unique and increasing")
        self.order_quote = _decimal(order_quote, "order_quote")
        self.initial_quote = _decimal(quote_start, "quote_start")
        self.initial_base = _decimal(base_start, "base_start")
        if self.order_quote <= 0:
            raise ValueError("order_quote must be positive")
        if self.initial_quote < 0 or self.initial_base < 0:
            raise ValueError("starting balances must be nonnegative")
        self.product_id = product_id
        self.window_id = window_id
        self.config = config
        self._fill_engine_source_sha256 = _sha256_file(Path(__file__))
        self._fill_contract_config = _fill_contract_config(config)
        self._fill_contract_config_sha256 = _sha256_json(
            self._fill_contract_config
        )
        self.fees = FeeAccumulator(fee_profile)
        self.fee_profile = fee_profile
        self.state = StrictL2Portfolio(quote=self.initial_quote, base=self.initial_base)
        self.active_orders: dict[tuple[str, int], StrictL2Order] = {}
        self.orders: dict[str, StrictL2Order] = {}
        self.fills: list[dict[str, Any]] = []
        self.order_events: list[dict[str, Any]] = []
        self.equity_curve: list[dict[str, Any]] = []
        self._order_counter = 0
        self._last_clock_ns: int | None = None
        self._last_sequence_num: int | None = None
        self._last_row: dict[str, Any] | None = None
        self._initial_equity_quote: Decimal | None = None
        self._maximum_equity_quote: Decimal | None = None
        self._maximum_drawdown_quote = Decimal("0")
        self._depth = _VisibleDepthLedger()
        self._closed = False

    def submit_order(
        self,
        side: str,
        level_index: int,
        decision_row: dict[str, Any],
        allow_same_row_activation: bool = False,
    ) -> StrictL2Order | None:
        if self._closed:
            raise RuntimeError("cannot submit an order after window close")
        if side not in {"buy", "sell"}:
            raise ValueError("order side must be buy or sell")
        if isinstance(level_index, bool) or not isinstance(level_index, int):
            raise ValueError("level_index must be an integer")
        if not 0 <= level_index < len(self.levels):
            raise ValueError("level_index is outside the grid")
        key = (side, level_index)
        if key in self.active_orders:
            return None
        decision_ts, decision_ns = _row_clock(decision_row, self.config.clock_source)
        decision_sequence_num = _sequence_num(decision_row)
        self._order_counter += 1
        order = StrictL2Order(
            order_id=f"order-{self._order_counter:06d}",
            side=side,
            level_index=level_index,
            limit_price=self.levels[level_index],
            target_base=self.order_quote / self.levels[level_index],
            decision_ts=decision_ts,
            decision_ns=decision_ns,
            decision_sequence_num=decision_sequence_num,
            eligible_after_ns=decision_ns + (self.config.latency_ms * 1_000_000),
            allow_same_row_activation=allow_same_row_activation,
        )
        self.active_orders[key] = order
        self.orders[order.order_id] = order
        self._append_order_event(
            order,
            decision_row,
            status="submitted",
            reason="initial_grid" if allow_same_row_activation else "adjacent_level_rearm",
        )
        return order

    def on_book(self, row: dict[str, Any]) -> None:
        if self._closed:
            raise RuntimeError("cannot process a book row after window close")
        self._validate_row_identity(row)
        row_ts, row_ns = _row_clock(row, self.config.clock_source)
        sequence_num = _sequence_num(row)
        if self._last_clock_ns is not None and row_ns < self._last_clock_ns:
            raise ValueError("book clock regressed inside the selected window")
        if self._last_sequence_num is not None and sequence_num <= self._last_sequence_num:
            raise ValueError("book sequence did not increase inside the selected window")

        midpoint = _decimal(row.get("midpoint"), "midpoint")
        if midpoint <= 0:
            raise ValueError("midpoint must be positive")
        if self._initial_equity_quote is None:
            self._initial_equity_quote = self.state.quote + (self.state.base * midpoint)
            self._maximum_equity_quote = self._initial_equity_quote

        self._depth.update(row)
        depth = self._depth
        resting_before = [
            order for order in self.active_orders.values() if order.status == "resting"
        ]
        pending_due = [
            order
            for order in self.active_orders.values()
            if order.status == "pending_latency"
            and row_ns >= order.eligible_after_ns
            and (
                order.allow_same_row_activation
                or sequence_num > order.decision_sequence_num
            )
        ]

        # A previously resting order existed before the event represented by
        # this emitted state.  Evaluate that cohort first.  Orders whose
        # deterministic latency expires only on this row arrive against the
        # remaining shadow depth afterward.  This avoids inventing sub-row
        # priority for a newly arriving order.
        for order in sorted(resting_before, key=self._order_priority):
            if not self._is_active(order) or order.status != "resting":
                continue
            if self._is_marketable(order, depth, strict=True):
                self._execute(order, row, depth, liquidity="maker", strict=True)

        for order in sorted(pending_due, key=self._order_priority):
            if not self._is_active(order):
                continue
            order.arrival_ts = row_ts
            order.arrival_ns = row_ns
            order.arrival_sequence_num = sequence_num
            if self._is_marketable(order, depth, strict=False):
                self._append_order_event(
                    order,
                    row,
                    status="arrived_marketable",
                    reason="crossed_visible_spread_on_arrival",
                )
                self._execute(order, row, depth, liquidity="taker", strict=False)
            else:
                order.status = "resting"
                self._append_order_event(
                    order,
                    row,
                    status="resting",
                    reason="did_not_cross_visible_spread_on_arrival",
                )

        equity = self.state.quote + self.state.rebate_receivable_quote + (
            self.state.base * midpoint
        )
        if self._maximum_equity_quote is None:
            self._maximum_equity_quote = equity
        self._maximum_equity_quote = max(self._maximum_equity_quote, equity)
        drawdown = self._maximum_equity_quote - equity
        self._maximum_drawdown_quote = max(self._maximum_drawdown_quote, drawdown)
        self.equity_curve.append(
            {
                "ts": row_ts,
                "sequence_num": sequence_num,
                "midpoint": _decimal_str(midpoint),
                "quote_balance": _decimal_str(self.state.quote),
                "base_balance": _decimal_str(self.state.base),
                "rebate_balance_quote": _decimal_str(
                    self.state.rebate_receivable_quote
                ),
                "equity_quote": _decimal_str(equity),
                "drawdown_quote": _decimal_str(drawdown),
                "window_id": self.window_id,
            }
        )
        self._last_clock_ns = row_ns
        self._last_sequence_num = sequence_num
        self._last_row = row

    def close_window(self) -> None:
        if self._closed:
            return
        for order in sorted(list(self.active_orders.values()), key=self._order_priority):
            if not self._is_active(order):
                continue
            order.status = "canceled_window_end"
            order.canceled_base = order.target_base - order.filled_base
            self.state.canceled_window_end += 1
            self._append_order_event(
                order,
                self._last_row,
                status="canceled_window_end",
                reason="audited_window_or_selected_slice_ended",
            )
            self.active_orders.pop((order.side, order.level_index), None)
        self._closed = True

    def result(self) -> dict[str, Any]:
        if self._last_row is None or self._initial_equity_quote is None:
            raise ValueError("at least one book row is required")
        if not self._closed:
            self.close_window()
        final_midpoint = _decimal(self._last_row.get("midpoint"), "midpoint")
        expected_quote = (
            self.initial_quote
            - self.state.buy_notional_quote
            - self.state.buy_gross_fees_quote
            + self.state.sell_notional_quote
            - self.state.sell_gross_fees_quote
        )
        expected_base = self.initial_base + self.state.buy_base - self.state.sell_base
        quote_error = self.state.quote - expected_quote
        base_error = self.state.base - expected_base
        fee_error = self.state.fees_gross_quote - (
            self.state.buy_gross_fees_quote + self.state.sell_gross_fees_quote
        )
        turnover_error = self.state.turnover_quote - (
            self.state.buy_notional_quote + self.state.sell_notional_quote
        )
        if any(
            value != 0
            for value in (quote_error, base_error, fee_error, turnover_error)
        ):
            raise AssertionError(
                "strict L2 cash/base/fee conservation failed: "
                f"quote={_decimal_str(quote_error)} "
                f"base={_decimal_str(base_error)} "
                f"fee={_decimal_str(fee_error)} "
                f"turnover={_decimal_str(turnover_error)}"
            )
        if self.state.quote < 0 or self.state.base < 0:
            raise AssertionError("strict L2 balances became negative")
        if _sha256_file(Path(__file__)) != self._fill_engine_source_sha256:
            raise AssertionError("strict L2 fill engine source changed during the run")

        final_equity_before_rebate = self.state.quote + (self.state.base * final_midpoint)
        final_equity = final_equity_before_rebate + self.state.rebate_receivable_quote
        summary = {
            "status": "completed",
            "engine": STRICT_L2_FILL_ENGINE,
            "product_id": self.product_id,
            "mode": "strict_l2",
            "window_id": self.window_id,
            "latency_ms": self.config.latency_ms,
            "clock_source": self.config.clock_source,
            "queue_policy": self.config.queue_policy,
            "partial_remainder_policy": self.config.partial_remainder_policy,
            "visible_depth_policy": STRICT_VISIBLE_DEPTH_POLICY,
            "event_cohort_policy": STRICT_EVENT_COHORT_POLICY,
            "fill_engine_source_sha256": self._fill_engine_source_sha256,
            "fill_contract_config": self._fill_contract_config,
            "fill_contract_config_sha256": self._fill_contract_config_sha256,
            "orders_submitted": len(self.orders),
            "filled_orders": self.state.filled_orders,
            "filled_buys": self.state.filled_buys,
            "filled_sells": self.state.filled_sells,
            "partial_fills": self.state.partial_fills,
            "partial_buys": self.state.partial_buys,
            "partial_sells": self.state.partial_sells,
            "maker_fills": self.state.maker_fills,
            "taker_fills": self.state.taker_fills,
            "missed_buys_insufficient_quote": self.state.missed_buys_quote,
            "missed_sells_insufficient_base": self.state.missed_sells_base,
            "canceled_no_visible_depth": self.state.canceled_no_visible_depth,
            "canceled_window_end": self.state.canceled_window_end,
            "buy_base": _decimal_str(self.state.buy_base),
            "sell_base": _decimal_str(self.state.sell_base),
            "buy_notional_quote": _decimal_str(self.state.buy_notional_quote),
            "sell_notional_quote": _decimal_str(self.state.sell_notional_quote),
            "turnover_quote": _decimal_str(self.state.turnover_quote),
            "fees_gross_quote": _decimal_str(self.state.fees_gross_quote),
            "fee_rebates_quote": _decimal_str(
                self.state.rebate_receivable_quote
            ),
            "fees_net_quote": _decimal_str(self.state.fees_net_quote),
            "initial_equity_quote": _decimal_str(self._initial_equity_quote),
            "final_equity_before_rebate_quote": _decimal_str(
                final_equity_before_rebate
            ),
            "final_equity_quote": _decimal_str(final_equity),
            "net_pnl_quote": _decimal_str(final_equity - self._initial_equity_quote),
            "max_drawdown_quote": _decimal_str(self._maximum_drawdown_quote),
            "final_quote_balance": _decimal_str(self.state.quote),
            "final_base_balance": _decimal_str(self.state.base),
            "final_rebate_balance_quote": _decimal_str(
                self.state.rebate_receivable_quote
            ),
            "quote_reconciliation_error": _decimal_str(quote_error),
            "base_reconciliation_error": _decimal_str(base_error),
            "fee_reconciliation_error": _decimal_str(fee_error),
            "turnover_reconciliation_error": _decimal_str(turnover_error),
            "fee_profile": fee_profile_to_report(self.fee_profile),
        }
        return {
            "summary": summary,
            "fills": self.fills,
            "order_events": self.order_events,
            "equity_curve": self.equity_curve,
        }

    def _validate_row_identity(self, row: dict[str, Any]) -> None:
        if row.get("product_id") != self.product_id:
            raise ValueError("book row product does not match the strict engine")
        if row.get("window_id") != self.window_id:
            raise ValueError("book row crossed the selected audited window boundary")
        if row.get("validity_status") != "valid" or not row.get("strict_l2_eligible"):
            raise ValueError("book row is not strict-L2 eligible")

    def _is_active(self, order: StrictL2Order) -> bool:
        return self.active_orders.get((order.side, order.level_index)) is order

    @staticmethod
    def _order_priority(order: StrictL2Order) -> tuple[int, Decimal, str]:
        if order.side == "buy":
            return (0, -order.limit_price, order.order_id)
        return (1, order.limit_price, order.order_id)

    @staticmethod
    def _is_marketable(
        order: StrictL2Order,
        depth: _VisibleDepthLedger,
        strict: bool,
    ) -> bool:
        if order.side == "buy":
            best = depth.best_ask()
            return bool(
                best is not None
                and (best < order.limit_price if strict else best <= order.limit_price)
            )
        best = depth.best_bid()
        return bool(
            best is not None
            and (best > order.limit_price if strict else best >= order.limit_price)
        )

    def _execute(
        self,
        order: StrictL2Order,
        row: dict[str, Any],
        depth: _VisibleDepthLedger,
        liquidity: str,
        strict: bool,
    ) -> None:
        if order.side == "buy":
            required_quote = self.order_quote + self.fees.gross_fee_for(
                self.order_quote,
                liquidity,
            )
            if self.state.quote < required_quote:
                order.status = "missed_insufficient_quote"
                self.state.missed_buys_quote += 1
                self.active_orders.pop((order.side, order.level_index), None)
                self._append_order_event(
                    order,
                    row,
                    status=order.status,
                    reason="full_original_order_not_fundable_at_limit_plus_gross_fee",
                )
                return
        elif self.state.base < order.target_base:
            order.status = "missed_insufficient_base"
            self.state.missed_sells_base += 1
            self.active_orders.pop((order.side, order.level_index), None)
            self._append_order_event(
                order,
                row,
                status=order.status,
                reason="full_original_sell_quantity_not_owned",
            )
            return

        filled_base, book_notional, consumed = depth.consume(
            side=order.side,
            limit_price=order.limit_price,
            target_base=order.target_base,
            strict=strict,
        )
        if filled_base <= 0:
            order.status = "canceled_no_visible_depth"
            order.canceled_base = order.target_base
            self.state.canceled_no_visible_depth += 1
            self.active_orders.pop((order.side, order.level_index), None)
            self._append_order_event(
                order,
                row,
                status=order.status,
                reason="no_eligible_visible_depth",
            )
            return

        notional_quote = (
            order.limit_price * filled_base if liquidity == "maker" else book_notional
        )
        fee_quote = self.fees.quote(notional_quote, liquidity)
        if order.side == "buy":
            self.state.buy_base += filled_base
            self.state.buy_notional_quote += notional_quote
            self.state.buy_gross_fees_quote += fee_quote.gross_fee_quote
        else:
            self.state.sell_base += filled_base
            self.state.sell_notional_quote += notional_quote
            self.state.sell_gross_fees_quote += fee_quote.gross_fee_quote
        self.state.quote = (
            self.initial_quote
            - self.state.buy_notional_quote
            - self.state.buy_gross_fees_quote
            + self.state.sell_notional_quote
            - self.state.sell_gross_fees_quote
        )
        self.state.base = self.initial_base + self.state.buy_base - self.state.sell_base
        self.state.rebate_receivable_quote += fee_quote.rebate_quote
        self.state.fees_gross_quote += fee_quote.gross_fee_quote
        self.state.fees_net_quote += fee_quote.net_fee_quote
        self.state.turnover_quote += notional_quote
        if liquidity == "maker":
            self.state.maker_fills += 1
        else:
            self.state.taker_fills += 1

        order.filled_base = filled_base
        remaining = order.target_base - filled_base
        is_full = remaining == 0
        if is_full:
            order.status = "filled"
            self.state.filled_orders += 1
            if order.side == "buy":
                self.state.filled_buys += 1
            else:
                self.state.filled_sells += 1
        else:
            order.status = "partial_canceled"
            order.canceled_base = remaining
            self.state.partial_fills += 1
            if order.side == "buy":
                self.state.partial_buys += 1
            else:
                self.state.partial_sells += 1

        average_price = notional_quote / filled_base
        row_ts, row_ns = _row_clock(row, self.config.clock_source)
        if order.arrival_ns is None:
            raise AssertionError("executed strict L2 order has no recorded arrival")
        fill_row = {
            "ts": row_ts,
            "sequence_num": _sequence_num(row),
            "order_id": order.order_id,
            "side": order.side,
            "grid_level_index": order.level_index,
            "limit_price": _decimal_str(order.limit_price),
            "price": _decimal_str(average_price),
            "target_base_quantity": _decimal_str(order.target_base),
            "base_quantity": _decimal_str(filled_base),
            "canceled_base_quantity": _decimal_str(remaining),
            "notional_quote": _decimal_str(notional_quote),
            "liquidity": liquidity,
            "fee_rate": _decimal_str(fee_quote.fee_rate),
            "fee_gross_quote": _decimal_str(fee_quote.gross_fee_quote),
            "fee_rebate_quote": _decimal_str(fee_quote.rebate_quote),
            "fee_net_quote": _decimal_str(fee_quote.net_fee_quote),
            "status": order.status,
            "queue_policy": self.config.queue_policy,
            "visible_depth_consumed": consumed,
            "decision_ts": order.decision_ts,
            "arrival_ts": order.arrival_ts,
            "configured_latency_ms": self.config.latency_ms,
            "arrival_latency_ms": _decimal_str(
                Decimal(order.arrival_ns - order.decision_ns) / Decimal(1_000_000)
            ),
            "decision_to_fill_ms": _decimal_str(
                Decimal(row_ns - order.decision_ns) / Decimal(1_000_000)
            ),
            "resting_time_ms": _decimal_str(
                Decimal(row_ns - order.arrival_ns) / Decimal(1_000_000)
            ),
            "quote_balance": _decimal_str(self.state.quote),
            "base_balance": _decimal_str(self.state.base),
            "rebate_balance_quote": _decimal_str(
                self.state.rebate_receivable_quote
            ),
            "best_bid": str(row.get("best_bid")),
            "best_ask": str(row.get("best_ask")),
            "product_id": self.product_id,
            "window_id": self.window_id,
        }
        self.fills.append(fill_row)
        self.active_orders.pop((order.side, order.level_index), None)
        self._append_order_event(
            order,
            row,
            status=order.status,
            reason=(
                "full_visible_execution"
                if is_full
                else "partial_visible_execution_remainder_canceled"
            ),
        )

        if is_full:
            if order.side == "buy" and order.level_index + 1 < len(self.levels):
                self.submit_order(
                    "sell",
                    order.level_index + 1,
                    row,
                    allow_same_row_activation=False,
                )
            elif order.side == "sell" and order.level_index - 1 >= 0:
                self.submit_order(
                    "buy",
                    order.level_index - 1,
                    row,
                    allow_same_row_activation=False,
                )

    def _append_order_event(
        self,
        order: StrictL2Order,
        row: dict[str, Any] | None,
        status: str,
        reason: str,
    ) -> None:
        event_row = row or {}
        timestamp = event_row.get(self.config.clock_source) or order.decision_ts
        self.order_events.append(
            {
                "ts": timestamp,
                "sequence_num": event_row.get("sequence_num"),
                "order_id": order.order_id,
                "side": order.side,
                "grid_level_index": order.level_index,
                "limit_price": _decimal_str(order.limit_price),
                "target_base_quantity": _decimal_str(order.target_base),
                "status": status,
                "reason": reason,
                "decision_ts": order.decision_ts,
                "eligible_after_ts": _format_timestamp_ns(order.eligible_after_ns),
                "arrival_ts": order.arrival_ts,
                "arrival_sequence_num": order.arrival_sequence_num,
                "configured_latency_ms": self.config.latency_ms,
                "arrival_latency_ms": (
                    _decimal_str(
                        Decimal(order.arrival_ns - order.decision_ns)
                        / Decimal(1_000_000)
                    )
                    if order.arrival_ns is not None
                    else None
                ),
                "filled_base_quantity": _decimal_str(order.filled_base),
                "canceled_base_quantity": _decimal_str(order.canceled_base),
                "quote_balance": _decimal_str(self.state.quote),
                "base_balance": _decimal_str(self.state.base),
                "product_id": self.product_id,
                "window_id": self.window_id,
            }
        )


def load_audited_book_window(
    derived_root: Path,
    product_id: str,
    config: StrictL2FillConfig,
    run_id: str | None = None,
    window_id: str | None = None,
    start: str | None = None,
    end: str | None = None,
    max_rows: int | None = None,
    discovery: dict[str, Any] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    if max_rows is not None and max_rows < 1:
        raise ValueError("max_rows must be at least 1")
    start_ns = _timestamp_ns(start, "start") if start else None
    end_ns = _timestamp_ns(end, "end") if end else None
    if start_ns is not None and end_ns is not None and end_ns <= start_ns:
        raise ValueError("end must be after start")

    # A caller may pass an earlier discovery report for presentation, but it is
    # never selection authority.  Rediscover here so a newly eligible run
    # cannot be hidden and a formerly eligible run cannot be injected.
    contract_discovery = discover_audited_book_runs(derived_root, product_id)
    eligible_audits = list(contract_discovery.get("eligible") or [])
    if run_id is not None:
        requested_audits = [
            row
            for row in list(contract_discovery.get("audits") or [])
            if row.get("run_id") == run_id and row.get("product_id") == product_id
        ]
        if len(requested_audits) != 1:
            raise AuditedBookSelectionError(
                "requires_l2_run_selection",
                f"Requested L2 run {run_id!r} did not identify exactly one audited {product_id} run.",
            )
        if not requested_audits[0].get("strict_l2_eligible"):
            raise AuditedBookSelectionError(
                "requires_valid_book_snapshots",
                f"Requested L2 run {run_id!r} failed its fresh strict-L2 audit.",
            )
        eligible_audits = requested_audits
    elif len(eligible_audits) != 1:
        raise AuditedBookSelectionError(
            "requires_l2_run_selection",
            "Strict L2 execution requires exactly one eligible run or an explicit --l2-run-id.",
        )
    selected_audit = eligible_audits[0]
    expected_contract_root = (
        derived_root / "v1" / "book_reconstruction"
    ).resolve()
    manifest_path = Path(str(selected_audit["manifest_path"])).resolve()
    if (
        manifest_path.name != "manifest.json"
        or manifest_path.parent.parent != expected_contract_root
    ):
        raise AuditedBookSelectionError(
            "requires_valid_book_snapshots",
            "Selected manifest is outside the requested reconstruction contract root.",
        )
    manifest_bytes = manifest_path.read_bytes()
    fresh_audit = audit_book_reconstruction_run(
        manifest_path,
        product_id=product_id,
    )
    if (
        not fresh_audit.get("valid")
        or not fresh_audit.get("strict_l2_eligible")
        or fresh_audit.get("run_id") != selected_audit.get("run_id")
    ):
        raise AuditedBookSelectionError(
            "requires_valid_book_snapshots",
            "Selected reconstruction no longer passes a fresh strict-L2 audit.",
        )
    selected_audit = fresh_audit
    if manifest_path.read_bytes() != manifest_bytes:
        raise AuditedBookSelectionError(
            "requires_valid_book_snapshots",
            "Selected manifest changed during its fresh strict-L2 audit.",
        )
    manifest = json.loads(manifest_bytes.decode("utf-8"))
    if manifest.get("run_id") != selected_audit.get("run_id"):
        raise AuditedBookSelectionError(
            "requires_valid_book_snapshots",
            "Audited manifest identity changed before strict consumption.",
        )

    windows_path = _artifact_path(manifest_path, manifest, "book_windows")
    snapshots_path = _artifact_path(manifest_path, manifest, "book_snapshots")
    eligible_windows: list[dict[str, Any]] = []
    for record in iter_jsonl(windows_path):
        row = record.payload
        if (
            isinstance(row, dict)
            and row.get("product_id") == product_id
            and row.get("strict_l2_eligible")
            and row.get("validity_status") == "valid"
        ):
            eligible_windows.append(row)
    if window_id is not None:
        eligible_windows = [row for row in eligible_windows if row.get("window_id") == window_id]
        if len(eligible_windows) != 1:
            raise AuditedBookSelectionError(
                "requires_l2_window_selection",
                f"Requested L2 window {window_id!r} did not identify exactly one audited eligible window in the selected run.",
            )
    elif len(eligible_windows) != 1:
        raise AuditedBookSelectionError(
            "requires_l2_window_selection",
            "Strict L2 execution requires exactly one eligible window or an explicit --l2-window-id.",
        )
    selected_window = eligible_windows[0]
    selected_window_id = str(selected_window["window_id"])

    rows: list[dict[str, Any]] = []
    window_rows_seen = 0
    previous_sequence: int | None = None
    previous_clock_ns: int | None = None
    for record in iter_jsonl(snapshots_path):
        row = record.payload
        if not isinstance(row, dict) or row.get("window_id") != selected_window_id:
            continue
        window_rows_seen += 1
        if row.get("product_id") != product_id:
            raise AuditedBookSelectionError(
                "requires_valid_book_snapshots",
                "Selected snapshot artifact changed product inside its audited window.",
            )
        if row.get("validity_status") != "valid" or not row.get("strict_l2_eligible"):
            raise AuditedBookSelectionError(
                "requires_valid_book_snapshots",
                "Selected snapshot artifact contains a non-eligible row.",
            )
        try:
            row_clock_ns = _timestamp_ns(row.get(config.clock_source), config.clock_source)
            sequence_num = _sequence_num(row)
        except ValueError as exc:
            raise AuditedBookSelectionError(
                "requires_valid_book_snapshots",
                f"Selected snapshot row cannot support the configured clock: {exc}",
            ) from exc
        if previous_sequence is not None and sequence_num <= previous_sequence:
            raise AuditedBookSelectionError(
                "requires_valid_book_snapshots",
                "Selected snapshot sequences are not strictly increasing.",
            )
        if previous_clock_ns is not None and row_clock_ns < previous_clock_ns:
            raise AuditedBookSelectionError(
                "requires_valid_book_snapshots",
                "Selected snapshot clock regressed.",
            )
        previous_sequence = sequence_num
        previous_clock_ns = row_clock_ns
        if start_ns is not None and row_clock_ns < start_ns:
            continue
        if end_ns is not None and row_clock_ns >= end_ns:
            continue
        rows.append(row)
        if max_rows is not None and len(rows) >= max_rows:
            break
    if not rows:
        raise AuditedBookSelectionError(
            "requires_l2_rows_in_selected_interval",
            "The audited window has no book rows inside the selected time bounds.",
        )
    if manifest_path.read_bytes() != manifest_bytes:
        raise AuditedBookSelectionError(
            "requires_valid_book_snapshots",
            "Selected manifest changed while the strict consumer was reading it.",
        )
    for artifact_name, artifact_path in (
        ("book_windows", windows_path),
        ("book_snapshots", snapshots_path),
    ):
        expected_hash = manifest["artifacts"][artifact_name].get("sha256")
        if not isinstance(expected_hash, str) or _sha256_file(artifact_path) != expected_hash:
            raise AuditedBookSelectionError(
                "requires_valid_book_snapshots",
                f"Audited artifact {artifact_name} changed during strict consumption.",
            )

    final_audit = audit_book_reconstruction_run(
        manifest_path,
        product_id=product_id,
    )
    if (
        not final_audit.get("valid")
        or not final_audit.get("strict_l2_eligible")
        or final_audit.get("run_id") != selected_audit.get("run_id")
        or manifest_path.read_bytes() != manifest_bytes
    ):
        raise AuditedBookSelectionError(
            "requires_valid_book_snapshots",
            "Selected reconstruction failed its post-consumption strict-L2 audit.",
        )

    selection_report = {
        "manifest_path": str(manifest_path),
        "run_id": selected_audit.get("run_id"),
        "product_id": product_id,
        "window_id": selected_window_id,
        "window_contract": selected_window,
        "book_snapshots_path": str(snapshots_path),
        "clock_source": config.clock_source,
        "start": start,
        "end": end,
        "max_rows": max_rows,
        "window_rows_seen": window_rows_seen,
        "rows_selected": len(rows),
        "first_selected_ts": rows[0].get(config.clock_source),
        "last_selected_ts": rows[-1].get(config.clock_source),
        "first_selected_sequence_num": rows[0].get("sequence_num"),
        "last_selected_sequence_num": rows[-1].get("sequence_num"),
        "depth_limits": sorted(
            {
                int(row["depth_limit"])
                for row in rows
                if isinstance(row.get("depth_limit"), int)
            }
        ),
        "depth_truncated_rows": sum(bool(row.get("depth_truncated")) for row in rows),
        "manifest_sha256": hashlib.sha256(manifest_bytes).hexdigest(),
        "book_windows_sha256": manifest["artifacts"]["book_windows"]["sha256"],
        "book_snapshots_sha256": manifest["artifacts"]["book_snapshots"]["sha256"],
        "selected_rows_sha256": _sha256_rows(rows),
        "selection_authority": "fresh_discovery_plus_pre_and_post_consumption_audit",
        "caller_discovery_supplied": discovery is not None,
    }
    return rows, selection_report


def simulate_gridbot_on_l2(
    rows: list[dict[str, Any]],
    levels: list[Decimal],
    start_index: int,
    product_id: str,
    quote_start: Decimal,
    base_start: Decimal,
    order_quote: Decimal,
    fee_profile: FeeProfile,
    fill_config: StrictL2FillConfig,
    selection_report: dict[str, Any],
) -> dict[str, Any]:
    if not rows:
        raise ValueError("no audited book rows available for strict backtest")
    if not 0 <= start_index < len(levels):
        raise ValueError("start_index is outside the grid")
    window_id = str(selection_report["window_id"])
    engine = StrictL2FillEngine(
        levels=levels,
        order_quote=order_quote,
        quote_start=quote_start,
        base_start=base_start,
        fee_profile=fee_profile,
        config=fill_config,
        product_id=product_id,
        window_id=window_id,
    )
    first_row = rows[0]
    for level_index in range(start_index - 1, -1, -1):
        engine.submit_order(
            "buy",
            level_index,
            first_row,
            allow_same_row_activation=True,
        )
    for level_index in range(start_index + 1, len(levels)):
        engine.submit_order(
            "sell",
            level_index,
            first_row,
            allow_same_row_activation=True,
        )
    for row in rows:
        engine.on_book(row)
    engine.close_window()
    result = engine.result()
    result["summary"].update(
        {
            "grid_type": "arithmetic",
            "grid_levels": [_decimal_str(level) for level in levels],
            "start_grid_level_index": start_index,
            "first_ts": rows[0].get(fill_config.clock_source),
            "last_ts": rows[-1].get(fill_config.clock_source),
            "book_rows_used": len(rows),
            "audited_book_selection": selection_report,
            "limitations": [
                "Strict fills consume only audited eligible book rows from one validity window.",
                "A resting order fills only after strict price-through; touch and same-level depletion are not execution evidence.",
                "Only emitted visible depth is consumed. Hidden liquidity and levels beyond the emitted depth are unknown.",
                "A partial fill cancels its uncertain remainder and does not arm the adjacent grid level.",
                "The configured latency is a deterministic research scenario, not measured transport latency when recv_ts is unavailable.",
                "Coinbase L2 cannot reveal exact queue position, L3 order lifecycles, participant identity, or hidden liquidity.",
                "This backtester does not place paper or live orders.",
            ],
        }
    )
    return result


def _artifact_path(
    manifest_path: Path,
    manifest: dict[str, Any],
    artifact_name: str,
) -> Path:
    artifacts = manifest.get("artifacts")
    metadata = artifacts.get(artifact_name) if isinstance(artifacts, dict) else None
    relative = metadata.get("path") if isinstance(metadata, dict) else None
    if not isinstance(relative, str) or not relative:
        raise AuditedBookSelectionError(
            "requires_valid_book_snapshots",
            f"Audited manifest is missing artifact {artifact_name}.",
        )
    run_dir = manifest_path.parent.resolve()
    resolved = (run_dir / relative).resolve()
    if resolved.parent != run_dir or not resolved.is_file():
        raise AuditedBookSelectionError(
            "requires_valid_book_snapshots",
            f"Audited artifact {artifact_name} is unavailable inside its run directory.",
        )
    return resolved


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
