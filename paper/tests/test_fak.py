from __future__ import annotations

import unittest
from decimal import Decimal, ROUND_HALF_UP, getcontext, localcontext

from paper_bot.fak import (
    BookLevel,
    FeeSchedule,
    SUPPORTED_TICK_PROFILES,
    buy_maker_amount_for_target_shares,
    quote_for_target_shares,
    simulate_buy_fak,
    simulate_sell_fak,
)


class FakSimulationTests(unittest.TestCase):
    def assertSixPlaces(self, value: Decimal) -> None:
        self.assertLessEqual(-value.as_tuple().exponent, 6)

    def test_buy_spans_multiple_asks_with_price_improvement_and_full_status(self):
        asks = (
            BookLevel(Decimal("0.89"), Decimal("3")),
            BookLevel(Decimal("0.90"), Decimal("5")),
        )

        result = simulate_buy_fak(
            asks=asks,
            requested_usdc=Decimal("5.00"),
            max_price=Decimal("0.90"),
            tick_size=Decimal("0.01"),
            min_order_shares=Decimal("1"),
            fee_schedule=FeeSchedule(Decimal("0"), Decimal("1")),
        )

        self.assertEqual(result.requested_quote, Decimal("5.000000"))
        self.assertIsNone(result.requested_shares)
        self.assertEqual(result.submitted_maker_amount, Decimal("5.000000"))
        self.assertEqual(result.submitted_taker_amount, Decimal("5.555500"))
        self.assertEqual(result.filled_shares, Decimal("5.555500"))
        self.assertEqual(result.quote_amount, Decimal("4.969950"))
        self.assertEqual(result.unfilled_quote, Decimal("0.030050"))
        self.assertEqual(result.unfilled_shares, Decimal("0.000000"))
        self.assertEqual(result.status, "full")
        self.assertEqual(len(result.legs), 2)
        self.assertEqual(result.legs[0].price, Decimal("0.89"))
        self.assertEqual(result.legs[0].shares, Decimal("3.000000"))
        self.assertEqual(result.legs[0].quote, Decimal("2.670000"))
        self.assertEqual(result.legs[1].price, Decimal("0.90"))
        self.assertEqual(result.legs[1].shares, Decimal("2.555500"))
        self.assertEqual(result.legs[1].quote, Decimal("2.299950"))
        self.assertEqual(sum(leg.shares for leg in result.legs), result.filled_shares)
        self.assertEqual(sum(leg.quote for leg in result.legs), result.quote_amount)

    def test_buy_ignores_asks_above_max_price_and_cancels_remaining_quote(self):
        asks = (
            BookLevel(Decimal("0.89"), Decimal("1")),
            BookLevel(Decimal("0.91"), Decimal("10")),
        )

        result = simulate_buy_fak(
            asks=asks,
            requested_usdc=Decimal("5.00"),
            max_price=Decimal("0.90"),
            tick_size=Decimal("0.01"),
            min_order_shares=Decimal("1"),
            fee_schedule=FeeSchedule(Decimal("0"), Decimal("1")),
        )

        self.assertEqual(result.filled_shares, Decimal("1.000000"))
        self.assertEqual(result.quote_amount, Decimal("0.890000"))
        self.assertEqual(result.unfilled_quote, Decimal("4.110000"))
        self.assertEqual(len(result.legs), 1)
        self.assertEqual(result.status, "partial")

    def test_buy_with_no_eligible_ask_returns_zero_fill(self):
        asks = (BookLevel(Decimal("0.91"), Decimal("10")),)

        result = simulate_buy_fak(
            asks=asks,
            requested_usdc=Decimal("5.00"),
            max_price=Decimal("0.90"),
            tick_size=Decimal("0.01"),
            min_order_shares=Decimal("1"),
            fee_schedule=FeeSchedule(Decimal("0"), Decimal("1")),
        )

        self.assertEqual(result.filled_shares, Decimal("0.000000"))
        self.assertEqual(result.quote_amount, Decimal("0.000000"))
        self.assertEqual(result.unfilled_quote, Decimal("5.000000"))
        self.assertEqual(result.legs, ())
        self.assertEqual(result.status, "zero")

    def test_official_tick_rounding_table_buy_taker_precision(self):
        self.assertEqual(
            SUPPORTED_TICK_PROFILES,
            {
                Decimal("0.1"): (1, 3),
                Decimal("0.01"): (2, 4),
                Decimal("0.001"): (3, 5),
                Decimal("0.0001"): (4, 6),
            },
        )

        cases = (
            (Decimal("0.1"), Decimal("3.333000")),
            (Decimal("0.01"), Decimal("3.333300")),
            (Decimal("0.001"), Decimal("3.333330")),
            (Decimal("0.0001"), Decimal("3.333333")),
        )
        for tick_size, expected_taker in cases:
            with self.subTest(tick_size=tick_size):
                result = simulate_buy_fak(
                    asks=(BookLevel(Decimal("0.30"), Decimal("20")),),
                    requested_usdc=Decimal("1.00"),
                    max_price=Decimal("0.30"),
                    tick_size=tick_size,
                    min_order_shares=Decimal("0.01"),
                    fee_schedule=FeeSchedule(Decimal("0"), Decimal("1")),
                )
                self.assertEqual(result.submitted_maker_amount, Decimal("1.000000"))
                self.assertEqual(result.submitted_taker_amount, expected_taker)
                self.assertEqual(result.filled_shares, expected_taker)

    def test_buy_direct_amount_rounds_down_to_two_decimals(self):
        result = simulate_buy_fak(
            asks=(BookLevel(Decimal("0.30"), Decimal("20")),),
            requested_usdc=Decimal("1.239000"),
            max_price=Decimal("0.30"),
            tick_size=Decimal("0.01"),
            min_order_shares=Decimal("0.01"),
            fee_schedule=FeeSchedule(Decimal("0"), Decimal("1")),
        )

        self.assertEqual(result.requested_quote, Decimal("1.239000"))
        self.assertEqual(result.submitted_maker_amount, Decimal("1.230000"))

    def test_buy_price_improvement_never_increases_fills_beyond_submitted_target(self):
        asks = (
            BookLevel(Decimal("0.89"), Decimal("3")),
            BookLevel(Decimal("0.90"), Decimal("5")),
        )

        result = simulate_buy_fak(
            asks=asks,
            requested_usdc=Decimal("5.00"),
            max_price=Decimal("0.90"),
            tick_size=Decimal("0.01"),
            min_order_shares=Decimal("1"),
            fee_schedule=FeeSchedule(Decimal("0"), Decimal("1")),
        )

        self.assertEqual(result.filled_shares, result.submitted_taker_amount)
        self.assertLess(result.quote_amount, result.submitted_maker_amount)

    def test_unsupported_tick_size_is_rejected(self):
        with self.assertRaises(ValueError):
            simulate_buy_fak(
                asks=(BookLevel(Decimal("0.89"), Decimal("1")),),
                requested_usdc=Decimal("5.00"),
                max_price=Decimal("0.90"),
                tick_size=Decimal("0.02"),
                min_order_shares=Decimal("1"),
                fee_schedule=FeeSchedule(Decimal("0"), Decimal("1")),
            )

        with self.assertRaises(ValueError):
            buy_maker_amount_for_target_shares(Decimal("1"), Decimal("0.90"), Decimal("0.02"))

    def test_buy_of_089_at_090_is_rejected_before_walking_book_when_minimum_is_one_share(self):
        result = simulate_buy_fak(
            asks=(BookLevel(Decimal("0.89"), Decimal("10")),),
            requested_usdc=Decimal("0.89"),
            max_price=Decimal("0.90"),
            tick_size=Decimal("0.01"),
            min_order_shares=Decimal("1"),
            fee_schedule=FeeSchedule(Decimal("0"), Decimal("1")),
        )

        self.assertEqual(result.submitted_maker_amount, Decimal("0.890000"))
        self.assertEqual(result.submitted_taker_amount, Decimal("0.988800"))
        self.assertEqual(result.status, "zero")
        self.assertEqual(result.filled_shares, Decimal("0.000000"))
        self.assertEqual(result.quote_amount, Decimal("0.000000"))

    def test_sell_rounds_submission_down_to_two_decimals_and_preserves_dust(self):
        bids = (
            BookLevel(Decimal("0.90"), Decimal("10")),
            BookLevel(Decimal("0.89"), Decimal("10")),
        )

        result = simulate_sell_fak(
            bids=bids,
            requested_shares=Decimal("5.555555"),
            min_price=Decimal("0.89"),
            tick_size=Decimal("0.01"),
            min_order_shares=Decimal("1"),
            fee_schedule=FeeSchedule(Decimal("0"), Decimal("1")),
        )

        self.assertEqual(result.requested_shares, Decimal("5.555555"))
        self.assertEqual(result.submitted_maker_amount, Decimal("5.550000"))
        self.assertEqual(result.submitted_taker_amount, Decimal("4.939500"))
        self.assertEqual(result.filled_shares, Decimal("5.550000"))
        self.assertEqual(result.quote_amount, Decimal("4.995000"))
        self.assertEqual(result.unfilled_shares, Decimal("0.005555"))
        self.assertEqual(result.status, "full")
        self.assertEqual(sum(leg.shares for leg in result.legs), result.filled_shares)
        self.assertEqual(sum(leg.quote for leg in result.legs), result.quote_amount)

    def test_sell_retains_submission_dust_and_fak_unfilled_shares(self):
        bids = (
            BookLevel(Decimal("0.91"), Decimal("1")),
            BookLevel(Decimal("0.90"), Decimal("1")),
        )

        result = simulate_sell_fak(
            bids=bids,
            requested_shares=Decimal("5.555555"),
            min_price=Decimal("0.89"),
            tick_size=Decimal("0.01"),
            min_order_shares=Decimal("1"),
            fee_schedule=FeeSchedule(Decimal("0"), Decimal("1")),
        )

        self.assertEqual(result.submitted_maker_amount, Decimal("5.550000"))
        self.assertEqual(result.filled_shares, Decimal("2.000000"))
        self.assertEqual(result.unfilled_shares, Decimal("3.555555"))
        self.assertEqual(result.status, "partial")
        self.assertEqual(sum(leg.shares for leg in result.legs), result.filled_shares)
        self.assertEqual(sum(leg.quote for leg in result.legs), result.quote_amount)

    def test_sell_walks_bids_high_to_low_down_to_minimum_price(self):
        bids = (
            BookLevel(Decimal("0.91"), Decimal("2")),
            BookLevel(Decimal("0.90"), Decimal("3")),
            BookLevel(Decimal("0.89"), Decimal("4")),
            BookLevel(Decimal("0.88"), Decimal("5")),
        )

        result = simulate_sell_fak(
            bids=bids,
            requested_shares=Decimal("4.00"),
            min_price=Decimal("0.89"),
            tick_size=Decimal("0.01"),
            min_order_shares=Decimal("1"),
            fee_schedule=FeeSchedule(Decimal("0"), Decimal("1")),
        )

        self.assertEqual(result.filled_shares, Decimal("4.000000"))
        self.assertEqual(result.quote_amount, Decimal("3.620000"))
        self.assertEqual(result.status, "full")
        self.assertEqual(len(result.legs), 2)
        self.assertEqual(result.legs[0].price, Decimal("0.91"))
        self.assertEqual(result.legs[1].price, Decimal("0.90"))
        self.assertEqual(sum(leg.shares for leg in result.legs), result.filled_shares)
        self.assertEqual(sum(leg.quote for leg in result.legs), result.quote_amount)

    def test_prices_not_aligned_to_tick_are_rejected(self):
        with self.assertRaises(ValueError):
            simulate_buy_fak(
                asks=(BookLevel(Decimal("0.891"), Decimal("1")),),
                requested_usdc=Decimal("5.00"),
                max_price=Decimal("0.90"),
                tick_size=Decimal("0.01"),
                min_order_shares=Decimal("1"),
                fee_schedule=FeeSchedule(Decimal("0"), Decimal("1")),
            )

    def test_buy_rejects_misaligned_max_price_before_quantize(self):
        with self.assertRaises(ValueError):
            simulate_buy_fak(
                asks=(BookLevel(Decimal("0.89"), Decimal("1")),),
                requested_usdc=Decimal("5.00"),
                max_price=Decimal("0.9000004"),
                tick_size=Decimal("0.01"),
                min_order_shares=Decimal("1"),
                fee_schedule=FeeSchedule(Decimal("0"), Decimal("1")),
            )

    def test_sell_rejects_misaligned_min_price_before_quantize(self):
        with self.assertRaises(ValueError):
            simulate_sell_fak(
                bids=(BookLevel(Decimal("0.91"), Decimal("1")),),
                requested_shares=Decimal("1.00"),
                min_price=Decimal("0.8900004"),
                tick_size=Decimal("0.01"),
                min_order_shares=Decimal("1"),
                fee_schedule=FeeSchedule(Decimal("0"), Decimal("1")),
            )

    def test_quote_helper_uses_actual_book_spend_only(self):
        asks = (
            BookLevel(Decimal("0.89"), Decimal("3")),
            BookLevel(Decimal("0.90"), Decimal("5")),
        )

        self.assertEqual(quote_for_target_shares(asks, Decimal("4"), Decimal("0.90")), Decimal("3.570000"))

    def test_quote_helper_skips_later_misaligned_price_after_early_fill(self):
        asks = (
            BookLevel(Decimal("0.50"), Decimal("1")),
            BookLevel(Decimal("0.5000001"), Decimal("10")),
        )

        with self.assertRaises(ValueError):
            quote_for_target_shares(asks, Decimal("1"), Decimal("0.50"))

    def test_quote_helper_rejects_negative_target_and_zero_max_price(self):
        asks = (BookLevel(Decimal("0.89"), Decimal("1")),)

        with self.assertRaises(ValueError):
            quote_for_target_shares(asks, Decimal("-1"), Decimal("0.90"))

        with self.assertRaises(ValueError):
            quote_for_target_shares(asks, Decimal("1"), Decimal("0"))

    def test_buy_maker_amount_for_target_shares_returns_largest_two_decimal_maker(self):
        cases = (
            (Decimal("0.1"), Decimal("3.333331")),
            (Decimal("0.01"), Decimal("3.333331")),
            (Decimal("0.001"), Decimal("3.333331")),
            (Decimal("0.0001"), Decimal("3.333331")),
        )
        for tick_size, target in cases:
            with self.subTest(tick_size=tick_size):
                maker = buy_maker_amount_for_target_shares(target, Decimal("0.30"), tick_size)
                self.assertEqual(maker.as_tuple().exponent, -2)
                self.assertGreater(maker, Decimal("0"))

                direct = simulate_buy_fak(
                    asks=(BookLevel(Decimal("0.30"), Decimal("20")),),
                    requested_usdc=maker,
                    max_price=Decimal("0.30"),
                    tick_size=tick_size,
                    min_order_shares=Decimal("0.01"),
                    fee_schedule=FeeSchedule(Decimal("0"), Decimal("1")),
                )
                self.assertLessEqual(direct.submitted_taker_amount, target)

                next_cent = simulate_buy_fak(
                    asks=(BookLevel(Decimal("0.30"), Decimal("20")),),
                    requested_usdc=maker + Decimal("0.01"),
                    max_price=Decimal("0.30"),
                    tick_size=tick_size,
                    min_order_shares=Decimal("0.01"),
                    fee_schedule=FeeSchedule(Decimal("0"), Decimal("1")),
                )
                self.assertGreater(next_cent.submitted_taker_amount, target)

    def test_subatomic_min_order_shares_is_rejected(self):
        asks = (BookLevel(Decimal("0.89"), Decimal("1")),)
        bids = (BookLevel(Decimal("0.91"), Decimal("1")),)

        with self.assertRaises(ValueError):
            simulate_buy_fak(
                asks=asks,
                requested_usdc=Decimal("5.00"),
                max_price=Decimal("0.90"),
                tick_size=Decimal("0.01"),
                min_order_shares=Decimal("0.0000004"),
                fee_schedule=FeeSchedule(Decimal("0"), Decimal("1")),
            )

        with self.assertRaises(ValueError):
            simulate_sell_fak(
                bids=bids,
                requested_shares=Decimal("1.00"),
                min_price=Decimal("0.89"),
                tick_size=Decimal("0.01"),
                min_order_shares=Decimal("0.0000004"),
                fee_schedule=FeeSchedule(Decimal("0"), Decimal("1")),
            )

    def test_buy_rejects_negative_or_non_finite_prices_and_negative_shares(self):
        with self.assertRaises(ValueError):
            simulate_buy_fak(
                asks=(BookLevel(Decimal("0.89"), Decimal("1")),),
                requested_usdc=Decimal("5.00"),
                max_price=Decimal("-0.90"),
                tick_size=Decimal("0.01"),
                min_order_shares=Decimal("1"),
                fee_schedule=FeeSchedule(Decimal("0"), Decimal("1")),
            )

        with self.assertRaises(ValueError):
            simulate_buy_fak(
                asks=(BookLevel(Decimal("NaN"), Decimal("1")),),
                requested_usdc=Decimal("5.00"),
                max_price=Decimal("0.90"),
                tick_size=Decimal("0.01"),
                min_order_shares=Decimal("1"),
                fee_schedule=FeeSchedule(Decimal("0"), Decimal("1")),
            )

        with self.assertRaises(ValueError):
            simulate_buy_fak(
                asks=(BookLevel(Decimal("0.89"), Decimal("-1")),),
                requested_usdc=Decimal("5.00"),
                max_price=Decimal("0.90"),
                tick_size=Decimal("0.01"),
                min_order_shares=Decimal("1"),
                fee_schedule=FeeSchedule(Decimal("0"), Decimal("1")),
            )

    def test_sell_rejects_negative_or_non_finite_prices_and_negative_shares(self):
        with self.assertRaises(ValueError):
            simulate_sell_fak(
                bids=(BookLevel(Decimal("0.91"), Decimal("1")),),
                requested_shares=Decimal("1.00"),
                min_price=Decimal("-0.89"),
                tick_size=Decimal("0.01"),
                min_order_shares=Decimal("1"),
                fee_schedule=FeeSchedule(Decimal("0"), Decimal("1")),
            )

        with self.assertRaises(ValueError):
            simulate_sell_fak(
                bids=(BookLevel(Decimal("0.91"), Decimal("1")),),
                requested_shares=Decimal("1.00"),
                min_price=Decimal("NaN"),
                tick_size=Decimal("0.01"),
                min_order_shares=Decimal("1"),
                fee_schedule=FeeSchedule(Decimal("0"), Decimal("1")),
            )

        with self.assertRaises(ValueError):
            simulate_sell_fak(
                bids=(BookLevel(Decimal("0.91"), Decimal("-1")),),
                requested_shares=Decimal("1.00"),
                min_price=Decimal("0.89"),
                tick_size=Decimal("0.01"),
                min_order_shares=Decimal("1"),
                fee_schedule=FeeSchedule(Decimal("0"), Decimal("1")),
            )

    def test_prices_above_one_are_rejected_before_fee_calculation(self):
        with self.assertRaises(ValueError):
            simulate_buy_fak(
                asks=(BookLevel(Decimal("1.10"), Decimal("1")),),
                requested_usdc=Decimal("5.00"),
                max_price=Decimal("1.10"),
                tick_size=Decimal("0.01"),
                min_order_shares=Decimal("1"),
                fee_schedule=FeeSchedule(Decimal("1"), Decimal("1")),
            )

        with self.assertRaises(ValueError):
            simulate_sell_fak(
                bids=(BookLevel(Decimal("1.10"), Decimal("1")),),
                requested_shares=Decimal("1.00"),
                min_price=Decimal("1.10"),
                tick_size=Decimal("0.01"),
                min_order_shares=Decimal("1"),
                fee_schedule=FeeSchedule(Decimal("1"), Decimal("1")),
            )

    def test_fee_schedule_rejects_negative_and_non_finite_values(self):
        with self.assertRaises(ValueError):
            simulate_buy_fak(
                asks=(BookLevel(Decimal("0.89"), Decimal("1")),),
                requested_usdc=Decimal("5.00"),
                max_price=Decimal("0.90"),
                tick_size=Decimal("0.01"),
                min_order_shares=Decimal("1"),
                fee_schedule=FeeSchedule(Decimal("-0.01"), Decimal("1")),
            )

        with self.assertRaises(ValueError):
            simulate_sell_fak(
                bids=(BookLevel(Decimal("0.91"), Decimal("1")),),
                requested_shares=Decimal("1.00"),
                min_price=Decimal("0.89"),
                tick_size=Decimal("0.01"),
                min_order_shares=Decimal("1"),
                fee_schedule=FeeSchedule(Decimal("0.01"), Decimal("NaN")),
            )

        with self.assertRaises(ValueError):
            simulate_buy_fak(
                asks=(BookLevel(Decimal("0.89"), Decimal("1")),),
                requested_usdc=Decimal("5.00"),
                max_price=Decimal("0.90"),
                tick_size=Decimal("0.01"),
                min_order_shares=Decimal("1"),
                fee_schedule=FeeSchedule(Decimal("0.01"), Decimal("-1")),
            )

    def test_fee_is_calculated_per_actual_level_fill_not_vwap(self):
        asks = (
            BookLevel(Decimal("0.80"), Decimal("1")),
            BookLevel(Decimal("0.90"), Decimal("1")),
        )

        result = simulate_buy_fak(
            asks=asks,
            requested_usdc=Decimal("1.70"),
            max_price=Decimal("0.90"),
            tick_size=Decimal("0.01"),
            min_order_shares=Decimal("1"),
            fee_schedule=FeeSchedule(Decimal("1"), Decimal("1")),
        )

        self.assertEqual(result.filled_shares, Decimal("1.888800"))
        self.assertEqual(result.quote_amount, Decimal("1.599920"))
        self.assertEqual(result.fee, Decimal("0.239992"))
        self.assertEqual(result.legs[0].fee, Decimal("0.160000"))
        self.assertEqual(result.legs[1].fee, Decimal("0.079992"))
        self.assertNotEqual(result.fee, Decimal("0.255000"))

    def test_every_persisted_amount_has_at_most_six_decimal_places(self):
        result = simulate_buy_fak(
            asks=(BookLevel(Decimal("0.89"), Decimal("3")), BookLevel(Decimal("0.90"), Decimal("5"))),
            requested_usdc=Decimal("5.00"),
            max_price=Decimal("0.90"),
            tick_size=Decimal("0.01"),
            min_order_shares=Decimal("1"),
            fee_schedule=FeeSchedule(Decimal("0.01"), Decimal("1")),
        )

        amounts = [
            result.requested_quote,
            result.requested_shares,
            result.submitted_maker_amount,
            result.submitted_taker_amount,
            result.filled_shares,
            result.quote_amount,
            result.unfilled_quote,
            result.unfilled_shares,
            result.fee,
        ]
        for leg in result.legs:
            amounts.extend([leg.price, leg.shares, leg.quote, leg.fee])

        for amount in amounts:
            if amount is None:
                continue
            self.assertSixPlaces(amount)

    def test_public_fak_results_are_independent_of_decimal_context(self):
        asks = (
            BookLevel(Decimal("0.89"), Decimal("3")),
            BookLevel(Decimal("0.90"), Decimal("5")),
        )
        bids = (
            BookLevel(Decimal("0.91"), Decimal("2")),
            BookLevel(Decimal("0.90"), Decimal("3")),
        )
        expected_buy = simulate_buy_fak(
            asks=asks,
            requested_usdc=Decimal("5.00"),
            max_price=Decimal("0.90"),
            tick_size=Decimal("0.01"),
            min_order_shares=Decimal("1"),
            fee_schedule=FeeSchedule(Decimal("0.01"), Decimal("1")),
        )
        expected_sell = simulate_sell_fak(
            bids=bids,
            requested_shares=Decimal("4.00"),
            min_price=Decimal("0.89"),
            tick_size=Decimal("0.01"),
            min_order_shares=Decimal("1"),
            fee_schedule=FeeSchedule(Decimal("0.01"), Decimal("1")),
        )
        expected_quote = quote_for_target_shares(asks, Decimal("4"), Decimal("0.90"))

        original_context = getcontext().copy()
        try:
            with localcontext() as ctx:
                ctx.prec = 6
                ctx.rounding = ROUND_HALF_UP
                self.assertEqual(
                    simulate_buy_fak(
                        asks=asks,
                        requested_usdc=Decimal("5.00"),
                        max_price=Decimal("0.90"),
                        tick_size=Decimal("0.01"),
                        min_order_shares=Decimal("1"),
                        fee_schedule=FeeSchedule(Decimal("0.01"), Decimal("1")),
                    ),
                    expected_buy,
                )
                self.assertEqual(
                    simulate_sell_fak(
                        bids=bids,
                        requested_shares=Decimal("4.00"),
                        min_price=Decimal("0.89"),
                        tick_size=Decimal("0.01"),
                        min_order_shares=Decimal("1"),
                        fee_schedule=FeeSchedule(Decimal("0.01"), Decimal("1")),
                    ),
                    expected_sell,
                )
                self.assertEqual(quote_for_target_shares(asks, Decimal("4"), Decimal("0.90")), expected_quote)
        finally:
            getcontext().prec = original_context.prec
            getcontext().rounding = original_context.rounding

    def test_input_books_are_not_mutated(self):
        asks = [BookLevel(Decimal("0.89"), Decimal("3")), BookLevel(Decimal("0.90"), Decimal("5"))]
        snapshot = tuple(asks)
        simulate_buy_fak(
            asks=asks,
            requested_usdc=Decimal("5.00"),
            max_price=Decimal("0.90"),
            tick_size=Decimal("0.01"),
            min_order_shares=Decimal("1"),
            fee_schedule=FeeSchedule(Decimal("0"), Decimal("1")),
        )
        self.assertEqual(tuple(asks), snapshot)


if __name__ == "__main__":
    unittest.main()
