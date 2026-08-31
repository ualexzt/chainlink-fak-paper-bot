import csv
import json
import tempfile
import unittest
from pathlib import Path

from research import backtest_90cent_switch_5m as switch

MarketState = switch.MarketState


def row(
    ts: int,
    *,
    mkt_ts: int = 0,
    symbol: str = "btc",
    up_a: float = 0.20,
    up_aq: float = 100.0,
    up_b: float = 0.19,
    up_bq: float = 100.0,
    down_a: float = 0.81,
    down_aq: float = 100.0,
    down_b: float = 0.80,
    down_bq: float = 100.0,
) -> dict:
    return {
        "ts": ts,
        "mkt_ts": mkt_ts,
        "age": ts - mkt_ts,
        "symbol": symbol,
        "tf": "5m",
        "up_ask": up_a,
        "up_askq": up_aq,
        "up_bid": up_b,
        "up_bidq": up_bq,
        "dn_ask": down_a,
        "dn_askq": down_aq,
        "dn_bid": down_b,
        "dn_bidq": down_bq,
    }


class SwitchStateTests(unittest.TestCase):
    def test_cross_before_last_150_seconds_does_not_enter(self):
        state = MarketState("btc", 0)
        state.process_row(row(148, up_a=0.89, down_a=0.12))
        state.process_row(row(149, up_a=0.90, down_a=0.11))
        state.process_row(row(150, up_a=0.91, down_a=0.10))

        self.assertIsNone(state.signal_side)
        self.assertFalse(state.tracks["strict_50"].initial_filled)
        self.assertFalse(state.tracks["optimistic_touch"].initial_filled)

    def test_age_150_exact_90_cross_fills_both_models_with_full_depth(self):
        state = MarketState("btc", 0)
        state.process_row(row(149, up_a=0.89, down_a=0.12))
        state.process_row(row(150, up_a=0.90, up_aq=50, down_a=0.11))

        self.assertEqual(state.signal_side, "UP")
        self.assertEqual(state.signal_ts, 150)
        for track in state.tracks.values():
            self.assertTrue(track.initial_filled)
            self.assertEqual(track.initial_side, "UP")
            self.assertEqual(track.initial_price, 0.90)

    def test_optimistic_fills_positive_quantity_while_strict_requires_50(self):
        state = MarketState("btc", 0)
        state.process_row(row(150, up_a=0.89, down_a=0.12))
        state.process_row(row(151, up_a=0.90, up_aq=10, down_a=0.11))

        self.assertFalse(state.tracks["strict_50"].initial_filled)
        self.assertTrue(state.tracks["optimistic_touch"].initial_filled)

    def test_cross_jump_above_90_is_signal_but_not_limit_fill(self):
        state = MarketState("btc", 0)
        state.process_row(row(150, up_a=0.89, down_a=0.12))
        state.process_row(row(151, up_a=0.91, down_a=0.10))

        self.assertEqual(state.signal_side, "UP")
        self.assertFalse(state.tracks["strict_50"].initial_filled)
        self.assertFalse(state.tracks["optimistic_touch"].initial_filled)

    def test_opposite_later_cross_sells_held_side_and_buys_opposite_once(self):
        state = MarketState("btc", 0)
        state.process_row(row(150, up_a=0.89, down_a=0.12))
        state.process_row(row(151, up_a=0.90, down_a=0.11))
        state.process_row(row(152, up_a=0.20, up_b=0.19, down_a=0.89))
        state.process_row(
            row(153, up_a=0.11, up_b=0.10, up_bq=50, down_a=0.90, down_aq=50)
        )
        state.process_row(row(154, up_a=0.91, up_b=0.90, down_a=0.10))

        for track in state.tracks.values():
            self.assertTrue(track.switched)
            self.assertEqual(track.switch_side, "DOWN")
            self.assertEqual(track.switch_sell_price, 0.10)
            self.assertEqual(track.switch_buy_price, 0.90)
            self.assertEqual(track.switch_ts, 153)
            self.assertEqual(track.switch_count, 1)

    def test_switch_depth_can_fill_optimistic_then_strict_on_later_cross(self):
        state = MarketState("btc", 0)
        state.process_row(row(150, up_a=0.89, down_a=0.12))
        state.process_row(row(151, up_a=0.90, down_a=0.11))
        state.process_row(row(152, up_a=0.20, up_b=0.19, down_a=0.89))
        state.process_row(
            row(153, up_a=0.11, up_b=0.10, up_bq=10, down_a=0.90, down_aq=10)
        )

        self.assertFalse(state.tracks["strict_50"].switched)
        self.assertTrue(state.tracks["optimistic_touch"].switched)

        state.process_row(row(154, up_a=0.12, up_b=0.11, down_a=0.89))
        state.process_row(
            row(155, up_a=0.11, up_b=0.10, up_bq=50, down_a=0.90, down_aq=50)
        )
        self.assertTrue(state.tracks["strict_50"].switched)
        self.assertEqual(state.tracks["optimistic_touch"].switch_count, 1)

    def test_simultaneous_cross_is_ambiguous_and_missing_or_gap_breaks_continuity(self):
        state = MarketState("btc", 0)
        state.process_row(row(150, up_a=0.89, down_a=0.89))
        state.process_row(row(151, up_a=0.90, down_a=0.90))
        self.assertTrue(state.ambiguous)
        self.assertIsNone(state.signal_side)

        missing = row(152, up_a=0.89, down_a=0.12)
        missing["dn_ask"] = None
        state.process_row(missing)
        state.process_row(row(153, up_a=0.90, down_a=0.11))
        self.assertIsNone(state.signal_side)

        state.process_row(row(154, up_a=0.89, down_a=0.12))
        state.process_row(row(157, up_a=0.90, down_a=0.11))
        self.assertIsNone(state.signal_side)


