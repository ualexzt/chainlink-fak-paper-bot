from __future__ import annotations

import unittest
from dataclasses import FrozenInstanceError, replace
from decimal import Decimal as D
from types import SimpleNamespace

from paper_bot.books import OrderBook
from paper_bot.domain import BookLevel, FakResult, FeeSchedule, InventoryLot
from paper_bot.gamma import MarketDefinition
from paper_bot.strategy import (
    Confirmation,
    LaneKey,
    MarketStrategyState,
    PositionPolicy,
    StrategyEvent,
)


class ResolverStub:
    def __init__(self, *, fresh=True, leader="DOWN", momentum=D("-1")):
        self.result = SimpleNamespace(fresh=fresh, leader=leader, momentum_5s_bps=momentum)

    def view(self, *args):
        return self.result


class ReverseTests(unittest.TestCase):
    config_hash = "0" * 64

    def setUp(self):
        self.market = MarketDefinition(
            symbol="btc", slug="btc-updown-5m-1000", market_id="market-1",
            mkt_ts=1_000, end_ts=1_300, up_token_id="up-token", down_token_id="down-token",
            tick_size=D("0.01"), min_order_shares=D("0.01"),
            fee_schedule=FeeSchedule(D("0"), D("1")),
        )
        self.books = {"up-token": OrderBook(), "down-token": OrderBook()}
        self._snapshot_books()
        self.lane = LaneKey(D("0.80"), Confirmation.BOOK_ONLY, PositionPolicy.IMMEDIATE_REVERSE)
        self.entry = StrategyEvent(
            lane=self.lane,
            kind="entry_attempt",
            market_id=self.market.market_id,
            mkt_ts=self.market.mkt_ts,
            token_id=self.market.up_token_id,
            side="UP",
            event_ts_ms=1_000_000,
            book_generation=self.books["up-token"].generation,
            config_hash=self.config_hash,
            fak=FakResult(
                requested_quote=D("5.000000"), requested_shares=None,
                submitted_maker_amount=D("5.000000"), submitted_taker_amount=D("5.555555"),
                filled_shares=D("5.555555"), quote_amount=D("4.444444"),
                unfilled_quote=D("0.555556"), unfilled_shares=D("0.000000"),
                fee=D("0.000000"), legs=(), status="full",
            ),
        )
        self.resolver = ResolverStub()

    def _snapshot_books(self, *, old_bid_shares=D("3"), opposite_ask=D("0.90"),
                        opposite_ask_shares=D("2.5"), sequence=1):
        self.books["up-token"].apply_snapshot(
            [BookLevel(D("0.79"), old_bid_shares)], [BookLevel(D("0.80"), D("10"))],
            sequence, sequence,
        )
        self.books["down-token"].apply_snapshot(
            [BookLevel(D("0.10"), D("10"))], [BookLevel(opposite_ask, opposite_ask_shares)],
            sequence, sequence,
        )

    def _state(self, clock_values=(1_000_000, 4_500_000)):
        values = iter(clock_values)
        return MarketStrategyState(
            thresholds=(D("0.80"),), config_hash=self.config_hash, clock_ns=lambda: next(values)
        )

    def _reverse(self, state=None, *, lane=None, entry=None, resolver=None, event_ts_ms=1_000_100):
        return (state or self._state()).on_reverse_event(
            lane or self.lane,
            entry or self.entry,
            self.market,
            self.books,
            resolver or self.resolver,
            event_ts_ms,
            1_200,
        )

    def test_partial_sell_and_buy_preserve_exact_inventory_and_telemetry(self):
        self.books["up-token"].apply_snapshot(
            [BookLevel(D("0.79"), D("1")), BookLevel(D("0.01"), D("2"))],
            [BookLevel(D("0.80"), D("10"))],
            2,
            2,
        )
        reverse = self._reverse()
        self.assertEqual(reverse.status, "COMPLETE")
        self.assertEqual(reverse.outcome, "PARTIAL_SELL_AND_BUY")
        self.assertEqual(reverse.config_hash, self.config_hash)
        self.assertEqual(reverse.sell.filled_shares, D("3.000000"))
        self.assertEqual(tuple(leg.price for leg in reverse.sell.legs), (D("0.790000"), D("0.010000")))
        self.assertEqual(reverse.buy.filled_shares, D("2.500000"))
        self.assertEqual(reverse.old_residual_shares, D("2.555555"))
        self.assertEqual(reverse.submission_dust_shares, D("0.005555"))
        self.assertEqual(reverse.expected_quote, D("2.250000"))
        self.assertEqual(reverse.buy.unfilled_quote, D("0.450000"))
        self.assertLessEqual(reverse.buy.submitted_taker_amount, reverse.sold_shares)
        self.assertEqual(reverse.buy.submitted_taker_amount, D("3.000000"))
        self.assertEqual(
            reverse.inventory_lots,
            (
                InventoryLot("up-token", "UP", D("2.555555"), "reverse_old_residual"),
                InventoryLot("down-token", "DOWN", D("2.500000"), "reverse_buy"),
            ),
        )
        self.assertEqual(reverse.sell_book_generation, 2)
        self.assertEqual(reverse.buy_book_generation, 1)
        self.assertEqual(reverse.leg_elapsed_ms, 3)
        self.assertEqual(
            reverse.transitions,
            ("ELIGIBLE", "SELL_ATTEMPTED", "SELL_FILLED_OR_PARTIAL", "BUY_ATTEMPTED", "COMPLETE"),
        )

    def test_price_improvement_never_buys_more_than_was_sold(self):
        self._snapshot_books(opposite_ask=D("0.89"), opposite_ask_shares=D("10"), sequence=2)
        reverse = self._reverse()
        self.assertEqual(reverse.expected_quote, D("2.670000"))
        self.assertEqual(reverse.buy.requested_quote, D("2.700000"))
        self.assertEqual(reverse.buy.filled_shares, reverse.buy.submitted_taker_amount)
        self.assertLessEqual(reverse.buy.filled_shares, reverse.sold_shares)
        self.assertEqual(reverse.buy.unfilled_quote, D("0.030000"))

    def test_hold_same_event_and_older_event_never_reverse(self):
        hold = replace(self.lane, policy=PositionPolicy.HOLD)
        hold_entry = replace(self.entry, lane=hold)
        self.assertIsNone(self._reverse(lane=hold, entry=hold_entry))
        state = self._state()
        self.assertIsNone(self._reverse(state, event_ts_ms=self.entry.event_ts_ms))
        self.assertIsNone(self._reverse(state, event_ts_ms=self.entry.event_ts_ms - 1))
        self.assertIsNotNone(self._reverse(state, event_ts_ms=self.entry.event_ts_ms + 1))

    def test_out_of_range_ask_can_return_and_trigger_once(self):
        self._snapshot_books(opposite_ask=D("0.95"), sequence=2)
        state = self._state()
        self.assertIsNone(self._reverse(state))
        self._snapshot_books(opposite_ask=D("0.89"), sequence=3)
        self.assertIsNotNone(self._reverse(state, event_ts_ms=1_000_200))
        self.assertIsNone(self._reverse(state, event_ts_ms=1_000_300))

    def test_chainlink_confirmation_can_arrive_while_book_remains_eligible(self):
        lane = replace(self.lane, policy=PositionPolicy.CHAINLINK_REVERSE)
        entry = replace(self.entry, lane=lane)
        state = self._state()
        stale = ResolverStub(fresh=False)
        self.assertIsNone(self._reverse(state, lane=lane, entry=entry, resolver=stale))
        blocked = (
            ResolverStub(fresh=True, leader="TIE", momentum=D("0")),
            ResolverStub(fresh=True, leader="UP", momentum=D("1")),
            ResolverStub(fresh=True, leader="DOWN", momentum=D("1")),
            ResolverStub(fresh=True, leader="DOWN", momentum=D("0")),
        )
        for index, resolver in enumerate(blocked, start=2):
            self.assertIsNone(
                self._reverse(
                    state, lane=lane, entry=entry, resolver=resolver,
                    event_ts_ms=1_000_000 + index * 100,
                )
            )
        confirmed = ResolverStub(fresh=True, leader="DOWN", momentum=D("-0.1"))
        self.assertIsNotNone(
            self._reverse(state, lane=lane, entry=entry, resolver=confirmed, event_ts_ms=1_000_700)
        )

    def test_zero_sell_is_terminal_without_buy_and_keeps_old_lot(self):
        self.books["up-token"].apply_snapshot(
            [], [BookLevel(D("0.80"), D("10"))], 2, 2
        )
        self.books["down-token"].apply_snapshot(
            [BookLevel(D("0.10"), D("10"))], [BookLevel(D("0.90"), D("2.5"))], 2, 2
        )
        state = self._state(clock_values=(1_000_000,))
        reverse = self._reverse(state)
        self.assertEqual(reverse.outcome, "ZERO_SELL")
        self.assertEqual(reverse.sold_shares, D("0.000000"))
        self.assertIsNone(reverse.buy)
        self.assertIsNone(reverse.buy_book_generation)
        self.assertIsNone(reverse.leg_elapsed_ms)
        self.assertEqual(
            reverse.inventory_lots,
            (InventoryLot("up-token", "UP", D("5.555555"), "reverse_old_residual"),),
        )
        self.assertIsNone(self._reverse(state, event_ts_ms=1_000_200))

    def test_zero_fill_entry_has_no_reverse_attempt(self):
        zero = replace(
            self.entry,
            fak=replace(
                self.entry.fak, filled_shares=D("0"), quote_amount=D("0"), status="zero"
            ),
        )
        self.assertIsNone(self._reverse(entry=zero))

    def test_invalid_books_wait_without_consuming_attempt(self):
        state = self._state()
        self.books["down-token"].invalidate()
        self.assertIsNone(self._reverse(state))
        self._snapshot_books(sequence=2)
        self.assertIsNotNone(self._reverse(state, event_ts_ms=1_000_200))

    def test_reverse_identity_mismatches_fail_closed(self):
        cases = (
            (replace(self.lane, confirmation=Confirmation.CHAINLINK_DIRECTION), self.entry),
            (self.lane, replace(self.entry, market_id="other")),
            (self.lane, replace(self.entry, token_id="other")),
            (self.lane, replace(self.entry, config_hash="1" * 64)),
            (self.lane, replace(self.entry, kind="not-entry")),
        )
        for lane, entry in cases:
            with self.subTest(lane=lane, entry=entry), self.assertRaises(ValueError):
                self._reverse(self._state(), lane=lane, entry=entry)

    def test_inventory_lots_are_validated_and_immutable(self):
        lot = InventoryLot("token", "UP", D("1"), "entry")
        with self.assertRaises(FrozenInstanceError):
            lot.shares = D("2")
        for args in (("", "UP", D("1"), "entry"), ("t", "SIDE", D("1"), "entry"),
                     ("t", "UP", D("0"), "entry"), ("t", "UP", 1.0, "entry"),
                     ("t", "UP", D("1"), "unknown")):
            with self.subTest(args=args), self.assertRaises(ValueError):
                InventoryLot(*args)


if __name__ == "__main__":
    unittest.main()
