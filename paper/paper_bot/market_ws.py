from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

from .domain import BookLevel


@dataclass(frozen=True)
class MarketSnapshot:
    token_id: str
    bids: tuple[BookLevel, ...]
    asks: tuple[BookLevel, ...]
    event_ts_ms: int
    sequence: int
    payload: Mapping[str, Any]


@dataclass(frozen=True)
class MarketDelta:
    token_id: str
    side: str
    price: Decimal
    shares: Decimal
    event_ts_ms: int
    sequence: int
    payload: Mapping[str, Any]
    batch_index: int = 0
    batch_size: int = 1
    batch_id: int = 0


@dataclass(frozen=True)
class MarketInvalidation:
    token_ids: tuple[str, ...]


MarketEvent = MarketSnapshot | MarketDelta | MarketInvalidation


async def _enqueue(queue: Any, event: MarketEvent) -> None:
    await getattr(queue, "put")(event)


def _nonnegative_int(value: Any) -> int:
    if isinstance(value, bool):
        raise ValueError
    result = int(value)
    if result < 0 or str(result) != str(value):
        raise ValueError
    return result


def _positive_decimal(value: Any, *, zero_allowed: bool = False) -> Decimal:
    if not isinstance(value, str):
        raise ValueError
    try:
        result = Decimal(value)
    except InvalidOperation as exc:
        raise ValueError from exc
    if not result.is_finite() or result < 0 or (not zero_allowed and result == 0):
        raise ValueError
    return result


def _levels(value: Any) -> tuple[BookLevel, ...]:
    if not isinstance(value, list):
        raise ValueError
    result: list[BookLevel] = []
    for item in value:
        if not isinstance(item, Mapping) or set(item) < {"price", "size"}:
            raise ValueError
        result.append(BookLevel(_positive_decimal(item["price"]), _positive_decimal(item["size"])))
    return tuple(result)


def _mapping_message(raw: str | bytes | Mapping[str, Any]) -> Mapping[str, Any] | None:
    try:
        value = json.loads(raw) if isinstance(raw, (str, bytes)) else raw
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    return value if isinstance(value, Mapping) else None


def parse_market_message(
    raw: str | bytes | Mapping[str, Any], *, sequence: int
) -> tuple[MarketSnapshot | MarketDelta, ...]:
    """Parse official direct frames and the documented public-client envelope."""
    if isinstance(raw, str) and raw == "PONG":
        return ()
    message = _mapping_message(raw)
    if message is None:
        return ()
    try:
        event_type = message.get("event_type", message.get("type"))
        payload = message.get("payload", message)
        if not isinstance(payload, Mapping):
            return ()
        event_ts_ms = _nonnegative_int(payload.get("timestamp", message.get("timestamp")))
        if event_type == "book":
            token_id = payload.get("asset_id", payload.get("tokenId", payload.get("token_id")))
            if not isinstance(token_id, str) or not token_id:
                return ()
            return (MarketSnapshot(
                token_id=token_id,
                bids=_levels(payload.get("bids")),
                asks=_levels(payload.get("asks")),
                event_ts_ms=event_ts_ms,
                sequence=sequence,
                payload=dict(message),
            ),)
        if event_type != "price_change":
            return ()
        changes = payload.get("price_changes", payload.get("priceChanges"))
        if not isinstance(changes, list):
            return ()
        events: list[MarketDelta] = []
        batch_size = len(changes)
        for index, change in enumerate(changes):
            if not isinstance(change, Mapping):
                raise ValueError
            token_id = change.get("asset_id", change.get("tokenId", change.get("token_id")))
            side_value = change.get("side")
            side = "bid" if side_value == "BUY" else "ask" if side_value == "SELL" else None
            if not isinstance(token_id, str) or not token_id or side is None:
                raise ValueError
            events.append(MarketDelta(
                token_id=token_id,
                side=side,
                price=_positive_decimal(change.get("price")),
                shares=_positive_decimal(change.get("size"), zero_allowed=True),
                event_ts_ms=event_ts_ms,
                sequence=sequence + index,
                payload=dict(message),
                batch_index=index,
                batch_size=batch_size,
                batch_id=sequence,
            ))
        return tuple(events)
    except (KeyError, TypeError, ValueError):
        return ()


