from __future__ import annotations

import asyncio
import ast
import contextlib
import io
import json
import sqlite3
import tempfile
import unittest
from decimal import Decimal as D
from pathlib import Path
from unittest.mock import patch

from rich.console import Console

from paper_bot.accounting import LanePosition, settle_lane
from paper_bot import cli
from paper_bot.cli import main as cli_main
from paper_bot.config import load_settings
from paper_bot.domain import FakResult, FillLeg
from paper_bot.monte_carlo import MODEL_VERSION, MonteCarloForecastEvent
from paper_bot.settlement import OfficialSettlement
from paper_bot.storage import Storage
from paper_bot.strategy import Confirmation, LaneKey, PositionPolicy, StrategyEvent
from paper_bot.tui import PaperDashboard


def fak(status: str, shares: str) -> FakResult:
    filled = D(shares)
    quote, fee = filled * D("0.8"), filled * D("0.02")
    requested = D("4")
    legs = () if filled == 0 else (FillLeg(D("0.8"), filled, quote, fee),)
    return FakResult(
        requested_quote=requested, requested_shares=None,
        submitted_maker_amount=requested, submitted_taker_amount=D("5"),
        filled_shares=filled, quote_amount=quote,
        unfilled_quote=requested - quote, unfilled_shares=D("5") - filled,
        fee=fee, legs=legs, status=status,
    )


class DashboardTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "paper.db"
        self.storage = Storage(self.path)
        self.storage.initialize()
        settings = load_settings({"DATA_DIR": self.tmp.name})
        self.settings = settings
        experiment_hash = self.storage.ensure_experiment(settings)
        self.experiment_hash = experiment_hash
        lane = LaneKey(D("0.80"), Confirmation.BOOK_ONLY, PositionPolicy.HOLD)
        definitions = (
            ("btc", "btc-market", 1_000, "full", "5"),
            ("eth", "eth-market", 1_000, "full", "5"),
            ("sol", "sol-market", 1_000, "partial", "2"),
            ("sol", "sol-next", 1_300, "zero", "0"),
        )
        self.entries = []
        for index, (symbol, market_id, mkt_ts, status, shares) in enumerate(definitions):
            event = StrategyEvent(
                lane, "entry_attempt", market_id, mkt_ts, f"{symbol}-up-{index}", "UP",
                1_100_000 + index, 1, experiment_hash, fak(status, shares),
            )
            self.storage.record_strategy_events((event,))
            self.entries.append(event)
        self.storage.record_settlement(
            "eth-market", OfficialSettlement("UP", 1_301),
            (settle_lane(LanePosition("eth-market", 1_300, lane, experiment_hash, self.entries[1]),
                         OfficialSettlement("UP", 1_301)),),
        )
        self.storage.record_settlement(
            "sol-market", OfficialSettlement("DOWN", 1_302),
            (settle_lane(LanePosition("sol-market", 1_300, lane, experiment_hash, self.entries[2]),
                         OfficialSettlement("DOWN", 1_302)),),
        )
        market_rows = []
        for index, (symbol, market_id, mkt_ts, _status, _shares) in enumerate(definitions):
            market_rows.append({
                "market_id": market_id, "symbol": symbol, "slug": f"{symbol}-updown-5m-{mkt_ts}",
                "mkt_ts": mkt_ts, "close_ts": mkt_ts + 300,
                "status": "SETTLED" if market_id in {"eth-market", "sol-market"} else "OPEN",
                "inactive": market_id in {"eth-market", "sol-market"},
                "books": {
                    "UP": {"valid": True, "generation": index + 1, "best_bid": D("0.79"),
                           "best_ask": D("0.80"), "bid_depth": D("20"), "ask_depth": D("18")},
                    "DOWN": {"valid": symbol != "sol", "generation": index + 1,
                             "best_bid": D("0.19"), "best_ask": D("0.20"),
                             "bid_depth": D("15"), "ask_depth": D("14")},
                },
            })
        resolver = [{
            "symbol": symbol, "market_id": f"{symbol}-market", "start": D("100"),
            "current": D("101"), "observation_ts_ms": 1_199_000, "age_ms": 1_000,
            "fresh": True, "distance": D("1"), "distance_bps": D("100"),
            "leader": "UP", "momentum_5s_bps": D("5"),
        } for symbol in ("btc", "eth", "sol")]
        self.storage.write_dashboard_snapshot({
            "version": 1, "experiment_hash": experiment_hash, "markets": market_rows,
            "resolver": resolver,
            "health": {"storage": None, "dashboard": None, "processing": None, "discovery": None,
                       "settlement": None, "journal_writable": True, "journal_reason": None,
                       "disk_free_bytes": 500_000_000, "disk_min_free_bytes": 100_000_000,
                       "pending_storage": False},
        }, 1_200_000)
        assert self.storage.db is not None
        self.storage.db.execute(
            "INSERT INTO health_events(event_ts_ms,kind,payload_json) VALUES (?,?,?)",
            (1_199_000, "market_reconnect", json.dumps({"status": "recovered"})),
        )
        self.storage.close()

    def tearDown(self) -> None:
        self.storage.close()
        self.tmp.cleanup()

    def rendered(self, *, clock_s: int = 1_200, **filters: str) -> str:
        dashboard = PaperDashboard(self.path, clock_s=lambda: clock_s, **filters)
        console = Console(record=True, width=300, height=200, file=io.StringIO())
        console.print(dashboard.render())
        output = console.export_text()
        dashboard.close()
        return output

    def test_populated_dashboard_renders_live_accounting_and_health(self) -> None:
        output = self.rendered()
        for expected in (
            "PAPER ONLY · NO ORDERS", "OVERVIEW", "Market pulse", "BTC", "SOL", "UP bid / ask",
            "CL leader", "Our signals", "observational", "BASE ONLY UP",
            "Open paper inventory", "round UTC", "ACTIVE POSITION", "virtual fills only",
            "System state", "market books",
            "Chainlink resolver", "database", "disk / journal",
        ):
            with self.subTest(expected=expected):
                self.assertIn(expected, output)

    def test_quality_shadow_views_render_cards_timeline_and_forward_metrics(self) -> None:
        storage = Storage(self.path)
        storage.initialize()
        snapshot = storage.load_dashboard_snapshot()
        snapshot["mode"] = "QUALITY_SHADOW_ONLY"
        snapshot["quality_shadow"] = [{
            "version": 1, "market_id": "btc-market", "mkt_ts": 1_000,
            "symbol": "btc", "stage": "ENTERED", "reason": None,
            "last_age": 200, "last_recorded_age": 200,
            "selected_side": "UP", "p30": "0.64", "entry_ask": "0.90",
            "filter_a": False, "filter_b": False, "repair_run": 2,
            "switch_age": None,
            "trail": [
                {"age": age, "up_bid": str(price - D("0.01")), "up_ask": str(price),
                 "down_bid": str(D("0.99") - price), "down_ask": str(D("1.01") - price)}
                for age, price in ((196, D("0.86")), (197, D("0.88")),
                                   (198, D("0.87")), (199, D("0.91")), (200, D("0.90")))
            ],
        }]
        snapshot["quality_results"] = [{
            "stage": "SETTLED", "market_id": "eth-old", "mkt_ts": 700,
            "symbol": "eth", "selected_side": "DOWN", "p30": "0.70",
            "entry_ask": "0.93", "switch_age": 168, "winner": "UP",
            "pnl": "-0.032258", "trail": [],
        }, {
            "stage": "SETTLED", "market_id": "btc-filtered", "mkt_ts": 400,
            "symbol": "btc", "selected_side": "UP", "p30": "0.65",
            "entry_ask": None, "switch_age": None, "winner": "UP",
            "pnl": None, "reason": "entry_ask_below_0.88", "trail": [],
        }]
        storage.write_dashboard_snapshot(snapshot, 1_200_000)
        storage.close()

        overview = self.rendered()
        for expected in ("QUALITY SHADOW · NO ORDERS", "ASK TREND", "Live decision rail",
                         "30s SIGNAL", "REPAIR WINDOW", "watch 2/3"):
            self.assertIn(expected, overview)
        performance = self.rendered(view="performance")
        for expected in ("SIGNAL HIT", "ENTRIES", "REPAIRS", "PAPER NET",
                         "BTC statistics", "ETH statistics", "SOL statistics", "AVG ENTRY",
                         "TRADE WIN", "SKIPPED HIT", "30s OUTCOMES", "P&L CURVE",
                         "Recent settled decisions", "SWITCH @168s", "SKIP · HIT"):
            self.assertIn(expected, performance)
        activity = self.rendered(view="activity")
        self.assertIn("Live strategy timeline", activity)
        self.assertIn("Decision tape", activity)
        for view in ("overview", "performance", "activity"):
            dashboard = PaperDashboard(self.path, view=view, clock_s=lambda: 1_200)
            console = Console(record=True, width=120, height=40, file=io.StringIO())
            console.print(dashboard.render())
            fixed = console.export_text()
            dashboard.close()
            self.assertTrue(fixed.strip())
            self.assertLessEqual(max(map(len, fixed.splitlines())), 120)

    def test_closed_unsettled_inventory_is_clearly_labelled(self) -> None:
        output = self.rendered(clock_s=1_400)
        self.assertIn("AWAITING SETTLEMENT", output)
        self.assertIn("closed 01:40 ago", output)
        self.assertIn("#btc-market", output)
        self.assertNotIn("ACTIVE POSITION", output)

    def test_lane_and_asset_filters_are_applied(self) -> None:
        output = self.rendered(
            asset="btc", threshold="0.8", confirmation="BOOK_ONLY", policy="HOLD",
            view="performance",
        )
        self.assertIn("BTC", output)
        self.assertNotIn("eth-updown", output)
        self.assertIn("BOOK_ONLY", output)
        self.assertIn("HOLD", output)

    def test_views_separate_overview_performance_and_activity(self) -> None:
        performance = self.rendered(view="performance")
        self.assertIn("Strategy scoreboard", performance)
        self.assertIn("Monte Carlo outcomes", performance)
        self.assertNotIn("Open paper inventory", performance)

        activity = self.rendered(view="activity")
        self.assertIn("Monte Carlo decision log", activity)
        self.assertIn("Feed and storage timeline", activity)
        self.assertIn("market_reconnect", activity)
        self.assertNotIn("Strategy scoreboard", activity)

    def test_composite_signal_agrees_and_deduplicates_exit_policies(self) -> None:
        storage = Storage(self.path)
        storage.initialize()
        self.assertEqual(storage.ensure_experiment(self.settings), self.experiment_hash)
        for policy in (PositionPolicy.IMMEDIATE_REVERSE, PositionPolicy.CHAINLINK_REVERSE):
            storage.record_strategy_events((StrategyEvent(
                LaneKey(D("0.80"), Confirmation.BOOK_ONLY, policy),
                "entry_attempt", "btc-market", 1_000, "btc-up-0", "UP",
                1_150_000, 1, self.experiment_hash, fak("full", "5"),
            ),))
        storage.record_strategy_events((MonteCarloForecastEvent(
            model_version=MODEL_VERSION, config_hash=self.experiment_hash,
            market_id="btc-market", mkt_ts=1_000, horizon_seconds=90,
            seconds_to_close=90, event_ts_ms=1_210_000, observation_ts_ms=1_209_000,
            side="UP", token_id="btc-up-0", book_generation=1,
            best_ask=D("0.86"), start=D("100"), current=D("101"),
            distance_bps=D("100"), probability=D("0.95"),
            break_even_probability=D("0.90"), edge=D("0.05"),
            history_points=60, simulations=10_000, sign_flips=4,
            mean_abs_step_bps=D("1.2"), decision="ENTER", reason="eligible",
        ),))
        storage.close()

        output = self.rendered()
        self.assertIn("CONFIRMED UP", output)
        self.assertIn("1 UP", output)  # one trigger, not three exit-policy copies
        self.assertIn("1/1 UP", output)
        self.assertIn("0.95", output)
        self.assertIn("0.05", output)

    def test_rejected_monte_carlo_observation_is_rendered_as_skipped_not_loss(self) -> None:
        storage = Storage(self.path)
        storage.initialize()
        self.assertEqual(storage.ensure_experiment(self.settings), self.experiment_hash)
        storage.record_strategy_events((MonteCarloForecastEvent(
            model_version=MODEL_VERSION, config_hash=self.experiment_hash,
            market_id="btc-market", mkt_ts=1_000, horizon_seconds=90,
            seconds_to_close=90, event_ts_ms=1_210_000, observation_ts_ms=1_209_000,
            side=None, token_id=None, book_generation=None, best_ask=D("0.80"),
            start=D("100"), current=D("101"), distance_bps=D("100"),
            probability=None, break_even_probability=None, edge=None,
            history_points=60, simulations=0, sign_flips=None,
            mean_abs_step_bps=None, decision="REJECT", reason="ask_outside_entry_band",
        ),))
        storage.close()

        output = self.rendered(view="activity")
        self.assertIn("REJECT", output)
        self.assertIn("SKIP", output)
        self.assertIn("SKIP means no paper trade", output)
        self.assertNotIn("LOSS", output)

    def test_uses_mode_ro_and_write_attempt_fails(self) -> None:
        before = self.path.read_bytes()
        dashboard = PaperDashboard(self.path)
        dashboard.open()
        assert dashboard._storage is not None and dashboard._storage.db is not None
        self.assertIn("mode=ro", f"file:{dashboard.db_path.absolute()}?mode=ro")
        with self.assertRaises(sqlite3.OperationalError):
            dashboard._storage.db.execute("CREATE TABLE forbidden_write(x INTEGER)")
        dashboard.close()
        self.assertEqual(before, self.path.read_bytes())

    def test_start_stop_twice_closes_connection_and_preserves_state(self) -> None:
        before = self.path.read_bytes()

        async def exercise() -> None:
            dashboard = PaperDashboard(self.path, refresh=0.001, clock_s=lambda: 1_200)
            for _ in range(2):
                with contextlib.redirect_stdout(io.StringIO()):
                    await dashboard.run(iterations=1)
                self.assertIsNone(dashboard._storage)

        asyncio.run(exercise())
        self.assertEqual(before, self.path.read_bytes())

    def test_watch_cli_passes_filters_without_loading_network_adapters_or_environment(self) -> None:
        top_imports = set()
        for node in ast.parse(Path(cli.__file__).read_text()).body:
            if isinstance(node, ast.Import):
                top_imports.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                top_imports.add(node.module)
        forbidden = {"aiohttp", "websockets", "engine", "gamma", "market_ws", "rtds"}
        self.assertTrue(forbidden.isdisjoint(top_imports))

        class ForbiddenEnvironment(dict):
            def __iter__(self):
                raise AssertionError("watch must not inspect environment credentials")

        with patch("paper_bot.tui.watch", return_value=0) as runner:
            self.assertEqual(cli_main([
                "watch", "--db", str(self.path), "--refresh", "2",
                "--view", "performance",
                "--asset", "btc", "--threshold", "0.8",
                "--confirmation", "BOOK_ONLY", "--policy", "HOLD",
            ], environment=ForbiddenEnvironment()), 0)
        runner.assert_called_once_with(
            str(self.path), 2.0, asset="btc", threshold="0.8",
            confirmation="BOOK_ONLY", policy="HOLD", view="performance",
        )


if __name__ == "__main__":
    unittest.main()
