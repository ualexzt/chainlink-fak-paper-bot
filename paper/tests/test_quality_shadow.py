from decimal import Decimal as D
import unittest

from paper_bot.quality_shadow import QualityBook, QualityShadowState


def books(up_ask: str, down_ask: str, *, up_bid: str | None = None, down_bid: str | None = None):
    return {
        "UP": QualityBook(D(up_bid or str(D(up_ask) - D("0.01"))), D(up_ask)),
        "DOWN": QualityBook(D(down_bid or str(D(down_ask) - D("0.01"))), D(down_ask)),
    }


class QualityShadowTests(unittest.TestCase):
    def test_quality_candidate_entry_switch_and_settlement_are_causal(self):
        state = QualityShadowState("m1", 1000, "btc")
        self.assertEqual(state.sample(30, books("0.64", "0.37"))[0]["event_type"], "quality_candidate")
        for age in range(31, 120):
            self.assertEqual(state.sample(age, books("0.90", "0.11")), ())
        entry = state.sample(120, books("0.90", "0.11"))
        self.assertEqual(entry[0]["event_type"], "quality_entry")
        self.assertEqual(state.entry_ask, D("0.90"))

        self.assertEqual(state.sample(121, books("0.90", "0.11", up_bid="0.69")), ())
        self.assertEqual(state.sample(122, books("0.90", "0.11", up_bid="0.69")), ())
        armed = state.sample(123, books("0.90", "0.11", up_bid="0.69"))
        self.assertEqual(armed[0]["event_type"], "quality_switch_armed")
        self.assertIsNone(state.switch_age)
        switched = state.sample(124, books("0.70", "0.31", up_bid="0.68"))
        self.assertEqual(switched[0]["event_type"], "quality_switch")
        self.assertEqual(state.switch_age, 124)

        settled = state.settle("DOWN", 1400)
        self.assertEqual(settled["event_type"], "quality_settlement")
        self.assertGreater(state.pnl, D("0"))

    def test_early_filters_and_entry_floor_reject(self):
        drawdown = QualityShadowState("a", 1000, "eth")
        drawdown.sample(30, books("0.64", "0.37"))
        for age in range(31, 41):
            drawdown.sample(age, books("0.30", "0.71"))
        for age in range(41, 121):
            events = drawdown.sample(age, books("0.90", "0.11"))
        self.assertTrue(drawdown.filter_a)
        self.assertEqual(events[0]["reason"], "warning_filter")

        cheap = QualityShadowState("b", 1000, "sol")
        cheap.sample(30, books("0.64", "0.37"))
        for age in range(31, 121):
            events = cheap.sample(age, books("0.87", "0.14"))
        self.assertEqual(events[0]["reason"], "entry_ask_below_0.88")

    def test_opposite_warning_needs_ten_consecutive_samples(self):
        state = QualityShadowState("m", 1000, "btc")
        state.sample(30, books("0.64", "0.37"))
        for age in range(31, 120):
            opposite = "0.71" if 100 <= age <= 108 else "0.11"
            state.sample(age, books("0.90", opposite))
        state.sample(120, books("0.90", "0.71"))
        self.assertFalse(state.filter_b, "a broken run must not be treated as ten samples")
        self.assertEqual(state.stage, "ENTERED")

    def test_gap_resets_repair_and_missed_boundaries_fail_closed(self):
        missed = QualityShadowState("m", 1000, "btc")
        self.assertEqual(missed.sample(31, books("0.90", "0.11"))[0]["reason"], "age30_not_sampled")

        state = QualityShadowState("n", 1000, "btc")
        state.sample(30, books("0.64", "0.37"))
        for age in range(31, 121):
            state.sample(age, books("0.90", "0.11"))
        state.sample(121, books("0.90", "0.11", up_bid="0.69"))
        state.sample(123, books("0.90", "0.11", up_bid="0.69"))
        state.sample(124, books("0.90", "0.11", up_bid="0.69"))
        self.assertIsNone(state.switch_due_age)
        self.assertEqual(state.repair_run, 2)

    def test_snapshot_restore_preserves_one_shot_state(self):
        original = QualityShadowState("m", 1000, "btc")
        original.sample(30, books("0.64", "0.37"))
        for age in range(31, 121):
            original.sample(age, books("0.90", "0.11"))
        restored = QualityShadowState.restore(original.snapshot())
        self.assertEqual(restored.snapshot(), original.snapshot())
        self.assertEqual(restored.sample(120, books("0.90", "0.11")), ())

    def test_recorded_age_tracks_incomplete_seconds_without_faking_continuity(self):
        state = QualityShadowState("m", 1000, "btc")
        state.mark_recorded(29)
        with self.assertRaisesRegex(ValueError, "must increase"):
            state.mark_recorded(29)
        state.sample(30, books("0.64", "0.37"))
        state.mark_recorded(30)
        state.mark_recorded(31)
        restored = QualityShadowState.restore(state.snapshot())
        self.assertEqual(restored.last_age, 30)
        self.assertEqual(restored.last_recorded_age, 31)

    def test_no_signal_is_terminal(self):
        state = QualityShadowState("m", 1000, "btc")
        event = state.sample(30, books("0.55", "0.46"))[0]
        self.assertEqual(event["event_type"], "quality_no_signal")
        self.assertEqual(state.stage, "NO_SIGNAL")
        self.assertEqual(state.sample(120, books("0.95", "0.06")), ())


if __name__ == "__main__":
    unittest.main()
