from __future__ import annotations

import asyncio
import contextlib
import io
import json
import sqlite3
import tempfile
import unittest
from decimal import Decimal as D
from pathlib import Path
from unittest.mock import Mock

from paper_bot.config import load_settings
from paper_bot.cli import main as cli_main
from paper_bot.domain import BookLevel, FeeSchedule
from paper_bot.engine import PaperEngine
from paper_bot.gamma import MarketDefinition
from paper_bot.journal import RawJournal
from paper_bot.market_ws import MarketDelta, MarketInvalidation, MarketSnapshot
from paper_bot.rtds import ResolverObservation
from paper_bot.storage import Storage


def market(symbol="btc", mkt_ts=900, suffix="1"):
    return MarketDefinition(
        symbol=symbol, slug=f"{symbol}-updown-5m-{mkt_ts}", market_id=f"{symbol}-market-{suffix}",
        mkt_ts=mkt_ts, end_ts=mkt_ts + 300,
        up_token_id=f"{symbol}-up-{suffix}", down_token_id=f"{symbol}-down-{suffix}",
        tick_size=D("0.01"), min_order_shares=D("1"),
        fee_schedule=FeeSchedule(D("0"), D("1")),
    )


class FakeGamma:
    def __init__(self, definitions):
        self.definitions = tuple(definitions)
        self.calls = []

    async def discover_current_and_next(self, symbols, now, *, include_closed=False):
        self.calls.append((tuple(symbols), now, include_closed))
        return tuple(
            item for item in self.definitions
            if item.symbol in symbols and (item.end_ts > now or include_closed)
        )


class RecoveringGamma(FakeGamma):
    def __init__(self, definitions):
        super().__init__(definitions)
        self.failures = 1

    async def discover_current_and_next(self, symbols, now, *, include_closed=False):
        if self.failures:
            self.failures -= 1
            raise OSError("injected private discovery detail")
        return await super().discover_current_and_next(
            symbols, now, include_closed=include_closed,
        )


def snapshot(token, bids, asks, ts, sequence):
    payload = {
        "event_type": "book", "asset_id": token, "timestamp": str(ts),
        "bids": [{"price": str(price), "size": str(size)} for price, size in bids],
        "asks": [{"price": str(price), "size": str(size)} for price, size in asks],
    }
    return MarketSnapshot(
        token, tuple(BookLevel(D(str(p)), D(str(s))) for p, s in bids),
        tuple(BookLevel(D(str(p)), D(str(s))) for p, s in asks), ts, sequence, payload,
    )


def delta(token, side, price, shares, ts, sequence, index=0, size=1, batch_id=None):
    payload = {
        "event_type": "price_change", "timestamp": str(ts),
        "price_changes": [{"asset_id": token, "side": "BUY" if side == "bid" else "SELL",
                           "price": str(price), "size": str(shares)}],
    }
    return MarketDelta(
        token, side, D(str(price)), D(str(shares)), ts, sequence, payload,
        index, size, sequence if batch_id is None else batch_id,
    )


def settlement_payload(definition, winner, *, final=True):
    prices = ["1", "0"] if winner == "UP" else ["0", "1"]
    if not final:
        prices = ["0.9995", "0.0005"]
    return {
        "slug": definition.slug, "closed": True,
        "outcomes": json.dumps(["Up", "Down"]),
        "outcomePrices": json.dumps(prices),
    }


class EngineReplayTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.settings = load_settings({"DATA_DIR": str(self.root)})
        self.now = [1100]
        self.now_ms = [1_100_000]
        self.resources = []

    async def asyncTearDown(self):
        for storage, journal in reversed(self.resources):
            storage.close()
            journal.close()
        self.temp.cleanup()

    async def make_engine(self, definitions, *, db_name="paper.db", journal_name="raw", **kwargs):
        storage = Storage(self.root / db_name)
        journal = kwargs.pop("journal", RawJournal(self.root / journal_name, min_free_bytes=0))
        self.resources.append((storage, journal))
        engine = PaperEngine(
            self.settings, gamma=FakeGamma(definitions), storage=storage, journal=journal,
            clock_s=lambda: self.now[0], clock_ms=lambda: self.now_ms[0], **kwargs,
        )
        await engine.initialize()
        return engine

    async def seed_books(self, engine, definition, *, up_ask="0.79", down_ask="0.20"):
        await engine.process_market_event(snapshot(
            definition.up_token_id, (("0.78", "20"),), ((up_ask, "20"),),
            1_100_000, 1,
        ))
        await engine.process_market_event(snapshot(
            definition.down_token_id, (("0.19", "20"),), ((down_ask, "20"),),
            1_100_000, 1,
        ))

    async def cross_up(self, engine, definition, *, liquidity="20"):
        batch_id = 10
        await engine.process_market_event(delta(
            definition.up_token_id, "ask", "0.79", "0", 1_100_100, 10, 0, 2, batch_id,
        ))
        await engine.process_market_event(delta(
            definition.up_token_id, "ask", "0.80", liquidity, 1_100_100, 11, 1, 2, batch_id,
        ))

    async def test_journal_first_batch_replay_and_partial_entry_reconcile(self):
        definition = market()
        engine = await self.make_engine((definition,))
        await self.seed_books(engine, definition)
        await self.cross_up(engine, definition, liquidity="2")

        db = engine.storage.db
        self.assertEqual(db.execute("SELECT COUNT(*) FROM signals").fetchone()[0], 3)
        self.assertEqual(db.execute("SELECT COUNT(*) FROM paper_orders WHERE status='partial'").fetchone()[0], 3)
        self.assertEqual(db.execute("SELECT COUNT(*) FROM inventory_lots WHERE shares='2'").fetchone()[0], 3)
        rows = sum(path.read_text().count("\n") for path in (self.root / "raw").glob("*.jsonl"))
        self.assertEqual(rows, 3, "two snapshots plus one raw price-change batch")

    async def test_journal_critical_pauses_entries_but_books_and_resolver_keep_monitoring(self):
        definition = market()
        low_disk = RawJournal(
            self.root / "low-disk", min_free_bytes=1,
            disk_usage=lambda _path: type("Usage", (), {"free": 0})(),
        )
        engine = await self.make_engine((definition,), journal=low_disk)
        await self.seed_books(engine, definition)
        observation = ResolverObservation(
            "btc", D("100"), 1_100_000, 1_100_000,
            {"topic": "crypto_prices_twap_sixty", "type": "update", "payload": {
                "symbol": "btc/usd", "full_accuracy_value": "100000000000000000000",
                "timestamp": 1_100_000, "window_s": 60,
            }},
        )
        await engine.process_resolver_event(observation)
        await self.cross_up(engine, definition)
        self.assertTrue(engine.books[definition.up_token_id].valid)
        self.assertEqual(engine.resolver.view("btc", definition.mkt_ts, 1_100_000).current, D("100"))
        self.assertEqual(engine.storage.db.execute("SELECT COUNT(*) FROM signals").fetchone()[0], 0)
        self.assertFalse(engine.journal.writable())

    async def test_disconnect_invalidates_open_market_without_losing_position(self):
        definition = market()
        engine = await self.make_engine((definition,))
        await self.seed_books(engine, definition)
        await self.cross_up(engine, definition)
        self.assertEqual(len(engine.positions[definition.market_id]), 3)
        await engine.process_market_event(MarketInvalidation(
            (definition.up_token_id, definition.down_token_id)
        ))
        self.assertFalse(engine.books[definition.up_token_id].valid)
        self.assertEqual(len(engine.positions[definition.market_id]), 3)
        self.assertEqual(engine.storage.db.execute("SELECT COUNT(*) FROM signals").fetchone()[0], 3)

    async def test_restart_restores_entry_evidence_then_reverse_is_one_shot(self):
        definition = market()
        first = await self.make_engine((definition,), db_name="restart.db", journal_name="restart-raw")
        await self.seed_books(first, definition)
        await self.cross_up(first, definition)
        first.storage.close()
        first.journal.close()

        second = await self.make_engine((definition,), db_name="restart.db", journal_name="restart-raw")
        restored = second.positions[definition.market_id]
        self.assertEqual(len(restored), 3)
        self.assertTrue(all(position.entry.fak.filled_shares == D("6.25") for position in restored.values()))
        await self.seed_books(second, definition)
        batch = 20
        await second.process_market_event(delta(
            definition.down_token_id, "ask", "0.20", "0", 1_100_200, 20, 0, 2, batch,
        ))
        await second.process_market_event(delta(
            definition.down_token_id, "ask", "0.89", "20", 1_100_200, 21, 1, 2, batch,
        ))
        reverse_count = second.storage.db.execute(
            "SELECT COUNT(*) FROM signals WHERE phase='REVERSE'"
        ).fetchone()[0]
        self.assertEqual(reverse_count, 1)
        second.storage.close()
        second.journal.close()

        third = await self.make_engine((definition,), db_name="restart.db", journal_name="restart-raw")
        self.assertEqual(sum(position.reverse is not None for position in third.positions[definition.market_id].values()), 1)
        reverse = next(position.reverse for position in third.positions[definition.market_id].values()
                       if position.reverse is not None)
        self.assertEqual(reverse.outcome, "FULL")
        await self.seed_books(third, definition, down_ask="0.89")
        await third.process_market_event(delta(
            definition.down_token_id, "ask", "0.89", "19", 1_100_300, 30,
        ))
        self.assertEqual(third.storage.db.execute(
            "SELECT COUNT(*) FROM signals WHERE phase='REVERSE'"
        ).fetchone()[0], 1)

    async def test_restart_rediscovers_closed_market_before_settlement(self):
        definition = market()
        first = await self.make_engine(
            (definition,), db_name="closed-restart.db", journal_name="closed-restart-raw",
        )
        await self.seed_books(first, definition)
        await self.cross_up(first, definition)
        first.storage.close()
        first.journal.close()

        self.now[0] = definition.end_ts + 60
        self.now_ms[0] = self.now[0] * 1000
        second = await self.make_engine(
            (definition,), db_name="closed-restart.db", journal_name="closed-restart-raw",
        )
        self.assertEqual(len(second.positions[definition.market_id]), 3)
        self.assertIn((self.settings.symbols, definition.mkt_ts, True), second.gamma.calls)

    async def test_successful_reverse_rescues_loss_and_hold_lanes_remain_losses(self):
        definition = market()

        async def fetch(_market):
            return settlement_payload(definition, "DOWN")

        engine = await self.make_engine((definition,), db_name="rescue.db", settlement_fetcher=fetch)
        await self.seed_books(engine, definition, down_ask="0.89")
        await self.cross_up(engine, definition)
        await engine.process_market_event(delta(
            definition.down_token_id, "bid", "0.19", "19", 1_100_200, 40,
        ))
        self.now[0] = 1201
        await engine.reconcile_settlements()
        classifications = [row[0] for row in engine.storage.db.execute(
            "SELECT json_extract(result_json,'$.classification') FROM lane_results "
            "ORDER BY json_extract(result_json,'$.classification')"
        )]
        self.assertEqual(classifications, ["hold", "hold", "rescued_loss"])
        pnls = [D(row[0]) for row in engine.storage.db.execute(
            "SELECT net_pnl FROM lane_results ORDER BY net_pnl"
        )]
        self.assertLess(pnls[0], 0)
        self.assertGreater(pnls[-1], 0)

    async def test_false_reverse_is_classified_when_initial_side_wins(self):
        definition = market()

        async def fetch(_market):
            return settlement_payload(definition, "UP")

        engine = await self.make_engine((definition,), db_name="false.db", settlement_fetcher=fetch)
        await self.seed_books(engine, definition, down_ask="0.89")
        await self.cross_up(engine, definition)
        await engine.process_market_event(delta(
            definition.down_token_id, "bid", "0.19", "19", 1_100_200, 50,
        ))
        self.now[0] = 1201
        await engine.reconcile_settlements()
        row = engine.storage.db.execute(
            "SELECT json_extract(result_json,'$.classification'),"
            "json_extract(result_json,'$.false_reverse'),"
            "json_extract(result_json,'$.harmed_winner') FROM lane_results "
            "WHERE json_extract(result_json,'$.classification')!='hold'"
        ).fetchone()
        self.assertEqual(tuple(row), ("harmed_winner", 1, 1))

    async def test_provisional_then_final_settlement_survives_restart(self):
        definition = market()
        responses = [settlement_payload(definition, "UP", final=False), settlement_payload(definition, "UP")]

        async def fetch(_market):
            return responses.pop(0)

        engine = await self.make_engine((definition,), db_name="settle.db", settlement_fetcher=fetch)
        await self.seed_books(engine, definition)
        await self.cross_up(engine, definition)
        self.now[0] = 1201
        await engine.reconcile_settlements()
        self.assertEqual(engine.storage.db.execute("SELECT COUNT(*) FROM settlements").fetchone()[0], 0)
        await engine.reconcile_settlements()
        self.assertEqual(engine.storage.db.execute("SELECT COUNT(*) FROM settlements").fetchone()[0], 1)
        self.assertEqual(engine.storage.db.execute("SELECT COUNT(*) FROM lane_results").fetchone()[0], 3)
        pnls = [D(row[0]) for row in engine.storage.db.execute("SELECT net_pnl FROM lane_results")]
        self.assertTrue(all(value > 0 for value in pnls))
        engine.storage.close()
        engine.journal.close()
        restarted = await self.make_engine((definition,), db_name="settle.db", settlement_fetcher=fetch)
        self.assertNotIn(definition.market_id, restarted.positions)

    async def test_three_assets_and_current_next_markets_remain_isolated(self):
        definitions = (
            market("btc", suffix="current"), market("btc", mkt_ts=1200, suffix="next"),
            market("eth", suffix="current"), market("sol", suffix="current"),
        )
        engine = await self.make_engine(definitions)
        btc = definitions[0]
        await self.seed_books(engine, btc)
        await self.cross_up(engine, btc)
        markets_with_signals = {
            row[0] for row in engine.storage.db.execute("SELECT DISTINCT market_id FROM signals")
        }
        self.assertEqual(markets_with_signals, {btc.market_id})
        self.assertEqual(set(engine.token_ids()), {
            token for definition in definitions
            for token in (definition.up_token_id, definition.down_token_id)
        })

    async def test_run_has_bounded_queue_named_tasks_and_stops_cleanly(self):
        definition = market()
        engine = await self.make_engine(
            (definition,), queue_maxsize=7,
            discovery_interval=3600, settlement_interval=3600, heartbeat_interval=3600,
        )
        run = asyncio.create_task(engine.run())
        for _ in range(100):
            if engine._running:
                break
            await asyncio.sleep(0)
        self.assertEqual(engine.queue.maxsize, 7)
        self.assertEqual({task.get_name() for task in engine._tasks}, {
            "event-processor", "market-discovery", "settlement-poller", "engine-heartbeat",
        })
        engine.stop()
        await run
        self.assertFalse(engine._running)

    async def test_discovery_supervisor_contains_transient_failure_then_recovers(self):
        definition = market()
        engine = await self.make_engine(
            (definition,), discovery_interval=0.001,
            settlement_interval=3600, heartbeat_interval=3600,
        )
        recovering = RecoveringGamma((definition,))
        engine.gamma = recovering
        run = asyncio.create_task(engine.run())
        try:
            for _ in range(100):
                if recovering.failures == 0 and engine.discovery_critical_reason is None:
                    break
                await asyncio.sleep(0.001)
            self.assertFalse(run.done())
            self.assertEqual(recovering.failures, 0)
            self.assertIsNone(engine.discovery_critical_reason)
        finally:
            engine.stop()
            await run

    async def test_settlement_supervisor_contains_transient_failure_then_recovers(self):
        definition = market()
        responses = [OSError("injected private settlement detail"), settlement_payload(definition, "UP")]

        async def fetch(_market):
            response = responses.pop(0)
            if isinstance(response, Exception):
                raise response
            return response

        engine = await self.make_engine(
            (definition,), settlement_fetcher=fetch, discovery_interval=3600,
            settlement_interval=0.001, heartbeat_interval=3600,
        )
        await self.seed_books(engine, definition)
        await self.cross_up(engine, definition)
        self.now[0] = 1201
        run = asyncio.create_task(engine.run())
        try:
            for _ in range(100):
                if engine.storage.db.execute("SELECT COUNT(*) FROM settlements").fetchone()[0]:
                    break
                await asyncio.sleep(0.001)
            self.assertFalse(run.done())
            self.assertEqual(responses, [])
            self.assertIsNone(engine.settlement_critical_reason)
            self.assertEqual(engine.storage.db.execute(
                "SELECT COUNT(*) FROM settlements"
            ).fetchone()[0], 1)
        finally:
            engine.stop()
            await run

    async def test_transient_sqlite_lock_retries_same_immutable_event_batch(self):
        waits = []

        async def sleep(delay):
            waits.append(delay)

        engine = await self.make_engine((market(),), sleep=sleep)
        original = engine.storage.record_strategy_events
        recorder = Mock(side_effect=[sqlite3.OperationalError("database is locked"), None])
        engine.storage.record_strategy_events = recorder
        events = (object(),)
        try:
            self.assertTrue(await engine._persist_events(events))
        finally:
            engine.storage.record_strategy_events = original
        self.assertEqual(recorder.call_args_list[0].args[0], events)
        self.assertEqual(recorder.call_args_list[1].args[0], events)
        self.assertEqual(waits, [0.05])
        self.assertIsNone(engine.storage_critical_reason)

    async def test_exhausted_lock_pauses_then_heartbeat_retry_persists_same_batch(self):
        async def sleep(_delay):
            return None

        definition = market()
        engine = await self.make_engine((definition,), db_name="retry.db", sleep=sleep)
        await self.seed_books(engine, definition)
        original = engine.storage.record_strategy_events
        calls = [0]

        def locked_then_recover(events):
            calls[0] += 1
            if calls[0] <= 3:
                raise sqlite3.OperationalError("database is locked")
            return original(events)

        engine.storage.record_strategy_events = locked_then_recover
        try:
            await self.cross_up(engine, definition)
            self.assertEqual(engine.storage.db.execute("SELECT COUNT(*) FROM signals").fetchone()[0], 0)
            self.assertEqual(engine.positions[definition.market_id], {})
            self.assertEqual(engine.storage_critical_reason, "sqlite_lock_retry_exhausted")
            await engine._retry_pending_storage()
        finally:
            engine.storage.record_strategy_events = original
        self.assertEqual(calls[0], 4)
        self.assertEqual(engine.storage.db.execute("SELECT COUNT(*) FROM signals").fetchone()[0], 3)
        self.assertEqual(len(engine.positions[definition.market_id]), 3)
        self.assertIsNone(engine.storage_critical_reason)

    async def test_processor_error_pauses_entries_but_event_loop_keeps_monitoring(self):
        definition = market()
        engine = await self.make_engine((definition,))
        await self.seed_books(engine, definition)
        original = engine.process_market_event
        first = [True]

        async def injected(event):
            if first[0]:
                first[0] = False
                raise ValueError("injected sensitive processing detail")
            await original(event)

        engine.process_market_event = injected
        loop = asyncio.create_task(engine._event_loop())
        try:
            await engine.queue.put(object())
            await engine.queue.put(MarketInvalidation((definition.up_token_id,)))
            await engine.queue.join()
        finally:
            loop.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await loop
            engine.process_market_event = original
        self.assertEqual(engine.processing_critical_reason, "ValueError")
        self.assertFalse(engine.books[definition.up_token_id].valid)
        self.assertNotIn("sensitive", engine.processing_critical_reason)

    async def test_rollover_retires_expired_empty_market_and_keeps_next(self):
        current = market(suffix="current")
        upcoming = market(mkt_ts=1200, suffix="next")
        engine = await self.make_engine((current, upcoming))
        self.now[0] = 1201
        await engine.discover_markets()
        tokens = set(engine.token_ids())
        self.assertNotIn(current.up_token_id, tokens)
        self.assertNotIn(current.down_token_id, tokens)
        self.assertIn(upcoming.up_token_id, tokens)
        self.assertIn(upcoming.down_token_id, tokens)

    async def test_cli_is_public_only_and_read_only_commands_close_database(self):
        definition = market()
        engine = await self.make_engine((definition,), db_name="paper.db")
        engine.storage.close()
        engine.journal.close()
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(cli_main(["status"], environment={"DATA_DIR": str(self.root)}), 0)
            self.assertEqual(cli_main(["check-db"], environment={"DATA_DIR": str(self.root)}), 0)
        self.assertIn('"markets":0', output.getvalue())
        self.assertTrue(output.getvalue().rstrip().endswith("ok"))
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            cli_main(["trade"], environment={})

    async def test_dashboard_snapshot_contains_live_books_resolver_and_public_health(self):
        definition = market()
        engine = await self.make_engine((definition,), db_name="dashboard.db")
        await self.seed_books(engine, definition)
        await engine.process_resolver_event(ResolverObservation(
            "btc", D("100"), 900_000, 900_000,
            {"topic": "crypto_prices_twap_sixty", "type": "update", "payload": {
                "symbol": "btc/usd", "full_accuracy_value": "100000000000000000000",
                "timestamp": 900_000, "window_s": 60,
            }},
        ))
        await engine.process_resolver_event(ResolverObservation(
            "btc", D("101"), 1_100_000, 1_100_000,
            {"topic": "crypto_prices_twap_sixty", "type": "update", "payload": {
                "symbol": "btc/usd", "full_accuracy_value": "101000000000000000000",
                "timestamp": 1_100_000, "window_s": 60,
            }},
        ))
        engine._write_dashboard_snapshot()
        snapshot = engine.storage.load_dashboard_snapshot()
        self.assertEqual(snapshot["markets"][0]["symbol"], "btc")
        self.assertEqual(snapshot["markets"][0]["slug"], definition.slug)
        self.assertEqual(snapshot["markets"][0]["books"]["UP"]["best_ask"], "0.79")
        self.assertEqual(snapshot["markets"][0]["books"]["DOWN"]["ask_depth"], "20")
        self.assertEqual(snapshot["resolver"][0]["start"], "100")
        self.assertEqual(snapshot["resolver"][0]["current"], "101")
        self.assertTrue(snapshot["health"]["journal_writable"])
        self.assertIsInstance(snapshot["health"]["disk_free_bytes"], int)

    async def test_transient_dashboard_write_failure_gates_then_recovers(self):
        engine = await self.make_engine((market(),), db_name="dashboard-retry.db")
        original = engine.storage.write_dashboard_snapshot
        first = [True]

        def fail_once(payload, timestamp):
            if first[0]:
                first[0] = False
                raise OSError("injected private dashboard detail")
            return original(payload, timestamp)

        engine.storage.write_dashboard_snapshot = fail_once
        try:
            engine._write_dashboard_snapshot()
            self.assertEqual(engine.dashboard_critical_reason, "OSError")
            self.assertFalse(engine._strategy_writable(True))
            engine._write_dashboard_snapshot()
        finally:
            engine.storage.write_dashboard_snapshot = original
        self.assertIsNone(engine.dashboard_critical_reason)
        self.assertTrue(engine._strategy_writable(True))


if __name__ == "__main__":
    unittest.main()
