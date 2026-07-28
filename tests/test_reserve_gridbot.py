from __future__ import annotations

import json
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory

from moneyman.reserve_gridbot import (
    ReserveGridConfig,
    _new_run_id,
    _selected_candle_rows_sha256,
    band_for_price,
    build_bands_and_slots,
    plan_buy,
    plan_exit,
    run_reserve_gridbot_backtest,
    simulate_reserve_gridbot_on_candles,
)


def _config(**overrides) -> ReserveGridConfig:
    values = {
        "product_id": "XRP-USD",
        "lower": "1.00",
        "upper": "1.20",
        "band_width": "0.20",
        "levels_per_band": 2,
        "band_active_lot_budget_cap": "20",
        "quote_start": "100",
        "exit_move_pct": "0.05",
        "cash_profit_bps": "20",
        "base_increment": "0.000001",
        "quote_increment": "0.01",
        "price_increment": "0.0001",
        "min_quote_notional": "1",
        "fee_rate": "0.006",
        "include_fallback_candles": True,
        "candle_path_assumption": "low-first",
    }
    values.update(overrides)
    return ReserveGridConfig(**values)


def _candle(
    *,
    ts: str = "2025-01-01T00:00:00Z",
    open_price: str = "1.10",
    high: str = "1.20",
    low: str = "1.00",
    close: str = "1.10",
    timeframe: str = "1m",
) -> dict[str, str]:
    return {
        "start_ts": ts,
        "product_id": "XRP-USD",
        "open": open_price,
        "high": high,
        "low": low,
        "close": close,
        "volume": "1000",
        "source_kind": "price_only_fallback",
        "source_provider": "fixture",
        "source_path": "fixture.jsonl",
        "timeframe": timeframe,
    }


def _flat_candle(ts: str, price: str, timeframe: str = "1m") -> dict[str, str]:
    return _candle(
        ts=ts,
        open_price=price,
        high=price,
        low=price,
        close=price,
        timeframe=timeframe,
    )


