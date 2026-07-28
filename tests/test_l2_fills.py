from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

from moneyman.fees import manual_fee_profile
from moneyman.l2_fills import (
    StrictL2FillConfig,
    StrictL2FillEngine,
)


_MISSING = object()
_START = datetime(2025, 8, 1, 21, 21, tzinfo=timezone.utc)


def _decimal_text(value: Decimal | str | int) -> str:
    return format(Decimal(str(value)).normalize(), "f")


def _timestamp(milliseconds: int) -> str:
    value = _START + timedelta(milliseconds=milliseconds)
    return value.isoformat(timespec="microseconds").replace("+00:00", "Z")


def _snapshot(
    milliseconds: int,
    sequence_num: int,
    *,
    bids: list[tuple[Decimal | str | int, Decimal | str | int]],
    asks: list[tuple[Decimal | str | int, Decimal | str | int]],
    window_id: str = "window-1",
) -> dict[str, Any]:
    bid_levels = [
        {"price": _decimal_text(price), "quantity": _decimal_text(quantity)}
        for price, quantity in bids
    ]
    ask_levels = [
        {"price": _decimal_text(price), "quantity": _decimal_text(quantity)}
        for price, quantity in asks
    ]
    best_bid = Decimal(bid_levels[0]["price"])
    best_ask = Decimal(ask_levels[0]["price"])
    if best_bid >= best_ask:
        raise ValueError("fixture book must be strictly uncrossed")
    midpoint = (best_bid + best_ask) / Decimal("2")
    timestamp = _timestamp(milliseconds)
    return {
        "schema": "moneyman.book_snapshot.v1",
        "product_id": "XRP-USD",
        "capture_stream_id": "strict-fill-fixture",
        "connection_epoch": 0,
        "window_id": window_id,
        "validity_status": "valid",
        "validity_reason": "continuous_update",
        "strict_l2_eligible": True,
        "envelope_ordinal": sequence_num,
        "sequence_num": sequence_num,
        "originating_snapshot_sequence_num": 0,
        "source_channel": "l2_data",
        "message_ts": timestamp,
        "event_ts": timestamp,
        "recv_ts": None,
        "depth_limit": max(len(bid_levels), len(ask_levels)),
        "depth_truncated": False,
        "best_bid": _decimal_text(best_bid),
        "best_ask": _decimal_text(best_ask),
        "midpoint": _decimal_text(midpoint),
        "spread": _decimal_text(best_ask - best_bid),
        "bid_levels": bid_levels,
        "ask_levels": ask_levels,
        "emitted_bid_level_count": len(bid_levels),
        "emitted_ask_level_count": len(ask_levels),
        "emitted_bid_depth": _decimal_text(
            sum((Decimal(level["quantity"]) for level in bid_levels), Decimal("0"))
        ),
        "emitted_ask_depth": _decimal_text(
            sum((Decimal(level["quantity"]) for level in ask_levels), Decimal("0"))
        ),
    }


def _fee_profile(
    *,
    maker: str = "0",
    taker: str = "0",
    rebate: str = "0",
):
    return manual_fee_profile(
        fee_rate="0",
        maker_fee_rate=maker,
        taker_fee_rate=taker,
        liquidity_assumption="maker",
        coinbase_one_advanced_rebate_rate=rebate,
        coinbase_one_monthly_rebate_cap="100",
        coinbase_one_monthly_rebate_used="0",
    )


def _engine(
    levels: list[str],
    *,
    order_quote: str = "100",
    quote_start: str = "200",
    base_start: str = "0",
    latency_ms: int = 0,
    maker_fee: str = "0",
    taker_fee: str = "0",
    rebate: str = "0",
) -> StrictL2FillEngine:
    return StrictL2FillEngine(
        levels=[Decimal(level) for level in levels],
        order_quote=Decimal(order_quote),
        quote_start=Decimal(quote_start),
        base_start=Decimal(base_start),
        fee_profile=_fee_profile(
            maker=maker_fee,
            taker=taker_fee,
            rebate=rebate,
        ),
        config=StrictL2FillConfig(
            latency_ms=latency_ms,
            clock_source="message_ts",
        ),
        product_id="XRP-USD",
        window_id="window-1",
    )


