from __future__ import annotations

import unittest
from dataclasses import FrozenInstanceError
from decimal import Decimal

import paper_bot.resolver as resolver
from paper_bot.resolver import ResolverState, ResolverView


class ResolverConstructorTests(unittest.TestCase):
    def test_view_dataclass_is_frozen_and_module_has_no_network_surface(self):
        view = ResolverView(
            symbol="btc",
            current=Decimal("100"),
            start=Decimal("99"),
            observation_ts_ms=300_000,
            age_ms=0,
            fresh=True,
            distance=Decimal("1"),
            distance_bps=Decimal("101.0101010101010101010101010"),
            leader="UP",
            momentum_5s_bps=Decimal("50"),
        )

        with self.assertRaises(FrozenInstanceError):
            view.current = Decimal("101")  # type: ignore[misc]

        self.assertFalse(hasattr(resolver, "request"))
        self.assertFalse(hasattr(resolver, "Binance"))
        self.assertFalse(hasattr(resolver, "binance"))

    def test_state_rejects_non_lowercase_or_duplicate_symbols(self):
        with self.assertRaises(ValueError):
            ResolverState(("BTC",))
        with self.assertRaises(ValueError):
            ResolverState(("btc", "btc"))

    def test_state_rejects_explicit_empty_symbol_iterables(self):
        with self.assertRaises(ValueError):
            ResolverState(())
        with self.assertRaises(ValueError):
            ResolverState([])

    def test_state_rejects_history_size_below_two_and_bool_values(self):
        for bad in (1, 0, True):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    ResolverState(("btc",), history_size=bad)


class ResolverAcceptTests(unittest.TestCase):
    def test_accept_rejects_malformed_future_duplicate_and_out_of_order_without_mutation(self):
        state = ResolverState(("btc",), history_size=2)

        self.assertTrue(state.accept("btc", Decimal("100"), 300_000, 300_000))
        fresh = state.view("btc", 300, 300_000)
        self.assertEqual(fresh.current, Decimal("100"))
        self.assertEqual(fresh.age_ms, 0)
        self.assertTrue(fresh.fresh)

        self.assertFalse(state.accept("btc", Decimal("101"), 300_001, 300_000))
        self.assertFalse(state.accept("btc", Decimal("101"), 299_999, 300_000))
        self.assertFalse(state.accept("btc", Decimal("101"), 300_000, 300_001))
        self.assertFalse(state.accept("btc", Decimal("101"), 310_001, 300_000))
        self.assertFalse(state.accept("btc", 101.0, 300_001, 300_001))
        self.assertFalse(state.accept("btc", Decimal("NaN"), 300_001, 300_001))
        self.assertFalse(state.accept("btc", Decimal("0"), 300_001, 300_001))
        self.assertFalse(state.accept("btc", Decimal("101"), True, 300_001))
        self.assertFalse(state.accept("btc", Decimal("101"), 300_001, False))
        self.assertFalse(state.accept("eth", Decimal("101"), 300_001, 300_001))

        self.assertTrue(state.accept("btc", Decimal("101"), 300_001, 300_001))
        after = state.view("btc", 300, 300_001)
        self.assertEqual(after.current, Decimal("101"))
        self.assertEqual(after.start, Decimal("100"))

    def test_accept_allows_exactly_ten_seconds_old_observation(self):
        state = ResolverState(("btc",))
        self.assertTrue(state.accept("btc", Decimal("100"), 300_000, 310_000))
        self.assertEqual(state.view("btc", 300, 310_000).age_ms, 10_000)

    def test_stale_symbols_are_deterministic_and_symbol_independent(self):
        state = ResolverState(("btc", "eth", "sol"))
        self.assertTrue(state.accept("btc", Decimal("100"), 300_000, 300_000))
        self.assertTrue(state.accept("eth", Decimal("200"), 305_000, 305_000))

        self.assertEqual(state.stale_symbols(304_999), ("eth", "sol"))
        self.assertEqual(state.stale_symbols(310_001), ("btc", "sol"))
        self.assertIsInstance(state.stale_symbols(310_001), tuple)
        with self.assertRaises(AttributeError):
            state.stale_symbols(310_001).append("x")  # type: ignore[attr-defined]


