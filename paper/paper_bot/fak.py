from __future__ import annotations

from decimal import Decimal, ROUND_DOWN, ROUND_UP, localcontext
from typing import Iterable

from .domain import SIX_PLACES, BookLevel, FakResult, FeeSchedule, FillLeg

TWO_PLACES = Decimal("0.01")
SUPPORTED_TICK_PROFILES: dict[Decimal, tuple[int, int]] = {
    Decimal("0.1"): (1, 3),
    Decimal("0.01"): (2, 4),
    Decimal("0.001"): (3, 5),
    Decimal("0.0001"): (4, 6),
}
SUPPORTED_TICKS = tuple(SUPPORTED_TICK_PROFILES)


def _q6(value: Decimal) -> Decimal:
    return value.quantize(SIX_PLACES, rounding=ROUND_DOWN)


def _q2(value: Decimal) -> Decimal:
    return value.quantize(TWO_PLACES, rounding=ROUND_DOWN)


def _is_supported_tick(tick_size: Decimal) -> bool:
    return tick_size in SUPPORTED_TICK_PROFILES


def _tick_profile(tick_size: Decimal) -> tuple[int, int]:
    if not tick_size.is_finite() or tick_size <= 0:
        raise ValueError("tick_size must be positive and finite")
    try:
        return SUPPORTED_TICK_PROFILES[tick_size]
    except KeyError as exc:
        raise ValueError("unsupported tick size") from exc


def _validate_order_price(price: Decimal, tick_size: Decimal) -> None:
    if not price.is_finite() or price <= 0 or price >= 1:
        raise ValueError("price must be in (0, 1) and finite")
    if price % tick_size != 0:
        raise ValueError("price not aligned to tick")


def _validate_supported_price(price: Decimal) -> None:
    if not price.is_finite() or price <= 0 or price >= 1:
        raise ValueError("price must be in (0, 1) and finite")
    if not any(price % tick == 0 for tick in SUPPORTED_TICKS):
        raise ValueError("price not aligned to supported tick")


def _validate_level(level: BookLevel, tick_size: Decimal) -> None:
    _validate_order_price(level.price, tick_size)
    if not level.shares.is_finite() or level.shares < 0:
        raise ValueError("shares must be non-negative and finite")


def _materialize_and_validate_levels(levels: Iterable[BookLevel], tick_size: Decimal) -> tuple[BookLevel, ...]:
    materialized = tuple(levels)
    for level in materialized:
        _validate_level(level, tick_size)
    return materialized


def _materialize_and_validate_quote_levels(levels: Iterable[BookLevel]) -> tuple[BookLevel, ...]:
    materialized = tuple(levels)
    for level in materialized:
        _validate_supported_price(level.price)
        if not level.shares.is_finite() or level.shares < 0:
            raise ValueError("shares must be non-negative and finite")
    return materialized


def _counter_amount_precision(tick_size: Decimal) -> int:
    return _tick_profile(tick_size)[1]


def _sdk_counter_amount(raw_amount: Decimal, precision: int) -> Decimal:
    if not raw_amount.is_finite():
        raise ValueError("amount must be finite")
    guard = Decimal(1).scaleb(-(precision + 4))
    target = Decimal(1).scaleb(-precision)
    return raw_amount.quantize(guard, rounding=ROUND_UP).quantize(target, rounding=ROUND_DOWN)


def _validate_fee_schedule(fee_schedule: FeeSchedule) -> None:
    if not fee_schedule.rate.is_finite() or fee_schedule.rate < 0:
        raise ValueError("fee rate must be non-negative and finite")
    if not fee_schedule.exponent.is_finite() or fee_schedule.exponent < 0:
        raise ValueError("fee exponent must be non-negative and finite")


def _fee_for_fill(price: Decimal, shares: Decimal, fee_schedule: FeeSchedule) -> Decimal:
    if shares <= 0:
        return Decimal("0.000000")
    per_share_fee = fee_schedule.rate * (price * (Decimal("1") - price)) ** fee_schedule.exponent
    return _q6(shares * per_share_fee)


def _status_from_fill(filled: Decimal, submitted: Decimal) -> str:
    if filled == 0:
        return "zero"
    if filled == submitted:
        return "full"
    return "partial"


def _build_result(
    *,
    requested_quote: Decimal | None,
    requested_shares: Decimal | None,
    submitted_maker_amount: Decimal,
    submitted_taker_amount: Decimal,
    filled_shares: Decimal,
    quote_amount: Decimal,
    unfilled_quote: Decimal | None,
    unfilled_shares: Decimal | None,
    fee: Decimal,
    legs: list[FillLeg],
    status: str,
) -> FakResult:
    return FakResult(
        requested_quote=None if requested_quote is None else _q6(requested_quote),
        requested_shares=None if requested_shares is None else _q6(requested_shares),
        submitted_maker_amount=_q6(submitted_maker_amount),
        submitted_taker_amount=_q6(submitted_taker_amount),
        filled_shares=_q6(filled_shares),
        quote_amount=_q6(quote_amount),
        unfilled_quote=None if unfilled_quote is None else _q6(unfilled_quote),
        unfilled_shares=None if unfilled_shares is None else _q6(unfilled_shares),
        fee=_q6(fee),
        legs=tuple(legs),
        status=status,
    )