class AccountingTests(unittest.TestCase):
    def test_switched_trade_accounts_for_three_fee_legs_and_rescues_loser(self):
        state = MarketState("btc", 0)
        state.process_row(row(150, up_a=0.89, down_a=0.12))
        state.process_row(row(151, up_a=0.90, down_a=0.11))
        state.process_row(row(152, up_a=0.20, up_b=0.19, down_a=0.89))
        state.process_row(row(153, up_a=0.11, up_b=0.10, down_a=0.90))
        state.process_row(row(299, up_a=0.10, up_b=0.09, down_a=0.91))
        outcome = {"winner": "DOWN", "fee_rate": 0.07, "fee_exponent": 1.0}

        record = switch.trade_record(state, "strict_50", outcome)

        self.assertAlmostEqual(record["initial_buy_fee_per_share"], 0.0063)
        self.assertAlmostEqual(record["switch_sell_fee_per_share"], 0.0063)
        self.assertAlmostEqual(record["switch_buy_fee_per_share"], 0.0063)
        self.assertAlmostEqual(record["hold_net_pnl_50"], -45.315)
        self.assertAlmostEqual(record["strategy_net_pnl_50"], -35.945)
        self.assertAlmostEqual(record["incremental_switch_pnl_50"], 9.37)
        self.assertEqual(record["switch_effect"], "rescued")
        self.assertEqual(record["final_side"], "DOWN")
        self.assertTrue(record["strategy_won"])

    def test_switch_harms_trade_when_original_side_recovers(self):
        state = MarketState("btc", 0)
        state.process_row(row(150, up_a=0.89, down_a=0.12))
        state.process_row(row(151, up_a=0.90, down_a=0.11))
        state.process_row(row(152, up_a=0.20, up_b=0.19, down_a=0.89))
        state.process_row(row(153, up_a=0.11, up_b=0.10, down_a=0.90))
        state.process_row(row(299, up_a=0.91, up_b=0.90, down_a=0.10))

        record = switch.trade_record(
            state,
            "optimistic_touch",
            {"winner": "UP", "fee_rate": 0.07, "fee_exponent": 1.0},
        )

        self.assertEqual(record["switch_effect"], "harmed")
        self.assertGreater(record["hold_net_pnl_50"], 0)
        self.assertLess(record["strategy_net_pnl_50"], record["hold_net_pnl_50"])
        self.assertFalse(record["strategy_won"])