class ResolverViewTests(unittest.TestCase):
    def test_view_captures_first_eligible_start_and_uses_latest_qualifying_momentum(self):
        state = ResolverState(("btc",), history_size=8)
        self.assertTrue(state.accept("btc", Decimal("100"), 300_000, 300_000))
        self.assertTrue(state.accept("btc", Decimal("101"), 301_000, 301_000))
        self.assertTrue(state.accept("btc", Decimal("104"), 305_000, 305_000))
        self.assertTrue(state.accept("btc", Decimal("105"), 306_000, 306_000))

        view = state.view("btc", 300, 306_000)
        self.assertEqual(view.current, Decimal("105"))
        self.assertEqual(view.observation_ts_ms, 306_000)
        self.assertEqual(view.age_ms, 0)
        self.assertTrue(view.fresh)
        self.assertEqual(view.start, Decimal("100"))
        self.assertEqual(view.distance, Decimal("5"))
        self.assertEqual(view.distance_bps, Decimal("500"))
        self.assertEqual(view.leader, "UP")
        self.assertEqual(
            view.momentum_5s_bps,
            (Decimal("105") / Decimal("101") - Decimal("1")) * Decimal("10000"),
        )

    def test_view_emits_momentum_without_start_when_history_is_fresh(self):
        state = ResolverState(("btc",), history_size=4)
        self.assertTrue(state.accept("btc", Decimal("100"), 311_000, 311_000))
        self.assertTrue(state.accept("btc", Decimal("101"), 316_000, 316_000))

        view = state.view("btc", 300, 316_000)
        self.assertTrue(view.fresh)
        self.assertIsNone(view.start)
        self.assertIsNone(view.distance)
        self.assertIsNone(view.distance_bps)
        self.assertIsNone(view.leader)
        self.assertEqual(view.momentum_5s_bps, Decimal("100"))

    def test_stale_view_preserves_last_display_values(self):
        state = ResolverState(("btc",), history_size=4)
        self.assertTrue(state.accept("btc", Decimal("100"), 300_000, 300_000))
        self.assertTrue(state.accept("btc", Decimal("101"), 306_000, 306_000))

        fresh = state.view("btc", 300, 306_000)
        stale = state.view("btc", 300, 320_001)

        self.assertTrue(fresh.fresh)
        self.assertFalse(stale.fresh)
        self.assertEqual(stale.current, fresh.current)
        self.assertEqual(stale.start, fresh.start)
        self.assertEqual(stale.distance, fresh.distance)
        self.assertEqual(stale.distance_bps, fresh.distance_bps)
        self.assertEqual(stale.leader, fresh.leader)
        self.assertEqual(stale.momentum_5s_bps, fresh.momentum_5s_bps)
        self.assertEqual(stale.age_ms, 14_001)

    def test_stale_first_lookup_recovers_start_from_history(self):
        state = ResolverState(("btc",), history_size=4)
        self.assertTrue(state.accept("btc", Decimal("100"), 300_000, 300_000))
        self.assertTrue(state.accept("btc", Decimal("101"), 306_000, 306_000))

        view = state.view("btc", 300, 320_001)
        self.assertFalse(view.fresh)
        self.assertEqual(view.start, Decimal("100"))
        self.assertEqual(view.distance, Decimal("1"))
        self.assertEqual(view.distance_bps, Decimal("100"))
        self.assertEqual(view.leader, "UP")

    def test_market_keyed_start_cache_prunes_by_timestamp_not_view_order(self):
        def seed(state: ResolverState) -> None:
            for ts, value in (
                (300_000, Decimal("100")),
                (305_000, Decimal("101")),
                (310_000, Decimal("102")),
            ):
                self.assertTrue(state.accept("btc", value, ts, ts))

        state = ResolverState(("btc",), history_size=3)
        seed(state)
        self.assertEqual(
            [
                state.view("btc", 305, 310_000).start,
                state.view("btc", 300, 310_000).start,
                state.view("btc", 305, 310_000).start,
            ],
            [Decimal("101"), Decimal("100"), Decimal("101")],
        )
        self.assertTrue(state.accept("btc", Decimal("103"), 315_000, 315_000))
        self.assertEqual(
            [
                state.view("btc", 310, 315_000).start,
                state.view("btc", 315, 315_000).start,
            ],
            [Decimal("102"), Decimal("103")],
        )
        self.assertTrue(state.accept("btc", Decimal("104"), 320_000, 320_000))
        self.assertEqual(state.view("btc", 305, 320_000).start, Decimal("101"))

        reverse = ResolverState(("btc",), history_size=3)
        seed(reverse)
        self.assertEqual(
            [
                reverse.view("btc", 300, 310_000).start,
                reverse.view("btc", 305, 310_000).start,
                reverse.view("btc", 300, 310_000).start,
            ],
            [Decimal("100"), Decimal("101"), Decimal("100")],
        )
        self.assertTrue(reverse.accept("btc", Decimal("103"), 315_000, 315_000))
        self.assertEqual(
            [
                reverse.view("btc", 310, 315_000).start,
                reverse.view("btc", 315, 315_000).start,
            ],
            [Decimal("102"), Decimal("103")],
        )
        self.assertTrue(reverse.accept("btc", Decimal("104"), 320_000, 320_000))
        self.assertEqual(reverse.view("btc", 305, 320_000).start, Decimal("101"))

    def test_bounded_history_keeps_start_surviving_eviction_and_rollover_is_per_symbol(self):
        state = ResolverState(("btc", "eth"), history_size=2)
        self.assertTrue(state.accept("btc", Decimal("100"), 300_000, 300_000))
        self.assertEqual(state.view("btc", 300, 300_000).start, Decimal("100"))

        self.assertTrue(state.accept("btc", Decimal("101"), 301_000, 301_000))
        self.assertTrue(state.accept("btc", Decimal("102"), 302_000, 302_000))
        btc_survives = state.view("btc", 300, 302_000)
        self.assertEqual(btc_survives.start, Decimal("100"))
        self.assertEqual(btc_survives.distance, Decimal("2"))

        self.assertTrue(state.accept("eth", Decimal("200"), 400_000, 400_000))
        self.assertEqual(state.view("eth", 400, 400_000).start, Decimal("200"))
        self.assertTrue(state.accept("eth", Decimal("201"), 401_000, 401_000))
        self.assertTrue(state.accept("eth", Decimal("202"), 402_000, 402_000))
        self.assertEqual(state.view("eth", 400, 402_000).start, Decimal("200"))
        self.assertEqual(state.view("btc", 300, 302_000).start, Decimal("100"))

    def test_view_rejects_invalid_inputs(self):
        state = ResolverState(("btc",))
        bad_cases = [
            ("BTC", 300, 300_000),
            ("eth", 300, 300_000),
            ("btc", 0, 300_000),
            ("btc", True, 300_000),
            ("btc", 300, 0),
            ("btc", 300, True),
        ]
        for symbol, mkt_ts, now_ms in bad_cases:
            with self.subTest(symbol=symbol, mkt_ts=mkt_ts, now_ms=now_ms):
                with self.assertRaises((TypeError, ValueError)):
                    state.view(symbol, mkt_ts, now_ms)


if __name__ == "__main__":
    unittest.main()