def _buy_target_amount(requested_usdc: Decimal, max_price: Decimal, precision: int) -> Decimal:
    raw_target = requested_usdc / max_price
    return _sdk_counter_amount(raw_target, precision)


def _sell_quote_amount(maker_amount: Decimal, min_price: Decimal, precision: int) -> Decimal:
    raw_quote = maker_amount * min_price
    return _sdk_counter_amount(raw_quote, precision)


def simulate_buy_fak(
    asks: Iterable[BookLevel],
    requested_usdc: Decimal,
    max_price: Decimal,
    tick_size: Decimal,
    min_order_shares: Decimal,
    fee_schedule: FeeSchedule,
) -> FakResult:
    with localcontext() as ctx:
        ctx.prec = 50
        ctx.rounding = ROUND_DOWN
        if not requested_usdc.is_finite() or requested_usdc < 0:
            raise ValueError("requested_usdc must be non-negative and finite")
        if not min_order_shares.is_finite() or min_order_shares < 0:
            raise ValueError("min_order_shares must be non-negative and finite")
        _validate_fee_schedule(fee_schedule)
        precision = _counter_amount_precision(tick_size)
        _validate_order_price(max_price, tick_size)

        requested_quote = _q6(requested_usdc)
        submitted_maker_amount = _q6(_q2(requested_usdc))
        submitted_taker_amount = _q6(_buy_target_amount(submitted_maker_amount, max_price, precision))
        min_order_shares_q6 = _q6(min_order_shares)
        if min_order_shares_q6 <= 0:
            raise ValueError("min_order_shares quantized to zero or negative")

        asks = _materialize_and_validate_levels(asks, tick_size)
        if submitted_taker_amount < min_order_shares_q6:
            return _build_result(
                requested_quote=requested_quote,
                requested_shares=None,
                submitted_maker_amount=submitted_maker_amount,
                submitted_taker_amount=submitted_taker_amount,
                filled_shares=Decimal("0"),
                quote_amount=Decimal("0"),
                unfilled_quote=requested_quote,
                unfilled_shares=submitted_taker_amount,
                fee=Decimal("0"),
                legs=[],
                status="zero",
            )

        legs: list[FillLeg] = []
        remaining_target = submitted_taker_amount
        filled_shares = Decimal("0")
        quote_amount = Decimal("0")
        fee = Decimal("0")

        for level in sorted(asks, key=lambda item: item.price):
            if remaining_target <= 0:
                break
            if level.price > max_price:
                break
            level_shares = _q6(level.shares)
            if level_shares <= 0:
                continue
            fill_shares = min(level_shares, remaining_target)
            fill_shares = _q6(fill_shares)
            if fill_shares <= 0:
                continue
            fill_quote = _q6(fill_shares * level.price)
            fill_fee = _fee_for_fill(level.price, fill_shares, fee_schedule)
            legs.append(FillLeg(price=_q6(level.price), shares=fill_shares, quote=fill_quote, fee=fill_fee))
            filled_shares += fill_shares
            quote_amount += fill_quote
            fee += fill_fee
            remaining_target = _q6(remaining_target - fill_shares)

        status = _status_from_fill(filled_shares, submitted_taker_amount)
        return _build_result(
            requested_quote=requested_quote,
            requested_shares=None,
            submitted_maker_amount=submitted_maker_amount,
            submitted_taker_amount=submitted_taker_amount,
            filled_shares=filled_shares,
            quote_amount=quote_amount,
            unfilled_quote=requested_quote - quote_amount,
            unfilled_shares=submitted_taker_amount - filled_shares,
            fee=fee,
            legs=legs,
            status=status,
        )