class MarketWsClient:
    def __init__(
        self,
        connector: Callable[[str], Awaitable[Any]],
        *,
        url: str = "wss://ws-subscriptions-clob.polymarket.com/ws/market",
        ping_interval: float = 10.0,
        reconnect_base: float = 0.5,
        reconnect_max: float = 30.0,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        if ping_interval <= 0 or reconnect_base < 0 or reconnect_max < reconnect_base:
            raise ValueError("invalid websocket timing")
        self.connector = connector
        self.url = url
        self.ping_interval = ping_interval
        self.reconnect_base = reconnect_base
        self.reconnect_max = reconnect_max
        self._sleep = sleep

    @staticmethod
    def _tokens(supplier: Callable[[], Any]) -> tuple[str, ...]:
        tokens = tuple(supplier())
        if not tokens or len(set(tokens)) != len(tokens) or any(not isinstance(item, str) or not item for item in tokens):
            raise ValueError("token supplier must return unique nonempty strings")
        return tokens

    async def _heartbeat(self, websocket: Any, pong: asyncio.Event) -> None:
        while True:
            await self._sleep(self.ping_interval)
            pong.clear()
            await websocket.send("PING")
            try:
                await asyncio.wait_for(pong.wait(), timeout=self.ping_interval)
            except TimeoutError as exc:
                raise ConnectionError("market PONG timeout") from exc

    async def _consume(self, websocket: Any, tokens: tuple[str, ...], queue: Any, pong: asyncio.Event) -> None:
        token_set = set(tokens)
        ready: set[str] = set()
        pending: list[MarketDelta] = []
        receive_sequence = 1
        async for raw in websocket:
            if raw == "PONG":
                pong.set()
                continue
            parsed = parse_market_message(raw, sequence=receive_sequence)
            receive_sequence += max(1, len(parsed))
            for event in parsed:
                if event.token_id not in token_set:
                    continue
                if isinstance(event, MarketSnapshot):
                    ready.add(event.token_id)
                    await _enqueue(queue, event)
                    if ready == token_set and pending:
                        for buffered in pending:
                            await _enqueue(queue, buffered)
                        pending.clear()
                elif event.token_id in ready:
                    if ready == token_set:
                        await _enqueue(queue, event)
                    else:
                        pending.append(event)
        raise ConnectionError("market websocket closed")

    async def _connection(self, tokens: tuple[str, ...], queue: Any) -> None:
        websocket = await self.connector(self.url)
        try:
            await websocket.send(json.dumps({"assets_ids": list(tokens), "type": "market"}, separators=(",", ":")))
            pong = asyncio.Event()
            consumer = asyncio.create_task(self._consume(websocket, tokens, queue, pong))
            heartbeat = asyncio.create_task(self._heartbeat(websocket, pong))
            tasks = (consumer, heartbeat)
            try:
                done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                for task in pending:
                    task.cancel()
                await asyncio.gather(*pending, return_exceptions=True)
                for task in done:
                    task.result()
            finally:
                for task in tasks:
                    if not task.done():
                        task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
        finally:
            close = getattr(websocket, "close", None)
            if close is not None:
                result = close()
                if isinstance(result, Awaitable):
                    await result

    async def run(self, token_ids_supplier: Callable[[], Any], market_event_queue: Any) -> None:
        delay = self.reconnect_base
        while True:
            tokens = self._tokens(token_ids_supplier)
            try:
                await self._connection(tokens, market_event_queue)
            except asyncio.CancelledError:
                raise
            except Exception:
                await _enqueue(market_event_queue, MarketInvalidation(tokens))
                await self._sleep(delay)
                delay = min(self.reconnect_max, max(self.reconnect_base, delay * 2))


__all__ = [
    "MarketDelta", "MarketEvent", "MarketInvalidation", "MarketSnapshot",
    "MarketWsClient", "parse_market_message",
]
