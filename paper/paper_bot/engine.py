from __future__ import annotations

import asyncio
import sqlite3
import time
from collections.abc import Awaitable, Callable, Mapping
from datetime import datetime, timezone
from typing import Any

from .accounting import LanePosition, settle_lane
from .books import InvalidBook, OrderBook
from .config import Settings
from .gamma import GammaClient, MarketDefinition
from .journal import JournalError, RawEvent, RawJournal
from .market_ws import MarketDelta, MarketInvalidation, MarketSnapshot, MarketWsClient
from .resolver import ResolverState
from .rtds import ResolverObservation, RtdsClient
from .settlement import OfficialSettlement, parse_official_settlement
from .storage import PersistedMarketState, Storage
from .strategy import LaneKey, MarketStrategyState, StrategyEvent

UTC = timezone.utc


class EngineInvariantError(RuntimeError):
    pass


async def _enqueue(queue: Any, event: Any) -> None:
    await getattr(queue, "put")(event)


class PaperEngine:
    def __init__(
        self,
        settings: Settings,
        *,
        gamma: GammaClient,
        storage: Storage,
        journal: RawJournal,
        market_ws: MarketWsClient | None = None,
        rtds: RtdsClient | None = None,
        settlement_fetcher: Callable[[MarketDefinition], Awaitable[Any]] | None = None,
        clock_s: Callable[[], int] = lambda: int(time.time()),
        clock_ms: Callable[[], int] = lambda: time.time_ns() // 1_000_000,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        queue_maxsize: int = 1024,
        discovery_interval: float = 30.0,
        settlement_interval: float = 15.0,
        heartbeat_interval: float = 1.0,
    ) -> None:
        if not isinstance(settings, Settings):
            raise TypeError("settings must be Settings")
        if isinstance(queue_maxsize, bool) or not isinstance(queue_maxsize, int) or queue_maxsize <= 0:
            raise ValueError("queue_maxsize must be a positive integer")
        if min(discovery_interval, settlement_interval, heartbeat_interval) <= 0:
            raise ValueError("engine intervals must be positive")
        self.settings = settings
        self.gamma = gamma
        self.storage = storage
        self.journal = journal
        self.market_ws = market_ws
        self.rtds = rtds
        self.settlement_fetcher = settlement_fetcher
        self._clock_s = clock_s
        self._clock_ms = clock_ms
        self._sleep = sleep
        self.discovery_interval = discovery_interval
        self.settlement_interval = settlement_interval
        self.heartbeat_interval = heartbeat_interval
        self.queue: asyncio.Queue[Any] = asyncio.Queue(maxsize=queue_maxsize)
        self.resolver = ResolverState(settings.symbols)
        self.markets: dict[str, MarketDefinition] = {}
        self.books: dict[str, OrderBook] = {}
        self.strategies: dict[str, MarketStrategyState] = {}
        self.positions: dict[str, dict[LaneKey, LanePosition]] = {}
        self._token_market: dict[str, str] = {}
        self._settled: set[str] = set()
        self._inactive: set[str] = set()
        self._tasks: list[asyncio.Task[Any]] = []
        self._stop_event = asyncio.Event()
        self._subscriptions_changed = asyncio.Event()
        self._initialized = False
        self._running = False
        self.experiment_hash: str | None = None
        self.storage_critical_reason: str | None = None
        self.processing_critical_reason: str | None = None
        self.discovery_critical_reason: str | None = None
        self.settlement_critical_reason: str | None = None
        self._pending_storage_events: tuple[Any, ...] | None = None
        self._market_batch_journal: dict[tuple[str, int, int], bool] = {}

    @staticmethod
    def _clock_value(value: Any, name: str) -> int:
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise EngineInvariantError(f"{name} must return a nonnegative integer")
        return value

    def _now_s(self) -> int:
        return self._clock_value(self._clock_s(), "clock_s")

    def _now_ms(self) -> int:
        return self._clock_value(self._clock_ms(), "clock_ms")

    async def initialize(self) -> None:
        if self._initialized:
            return
        self.storage.initialize()
        self.experiment_hash = self.storage.ensure_experiment(self.settings)
        restored = self.storage.load_open_market_states()
        foreign = tuple(state for state in restored if state.experiment_hash != self.experiment_hash)
        if foreign:
            raise EngineInvariantError("open state belongs to another experiment version")

        discovery_times = {self._now_s()}
        discovery_times.update(state.mkt_ts for state in restored)
        discovered: list[MarketDefinition] = []
        for at in sorted(discovery_times):
            discovered.extend(await self.gamma.discover_current_and_next(self.settings.symbols, at))
        by_id = {state.market_id: state for state in restored}
        now_s = self._now_s()
        self._register_markets(
            market for market in discovered
            if market.end_ts > now_s or market.market_id in by_id
        )
        missing = set(by_id) - set(self.markets)
        if missing:
            raise EngineInvariantError("persisted market could not be rediscovered")
        for market_id, persisted in by_id.items():
            self._restore_market(persisted, self.markets[market_id])
        self._initialized = True

    def _strategy(self) -> MarketStrategyState:
        assert self.experiment_hash is not None
        return MarketStrategyState(
            thresholds=self.settings.thresholds,
            paper_notional_usd=self.settings.paper_notional_usd,
            config_hash=self.experiment_hash,
        )

    def _register_markets(self, definitions: Any) -> None:
        before = self.token_ids()
        for market in definitions:
            if not isinstance(market, MarketDefinition):
                raise EngineInvariantError("Gamma returned an invalid market definition")
            existing = self.markets.get(market.market_id)
            if existing is not None and existing != market:
                raise EngineInvariantError("market definition changed")
            for token_id in (market.up_token_id, market.down_token_id):
                owner = self._token_market.get(token_id)
                if owner is not None and owner != market.market_id:
                    raise EngineInvariantError("token belongs to multiple markets")
                self._token_market[token_id] = market.market_id
                self.books.setdefault(token_id, OrderBook())
            self.markets[market.market_id] = market
            self.strategies.setdefault(market.market_id, self._strategy())
            self.positions.setdefault(market.market_id, {})
        if self._running and self.token_ids() != before:
            self._subscriptions_changed.set()

    def _restore_market(self, persisted: PersistedMarketState, market: MarketDefinition) -> None:
        if (persisted.market_id, persisted.mkt_ts, persisted.close_ts) != (
            market.market_id, market.mkt_ts, market.end_ts,
        ):
            raise EngineInvariantError("persisted market identity changed")
        strategy = self.strategies[market.market_id]
        reversed_lanes = tuple(
            record.lane for record in persisted.lane_records if record.reverse_attempted
        )
        strategy.restore_attempts(persisted.attempted_lane_keys, reversed_lanes)
        restored_positions: dict[LaneKey, LanePosition] = {}
        for record in persisted.lane_records:
            if record.entry.config_hash != persisted.experiment_hash:
                raise EngineInvariantError("persisted entry experiment mismatch")
            restored_positions[record.lane] = LanePosition(
                market.market_id, market.end_ts, record.lane,
                persisted.experiment_hash, record.entry, record.reverse,
            )
        self.positions[market.market_id] = restored_positions

    def token_ids(self) -> tuple[str, ...]:
        return tuple(sorted(
            token_id for token_id, market_id in self._token_market.items()
            if market_id not in self._inactive
        ))

    def _retire_expired_empty_markets(self, now_s: int) -> None:
        before = self.token_ids()
        for market_id, market in self.markets.items():
            if market.end_ts <= now_s and not self.positions[market_id]:
                self._inactive.add(market_id)
        if self._running and self.token_ids() != before:
            self._subscriptions_changed.set()

    def _journal_market_event(self, event: MarketSnapshot | MarketDelta) -> bool:
        market_id = self._token_market.get(event.token_id)
        market = None if market_id is None else self.markets.get(market_id)
        try:
            self.journal.append(RawEvent(
                source="market_ws",
                receive_ts_ms=self._now_ms(),
                source_ts_ms=event.event_ts_ms,
                symbol=None if market is None else market.symbol,
                token_id=event.token_id,
                payload=event.payload,
            ))
            return True
        except (JournalError, OSError, TypeError, ValueError):
            return False

    def _journal_resolver_event(self, event: ResolverObservation) -> bool:
        try:
            self.journal.append(RawEvent(
                source="rtds",
                receive_ts_ms=event.receive_ts_ms,
                source_ts_ms=event.observation_ts_ms,
                symbol=event.symbol,
                token_id=None,
                payload=event.payload,
            ))
            return True
        except (JournalError, OSError, TypeError, ValueError):
            return False

    def _strategy_writable(self, journaled: bool) -> bool:
        return (
            journaled
            and self.storage_critical_reason is None
            and self.processing_critical_reason is None
            and self.discovery_critical_reason is None
            and self.journal.writable()
        )

    def _book_mapping(self, market: MarketDefinition) -> dict[str, OrderBook]:
        return {
            market.up_token_id: self.books[market.up_token_id],
            market.down_token_id: self.books[market.down_token_id],
        }

    def _reverse_events(self, market: MarketDefinition, event_ts_ms: int, now_s: int) -> tuple[Any, ...]:
        results: list[Any] = []
        strategy = self.strategies[market.market_id]
        for lane, position in tuple(self.positions[market.market_id].items()):
            if position.reverse is not None:
                continue
            reverse = strategy.on_reverse_event(
                lane, position.entry, market, self._book_mapping(market), self.resolver,
                event_ts_ms, now_s,
            )
            if reverse is not None:
                results.append(reverse)
        return tuple(results)

    async def _persist_events(self, events: tuple[Any, ...]) -> bool:
        if not events:
            return True
        for attempt, delay in enumerate((0.05, 0.2, 0.5), start=1):
            try:
                self.storage.record_strategy_events(events)
                return True
            except sqlite3.OperationalError as exc:
                if "locked" not in str(exc).lower() and "busy" not in str(exc).lower():
                    self.storage_critical_reason = type(exc).__name__
                    return False
                if attempt == 3:
                    self.storage_critical_reason = "sqlite_lock_retry_exhausted"
                    self._pending_storage_events = events
                    return False
                await self._sleep(delay)
            except Exception as exc:
                self.storage_critical_reason = type(exc).__name__
                return False
        return False

    async def _retry_pending_storage(self) -> None:
        events = self._pending_storage_events
        if events is None:
            return
        self.storage_critical_reason = None
        self._pending_storage_events = None
        if not await self._persist_events(events):
            return
        by_market: dict[str, list[Any]] = {}
        for event in events:
            market_id = getattr(event, "market_id", None)
            if not isinstance(market_id, str) or market_id not in self.markets:
                raise EngineInvariantError("pending storage event market is invalid")
            by_market.setdefault(market_id, []).append(event)
        for market_id, grouped in by_market.items():
            self._adopt_events(self.markets[market_id], tuple(grouped))

    def _adopt_events(self, market: MarketDefinition, events: tuple[Any, ...]) -> None:
        market_positions = self.positions[market.market_id]
        for event in events:
            if isinstance(event, StrategyEvent):
                market_positions[event.lane] = LanePosition(
                    market.market_id, market.end_ts, event.lane,
                    event.config_hash, event,
                )
            else:
                previous = market_positions.get(event.lane)
                if previous is None:
                    raise EngineInvariantError("reverse has no in-memory entry")
                market_positions[event.lane] = LanePosition(
                    market.market_id, market.end_ts, event.lane,
                    event.config_hash, previous.entry, event,
                )

    async def process_market_event(self, event: Any) -> None:
        if isinstance(event, MarketInvalidation):
            self._market_batch_journal.clear()
            for token_id in event.token_ids:
                book = self.books.get(token_id)
                if book is not None:
                    book.invalidate()
            return
        if not isinstance(event, (MarketSnapshot, MarketDelta)):
            return
        market_id = self._token_market.get(event.token_id)
        batch_key: tuple[str, int, int] | None = None
        if isinstance(event, MarketDelta):
            batch_key = (market_id or event.token_id, event.event_ts_ms, event.batch_id)
            if event.batch_index == 0:
                journaled = self._journal_market_event(event)
                self._market_batch_journal[batch_key] = journaled
            elif batch_key not in self._market_batch_journal:
                return
            else:
                journaled = self._market_batch_journal[batch_key]
        else:
            journaled = self._journal_market_event(event)
        if market_id is None or market_id in self._inactive:
            if batch_key is not None and event.batch_index + 1 == event.batch_size:
                self._market_batch_journal.pop(batch_key, None)
            return
        market = self.markets[market_id]
        book = self.books[event.token_id]
        try:
            if isinstance(event, MarketSnapshot):
                book.apply_snapshot(event.bids, event.asks, event.event_ts_ms, event.sequence)
                accepted = True
            else:
                accepted = book.apply_delta(
                    event.side, event.price, event.shares, event.event_ts_ms, event.sequence
                )
        except (InvalidBook, ValueError):
            if batch_key is not None and event.batch_index + 1 == event.batch_size:
                self._market_batch_journal.pop(batch_key, None)
            return
        if not accepted:
            return
        if isinstance(event, MarketDelta) and event.batch_index + 1 < event.batch_size:
            return
        if batch_key is not None:
            self._market_batch_journal.pop(batch_key, None)
        now_s = self._now_s()
        if not self._strategy_writable(journaled):
            return
        strategy = self.strategies[market_id]
        reverse_events = self._reverse_events(market, event.event_ts_ms, now_s)
        entry_events = strategy.on_book_event(
            market, self._book_mapping(market), self.resolver, event.event_ts_ms, now_s
        )
        events = reverse_events + entry_events
        if await self._persist_events(events):
            self._adopt_events(market, events)

    async def process_resolver_event(self, event: Any) -> None:
        if not isinstance(event, ResolverObservation):
            return
        journaled = self._journal_resolver_event(event)
        accepted = self.resolver.accept(
            event.symbol, event.value, event.observation_ts_ms, event.receive_ts_ms
        )
        if not accepted or not self._strategy_writable(journaled):
            return
        now_s = self._now_s()
        events_by_market: list[tuple[MarketDefinition, tuple[Any, ...]]] = []
        all_events: list[Any] = []
        for market in self.markets.values():
            if market.symbol != event.symbol or market.market_id in self._inactive:
                continue
            reverses = self._reverse_events(market, event.observation_ts_ms, now_s)
            if reverses:
                events_by_market.append((market, reverses))
                all_events.extend(reverses)
        materialized = tuple(all_events)
        if await self._persist_events(materialized):
            for market, events in events_by_market:
                self._adopt_events(market, events)

    async def reconcile_settlements(self) -> None:
        if self.settlement_fetcher is None or self.storage_critical_reason is not None:
            return
        now_s = self._now_s()
        for market in tuple(self.markets.values()):
            positions = self.positions[market.market_id]
            if market.market_id in self._settled or market.end_ts > now_s or not positions:
                continue
            payload = await self.settlement_fetcher(market)
            if isinstance(payload, OfficialSettlement):
                settlement = payload
            elif isinstance(payload, Mapping):
                settlement = parse_official_settlement(payload, market.slug)
            else:
                settlement = None
            if settlement is None:
                continue
            results = tuple(settle_lane(position, settlement) for position in positions.values())
            try:
                self.storage.record_settlement(market.market_id, settlement, results)
            except Exception as exc:
                self.storage_critical_reason = type(exc).__name__
                return
            self._settled.add(market.market_id)
            self._inactive.add(market.market_id)
            self._subscriptions_changed.set()

    async def discover_markets(self) -> None:
        definitions = await self.gamma.discover_current_and_next(self.settings.symbols, self._now_s())
        now_s = self._now_s()
        self._register_markets(market for market in definitions if market.end_ts > now_s)
        self._retire_expired_empty_markets(now_s)

    async def _event_loop(self) -> None:
        while True:
            event = await self.queue.get()
            try:
                try:
                    if isinstance(event, ResolverObservation):
                        await self.process_resolver_event(event)
                    else:
                        await self.process_market_event(event)
                except Exception as exc:
                    self.processing_critical_reason = type(exc).__name__
            finally:
                self.queue.task_done()

    async def _discovery_loop(self) -> None:
        while True:
            try:
                await self.discover_markets()
            except Exception as exc:
                self.discovery_critical_reason = type(exc).__name__
            else:
                self.discovery_critical_reason = None
            await self._sleep(self.discovery_interval)

    async def _settlement_loop(self) -> None:
        while True:
            try:
                await self.reconcile_settlements()
            except Exception as exc:
                self.settlement_critical_reason = type(exc).__name__
            else:
                self.settlement_critical_reason = None
            await self._sleep(self.settlement_interval)

    async def _heartbeat_loop(self) -> None:
        while True:
            now = datetime.fromtimestamp(self._now_s(), UTC)
            try:
                self.journal.rotate_if_needed(now)
                self.journal.writable()
            except JournalError:
                pass
            await self._retry_pending_storage()
            self._retire_expired_empty_markets(self._now_s())
            await self._sleep(self.heartbeat_interval)

    async def _market_feed_supervisor(self) -> None:
        assert self.market_ws is not None
        while True:
            subscribed = self.token_ids()
            self._subscriptions_changed.clear()
            feed = asyncio.create_task(
                self.market_ws.run(self.token_ids, self.queue), name="market-feed-connection"
            )
            changed = asyncio.create_task(
                self._subscriptions_changed.wait(), name="market-subscription-change"
            )
            try:
                done, _ = await asyncio.wait((feed, changed), return_when=asyncio.FIRST_COMPLETED)
                if feed in done:
                    feed.result()
                    raise EngineInvariantError("market feed stopped unexpectedly")
                feed.cancel()
                await asyncio.gather(feed, return_exceptions=True)
                await _enqueue(self.queue, MarketInvalidation(subscribed))
            finally:
                for task in (feed, changed):
                    if not task.done():
                        task.cancel()
                await asyncio.gather(feed, changed, return_exceptions=True)

    async def run(self) -> None:
        if not self._initialized:
            raise EngineInvariantError("initialize must complete before run")
        if self._running:
            raise EngineInvariantError("engine is already running")
        self._running = True
        self._stop_event.clear()
        tasks = [
            asyncio.create_task(self._event_loop(), name="event-processor"),
            asyncio.create_task(self._discovery_loop(), name="market-discovery"),
            asyncio.create_task(self._settlement_loop(), name="settlement-poller"),
            asyncio.create_task(self._heartbeat_loop(), name="engine-heartbeat"),
        ]
        if self.market_ws is not None:
            tasks.append(asyncio.create_task(self._market_feed_supervisor(), name="market-feed"))
        if self.rtds is not None:
            tasks.append(asyncio.create_task(self.rtds.run(self.queue), name="resolver-feed"))
        self._tasks = tasks
        stopped = asyncio.create_task(self._stop_event.wait(), name="engine-stop")
        try:
            done, _ = await asyncio.wait((*tasks, stopped), return_when=asyncio.FIRST_COMPLETED)
            if stopped not in done:
                for task in done:
                    task.result()
        finally:
            stopped.cancel()
            for task in tasks:
                task.cancel()
            await asyncio.gather(stopped, *tasks, return_exceptions=True)
            self._tasks = []
            self._running = False

    def stop(self) -> None:
        self._stop_event.set()


__all__ = ["EngineInvariantError", "PaperEngine"]