def simulate_sell_fak(
    bids: Iterable[BookLevel],
    requested_shares: Decimal,
    min_price: Decimal,
    tick_size: Decimal,
    min_order_shares: Decimal,
    fee_schedule: FeeSchedule,
) -> FakResult:
    with localcontext() as ctx:
        ctx.prec = 50
        ctx.rounding = ROUND_DOWN
        if not requested_shares.is_finite() or requested_shares < 0:
            raise ValueError("requested_shares must be non-negative and finite")
        if not min_order_shares.is_finite() or min_order_shares < 0:
            raise ValueError("min_order_shares must be non-negative and finite")
        _validate_fee_schedule(fee_schedule)
        precision = _counter_amount_precision(tick_size)
        _validate_order_price(min_price, tick_size)

        requested_shares_q6 = _q6(requested_shares)
        submitted_maker_amount = _q6(_q2(requested_shares))
        submitted_taker_amount = _q6(_sell_quote_amount(submitted_maker_amount, min_price, precision))
        min_order_shares_q6 = _q6(min_order_shares)
        if min_order_shares_q6 <= 0:
            raise ValueError("min_order_shares quantized to zero or negative")

        bids = _materialize_and_validate_levels(bids, tick_size)
        if submitted_maker_amount < min_order_shares_q6:
            return _build_result(
                requested_quote=None,
                requested_shares=requested_shares_q6,
                submitted_maker_amount=submitted_maker_amount,
                submitted_taker_amount=submitted_taker_amount,
                filled_shares=Decimal("0"),
                quote_amount=Decimal("0"),
                unfilled_quote=None,
                unfilled_shares=requested_shares_q6,
                fee=Decimal("0"),
                legs=[],
                status="zero",
            )

        legs: list[FillLeg] = []
        remaining_shares = submitted_maker_amount
        filled_shares = Decimal("0")
        quote_amount = Decimal("0")
        fee = Decimal("0")

        for level in sorted(bids, key=lambda item: item.price, reverse=True):
            if remaining_shares <= 0:
                break
            if level.price < min_price:
                break
            level_shares = _q6(level.shares)
            if level_shares <= 0:
                continue
            fill_shares = min(level_shares, remaining_shares)
            fill_shares = _q6(fill_shares)
            if fill_shares <= 0:
                continue
            fill_quote = _q6(fill_shares * level.price)
            fill_fee = _fee_for_fill(level.price, fill_shares, fee_schedule)
            legs.append(FillLeg(price=_q6(level.price), shares=fill_shares, quote=fill_quote, fee=fill_fee))
            filled_shares += fill_shares
            quote_amount += fill_quote
            fee += fill_fee
            remaining_shares = _q6(remaining_shares - fill_shares)

        status = _status_from_fill(filled_shares, submitted_maker_amount)
        return _build_result(
            requested_quote=None,
            requested_shares=requested_shares_q6,
            submitted_maker_amount=submitted_maker_amount,
            submitted_taker_amount=submitted_taker_amount,
            filled_shares=filled_shares,
            quote_amount=quote_amount,
            unfilled_quote=None,
            unfilled_shares=requested_shares_q6 - filled_shares,
            fee=fee,
            legs=legs,
            status=status,
        )


def quote_for_target_shares(asks: Iterable[BookLevel], target_shares: Decimal, max_price: Decimal) -> Decimal:
    with localcontext() as ctx:
        ctx.prec = 50
        ctx.rounding = ROUND_DOWN
        if not target_shares.is_finite() or target_shares < 0:
            raise ValueError("target_shares must be non-negative and finite")
        _validate_supported_price(max_price)
        target_shares_q6 = _q6(target_shares)
        asks = _materialize_and_validate_quote_levels(asks)
        remaining = target_shares_q6
        quote = Decimal("0")
        for level in sorted(asks, key=lambda item: item.price):
            if remaining <= 0:
                break
            if level.price > max_price:
                break
            level_shares = _q6(level.shares)
            if level_shares <= 0:
                continue
            fill_shares = min(level_shares, remaining)
            fill_shares = _q6(fill_shares)
            if fill_shares <= 0:
                continue
            level_quote = _q6(fill_shares * level.price)
            quote += level_quote
            remaining = _q6(remaining - fill_shares)
        return _q6(quote)


def buy_maker_amount_for_target_shares(target_shares: Decimal, max_price: Decimal, tick_size: Decimal) -> Decimal:
    with localcontext() as ctx:
        ctx.prec = 50
        ctx.rounding = ROUND_DOWN
        if not target_shares.is_finite() or target_shares < 0:
            raise ValueError("target_shares must be non-negative and finite")
        precision = _counter_amount_precision(tick_size)
        _validate_order_price(max_price, tick_size)
        target_shares_q6 = _q6(target_shares)
        threshold = target_shares_q6 + Decimal(1).scaleb(-precision)
        upper = _q2(threshold * max_price)
        low_cents = 0
        high_cents = int((upper * 100).to_integral_value(rounding=ROUND_DOWN))
        best_cents = 0
        while low_cents <= high_cents:
            mid_cents = (low_cents + high_cents) // 2
            candidate = Decimal(mid_cents).scaleb(-2)
            candidate_taker = _q6(_buy_target_amount(candidate, max_price, precision))
            if candidate_taker <= target_shares_q6:
                best_cents = mid_cents
                low_cents = mid_cents + 1
            else:
                high_cents = mid_cents - 1
        return _q2(Decimal(best_cents).scaleb(-2))


__all__ = [
    "SIX_PLACES",
    "BookLevel",
    "FeeSchedule",
    "FillLeg",
    "FakResult",
    "buy_maker_amount_for_target_shares",
    "quote_for_target_shares",
    "simulate_buy_fak",
    "simulate_sell_fak",
]