def _get(row: dict[str, Any], *names: str, default: Any = _MISSING) -> Any:
    for name in names:
        if name in row:
            return row[name]
    if default is not _MISSING:
        return default
    raise AssertionError(f"none of {names!r} found in row with keys {sorted(row)}")


def _decimal(row: dict[str, Any], *names: str) -> Decimal:
    return Decimal(str(_get(row, *names)))


def _fill_quantity(fill: dict[str, Any]) -> Decimal:
    return _decimal(fill, "base_quantity", "quantity", "base_delta")


def _fill_notional(fill: dict[str, Any]) -> Decimal:
    return _decimal(fill, "notional_quote", "fill_notional_quote", "order_quote")


def _fill_fee(fill: dict[str, Any]) -> Decimal:
    return _decimal(fill, "fee_gross_quote", "gross_fee_quote")


def _fill_rebate(fill: dict[str, Any]) -> Decimal:
    return _decimal(fill, "fee_rebate_quote", "rebate_quote")


def _fill_price(fill: dict[str, Any]) -> Decimal:
    return _decimal(fill, "price")


def _side(fill: dict[str, Any]) -> str:
    return str(_get(fill, "side")).lower()


def _liquidity(fill: dict[str, Any]) -> str:
    return str(_get(fill, "liquidity", "liquidity_assumption")).lower()


def _event_text(result: dict[str, Any]) -> str:
    return " ".join(
        " ".join(str(value) for value in event.values()).lower()
        for event in result["order_events"]
    )


