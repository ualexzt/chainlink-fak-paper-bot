from __future__ import annotations

import hashlib
import random
from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP
from typing import Mapping, Sequence

from .books import InvalidBook, OrderBook
from .fak import simulate_buy_fak
from .gamma import MarketDefinition
from .resolver import ResolverState
from .strategy import Confirmation, LaneKey, PositionPolicy, StrategyEvent

MODEL_VERSION = "twap-segmented-block-bootstrap-v2"
HORIZONS = (90, 60, 30)
ENTRY_FLOOR = Decimal("0.85")
ENTRY_CEILING = Decimal("0.90")
EDGE_MARGIN = Decimal("0.03")
SIMULATIONS = 10_000
MIN_HISTORY_POINTS = 30
BLOCK_SIZE = 5
PROBABILITY_QUANTUM = Decimal("0.0001")

_CONFIRMATION_BY_HORIZON = {
    90: Confirmation.MC_BOOTSTRAP_90_V2,
    60: Confirmation.MC_BOOTSTRAP_60_V2,
    30: Confirmation.MC_BOOTSTRAP_30_V2,
}


@dataclass(frozen=True)
class MonteCarloForecastEvent:
    model_version: str
    config_hash: str
    market_id: str
    mkt_ts: int
    horizon_seconds: int
    seconds_to_close: int
    event_ts_ms: int
    observation_ts_ms: int | None
    side: str | None
    token_id: str | None
    book_generation: int | None
    best_ask: Decimal | None
    start: Decimal | None
    current: Decimal | None
    distance_bps: Decimal | None
    probability: Decimal | None
    break_even_probability: Decimal | None
    edge: Decimal | None
    history_points: int
    simulations: int
    sign_flips: int | None
    mean_abs_step_bps: Decimal | None
    decision: str
    reason: str

    def __post_init__(self) -> None:
        if self.model_version != MODEL_VERSION:
            raise ValueError("unsupported Monte Carlo model version")
        if (
            not isinstance(self.config_hash, str) or len(self.config_hash) != 64
            or any(character not in "0123456789abcdef" for character in self.config_hash)
        ):
            raise ValueError("config_hash must be a lowercase SHA-256 digest")
        if not self.market_id or self.mkt_ts <= 0 or self.horizon_seconds not in HORIZONS:
            raise ValueError("invalid Monte Carlo market identity")
        integer_fields = (
            self.seconds_to_close, self.event_ts_ms, self.history_points, self.simulations
        )
        if any(isinstance(value, bool) or not isinstance(value, int) or value < 0 for value in integer_fields):
            raise ValueError("invalid Monte Carlo integer field")
        for value in (self.observation_ts_ms, self.book_generation, self.sign_flips):
            if value is not None and (
                isinstance(value, bool) or not isinstance(value, int) or value < 0
            ):
                raise ValueError("invalid optional Monte Carlo integer field")
        if self.side not in {None, "UP", "DOWN"} or not self.reason:
            raise ValueError("invalid Monte Carlo classification")
        if self.decision not in {"ENTER", "REJECT", "MISSED"}:
            raise ValueError("invalid Monte Carlo decision")
        decimals = (
            self.best_ask, self.start, self.current, self.distance_bps, self.probability,
            self.break_even_probability, self.edge, self.mean_abs_step_bps,
        )
        if any(value is not None and (not isinstance(value, Decimal) or not value.is_finite()) for value in decimals):
            raise ValueError("Monte Carlo decimal fields must be finite Decimals")
        if self.best_ask is not None and not Decimal("0") < self.best_ask < Decimal("1"):
            raise ValueError("Monte Carlo ask must be in (0,1)")
        if self.start is not None and self.start <= 0 or self.current is not None and self.current <= 0:
            raise ValueError("Monte Carlo resolver prices must be positive")
        if any(value is not None and not Decimal("0") <= value <= Decimal("1")
               for value in (self.probability, self.break_even_probability)):
            raise ValueError("Monte Carlo probabilities must be in [0,1]")
        if self.mean_abs_step_bps is not None and self.mean_abs_step_bps < 0:
            raise ValueError("Monte Carlo absolute movement must be nonnegative")
        if self.decision == "ENTER" and (
            self.reason != "eligible" or self.side is None or not self.token_id
            or self.book_generation is None or self.book_generation <= 0
            or self.best_ask is None or self.probability is None
            or self.break_even_probability is None or self.edge is None
            or self.simulations != SIMULATIONS
        ):
            raise ValueError("eligible Monte Carlo entry evidence is incomplete")


