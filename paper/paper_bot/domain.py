from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import Enum

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
