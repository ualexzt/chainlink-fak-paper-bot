from __future__ import annotations

import unittest
from dataclasses import FrozenInstanceError, replace
from decimal import Decimal as D

from paper_bot.accounting import LanePosition, aggregate_results, settle_lane
from paper_bot.domain import FakResult, FillLeg, InventoryLot, ReverseSequence
from paper_bot.settlement import OfficialSettlement
from paper_bot.strategy import Confirmation, LaneKey, PositionPolicy, StrategyEvent


def fak(legs, *, requested_quote=None, requested_shares=None, status="full"):
    legs = tuple(legs)
    shares = sum((leg.shares for leg in legs), D("0"))
    quote = sum((leg.quote for leg in legs), D("0"))
    fee = sum((leg.fee for leg in legs), D("0"))
    return FakResult(
        requested_quote=requested_quote,
        requested_shares=requested_shares,
        submitted_maker_amount=requested_quote or requested_shares or D("0"),
        submitted_taker_amount=shares,
        filled_shares=shares,
        quote_amount=quote,
        unfilled_quote=D("0") if requested_quote is not None else None,
        unfilled_shares=D("0") if requested_shares is not None else None,
        fee=fee,
        legs=legs,
        status=status,
    )


class AccountingTests(unittest.TestCase):
    config_hash = "a" * 64

    def setUp(self):
        self.lane = LaneKey(D("0.80"), Confirmation.BOOK_ONLY, PositionPolicy.IMMEDIATE_REVERSE)
        self.entry_fak = fak(
            (
                FillLeg(D("0.80"), D("2"), D("1.60"), D("0.04")),
                FillLeg(D("0.80"), D("3"), D("2.40"), D("0.06")),
            ),
            requested_quote=D("4.00"),
        )
        self.entry = StrategyEvent(
            lane=self.lane, kind="entry_attempt", market_id="market-1", mkt_ts=1_000,
            token_id="up-token", side="UP", event_ts_ms=1_100_000, book_generation=1,
            config_hash=self.config_hash, fak=self.entry_fak,
        )

    def position(self, *, reverse=None, close_ts=1_300):
        return LanePosition(
            market_id="market-1", market_close_ts=close_ts, lane=self.lane,
            config_hash=self.config_hash, entry=self.entry, reverse=reverse,
        )

    def reverse(self):
        sell = fak(
            (FillLeg(D("0.20"), D("3"), D("0.60"), D("0.03")),),
            requested_shares=D("5"), status="partial",
        )
        buy = fak(
            (FillLeg(D("0.90"), D("2"), D("1.80"), D("0.02")),),
            requested_quote=D("2.70"), status="partial",
        )
        return ReverseSequence(
            lane=self.lane, market_id="market-1", mkt_ts=1_000,
            config_hash=self.config_hash, old_side="UP", new_side="DOWN",
            status="COMPLETE", outcome="PARTIAL_SELL_AND_BUY",
            transitions=("ELIGIBLE", "SELL_ATTEMPTED", "SELL_FILLED_OR_PARTIAL", "BUY_ATTEMPTED", "COMPLETE"),
            requested_shares=D("5"), sold_shares=D("3"), old_residual_shares=D("2"),
            submission_dust_shares=D("0"), opposite_shares=D("2"),
            expected_quote=D("1.80"), sell=sell, buy=buy,
            inventory_lots=(
                InventoryLot("up-token", "UP", D("2"), "reverse_old_residual"),
                InventoryLot("down-token", "DOWN", D("2"), "reverse_buy"),
            ),
            sell_book_generation=1, buy_book_generation=1,
            trigger_ts_ms=1_200_000, leg_elapsed_ms=1,
        )

    def test_hold_payout_and_exact_net_use_actual_fill_legs(self):
        result = settle_lane(self.position(), OfficialSettlement("UP", 1_301))
        self.assertEqual(result.payouts, D("5"))
        self.assertEqual(result.entry_buy_cost, D("4.00"))
        self.assertEqual(result.entry_fee, D("0.10"))
        self.assertEqual(result.total_fees, D("0.10"))
        self.assertEqual(result.net_pnl, D("0.90"))
        self.assertEqual(result.hold_counterfactual, D("0.90"))
        self.assertEqual(result.reverse_incremental_effect, D("0"))
        self.assertEqual(result.classification, "hold")
        self.assertEqual(result.inventory_lots, (InventoryLot("up-token", "UP", D("5"), "entry"),))

    def test_reverse_reconciles_three_fee_legs_and_all_remaining_lots(self):
        result = settle_lane(self.position(reverse=self.reverse()), OfficialSettlement("DOWN"))
        self.assertEqual(result.payouts, D("2"))
        self.assertEqual(result.reverse_sell_proceeds, D("0.60"))
        self.assertEqual(result.reverse_buy_cost, D("1.80"))
        self.assertEqual(result.entry_fee, D("0.10"))
        self.assertEqual(result.reverse_sell_fee, D("0.03"))
        self.assertEqual(result.reverse_buy_fee, D("0.02"))
        self.assertEqual(result.total_fees, D("0.15"))
        self.assertEqual(result.net_pnl, D("-3.35"))
        self.assertEqual(result.hold_counterfactual, D("-4.10"))
        self.assertEqual(result.reverse_incremental_effect, D("0.75"))
        self.assertEqual(result.classification, "rescued_loss")
        self.assertTrue(result.rescued_loss)
        self.assertFalse(result.false_reverse)

    def test_reverse_that_leaves_original_winner_is_harmed_and_false(self):
        result = settle_lane(self.position(reverse=self.reverse()), OfficialSettlement("UP"))
        self.assertEqual(result.payouts, D("2"))
        self.assertEqual(result.net_pnl, D("-3.35"))
        self.assertEqual(result.hold_counterfactual, D("0.90"))
        self.assertEqual(result.reverse_incremental_effect, D("-4.25"))
        self.assertEqual(result.classification, "harmed_winner")
        self.assertTrue(result.harmed_winner)
        self.assertTrue(result.false_reverse)

    def test_unresolved_preserves_operational_costs_but_not_final_pnl(self):
        result = settle_lane(self.position(reverse=self.reverse()), None)
        self.assertFalse(result.settled)
        self.assertEqual(result.classification, "unresolved")
        self.assertEqual(result.total_fees, D("0.15"))
        self.assertIsNone(result.net_pnl)
        self.assertIsNone(result.hold_counterfactual)
        self.assertIsNone(result.reverse_incremental_effect)
        self.assertIsNone(result.false_reverse)

    def test_fill_totals_must_reconcile_before_accounting(self):
        broken = replace(self.entry_fak, fee=D("99"))
        position = replace(self.position(), entry=replace(self.entry, fak=broken))
        with self.assertRaisesRegex(ValueError, "do not reconcile"):
            settle_lane(position, OfficialSettlement("UP"))

    def test_position_identity_and_result_immutability_fail_closed(self):
        with self.assertRaises(ValueError):
            replace(self.position(), market_id="other")
        with self.assertRaises(ValueError):
            replace(self.position(), config_hash="b" * 64)
        with self.assertRaisesRegex(ValueError, "inventory does not reconcile"):
            self.position(reverse=replace(self.reverse(), opposite_shares=D("3")))
        result = settle_lane(self.position(), OfficialSettlement("UP"))
        with self.assertRaises(FrozenInstanceError):
            result.net_pnl = D("100")

    def test_aggregate_excludes_unresolved_and_uses_chronological_cash_equity(self):
        base = settle_lane(self.position(), OfficialSettlement("UP"))
        results = (
            replace(base, market_id="m3", market_close_ts=30, net_pnl=D("-4"), rescued_loss=True),
            replace(base, market_id="m1", market_close_ts=10, net_pnl=D("5"), rescued_loss=False),
            replace(base, market_id="m4", market_close_ts=40, net_pnl=D("3"), harmed_winner=True,
                    false_reverse=True),
            replace(base, market_id="m2", market_close_ts=20, net_pnl=D("-2")),
            settle_lane(self.position(close_ts=5), None),
        )
        stats = aggregate_results(results)
        self.assertEqual(tuple(result.market_close_ts for result in stats.results), (5, 10, 20, 30, 40))
        self.assertEqual(stats.settled_count, 4)
        self.assertEqual((stats.wins, stats.losses, stats.breakeven), (2, 2, 0))
        self.assertEqual(stats.net_pnl, sum((r.net_pnl for r in results if r.net_pnl is not None), D("0")))
        self.assertEqual(stats.gross_profit, D("8"))
        self.assertEqual(stats.gross_loss, D("6"))
        self.assertEqual(stats.profit_factor, D("8") / D("6"))
        self.assertEqual(stats.win_rate, D("0.5"))
        self.assertEqual(stats.normalized_ev_per_filled_share, D("2") / D("20"))
        self.assertEqual(stats.max_drawdown, D("6"))
        self.assertEqual((stats.rescued_losses, stats.harmed_winners, stats.false_reverses), (1, 1, 1))

    def test_empty_aggregate_has_no_fabricated_rates(self):
        stats = aggregate_results(())
        self.assertEqual(stats.settled_count, 0)
        self.assertEqual(stats.net_pnl, D("0"))
        self.assertIsNone(stats.win_rate)
        self.assertIsNone(stats.profit_factor)
        self.assertIsNone(stats.normalized_ev_per_filled_share)

    def test_aggregate_rejects_mixed_lane_or_experiment_identity(self):
        result = settle_lane(self.position(), OfficialSettlement("UP"))
        other_lane = replace(self.lane, threshold=D("0.85"))
        with self.assertRaisesRegex(ValueError, "one lane"):
            aggregate_results((result, replace(result, lane=other_lane)))
        with self.assertRaisesRegex(ValueError, "one lane"):
            aggregate_results((result, replace(result, config_hash="b" * 64)))


if __name__ == "__main__":
    unittest.main()
