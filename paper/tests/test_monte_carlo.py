from __future__ import annotations

import unittest
from decimal import Decimal as D

from paper_bot.books import OrderBook
from paper_bot.domain import BookLevel, FeeSchedule
from paper_bot.gamma import MarketDefinition
from paper_bot.monte_carlo import (
    MODEL_VERSION,
    MonteCarloForecastEvent,
    MonteCarloShadowState,
    _bootstrap_probability,
)
from paper_bot.resolver import ResolverState
from paper_bot.strategy import Confirmation, StrategyEvent


class MonteCarloTests(unittest.TestCase):
    def setUp(self):
        self.market = MarketDefinition(
            symbol="btc", slug="btc-updown-5m-1000", market_id="market-1",
            mkt_ts=1_000, end_ts=1_300, up_token_id="up", down_token_id="down",
            tick_size=D("0.01"), min_order_shares=D("1"),
            fee_schedule=FeeSchedule(D("0"), D("1")),
        )
        self.books = {"up": OrderBook(), "down": OrderBook()}
        for token, bid, ask in (("up", "0.84", "0.85"), ("down", "0.14", "0.15")):
            self.books[token].apply_snapshot(
                [BookLevel(D(bid), D("20"))], [BookLevel(D(ask), D("20"))], 1, 1
            )
        self.resolver = ResolverState(("btc",))
        observations = [(1_000_000, D("100"))] + [
            (1_176_000 + index * 1_000, D("100.01") + D(index) / D("100"))
            for index in range(39)
        ]
        for ts, value in observations:
            self.assertTrue(self.resolver.accept("btc", value, ts, ts))

    def test_bootstrap_is_deterministic_and_uses_only_supplied_history(self):
        history = self.resolver.history("btc")
        first = _bootstrap_probability(
            history, start=D("100"), current=history[-1][1], side="UP",
            seconds_to_close=85, seed_material="fixed",
        )
        second = _bootstrap_probability(
            history, start=D("100"), current=history[-1][1], side="UP",
            seconds_to_close=85, seed_material="fixed",
        )
        self.assertEqual(first, second)
        self.assertEqual(first[0], D("1.0000"))
        self.assertEqual(first[3], 39)

    def test_feed_gap_is_never_converted_into_a_return(self):
        history = self.resolver.history("btc")
        gapped = history[:35] + tuple(
            (timestamp + 120_000, value) for timestamp, value in history[35:]
        )
        result = _bootstrap_probability(
            gapped, start=D("100"), current=gapped[-1][1], side="UP",
            seconds_to_close=60, seed_material="gap",
        )
        self.assertEqual(result[0], D("1.0000"))
        self.assertEqual(result[3], 38)

    def test_too_few_non_gap_returns_remains_fail_closed(self):
        history = tuple((1_000_000 + index * 120_000, D("100") + D(index)) for index in range(30))
        with self.assertRaisesRegex(ValueError, "continuous_history_insufficient"):
            _bootstrap_probability(
                history, start=D("100"), current=history[-1][1], side="UP",
                seconds_to_close=60, seed_material="all-gaps",
            )

    def test_90_second_lane_enters_only_with_fee_adjusted_edge(self):
        state = MonteCarloShadowState(paper_notional_usd=D("5"), config_hash="a" * 64)
        events = state.on_event(self.market, self.books, self.resolver, 1_214_500, 1_215)
        self.assertEqual(len(events), 2)
        forecast, entry = events
        self.assertIsInstance(forecast, MonteCarloForecastEvent)
        self.assertIsInstance(entry, StrategyEvent)
        self.assertEqual(forecast.model_version, MODEL_VERSION)
        self.assertEqual(forecast.horizon_seconds, 90)
        self.assertEqual(forecast.decision, "ENTER")
        self.assertEqual(forecast.break_even_probability, D("0.8500"))
        self.assertEqual(entry.lane.confirmation, Confirmation.MC_BOOTSTRAP_90_V3)
        self.assertEqual(entry.fak.status, "full")
        self.assertEqual(state.on_event(self.market, self.books, self.resolver, 1_215_000, 1_215), ())

    def test_stale_resolver_waits_for_first_valid_snapshot_in_fixed_window(self):
        state = MonteCarloShadowState(paper_notional_usd=D("5"), config_hash="b" * 64)
        events = state.on_event(self.market, self.books, self.resolver, 1_225_000, 1_225)
        self.assertEqual(events, ())
        self.assertTrue(self.resolver.accept("btc", D("100.40"), 1_225_100, 1_225_100))
        events = state.on_event(self.market, self.books, self.resolver, 1_225_100, 1_225, 1_225_100)
        self.assertEqual(len(events), 2)
        self.assertEqual(events[0].decision, "ENTER")

    def test_window_records_reject_if_no_valid_snapshot_ever_arrives(self):
        state = MonteCarloShadowState(paper_notional_usd=D("5"), config_hash="d" * 64)
        self.assertEqual(state.on_event(self.market, self.books, self.resolver, 1_225_000, 1_225), ())
        events = state.on_event(self.market, self.books, self.resolver, 1_240_000, 1_240)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].horizon_seconds, 90)
        self.assertEqual(events[0].decision, "REJECT")
        self.assertEqual(events[0].reason, "window_no_valid_snapshot_resolver_stale")
        close = state.on_event(self.market, self.books, self.resolver, 1_301_000, 1_301)
        self.assertEqual([event.horizon_seconds for event in close], [60, 30])
        self.assertEqual(close[0].decision, "REJECT")
        self.assertEqual(close[1].decision, "MISSED")

    def test_late_start_records_missed_windows_then_waits_in_current_lane(self):
        state = MonteCarloShadowState(paper_notional_usd=D("5"), config_hash="c" * 64)
        events = state.on_event(self.market, self.books, self.resolver, 1_275_000, 1_275)
        self.assertEqual([event.horizon_seconds for event in events if isinstance(event, MonteCarloForecastEvent)],
                         [90, 60])
        self.assertEqual([event.reason for event in events], ["window_missed", "window_missed"])
        self.assertTrue(self.resolver.accept("btc", D("100.40"), 1_275_100, 1_275_100))
        current = state.on_event(
            self.market, self.books, self.resolver, 1_275_100, 1_275, 1_275_100
        )
        self.assertEqual(current[0].horizon_seconds, 30)


if __name__ == "__main__":
    unittest.main()
