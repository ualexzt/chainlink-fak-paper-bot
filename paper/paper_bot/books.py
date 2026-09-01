from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from numbers import Integral
from typing import Iterable

from .domain import BookLevel


class InvalidBook(ValueError):
    pass


@dataclass(init=False)
class OrderBook:
    _bids: tuple[BookLevel, ...]
    _asks: tuple[BookLevel, ...]
    _generation: int
    _valid: bool
    _last_event_ts_ms: int | None
    _last_sequence: int | None

    def __init__(self) -> None:
        self._bids = ()
        self._asks = ()
        self._generation = 0
        self._valid = False
        self._last_event_ts_ms = None
        self._last_sequence = None

    @property
    def generation(self) -> int:
        return self._generation

    @property
    def valid(self) -> bool:
        return self._valid

    def invalidate(self) -> None:
        self._valid = False

    def executable_bids(self) -> tuple[BookLevel, ...]:
        if not self._valid:
            raise InvalidBook("book is invalid")
        return self._clone_levels(self._bids)

    def executable_asks(self) -> tuple[BookLevel, ...]:
        if not self._valid:
            raise InvalidBook("book is invalid")
        return self._clone_levels(self._asks)

    def apply_snapshot(
        self,
        bids: Iterable[BookLevel],
        asks: Iterable[BookLevel],
        event_ts_ms: int,
        sequence: int,
    ) -> int:
        self._validate_event_fields(event_ts_ms, sequence)
        if self._valid:
            if self._last_event_ts_ms is not None and event_ts_ms < self._last_event_ts_ms:
                raise InvalidBook("snapshot is stale")
            if self._last_sequence is not None and sequence <= self._last_sequence:
                raise InvalidBook("snapshot is stale")
        next_bids = self._normalize_snapshot_levels(bids, reverse=True)
        next_asks = self._normalize_snapshot_levels(asks, reverse=False)
        self._validate_spread(next_bids, next_asks)

        self._bids = next_bids
        self._asks = next_asks
        self._generation += 1
        self._valid = True
        self._last_event_ts_ms = event_ts_ms
        self._last_sequence = sequence
        return self._generation

    def apply_delta(self, side: str, price: Decimal, shares: Decimal, event_ts_ms: int, sequence: int) -> bool:
        if not self._valid:
            return False

        self._validate_event_fields(event_ts_ms, sequence)
        if self._last_event_ts_ms is not None and event_ts_ms < self._last_event_ts_ms:
            return False
        if self._last_sequence is not None and sequence <= self._last_sequence:
            return False
        if side not in {"bid", "ask"}:
            raise ValueError("side must be bid or ask")
        self._validate_price(price)
        self._validate_delta_shares(shares)

        bids_map = self._levels_to_map(self._bids)
        asks_map = self._levels_to_map(self._asks)
        target_map = bids_map if side == "bid" else asks_map
        if shares == 0:
            target_map.pop(price, None)
        else:
            target_map[price] = shares

        next_bids = self._sorted_levels(bids_map, reverse=True)
        next_asks = self._sorted_levels(asks_map, reverse=False)
        self._validate_spread(next_bids, next_asks)

        self._bids = next_bids
        self._asks = next_asks
        self._last_event_ts_ms = event_ts_ms
        self._last_sequence = sequence
        return True

    @staticmethod
    def _clone_levels(levels: tuple[BookLevel, ...]) -> tuple[BookLevel, ...]:
        return tuple(BookLevel(level.price, level.shares) for level in levels)

    @staticmethod
    def _validate_event_fields(event_ts_ms: int, sequence: int) -> None:
        if not isinstance(event_ts_ms, Integral) or isinstance(event_ts_ms, bool) or event_ts_ms < 0:
            raise ValueError("event_ts_ms must be a non-negative integer")
        if not isinstance(sequence, Integral) or isinstance(sequence, bool) or sequence < 0:
            raise ValueError("sequence must be a non-negative integer")

    @staticmethod
    def _validate_price(price: Decimal) -> None:
        if not isinstance(price, Decimal) or not price.is_finite() or price <= 0 or price >= 1:
            raise ValueError("price must be in (0, 1) and finite")

    @staticmethod
    def _validate_snapshot_shares(shares: Decimal) -> None:
        if not isinstance(shares, Decimal) or not shares.is_finite() or shares <= 0:
            raise ValueError("shares must be strictly positive and finite")

    @staticmethod
    def _validate_delta_shares(shares: Decimal) -> None:
        if not isinstance(shares, Decimal) or not shares.is_finite() or shares < 0:
            raise ValueError("shares must be non-negative and finite")

    def _normalize_snapshot_levels(self, levels: Iterable[BookLevel], *, reverse: bool) -> tuple[BookLevel, ...]:
        materialized = tuple(levels)
        normalized: dict[Decimal, Decimal] = {}
        for level in materialized:
            self._validate_price(level.price)
            self._validate_snapshot_shares(level.shares)
            normalized[level.price] = level.shares
        return self._sorted_levels(normalized, reverse=reverse)

    @staticmethod
    def _levels_to_map(levels: tuple[BookLevel, ...]) -> dict[Decimal, Decimal]:
        return {level.price: level.shares for level in levels}

    @staticmethod
    def _sorted_levels(levels: dict[Decimal, Decimal], *, reverse: bool) -> tuple[BookLevel, ...]:
        return tuple(BookLevel(price, shares) for price, shares in sorted(levels.items(), key=lambda item: item[0], reverse=reverse))

    @staticmethod
    def _validate_spread(bids: tuple[BookLevel, ...], asks: tuple[BookLevel, ...]) -> None:
        if bids and asks and bids[0].price >= asks[0].price:
            raise InvalidBook("book is locked or crossed")


__all__ = ["InvalidBook", "OrderBook"]
