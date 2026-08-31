import json
import tempfile
import unittest
from pathlib import Path

from research.paired_maker_backtest import (
    MarketState,
    quote_price,
    run_backtest,
    settle_pair,
    simulate_market,
)


class PairPrimitiveTests(unittest.TestCase):
    def test_quote_is_one_cent_below_midpoint_on_tick_grid(self):
        self.assertEqual(quote_price(0.505), 0.49)
        self.assertEqual(quote_price(0.51), 0.50)
        self.assertEqual(quote_price(0.01), 0.0)

    def test_settlement_pays_winning_leg_and_exposes_unhedged_loss(self):
        state = MarketState("btc", "15m", 1000, 900)
        state.up_filled = 50
        state.down_filled = 0
        state.up_cost = 24.5
        self.assertEqual(
            settle_pair(state, "UP"),
            {"payout": 50.0, "cost": 24.5, "pnl": 25.5, "paired": False},
        )
        self.assertEqual(settle_pair(state, "DOWN")["pnl"], -24.5)

    def test_full_pair_locks_two_share_balanced_profit(self):
        state = MarketState("btc", "15m", 1000, 900)
        state.up_filled = state.down_filled = 50
        state.up_cost = 24.5
        state.down_cost = 24.0
        result = settle_pair(state, "UP")
        self.assertTrue(result["paired"])
        self.assertAlmostEqual(result["payout"], 50.0)
        self.assertAlmostEqual(result["pnl"], 1.5)

    def test_quote_touch_fills_before_requote_and_partial_quantity_is_capped(self):
        rows = [
            {
                "ts": 1000,
                "age": 0,
                "up_bid": 0.49,
                "up_ask": 0.51,
                "up_askq": 100,
                "dn_bid": 0.49,
                "dn_ask": 0.51,
                "dn_askq": 100,
            },
            {
                "ts": 1001,
                "age": 1,
                "up_bid": 0.48,
                "up_ask": 0.49,
                "up_askq": 7,
                "dn_bid": 0.49,
                "dn_ask": 0.51,
                "dn_askq": 100,
            },
        ]
        state = simulate_market("btc", "15m", 1000, rows, mode="quote_touch")
        self.assertEqual(state.up_filled, 7.0)
        self.assertEqual(state.down_filled, 0.0)
        self.assertEqual(state.up_fill_count, 1)

    def test_fifty_share_quote_below_five_usdc_is_not_placed(self):
        rows = [
            {
                "ts": 1000,
                "age": 0,
                "up_bid": 0.04,
                "up_ask": 0.05,
                "up_askq": 100,
                "dn_bid": 0.04,
                "dn_ask": 0.05,
                "dn_askq": 100,
            }
        ]
        state = simulate_market("btc", "15m", 1000, rows)
        self.assertFalse(state.initialized)

    def test_strict_full_mode_does_not_count_small_touch(self):
        rows = [
            {
                "ts": 1000,
                "age": 0,
                "up_bid": 0.49,
                "up_ask": 0.51,
                "up_askq": 100,
                "dn_bid": 0.49,
                "dn_ask": 0.51,
                "dn_askq": 100,
            },
            {
                "ts": 1001,
                "age": 1,
                "up_bid": 0.48,
                "up_ask": 0.49,
                "up_askq": 7,
                "dn_bid": 0.49,
                "dn_ask": 0.51,
                "dn_askq": 100,
            },
        ]
        state = simulate_market("btc", "15m", 1000, rows, mode="strict_full")
        self.assertEqual(state.up_filled, 0.0)

    def test_reward_eligibility_requires_both_50_share_orders_within_1_5_cents(self):
        row = {"up_bid": 0.49, "up_ask": 0.51, "dn_bid": 0.49, "dn_ask": 0.51}
        state = MarketState("btc", "15m", 1000, 900)
        state.up_order_price = state.down_order_price = 0.49
        state.up_remaining = state.down_remaining = 50.0
        state.update_reward_seconds(row, min_size=50, max_spread_cents=1.5)
        self.assertEqual(state.reward_eligible_seconds, 1)
        state.up_remaining = 49.0
        state.update_reward_seconds(row, min_size=50, max_spread_cents=1.5)
        self.assertEqual(state.reward_eligible_seconds, 1)


class PairRunTests(unittest.TestCase):
    def test_run_backtest_writes_summary_and_pair_rows(self):
        from research.paired_maker_backtest import run_backtest

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "data"
            out = Path(tmp) / "out"
            root.mkdir()
            rows = []
            for age in range(300):
                rows.append(
                    {
                        "ts": 1000 + age,
                        "symbol": "btc",
                        "tf": "5m",
                        "mkt_ts": 1000,
                        "age": age,
                        "strike_cl": 100.0,
                        "cl_twap60": 101.0 if age == 299 else 100.0,
                        "up_bid": 0.49,
                        "up_ask": 0.51 if age == 0 else 0.49,
                        "up_bidq": 100,
                        "up_askq": 50,
                        "dn_bid": 0.49,
                        "dn_ask": 0.51,
                        "dn_bidq": 100,
                        "dn_askq": 50,
                    }
                )
            (root / "2026-01-01.jsonl").write_text(
                "".join(json.dumps(row) + "\n" for row in rows)
            )
            summary = run_backtest(root, out)
            self.assertEqual(summary["config"]["order_size"], 50)
            self.assertIn("quote_touch", summary["modes"])
            self.assertTrue((out / "paired_maker_summary.json").exists())
            self.assertTrue((out / "paired_maker_pairs.csv").exists())

    def test_incomplete_full_pair_is_not_counted_in_settled_rate(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "data"
            out = Path(tmp) / "out"
            root.mkdir()
            rows = []
            for age in range(300):
                rows.append(
                    {
                        "ts": 1000 + age,
                        "symbol": "btc",
                        "tf": "5m",
                        "mkt_ts": 1000,
                        "age": age,
                        "strike_cl": 100.0,
                        "cl_twap60": 101.0 if age == 299 else 100.0,
                        "up_bid": 0.49,
                        "up_ask": 0.51 if age == 0 else 0.49,
                        "up_askq": 50,
                        "dn_bid": 0.49,
                        "dn_ask": 0.51 if age == 0 else 0.49,
                        "dn_askq": 50,
                    }
                )
            for age in range(2):
                rows.append(
                    {
                        "ts": 2000 + age,
                        "symbol": "btc",
                        "tf": "5m",
                        "mkt_ts": 2000,
                        "age": age,
                        "strike_cl": 100.0,
                        "cl_twap60": 100.0,
                        "up_bid": 0.49,
                        "up_ask": 0.51 if age == 0 else 0.49,
                        "up_askq": 50,
                        "dn_bid": 0.49,
                        "dn_ask": 0.51 if age == 0 else 0.49,
                        "dn_askq": 50,
                    }
                )
            (root / "2026-01-01.jsonl").write_text(
                "".join(json.dumps(row) + "\n" for row in rows)
            )
            summary = run_backtest(root, out)
            mode = summary["modes"]["quote_touch"]
            self.assertEqual(mode["full_pair_markets"], 2)
            self.assertEqual(mode["complete_markets"], 1)
            self.assertAlmostEqual(mode["full_pair_rate_of_settled"], 1.0)


if __name__ == "__main__":
    unittest.main()
