from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from enum import Enum
from hashlib import sha256
from numbers import Integral
from time import monotonic_ns

from .books import InvalidBook, OrderBook
from .domain import FakResult, InventoryLot, ReverseSequence
from .fak import (
    buy_maker_amount_for_target_shares,
    quote_for_target_shares,
    simulate_buy_fak,
    simulate_sell_fak,
)
from .gamma import MarketDefinition
from .resolver import ResolverState, ResolverView

class Confirmation(str, Enum):
    BOOK_ONLY = "BOOK_ONLY"
    CHAINLINK_DIRECTION = "CHAINLINK_DIRECTION"
    CHAINLINK_CONFIRMED = "CHAINLINK_CONFIRMED"
    MC_BOOTSTRAP_90_V1 = "MC_BOOTSTRAP_90_V1"
    MC_BOOTSTRAP_60_V1 = "MC_BOOTSTRAP_60_V1"
    MC_BOOTSTRAP_30_V1 = "MC_BOOTSTRAP_30_V1"


class PositionPolicy(str, Enum):
    HOLD = "HOLD"
    IMMEDIATE_REVERSE = "IMMEDIATE_REVERSE"
    CHAINLINK_REVERSE = "CHAINLINK_REVERSE"


@dataclass(frozen=True, order=True)
class LaneKey:
    threshold: Decimal
    confirmation: Confirmation
    policy: PositionPolicy


@dataclass(frozen=True)
class StrategyEvent:
    lane: LaneKey
    kind: str
    market_id: str
    mkt_ts: int
    token_id: str
    side: str
    event_ts_ms: int
    book_generation: int
    config_hash: str
    fak: FakResult


DEFAULT_THRESHOLDS = tuple(Decimal(value) for value in ("0.80", "0.85", "0.89", "0.90"))
BASE_CONFIRMATIONS = (
    Confirmation.BOOK_ONLY,
    Confirmation.CHAINLINK_DIRECTION,
    Confirmation.CHAINLINK_CONFIRMED,
)


def _validated_thresholds(thresholds: Sequence[Decimal]) -> tuple[Decimal, ...]:
    values = tuple(thresholds)
    if not values:
        raise ValueError("at least one threshold is required")
    if any(not isinstance(value, Decimal) or not value.is_finite() or value <= 0 or value >= 1 for value in values):
        raise ValueError("thresholds must be Decimal values in (0, 1)")
    if len(set(values)) != len(values):
        raise ValueError("thresholds must be unique")
    if tuple(sorted(values)) != values:
        raise ValueError("thresholds must be strictly increasing")
    return values


def all_lane_keys(thresholds: Sequence[Decimal] | None = None) -> tuple[LaneKey, ...]:
    values = DEFAULT_THRESHOLDS if thresholds is None else _validated_thresholds(thresholds)
    return tuple(
        LaneKey(threshold, confirmation, policy)
        for threshold in values
        for confirmation in BASE_CONFIRMATIONS
        for policy in PositionPolicy
    )


def _validate_nonnegative_integer(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, Integral) or value < 0:
        raise ValueError(f"{name} must be a non-negative integer")
    return int(value)


def _book_for_side(books: Mapping[str, OrderBook], market: MarketDefinition, side: str) -> OrderBook:
    token_id = market.up_token_id if side == "UP" else market.down_token_id
    for key in (token_id, side, side.lower()):
        book = books.get(key)
        if book is not None:
            if not isinstance(book, OrderBook):
                raise TypeError("book values must be OrderBook instances")
            return book
    raise KeyError(f"missing {side} book")


