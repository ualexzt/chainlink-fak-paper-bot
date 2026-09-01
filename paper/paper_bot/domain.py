from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import Enum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .strategy import LaneKey

SIX_PLACES = Decimal("0.000001")


class Asset(str, Enum):
    BTC = "btc"
    ETH = "eth"
    SOL = "sol"


@dataclass(frozen=True)
class BookLevel:
    price: Decimal
    shares: Decimal


@dataclass(frozen=True)
class FeeSchedule:
    rate: Decimal
    exponent: Decimal


@dataclass(frozen=True)
class FillLeg:
    price: Decimal
    shares: Decimal
    quote: Decimal
    fee: Decimal


@dataclass(frozen=True)
class FakResult:
    requested_quote: Decimal | None
    requested_shares: Decimal | None
    submitted_maker_amount: Decimal
    submitted_taker_amount: Decimal
    filled_shares: Decimal
    quote_amount: Decimal
    unfilled_quote: Decimal | None
    unfilled_shares: Decimal | None
    fee: Decimal
    legs: tuple[FillLeg, ...]
    status: str


@dataclass(frozen=True)
class InventoryLot:
    """A positive virtual position lot retained through settlement."""

    token_id: str
    side: str
    shares: Decimal
    source: str

    def __post_init__(self) -> None:
        if not self.token_id:
            raise ValueError("inventory token_id must be nonempty")
        if self.side not in {"UP", "DOWN"}:
            raise ValueError("inventory side must be UP or DOWN")
        if not isinstance(self.shares, Decimal) or not self.shares.is_finite() or self.shares <= 0:
            raise ValueError("inventory shares must be positive and finite")
        if self.source not in {"entry", "reverse_old_residual", "reverse_buy"}:
            raise ValueError("invalid inventory source")


@dataclass(frozen=True)
class ReverseSequence:
    lane: LaneKey
    market_id: str
    mkt_ts: int
    config_hash: str
    old_side: str
    new_side: str
    status: str
    outcome: str
    transitions: tuple[str, ...]
    requested_shares: Decimal
    sold_shares: Decimal
    old_residual_shares: Decimal
    submission_dust_shares: Decimal
    opposite_shares: Decimal
    expected_quote: Decimal
    sell: FakResult
    buy: FakResult | None
    inventory_lots: tuple[InventoryLot, ...]
    sell_book_generation: int
    buy_book_generation: int | None
    trigger_ts_ms: int
    leg_elapsed_ms: int | None
