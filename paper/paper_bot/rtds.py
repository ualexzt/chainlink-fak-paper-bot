from __future__ import annotations

import asyncio
import json
import re
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

SYMBOLS = ("btc", "eth", "sol")
TOPIC = "crypto_prices_twap_sixty"
E18 = Decimal(10) ** 18
_SIGNED_INTEGER = re.compile(r"-?(?:0|[1-9][0-9]*)\Z")


@dataclass(frozen=True)
class ResolverObservation:
    symbol: str
    value: Decimal
    observation_ts_ms: int
    receive_ts_ms: int
    payload: Mapping[str, Any]


async def _enqueue(queue: Any, observation: ResolverObservation) -> None:
    await getattr(queue, "put")(observation)


def parse_e18(raw: Any) -> Decimal:
    if not isinstance(raw, str) or _SIGNED_INTEGER.fullmatch(raw) is None:
        raise ValueError("full_accuracy_value must be a canonical signed integer")
    return Decimal(int(raw)) / E18


def _messages(raw: str | bytes | Mapping[str, Any] | list[Any]) -> tuple[Any, ...]:
    try:
        value = json.loads(raw) if isinstance(raw, (str, bytes)) else raw
    except (UnicodeDecodeError, json.JSONDecodeError):
        return ()
    return tuple(value) if isinstance(value, list) else (value,)


def parse_rtds_message(
    raw: str | bytes | Mapping[str, Any] | list[Any],
    *,
    receive_ts_ms: int,
    symbols: tuple[str, ...] = SYMBOLS,
    stale_after_ms: int = 10_000,
) -> tuple[ResolverObservation, ...]:
    if isinstance(receive_ts_ms, bool) or not isinstance(receive_ts_ms, int) or receive_ts_ms <= 0:
        raise ValueError("receive_ts_ms must be a positive integer")
    allowed = set(symbols)
    observations: list[ResolverObservation] = []
    for message in _messages(raw):
        try:
            if not isinstance(message, Mapping):
                continue
            if message.get("topic") != TOPIC or message.get("type") != "update":
                continue
            payload = message.get("payload")
            if not isinstance(payload, Mapping) or payload.get("window_s") != 60:
                continue
            pair = payload.get("symbol")
            if not isinstance(pair, str) or pair != pair.lower() or not pair.endswith("/usd"):
                continue
            symbol = pair.removesuffix("/usd")
            if symbol not in allowed:
                continue
            observation_ts_ms = payload.get("timestamp")
            if isinstance(observation_ts_ms, bool) or not isinstance(observation_ts_ms, int):
                continue
            age = receive_ts_ms - observation_ts_ms
            if observation_ts_ms <= 0 or age < 0 or age > stale_after_ms:
                continue
            value = parse_e18(payload.get("full_accuracy_value"))
            if value <= 0:
                continue
            observations.append(ResolverObservation(
                symbol=symbol,
                value=value,
                observation_ts_ms=observation_ts_ms,
                receive_ts_ms=receive_ts_ms,
                payload=dict(message),
            ))
        except (ArithmeticError, ValueError):
            continue
    return tuple(observations)


class RtdsClient:
    def __init__(
        self,
        connector: Callable[[str], Awaitable[Any]],
        *,
        url: str = "wss://ws-live-data.polymarket.com",
        symbols: tuple[str, ...] = SYMBOLS,
        ping_interval: float = 5.0,
        stale_after_ms: int = 10_000,
        reconnect_base: float = 0.5,
        reconnect_max: float = 30.0,
        clock_ms: Callable[[], int] = lambda: time.time_ns() // 1_000_000,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        if (
            not symbols or len(set(symbols)) != len(symbols)
            or any(not isinstance(symbol, str) or symbol not in SYMBOLS for symbol in symbols)
        ):
            raise ValueError("symbols must be unique lowercase strings")
        if ping_interval <= 0 or stale_after_ms <= 0 or reconnect_base < 0 or reconnect_max < reconnect_base:
            raise ValueError("invalid RTDS timing")
        self.connector = connector
        self.url = url
        self.symbols = symbols
        self.ping_interval = ping_interval
        self.stale_after_ms = stale_after_ms
        self.reconnect_base = reconnect_base
        self.reconnect_max = reconnect_max
        self._clock_ms = clock_ms
        self._sleep = sleep
        self._last_observation = {symbol: 0 for symbol in symbols}

    async def _heartbeat(
        self, websocket: Any, refreshed: dict[str, int], pong: asyncio.Event
    ) -> None:
        while True:
            await self._sleep(self.ping_interval)
            pong.clear()
            await websocket.send("PING")
            try:
                await asyncio.wait_for(pong.wait(), timeout=self.ping_interval)
            except TimeoutError as exc:
                raise ConnectionError("RTDS PONG timeout") from exc
            now_ms = self._clock_ms()
            if any(now_ms - refreshed[symbol] > self.stale_after_ms for symbol in self.symbols):
                raise ConnectionError("RTDS TWAP-60 watchdog stale")

    async def _consume(
        self, websocket: Any, queue: Any, refreshed: dict[str, int], pong: asyncio.Event
    ) -> None:
        async for raw in websocket:
            if raw == "PONG":
                pong.set()
                continue
            receive_ts_ms = self._clock_ms()
            for observation in parse_rtds_message(
                raw, receive_ts_ms=receive_ts_ms, symbols=self.symbols,
                stale_after_ms=self.stale_after_ms,
            ):
                if observation.observation_ts_ms <= self._last_observation[observation.symbol]:
                    continue
                self._last_observation[observation.symbol] = observation.observation_ts_ms
                refreshed[observation.symbol] = receive_ts_ms
                await _enqueue(queue, observation)
        raise ConnectionError("RTDS websocket closed")

    async def _connection(self, queue: Any) -> None:
        websocket = await self.connector(self.url)
        try:
            frame = {
                "action": "subscribe",
                "subscriptions": [{"topic": TOPIC, "type": "update"}],
            }
            await websocket.send(json.dumps(frame, separators=(",", ":")))
            started = self._clock_ms()
            refreshed = {symbol: started for symbol in self.symbols}
            pong = asyncio.Event()
            consumer = asyncio.create_task(self._consume(websocket, queue, refreshed, pong))
            heartbeat = asyncio.create_task(self._heartbeat(websocket, refreshed, pong))
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

    async def run(self, resolver_event_queue: Any) -> None:
        delay = self.reconnect_base
        while True:
            try:
                await self._connection(resolver_event_queue)
            except asyncio.CancelledError:
                raise
            except Exception:
                await self._sleep(delay)
                delay = min(self.reconnect_max, max(self.reconnect_base, delay * 2))


__all__ = ["ResolverObservation", "RtdsClient", "SYMBOLS", "TOPIC", "parse_e18", "parse_rtds_message"]
