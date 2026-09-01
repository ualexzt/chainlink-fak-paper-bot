from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from decimal import Decimal

from .domain import FakResult, InventoryLot, ReverseSequence
from .settlement import OfficialSettlement
from .strategy import LaneKey, StrategyEvent

ZERO = Decimal("0")


@dataclass(frozen=True)
class LanePosition:
    market_id: str
    market_close_ts: int
    lane: LaneKey
    config_hash: str
    entry: StrategyEvent
    reverse: ReverseSequence | None = None

    def __post_init__(self) -> None:
        if not self.market_id or self.market_id != self.entry.market_id:
            raise ValueError("position market identity mismatch")
        if isinstance(self.market_close_ts, bool) or not isinstance(self.market_close_ts, int) or self.market_close_ts < 0:
            raise ValueError("market_close_ts must be a non-negative integer")
        if self.lane != self.entry.lane:
            raise ValueError("position lane mismatch")
        if self.config_hash != self.entry.config_hash:
            raise ValueError("position config mismatch")
        if self.reverse is not None:
            if (
                self.reverse.market_id != self.market_id
                or self.reverse.mkt_ts != self.entry.mkt_ts
                or self.reverse.lane != self.lane
                or self.reverse.config_hash != self.config_hash
                or self.reverse.old_side != self.entry.side
            ):
                raise ValueError("reverse identity mismatch")
            old_lot_shares = sum(
                (lot.shares for lot in self.reverse.inventory_lots if lot.side == self.reverse.old_side),
                ZERO,
            )
            new_lot_shares = sum(
                (lot.shares for lot in self.reverse.inventory_lots if lot.side == self.reverse.new_side),
                ZERO,
            )
            if (
                self.reverse.sell.filled_shares != self.reverse.sold_shares
                or self.entry.fak.filled_shares - self.reverse.sold_shares
                != self.reverse.old_residual_shares
                or old_lot_shares != self.reverse.old_residual_shares
                or new_lot_shares != self.reverse.opposite_shares
                or (
                    self.reverse.buy is None
                    and self.reverse.opposite_shares != ZERO
                )
                or (
                    self.reverse.buy is not None
                    and self.reverse.buy.filled_shares != self.reverse.opposite_shares
                )
            ):
                raise ValueError("reverse inventory does not reconcile")

    @property
    def inventory_lots(self) -> tuple[InventoryLot, ...]:
        if self.reverse is not None:
            return self.reverse.inventory_lots
        if self.entry.fak.filled_shares <= 0:
            return ()
        return (
            InventoryLot(
                token_id=self.entry.token_id,
                side=self.entry.side,
                shares=self.entry.fak.filled_shares,
                source="entry",
            ),
        )


@dataclass(frozen=True)
class LaneResult:
    market_id: str
    market_close_ts: int
    lane: LaneKey
    config_hash: str
    settled: bool
    winner: str | None
    classification: str
    initial_side: str
    inventory_lots: tuple[InventoryLot, ...]
    filled_shares: Decimal
    payouts: Decimal
    reverse_sell_proceeds: Decimal
    entry_buy_cost: Decimal
    reverse_buy_cost: Decimal
    entry_fee: Decimal
    reverse_sell_fee: Decimal
    reverse_buy_fee: Decimal
    total_fees: Decimal
    net_pnl: Decimal | None
    hold_counterfactual: Decimal | None
    reverse_incremental_effect: Decimal | None
    rescued_loss: bool | None
    harmed_winner: bool | None
    false_reverse: bool | None


@dataclass(frozen=True)
class LaneStats:
    results: tuple[LaneResult, ...]
    settled_count: int
    wins: int
    losses: int
    breakeven: int
    rescued_losses: int
    harmed_winners: int
    false_reverses: int
    net_pnl: Decimal
    gross_profit: Decimal
    gross_loss: Decimal
    normalized_ev_per_filled_share: Decimal | None
    win_rate: Decimal | None
    profit_factor: Decimal | None
    max_drawdown: Decimal


def _leg_sums(result: FakResult) -> tuple[Decimal, Decimal, Decimal]:
    if not isinstance(result, FakResult):
        raise TypeError("execution result must be FakResult")
    amounts = (
        result.submitted_maker_amount,
        result.submitted_taker_amount,
        result.filled_shares,
        result.quote_amount,
        result.fee,
    )
    if any(not isinstance(value, Decimal) or not value.is_finite() or value < 0 for value in amounts):
        raise ValueError("FAK totals must be non-negative finite Decimals")
    for leg in result.legs:
        if (
            not isinstance(leg.price, Decimal)
            or not isinstance(leg.shares, Decimal)
            or not isinstance(leg.quote, Decimal)
            or not isinstance(leg.fee, Decimal)
            or not all(value.is_finite() for value in (leg.price, leg.shares, leg.quote, leg.fee))
            or not (Decimal("0") < leg.price < Decimal("1"))
            or leg.shares <= 0
            or leg.quote < 0
            or leg.fee < 0
        ):
            raise ValueError("invalid FAK fill leg")
    shares = sum((leg.shares for leg in result.legs), ZERO)
    quote = sum((leg.quote for leg in result.legs), ZERO)
    fee = sum((leg.fee for leg in result.legs), ZERO)
    if shares != result.filled_shares or quote != result.quote_amount or fee != result.fee:
        raise ValueError("FAK totals do not reconcile with fill legs")
    return shares, quote, fee


