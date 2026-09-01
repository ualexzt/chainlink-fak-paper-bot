from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from decimal import Decimal
from typing import Iterable

_FRESH_WINDOW_MS = 10_000
_MOMENTUM_WINDOW_MS = 5_000
_DEFAULT_HISTORY_SIZE = 120
_BPS = Decimal("10000")


@dataclass(frozen=True)
class ResolverView:
    symbol: str
    current: Decimal | None
    start: Decimal | None
    observation_ts_ms: int | None
    age_ms: int | None
    fresh: bool
    distance: Decimal | None
    distance_bps: Decimal | None
    leader: str | None
    momentum_5s_bps: Decimal | None


@dataclass
class _SymbolState:
    history: deque[tuple[int, Decimal]]
    start_cache: dict[int, Decimal]
    latest_value: Decimal | None = None
    latest_ts_ms: int | None = None


class ResolverState:
    def __init__(self, symbols: Iterable[str] | None = None, history_size: int = _DEFAULT_HISTORY_SIZE):
        if isinstance(history_size, bool) or not isinstance(history_size, int) or history_size < 2:
            raise ValueError("history_size must be an integer >= 2")
        configured = ("btc", "eth", "sol") if symbols is None else tuple(symbols)
        if not configured:
            raise ValueError("at least one symbol must be configured")
        seen: set[str] = set()
        for symbol in configured:
            self._validate_symbol_config(symbol)
            if symbol in seen:
                raise ValueError("duplicate configured symbol")
            seen.add(symbol)
        self._symbols = configured
        self._symbol_set = seen
        self._history_size = history_size
        self._states = {
            symbol: _SymbolState(
                history=deque(maxlen=history_size),
                start_cache={},
            )
            for symbol in configured
        }

    @staticmethod
    def _validate_symbol_config(symbol: object) -> None:
        if not isinstance(symbol, str) or not symbol or symbol != symbol.lower():
            raise ValueError("configured symbols must be strict lowercase strings")

    @staticmethod
    def _validate_symbol(symbol: object) -> str:
        if not isinstance(symbol, str) or not symbol or symbol != symbol.lower():
            raise ValueError("symbol must be a strict lowercase string")
        return symbol

    @staticmethod
    def _validate_timestamp(value: object, name: str) -> int:
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise ValueError(f"{name} must be a positive integer")
        return value

    @staticmethod
    def _validate_value(value: object) -> Decimal:
        if not isinstance(value, Decimal) or not value.is_finite() or value <= 0:
            raise ValueError("value must be a finite positive Decimal")
        return value

    def accept(self, symbol: str, value: Decimal, observation_ts_ms: int, receive_ts_ms: int) -> bool:
        try:
            symbol = self._validate_symbol(symbol)
            if symbol not in self._symbol_set:
                return False
            value = self._validate_value(value)
            observation_ts_ms = self._validate_timestamp(observation_ts_ms, "observation_ts_ms")
            receive_ts_ms = self._validate_timestamp(receive_ts_ms, "receive_ts_ms")
        except ValueError:
            return False

        state = self._states[symbol]
        if observation_ts_ms > receive_ts_ms:
            return False
        if receive_ts_ms - observation_ts_ms > _FRESH_WINDOW_MS:
            return False
        if state.latest_ts_ms is not None and observation_ts_ms <= state.latest_ts_ms:
            return False

        state.history.append((observation_ts_ms, value))
        state.latest_ts_ms = observation_ts_ms
        state.latest_value = value
        return True

    def view(self, symbol: str, mkt_ts: int, now_ms: int) -> ResolverView:
        symbol = self._validate_symbol(symbol)
        if symbol not in self._symbol_set:
            raise ValueError("unknown symbol")
        mkt_ts = self._validate_timestamp(mkt_ts, "mkt_ts")
        now_ms = self._validate_timestamp(now_ms, "now_ms")

        state = self._states[symbol]
        current = state.latest_value
        obs_ts_ms = state.latest_ts_ms
        age_ms = None if obs_ts_ms is None else now_ms - obs_ts_ms
        fresh = obs_ts_ms is not None and 0 <= age_ms <= _FRESH_WINDOW_MS

        window_start = mkt_ts * 1000
        window_end = window_start + _FRESH_WINDOW_MS
        start = self._start_for_market(state, mkt_ts, window_start, window_end)

        distance = None
        distance_bps = None
        leader = None
        if current is not None and start is not None:
            distance = current - start
            distance_bps = distance / start * _BPS
            leader = "UP" if distance > 0 else "DOWN" if distance < 0 else "TIE"

        momentum_5s_bps = None
        if current is not None and obs_ts_ms is not None:
            cutoff = obs_ts_ms - _MOMENTUM_WINDOW_MS
            for prior_ts, prior_value in reversed(state.history):
                if prior_ts == obs_ts_ms:
                    continue
                if prior_ts <= cutoff:
                    momentum_5s_bps = (current / prior_value - Decimal("1")) * _BPS
                    break

        return ResolverView(
            symbol=symbol,
            current=current,
            start=start,
            observation_ts_ms=obs_ts_ms,
            age_ms=age_ms,
            fresh=fresh,
            distance=distance,
            distance_bps=distance_bps,
            leader=leader,
            momentum_5s_bps=momentum_5s_bps,
        )

    def _start_for_market(
        self,
        state: _SymbolState,
        mkt_ts: int,
        window_start: int,
        window_end: int,
    ) -> Decimal | None:
        cached = state.start_cache.get(mkt_ts)
        if cached is not None:
            return cached
        for candidate_ts, candidate_value in state.history:
            if window_start <= candidate_ts <= window_end:
                self._cache_start(state, mkt_ts, candidate_value)
                return candidate_value
        return None

    def _cache_start(self, state: _SymbolState, mkt_ts: int, start: Decimal) -> None:
        if mkt_ts in state.start_cache:
            return
        if len(state.start_cache) >= self._history_size:
            oldest_mkt_ts = min(state.start_cache)
            if mkt_ts <= oldest_mkt_ts:
                return
            state.start_cache.pop(oldest_mkt_ts)
        state.start_cache[mkt_ts] = start

    def stale_symbols(self, now_ms: int) -> tuple[str, ...]:
        now_ms = self._validate_timestamp(now_ms, "now_ms")
        stale: list[str] = []
        for symbol in self._symbols:
            state = self._states[symbol]
            if state.latest_ts_ms is None:
                stale.append(symbol)
                continue
            age_ms = now_ms - state.latest_ts_ms
            if age_ms < 0 or age_ms > _FRESH_WINDOW_MS:
                stale.append(symbol)
        return tuple(stale)


__all__ = ["ResolverState", "ResolverView"]
