import csv
import json
import math
import tempfile
import unittest
from pathlib import Path

from research.backtest_90cent_5m import (
    MarketState,
    load_gamma_outcomes,
    run_backtest,
    trade_result,
)


def row(
    age: int,
    up_ask: float | None,
    dn_ask: float | None,
    *,
    up_askq: float | None = 50.0,
    dn_askq: float | None = 50.0,
) -> dict:
    return {
        "ts": 1000 + age,
        "age": age,
        "up_ask": up_ask,
        "dn_ask": dn_ask,
        "up_askq": up_askq,
        "dn_askq": dn_askq,
    }


class EntryStateTests(unittest.TestCase):
    def test_initial_ask_above_threshold_is_baseline_not_cross(self):
        state = MarketState("btc", 1000, thresholds=(0.90,))
        state.process_row(row(0, 0.91, 0.09))
        self.assertFalse(state.entries[0.90].signal)

    def test_taker_uses_first_rising_cross_actual_ask(self):
        state = MarketState("btc", 1000, thresholds=(0.90,))
        state.process_row(row(0, 0.89, 0.11))
        state.process_row(row(1, 0.91, 0.09))
        entry = state.entries[0.90]
        self.assertTrue(entry.signal)
        self.assertEqual(entry.side, "UP")
        self.assertEqual(entry.trigger_age, 1)
        self.assertAlmostEqual(entry.taker_entry_price, 0.91)

    def test_missing_paired_book_breaks_crossing_continuity(self):
        state = MarketState("btc", 1000, thresholds=(0.90,))
        state.process_row(row(0, 0.89, 0.11))
        state.process_row(row(1, None, 0.10))
        state.process_row(row(2, 0.90, 0.10))
        self.assertFalse(state.entries[0.90].signal)
        state.process_row(row(3, 0.89, 0.11))
        state.process_row(row(4, 0.90, 0.10))
        self.assertTrue(state.entries[0.90].signal)
        self.assertEqual(state.entries[0.90].trigger_age, 4)

    def test_timestamp_gap_breaks_crossing_continuity(self):
        state = MarketState("btc", 1000, thresholds=(0.90,))
        state.process_row(row(0, 0.89, 0.11))
        state.process_row(row(2, 0.90, 0.10))
        self.assertFalse(state.entries[0.90].signal)
        state.process_row(row(3, 0.89, 0.11))
        state.process_row(row(4, 0.90, 0.10))
        self.assertTrue(state.entries[0.90].signal)

    def test_maker_cannot_fill_on_trigger_row_but_fills_later_at_limit(self):
        state = MarketState("btc", 1000, thresholds=(0.90,))
        state.process_row(row(0, 0.89, 0.11))
        state.process_row(row(1, 0.90, 0.10))
        entry = state.entries[0.90]
        self.assertAlmostEqual(entry.maker_limit, 0.89)
        self.assertIsNone(entry.maker_entry_price)
        state.process_row(row(2, 0.89, 0.11))
        self.assertAlmostEqual(entry.maker_entry_price, 0.89)
        self.assertEqual(entry.maker_fill_age, 2)
        self.assertEqual(entry.maker_time_to_fill, 1)

    def test_first_triggered_side_is_never_replaced(self):
        state = MarketState("btc", 1000, thresholds=(0.90,))
        state.process_row(row(0, 0.89, 0.10))
        state.process_row(row(1, 0.90, 0.09))
        state.process_row(row(2, 0.20, 0.89))
        state.process_row(row(3, 0.10, 0.90))
        self.assertEqual(state.entries[0.90].side, "UP")
        self.assertEqual(state.entries[0.90].trigger_age, 1)

    def test_first_cross_without_quantity_selects_side_but_taker_is_unfilled(self):
        state = MarketState("btc", 1000, thresholds=(0.90,))
        state.process_row(row(0, 0.89, 0.11))
        state.process_row(row(1, 0.90, 0.10, up_askq=0.0))
        entry = state.entries[0.90]
        self.assertTrue(entry.signal)
        self.assertEqual(entry.side, "UP")
        self.assertIsNone(entry.taker_entry_price)
        self.assertAlmostEqual(entry.maker_limit, 0.89)
        state.process_row(row(2, 0.89, 0.11))
        self.assertAlmostEqual(entry.maker_entry_price, 0.89)
        state.process_row(row(3, 0.10, 0.89))
        state.process_row(row(4, 0.09, 0.90))
        self.assertEqual(entry.side, "UP")

    def test_asymmetric_quantity_simultaneous_cross_is_ambiguous(self):
        state = MarketState("btc", 1000, thresholds=(0.90,))
        state.process_row(row(0, 0.89, 0.89))
        state.process_row(row(1, 0.90, 0.90, up_askq=50.0, dn_askq=0.0))
        entry = state.entries[0.90]
        self.assertTrue(entry.ambiguous)
        self.assertFalse(entry.signal)

    def test_simultaneous_cross_is_ambiguous_and_skipped(self):
        state = MarketState("btc", 1000, thresholds=(0.90,))
        state.process_row(row(0, 0.89, 0.89))
        state.process_row(row(1, 0.90, 0.90))
        entry = state.entries[0.90]
        self.assertTrue(entry.ambiguous)
        self.assertFalse(entry.signal)
        self.assertIsNone(entry.taker_entry_price)

    def test_maker_remains_unfilled_without_later_pullback(self):
        state = MarketState("btc", 1000, thresholds=(0.90,))
        state.process_row(row(0, 0.89, 0.11))
        state.process_row(row(1, 0.90, 0.10))
        state.process_row(row(2, 0.92, 0.08))
        self.assertIsNone(state.entries[0.90].maker_entry_price)