def _book_for_side(
    books: Mapping[str, OrderBook], market: MarketDefinition, side: str
) -> tuple[str, OrderBook]:
    token_id = market.up_token_id if side == "UP" else market.down_token_id
    book = books.get(token_id)
    if not isinstance(book, OrderBook):
        raise InvalidBook("missing side book")
    return token_id, book


def _bootstrap_probability(
    history: Sequence[tuple[int, Decimal]],
    *,
    start: Decimal,
    current: Decimal,
    side: str,
    seconds_to_close: int,
    seed_material: str,
) -> tuple[Decimal, int, Decimal, int]:
    if len(history) < MIN_HISTORY_POINTS:
        raise ValueError("history_insufficient")
    intervals = [history[index][0] - history[index - 1][0] for index in range(1, len(history))]
    if any(interval <= 0 for interval in intervals):
        raise ValueError("history_not_monotonic")
    # RTDS normally updates every 1-2 seconds. A wider interval is a feed gap,
    # not a price return; a data set dominated by gaps must not redefine its
    # own expected cadence and accidentally admit them.
    continuity_limit_ms = 3_000
    segments: list[list[Decimal]] = [[]]
    valid_intervals: list[int] = []
    for index, interval in enumerate(intervals, start=1):
        if interval > continuity_limit_ms:
            segments.append([])
            continue
        valid_intervals.append(interval)
        segments[-1].append(history[index][1] / history[index - 1][1] - Decimal("1"))
    returns = [change for segment in segments for change in segment]
    if len(returns) < MIN_HISTORY_POINTS - 1:
        raise ValueError("continuous_history_insufficient")
    ordered_intervals = sorted(valid_intervals)
    interval_ms = ordered_intervals[len(ordered_intervals) // 2]
    steps = max(1, (seconds_to_close * 1000 + interval_ms - 1) // interval_ms)

    blocks = tuple(
        tuple(segment[index:index + BLOCK_SIZE])
        for segment in segments
        for index in range(len(segment) - BLOCK_SIZE + 1)
    )
    if not blocks:
        raise ValueError("continuous_block_history_insufficient")
    recent = returns[-30:]
    signs = [1 if value > 0 else -1 if value < 0 else 0 for value in recent]
    nonzero = [value for value in signs if value]
    sign_flips = sum(left != right for left, right in zip(nonzero, nonzero[1:]))
    mean_abs_step_bps = (
        sum((abs(value) for value in recent), Decimal("0"))
        / Decimal(len(recent))
        * Decimal("10000")
    ).quantize(PROBABILITY_QUANTUM, rounding=ROUND_HALF_UP)

    seed = int.from_bytes(hashlib.sha256(seed_material.encode("utf-8")).digest()[:8], "big")
    rng = random.Random(seed)
    wins = 0
    for _ in range(SIMULATIONS):
        value = current
        remaining = steps
        while remaining:
            block = blocks[rng.randrange(len(blocks))]
            for change in block[:remaining]:
                value *= Decimal("1") + change
            remaining -= min(remaining, len(block))
        if (side == "UP" and value >= start) or (side == "DOWN" and value < start):
            wins += 1
    probability = (Decimal(wins) / Decimal(SIMULATIONS)).quantize(
        PROBABILITY_QUANTUM, rounding=ROUND_HALF_UP
    )
    return probability, sign_flips, mean_abs_step_bps, len(returns) + 1


class MonteCarloShadowState:
    """One immutable HOLD-only forecast per 90/60/30-second window."""

    def __init__(self, *, paper_notional_usd: Decimal, config_hash: str) -> None:
        if (
            not isinstance(paper_notional_usd, Decimal)
            or not paper_notional_usd.is_finite()
            or paper_notional_usd <= 0
        ):
            raise ValueError("paper_notional_usd must be positive")
        if (
            not isinstance(config_hash, str) or len(config_hash) != 64
            or any(character not in "0123456789abcdef" for character in config_hash)
        ):
            raise ValueError("config_hash must be a lowercase SHA-256 digest")
        self.paper_notional_usd = paper_notional_usd
        self.config_hash = config_hash
        self._attempted: set[int] = set()
        self._market_key: tuple[str, int] | None = None

    @property
    def has_observations(self) -> bool:
        return bool(self._attempted)

    def restore_attempts(self, horizons: Sequence[int]) -> None:
        if self._attempted or self._market_key is not None:
            raise RuntimeError("Monte Carlo attempts can only be restored into fresh state")
        values = tuple(horizons)
        if len(set(values)) != len(values) or any(value not in HORIZONS for value in values):
            raise ValueError("invalid restored Monte Carlo horizon")
        self._attempted.update(values)

    def on_event(
        self,
        market: MarketDefinition,
        books: Mapping[str, OrderBook],
        resolver: ResolverState,
        event_ts_ms: int,
        now_ts: int,
        now_ms: int | None = None,
    ) -> tuple[MonteCarloForecastEvent | StrategyEvent, ...]:
        self._bind_market(market)
        if (
            isinstance(event_ts_ms, bool) or not isinstance(event_ts_ms, int) or event_ts_ms < 0
            or isinstance(now_ts, bool) or not isinstance(now_ts, int) or now_ts < 0
        ):
            raise ValueError("event timestamps must be nonnegative integers")
        if now_ms is None:
            now_ms = max(now_ts * 1000, event_ts_ms)
        if isinstance(now_ms, bool) or not isinstance(now_ms, int) or now_ms < 0:
            raise ValueError("now_ms must be a nonnegative integer")
        seconds_to_close = market.end_ts - now_ts
        if seconds_to_close <= 0 or seconds_to_close > HORIZONS[0]:
            return ()

        output: list[MonteCarloForecastEvent | StrategyEvent] = []
        active_horizon: int | None = None
        for index, horizon in enumerate(HORIZONS):
            if horizon in self._attempted:
                continue
            lower = HORIZONS[index + 1] if index + 1 < len(HORIZONS) else 0
            if seconds_to_close <= lower:
                output.append(self._missed(market, horizon, seconds_to_close, event_ts_ms))
                self._attempted.add(horizon)
            elif seconds_to_close <= horizon:
                active_horizon = horizon
                break
        if active_horizon is None:
            return tuple(output)

        forecast, entry = self._evaluate(
            market, books, resolver, active_horizon, seconds_to_close, event_ts_ms, now_ms
        )
        self._attempted.add(active_horizon)
        output.append(forecast)
        if entry is not None:
            output.append(entry)
        return tuple(output)

    def _evaluate(
        self,
        market: MarketDefinition,
        books: Mapping[str, OrderBook],
        resolver: ResolverState,
        horizon: int,
        seconds_to_close: int,
        event_ts_ms: int,
        now_ms: int,
    ) -> tuple[MonteCarloForecastEvent, StrategyEvent | None]:
        common = dict(
            model_version=MODEL_VERSION,
            config_hash=self.config_hash,
            market_id=market.market_id,
            mkt_ts=market.mkt_ts,
            horizon_seconds=horizon,
            seconds_to_close=seconds_to_close,
            event_ts_ms=event_ts_ms,
        )
        try:
            view = resolver.view(market.symbol, market.mkt_ts, now_ms)
            history = resolver.history(market.symbol)
        except (AttributeError, KeyError, ValueError):
            return MonteCarloForecastEvent(
                **common, observation_ts_ms=None, side=None, token_id=None,
                book_generation=None, best_ask=None, start=None, current=None,
                distance_bps=None, probability=None, break_even_probability=None,
                edge=None, history_points=0, simulations=0, sign_flips=None,
                mean_abs_step_bps=None, decision="REJECT", reason="resolver_unavailable",
            ), None

        base = dict(
            **common,
            observation_ts_ms=view.observation_ts_ms,
            start=view.start,
            current=view.current,
            distance_bps=view.distance_bps,
            history_points=len(history),
        )
        if not view.fresh:
            return self._rejected(base, "resolver_stale"), None
        if view.start is None or view.current is None or view.leader not in {"UP", "DOWN"}:
            return self._rejected(base, "leader_unavailable"), None
        side = view.leader
        try:
            probability, sign_flips, mean_abs_step_bps, effective_history_points = _bootstrap_probability(
                history,
                start=view.start,
                current=view.current,
                side=side,
                seconds_to_close=seconds_to_close,
                seed_material=(
                    f"{MODEL_VERSION}|{self.config_hash}|{market.market_id}|{horizon}|"
                    f"{view.observation_ts_ms}"
                ),
            )
        except ValueError as exc:
            return self._rejected(base, str(exc), side=side), None

        forecast_base = {
            **base,
            "side": side,
            "probability": probability,
            "history_points": effective_history_points,
            "simulations": SIMULATIONS,
            "sign_flips": sign_flips,
            "mean_abs_step_bps": mean_abs_step_bps,
        }
        try:
            token_id, book = _book_for_side(books, market, side)
            asks = book.executable_asks()
        except (InvalidBook, KeyError):
            return self._rejected(forecast_base, "book_invalid"), None
        if not asks:
            return self._rejected(forecast_base, "book_empty"), None
        best_ask = asks[0].price
        execution_base = dict(
            **forecast_base,
            token_id=token_id,
            book_generation=book.generation,
            best_ask=best_ask,
        )
        if not ENTRY_FLOOR <= best_ask <= ENTRY_CEILING:
            return self._rejected(execution_base, "ask_outside_entry_band"), None

        fak = simulate_buy_fak(
            asks,
            self.paper_notional_usd,
            best_ask,
            market.tick_size,
            market.min_order_shares,
            market.fee_schedule,
        )
        if fak.filled_shares <= 0:
            return self._rejected(execution_base, "no_fill"), None
        break_even = ((fak.quote_amount + fak.fee) / fak.filled_shares).quantize(
            PROBABILITY_QUANTUM, rounding=ROUND_HALF_UP
        )
        edge = (probability - break_even).quantize(PROBABILITY_QUANTUM, rounding=ROUND_HALF_UP)
        final = dict(
            **execution_base,
            break_even_probability=break_even,
            edge=edge,
        )
        if edge < EDGE_MARGIN:
            return self._rejected(final, "edge_below_margin"), None

        lane = LaneKey(ENTRY_FLOOR, _CONFIRMATION_BY_HORIZON[horizon], PositionPolicy.HOLD)
        forecast = MonteCarloForecastEvent(**final, decision="ENTER", reason="eligible")
        entry = StrategyEvent(
            lane=lane,
            kind="entry_attempt",
            market_id=market.market_id,
            mkt_ts=market.mkt_ts,
            token_id=token_id,
            side=side,
            event_ts_ms=event_ts_ms,
            book_generation=book.generation,
            config_hash=self.config_hash,
            fak=fak,
        )
        return forecast, entry

    @staticmethod
    def _rejected(base: Mapping[str, object], reason: str, **overrides: object) -> MonteCarloForecastEvent:
        values = {
            "observation_ts_ms": None,
            "side": None,
            "token_id": None,
            "book_generation": None,
            "best_ask": None,
            "start": None,
            "current": None,
            "distance_bps": None,
            "probability": None,
            "break_even_probability": None,
            "edge": None,
            "history_points": 0,
            "simulations": 0,
            "sign_flips": None,
            "mean_abs_step_bps": None,
        }
        values.update(base)
        values.update(overrides)
        return MonteCarloForecastEvent(**values, decision="REJECT", reason=reason)

    def _missed(
        self, market: MarketDefinition, horizon: int, seconds_to_close: int, event_ts_ms: int
    ) -> MonteCarloForecastEvent:
        return MonteCarloForecastEvent(
            model_version=MODEL_VERSION,
            config_hash=self.config_hash,
            market_id=market.market_id,
            mkt_ts=market.mkt_ts,
            horizon_seconds=horizon,
            seconds_to_close=seconds_to_close,
            event_ts_ms=event_ts_ms,
            observation_ts_ms=None,
            side=None,
            token_id=None,
            book_generation=None,
            best_ask=None,
            start=None,
            current=None,
            distance_bps=None,
            probability=None,
            break_even_probability=None,
            edge=None,
            history_points=0,
            simulations=0,
            sign_flips=None,
            mean_abs_step_bps=None,
            decision="MISSED",
            reason="window_missed",
        )

    def _bind_market(self, market: MarketDefinition) -> None:
        key = (market.market_id, market.mkt_ts)
        if self._market_key is None:
            self._market_key = key
        elif self._market_key != key:
            raise ValueError("MonteCarloShadowState cannot be reused across markets")


__all__ = [
    "BLOCK_SIZE",
    "EDGE_MARGIN",
    "ENTRY_CEILING",
    "ENTRY_FLOOR",
    "HORIZONS",
    "MIN_HISTORY_POINTS",
    "MODEL_VERSION",
    "MonteCarloForecastEvent",
    "MonteCarloShadowState",
    "SIMULATIONS",
]
