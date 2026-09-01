from __future__ import annotations

import unittest
from decimal import Decimal

from paper_bot.books import InvalidBook, OrderBook
from paper_bot.domain import BookLevel


class OrderBookTests(unittest.TestCase):
    def assertLevels(self, actual: tuple[BookLevel, ...], expected: tuple[BookLevel, ...]) -> None:
        self.assertEqual(actual, expected)

    def test_constructor_does_not_allow_bypassing_snapshot_lifecycle(self):
        with self.assertRaises(TypeError):
            OrderBook(_bids=(BookLevel(Decimal("0.89"), Decimal("1")),), _asks=(), _valid=True)

    def test_snapshot_sorts_normalizes_and_sets_generation(self):
        book = OrderBook()

        generation = book.apply_snapshot(
            bids=(
                BookLevel(Decimal("0.89"), Decimal("1")),
                BookLevel(Decimal("0.91"), Decimal("2")),
                BookLevel(Decimal("0.90"), Decimal("3")),
                BookLevel(Decimal("0.91"), Decimal("4")),
            ),
            asks=(
                BookLevel(Decimal("0.94"), Decimal("7")),
                BookLevel(Decimal("0.92"), Decimal("5")),
                BookLevel(Decimal("0.93"), Decimal("6")),
                BookLevel(Decimal("0.92"), Decimal("8")),
            ),
            event_ts_ms=1000,
            sequence=1,
        )

        self.assertEqual(generation, 1)
        self.assertEqual(book.generation, 1)
        self.assertTrue(book.valid)
        self.assertLevels(
            book.executable_bids(),
            (
                BookLevel(Decimal("0.91"), Decimal("4")),
                BookLevel(Decimal("0.90"), Decimal("3")),
                BookLevel(Decimal("0.89"), Decimal("1")),
            ),
        )
        self.assertLevels(
            book.executable_asks(),
            (
                BookLevel(Decimal("0.92"), Decimal("8")),
                BookLevel(Decimal("0.93"), Decimal("6")),
                BookLevel(Decimal("0.94"), Decimal("7")),
            ),
        )

    def test_snapshot_rejects_invalid_levels_without_mutation(self):
        book = OrderBook()
        book.apply_snapshot(
            bids=(BookLevel(Decimal("0.89"), Decimal("1")),),
            asks=(BookLevel(Decimal("0.92"), Decimal("1")),),
            event_ts_ms=1000,
            sequence=1,
        )

        with self.assertRaises(ValueError):
            book.apply_snapshot(
                bids=(BookLevel(Decimal("0.89"), Decimal("0")),),
                asks=(BookLevel(Decimal("0.92"), Decimal("1")),),
                event_ts_ms=1001,
                sequence=2,
            )

        self.assertEqual(book.generation, 1)
        self.assertTrue(book.valid)
        self.assertLevels(book.executable_bids(), (BookLevel(Decimal("0.89"), Decimal("1")),))
        self.assertLevels(book.executable_asks(), (BookLevel(Decimal("0.92"), Decimal("1")),))

    def test_snapshot_rejects_locked_or_crossed_books_without_mutation(self):
        book = OrderBook()
        book.apply_snapshot(
            bids=(BookLevel(Decimal("0.89"), Decimal("1")),),
            asks=(BookLevel(Decimal("0.92"), Decimal("1")),),
            event_ts_ms=1000,
            sequence=1,
        )

        with self.assertRaises(InvalidBook):
            book.apply_snapshot(
                bids=(BookLevel(Decimal("0.95"), Decimal("1")),),
                asks=(BookLevel(Decimal("0.94"), Decimal("1")),),
                event_ts_ms=1001,
                sequence=2,
            )

        self.assertEqual(book.generation, 1)
        self.assertTrue(book.valid)
        self.assertLevels(book.executable_bids(), (BookLevel(Decimal("0.89"), Decimal("1")),))
        self.assertLevels(book.executable_asks(), (BookLevel(Decimal("0.92"), Decimal("1")),))

    def test_delta_before_snapshot_and_while_invalid_is_ignored(self):
        book = OrderBook()

        self.assertFalse(book.apply_delta("bid", Decimal("0.90"), Decimal("1"), 1000, 1))
        self.assertEqual(book.generation, 0)
        self.assertFalse(book.valid)

        book.apply_snapshot(
            bids=(BookLevel(Decimal("0.89"), Decimal("1")),),
            asks=(BookLevel(Decimal("0.92"), Decimal("1")),),
            event_ts_ms=1000,
            sequence=1,
        )
        book.invalidate()

        self.assertFalse(book.apply_delta("ask", Decimal("0.93"), Decimal("1"), 1001, 2))
        self.assertEqual(book.generation, 1)
        self.assertFalse(book.valid)
        with self.assertRaises(InvalidBook):
            book.executable_bids()
        with self.assertRaises(InvalidBook):
            book.executable_asks()

    def test_invalidate_blocks_executable_views_and_new_snapshot_reconnects(self):
        book = OrderBook()
        book.apply_snapshot(
            bids=(BookLevel(Decimal("0.89"), Decimal("1")),),
            asks=(BookLevel(Decimal("0.92"), Decimal("1")),),
            event_ts_ms=1000,
            sequence=1,
        )

        book.invalidate()
        self.assertEqual(book.generation, 1)
        self.assertFalse(book.valid)
        with self.assertRaises(InvalidBook):
            book.executable_bids()

        generation = book.apply_snapshot(
            bids=(BookLevel(Decimal("0.90"), Decimal("2")),),
            asks=(BookLevel(Decimal("0.93"), Decimal("3")),),
            event_ts_ms=2000,
            sequence=2,
        )

        self.assertEqual(generation, 2)
        self.assertEqual(book.generation, 2)
        self.assertTrue(book.valid)
        self.assertLevels(book.executable_bids(), (BookLevel(Decimal("0.90"), Decimal("2")),))
        self.assertLevels(book.executable_asks(), (BookLevel(Decimal("0.93"), Decimal("3")),))

    def test_stale_snapshot_is_rejected_without_mutation(self):
        book = OrderBook()
        book.apply_snapshot(
            bids=(BookLevel(Decimal("0.89"), Decimal("1")),),
            asks=(BookLevel(Decimal("0.92"), Decimal("1")),),
            event_ts_ms=1000,
            sequence=10,
        )

        with self.assertRaises(InvalidBook):
            book.apply_snapshot(
                bids=(BookLevel(Decimal("0.90"), Decimal("2")),),
                asks=(BookLevel(Decimal("0.93"), Decimal("3")),),
                event_ts_ms=999,
                sequence=11,
            )

        self.assertEqual(book.generation, 1)
        self.assertTrue(book.valid)
        self.assertLevels(book.executable_bids(), (BookLevel(Decimal("0.89"), Decimal("1")),))
        self.assertLevels(book.executable_asks(), (BookLevel(Decimal("0.92"), Decimal("1")),))

    def test_stale_delta_precedence_rejects_invalid_payloads_without_mutation(self):
        book = OrderBook()
        book.apply_snapshot(
            bids=(BookLevel(Decimal("0.89"), Decimal("1")),),
            asks=(BookLevel(Decimal("0.92"), Decimal("1")),),
            event_ts_ms=1000,
            sequence=10,
        )

        baseline_bids = book.executable_bids()
        baseline_asks = book.executable_asks()

        cases = (
            ("hold", Decimal("0.89"), Decimal("2"), 999, 11),
            ("bid", Decimal("NaN"), Decimal("2"), 999, 11),
            ("bid", Decimal("1.1"), Decimal("2"), 999, 11),
            ("bid", Decimal("0.89"), Decimal("-1"), 999, 11),
            ("bid", Decimal("0.89"), Decimal("NaN"), 999, 11),
            ("bid", Decimal("0.95"), Decimal("2"), 999, 11),
        )
        for args in cases:
            with self.subTest(args=args):
                self.assertFalse(book.apply_delta(*args))
                self.assertLevels(book.executable_bids(), baseline_bids)
                self.assertLevels(book.executable_asks(), baseline_asks)
                self.assertEqual(book.generation, 1)

    def test_delta_rejects_stale_timestamp_and_sequence_but_accepts_same_timestamp_newer_sequence(self):
        book = OrderBook()
        book.apply_snapshot(
            bids=(BookLevel(Decimal("0.89"), Decimal("1")),),
            asks=(BookLevel(Decimal("0.92"), Decimal("1")),),
            event_ts_ms=1000,
            sequence=10,
        )

        self.assertFalse(book.apply_delta("bid", Decimal("0.89"), Decimal("2"), 999, 11))
        self.assertFalse(book.apply_delta("bid", Decimal("0.89"), Decimal("2"), 1000, 10))
        self.assertTrue(book.apply_delta("bid", Decimal("0.89"), Decimal("2"), 1000, 11))
        self.assertEqual(book.generation, 1)
        self.assertLevels(book.executable_bids(), (BookLevel(Decimal("0.89"), Decimal("2")),))

    def test_zero_size_delta_removes_level_and_state_remains_atomic_on_cross(self):
        book = OrderBook()
        book.apply_snapshot(
            bids=(BookLevel(Decimal("0.91"), Decimal("3")), BookLevel(Decimal("0.90"), Decimal("2"))),
            asks=(BookLevel(Decimal("0.93"), Decimal("4")), BookLevel(Decimal("0.94"), Decimal("5"))),
            event_ts_ms=1000,
            sequence=1,
        )

        self.assertTrue(book.apply_delta("bid", Decimal("0.91"), Decimal("0"), 1001, 2))
        self.assertLevels(book.executable_bids(), (BookLevel(Decimal("0.90"), Decimal("2")),))
        self.assertEqual(book.generation, 1)

        with self.assertRaises(InvalidBook):
            book.apply_delta("bid", Decimal("0.95"), Decimal("1"), 1002, 3)

        self.assertLevels(book.executable_bids(), (BookLevel(Decimal("0.90"), Decimal("2")),))
        self.assertLevels(
            book.executable_asks(),
            (
                BookLevel(Decimal("0.93"), Decimal("4")),
                BookLevel(Decimal("0.94"), Decimal("5")),
            ),
        )
        self.assertEqual(book.generation, 1)

    def test_delta_invalid_inputs_raise_and_do_not_mutate(self):
        book = OrderBook()
        book.apply_snapshot(
            bids=(BookLevel(Decimal("0.89"), Decimal("1")),),
            asks=(BookLevel(Decimal("0.92"), Decimal("1")),),
            event_ts_ms=1000,
            sequence=1,
        )

        baseline_bids = book.executable_bids()
        baseline_asks = book.executable_asks()

        for args in (
            ("hold", Decimal("0.90"), Decimal("1"), 1001, 2),
            ("bid", Decimal("0"), Decimal("1"), 1001, 2),
            ("ask", Decimal("0.90"), Decimal("-1"), 1001, 2),
            ("ask", Decimal("0.90"), Decimal("1"), -1, 2),
            ("ask", Decimal("0.90"), Decimal("1"), 1001, -1),
        ):
            with self.subTest(args=args):
                with self.assertRaises((InvalidBook, ValueError)):
                    book.apply_delta(*args)
                self.assertLevels(book.executable_bids(), baseline_bids)
                self.assertLevels(book.executable_asks(), baseline_asks)
                self.assertEqual(book.generation, 1)

    def test_executable_views_are_cloned_and_books_are_independent(self):
        book_one = OrderBook()
        book_two = OrderBook()

        book_one.apply_snapshot(
            bids=(BookLevel(Decimal("0.89"), Decimal("1")),),
            asks=(BookLevel(Decimal("0.92"), Decimal("1")),),
            event_ts_ms=1000,
            sequence=1,
        )
        first_bids = book_one.executable_bids()
        second_bids = book_one.executable_bids()
        first_asks = book_one.executable_asks()
        second_asks = book_one.executable_asks()

        self.assertIsNot(first_bids, second_bids)
        self.assertIsNot(first_asks, second_asks)
        self.assertIsNot(first_bids[0], second_bids[0])
        self.assertIsNot(first_asks[0], second_asks[0])

        with self.assertRaises(InvalidBook):
            book_two.executable_bids()

        book_two.apply_snapshot(
            bids=(BookLevel(Decimal("0.90"), Decimal("2")),),
            asks=(BookLevel(Decimal("0.93"), Decimal("3")),),
            event_ts_ms=1000,
            sequence=1,
        )

        self.assertLevels(book_one.executable_bids(), (BookLevel(Decimal("0.89"), Decimal("1")),))
        self.assertLevels(book_two.executable_bids(), (BookLevel(Decimal("0.90"), Decimal("2")),))
        self.assertLevels(book_one.executable_asks(), (BookLevel(Decimal("0.92"), Decimal("1")),))
        self.assertLevels(book_two.executable_asks(), (BookLevel(Decimal("0.93"), Decimal("3")),))