def _classification(
    position: LanePosition,
    settlement: OfficialSettlement,
    incremental: Decimal,
) -> tuple[str, bool, bool, bool]:
    if position.reverse is None:
        return "hold", False, False, False
    initial_won = position.entry.side == settlement.winner
    rescued = not initial_won and incremental > 0
    harmed = initial_won and incremental < 0
    false_reverse = initial_won
    if rescued:
        label = "rescued_loss"
    elif harmed:
        label = "harmed_winner"
    elif false_reverse:
        label = "false_reverse"
    elif incremental > 0:
        label = "improved"
    elif incremental < 0:
        label = "worsened"
    else:
        label = "neutral"
    return label, rescued, harmed, false_reverse


def settle_lane(
    position: LanePosition, settlement: OfficialSettlement | None
) -> LaneResult:
    if not isinstance(position, LanePosition):
        raise TypeError("position must be LanePosition")
    entry_shares, entry_cost, entry_fee = _leg_sums(position.entry.fak)
    reverse_sell_proceeds = ZERO
    reverse_sell_fee = ZERO
    reverse_buy_cost = ZERO
    reverse_buy_fee = ZERO
    if position.reverse is not None:
        _, reverse_sell_proceeds, reverse_sell_fee = _leg_sums(position.reverse.sell)
        if position.reverse.buy is not None:
            _, reverse_buy_cost, reverse_buy_fee = _leg_sums(position.reverse.buy)

    total_fees = entry_fee + reverse_sell_fee + reverse_buy_fee
    common = dict(
        market_id=position.market_id,
        market_close_ts=position.market_close_ts,
        lane=position.lane,
        config_hash=position.config_hash,
        initial_side=position.entry.side,
        inventory_lots=position.inventory_lots,
        filled_shares=entry_shares,
        reverse_sell_proceeds=reverse_sell_proceeds,
        entry_buy_cost=entry_cost,
        reverse_buy_cost=reverse_buy_cost,
        entry_fee=entry_fee,
        reverse_sell_fee=reverse_sell_fee,
        reverse_buy_fee=reverse_buy_fee,
        total_fees=total_fees,
    )
    if settlement is None:
        return LaneResult(
            **common,
            settled=False,
            winner=None,
            classification="unresolved",
            payouts=ZERO,
            net_pnl=None,
            hold_counterfactual=None,
            reverse_incremental_effect=None,
            rescued_loss=None,
            harmed_winner=None,
            false_reverse=None,
        )
    if not isinstance(settlement, OfficialSettlement):
        raise TypeError("settlement must be OfficialSettlement or None")

    payouts = sum(
        (lot.shares for lot in position.inventory_lots if lot.side == settlement.winner),
        ZERO,
    )
    net_pnl = payouts + reverse_sell_proceeds - entry_cost - reverse_buy_cost - total_fees
    hold_payout = entry_shares if position.entry.side == settlement.winner else ZERO
    hold_counterfactual = hold_payout - entry_cost - entry_fee
    incremental = net_pnl - hold_counterfactual
    classification, rescued, harmed, false_reverse = _classification(position, settlement, incremental)
    return LaneResult(
        **common,
        settled=True,
        winner=settlement.winner,
        classification=classification,
        payouts=payouts,
        net_pnl=net_pnl,
        hold_counterfactual=hold_counterfactual,
        reverse_incremental_effect=incremental,
        rescued_loss=rescued,
        harmed_winner=harmed,
        false_reverse=false_reverse,
    )


def aggregate_results(results: Iterable[LaneResult]) -> LaneStats:
    materialized = tuple(results)
    if not all(isinstance(result, LaneResult) for result in materialized):
        raise TypeError("results must contain LaneResult records")
    identities = {(result.lane, result.config_hash) for result in materialized}
    if len(identities) > 1:
        raise ValueError("results must belong to one lane and experiment")
    ordered = tuple(sorted(materialized, key=lambda item: (item.market_close_ts, item.market_id)))
    settled = tuple(result for result in ordered if result.settled)
    if any(result.net_pnl is None for result in settled):
        raise ValueError("settled result is missing net_pnl")

    net_pnl = sum((result.net_pnl for result in settled if result.net_pnl is not None), ZERO)
    gross_profit = sum(
        (result.net_pnl for result in settled if result.net_pnl is not None and result.net_pnl > 0), ZERO
    )
    gross_loss = -sum(
        (result.net_pnl for result in settled if result.net_pnl is not None and result.net_pnl < 0), ZERO
    )
    wins = sum(result.net_pnl is not None and result.net_pnl > 0 for result in settled)
    losses = sum(result.net_pnl is not None and result.net_pnl < 0 for result in settled)
    breakeven = len(settled) - wins - losses
    filled_shares = sum((result.filled_shares for result in settled), ZERO)

    equity = ZERO
    peak = ZERO
    max_drawdown = ZERO
    for result in settled:
        assert result.net_pnl is not None
        equity += result.net_pnl
        peak = max(peak, equity)
        max_drawdown = max(max_drawdown, peak - equity)

    settled_count = len(settled)
    return LaneStats(
        results=ordered,
        settled_count=settled_count,
        wins=wins,
        losses=losses,
        breakeven=breakeven,
        rescued_losses=sum(result.rescued_loss is True for result in settled),
        harmed_winners=sum(result.harmed_winner is True for result in settled),
        false_reverses=sum(result.false_reverse is True for result in settled),
        net_pnl=net_pnl,
        gross_profit=gross_profit,
        gross_loss=gross_loss,
        normalized_ev_per_filled_share=(net_pnl / filled_shares if filled_shares > 0 else None),
        win_rate=(Decimal(wins) / Decimal(settled_count) if settled_count else None),
        profit_factor=(gross_profit / gross_loss if gross_loss > 0 else None),
        max_drawdown=max_drawdown,
    )


__all__ = [
    "LanePosition",
    "LaneResult",
    "LaneStats",
    "aggregate_results",
    "settle_lane",
]