class StrictL2FillTests(unittest.TestCase):
    def test_window_boundary_is_rejected_before_state_can_cross_it(self) -> None:
        engine = _engine(["100"])
        engine.on_book(
            _snapshot(0, 0, bids=[("99", "4")], asks=[("101", "4")])
        )

        with self.assertRaisesRegex(ValueError, "window boundary"):
            engine.on_book(
                _snapshot(
                    1,
                    1,
                    bids=[("99", "4")],
                    asks=[("101", "4")],
                    window_id="window-2",
                )
            )

    def test_touch_and_same_level_depletion_do_not_fill(self) -> None:
        engine = _engine(["100"])
        decision = _snapshot(0, 0, bids=[("99", "4")], asks=[("101", "4")])
        engine.on_book(decision)
        engine.submit_order("buy", 0, decision, allow_same_row_activation=False)

        engine.on_book(
            _snapshot(1, 1, bids=[("99", "4")], asks=[("101", "4")])
        )
        engine.on_book(
            _snapshot(2, 2, bids=[("99", "4")], asks=[("100", "2")])
        )
        engine.on_book(
            _snapshot(3, 3, bids=[("99", "4")], asks=[("100", "0.5")])
        )
        engine.on_book(
            _snapshot(4, 4, bids=[("99", "4")], asks=[("101", "4")])
        )
        engine.close_window()
        result = engine.result()

        self.assertEqual(result["fills"], [])
        self.assertEqual(result["summary"]["filled_orders"], 0)
        self.assertEqual(
            _decimal(result["summary"], "final_quote_balance", "final_quote_balance_quote"),
            Decimal("200"),
        )
        self.assertEqual(
            _decimal(result["summary"], "final_base_balance"),
            Decimal("0"),
        )

    def test_strict_price_through_fills_resting_order_as_maker(self) -> None:
        engine = _engine(
            ["100"],
            maker_fee="0.01",
            taker_fee="0.02",
        )
        decision = _snapshot(0, 0, bids=[("99", "4")], asks=[("101", "4")])
        engine.on_book(decision)
        engine.submit_order("buy", 0, decision, allow_same_row_activation=False)
        engine.on_book(
            _snapshot(1, 1, bids=[("99", "4")], asks=[("101", "4")])
        )
        engine.on_book(
            _snapshot(2, 2, bids=[("99", "4")], asks=[("100", "4")])
        )
        engine.on_book(
            _snapshot(3, 3, bids=[("99", "4")], asks=[("99.5", "2")])
        )
        engine.close_window()
        result = engine.result()

        self.assertTrue(result["fills"])
        self.assertEqual({_liquidity(fill) for fill in result["fills"]}, {"maker"})
        self.assertEqual({_fill_price(fill) for fill in result["fills"]}, {Decimal("100")})
        self.assertEqual(result["summary"]["filled_orders"], 1)
        self.assertEqual(result["summary"]["maker_fills"], 1)
        self.assertEqual(result["summary"]["taker_fills"], 0)
        self.assertEqual(
            sum((_fill_quantity(fill) for fill in result["fills"]), Decimal("0")),
            Decimal("1"),
        )
        self.assertEqual(
            sum((_fill_notional(fill) for fill in result["fills"]), Decimal("0")),
            Decimal("100"),
        )
        self.assertEqual(
            sum((_fill_fee(fill) for fill in result["fills"]), Decimal("0")),
            Decimal("1"),
        )
        self.assertEqual(
            _decimal(result["summary"], "final_quote_balance", "final_quote_balance_quote"),
            Decimal("99"),
        )
        self.assertEqual(
            _decimal(result["summary"], "final_base_balance"),
            Decimal("1"),
        )

    def test_arrival_crossing_sweeps_visible_asks_as_taker(self) -> None:
        engine = _engine(
            ["101"],
            order_quote="101",
            latency_ms=100,
            maker_fee="0.01",
            taker_fee="0.02",
        )
        decision = _snapshot(0, 0, bids=[("99", "4")], asks=[("102", "4")])
        engine.on_book(decision)
        engine.submit_order("buy", 0, decision, allow_same_row_activation=False)
        engine.on_book(
            _snapshot(50, 1, bids=[("99", "4")], asks=[("100", "4")])
        )
        engine.on_book(
            _snapshot(
                100,
                2,
                bids=[("99", "4")],
                asks=[("100", "0.4"), ("100.5", "0.3"), ("101", "0.5")],
            )
        )
        engine.close_window()
        result = engine.result()

        self.assertTrue(result["fills"])
        self.assertEqual({_liquidity(fill) for fill in result["fills"]}, {"taker"})
        self.assertEqual({_fill_price(fill) for fill in result["fills"]}, {Decimal("100.45")})
        self.assertEqual(result["summary"]["filled_orders"], 1)
        self.assertEqual(result["summary"]["maker_fills"], 0)
        self.assertEqual(result["summary"]["taker_fills"], 1)
        self.assertEqual(
            sum((_fill_quantity(fill) for fill in result["fills"]), Decimal("0")),
            Decimal("1"),
        )
        self.assertEqual(
            sum((_fill_notional(fill) for fill in result["fills"]), Decimal("0")),
            Decimal("100.45"),
        )
        self.assertEqual(
            sum((_fill_fee(fill) for fill in result["fills"]), Decimal("0")),
            Decimal("2.0090"),
        )
        self.assertEqual(
            _decimal(result["summary"], "final_quote_balance", "final_quote_balance_quote"),
            Decimal("97.5410"),
        )
        self.assertEqual(
            _decimal(result["summary"], "final_base_balance"),
            Decimal("1"),
        )

    def test_visible_depth_is_shared_and_uncertain_remainder_is_canceled(self) -> None:
        engine = _engine(
            ["100", "101"],
            order_quote="101",
            latency_ms=100,
            quote_start="500",
        )
        decision = _snapshot(0, 0, bids=[("99", "4")], asks=[("102", "4")])
        engine.on_book(decision)
        engine.submit_order("buy", 0, decision, allow_same_row_activation=False)
        engine.submit_order("buy", 1, decision, allow_same_row_activation=False)
        engine.on_book(
            _snapshot(100, 1, bids=[("98", "4")], asks=[("99", "1.2")])
        )
        engine.close_window()
        result = engine.result()

        self.assertEqual(
            sum((_fill_quantity(fill) for fill in result["fills"]), Decimal("0")),
            Decimal("1.2"),
        )
        self.assertEqual(
            _decimal(result["summary"], "final_base_balance"),
            Decimal("1.2"),
        )
        self.assertEqual(result["summary"]["filled_orders"], 1)
        self.assertEqual(result["summary"]["partial_fills"], 1)
        self.assertEqual(
            [fill["limit_price"] for fill in result["fills"]],
            ["101", "100"],
        )
        self.assertEqual(
            [fill["status"] for fill in result["fills"]],
            ["filled", "partial_canceled"],
        )
        event_text = _event_text(result)
        self.assertIn("partial", event_text)
        self.assertIn("cancel", event_text)
        self.assertIn("visible", event_text)

    def test_resting_cohort_precedes_a_more_aggressive_new_arrival(self) -> None:
        engine = _engine(
            ["99", "100"],
            order_quote="100",
            quote_start="500",
            latency_ms=0,
        )
        decision = _snapshot(0, 0, bids=[("98", "4")], asks=[("101", "4")])
        engine.on_book(decision)
        engine.submit_order("buy", 0, decision, allow_same_row_activation=False)
        resting_row = _snapshot(
            1,
            1,
            bids=[("98", "4")],
            asks=[("101", "4")],
        )
        engine.on_book(resting_row)
        engine.submit_order("buy", 1, resting_row, allow_same_row_activation=False)

        engine.on_book(
            _snapshot(2, 2, bids=[("97", "4")], asks=[("98", "1.5")])
        )
        engine.close_window()
        result = engine.result()

        self.assertEqual(len(result["fills"]), 2)
        self.assertEqual(result["fills"][0]["limit_price"], "99")
        self.assertEqual(result["fills"][0]["liquidity"], "maker")
        self.assertEqual(result["fills"][0]["status"], "filled")
        self.assertEqual(result["fills"][1]["limit_price"], "100")
        self.assertEqual(result["fills"][1]["liquidity"], "taker")
        self.assertEqual(result["fills"][1]["status"], "partial_canceled")
        self.assertEqual(
            sum((_fill_quantity(fill) for fill in result["fills"]), Decimal("0")),
            Decimal("1.5"),
        )

    def test_unchanged_visible_depth_is_not_reused_on_later_rows(self) -> None:
        engine = _engine(
            ["100", "101"],
            order_quote="100",
            quote_start="500",
            latency_ms=100,
        )
        decision = _snapshot(0, 0, bids=[("98", "4")], asks=[("102", "4")])
        engine.on_book(decision)
        engine.submit_order("buy", 1, decision, allow_same_row_activation=False)

        first_fill_row = _snapshot(
            100,
            1,
            bids=[("98", "4")],
            asks=[("99", "1")],
        )
        engine.on_book(first_fill_row)
        engine.submit_order("buy", 0, first_fill_row, allow_same_row_activation=False)
        engine.on_book(
            _snapshot(200, 2, bids=[("98", "4")], asks=[("99", "1")])
        )
        engine.close_window()
        result = engine.result()

        self.assertEqual(len(result["fills"]), 2)
        self.assertEqual(result["fills"][0]["status"], "filled")
        self.assertEqual(result["fills"][1]["status"], "partial_canceled")
        self.assertEqual(
            sum((_fill_quantity(fill) for fill in result["fills"]), Decimal("0")),
            Decimal("1"),
        )

    def test_only_positive_observed_delta_replenishes_shadow_depth(self) -> None:
        engine = _engine(
            ["100", "101"],
            order_quote="100",
            quote_start="500",
            latency_ms=100,
        )
        decision = _snapshot(0, 0, bids=[("98", "4")], asks=[("102", "4")])
        engine.on_book(decision)
        engine.submit_order("buy", 1, decision, allow_same_row_activation=False)

        first_fill_row = _snapshot(
            100,
            1,
            bids=[("98", "4")],
            asks=[("99", "1")],
        )
        engine.on_book(first_fill_row)
        first_quantity = _fill_quantity(engine.fills[0])
        engine.submit_order("buy", 0, first_fill_row, allow_same_row_activation=False)
        engine.on_book(
            _snapshot(200, 2, bids=[("98", "4")], asks=[("99", "1.5")])
        )
        engine.close_window()
        result = engine.result()

        self.assertEqual(len(result["fills"]), 2)
        self.assertEqual(
            _fill_quantity(result["fills"][1]),
            Decimal("1.5") - first_quantity,
        )
        self.assertEqual(
            sum((_fill_quantity(fill) for fill in result["fills"]), Decimal("0")),
            Decimal("1.5"),
        )

    def test_observed_decrease_floors_shadow_depth_at_zero(self) -> None:
        engine = _engine(
            ["100", "125"],
            order_quote="100",
            quote_start="500",
            latency_ms=100,
        )
        decision = _snapshot(0, 0, bids=[("98", "4")], asks=[("130", "4")])
        engine.on_book(decision)
        engine.submit_order("buy", 1, decision, allow_same_row_activation=False)

        first_fill_row = _snapshot(
            100,
            1,
            bids=[("98", "4")],
            asks=[("99", "1")],
        )
        engine.on_book(first_fill_row)
        engine.submit_order("buy", 0, first_fill_row, allow_same_row_activation=False)
        engine.on_book(
            _snapshot(200, 2, bids=[("98", "4")], asks=[("99", "0.5")])
        )
        self.assertEqual(len(engine.fills), 1)

        engine.on_book(
            _snapshot(300, 3, bids=[("98", "4")], asks=[("99", "0.6")])
        )
        engine.close_window()
        result = engine.result()

        self.assertEqual(len(result["fills"]), 2)
        self.assertEqual(_fill_quantity(result["fills"][0]), Decimal("0.8"))
        self.assertEqual(_fill_quantity(result["fills"][1]), Decimal("0.1"))
        self.assertEqual(result["fills"][1]["status"], "partial_canceled")

    def test_top_n_disappearance_and_reentry_do_not_restore_consumed_depth(self) -> None:
        engine = _engine(
            ["100", "101"],
            order_quote="100",
            quote_start="500",
            latency_ms=100,
        )
        decision = _snapshot(0, 0, bids=[("98", "4")], asks=[("102", "4")])
        engine.on_book(decision)
        engine.submit_order("buy", 1, decision, allow_same_row_activation=False)

        first_fill_row = _snapshot(
            100,
            1,
            bids=[("98", "4")],
            asks=[("99", "1"), ("102", "4")],
        )
        engine.on_book(first_fill_row)
        engine.submit_order("buy", 0, first_fill_row, allow_same_row_activation=False)
        engine.on_book(
            _snapshot(200, 2, bids=[("98", "4")], asks=[("102", "4")])
        )
        engine.on_book(
            _snapshot(
                300,
                3,
                bids=[("98", "4")],
                asks=[("99", "1"), ("102", "4")],
            )
        )
        engine.close_window()
        result = engine.result()

        self.assertEqual(len(result["fills"]), 2)
        self.assertEqual(result["fills"][1]["liquidity"], "maker")
        self.assertEqual(result["fills"][1]["status"], "partial_canceled")
        self.assertEqual(
            sum((_fill_quantity(fill) for fill in result["fills"]), Decimal("0")),
            Decimal("1"),
        )

    def test_bid_shadow_depth_mirrors_ask_no_reuse_behavior(self) -> None:
        engine = _engine(
            ["99", "100"],
            order_quote="100",
            quote_start="0",
            base_start="3",
            latency_ms=100,
        )
        decision = _snapshot(0, 0, bids=[("98", "4")], asks=[("102", "4")])
        engine.on_book(decision)
        engine.submit_order("sell", 1, decision, allow_same_row_activation=False)

        first_fill_row = _snapshot(
            100,
            1,
            bids=[("101", "1")],
            asks=[("102", "4")],
        )
        engine.on_book(first_fill_row)
        engine.submit_order("sell", 0, first_fill_row, allow_same_row_activation=False)
        engine.on_book(
            _snapshot(200, 2, bids=[("101", "1")], asks=[("102", "4")])
        )
        engine.close_window()
        result = engine.result()

        self.assertEqual(len(result["fills"]), 1)
        self.assertEqual(result["fills"][0]["side"], "sell")
        self.assertEqual(_fill_quantity(result["fills"][0]), Decimal("1"))

    def test_shadow_depth_is_independent_between_visible_prices(self) -> None:
        engine = _engine(
            ["99", "101"],
            order_quote="101",
            quote_start="500",
            latency_ms=100,
        )
        decision = _snapshot(0, 0, bids=[("97", "4")], asks=[("102", "4")])
        engine.on_book(decision)
        engine.submit_order("buy", 1, decision, allow_same_row_activation=False)

        first_fill_row = _snapshot(
            100,
            1,
            bids=[("97", "4")],
            asks=[("100", "1")],
        )
        engine.on_book(first_fill_row)
        engine.submit_order("buy", 0, first_fill_row, allow_same_row_activation=False)
        engine.on_book(
            _snapshot(
                200,
                2,
                bids=[("97", "4")],
                asks=[("98", "1"), ("100", "1")],
            )
        )
        engine.close_window()
        result = engine.result()

        self.assertEqual(len(result["fills"]), 2)
        self.assertEqual(
            [fill["visible_depth_consumed"][0]["book_price"] for fill in result["fills"]],
            ["100", "98"],
        )
        self.assertEqual(
            sum((_fill_quantity(fill) for fill in result["fills"]), Decimal("0")),
            Decimal("2"),
        )

    def test_latency_fields_separate_arrival_delay_from_resting_time(self) -> None:
        engine = _engine(["100"], latency_ms=100)
        decision = _snapshot(0, 0, bids=[("99", "4")], asks=[("101", "4")])
        engine.on_book(decision)
        engine.submit_order("buy", 0, decision, allow_same_row_activation=False)
        engine.on_book(
            _snapshot(150, 1, bids=[("99", "4")], asks=[("101", "4")])
        )
        engine.on_book(
            _snapshot(300, 2, bids=[("99", "4")], asks=[("99.5", "4")])
        )
        engine.close_window()
        result = engine.result()

        self.assertEqual(len(result["fills"]), 1)
        fill = result["fills"][0]
        self.assertEqual(fill["liquidity"], "maker")
        self.assertEqual(fill["configured_latency_ms"], 100)
        self.assertEqual(_decimal(fill, "arrival_latency_ms"), Decimal("150"))
        self.assertEqual(_decimal(fill, "decision_to_fill_ms"), Decimal("300"))
        self.assertEqual(_decimal(fill, "resting_time_ms"), Decimal("150"))

    def test_latency_can_worsen_then_miss_the_same_buy(self) -> None:
        decision = _snapshot(0, 0, bids=[("99", "4")], asks=[("100", "4")])

        immediate = _engine(
            ["101"],
            order_quote="101",
            quote_start="300",
            latency_ms=0,
        )
        immediate.submit_order("buy", 0, decision, allow_same_row_activation=True)
        immediate.on_book(decision)
        immediate.close_window()
        immediate_result = immediate.result()

        delayed = _engine(
            ["101"],
            order_quote="101",
            quote_start="300",
            latency_ms=100,
        )
        delayed.on_book(decision)
        delayed.submit_order("buy", 0, decision, allow_same_row_activation=False)
        delayed.on_book(
            _snapshot(100, 1, bids=[("100", "4")], asks=[("101", "4")])
        )
        delayed.close_window()
        delayed_result = delayed.result()

        missed = _engine(
            ["101"],
            order_quote="101",
            quote_start="300",
            latency_ms=200,
        )
        missed.on_book(decision)
        missed.submit_order("buy", 0, decision, allow_same_row_activation=False)
        missed.on_book(
            _snapshot(100, 1, bids=[("100", "4")], asks=[("101", "4")])
        )
        missed.on_book(
            _snapshot(200, 2, bids=[("101", "4")], asks=[("102", "4")])
        )
        missed.on_book(
            _snapshot(300, 3, bids=[("101", "4")], asks=[("102", "4")])
        )
        missed.close_window()
        missed_result = missed.result()

        self.assertEqual(
            sum((_fill_notional(fill) for fill in immediate_result["fills"]), Decimal("0")),
            Decimal("100"),
        )
        self.assertEqual(
            sum((_fill_notional(fill) for fill in delayed_result["fills"]), Decimal("0")),
            Decimal("101"),
        )
        self.assertEqual(missed_result["fills"], [])

    def test_insufficient_quote_or_base_never_creates_a_partial_position(self) -> None:
        buy = _engine(
            ["100"],
            quote_start="100",
            maker_fee="0.01",
        )
        buy_decision = _snapshot(0, 0, bids=[("99", "4")], asks=[("101", "4")])
        buy.on_book(buy_decision)
        buy.submit_order("buy", 0, buy_decision, allow_same_row_activation=False)
        buy.on_book(
            _snapshot(1, 1, bids=[("99", "4")], asks=[("101", "4")])
        )
        buy.on_book(
            _snapshot(2, 2, bids=[("99", "4")], asks=[("99.5", "4")])
        )
        buy.close_window()
        buy_result = buy.result()

        sell = _engine(
            ["100"],
            quote_start="0",
            base_start="0.5",
        )
        sell_decision = _snapshot(0, 0, bids=[("99", "4")], asks=[("101", "4")])
        sell.on_book(sell_decision)
        sell.submit_order("sell", 0, sell_decision, allow_same_row_activation=False)
        sell.on_book(
            _snapshot(1, 1, bids=[("99", "4")], asks=[("101", "4")])
        )
        sell.on_book(
            _snapshot(2, 2, bids=[("100.5", "4")], asks=[("101", "4")])
        )
        sell.close_window()
        sell_result = sell.result()

        self.assertEqual(buy_result["fills"], [])
        self.assertEqual(sell_result["fills"], [])
        self.assertEqual(buy_result["summary"]["missed_buys_insufficient_quote"], 1)
        self.assertEqual(sell_result["summary"]["missed_sells_insufficient_base"], 1)
        self.assertEqual(
            _decimal(buy_result["summary"], "final_quote_balance", "final_quote_balance_quote"),
            Decimal("100"),
        )
        self.assertEqual(
            _decimal(buy_result["summary"], "final_base_balance"),
            Decimal("0"),
        )
        self.assertEqual(
            _decimal(sell_result["summary"], "final_quote_balance", "final_quote_balance_quote"),
            Decimal("0"),
        )
        self.assertEqual(
            _decimal(sell_result["summary"], "final_base_balance"),
            Decimal("0.5"),
        )
        self.assertIn("insufficient", _event_text(buy_result))
        self.assertIn("quote", _event_text(buy_result))
        self.assertIn("insufficient", _event_text(sell_result))
        self.assertIn("base", _event_text(sell_result))

    def test_unfunded_pooled_intent_can_rest_but_requires_inventory_at_execution(self) -> None:
        engine = _engine(
            ["100", "110"],
            quote_start="250",
            base_start="0",
        )
        decision = _snapshot(0, 0, bids=[("99", "4")], asks=[("101", "4")])
        engine.on_book(decision)
        sell_intent = engine.submit_order(
            "sell",
            1,
            decision,
            allow_same_row_activation=False,
        )
        engine.submit_order("buy", 0, decision, allow_same_row_activation=False)

        engine.on_book(
            _snapshot(1, 1, bids=[("99", "4")], asks=[("101", "4")])
        )
        self.assertIsNotNone(sell_intent)
        self.assertEqual(sell_intent.status, "resting")
        self.assertEqual(engine.state.base, Decimal("0"))

        engine.on_book(
            _snapshot(2, 2, bids=[("99", "4")], asks=[("99.5", "4")])
        )
        self.assertEqual(engine.state.base, Decimal("1"))
        engine.on_book(
            _snapshot(3, 3, bids=[("111", "4")], asks=[("112", "4")])
        )
        engine.close_window()
        result = engine.result()

        sell_fills = [fill for fill in result["fills"] if fill["side"] == "sell"]
        self.assertEqual(len(sell_fills), 1)
        self.assertEqual(sell_fills[0]["liquidity"], "maker")
        self.assertEqual(sell_fills[0]["decision_ts"], decision["message_ts"])
        self.assertGreaterEqual(
            _decimal(result["summary"], "final_base_balance"),
            Decimal("0"),
        )
        self.assertEqual(
            _decimal(result["summary"], "base_reconciliation_error"),
            Decimal("0"),
        )

    def test_cash_base_fees_and_rebates_reconcile_exactly(self) -> None:
        engine = _engine(
            ["100", "110"],
            quote_start="250",
            maker_fee="0.01",
            taker_fee="0.02",
            rebate="0.25",
        )
        decision = _snapshot(0, 0, bids=[("99", "4")], asks=[("101", "4")])
        engine.on_book(decision)
        engine.submit_order("buy", 0, decision, allow_same_row_activation=False)
        engine.on_book(
            _snapshot(1, 1, bids=[("99", "4")], asks=[("101", "4")])
        )
        engine.on_book(
            _snapshot(2, 2, bids=[("99", "4")], asks=[("99.5", "4")])
        )

        sell_decision = _snapshot(3, 3, bids=[("111", "4")], asks=[("112", "4")])
        engine.on_book(sell_decision)
        engine.submit_order("sell", 1, sell_decision, allow_same_row_activation=True)
        engine.close_window()
        result = engine.result()

        buy_quantity = sum(
            (_fill_quantity(fill) for fill in result["fills"] if _side(fill) == "buy"),
            Decimal("0"),
        )
        sell_quantity = sum(
            (_fill_quantity(fill) for fill in result["fills"] if _side(fill) == "sell"),
            Decimal("0"),
        )
        buy_notional = sum(
            (_fill_notional(fill) for fill in result["fills"] if _side(fill) == "buy"),
            Decimal("0"),
        )
        sell_notional = sum(
            (_fill_notional(fill) for fill in result["fills"] if _side(fill) == "sell"),
            Decimal("0"),
        )
        gross_fees = sum((_fill_fee(fill) for fill in result["fills"]), Decimal("0"))
        rebates = sum((_fill_rebate(fill) for fill in result["fills"]), Decimal("0"))
        final_quote = _decimal(
            result["summary"],
            "final_quote_balance",
            "final_quote_balance_quote",
        )
        final_base = _decimal(result["summary"], "final_base_balance")

        self.assertEqual({_liquidity(fill) for fill in result["fills"]}, {"maker", "taker"})
        self.assertEqual(
            final_quote,
            Decimal("250") - buy_notional - gross_fees + sell_notional,
        )
        self.assertEqual(
            final_base,
            buy_quantity - sell_quantity,
        )
        self.assertEqual(
            _decimal(result["summary"], "fees_gross_quote", "gross_fees_quote"),
            gross_fees,
        )
        self.assertEqual(
            _decimal(result["summary"], "fee_rebates_quote", "rebates_quote"),
            rebates,
        )
        self.assertEqual(
            _decimal(result["summary"], "fees_net_quote", "net_fees_quote"),
            gross_fees - rebates,
        )
        self.assertEqual(
            _decimal(result["summary"], "quote_reconciliation_error"),
            Decimal("0"),
        )
        self.assertEqual(
            _decimal(result["summary"], "base_reconciliation_error"),
            Decimal("0"),
        )
        self.assertGreaterEqual(final_quote, Decimal("0"))
        self.assertGreaterEqual(final_base, Decimal("0"))
        self.assertEqual(
            result["summary"]["visible_depth_policy"],
            "observed_delta_shadow_v1",
        )
        self.assertEqual(
            result["summary"]["event_cohort_policy"],
            "resting_before_arrival_price_priority",
        )
        self.assertRegex(result["summary"]["fill_engine_source_sha256"], r"^[0-9a-f]{64}$")
        self.assertRegex(result["summary"]["fill_contract_config_sha256"], r"^[0-9a-f]{64}$")


if __name__ == "__main__":
    unittest.main()
