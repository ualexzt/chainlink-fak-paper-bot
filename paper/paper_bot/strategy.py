from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from enum import Enum
from hashlib import sha256
from numbers import Integral

from .books import InvalidBook, OrderBook
from .domain import FakResult
from .fak import simulate_buy_fak
from .gamma import MarketDefinition
from .resolver import ResolverState, ResolverView


class Confirmation(str, Enum):
    BOOK_ONLY = "BOOK_ONLY"
    CHAINLINK_DIRECTION = "CHAINLINK_DIRECTION"
    CHAINLINK_CONFIRMED = "CHAINLINK_CONFIRMED"


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
        for confirmation in Confirmation
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

        self.paper_notional_usd = paper_notional_usd
        self.config_hash = config_hash
        self._attempted: set[tuple[Decimal, Confirmation]] = set()
        self._previous: dict[str, tuple[int, Decimal]] = {}
        self._market_key: tuple[str, int] | None = None

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
        market_key = (market.market_id, market.mkt_ts)
        if self._market_key is None:
            self._market_key = market_key
        elif self._market_key != market_key:
            raise ValueError("MarketStrategyState cannot be reused across markets")

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
            for confirmation in Confirmation:
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


__all__ = [
    "Confirmation",
    "LaneKey",
    "MarketStrategyState",
    "PositionPolicy",
    "StrategyEvent",
    "all_lane_keys",
]
