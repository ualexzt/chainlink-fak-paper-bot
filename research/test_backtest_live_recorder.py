import json
import tempfile
import unittest
from pathlib import Path

from research.backtest_live_recorder import (
    CONFIGS,
    MarketState,
    fee,
    pnl_for,
    row_signal,
    run_backtest,
    settle_market,
)


class StrategyPrimitiveTests(unittest.TestCase):
    def test_row_signal_uses_the_leader_ask_and_margin(self):
        row = {
            "age": 60,
            "leader_cl": "UP",
            "fair_leader_lut": 0.80,
            "dist_cl_bps": 12.0,
            "up_ask": 0.75,
            "dn_ask": 0.20,
        }
        self.assertEqual(
            row_signal(row, margin=0.04, floor=8.0),
            ("UP", 0.75, 0.80, 12.0),
        )
        self.assertIsNone(row_signal(row, margin=0.06, floor=8.0))

    def test_row_signal_uses_down_ask_and_rejects_invalid_rows(self):
        row = {
            "age": 60,
            "leader_cl": "DOWN",
            "fair_leader_lut": 0.70,
            "dist_cl_bps": -10.0,
            "up_ask": 0.90,
            "dn_ask": 0.65,
        }
        self.assertEqual(
            row_signal(row, margin=0.04, floor=8.0),
            ("DOWN", 0.65, 0.70, -10.0),
        )
        for bad in (
            {**row, "dn_ask": None},
            {**row, "dn_ask": 0.0},
            {**row, "dist_cl_bps": -3.0},
            {**row, "age": 30},
        ):
            self.assertIsNone(row_signal(bad, margin=0.04, floor=8.0))

    def test_settle_market_uses_final_twap_and_treats_tie_as_up(self):
        state = MarketState("btc", "15m", 1000, 900)
        state.strike_cl = 100.0
        self.assertEqual(settle_market(state, 100.0), "UP")
        self.assertEqual(settle_market(state, 99.9), "DOWN")

    def test_settle_market_rejects_missing_exact_values(self):
        state = MarketState("btc", "15m", 1000, 900)
        self.assertIsNone(settle_market(state, None))
        state.strike_cl = 0.0
        self.assertIsNone(settle_market(state, 100.0))

    def test_fee_and_pnl_match_existing_research_model(self):
        self.assertAlmostEqual(fee(0.70), 0.03)
        self.assertAlmostEqual(pnl_for(0.70, True), 0.27)
        self.assertAlmostEqual(pnl_for(0.70, False), -0.73)

    def test_parameter_grid_is_the_approved_nine_combinations(self):
        self.assertEqual(len(CONFIGS), 9)
        self.assertEqual({c.margin for c in CONFIGS}, {0.03, 0.04, 0.06})
        self.assertEqual({c.floor for c in CONFIGS}, {3.0, 8.0, 15.0})


class StreamingRunTests(unittest.TestCase):
    def test_run_backtest_accepts_first_signal_once_and_scores_terminal_row(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "data"
            out = Path(tmp) / "out"
            root.mkdir()
            rows = []
            for age in range(60, 900):
                rows.append(
                    {
                        "ts": 1000 + age,
                        "symbol": "btc",
                        "tf": "15m",
                        "mkt_ts": 1000,
                        "age": age,
                        "strike_cl": 100.0,
                        "cl_twap60": 100.0 if age == 899 else 101.0,
                        "leader_cl": "UP",
                        "fair_leader_lut": 0.80,
                        "dist_cl_bps": 20.0,
                        "up_ask": 0.75 if age == 60 else 0.74,
                        "dn_ask": 0.20,
                    }
                )
            (root / "2026-01-01.jsonl").write_text(
                "".join(json.dumps(r) + "\n" for r in rows)
            )
            result = run_backtest(root, out)
            config = next(
                c
                for c in result["configs"]
                if c["margin"] == 0.04 and c["floor"] == 8.0
            )
            self.assertEqual(config["fills"], 1)
            self.assertEqual(config["wins"], 1)
            self.assertAlmostEqual(config["pnl_total"], 0.225)
            self.assertEqual(len(list(out.glob("*fills.csv"))), 1)

    def test_run_backtest_counts_duplicate_and_excludes_incomplete_market(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "data"
            out = Path(tmp) / "out"
            root.mkdir()
            row = {
                "ts": 1060,
                "symbol": "btc",
                "tf": "15m",
                "mkt_ts": 1000,
                "age": 60,
                "strike_cl": 100.0,
                "cl_twap60": 101.0,
                "leader_cl": "UP",
                "fair_leader_lut": 0.80,
                "dist_cl_bps": 20.0,
                "up_ask": 0.75,
                "dn_ask": 0.20,
            }
            (root / "2026-01-01.jsonl").write_text(
                json.dumps(row) + "\n" + json.dumps(row) + "\n"
            )
            result = run_backtest(root, out)
            config = next(
                c
                for c in result["configs"]
                if c["margin"] == 0.04 and c["floor"] == 8.0
            )
            self.assertEqual(result["coverage"]["duplicate_rows"], 1)
            self.assertEqual(result["markets"]["incomplete"], 1)
            self.assertEqual(config["fills"], 0)
            self.assertEqual(config["incomplete_signal_markets"], 1)
            self.assertEqual(result["symbols"], ["btc"])
            written = json.loads(
                (out / "live_chainlink_backtest_summary.json").read_text()
            )
            self.assertIn("artifacts", written)


if __name__ == "__main__":
    unittest.main()
