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
        experiment_hash = self.storage.ensure_experiment(settings)
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

    def rendered(self, **filters: str) -> str:
        dashboard = PaperDashboard(self.path, clock_s=lambda: 1_200, **filters)
        console = Console(record=True, width=300, height=200, file=io.StringIO())
        console.print(dashboard.render())
        output = console.export_text()
        dashboard.close()
        return output

    def test_populated_dashboard_renders_live_accounting_and_health(self) -> None:
        output = self.rendered()
        for expected in (
            "PAPER ONLY — NO ORDERS", "BTC", "ETH", "SOL", "countdown", "UP OK", "DOWN INVALID",
            "start", "current", "distance", "leader", "mom 5s", "age", "0.8/BOOK_ONLY/HOLD",
            "full/partial/zero", "2/1/1", "resolved", "1/1", "net PnL", "EV/share", "drawdown",
            "Open old/new inventory", "requested/filled", "VWAP", "payout scenarios",
            "reconnect", "stale", "database", "disk", "market_reconnect",
        ):
            with self.subTest(expected=expected):
                self.assertIn(expected, output)

    def test_lane_and_asset_filters_are_applied(self) -> None:
        output = self.rendered(asset="btc", threshold="0.8", confirmation="BOOK_ONLY", policy="HOLD")
        self.assertIn("BTC", output)
        self.assertNotIn("eth-updown", output)
        self.assertIn("0.8/BOOK_ONLY/HOLD", output)

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
                "--asset", "btc", "--threshold", "0.8",
                "--confirmation", "BOOK_ONLY", "--policy", "HOLD",
            ], environment=ForbiddenEnvironment()), 0)
        runner.assert_called_once_with(
            str(self.path), 2.0, asset="btc", threshold="0.8",
            confirmation="BOOK_ONLY", policy="HOLD",
        )


if __name__ == "__main__":
    unittest.main()
