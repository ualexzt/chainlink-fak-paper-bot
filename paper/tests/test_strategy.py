from __future__ import annotations

import unittest
from dataclasses import FrozenInstanceError
from decimal import Decimal as D
from types import SimpleNamespace

from paper_bot.books import OrderBook
from paper_bot.domain import BookLevel, FeeSchedule
from paper_bot.gamma import MarketDefinition
from paper_bot.strategy import (
    Confirmation,
    LaneKey,
    MarketStrategyState,
    PositionPolicy,
    all_lane_keys,
)


class ResolverStub:
    def __init__(self, *, fresh=False, leader=None, momentum=None):
        self.result = SimpleNamespace(fresh=fresh, leader=leader, momentum_5s_bps=momentum)
        self.calls = []

    def view(self, symbol, mkt_ts, now_ms):
        self.calls.append((symbol, mkt_ts, now_ms))
        return self.result


class StrategyTests(unittest.TestCase):
    def setUp(self):
        self.market = MarketDefinition(
            symbol="btc", slug="btc-updown-5m-1000", market_id="market-1",
            mkt_ts=1_000, end_ts=1_300, up_token_id="up-token", down_token_id="down-token",
            tick_size=D("0.01"), min_order_shares=D("0.01"),
            fee_schedule=FeeSchedule(D("0.1"), D("1")),
        )
        self.books = {"up-token": OrderBook(), "down-token": OrderBook()}
        self._snapshot("UP", D("0.79"), 1)
        self._snapshot("DOWN", D("0.79"), 1)
        self.stale = ResolverStub()

    def _book(self, side):
        return self.books["up-token" if side == "UP" else "down-token"]

    def _snapshot(self, side, ask, sequence):
        self._book(side).apply_snapshot(
            [BookLevel(D("0.40"), D("20"))], [BookLevel(ask, D("20"))], sequence, sequence
        )

    def _set_best_ask(self, side, old, new, sequence, shares=D("20")):
        book = self._book(side)
        book.apply_delta("ask", old, D("0"), sequence, sequence)
        book.apply_delta("ask", new, shares, sequence + 1, sequence + 1)

    def _baseline(self, state, resolver=None, now=1_200):
        self.assertEqual(
            state.on_book_event(self.market, self.books, resolver or self.stale, 1_200_000, now), ()
        )

    def test_fixed_matrix_is_unique_immutable_and_ordered(self):
        lanes = all_lane_keys()
        self.assertEqual(len(lanes), 36)
        self.assertEqual(len(set(lanes)), 36)
        self.assertEqual({lane.threshold for lane in lanes}, {D("0.80"), D("0.85"), D("0.89"), D("0.90")})
        self.assertEqual(lanes[0], LaneKey(D("0.80"), Confirmation.BOOK_ONLY, PositionPolicy.HOLD))
        with self.assertRaises(FrozenInstanceError):
            lanes[0].threshold = D("0.70")

    def test_threshold_and_identity_inputs_fail_closed(self):
        invalid_thresholds = ((), (D("0.80"), D("0.80")), (D("0.90"), D("0.80")),
                              (D("0"),), (D("NaN"),), (0.8,))
        for thresholds in invalid_thresholds:
            with self.subTest(thresholds=thresholds), self.assertRaises(ValueError):
                all_lane_keys(thresholds)
        for notional in (D("0"), D("NaN"), 5.0):
            with self.subTest(notional=notional), self.assertRaises(ValueError):
                MarketStrategyState(paper_notional_usd=notional)
        with self.assertRaises(ValueError):
            MarketStrategyState(config_hash="not-a-digest")

    def test_entry_window_uses_now_and_includes_only_one_through_150_seconds(self):
        for index, (now, eligible) in enumerate(((1_149, False), (1_150, True), (1_299, True), (1_300, False))):
            with self.subTest(now=now):
                self._snapshot("UP", D("0.79"), 10 + index * 10)
                self._snapshot("DOWN", D("0.79"), 10 + index * 10)
                state = MarketStrategyState(thresholds=(D("0.80"),))
                self._baseline(state, now=1_149)
                self._set_best_ask("UP", D("0.79"), D("0.80"), 12 + index * 10)
                events = state.on_book_event(self.market, self.books, self.stale, 99, now)
                self.assertEqual(bool(events), eligible)

    def test_invalid_timestamps_are_rejected_before_persistence(self):
        state = MarketStrategyState(thresholds=(D("0.80"),))
        for event_ts_ms, now_ts in ((-1, 1_200), (True, 1_200), (1, -1), (1, 1.5)):
            with self.subTest(event_ts_ms=event_ts_ms, now_ts=now_ts), self.assertRaises(ValueError):
                state.on_book_event(self.market, self.books, self.stale, event_ts_ms, now_ts)

    def test_continuous_cross_records_evidence_and_clones_one_fak(self):
        digest = "a" * 64
        state = MarketStrategyState(thresholds=(D("0.80"),), config_hash=digest)
        self._baseline(state)
        generation = self._book("UP").generation
        self._set_best_ask("UP", D("0.79"), D("0.80"), 2)
        events = state.on_book_event(self.market, self.books, self.stale, 1_234_567, 1_200)
        self.assertEqual(len(events), 3)
        self.assertEqual({event.lane.policy for event in events}, set(PositionPolicy))
        self.assertEqual({event.lane.confirmation for event in events}, {Confirmation.BOOK_ONLY})
        self.assertTrue(all(event.event_ts_ms == 1_234_567 for event in events))
        self.assertTrue(all(event.book_generation == generation for event in events))
        self.assertTrue(all(event.config_hash == digest for event in events))
        self.assertTrue(all(event.market_id == "market-1" for event in events))
        self.assertTrue(all(event.mkt_ts == 1_000 for event in events))
        self.assertTrue(all(event.token_id == "up-token" for event in events))
        self.assertTrue(all(event.fak is events[0].fak for event in events))
        self.assertTrue(events[0].fak.legs)
        self.assertEqual(events[0].fak.fee, sum((leg.fee for leg in events[0].fak.legs), D("0")))

    def test_chainlink_gates_and_resolver_time_contract(self):
        resolver = ResolverStub(fresh=True, leader="UP", momentum=D("1"))
        state = MarketStrategyState(thresholds=(D("0.80"),))
        self._baseline(state, resolver)
        self._set_best_ask("UP", D("0.79"), D("0.80"), 2)
        events = state.on_book_event(self.market, self.books, resolver, 1_200_100, 1_200)
        self.assertEqual(len(events), 9)
        self.assertEqual({event.lane.confirmation for event in events}, set(Confirmation))
        self.assertEqual(resolver.calls[-1], ("btc", 1_000, 1_200_000))
        for confirmation in Confirmation:
            results = [event.fak for event in events if event.lane.confirmation is confirmation]
            self.assertEqual(len(results), 3)
            self.assertTrue(all(result is results[0] for result in results))
        self.assertIsNot(events[0].fak, events[3].fak)

    def test_stale_tied_opposite_and_zero_momentum_block_dependent_lanes(self):
        cases = (
            (ResolverStub(fresh=False, leader="UP", momentum=D("1")), 3),
            (ResolverStub(fresh=True, leader="TIE", momentum=D("1")), 3),
            (ResolverStub(fresh=True, leader="DOWN", momentum=D("-1")), 3),
            (ResolverStub(fresh=True, leader="UP", momentum=D("0")), 6),
        )
        for index, (resolver, count) in enumerate(cases):
            with self.subTest(view=resolver.result):
                self._snapshot("UP", D("0.79"), 20 + index * 10)
                self._snapshot("DOWN", D("0.79"), 20 + index * 10)
                state = MarketStrategyState(thresholds=(D("0.80"),))
                self._baseline(state, resolver)
                self._set_best_ask("UP", D("0.79"), D("0.80"), 22 + index * 10)
                events = state.on_book_event(self.market, self.books, resolver, 1_200_100, 1_200)
                self.assertEqual(len(events), count)

    def test_down_confirmation_requires_negative_momentum(self):
        resolver = ResolverStub(fresh=True, leader="DOWN", momentum=D("-0.1"))
        state = MarketStrategyState(thresholds=(D("0.80"),))
        self._baseline(state, resolver)
        self._set_best_ask("DOWN", D("0.79"), D("0.80"), 2)
        events = state.on_book_event(self.market, self.books, resolver, 1_200_100, 1_200)
        self.assertEqual(len(events), 9)
        self.assertEqual({event.side for event in events}, {"DOWN"})

    def test_simultaneous_cross_is_ambiguous_and_updates_baseline(self):
        state = MarketStrategyState(thresholds=(D("0.80"),))
        self._baseline(state)
        self._set_best_ask("UP", D("0.79"), D("0.80"), 2)
        self._set_best_ask("DOWN", D("0.79"), D("0.80"), 2)
        self.assertEqual(state.on_book_event(self.market, self.books, self.stale, 1_200_100, 1_200), ())
        self.assertEqual(state.on_book_event(self.market, self.books, self.stale, 1_200_200, 1_200), ())

    def test_invalid_or_new_generation_breaks_continuity_until_next_event(self):
        state = MarketStrategyState(thresholds=(D("0.80"),))
        self._baseline(state)
        self._book("DOWN").invalidate()
        self._set_best_ask("UP", D("0.79"), D("0.80"), 2)
        self.assertEqual(state.on_book_event(self.market, self.books, self.stale, 1_200_100, 1_200), ())
        self._snapshot("DOWN", D("0.79"), 5)
        self.assertEqual(state.on_book_event(self.market, self.books, self.stale, 1_200_200, 1_200), ())
        self._set_best_ask("UP", D("0.80"), D("0.79"), 6)
        self.assertEqual(state.on_book_event(self.market, self.books, self.stale, 1_200_300, 1_200), ())
        self._set_best_ask("UP", D("0.79"), D("0.80"), 8)
        self.assertEqual(len(state.on_book_event(self.market, self.books, self.stale, 1_200_400, 1_200)), 3)

    def test_jump_above_limit_is_zero_and_never_retries(self):
        state = MarketStrategyState(thresholds=(D("0.80"),))
        self._baseline(state)
        self._set_best_ask("UP", D("0.79"), D("0.95"), 2)
        events = state.on_book_event(self.market, self.books, self.stale, 1_200_100, 1_200)
        self.assertEqual(len(events), 3)
        self.assertEqual({event.fak.status for event in events}, {"zero"})
        self._set_best_ask("UP", D("0.95"), D("0.79"), 4)
        state.on_book_event(self.market, self.books, self.stale, 1_200_200, 1_200)
        self._set_best_ask("UP", D("0.79"), D("0.80"), 6)
        self.assertEqual(state.on_book_event(self.market, self.books, self.stale, 1_200_300, 1_200), ())

    def test_partial_and_full_attempts_never_reenter(self):
        for index, (liquidity, status) in enumerate(((D("1"), "partial"), (D("20"), "full"))):
            with self.subTest(status=status):
                base = 20 + index * 20
                self._snapshot("UP", D("0.79"), base)
                self._snapshot("DOWN", D("0.79"), base)
                state = MarketStrategyState(thresholds=(D("0.80"),))
                self._baseline(state)
                self._set_best_ask("UP", D("0.79"), D("0.80"), base + 2, liquidity)
                events = state.on_book_event(self.market, self.books, self.stale, 1_200_100, 1_200)
                self.assertEqual(events[0].fak.status, status)
                self._set_best_ask("UP", D("0.80"), D("0.79"), base + 4)
                state.on_book_event(self.market, self.books, self.stale, 1_200_200, 1_200)
                self._set_best_ask("UP", D("0.79"), D("0.80"), base + 6)
                self.assertEqual(state.on_book_event(self.market, self.books, self.stale, 1_200_300, 1_200), ())

    def test_blocked_chainlink_cross_does_not_resurrect_without_new_cross(self):
        resolver = ResolverStub(fresh=False, leader="UP", momentum=D("1"))
        state = MarketStrategyState(thresholds=(D("0.80"),))
        self._baseline(state, resolver)
        self._set_best_ask("UP", D("0.79"), D("0.80"), 2)
        self.assertEqual(len(state.on_book_event(self.market, self.books, resolver, 1_200_100, 1_200)), 3)
        resolver.result = SimpleNamespace(fresh=True, leader="UP", momentum_5s_bps=D("1"))
        self.assertEqual(state.on_book_event(self.market, self.books, resolver, 1_200_200, 1_200), ())

    def test_state_is_bound_to_one_market(self):
        state = MarketStrategyState(thresholds=(D("0.80"),))
        self._baseline(state)
        other_market = MarketDefinition(
            symbol=self.market.symbol,
            slug="btc-updown-5m-1300",
            market_id="market-2",
            mkt_ts=1_300,
            end_ts=1_600,
            up_token_id=self.market.up_token_id,
            down_token_id=self.market.down_token_id,
            tick_size=self.market.tick_size,
            min_order_shares=self.market.min_order_shares,
            fee_schedule=self.market.fee_schedule,
        )
        with self.assertRaisesRegex(ValueError, "cannot be reused"):
            state.on_book_event(other_market, self.books, self.stale, 1_300_000, 1_450)


if __name__ == "__main__":
    unittest.main()