class RunnerTests(unittest.TestCase):
    def test_runner_filters_universe_excludes_incomplete_and_reconciles_artifacts(self):
        rows = [
            row(1050, mkt_ts=900, symbol="btc", up_a=0.89, down_a=0.12),
            row(1051, mkt_ts=900, symbol="btc", up_a=0.90, down_a=0.11),
            row(1052, mkt_ts=900, symbol="btc", up_a=0.20, up_b=0.19, down_a=0.89),
            row(1053, mkt_ts=900, symbol="btc", up_a=0.11, up_b=0.10, down_a=0.90),
            row(1199, mkt_ts=900, symbol="btc", up_a=0.10, up_b=0.09, down_a=0.91),
            row(1350, mkt_ts=1200, symbol="eth", up_a=0.89, down_a=0.12),
            row(1351, mkt_ts=1200, symbol="eth", up_a=0.90, down_a=0.11),
            row(1499, mkt_ts=1200, symbol="eth", up_a=0.91, down_a=0.10),
            row(1650, mkt_ts=1500, symbol="sol", up_a=0.89, down_a=0.12),
            row(1651, mkt_ts=1500, symbol="sol", up_a=0.90, down_a=0.11),
            row(1050, mkt_ts=900, symbol="doge", up_a=0.89, down_a=0.12),
        ]
        outcomes = {
            ("btc", 900): {"winner": "DOWN", "fee_rate": 0.07, "fee_exponent": 1.0},
            ("eth", 1200): {"winner": "UP", "fee_rate": 0.07, "fee_exponent": 1.0},
            ("sol", 1500): {"winner": "UP", "fee_rate": 0.07, "fee_exponent": 1.0},
        }

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data_dir = root / "data"
            out_dir = root / "out"
            data_dir.mkdir()
            with (data_dir / "sample.jsonl").open("w") as handle:
                for item in rows:
                    handle.write(json.dumps(item) + "\n")

            summary = switch.run_backtest(data_dir, out_dir, outcomes)
            with (out_dir / "summary.json").open() as handle:
                written_summary = json.load(handle)
            with (out_dir / "trades.csv").open(newline="") as handle:
                trades = list(csv.DictReader(handle))

        self.assertEqual(summary, written_summary)
        self.assertEqual(summary["markets"], {"seen": 3, "complete": 2, "incomplete": 1})
        self.assertEqual(summary["coverage"]["unsupported_symbol_rows"], 1)
        self.assertEqual(summary["artifacts"], {"summary": "summary.json", "trades": "trades.csv"})
        self.assertEqual(len(trades), 6)
        for model in switch.EXECUTION_MODELS:
            stats = summary["models"][model]
            complete = [r for r in trades if r["model"] == model and r["complete"] == "True"]
            fills = [r for r in complete if r["initial_filled"] == "True"]
            switched = [r for r in fills if r["switched"] == "True"]
            self.assertEqual(stats["complete_markets"], len(complete))
            self.assertEqual(stats["initial_fills"], len(fills))
            self.assertEqual(stats["switches"], len(switched))
            self.assertEqual(stats["rescued_initial_losses"], 1)
            self.assertEqual(stats["harmed_initial_winners"], 0)
            self.assertAlmostEqual(
                stats["hold_net_pnl_total_50"],
                sum(float(r["hold_net_pnl_50"]) for r in fills),
            )
            self.assertAlmostEqual(
                stats["strategy_net_pnl_total_50"],
                sum(float(r["strategy_net_pnl_50"]) for r in fills),
            )
            self.assertAlmostEqual(stats["incremental_switch_pnl_total_50"], 9.37)


if __name__ == "__main__":
    unittest.main()