class AccountingTests(unittest.TestCase):
    def test_taker_and_maker_trade_accounting(self):
        taker_win = trade_result("UP", "UP", 0.91, "taker")
        self.assertTrue(taker_win["won"])
        self.assertAlmostEqual(taker_win["gross_pnl"], 0.09)
        self.assertAlmostEqual(taker_win["fee"], 0.005733)
        self.assertAlmostEqual(taker_win["net_pnl"], 0.084267)

        taker_loss = trade_result("UP", "DOWN", 0.91, "taker")
        self.assertFalse(taker_loss["won"])
        self.assertAlmostEqual(taker_loss["gross_pnl"], -0.91)
        self.assertAlmostEqual(taker_loss["net_pnl"], -0.915733)

        maker_win = trade_result("DOWN", "DOWN", 0.89, "maker")
        self.assertAlmostEqual(maker_win["fee"], 0.0)
        self.assertAlmostEqual(maker_win["net_pnl"], 0.11)


class BacktestRunnerTests(unittest.TestCase):
    def test_run_backtest_requires_official_outcomes(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            data_dir.mkdir()
            (data_dir / "one.jsonl").write_text("{}\n")
            with self.assertRaises(TypeError):
                run_backtest(data_dir, Path(tmp) / "out")

    def test_load_gamma_outcomes_reads_winner_and_fee_schedule(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "outcomes.jsonl"
            path.write_text(
                json.dumps(
                    {
                        "symbol": "btc",
                        "mkt_ts": 1000,
                        "slug": "btc-updown-5m-1000",
                        "winner": "Down",
                        "outcomes": ["Up", "Down"],
                        "outcomePrices": ["0", "1"],
                        "closed": True,
                        "umaResolutionStatus": "resolved",
                        "feeSchedule": {"rate": 0.07, "exponent": 1},
                    }
                )
                + "\n"
            )
            outcomes = load_gamma_outcomes(path)
            self.assertEqual(outcomes[("btc", 1000)]["winner"], "DOWN")
            self.assertEqual(outcomes[("btc", 1000)]["fee_rate"], 0.07)
            self.assertEqual(outcomes[("btc", 1000)]["fee_exponent"], 1.0)

    def test_load_gamma_outcomes_rejects_winner_disagreement(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "outcomes.jsonl"
            path.write_text(
                json.dumps(
                    {
                        "symbol": "btc",
                        "mkt_ts": 1000,
                        "slug": "btc-updown-5m-1000",
                        "winner": "UP",
                        "outcomes": ["Up", "Down"],
                        "outcomePrices": ["0", "1"],
                        "closed": True,
                        "umaResolutionStatus": "resolved",
                        "feeSchedule": {"rate": 0.07, "exponent": 1},
                    }
                )
                + "\n"
            )
            with self.assertRaisesRegex(ValueError, "winner disagreement"):
                load_gamma_outcomes(path)

    def test_run_backtest_writes_outputs_and_excludes_incomplete_outcomes(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            out_dir = Path(tmp) / "out"
            data_dir.mkdir()

            rows = []
            for age in range(300):
                if age == 11:
                    up_ask, dn_ask = 0.90, 0.10
                elif age == 12:
                    up_ask, dn_ask = 0.89, 0.11
                else:
                    up_ask, dn_ask = 0.895, 0.105
                rows.append(
                    {
                        "ts": 1000 + age,
                        "symbol": "btc",
                        "tf": "5m",
                        "mkt_ts": 1000,
                        "age": age,
                        "strike_cl": 100.0,
                        "cl_twap60": 101.0 if age == 299 else 100.0,
                        "up_ask": up_ask,
                        "dn_ask": dn_ask,
                        "up_askq": 50.0,
                        "dn_askq": 50.0,
                    }
                )

            for age in range(20):
                rows.append(
                    {
                        "ts": 2000 + age,
                        "symbol": "eth",
                        "tf": "5m",
                        "mkt_ts": 2000,
                        "age": age,
                        "strike_cl": 100.0,
                        "cl_twap60": 100.0,
                        "up_ask": 0.90 if age == 11 else 0.89 if age == 12 else 0.895,
                        "dn_ask": 0.10 if age == 11 else 0.11 if age == 12 else 0.105,
                        "up_askq": 50.0,
                        "dn_askq": 50.0,
                    }
                )

            rows.append(
                {
                    "ts": 3000,
                    "symbol": "xrp",
                    "tf": "5m",
                    "mkt_ts": 3000,
                    "age": 0,
                    "up_ask": 0.50,
                    "dn_ask": 0.50,
                    "up_askq": 50.0,
                    "dn_askq": 50.0,
                }
            )
            (data_dir / "2026-01-01.jsonl").write_text(
                "".join(json.dumps(item) + "\n" for item in rows)
            )

            outcomes = {
                ("btc", 1000): {
                    "winner": "DOWN",
                    "fee_rate": 0.07,
                    "fee_exponent": 1.0,
                }
            }
            summary = run_backtest(data_dir, out_dir, outcomes=outcomes)
            taker = summary["configs"]["taker"]["0.90"]
            maker = summary["configs"]["maker"]["0.90"]
            self.assertEqual(summary["markets"]["seen"], 2)
            self.assertEqual(taker["complete_markets"], 1)
            self.assertEqual(taker["incomplete_markets"], 1)
            self.assertEqual(taker["signals"], 1)
            self.assertEqual(taker["fills"], 1)
            self.assertEqual(taker["wins"], 0)
            self.assertEqual(taker["losses"], 1)
            self.assertEqual(taker["win_rate"], 0.0)
            self.assertEqual(maker["fills"], 1)
            self.assertEqual(maker["wins"], 0)
            self.assertEqual(maker["losses"], 1)
            self.assertEqual(summary["settlement_source"], "official_gamma_outcomePrices")
            self.assertTrue((out_dir / "summary.json").exists())
            self.assertTrue((out_dir / "trades.csv").exists())

            with (out_dir / "trades.csv").open(newline="") as handle:
                trade_rows = list(csv.DictReader(handle))
            self.assertEqual(len(trade_rows), 20)
            complete_fills = [
                item
                for item in trade_rows
                if item["complete"] == "True" and item["filled"] == "True"
            ]
            self.assertEqual(len(complete_fills), 2)
            self.assertEqual(
                summary["artifacts"],
                {"summary": "summary.json", "trades": "trades.csv"},
            )

            for variant in ("taker", "maker"):
                for threshold in summary["thresholds"]:
                    key = f"{threshold:.2f}"
                    stats = summary["configs"][variant][key]
                    config_rows = [
                        item
                        for item in trade_rows
                        if item["variant"] == variant and item["threshold_key"] == key
                    ]
                    complete_rows = [
                        item for item in config_rows if item["complete"] == "True"
                    ]
                    fill_rows = [
                        item for item in complete_rows if item["filled"] == "True"
                    ]
                    self.assertEqual(len(complete_rows), stats["complete_markets"])
                    self.assertEqual(len(fill_rows), stats["fills"])
                    self.assertEqual(
                        sum(item["won"] == "True" for item in fill_rows),
                        stats["wins"],
                    )
                    self.assertEqual(
                        sum(item["won"] == "False" for item in fill_rows),
                        stats["losses"],
                    )
                    self.assertTrue(
                        math.isclose(
                            sum(float(item["net_pnl"]) for item in fill_rows),
                            stats["modeled_net_pnl_total_1share"],
                            abs_tol=1e-12,
                        )
                    )


if __name__ == "__main__":
    unittest.main()
