import json
import os
import tempfile
import unittest
from collections import deque

os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="recorder-tests-")
os.environ["LUT_PATH"] = ""

from recorder import recorder


class RtdsWatchdogTests(unittest.TestCase):
    def test_stale_symbols_include_timed_out_and_missing_symbols(self):
        self.assertEqual(
            recorder.stale_rtds_symbols(
                {"btc": 1.0}, ["btc", "eth"], now=12.0, timeout=10.0
            ),
            ["btc", "eth"],
        )
        self.assertEqual(
            recorder.stale_rtds_symbols(
                {"btc": 2.1, "eth": 2.0}, ["btc", "eth"], now=12.0, timeout=10.0
            ),
            [],
        )

    def test_observations_must_be_finite_positive_and_newer(self):
        cached = (100.0, 2_000)
        self.assertFalse(recorder.accept_cl_observation(cached, 99.0, 1_999))
        self.assertFalse(recorder.accept_cl_observation(cached, 101.0, 2_000))
        self.assertFalse(recorder.accept_cl_observation(cached, float("nan"), 2_001))
        self.assertFalse(recorder.accept_cl_observation(cached, 101.0, 0))
        self.assertTrue(recorder.accept_cl_observation(cached, 101.0, 2_001))


class RtdsFrameTests(unittest.TestCase):
    def test_only_new_selected_twap60_refreshes_watchdog_and_history(self):
        state = recorder.State()
        valid = json.dumps(
            {
                "topic": "crypto_prices_twap_sixty",
                "payload": {
                    "symbol": "btc/usd",
                    "full_accuracy_value": str(100 * recorder.E18),
                    "timestamp": 300_000,
                },
            }
        )
        self.assertEqual(
            recorder.process_rtds_frame(valid, state, ["btc", "eth"]), {"btc"}
        )
        self.assertEqual(state.cl[("btc", "twap60")], (100.0, 300_000))
        self.assertEqual(list(state.cl_history["btc"]), [(300_000, 100.0)])

        self.assertEqual(recorder.process_rtds_frame("PONG", state, ["btc"]), set())
        self.assertEqual(
            recorder.process_rtds_frame(
                valid.replace("btc/usd", "doge/usd"), state, ["btc"]
            ),
            set(),
        )
        self.assertEqual(recorder.process_rtds_frame(valid, state, ["btc"]), set())
        self.assertEqual(len(state.cl_history["btc"]), 1)

        twap30 = valid.replace("twap_sixty", "twap_thirty").replace(
            "300000", "301000"
        )
        self.assertEqual(recorder.process_rtds_frame(twap30, state, ["btc"]), set())
        self.assertEqual(state.cl[("btc", "twap30")], (100.0, 301_000))
        self.assertEqual(len(state.cl_history["btc"]), 1)

    def test_future_observation_is_not_cached_and_does_not_block_recovery(self):
        state = recorder.State()
        def frame(timestamp):
            return json.dumps(
                {
                    "topic": "crypto_prices_twap_sixty",
                    "payload": {
                        "symbol": "btc/usd",
                        "full_accuracy_value": str(100 * recorder.E18),
                        "timestamp": timestamp,
                    },
                }
            )

        self.assertEqual(
            recorder.process_rtds_frame(
                frame(999_000), state, ["btc"], now_ms=300_000
            ),
            set(),
        )
        self.assertNotIn(("btc", "twap60"), state.cl)
        self.assertEqual(
            recorder.process_rtds_frame(
                frame(300_000), state, ["btc"], now_ms=300_000
            ),
            {"btc"},
        )
        self.assertEqual(state.cl[("btc", "twap60")][1], 300_000)


