from __future__ import annotations

import hashlib
import sqlite3
import tempfile
import unittest
from dataclasses import replace
from decimal import Decimal as D
from pathlib import Path

from paper_bot.accounting import LanePosition, settle_lane
from paper_bot.config import load_settings
from paper_bot.domain import FakResult, FillLeg, InventoryLot, ReverseSequence
from paper_bot.settlement import OfficialSettlement
from paper_bot.storage import Storage, StorageInvariantError, TABLES, canonical_decimal
from paper_bot.strategy import Confirmation, LaneKey, PositionPolicy, StrategyEvent


def fak(legs, *, requested_quote=None, requested_shares=None, submitted_maker=None,
        submitted_taker=None, unfilled_quote=None, unfilled_shares=None, status="full"):
    legs = tuple(legs)
    shares = sum((leg.shares for leg in legs), D("0"))
    quote = sum((leg.quote for leg in legs), D("0"))
    fee = sum((leg.fee for leg in legs), D("0"))
    return FakResult(
        requested_quote=requested_quote, requested_shares=requested_shares,
        submitted_maker_amount=submitted_maker if submitted_maker is not None else quote,
        submitted_taker_amount=submitted_taker if submitted_taker is not None else shares,
        filled_shares=shares, quote_amount=quote,
        unfilled_quote=unfilled_quote, unfilled_shares=unfilled_shares,
        fee=fee, legs=legs, status=status,
    )


class StorageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp.name) / "paper.db"
        self.settings = load_settings({"DATA_DIR": self.temp.name})
        self.storage = Storage(self.db_path)
        self.storage.initialize()
        self.experiment_hash = self.storage.ensure_experiment(self.settings)
        self.lane = LaneKey(D("0.80"), Confirmation.BOOK_ONLY, PositionPolicy.IMMEDIATE_REVERSE)
        self.entry_fak = fak(
            (FillLeg(D("0.80"), D("5"), D("4.00"), D("0.10")),),
            requested_quote=D("4.00"), submitted_maker=D("4.00"), submitted_taker=D("5"),
            unfilled_quote=D("0"), unfilled_shares=D("0"),
        )
        self.entry = StrategyEvent(
            lane=self.lane, kind="entry_attempt", market_id="market-1", mkt_ts=1_000,
            token_id="up-token", side="UP", event_ts_ms=1_100_000,
            book_generation=1, config_hash=self.experiment_hash, fak=self.entry_fak,
        )

    def tearDown(self):
        self.storage.close()
        self.temp.cleanup()

    @property
    def db(self):
        assert self.storage.db is not None
        return self.storage.db

    def reverse(self):
        sell = fak(
            (FillLeg(D("0.20"), D("3"), D("0.60"), D("0.03")),),
            requested_shares=D("5"), submitted_maker=D("5"), submitted_taker=D("0.05"),
            unfilled_shares=D("2"), status="partial",
        )
        buy = fak(
            (FillLeg(D("0.90"), D("2"), D("1.80"), D("0.02")),),
            requested_quote=D("2.70"), submitted_maker=D("2.70"), submitted_taker=D("3"),
            unfilled_quote=D("0.90"), unfilled_shares=D("1"), status="partial",
        )
        return ReverseSequence(
            lane=self.lane, market_id="market-1", mkt_ts=1_000,
            config_hash=self.experiment_hash, old_side="UP", new_side="DOWN",
            status="COMPLETE", outcome="PARTIAL_SELL_AND_BUY",
            transitions=("ELIGIBLE", "SELL_ATTEMPTED", "SELL_FILLED_OR_PARTIAL", "BUY_ATTEMPTED", "COMPLETE"),
            requested_shares=D("5"), sold_shares=D("3"), old_residual_shares=D("2"),
            submission_dust_shares=D("0"), opposite_shares=D("2"), expected_quote=D("1.80"),
            sell=sell, buy=buy,
            inventory_lots=(
                InventoryLot("up-token", "UP", D("2"), "reverse_old_residual"),
                InventoryLot("down-token", "DOWN", D("2"), "reverse_buy"),
            ),
            sell_book_generation=1, buy_book_generation=1,
            trigger_ts_ms=1_200_000, leg_elapsed_ms=1,
        )

    def test_schema_enables_wal_foreign_keys_and_all_required_tables(self):
        self.assertEqual(self.db.execute("PRAGMA journal_mode").fetchone()[0].lower(), "wal")
        self.assertEqual(self.db.execute("PRAGMA foreign_keys").fetchone()[0], 1)
        tables = {row[0] for row in self.db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertTrue(set(TABLES).issubset(tables))
        for table in ("markets", "tokens", "signals", "paper_orders", "paper_fill_legs",
                      "inventory_lots", "reverse_sequences", "lane_results"):
            with self.subTest(table=table):
                self.assertTrue(tuple(self.db.execute(f"PRAGMA foreign_key_list({table})")))

    def test_experiment_hash_uses_only_canonical_strategy_settings(self):
        stored = self.db.execute(
            "SELECT settings_json FROM experiment_versions WHERE experiment_hash=?",
            (self.experiment_hash,),
        ).fetchone()[0]
        self.assertEqual(hashlib.sha256(stored.encode()).hexdigest(), self.experiment_hash)
        same_strategy = replace(self.settings, data_dir=Path("/different/operational/path"))
        self.assertEqual(self.storage.ensure_experiment(same_strategy), self.experiment_hash)
        changed = replace(self.settings, paper_notional_usd=D("6.00"))
        self.assertNotEqual(self.storage.ensure_experiment(changed), self.experiment_hash)

    def test_entry_is_atomic_idempotent_and_stores_canonical_decimal_text(self):
        self.storage.record_strategy_events((self.entry,))
        self.storage.record_strategy_events((self.entry,))
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM signals").fetchone()[0], 1)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM paper_orders").fetchone()[0], 1)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM paper_fill_legs").fetchone()[0], 1)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM book_generations").fetchone()[0], 1)
        row = self.db.execute(
            "SELECT filled_shares,quote_amount,fee,typeof(filled_shares) FROM paper_orders"
        ).fetchone()
        self.assertEqual(tuple(row), ("5", "4", "0.1", "text"))
        lot = self.db.execute("SELECT shares,typeof(shares),open FROM inventory_lots").fetchone()
        self.assertEqual(tuple(lot), ("5", "text", 1))

    def test_conflicting_duplicate_signal_is_rejected_without_mutation(self):
        self.storage.record_strategy_events((self.entry,))
        conflict = replace(self.entry, event_ts_ms=self.entry.event_ts_ms + 1)
        with self.assertRaisesRegex(StorageInvariantError, "conflicting duplicate"):
            self.storage.record_strategy_events((conflict,))
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM signals").fetchone()[0], 1)

    def test_batch_rolls_back_market_signal_order_leg_and_inventory_on_bad_totals(self):
        second_lane = replace(self.lane, threshold=D("0.85"))
        broken_fak = replace(self.entry_fak, fee=D("9"))
        broken = replace(self.entry, lane=second_lane, fak=broken_fak)
        with self.assertRaisesRegex(StorageInvariantError, "fill legs"):
            self.storage.record_strategy_events((self.entry, broken))
        for table in ("markets", "tokens", "signals", "paper_orders", "paper_fill_legs", "inventory_lots"):
            with self.subTest(table=table):
                self.assertEqual(self.db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0], 0)

    def test_inventory_constraint_rejects_negative_or_noncanonical_values(self):
        self.storage.record_strategy_events((self.entry,))
        for value in ("-1", "5.00", 5.0):
            with self.subTest(value=value), self.assertRaises(sqlite3.IntegrityError):
                self.db.execute("UPDATE inventory_lots SET shares=?", (value,))

    def test_restart_restores_attempted_lanes_open_positions_and_prevents_duplicate_entry(self):
        zero_lane = replace(self.lane, threshold=D("0.85"))
        zero_fak = fak((), requested_quote=D("4"), submitted_maker=D("4"),
                       submitted_taker=D("4.7"), unfilled_quote=D("4"),
                       unfilled_shares=D("4.7"), status="zero")
        zero = replace(self.entry, lane=zero_lane, event_ts_ms=1_100_100, fak=zero_fak)
        self.storage.record_strategy_events((self.entry, zero))
        self.storage.close()

        self.storage = Storage(self.db_path)
        self.storage.initialize()
        self.assertEqual(self.storage.ensure_experiment(self.settings), self.experiment_hash)
        states = self.storage.load_open_market_states()
        self.assertEqual(len(states), 1)
        self.assertEqual(states[0].attempted_lane_keys, (self.lane, zero_lane))
        self.assertEqual(len(states[0].open_positions), 1)
        self.assertEqual(states[0].open_positions[0].inventory_lots[0].shares, D("5"))
        self.storage.record_strategy_events((self.entry, zero))
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM signals").fetchone()[0], 2)

    def test_reverse_is_atomic_idempotent_and_restores_distinct_lots(self):
        self.storage.record_strategy_events((self.entry,))
        reverse = self.reverse()
        self.storage.record_strategy_events((reverse,))
        self.storage.record_strategy_events((reverse,))
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM signals").fetchone()[0], 2)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM reverse_sequences").fetchone()[0], 1)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM paper_orders").fetchone()[0], 3)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM book_generations").fetchone()[0], 2)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM inventory_lots WHERE open=1").fetchone()[0], 2)
        state = self.storage.load_open_market_states()[0]
        position = state.open_positions[0]
        self.assertTrue(position.reverse_attempted)
        self.assertEqual({(lot.side, lot.shares) for lot in position.inventory_lots},
                         {("UP", D("2")), ("DOWN", D("2"))})

    def test_reverse_inventory_failure_rolls_back_every_reverse_row(self):
        self.storage.record_strategy_events((self.entry,))
        broken = replace(self.reverse(), requested_shares=D("6"))
        with self.assertRaisesRegex(StorageInvariantError, "inventory input"):
            self.storage.record_strategy_events((broken,))
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM signals").fetchone()[0], 1)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM reverse_sequences").fetchone()[0], 0)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM inventory_lots WHERE open=1").fetchone()[0], 1)

    def test_reverse_cannot_create_inventory_through_inconsistent_output_lots(self):
        self.storage.record_strategy_events((self.entry,))
        reverse = self.reverse()
        created = replace(
            reverse,
            inventory_lots=reverse.inventory_lots +
            (InventoryLot("down-token", "DOWN", D("1"), "reverse_buy"),),
        )
        with self.assertRaisesRegex(StorageInvariantError, "inventory output"):
            self.storage.record_strategy_events((created,))
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM signals").fetchone()[0], 1)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM inventory_lots WHERE open=1").fetchone()[0], 1)

    def test_positive_reverse_sell_requires_persisted_buy_attempt(self):
        self.storage.record_strategy_events((self.entry,))
        reverse = self.reverse()
        missing_buy = replace(
            reverse,
            buy=None,
            buy_book_generation=None,
            opposite_shares=D("0"),
            inventory_lots=(
                InventoryLot("up-token", "UP", D("2"), "reverse_old_residual"),
            ),
        )
        with self.assertRaisesRegex(StorageInvariantError, "inventory output"):
            self.storage.record_strategy_events((missing_buy,))
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM signals").fetchone()[0], 1)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM paper_orders").fetchone()[0], 1)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM reverse_sequences").fetchone()[0], 0)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM inventory_lots WHERE open=1").fetchone()[0], 1)

    def test_settlement_and_results_are_atomic_idempotent_and_immutable(self):
        self.storage.record_strategy_events((self.entry,))
        reverse = self.reverse()
        self.storage.record_strategy_events((reverse,))
        position = LanePosition(
            "market-1", 1_300, self.lane, self.experiment_hash, self.entry, reverse
        )
        settlement = OfficialSettlement("DOWN", 1_301)
        result = settle_lane(position, settlement)
        self.storage.record_settlement("market-1", settlement, (result,))
        self.storage.record_settlement("market-1", settlement, (result,))
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM settlements").fetchone()[0], 1)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM lane_results").fetchone()[0], 1)
        self.assertEqual(self.storage.dashboard_snapshot().open_positions, 0)
        with self.assertRaisesRegex(StorageInvariantError, "already settled"):
            self.storage.record_strategy_events((self.entry,))
        with self.assertRaisesRegex(StorageInvariantError, "immutable"):
            self.storage.record_settlement("market-1", OfficialSettlement("UP", 1_301), (result,))
        with self.assertRaisesRegex(StorageInvariantError, "immutable"):
            self.storage.record_settlement(
                "market-1", settlement, (replace(result, net_pnl=result.net_pnl + D("1")),)
            )

    def test_unknown_market_settlement_rolls_back_settlement_row(self):
        result = settle_lane(LanePosition(
            "market-1", 1_300, self.lane, self.experiment_hash, self.entry
        ), OfficialSettlement("UP"))
        with self.assertRaisesRegex(StorageInvariantError, "unknown"):
            self.storage.record_settlement("market-1", OfficialSettlement("UP"), (result,))
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM settlements").fetchone()[0], 0)

    def test_settlement_requires_nonempty_results_for_persisted_entry_lanes(self):
        self.storage.record_strategy_events((self.entry,))
        settlement = OfficialSettlement("UP")
        result = settle_lane(
            LanePosition("market-1", 1_300, self.lane, self.experiment_hash, self.entry),
            settlement,
        )
        with self.assertRaisesRegex(StorageInvariantError, "requires lane results"):
            self.storage.record_settlement("market-1", settlement, ())
        unsignaled_lane = replace(self.lane, threshold=D("0.85"))
        with self.assertRaisesRegex(StorageInvariantError, "no persisted entry"):
            self.storage.record_settlement(
                "market-1", settlement, (replace(result, lane=unsignaled_lane),)
            )
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM settlements").fetchone()[0], 0)

    def test_same_factual_settlement_supports_two_experiment_versions(self):
        self.storage.record_strategy_events((self.entry,))
        settlement = OfficialSettlement("UP", 1_301)
        first = settle_lane(
            LanePosition("market-1", 1_300, self.lane, self.experiment_hash, self.entry),
            settlement,
        )
        self.storage.record_settlement("market-1", settlement, (first,))

        changed = replace(self.settings, paper_notional_usd=D("6"))
        second_hash = self.storage.ensure_experiment(changed)
        second_entry = replace(self.entry, config_hash=second_hash)
        self.storage.record_strategy_events((second_entry,))
        second = settle_lane(
            LanePosition("market-1", 1_300, self.lane, second_hash, second_entry),
            settlement,
        )
        self.storage.record_settlement("market-1", settlement, (second,))
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM settlements").fetchone()[0], 1)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM lane_results").fetchone()[0], 2)

    def test_read_only_uri_reads_snapshots_and_cannot_write(self):
        self.storage.record_strategy_events((self.entry,))
        self.storage.close()
        readonly = Storage(self.db_path, read_only=True)
        try:
            readonly.initialize()
            self.assertEqual(readonly.dashboard_snapshot().signals, 1)
            self.assertEqual(readonly.db.execute("PRAGMA query_only").fetchone()[0], 1)
            with self.assertRaises(sqlite3.OperationalError):
                readonly.ensure_experiment(self.settings)
            with self.assertRaises(sqlite3.OperationalError):
                readonly.db.execute("DELETE FROM signals")
        finally:
            readonly.close()

    def test_canonical_decimal_rejects_nonfinite_and_normalizes_scale(self):
        self.assertEqual(canonical_decimal(D("5.000000")), "5")
        self.assertEqual(canonical_decimal(D("0.0100")), "0.01")
        self.assertEqual(canonical_decimal(D("-0")), "0")
        for value in (D("NaN"), D("Infinity")):
            with self.subTest(value=value), self.assertRaises(StorageInvariantError):
                canonical_decimal(value)


if __name__ == "__main__":
    unittest.main()
