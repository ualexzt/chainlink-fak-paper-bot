from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Awaitable, Callable

from .domain import FeeSchedule

SUPPORTED_TICKS = {Decimal("0.1"), Decimal("0.01"), Decimal("0.001"), Decimal("0.0001")}
ZERO_FEE_SCHEDULE = FeeSchedule(Decimal("0"), Decimal("1"))
_VALID_SYMBOLS = {"btc", "eth", "sol"}
_RFC3339_SECOND_PRECISION = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:Z|[+-]\d{2}:\d{2})$")


class GammaValidationError(ValueError):
    pass


@dataclass(frozen=True)
class MarketDefinition:
    symbol: str
    slug: str
    market_id: str
    mkt_ts: int
    end_ts: int
    up_token_id: str
    down_token_id: str
    tick_size: Decimal
    min_order_shares: Decimal
    fee_schedule: FeeSchedule


JsonGetter = Callable[[str, dict[str, str]], Awaitable[Any]]


def _require_mapping(value: Any, field: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise GammaValidationError(f"{field} must be a mapping")
    return value


def _require_str(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise GammaValidationError(f"{field} must be a nonempty string")
    return value


def _require_bool(value: Any, field: str) -> bool:
    if not isinstance(value, bool):
        raise GammaValidationError(f"{field} must be a bool")
    return value


def _to_decimal(value: Any, field: str) -> Decimal:
    if isinstance(value, bool) or value is None or isinstance(value, str):
        raise GammaValidationError(f"{field} must be numeric")
    if isinstance(value, Decimal):
        result = value
    elif isinstance(value, (int, float)):
        try:
            result = Decimal(str(value))
        except Exception as exc:  # noqa: BLE001
            raise GammaValidationError(f"{field} must be numeric") from exc
    else:
        raise GammaValidationError(f"{field} must be numeric")
    if not result.is_finite():
        raise GammaValidationError(f"{field} must be finite")
    return result


def _to_nonnegative_int(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise GammaValidationError(f"{field} must be a nonnegative integer")
    if value < 0:
        raise GammaValidationError(f"{field} must be a nonnegative integer")
    return value


def _parse_rfc3339_epoch(value: Any, field: str) -> int:
    text = _require_str(value, field)
    if not _RFC3339_SECOND_PRECISION.fullmatch(text):
        raise GammaValidationError(f"{field} must be canonical RFC3339 second precision")
    normalized = text.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise GammaValidationError(f"{field} must be RFC3339") from exc
    if dt.tzinfo is None or dt.utcoffset() is None:
        raise GammaValidationError(f"{field} must be timezone-aware")
    if dt.microsecond:
        raise GammaValidationError(f"{field} must have second precision")
    return int(dt.astimezone(timezone.utc).timestamp())


def _parse_json_string_list(value: Any, field: str) -> tuple[str, str]:
    text = _require_str(value, field)
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        raise GammaValidationError(f"{field} must be JSON") from exc
    if not isinstance(parsed, list) or len(parsed) != 2:
        raise GammaValidationError(f"{field} must contain exactly two strings")
    first, second = parsed
    if not all(isinstance(item, str) for item in parsed):
        raise GammaValidationError(f"{field} must contain exactly two strings")
    return first, second


def _parse_fee_schedule(payload: Mapping[str, Any]) -> FeeSchedule:
    rate = _to_decimal(payload.get("rate"), "feeSchedule.rate")
    exponent = _to_decimal(payload.get("exponent"), "feeSchedule.exponent")
    if rate < 0 or exponent < 0:
        raise GammaValidationError("feeSchedule values must be nonnegative")
    return FeeSchedule(rate, exponent)


def validate_market(payload: Mapping[str, Any], symbol: str, mkt_ts: int) -> MarketDefinition:
    market = _require_mapping(payload, "payload")
    if symbol not in _VALID_SYMBOLS:
        raise GammaValidationError("symbol must be one of btc, eth, sol")
    if isinstance(mkt_ts, bool) or not isinstance(mkt_ts, int) or mkt_ts < 0 or mkt_ts % 300:
        raise GammaValidationError("mkt_ts must be a nonnegative 300-aligned integer")

    slug = _require_str(market.get("slug"), "slug")
    expected_slug = f"{symbol}-updown-5m-{mkt_ts}"
    if slug != expected_slug:
        raise GammaValidationError("slug mismatch")

    market_id = _require_str(market.get("id"), "id")
    start_ts = _parse_rfc3339_epoch(market.get("eventStartTime"), "eventStartTime")
    end_ts = _parse_rfc3339_epoch(market.get("endDate"), "endDate")
    if start_ts != mkt_ts or end_ts != mkt_ts + 300:
        raise GammaValidationError("market interval mismatch")

    if _require_bool(market.get("enableOrderBook"), "enableOrderBook") is not True:
        raise GammaValidationError("enableOrderBook must be true")

    seconds_delay = market.get("secondsDelay", 0)
    if isinstance(seconds_delay, bool) or seconds_delay is None:
        raise GammaValidationError("secondsDelay must be numeric or omitted")
    delay = _to_decimal(seconds_delay, "secondsDelay")
    if delay != 0:
        raise GammaValidationError("secondsDelay must be zero when configured")

    outcomes = _parse_json_string_list(market.get("outcomes"), "outcomes")
    token_ids = _parse_json_string_list(market.get("clobTokenIds"), "clobTokenIds")
    labels = set(outcomes)
    if labels != {"Up", "Down"}:
        raise GammaValidationError("outcomes must contain one Up and one Down")
    if len({token_ids[0], token_ids[1]}) != 2 or not token_ids[0] or not token_ids[1]:
        raise GammaValidationError("clobTokenIds must contain two distinct nonempty strings")

    up_index = outcomes.index("Up")
    down_index = outcomes.index("Down")
    up_token_id = token_ids[up_index]
    down_token_id = token_ids[down_index]
    if up_token_id == down_token_id:
        raise GammaValidationError("clobTokenIds must be distinct")

    tick_size = _to_decimal(market.get("orderPriceMinTickSize"), "orderPriceMinTickSize")
    if tick_size not in SUPPORTED_TICKS or tick_size <= 0:
        raise GammaValidationError("orderPriceMinTickSize must be one of the supported official ticks")

    min_order_shares = _to_decimal(market.get("orderMinSize"), "orderMinSize")
    if min_order_shares <= 0:
        raise GammaValidationError("orderMinSize must be positive")

    resolution_source = _require_str(market.get("resolutionSource"), "resolutionSource")
    expected_source = f"https://data.chain.link/streams/{symbol}-usd-twap-60s-streams"
    if resolution_source != expected_source:
        raise GammaValidationError("resolutionSource mismatch")

    config_id = market.get("cryptoMarketConfigId")
    if config_id is not None:
        if _require_str(config_id, "cryptoMarketConfigId") != f"{symbol}-5m-twap-60":
            raise GammaValidationError("cryptoMarketConfigId mismatch")

    config = market.get("cryptoMarketConfig")
    if config is not None:
        config_map = _require_mapping(config, "cryptoMarketConfig")
        if _require_str(config_map.get("id"), "cryptoMarketConfig.id") != f"{symbol}-5m-twap-60":
            raise GammaValidationError("cryptoMarketConfig.id mismatch")
        if _require_str(config_map.get("asset"), "cryptoMarketConfig.asset") != symbol:
            raise GammaValidationError("cryptoMarketConfig.asset mismatch")
        if _require_str(config_map.get("duration"), "cryptoMarketConfig.duration") != "5m":
            raise GammaValidationError("cryptoMarketConfig.duration mismatch")
        if _require_bool(config_map.get("twapEnabled"), "cryptoMarketConfig.twapEnabled") is not True:
            raise GammaValidationError("cryptoMarketConfig.twapEnabled must be true")
        lookback = _to_decimal(config_map.get("twapLookbackSeconds"), "cryptoMarketConfig.twapLookbackSeconds")
        if lookback != 60:
            raise GammaValidationError("cryptoMarketConfig.twapLookbackSeconds mismatch")
        if config_id is not None and config_map.get("id") != config_id:
            raise GammaValidationError("cryptoMarketConfig and cryptoMarketConfigId mismatch")

    fees_enabled = _require_bool(market.get("feesEnabled"), "feesEnabled")
    if fees_enabled:
        fee_payload = _require_mapping(market.get("feeSchedule"), "feeSchedule")
        fee_schedule = _parse_fee_schedule(fee_payload)
    else:
        fee_schedule = ZERO_FEE_SCHEDULE

    return MarketDefinition(
        symbol=symbol,
        slug=slug,
        market_id=market_id,
        mkt_ts=mkt_ts,
        end_ts=end_ts,
        up_token_id=up_token_id,
        down_token_id=down_token_id,
        tick_size=tick_size,
        min_order_shares=min_order_shares,
        fee_schedule=fee_schedule,
    )


@dataclass(frozen=True)
class GammaClient:
    base_url: str
    get_json: JsonGetter

    async def get_market_by_slug(
        self, slug: str, *, include_closed: bool = False,
    ) -> Mapping[str, Any] | None:
        if not isinstance(include_closed, bool):
            raise GammaValidationError("include_closed must be boolean")
        params = {"slug": slug}
        if include_closed:
            params["closed"] = "true"
        response = await self.get_json(self.base_url.rstrip("/") + "/markets", params)
        if not isinstance(response, list):
            raise GammaValidationError("Gamma list response must be a list")
        if not response:
            return None
        if len(response) != 1:
            raise GammaValidationError("Gamma list response must contain at most one market")
        item = response[0]
        if not isinstance(item, Mapping):
            raise GammaValidationError("Gamma market entry must be a mapping")
        return item

    async def discover_current_and_next(self, symbols: Iterable[str], now: int) -> tuple[MarketDefinition, ...]:
        if isinstance(now, bool):
            raise GammaValidationError("now must be a nonnegative epoch second")
        now_decimal = _to_decimal(now, "now")
        if now_decimal < 0:
            raise GammaValidationError("now must be a nonnegative epoch second")
        aligned = int(now_decimal // Decimal("300") * Decimal("300"))

        results: list[MarketDefinition] = []
        for symbol in symbols:
            if symbol not in _VALID_SYMBOLS:
                raise GammaValidationError("symbol must be one of btc, eth, sol")
            for mkt_ts in (aligned, aligned + 300):
                slug = f"{symbol}-updown-5m-{mkt_ts}"
                payload = await self.get_market_by_slug(slug)
                if payload is None:
                    continue
                results.append(validate_market(payload, symbol, mkt_ts))
        return tuple(results)


__all__ = ["GammaClient", "GammaValidationError", "MarketDefinition", "validate_market"]