class ResolverMetricTests(unittest.TestCase):
    def test_multiple_updates_before_aggregate_keep_first_eligible_start(self):
        state = recorder.State()
        frames = []
        for timestamp, value in ((300_000, 100), (301_000, 101)):
            frames.append(
                {
                    "topic": "crypto_prices_twap_sixty",
                    "payload": {
                        "symbol": "btc/usd",
                        "full_accuracy_value": str(value * recorder.E18),
                        "timestamp": timestamp,
                    },
                }
            )
        recorder.process_rtds_frame(
            json.dumps(frames), state, ["btc"], now_ms=301_000
        )

        fields = recorder.resolver_row_fields(
            state,
            "btc",
            "5m",
            300,
            301_000,
            state.cl[("btc", "twap60")],
        )

        self.assertEqual(fields["resolver_start_twap"], 100.0)
        self.assertEqual(fields["resolver_distance"], 1.0)

    def test_start_capture_uses_only_first_observation_in_first_ten_seconds(self):
        self.assertEqual(
            recorder.capture_resolver_start(None, 100.0, 300_000, 300), 100.0
        )
        self.assertEqual(
            recorder.capture_resolver_start(None, 101.0, 310_000, 300), 101.0
        )
        self.assertIsNone(
            recorder.capture_resolver_start(None, 101.0, 310_001, 300)
        )
        self.assertEqual(
            recorder.capture_resolver_start(100.0, 102.0, 305_000, 300), 100.0
        )

    def test_freshness_rejects_future_and_old_observations(self):
        self.assertEqual(recorder.cl_age_and_fresh(310_000, 305_000, 10.0), (5_000, True))
        self.assertEqual(recorder.cl_age_and_fresh(310_000, 299_999, 10.0), (10_001, False))
        self.assertEqual(recorder.cl_age_and_fresh(310_000, 310_001, 10.0), (-1, False))
        self.assertEqual(recorder.cl_age_and_fresh(310_000, None, 10.0), (None, False))

    def test_resolver_metrics_report_distance_bps_and_leader(self):
        self.assertEqual(recorder.resolver_metrics(101.0, 100.0), (1.0, 100.0, "UP"))
        self.assertEqual(recorder.resolver_metrics(99.0, 100.0), (-1.0, -100.0, "DOWN"))
        self.assertEqual(recorder.resolver_metrics(100.0, 100.0), (0.0, 0.0, "TIE"))

    def test_five_second_momentum_uses_latest_qualifying_prior_observation(self):
        history = deque(
            [
                (300_000, 100.0),
                (303_000, 101.0),
                (305_000, 102.0),
                (306_000, 103.0),
            ]
        )
        self.assertAlmostEqual(recorder.resolver_momentum_5s_bps(history), 300.0)
        self.assertIsNone(recorder.resolver_momentum_5s_bps(deque([(300_000, 100.0)])))

    def test_fresh_momentum_is_available_when_start_capture_was_missed(self):
        state = recorder.State()
        state.cl_history["btc"].extend(
            [(311_000, 100.0), (316_000, 101.0)]
        )
        fields = recorder.resolver_row_fields(
            state, "btc", "5m", 300, 316_000, (101.0, 316_000)
        )
        self.assertIsNone(fields["resolver_start_twap"])
        self.assertIsNone(fields["resolver_distance"])
        self.assertIsNone(fields["resolver_leader"])
        self.assertAlmostEqual(fields["resolver_momentum_5s_bps"], 100.0)

    def test_resolver_row_fields_capture_start_and_null_stale_or_missed_metrics(self):
        state = recorder.State()
        state.cl_history["btc"].append((300_000, 100.0))
        first = recorder.resolver_row_fields(
            state, "btc", "5m", 300, 300_000, (100.0, 300_000)
        )
        self.assertEqual(first["cl_age_ms"], 0)
        self.assertTrue(first["cl_fresh"])
        self.assertEqual(first["resolver_start_twap"], 100.0)
        self.assertEqual(first["resolver_leader"], "TIE")

        state.cl_history["btc"].append((306_000, 101.0))
        moved = recorder.resolver_row_fields(
            state, "btc", "5m", 300, 306_000, (101.0, 306_000)
        )
        self.assertEqual(moved["resolver_distance"], 1.0)
        self.assertEqual(moved["resolver_distance_bps"], 100.0)
        self.assertEqual(moved["resolver_leader"], "UP")
        self.assertAlmostEqual(moved["resolver_momentum_5s_bps"], 100.0)

        stale = recorder.resolver_row_fields(
            state, "btc", "5m", 300, 320_001, (101.0, 306_000)
        )
        self.assertFalse(stale["cl_fresh"])
        self.assertEqual(stale["resolver_start_twap"], 100.0)
        self.assertIsNone(stale["resolver_distance"])
        self.assertIsNone(stale["resolver_momentum_5s_bps"])

        missed = recorder.resolver_row_fields(
            recorder.State(), "btc", "5m", 300, 311_000, (101.0, 311_000)
        )
        self.assertTrue(missed["cl_fresh"])
        self.assertIsNone(missed["resolver_start_twap"])
        self.assertIsNone(missed["resolver_leader"])


if __name__ == "__main__":
    unittest.main()