class MarketStrategyState:
    """Evaluate one market's immutable entry experiment from accepted book events."""

    def __init__(
        self,
        *,
        thresholds: Sequence[Decimal] | None = None,
        paper_notional_usd: Decimal = Decimal("5.00"),
        config_hash: str | None = None,
        clock_ns: Callable[[], int] = monotonic_ns,
    ) -> None:
        self.thresholds = DEFAULT_THRESHOLDS if thresholds is None else _validated_thresholds(thresholds)
        if (
            not isinstance(paper_notional_usd, Decimal)
            or not paper_notional_usd.is_finite()
            or paper_notional_usd <= 0
        ):
            raise ValueError("paper_notional_usd must be a positive finite Decimal")
        if config_hash is None:
            identity = ",".join(str(value) for value in self.thresholds) + "|" + str(paper_notional_usd)
            config_hash = sha256(identity.encode("ascii")).hexdigest()
        if (
            not isinstance(config_hash, str)
            or len(config_hash) != 64
            or any(character not in "0123456789abcdef" for character in config_hash)
        ):
            raise ValueError("config_hash must be a lowercase SHA-256 hex digest")
        if not callable(clock_ns):
            raise ValueError("clock_ns must be callable")

        self.paper_notional_usd = paper_notional_usd
        self.config_hash = config_hash
        self._clock_ns = clock_ns
        self._attempted: set[tuple[Decimal, Confirmation]] = set()
        self._previous: dict[str, tuple[int, Decimal]] = {}
        self._market_key: tuple[str, int] | None = None
        self._reverse_attempted: set[LaneKey] = set()

    def restore_attempts(
        self,
        attempted_lanes: Sequence[LaneKey],
        reverse_attempted_lanes: Sequence[LaneKey] = (),
    ) -> None:
        """Hydrate durable one-shot guards before any fresh market event."""
        if self._market_key is not None or self._previous or self._attempted or self._reverse_attempted:
            raise RuntimeError("strategy attempts can only be restored into fresh state")
        attempted = tuple(attempted_lanes)
        reversed_lanes = tuple(reverse_attempted_lanes)
        if any(not isinstance(lane, LaneKey) or lane.threshold not in self.thresholds for lane in attempted):
            raise ValueError("invalid restored entry lane")
        if any(not isinstance(lane, LaneKey) or lane not in attempted for lane in reversed_lanes):
            raise ValueError("invalid restored reverse lane")
        self._attempted.update((lane.threshold, lane.confirmation) for lane in attempted)
        self._reverse_attempted.update(reversed_lanes)

    def on_book_event(
        self,
        market: MarketDefinition,
        books: Mapping[str, OrderBook],
        resolver: ResolverState,
        event_ts_ms: int,
        now_ts: int,
    ) -> tuple[StrategyEvent, ...]:
        event_ts_ms = _validate_nonnegative_integer(event_ts_ms, "event_ts_ms")
        now_ts = _validate_nonnegative_integer(now_ts, "now_ts")
        self._bind_market(market)

        current: dict[str, tuple[int, Decimal]] = {}
        side_books: dict[str, OrderBook] = {}
        for side in ("UP", "DOWN"):
            try:
                book = _book_for_side(books, market, side)
                asks = book.executable_asks()
            except (KeyError, InvalidBook):
                continue
            if asks:
                side_books[side] = book
                current[side] = (book.generation, asks[0].price)

        continuous = (
            len(current) == 2
            and all(side in self._previous for side in ("UP", "DOWN"))
            and all(current[side][0] == self._previous[side][0] for side in ("UP", "DOWN"))
        )
        crosses: list[tuple[str, Decimal]] = []
        if continuous:
            for side in ("UP", "DOWN"):
                current_ask = current[side][1]
                previous_ask = self._previous[side][1]
                crosses.extend(
                    (side, threshold)
                    for threshold in self.thresholds
                    if previous_ask < threshold <= current_ask
                )
        self._previous = current

        seconds_to_close = market.end_ts - now_ts
        if not (0 < seconds_to_close <= 150) or not crosses:
            return ()
        if len({side for side, _ in crosses}) != 1:
            return ()

        resolver_view: ResolverView | None
        try:
            resolver_view = resolver.view(market.symbol, market.mkt_ts, now_ts * 1000)
        except (AttributeError, KeyError, ValueError):
            resolver_view = None

        events: list[StrategyEvent] = []
        side = crosses[0][0]
        token_id = market.up_token_id if side == "UP" else market.down_token_id
        crossed_thresholds = {threshold for _, threshold in crosses}
        book = side_books[side]
        asks = book.executable_asks()
        for threshold in self.thresholds:
            if threshold not in crossed_thresholds:
                continue
            for confirmation in BASE_CONFIRMATIONS:
                attempt_key = (threshold, confirmation)
                if attempt_key in self._attempted or not self._gate(confirmation, side, resolver_view):
                    continue
                result = simulate_buy_fak(
                    asks,
                    self.paper_notional_usd,
                    threshold,
                    market.tick_size,
                    market.min_order_shares,
                    market.fee_schedule,
                )
                self._attempted.add(attempt_key)
                events.extend(
                    StrategyEvent(
                        lane=LaneKey(threshold, confirmation, policy),
                        kind="entry_attempt",
                        market_id=market.market_id,
                        mkt_ts=market.mkt_ts,
                        token_id=token_id,
                        side=side,
                        event_ts_ms=event_ts_ms,
                        book_generation=book.generation,
                        config_hash=self.config_hash,
                        fak=result,
                    )
                    for policy in PositionPolicy
                )
        return tuple(events)

    @staticmethod
    def _gate(confirmation: Confirmation, side: str, view: ResolverView | None) -> bool:
        if confirmation is Confirmation.BOOK_ONLY:
            return True
        if view is None or not view.fresh or view.leader != side:
            return False
        if confirmation is Confirmation.CHAINLINK_DIRECTION:
            return True
        momentum = view.momentum_5s_bps
        return momentum is not None and (
            (side == "UP" and momentum > 0) or (side == "DOWN" and momentum < 0)
        )

    def on_reverse_event(
        self,
        lane: LaneKey,
        entry: StrategyEvent,
        market: MarketDefinition,
        books: Mapping[str, OrderBook],
        resolver: ResolverState,
        event_ts_ms: int,
        now_ts: int,
    ) -> ReverseSequence | None:
        """Attempt the lane's single non-atomic reverse, if its policy fires."""
        event_ts_ms = _validate_nonnegative_integer(event_ts_ms, "event_ts_ms")
        now_ts = _validate_nonnegative_integer(now_ts, "now_ts")
        self._bind_market(market)
        self._validate_reverse_identity(lane, entry, market)
        if lane.policy is PositionPolicy.HOLD or lane in self._reverse_attempted:
            return None
        if entry.fak.filled_shares <= 0 or event_ts_ms <= entry.event_ts_ms:
            return None
        if market.end_ts - now_ts <= 0:
            return None

        old_side = entry.side
        new_side = "DOWN" if old_side == "UP" else "UP"
        try:
            old_book = _book_for_side(books, market, old_side)
            new_book = _book_for_side(books, market, new_side)
            old_bids = old_book.executable_bids()
            new_asks = new_book.executable_asks()
        except (KeyError, InvalidBook):
            return None
        if not new_asks or not (Decimal("0.89") <= new_asks[0].price <= Decimal("0.90")):
            return None
        if lane.policy is PositionPolicy.CHAINLINK_REVERSE:
            try:
                view = resolver.view(market.symbol, market.mkt_ts, now_ts * 1000)
            except (AttributeError, KeyError, ValueError):
                return None
            if not self._gate(Confirmation.CHAINLINK_CONFIRMED, new_side, view):
                return None

        sell = simulate_sell_fak(
            old_bids,
            entry.fak.filled_shares,
            market.tick_size,
            market.tick_size,
            market.min_order_shares,
            market.fee_schedule,
        )
        sell_completed_ns = self._clock_ns()
        sold = sell.filled_shares
        residual = entry.fak.filled_shares - sold
        dust = entry.fak.filled_shares - sell.submitted_maker_amount
        lots = self._inventory_lots(market, old_side, new_side, residual, Decimal("0"))
        if sold <= 0:
            result = ReverseSequence(
                lane=lane,
                market_id=market.market_id,
                mkt_ts=market.mkt_ts,
                config_hash=entry.config_hash,
                old_side=old_side,
                new_side=new_side,
                status="COMPLETE",
                outcome="ZERO_SELL",
                transitions=("ELIGIBLE", "SELL_ATTEMPTED", "SELL_FILLED_OR_PARTIAL", "COMPLETE"),
                requested_shares=entry.fak.filled_shares,
                sold_shares=sold,
                old_residual_shares=residual,
                submission_dust_shares=max(Decimal("0"), dust),
                opposite_shares=Decimal("0"),
                expected_quote=Decimal("0"),
                sell=sell,
                buy=None,
                inventory_lots=lots,
                sell_book_generation=old_book.generation,
                buy_book_generation=None,
                trigger_ts_ms=event_ts_ms,
                leg_elapsed_ms=None,
            )
            self._reverse_attempted.add(lane)
            return result

        expected = quote_for_target_shares(new_asks, sold, Decimal("0.90"))
        maker_quote = buy_maker_amount_for_target_shares(sold, Decimal("0.90"), market.tick_size)
        buy_started_ns = self._clock_ns()
        if buy_started_ns < sell_completed_ns:
            raise RuntimeError("clock_ns must be monotonic")
        buy = simulate_buy_fak(
            new_asks,
            maker_quote,
            Decimal("0.90"),
            market.tick_size,
            market.min_order_shares,
            market.fee_schedule,
        )
        lots = self._inventory_lots(market, old_side, new_side, residual, buy.filled_shares)
        if residual > 0 and buy.status != "full":
            outcome = "PARTIAL_SELL_AND_BUY"
        elif residual > 0:
            outcome = "PARTIAL_SELL"
        elif buy.status != "full":
            outcome = "PARTIAL_BUY"
        else:
            outcome = "FULL"
        result = ReverseSequence(
            lane=lane,
            market_id=market.market_id,
            mkt_ts=market.mkt_ts,
            config_hash=entry.config_hash,
            old_side=old_side,
            new_side=new_side,
            status="COMPLETE",
            outcome=outcome,
            transitions=(
                "ELIGIBLE",
                "SELL_ATTEMPTED",
                "SELL_FILLED_OR_PARTIAL",
                "BUY_ATTEMPTED",
                "COMPLETE",
            ),
            requested_shares=entry.fak.filled_shares,
            sold_shares=sold,
            old_residual_shares=residual,
            submission_dust_shares=max(Decimal("0"), dust),
            opposite_shares=buy.filled_shares,
            expected_quote=expected,
            sell=sell,
            buy=buy,
            inventory_lots=lots,
            sell_book_generation=old_book.generation,
            buy_book_generation=new_book.generation,
            trigger_ts_ms=event_ts_ms,
            leg_elapsed_ms=(buy_started_ns - sell_completed_ns) // 1_000_000,
        )
        self._reverse_attempted.add(lane)
        return result

    def _bind_market(self, market: MarketDefinition) -> None:
        market_key = (market.market_id, market.mkt_ts)
        if self._market_key is None:
            self._market_key = market_key
        elif self._market_key != market_key:
            raise ValueError("MarketStrategyState cannot be reused across markets")

    def _validate_reverse_identity(
        self, lane: LaneKey, entry: StrategyEvent, market: MarketDefinition
    ) -> None:
        expected_token = market.up_token_id if entry.side == "UP" else market.down_token_id
        if lane != entry.lane:
            raise ValueError("reverse lane does not match entry lane")
        if lane.threshold not in self.thresholds:
            raise ValueError("reverse lane threshold is not configured")
        if entry.kind != "entry_attempt":
            raise ValueError("reverse requires an entry attempt")
        if (entry.market_id, entry.mkt_ts) != (market.market_id, market.mkt_ts):
            raise ValueError("reverse entry market mismatch")
        if entry.side not in {"UP", "DOWN"} or entry.token_id != expected_token:
            raise ValueError("reverse entry token mismatch")
        if entry.config_hash != self.config_hash:
            raise ValueError("reverse entry config mismatch")

    @staticmethod
    def _inventory_lots(
        market: MarketDefinition,
        old_side: str,
        new_side: str,
        old_residual: Decimal,
        opposite_shares: Decimal,
    ) -> tuple[InventoryLot, ...]:
        lots: list[InventoryLot] = []
        if old_residual > 0:
            old_token = market.up_token_id if old_side == "UP" else market.down_token_id
            lots.append(InventoryLot(old_token, old_side, old_residual, "reverse_old_residual"))
        if opposite_shares > 0:
            new_token = market.up_token_id if new_side == "UP" else market.down_token_id
            lots.append(InventoryLot(new_token, new_side, opposite_shares, "reverse_buy"))
        return tuple(lots)

    # Friendly alias for callers that model the event as a position event.
    on_position_event = on_reverse_event


__all__ = [
    "Confirmation",
    "BASE_CONFIRMATIONS",
    "LaneKey",
    "MarketStrategyState",
    "PositionPolicy",
    "StrategyEvent",
    "all_lane_keys",
]