def _format_test_ts(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


class ReserveGridPlanningTests(unittest.TestCase):
    def test_half_open_bands_have_unique_boundary_levels(self) -> None:
        config = _config(upper="1.40")
        bands, slots = build_bands_and_slots(config)

        self.assertEqual([slot.entry_price for slot in slots], [
            Decimal("1.00"),
            Decimal("1.10"),
            Decimal("1.20"),
            Decimal("1.30"),
        ])
        self.assertEqual(band_for_price(Decimal("1.1999"), bands).band_id, "band-000")
        self.assertEqual(band_for_price(Decimal("1.20"), bands).band_id, "band-001")
        self.assertIsNone(band_for_price(Decimal("1.40"), bands))
        self.assertEqual(slots[0].cash_budget, Decimal("10"))

    def test_entry_spacing_must_align_exactly_to_price_increment(self) -> None:
        with self.assertRaisesRegex(ValueError, "align exactly to price_increment"):
            build_bands_and_slots(
                _config(levels_per_band=3, price_increment="0.01")
            )

    def test_overflow_cap_must_not_be_negative(self) -> None:
        with self.assertRaisesRegex(ValueError, "cannot be negative"):
            build_bands_and_slots(
                _config(overflow_global_active_lot_budget_cap="-1")
            )

    def test_run_ids_and_selected_row_fingerprints_are_replay_safe(self) -> None:
        candles = [_candle()]

        self.assertNotEqual(_new_run_id(), _new_run_id())
        self.assertEqual(
            _selected_candle_rows_sha256(candles),
            _selected_candle_rows_sha256(candles),
        )
        changed = [dict(candles[0], close="1.11")]
        self.assertNotEqual(
            _selected_candle_rows_sha256(candles),
            _selected_candle_rows_sha256(changed),
        )

    def test_all_in_buy_budget_includes_gross_buy_fee(self) -> None:
        terms = plan_buy(
            entry_price=Decimal("2"),
            cash_budget=Decimal("10"),
            gross_buy_fee_rate=Decimal("0.01"),
            base_increment=Decimal("0.001"),
            min_quote_notional=Decimal("1"),
        )

        self.assertLessEqual(terms["cash_cost"], Decimal("10"))
        self.assertEqual(terms["cash_cost"], terms["buy_notional"] + terms["gross_buy_fee"])
        self.assertEqual(terms["base_quantity"] % Decimal("0.001"), Decimal("0"))

    def test_principal_recovery_exit_returns_cash_target_and_reserve(self) -> None:
        buy = plan_buy(
            entry_price=Decimal("3"),
            cash_budget=Decimal("5"),
            gross_buy_fee_rate=Decimal("0.006"),
            base_increment=Decimal("0.000001"),
            min_quote_notional=Decimal("1"),
        )
        exit_plan = plan_exit(
            cash_cost=buy["cash_cost"],
            base_quantity=buy["base_quantity"],
            target_exit_price=Decimal("3.15"),
            cash_profit_bps=Decimal("20"),
            gross_sell_fee_rate=Decimal("0.006"),
            base_increment=Decimal("0.000001"),
            exit_policy="principal_recovery",
        )

        self.assertTrue(exit_plan["feasible"])
        self.assertGreaterEqual(
            exit_plan["net_sell_proceeds"],
            buy["cash_cost"] + exit_plan["target_cash_profit"],
        )
        self.assertGreater(exit_plan["reserve_quantity"], Decimal("0"))
        self.assertEqual(
            exit_plan["sell_quantity"] + exit_plan["reserve_quantity"],
            buy["base_quantity"],
        )

    def test_rounding_can_make_a_tiny_reserve_infeasible(self) -> None:
        exit_plan = plan_exit(
            cash_cost=Decimal("10"),
            base_quantity=Decimal("10"),
            target_exit_price=Decimal("1.02"),
            cash_profit_bps=Decimal("1"),
            gross_sell_fee_rate=Decimal("0.01"),
            base_increment=Decimal("1"),
            exit_policy="principal_recovery",
        )

        self.assertFalse(exit_plan["feasible"])
        self.assertIn(exit_plan["reason"], {"sell_quantity_exceeds_bought_base", "reserve_below_one_base_increment"})


class ReserveGridSimulationTests(unittest.TestCase):
    def test_backtest_writer_emits_lot_diagnostic_sidecar(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            candle_dir = root / "derived" / "v1" / "candles_fallback"
            candle_dir.mkdir(parents=True)
            candle_path = candle_dir / "part_fixture.jsonl"
            candles = [
                _flat_candle("2025-01-01T00:00:00Z", "1.20", "1h"),
                _flat_candle("2025-01-01T01:00:00Z", "1.10", "1h"),
            ]
            for candle in candles:
                candle["source_path"] = str(candle_path)
            with candle_path.open("wt", encoding="utf-8", newline="\n") as handle:
                for candle in candles:
                    handle.write(json.dumps(candle, sort_keys=True) + "\n")

            result = run_reserve_gridbot_backtest(
                derived_root=root / "derived",
                catalog_root=root / "catalog",
                product="XRP-USD",
                lower="1.10",
                upper="1.20",
                band_width="0.10",
                levels_per_band=1,
                band_active_lot_budget_cap="10",
                quote_start="100",
                exit_move_pct="0.05",
                cash_profit_bps="20",
                price_increment="0.01",
                fee_rate="0",
                include_fallback_candles=True,
                providers=("fixture",),
            )

            run_dir = Path(result["run_dir"])
            self.assertEqual(
                result["summary"]["engine"],
                "banded_lot_reserve_gridbot_v1.4",
            )
            self.assertTrue((run_dir / "lot_diagnostics.jsonl").exists())
            diagnostic_rows = [
                json.loads(line)
                for line in (run_dir / "lot_diagnostics.jsonl")
                .read_text(encoding="utf-8")
                .splitlines()
            ]
            self.assertEqual(len(diagnostic_rows), 1)
            self.assertEqual(diagnostic_rows[0]["lot_id"], "lot-000001")

    def test_entry_guard_is_causal_and_blocks_only_new_buys(self) -> None:
        candles = [
            _flat_candle("2025-01-01T00:00:00Z", "1.20"),
            _flat_candle("2025-01-01T00:01:00Z", "1.10"),
            _flat_candle("2025-01-01T00:02:00Z", "1.00"),
            _candle(
                ts="2025-01-01T00:03:00Z",
                open_price="1.00",
                high="1.00",
                low="0.90",
                close="0.95",
            ),
        ]
        guarded_config = _config(
            lower="0.90",
            upper="1.00",
            band_width="0.10",
            levels_per_band=1,
            band_active_lot_budget_cap="10",
            price_increment="0.01",
            fee_rate="0",
            candle_path_assumption="high-first",
            start="2025-01-01T00:03:00Z",
            entry_guard="ema_cross",
            entry_guard_fast_ema_span_candles=2,
            entry_guard_slow_ema_span_candles=3,
        )

        guarded = simulate_reserve_gridbot_on_candles(candles, guarded_config)
        changed_current_close = [dict(row) for row in candles]
        changed_current_close[-1]["close"] = "0.90"
        guarded_changed_close = simulate_reserve_gridbot_on_candles(
            changed_current_close,
            guarded_config,
        )
        control = simulate_reserve_gridbot_on_candles(
            [candles[-1]],
            replace(guarded_config, entry_guard="none", start=None),
        )

        guard_events = [
            event
            for event in guarded["events"]
            if str(event.get("reason") or "").startswith("entry_guard_")
        ]
        changed_guard_events = [
            event
            for event in guarded_changed_close["events"]
            if str(event.get("reason") or "").startswith("entry_guard_")
        ]
        self.assertEqual(guard_events, changed_guard_events)
        self.assertEqual(len(guard_events), 1)
        self.assertEqual(
            guard_events[0]["reason"],
            "entry_guard_fast_ema_below_slow_ema",
        )
        self.assertEqual(guard_events[0]["entry_guard_signal_as_of_ts"], "2025-01-01T00:02:00Z")
        self.assertLess(
            Decimal(guard_events[0]["entry_guard_fast_ema"]),
            Decimal(guard_events[0]["entry_guard_slow_ema"]),
        )
        self.assertEqual(
            Decimal(guard_events[0]["entry_guard_fast_ema"]).quantize(
                Decimal("0.000001")
            ),
            Decimal("1.044444"),
        )
        self.assertEqual(
            Decimal(guard_events[0]["entry_guard_slow_ema"]),
            Decimal("1.075"),
        )
        self.assertEqual(guarded["summary"]["filled_buys"], 0)
        self.assertEqual(control["summary"]["filled_buys"], 1)
        self.assertEqual(guarded["summary"]["missed_buys_entry_guard_downtrend"], 1)
        self.assertEqual(guarded["summary"]["entry_guard_band_reconciliation_error"], 0)
        self.assertEqual(guarded["summary"]["entry_guard_tranche_reconciliation_error"], 0)
        self.assertEqual(guarded["summary"]["entry_guard_reason_reconciliation_error"], 0)

    def test_backtest_writer_loads_signal_only_preroll_before_start(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            candle_dir = root / "derived" / "v1" / "candles_fallback"
            candle_dir.mkdir(parents=True)
            candle_path = candle_dir / "part_fixture.jsonl"
            candles = [
                _flat_candle("2025-01-01T00:00:00Z", "1.20"),
                _flat_candle("2025-01-01T00:01:00Z", "1.10"),
                _flat_candle("2025-01-01T00:02:00Z", "1.00"),
                _candle(
                    ts="2025-01-01T00:03:00Z",
                    open_price="1.00",
                    high="1.00",
                    low="0.90",
                    close="0.95",
                ),
            ]
            for candle in candles:
                candle["source_path"] = str(candle_path)
            with candle_path.open("wt", encoding="utf-8", newline="\n") as handle:
                for candle in candles:
                    handle.write(json.dumps(candle, sort_keys=True) + "\n")

            result = run_reserve_gridbot_backtest(
                derived_root=root / "derived",
                catalog_root=root / "catalog",
                product="XRP-USD",
                lower="0.90",
                upper="1.00",
                band_width="0.10",
                levels_per_band=1,
                band_active_lot_budget_cap="10",
                quote_start="100",
                exit_move_pct="0.05",
                cash_profit_bps="20",
                price_increment="0.01",
                fee_rate="0",
                include_fallback_candles=True,
                candle_path_assumption="high-first",
                start="2025-01-01T00:03:00Z",
                providers=("fixture",),
                entry_guard="ema_cross",
                entry_guard_fast_ema_span_candles=2,
                entry_guard_slow_ema_span_candles=3,
            )

            report = result["summary"]["candle_input_report"]
            self.assertEqual(result["summary"]["candles_used"], 1)
            self.assertEqual(result["summary"]["entry_guard_signal_preroll_candles_used"], 3)
            self.assertEqual(report["signal_preroll_rows_loaded"], 3)
            self.assertIsNotNone(report["selected_signal_preroll_rows_sha256"])
            self.assertEqual(
                report["trading_derived_sources"][0]["derived_path"],
                str(candle_path.resolve()),
            )
            self.assertEqual(
                report["trading_derived_sources"][0]["selected_rows"],
                1,
            )
            self.assertEqual(
                report["signal_preroll_derived_sources"][0]["selected_rows"],
                3,
            )
            self.assertIsNotNone(
                report["signal_preroll_derived_sources"][0]["sha256"]
            )
            self.assertNotEqual(
                report["selected_signal_preroll_rows_sha256"],
                report["selected_trading_candle_rows_sha256"],
            )

    def test_entry_guard_fails_closed_during_warmup_and_after_small_gap(self) -> None:
        warmup_candles = [
            _flat_candle("2025-01-01T00:00:00Z", "1.20"),
            _flat_candle("2025-01-01T00:01:00Z", "1.10"),
            _candle(
                ts="2025-01-01T00:02:00Z",
                open_price="1.10",
                high="1.10",
                low="1.00",
                close="1.00",
            ),
        ]
        config = _config(
            lower="1.00",
            upper="1.10",
            band_width="0.10",
            levels_per_band=1,
            price_increment="0.01",
            fee_rate="0",
            candle_path_assumption="high-first",
            start="2025-01-01T00:02:00Z",
            entry_guard="ema_cross",
            entry_guard_fast_ema_span_candles=2,
            entry_guard_slow_ema_span_candles=3,
        )
        warmup = simulate_reserve_gridbot_on_candles(warmup_candles, config)
        self.assertEqual(warmup["summary"]["missed_buys_entry_guard_warmup"], 1)
        self.assertEqual(warmup["summary"]["gross_fees_quote"], "0")

        stale_candles = [
            _flat_candle("2025-01-01T00:00:00Z", "1.00"),
            _flat_candle("2025-01-01T00:01:00Z", "1.10"),
            _flat_candle("2025-01-01T00:02:00Z", "1.20"),
            _candle(
                ts="2025-01-01T00:04:00Z",
                open_price="1.20",
                high="1.20",
                low="1.00",
                close="1.00",
            ),
            _flat_candle("2025-01-01T00:05:00Z", "1.00"),
        ]
        stale = simulate_reserve_gridbot_on_candles(
            stale_candles,
            replace(config, start="2025-01-01T00:04:00Z"),
        )
        self.assertGreaterEqual(stale["summary"]["missed_buys_entry_guard_stale"], 1)
        self.assertEqual(stale["summary"]["entry_guard_candle_counts"]["stale"], 1)
        self.assertNotIn(
            stale["equity_curve"][-1]["entry_guard_status"],
            {"stale", "warmup"},
        )

    def test_entry_guard_large_gap_resets_and_requires_new_warmup(self) -> None:
        candles = [
            _flat_candle("2025-01-01T00:00:00Z", "1.00"),
            _flat_candle("2025-01-01T00:01:00Z", "1.10"),
            _flat_candle("2025-01-01T00:02:00Z", "1.20"),
            _candle(
                ts="2025-01-01T00:05:00Z",
                open_price="1.20",
                high="1.20",
                low="1.00",
                close="1.00",
            ),
            _flat_candle("2025-01-01T00:06:00Z", "1.00"),
            _flat_candle("2025-01-01T00:07:00Z", "1.00"),
            _flat_candle("2025-01-01T00:08:00Z", "1.00"),
        ]
        result = simulate_reserve_gridbot_on_candles(
            candles,
            _config(
                lower="1.00",
                upper="1.10",
                band_width="0.10",
                levels_per_band=1,
                price_increment="0.01",
                fee_rate="0",
                candle_path_assumption="high-first",
                start="2025-01-01T00:05:00Z",
                entry_guard="ema_cross",
                entry_guard_fast_ema_span_candles=2,
                entry_guard_slow_ema_span_candles=3,
            ),
        )

        self.assertEqual(result["summary"]["entry_guard_large_gap_reset_count"], 1)
        self.assertEqual(result["summary"]["entry_guard_candle_counts"]["warmup"], 3)
        self.assertGreaterEqual(result["summary"]["missed_buys_entry_guard_warmup"], 1)
        self.assertNotIn(
            result["equity_curve"][-1]["entry_guard_status"],
            {"stale", "warmup"},
        )

    def test_entry_guard_off_preserves_direct_simulator_start_semantics(self) -> None:
        candles = [
            _flat_candle("2025-01-01T00:00:00Z", "1.20"),
            _flat_candle("2025-01-01T00:01:00Z", "1.10"),
            _flat_candle("2025-01-01T00:02:00Z", "1.00"),
        ]
        config = _config(start="2025-01-01T00:02:00Z", entry_guard="none")

        with_start = simulate_reserve_gridbot_on_candles(candles, config)
        without_start = simulate_reserve_gridbot_on_candles(
            candles,
            replace(config, start=None),
        )

        self.assertEqual(with_start, without_start)
        self.assertNotIn("entry_guard_status", with_start["equity_curve"][0])

    def test_entry_guard_uses_only_the_last_slow_span_as_preroll(self) -> None:
        extra_old = [
            _flat_candle("2024-12-31T23:58:00Z", "5.00"),
            _flat_candle("2024-12-31T23:59:00Z", "5.00"),
        ]
        frozen_preroll = [
            _flat_candle("2025-01-01T00:00:00Z", "1.20"),
            _flat_candle("2025-01-01T00:01:00Z", "1.10"),
            _flat_candle("2025-01-01T00:02:00Z", "1.00"),
        ]
        trade = _candle(
            ts="2025-01-01T00:03:00Z",
            open_price="1.00",
            high="1.00",
            low="0.90",
            close="0.95",
        )
        config = _config(
            lower="0.90",
            upper="1.00",
            band_width="0.10",
            levels_per_band=1,
            price_increment="0.01",
            fee_rate="0",
            candle_path_assumption="high-first",
            start="2025-01-01T00:03:00Z",
            entry_guard="ema_cross",
            entry_guard_fast_ema_span_candles=2,
            entry_guard_slow_ema_span_candles=3,
        )

        with_extra_history = simulate_reserve_gridbot_on_candles(
            extra_old + frozen_preroll + [trade],
            config,
        )
        frozen_only = simulate_reserve_gridbot_on_candles(
            frozen_preroll + [trade],
            config,
        )

        self.assertEqual(with_extra_history, frozen_only)

    def test_entry_guard_never_blocks_existing_lot_exit(self) -> None:
        candles = [
            _flat_candle("2025-01-01T00:00:00Z", "0.90"),
            _flat_candle("2025-01-01T00:01:00Z", "1.00"),
            _flat_candle("2025-01-01T00:02:00Z", "1.10"),
            _candle(
                ts="2025-01-01T00:03:00Z",
                open_price="1.10",
                high="1.10",
                low="1.00",
                close="1.00",
            ),
            _flat_candle("2025-01-01T00:04:00Z", "0.90"),
            _candle(
                ts="2025-01-01T00:05:00Z",
                open_price="0.90",
                high="1.05",
                low="0.90",
                close="0.90",
            ),
        ]
        result = simulate_reserve_gridbot_on_candles(
            candles,
            _config(
                lower="1.00",
                upper="1.10",
                band_width="0.10",
                levels_per_band=1,
                price_increment="0.01",
                fee_rate="0",
                candle_path_assumption="high-first",
                start="2025-01-01T00:03:00Z",
                entry_guard="ema_cross",
                entry_guard_fast_ema_span_candles=2,
                entry_guard_slow_ema_span_candles=3,
            ),
        )

        self.assertEqual(result["summary"]["completed_lots"], 1)
        self.assertEqual(result["summary"]["end_open_unrecovered_lots"], 0)
        self.assertGreaterEqual(result["summary"]["missed_buys_entry_guard_downtrend"], 1)
        self.assertEqual(
            [event["event"] for event in result["events"] if event["ts"] == "2025-01-01T00:05:00Z"][:2],
            ["exit_filled", "buy_missed"],
        )

    def test_entry_guard_requires_valid_spans_and_one_minute_candles(self) -> None:
        with self.assertRaisesRegex(ValueError, "entry_guard must be"):
            simulate_reserve_gridbot_on_candles(
                [_candle()],
                _config(entry_guard="unknown"),
            )
        with self.assertRaisesRegex(ValueError, "must exceed the fast span"):
            simulate_reserve_gridbot_on_candles(
                [_candle()],
                _config(
                    entry_guard="ema_cross",
                    entry_guard_fast_ema_span_candles=3,
                    entry_guard_slow_ema_span_candles=3,
                ),
            )
        with self.assertRaisesRegex(ValueError, "requires one-minute candles"):
            simulate_reserve_gridbot_on_candles(
                [_flat_candle("2025-01-01T00:00:00Z", "1.10", "1h")],
                _config(
                    entry_guard="ema_cross",
                    entry_guard_fast_ema_span_candles=2,
                    entry_guard_slow_ema_span_candles=3,
                ),
            )

    def test_low_first_and_high_first_keep_distinct_same_candle_order(self) -> None:
        candles = [_candle()]
        low_first = simulate_reserve_gridbot_on_candles(candles, _config())
        high_first = simulate_reserve_gridbot_on_candles(
            candles,
            _config(candle_path_assumption="high-first"),
        )

        self.assertEqual(low_first["summary"]["completed_lots"], 2)
        self.assertEqual(high_first["summary"]["completed_lots"], 1)
        self.assertGreater(Decimal(low_first["summary"]["reserve_base"]), Decimal("0"))
        self.assertEqual(low_first["summary"]["cash_reconciliation_error"], "0")
        self.assertEqual(low_first["summary"]["base_reconciliation_error"], "0")
        self.assertEqual(low_first["summary"]["pnl_reconciliation_error"], "0")
        self.assertEqual(low_first["lot_diagnostics"][0]["close_sample_count_while_open"], 0)
        self.assertIsNone(
            low_first["lot_diagnostics"][0][
                "close_sampled_maximum_adverse_excursion_bps"
            ]
        )

    def test_recorded_open_gap_is_processed_before_intracandle_extremes(self) -> None:
        candles = [
            _candle(
                ts="2025-01-01T00:00:00Z",
                open_price="1.02",
                high="1.02",
                low="1.00",
                close="1.02",
            ),
            _candle(
                ts="2025-01-01T00:01:00Z",
                open_price="1.06",
                high="1.07",
                low="1.00",
                close="1.01",
            ),
        ]
        result = simulate_reserve_gridbot_on_candles(
            candles,
            _config(levels_per_band=1, fee_rate="0"),
        )
        second_candle_events = [
            event["event"]
            for event in result["events"]
            if event["ts"] == "2025-01-01T00:01:00Z"
        ]

        self.assertEqual(
            second_candle_events,
            ["exit_filled", "buy_filled", "exit_filled"],
        )

    def test_static_infeasible_slot_is_disabled_after_one_attempt(self) -> None:
        candles = [
            _candle(),
            _candle(ts="2025-01-01T00:01:00Z"),
        ]
        result = simulate_reserve_gridbot_on_candles(
            candles,
            _config(
                levels_per_band=1,
                band_active_lot_budget_cap="10",
                base_increment="1",
                exit_move_pct="0.02",
                cash_profit_bps="1",
                fee_rate="0.01",
            ),
        )

        self.assertEqual(result["summary"]["disabled_infeasible_slots"], 1)
        self.assertEqual(
            [event["event"] for event in result["events"]],
            ["slot_disabled"],
        )

    def test_v1_rejects_non_xrp_product_defaults(self) -> None:
        with self.assertRaisesRegex(ValueError, "limited to XRP-USD"):
            simulate_reserve_gridbot_on_candles(
                [_candle()],
                _config(product_id="BTC-USD"),
            )

        mismatched = _candle()
        mismatched["product_id"] = "BTC-USD"
        with self.assertRaisesRegex(ValueError, "candle must have product_id XRP-USD"):
            simulate_reserve_gridbot_on_candles([mismatched], _config())

    def test_candles_require_strict_chronological_uniqueness(self) -> None:
        duplicate_ts = [_candle(), _candle()]

        with self.assertRaisesRegex(ValueError, "strictly increasing unique"):
            simulate_reserve_gridbot_on_candles(duplicate_ts, _config())

    def test_shared_cash_prevents_band_caps_from_minting_money(self) -> None:
        config = _config(
            upper="1.40",
            levels_per_band=1,
            band_active_lot_budget_cap="10",
            quote_start="10",
            exit_move_pct="0.50",
        )
        candles = [_candle(open_price="1.30", high="1.30", low="1.00", close="1.00")]
        result = simulate_reserve_gridbot_on_candles(candles, config)
        summary = result["summary"]

        self.assertTrue(summary["band_caps_exceed_starting_cash"])
        self.assertEqual(summary["filled_buys"], 1)
        self.assertGreaterEqual(summary["missed_buys_insufficient_shared_cash"], 1)
        self.assertLessEqual(Decimal(summary["maximum_active_cash_cost"]), Decimal("10"))
        self.assertGreaterEqual(Decimal(summary["final_quote_cash"]), Decimal("0"))

    def test_rebate_changes_receivable_but_never_spendable_cash_or_exit(self) -> None:
        candles = [_candle()]
        without_rebate = simulate_reserve_gridbot_on_candles(
            candles,
            _config(coinbase_one_advanced_rebate_rate="0"),
        )
        with_rebate = simulate_reserve_gridbot_on_candles(
            candles,
            _config(coinbase_one_advanced_rebate_rate="0.25"),
        )

        self.assertEqual(
            without_rebate["summary"]["final_quote_cash"],
            with_rebate["summary"]["final_quote_cash"],
        )
        self.assertEqual(
            without_rebate["summary"]["final_equity_before_rebate"],
            with_rebate["summary"]["final_equity_before_rebate"],
        )
        self.assertEqual(
            [lot["planned_sell_quantity"] for lot in without_rebate["lots"]],
            [lot["planned_sell_quantity"] for lot in with_rebate["lots"]],
        )
        self.assertGreater(
            Decimal(with_rebate["summary"]["final_equity_after_modeled_rebate"]),
            Decimal(without_rebate["summary"]["final_equity_after_modeled_rebate"]),
        )

    def test_full_lot_control_has_no_reserve(self) -> None:
        candles = [_candle()]
        reserve_result = simulate_reserve_gridbot_on_candles(candles, _config())
        full_lot_result = simulate_reserve_gridbot_on_candles(
            candles,
            _config(exit_policy="full_lot"),
        )

        self.assertGreater(Decimal(reserve_result["summary"]["reserve_base"]), Decimal("0"))
        self.assertEqual(full_lot_result["summary"]["reserve_base"], "0")
        self.assertGreater(
            Decimal(full_lot_result["summary"]["final_quote_cash"]),
            Decimal(reserve_result["summary"]["final_quote_cash"]),
        )

    def test_repeated_simulation_is_deterministic(self) -> None:
        candles = [
            _candle(),
            _candle(
                ts="2025-01-01T00:01:00Z",
                open_price="1.10",
                high="1.18",
                low="1.02",
                close="1.12",
            ),
        ]
        config = _config()

        first = simulate_reserve_gridbot_on_candles(candles, config)
        second = simulate_reserve_gridbot_on_candles(candles, config)

        self.assertEqual(first, second)
        self.assertEqual(first["summary"]["cash_reconciliation_error"], "0")
        self.assertEqual(first["summary"]["base_reconciliation_error"], "0")
        self.assertEqual(first["summary"]["pnl_reconciliation_error"], "0")
        self.assertEqual(first["summary"]["gross_fee_reconciliation_error"], "0")
        self.assertEqual(first["summary"]["rebate_reconciliation_error"], "0")
        self.assertEqual(first["summary"]["net_fee_reconciliation_error"], "0")
        self.assertEqual(first["summary"]["turnover_reconciliation_error"], "0")
        self.assertEqual(first["summary"]["cash_profit_reconciliation_error"], "0")
        self.assertEqual(first["summary"]["band_active_cash_reconciliation_error"], "0")
        self.assertEqual(first["summary"]["band_reserve_base_reconciliation_error"], "0")
        self.assertEqual(first["summary"]["band_cash_profit_reconciliation_error"], "0")
        self.assertTrue(all(Decimal(row["max_active_cash_cost"]) <= Decimal("20") for row in first["bands"]))

    def test_lot_recovery_diagnostics_are_post_trade_and_measure_recovery_path(self) -> None:
        start = datetime(2025, 1, 1, tzinfo=timezone.utc)
        prices: list[str] = []
        current_price = "1.20"
        for hour in range(170):
            if hour == 1:
                current_price = "1.10"
            elif hour == 2:
                current_price = "1.05"
            elif hour == 7:
                current_price = "1.08"
            elif hour == 25:
                current_price = "1.12"
            elif hour == 169:
                current_price = "1.16"
            prices.append(current_price)
        candles = [
            _flat_candle(
                (start + timedelta(hours=hour)).isoformat().replace("+00:00", "Z"),
                price,
                "1h",
            )
            for hour, price in enumerate(prices)
        ]
        result = simulate_reserve_gridbot_on_candles(
            candles,
            _config(
                lower="1.10",
                upper="1.20",
                band_width="0.10",
                levels_per_band=1,
                band_active_lot_budget_cap="10",
                fee_rate="0",
                price_increment="0.01",
            ),
        )

        self.assertEqual(
            result["summary"]["base_trade_decision_fingerprint_sha256"],
            "61535eb097e6e902d6f338f75af8d903be58e649a8e8da078e60327f2e0ffd28",
        )
        self.assertEqual(
            [(event["ts"], event["event"]) for event in result["events"]],
            [
                ("2025-01-01T01:00:00Z", "buy_filled"),
                ("2025-01-08T01:00:00Z", "exit_filled"),
            ],
        )
        diagnostic = result["lot_diagnostics"][0]
        self.assertEqual(diagnostic["lot_id"], "lot-000001")
        self.assertEqual(diagnostic["status"], "completed_recovered")
        self.assertEqual(diagnostic["recovery_seconds"], "604800")
        self.assertEqual(diagnostic["diagnostic_observation_seconds"], "604800")
        self.assertEqual(diagnostic["minimum_assumed_path_price_while_open"], "1.05")
        self.assertEqual(
            Decimal(
                diagnostic["path_assumed_maximum_adverse_excursion_bps"]
            ).quantize(Decimal("0.0001")),
            Decimal("454.5455"),
        )
        self.assertEqual(diagnostic["minimum_close_while_open"], "1.05")
        self.assertEqual(
            Decimal(
                diagnostic["close_sampled_maximum_adverse_excursion_bps"]
            ).quantize(Decimal("0.0001")),
            Decimal("454.5455"),
        )
        self.assertEqual(diagnostic["recovery_windows"]["7d"]["status"], "recovered")
        self.assertTrue(diagnostic["recovery_windows"]["7d"]["recovered"])
        self.assertTrue(diagnostic["recovery_windows"]["14d"]["recovered"])
        self.assertTrue(diagnostic["recovery_windows"]["28d"]["recovered"])
        self.assertEqual(diagnostic["price_only_close_markouts"]["1h"]["close"], "1.05")
        self.assertEqual(diagnostic["price_only_close_markouts"]["6h"]["close"], "1.08")
        self.assertEqual(diagnostic["price_only_close_markouts"]["24h"]["close"], "1.12")
        self.assertEqual(diagnostic["price_only_close_markouts"]["7d"]["close"], "1.16")
        self.assertTrue(
            all(
                markout["status"] == "observed"
                for markout in diagnostic["price_only_close_markouts"].values()
            )
        )
        recovery_summary = result["summary"]["lot_recovery_diagnostics"]
        self.assertTrue(recovery_summary["diagnostic_only"])
        self.assertFalse(recovery_summary["used_by_trade_decisions"])
        self.assertEqual(recovery_summary["lots_observed"], 1)
        self.assertEqual(recovery_summary["completed_recovered_lots"], 1)
        self.assertEqual(recovery_summary["open_right_censored_lots"], 0)
        self.assertEqual(
            recovery_summary["recovery_windows"]["7d"]["recovery_rate_among_eligible"],
            "1",
        )
        self.assertEqual(
            recovery_summary["active_cash_cost_time_reconciliation_error_quote_seconds"],
            "0",
        )

    def test_open_lot_recovery_windows_distinguish_failure_from_censoring(self) -> None:
        start = datetime(2025, 1, 1, tzinfo=timezone.utc)
        candles = []
        for hour in range(242):
            if hour == 0:
                price = "1.20"
            elif hour == 1:
                price = "1.10"
            elif hour < 25:
                price = "1.05"
            else:
                price = "1.00"
            candles.append(
                _flat_candle(
                    (start + timedelta(hours=hour)).isoformat().replace(
                        "+00:00", "Z"
                    ),
                    price,
                    "1h",
                )
            )
        result = simulate_reserve_gridbot_on_candles(
            candles,
            _config(
                lower="1.10",
                upper="1.20",
                band_width="0.10",
                levels_per_band=1,
                band_active_lot_budget_cap="10",
                fee_rate="0",
                price_increment="0.01",
            ),
        )

        diagnostic = result["lot_diagnostics"][0]
        self.assertEqual(diagnostic["status"], "open_right_censored")
        self.assertEqual(diagnostic["diagnostic_observation_seconds"], "864000")
        self.assertEqual(
            diagnostic["recovery_windows"]["7d"]["status"],
            "not_recovered",
        )
        self.assertFalse(diagnostic["recovery_windows"]["7d"]["recovered"])
        self.assertEqual(
            diagnostic["recovery_windows"]["14d"]["status"],
            "right_censored",
        )
        self.assertIsNone(diagnostic["recovery_windows"]["14d"]["recovered"])
        self.assertEqual(
            diagnostic["recovery_windows"]["28d"]["status"],
            "right_censored",
        )
        summary = result["summary"]["lot_recovery_diagnostics"]
        self.assertEqual(summary["recovery_windows"]["7d"]["eligible_lots"], 1)
        self.assertEqual(summary["recovery_windows"]["7d"]["known_failure_lots"], 1)
        self.assertEqual(
            summary["recovery_windows"]["7d"]["recovery_rate_among_eligible"],
            "0",
        )
        self.assertEqual(summary["recovery_windows"]["14d"]["eligible_lots"], 0)
        self.assertEqual(summary["recovery_windows"]["14d"]["right_censored_lots"], 1)

    def test_markout_after_a_timestamp_gap_is_not_treated_as_on_time(self) -> None:
        candles = [
            _flat_candle("2025-01-01T00:00:00Z", "1.20", "1h"),
            _flat_candle("2025-01-01T01:00:00Z", "1.10", "1h"),
            _flat_candle("2025-01-01T02:00:00Z", "1.05", "1h"),
            _flat_candle("2025-01-01T03:00:00Z", "1.04", "1h"),
            _flat_candle("2025-01-01T10:00:00Z", "1.03", "1h"),
        ]
        result = simulate_reserve_gridbot_on_candles(
            candles,
            _config(
                lower="1.10",
                upper="1.20",
                band_width="0.10",
                levels_per_band=1,
                band_active_lot_budget_cap="10",
                fee_rate="0",
                price_increment="0.01",
            ),
        )

        markouts = result["lot_diagnostics"][0]["price_only_close_markouts"]
        self.assertEqual(markouts["1h"]["status"], "observed")
        self.assertEqual(markouts["6h"]["status"], "delayed_by_data_gap")
        self.assertIsNone(markouts["6h"]["price_change_bps"])
        self.assertEqual(
            result["summary"]["lot_recovery_diagnostics"]["candle_coverage"][
                "gaps_exceeding_tolerance"
            ],
            1,
        )

    def test_sparse_candles_cannot_define_their_own_recovery_cadence(self) -> None:
        candles = [
            _flat_candle("2025-01-01T00:00:00Z", "1.20"),
            _flat_candle("2025-01-01T00:01:00Z", "1.10"),
            _flat_candle("2025-01-09T00:01:00Z", "1.00"),
        ]
        result = simulate_reserve_gridbot_on_candles(
            candles,
            _config(
                lower="1.10",
                upper="1.20",
                band_width="0.10",
                levels_per_band=1,
                band_active_lot_budget_cap="10",
                fee_rate="0",
                price_increment="0.01",
            ),
        )

        recovery = result["lot_diagnostics"][0]["recovery_windows"]["7d"]
        summary = result["summary"]["lot_recovery_diagnostics"]
        self.assertEqual(recovery["status"], "data_gap_unknown")
        self.assertIsNone(recovery["recovered"])
        self.assertFalse(recovery["full_followup_eligible"])
        self.assertEqual(summary["recovery_windows"]["7d"]["eligible_lots"], 0)
        self.assertEqual(
            summary["recovery_windows"]["7d"]["data_gap_unknown_lots"],
            1,
        )
        self.assertEqual(
            summary["candle_coverage"]["expected_interval_seconds"],
            "60",
        )
        self.assertEqual(summary["candle_coverage"]["gaps_exceeding_tolerance"], 1)

    def test_recovery_observed_before_a_gap_is_excluded_from_full_cohort(self) -> None:
        candles = [
            _flat_candle("2025-01-01T00:00:00Z", "1.20"),
            _flat_candle("2025-01-01T00:01:00Z", "1.10"),
            _flat_candle("2025-01-01T00:02:00Z", "1.16"),
            _flat_candle("2025-01-09T00:01:00Z", "1.16"),
        ]
        result = simulate_reserve_gridbot_on_candles(
            candles,
            _config(
                lower="1.10",
                upper="1.20",
                band_width="0.10",
                levels_per_band=1,
                band_active_lot_budget_cap="10",
                fee_rate="0",
                price_increment="0.01",
            ),
        )

        recovery = result["lot_diagnostics"][0]["recovery_windows"]["7d"]
        summary = result["summary"]["lot_recovery_diagnostics"][
            "recovery_windows"
        ]["7d"]
        self.assertEqual(recovery["status"], "recovered")
        self.assertTrue(recovery["recovered"])
        self.assertFalse(recovery["full_followup_eligible"])
        self.assertFalse(recovery["coverage_complete_through_deadline"])
        self.assertEqual(summary["eligible_lots"], 0)
        self.assertEqual(summary["recovered_lots"], 0)
        self.assertEqual(summary["recovered_but_ineligible_lots"], 1)
        self.assertIsNone(summary["recovery_rate_among_eligible"])

    def test_lot_created_at_gap_boundary_is_coverage_incomplete(self) -> None:
        start = datetime(2025, 1, 1, tzinfo=timezone.utc)
        candles = [_flat_candle(_format_test_ts(start), "1.20", "1h")]
        for hour in range(2, 171):
            price = "1.10" if hour == 2 else "1.00"
            candles.append(
                _flat_candle(
                    _format_test_ts(start + timedelta(hours=hour)),
                    price,
                    "1h",
                )
            )
        result = simulate_reserve_gridbot_on_candles(
            candles,
            _config(
                lower="1.10",
                upper="1.20",
                band_width="0.10",
                levels_per_band=1,
                band_active_lot_budget_cap="10",
                fee_rate="0",
                price_increment="0.01",
            ),
        )

        diagnostic = result["lot_diagnostics"][0]
        recovery = diagnostic["recovery_windows"]["7d"]
        summary = result["summary"]["lot_recovery_diagnostics"]
        self.assertTrue(diagnostic["entry_timestamp_is_gap_right_boundary"])
        self.assertFalse(
            diagnostic["candle_coverage_complete_to_observation_end"]
        )
        self.assertEqual(recovery["status"], "data_gap_unknown")
        self.assertFalse(recovery["full_followup_eligible"])
        self.assertEqual(summary["recovery_windows"]["7d"]["eligible_lots"], 0)
        self.assertEqual(
            summary["recovery_windows"]["7d"]["data_gap_unknown_lots"],
            1,
        )
        self.assertEqual(summary["adverse_excursion_coverage_complete_lots"], 0)
        self.assertEqual(summary["adverse_excursion_coverage_incomplete_lots"], 1)

    def test_buy_markouts_continue_after_the_lot_recovers(self) -> None:
        start = datetime(2025, 1, 1, tzinfo=timezone.utc)
        prices: list[str] = []
        current_price = "1.20"
        for hour in range(170):
            if hour == 1:
                current_price = "1.10"
            elif hour == 2:
                current_price = "1.16"
            elif hour == 7:
                current_price = "1.12"
            elif hour == 25:
                current_price = "1.13"
            elif hour == 169:
                current_price = "1.14"
            prices.append(current_price)
        candles = [
            _flat_candle(
                (start + timedelta(hours=hour)).isoformat().replace("+00:00", "Z"),
                price,
                "1h",
            )
            for hour, price in enumerate(prices)
        ]
        config = _config(
            lower="1.10",
            upper="1.20",
            band_width="0.10",
            levels_per_band=1,
            band_active_lot_budget_cap="10",
            fee_rate="0",
            price_increment="0.01",
        )
        prefix = simulate_reserve_gridbot_on_candles(candles[:3], config)
        result = simulate_reserve_gridbot_on_candles(candles, config)

        diagnostic = result["lot_diagnostics"][0]
        self.assertEqual(prefix["events"], result["events"])
        self.assertEqual(
            prefix["summary"]["base_trade_decision_fingerprint_sha256"],
            result["summary"]["base_trade_decision_fingerprint_sha256"],
        )
        self.assertEqual(
            prefix["lot_diagnostics"][0]["price_only_close_markouts"]["6h"][
                "status"
            ],
            "right_censored",
        )
        self.assertEqual(diagnostic["recovery_seconds"], "3600")
        self.assertEqual(diagnostic["price_only_close_markouts"]["6h"]["status"], "observed")
        self.assertEqual(diagnostic["price_only_close_markouts"]["6h"]["close"], "1.12")
        self.assertEqual(diagnostic["price_only_close_markouts"]["7d"]["status"], "observed")
        self.assertEqual(diagnostic["price_only_close_markouts"]["7d"]["close"], "1.14")

    def test_path_adverse_excursion_does_not_leak_between_same_slot_lots(self) -> None:
        candles = [
            _candle(
                ts="2025-01-01T00:00:00Z",
                open_price="1.14",
                high="1.14",
                low="1.10",
                close="1.10",
                timeframe="1h",
            ),
            _candle(
                ts="2025-01-01T01:00:00Z",
                open_price="1.10",
                high="1.16",
                low="1.00",
                close="1.05",
                timeframe="1h",
            ),
        ]
        config = _config(
            lower="1.10",
            upper="1.20",
            band_width="0.10",
            levels_per_band=1,
            band_active_lot_budget_cap="10",
            fee_rate="0",
            price_increment="0.01",
        )
        low_first = simulate_reserve_gridbot_on_candles(candles, config)
        high_first = simulate_reserve_gridbot_on_candles(
            candles,
            replace(config, candle_path_assumption="high-first"),
        )

        def event_skeleton(result):
            return [
                (event["ts"], event["event"], event.get("lot_id"))
                for event in result["events"]
            ]

        self.assertEqual(event_skeleton(low_first), event_skeleton(high_first))
        low_diagnostics = {row["lot_id"]: row for row in low_first["lot_diagnostics"]}
        high_diagnostics = {row["lot_id"]: row for row in high_first["lot_diagnostics"]}
        self.assertEqual(
            low_diagnostics["lot-000001"][
                "minimum_assumed_path_price_while_open"
            ],
            "1",
        )
        self.assertEqual(
            high_diagnostics["lot-000001"][
                "minimum_assumed_path_price_while_open"
            ],
            "1.1",
        )
        self.assertEqual(
            low_diagnostics["lot-000002"][
                "minimum_assumed_path_price_while_open"
            ],
            "1.05",
        )
        self.assertEqual(
            high_diagnostics["lot-000002"][
                "minimum_assumed_path_price_while_open"
            ],
            "1",
        )
        self.assertEqual(
            low_diagnostics["lot-000002"][
                "close_sampled_maximum_adverse_excursion_bps"
            ],
            high_diagnostics["lot-000002"][
                "close_sampled_maximum_adverse_excursion_bps"
            ],
        )


class ReserveGridOverflowTests(unittest.TestCase):
    def test_overflow_adds_one_tagged_base_first_tranche_at_crossed_level(self) -> None:
        candles = [
            _flat_candle("2025-01-01T00:00:00Z", "1.20"),
            _flat_candle("2025-01-01T00:01:00Z", "1.19"),
        ]
        result = simulate_reserve_gridbot_on_candles(
            candles,
            _config(
                levels_per_band=20,
                band_active_lot_budget_cap="100",
                quote_start="100",
                price_increment="0.01",
                fee_rate="0",
                overflow_global_active_lot_budget_cap="100",
            ),
        )
        summary = result["summary"]
        fills = [event for event in result["events"] if event["event"] == "buy_filled"]

        self.assertEqual([event["tranche"] for event in fills], ["base", "overflow"])
        self.assertEqual([lot["tranche"] for lot in result["lots"]], ["base", "overflow"])
        self.assertEqual(summary["entry_level_count"], 20)
        self.assertEqual(summary["trading_slot_count"], 40)
        self.assertEqual(summary["base_filled_buys"], 1)
        self.assertEqual(summary["overflow_filled_buys"], 1)
        self.assertEqual(summary["base_end_open_unrecovered_lots"], 1)
        self.assertEqual(summary["overflow_end_open_unrecovered_lots"], 1)
        self.assertEqual(summary["final_base_active_cash_cost"], "4.9999992")
        self.assertEqual(summary["final_overflow_active_cash_cost"], "4.9999992")
        self.assertEqual(summary["final_quote_cash"], "90.0000016")

    def test_overflow_global_cap_does_not_change_base_band_caps(self) -> None:
        candles = [
            _flat_candle("2025-01-01T00:00:00Z", "1.40"),
            _flat_candle("2025-01-01T00:01:00Z", "1.00"),
        ]
        result = simulate_reserve_gridbot_on_candles(
            candles,
            _config(
                upper="1.40",
                levels_per_band=20,
                band_active_lot_budget_cap="100",
                quote_start="1000",
                price_increment="0.01",
                exit_move_pct="0.50",
                fee_rate="0",
                overflow_global_active_lot_budget_cap="100",
            ),
        )
        summary = result["summary"]

        self.assertEqual(summary["base_filled_buys"], 40)
        self.assertEqual(summary["overflow_filled_buys"], 20)
        self.assertEqual(summary["missed_buys_overflow_global_cap"], 20)
        self.assertLessEqual(
            Decimal(summary["maximum_overflow_active_cash_cost"]),
            Decimal("100"),
        )
        self.assertTrue(
            all(
                Decimal(band["max_base_active_cash_cost"])
                <= Decimal(band["base_active_lot_budget_cap"])
                for band in result["bands"]
            )
        )
        self.assertGreater(
            Decimal(result["bands"][1]["max_overflow_active_cash_cost"]),
            Decimal("0"),
        )
        self.assertEqual(result["bands"][0]["max_overflow_active_cash_cost"], "0")

    def test_overflow_uses_same_shared_cash_and_base_gets_priority(self) -> None:
        candles = [
            _flat_candle("2025-01-01T00:00:00Z", "1.20"),
            _flat_candle("2025-01-01T00:01:00Z", "1.19"),
        ]
        result = simulate_reserve_gridbot_on_candles(
            candles,
            _config(
                levels_per_band=20,
                band_active_lot_budget_cap="100",
                quote_start="7",
                price_increment="0.01",
                fee_rate="0",
                overflow_global_active_lot_budget_cap="100",
            ),
        )
        summary = result["summary"]
        decisions = [
            (event["event"], event["tranche"], event.get("reason"))
            for event in result["events"]
        ]

        self.assertEqual(
            decisions,
            [
                ("buy_filled", "base", None),
                ("buy_missed", "overflow", "insufficient_shared_cash"),
            ],
        )
        self.assertEqual(summary["base_filled_buys"], 1)
        self.assertEqual(summary["overflow_filled_buys"], 0)
        self.assertEqual(summary["final_overflow_active_cash_cost"], "0")
        self.assertEqual(summary["final_quote_cash"], "2.0000008")

    def test_reserve_value_never_funds_overflow(self) -> None:
        candles = [_flat_candle("2025-01-01T00:00:00Z", "1.20")]
        minute = 1
        for _ in range(22):
            candles.append(
                _flat_candle(f"2025-01-01T00:{minute:02d}:00Z", "1.19")
            )
            minute += 1
            candles.append(
                _flat_candle(f"2025-01-01T00:{minute:02d}:00Z", "1.25")
            )
            minute += 1
        candles.append(_flat_candle(f"2025-01-01T00:{minute:02d}:00Z", "1.19"))
        result = simulate_reserve_gridbot_on_candles(
            candles,
            _config(
                levels_per_band=20,
                band_active_lot_budget_cap="100",
                quote_start="5",
                price_increment="0.01",
                fee_rate="0",
                overflow_global_active_lot_budget_cap="100",
            ),
        )
        summary = result["summary"]

        self.assertEqual(summary["base_completed_lots"], 22)
        self.assertEqual(summary["base_end_open_unrecovered_lots"], 1)
        self.assertEqual(summary["overflow_lots_created"], 0)
        self.assertGreaterEqual(summary["missed_buys_insufficient_shared_cash"], 23)
        self.assertGreater(
            Decimal(summary["reserve_value_quote"]),
            Decimal(summary["slot_cash_budget_all_in"]),
        )
        self.assertEqual(summary["overflow_tranche_reserve_base"], "0")
        self.assertEqual(summary["final_overflow_active_cash_cost"], "0")

    def test_overflow_full_lot_control_keeps_same_event_skeleton(self) -> None:
        candles = [
            _flat_candle("2025-01-01T00:00:00Z", "1.20"),
            _flat_candle("2025-01-01T00:01:00Z", "1.19"),
            _flat_candle("2025-01-01T00:02:00Z", "1.25"),
        ]
        config = _config(
            levels_per_band=20,
            band_active_lot_budget_cap="100",
            quote_start="20",
            price_increment="0.01",
            fee_rate="0",
            overflow_global_active_lot_budget_cap="100",
        )
        reserve = simulate_reserve_gridbot_on_candles(candles, config)
        full_lot = simulate_reserve_gridbot_on_candles(
            candles,
            replace(config, exit_policy="full_lot"),
        )

        def skeleton(result):
            return [
                (
                    event["ts"],
                    event["event"],
                    event["tranche"],
                    event["slot_id"],
                    event.get("entry_price"),
                    event.get("exit_price"),
                )
                for event in result["events"]
            ]

        self.assertEqual(skeleton(reserve), skeleton(full_lot))
        self.assertGreater(Decimal(reserve["summary"]["reserve_base"]), Decimal("0"))
        self.assertGreater(
            Decimal(reserve["summary"]["overflow_tranche_reserve_base"]),
            Decimal("0"),
        )
        self.assertEqual(full_lot["summary"]["reserve_base"], "0")
        self.assertEqual(
            reserve["summary"]["final_equity_before_rebate"],
            full_lot["summary"]["final_equity_before_rebate"],
        )

    def test_completed_overflow_exit_releases_capacity_for_later_entry(self) -> None:
        candles = [
            _flat_candle("2025-01-01T00:00:00Z", "1.20"),
            _flat_candle("2025-01-01T00:01:00Z", "1.19"),
            _flat_candle("2025-01-01T00:02:00Z", "1.25"),
            _flat_candle("2025-01-01T00:03:00Z", "1.18"),
        ]
        result = simulate_reserve_gridbot_on_candles(
            candles,
            _config(
                levels_per_band=20,
                band_active_lot_budget_cap="100",
                quote_start="1000",
                price_increment="0.01",
                fee_rate="0",
                overflow_global_active_lot_budget_cap="5",
            ),
        )
        summary = result["summary"]

        self.assertEqual(summary["overflow_filled_buys"], 2)
        self.assertEqual(summary["overflow_completed_lots"], 1)
        self.assertEqual(summary["overflow_end_open_unrecovered_lots"], 1)
        self.assertEqual(summary["missed_buys_overflow_global_cap"], 1)
        self.assertEqual(summary["maximum_overflow_active_cash_cost"], "4.9999992")
        self.assertEqual(summary["final_overflow_active_cash_cost"], "4.9999992")
        self.assertEqual(summary["overflow_cap_excess_error"], "0")

    def test_overflow_preserves_base_trade_fingerprint_and_reconciles(self) -> None:
        candles = [
            _candle(),
            _candle(
                ts="2025-01-01T00:01:00Z",
                open_price="1.10",
                high="1.18",
                low="1.02",
                close="1.12",
            ),
        ]
        fixed_config = _config(quote_start="1000")
        overflow_config = replace(
            fixed_config,
            overflow_global_active_lot_budget_cap="100",
        )
        fixed = simulate_reserve_gridbot_on_candles(candles, fixed_config)
        overflow = simulate_reserve_gridbot_on_candles(candles, overflow_config)
        repeated = simulate_reserve_gridbot_on_candles(candles, overflow_config)

        self.assertEqual(overflow, repeated)
        self.assertEqual(
            fixed["summary"]["base_trade_decision_fingerprint_sha256"],
            overflow["summary"]["base_trade_decision_fingerprint_sha256"],
        )
        self.assertEqual(
            fixed["summary"]["base_filled_buys"],
            overflow["summary"]["base_filled_buys"],
        )
        for key, value in overflow["summary"].items():
            if key.endswith("_reconciliation_error"):
                self.assertEqual(Decimal(str(value)), Decimal("0"), key)
        self.assertEqual(overflow["summary"]["overflow_cap_excess_error"], "0")
        self.assertEqual(overflow["summary"]["base_band_cap_excess_error"], "0")


if __name__ == "__main__":
    unittest.main()
