from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Mapping


ZERO = Decimal("0")
ONE = Decimal("1")
SIGNAL_AGE = 30
ENTRY_AGE = 120
LAST_REPAIR_TRIGGER_AGE = 240
SIGNAL_ASK = Decimal("0.60")
ENTRY_ASK_FLOOR = Decimal("0.88")
EARLY_DRAWDOWN = Decimal("0.30")
EARLY_DRAWDOWN_RUN = 10
OPPOSITE_WARNING_ASK = Decimal("0.70")
OPPOSITE_WARNING_RUN = 10
REPAIR_DRAWDOWN = Decimal("0.20")
REPAIR_RUN = 3
PAPER_NOTIONAL = Decimal("1")
VERSION = 1


def _price(value: Any, name: str) -> Decimal:
    if not isinstance(value, Decimal) or not value.is_finite() or not ZERO < value < ONE:
        raise ValueError(f"{name} must be a finite Decimal in (0,1)")
    return value


@dataclass(frozen=True)
class QualityBook:
    bid: Decimal
    ask: Decimal

    def __post_init__(self) -> None:
        _price(self.bid, "bid")
        _price(self.ask, "ask")
        if self.bid >= self.ask:
            raise ValueError("quality book must not be locked or crossed")


class QualityShadowState:
    """Causal one-second shadow state; it never submits or simulates venue orders."""

    def __init__(self, market_id: str, mkt_ts: int, symbol: str) -> None:
        if not market_id or not symbol:
            raise ValueError("quality market identity must be nonempty")
        if isinstance(mkt_ts, bool) or not isinstance(mkt_ts, int) or mkt_ts < 0:
            raise ValueError("quality market timestamp must be nonnegative")
        self.market_id, self.mkt_ts, self.symbol = market_id, mkt_ts, symbol
        self.stage = "WATCHING"
        self.reason: str | None = None
        self.last_age: int | None = None
        self.last_recorded_age: int | None = None
        self.selected_side: str | None = None
        self.p30: Decimal | None = None
        self.filter_a_run = 0
        self.filter_b_run = 0
        self.filter_a = False
        self.filter_b = False
        self.entry_ask: Decimal | None = None
        self.shares: Decimal | None = None
        self.repair_run = 0
        self.switch_due_age: int | None = None
        self.switch_age: int | None = None
        self.switch_bid: Decimal | None = None
        self.opposite_ask: Decimal | None = None
        self.extra_capital: Decimal | None = None
        self.winner: str | None = None
        self.pnl: Decimal | None = None

    @property
    def observed(self) -> bool:
        return self.last_age is not None

    @property
    def terminal_before_settlement(self) -> bool:
        return self.stage in {"NO_SIGNAL", "REJECTED", "MISSED"}

    @staticmethod
    def _opposite(side: str) -> str:
        return "DOWN" if side == "UP" else "UP"

    def _event(self, kind: str, age: int, **detail: Any) -> dict[str, Any]:
        return {
            "event_type": f"quality_{kind}", "version": VERSION,
            "market_id": self.market_id, "mkt_ts": self.mkt_ts,
            "symbol": self.symbol, "age": age, **detail,
        }

    def sample(self, age: int, books: Mapping[str, QualityBook]) -> tuple[dict[str, Any], ...]:
        if isinstance(age, bool) or not isinstance(age, int) or not 0 <= age < 300:
            raise ValueError("quality sample age must be in [0,300)")
        if set(books) != {"UP", "DOWN"} or any(not isinstance(book, QualityBook) for book in books.values()):
            raise ValueError("quality sample requires exact UP and DOWN books")
        if self.last_age is not None and age <= self.last_age:
            return ()
        consecutive = self.last_age is not None and age == self.last_age + 1
        events: list[dict[str, Any]] = []

        if self.stage == "WATCHING" and age > SIGNAL_AGE:
            self.stage, self.reason = "MISSED", "age30_not_sampled"
            events.append(self._event("missed", age, reason=self.reason))
        elif self.stage == "WATCHING" and age == SIGNAL_AGE:
            sides = [side for side in ("UP", "DOWN") if books[side].ask >= SIGNAL_ASK]
            if len(sides) != 1:
                self.stage, self.reason = "NO_SIGNAL", "ambiguous" if len(sides) == 2 else "below_0.60"
                events.append(self._event("no_signal", age, reason=self.reason,
                                          up_ask=books["UP"].ask, down_ask=books["DOWN"].ask))
            else:
                self.stage, self.selected_side = "CANDIDATE", sides[0]
                self.p30 = books[sides[0]].ask
                events.append(self._event("candidate", age, side=sides[0], ask=self.p30))

        if self.stage == "CANDIDATE":
            assert self.selected_side is not None and self.p30 is not None
            chosen, opposite = books[self.selected_side], books[self._opposite(self.selected_side)]
            if age > ENTRY_AGE:
                self.stage, self.reason = "MISSED", "age120_not_sampled"
                events.append(self._event("missed", age, side=self.selected_side, reason=self.reason))
            elif SIGNAL_AGE < age <= ENTRY_AGE:
                if not consecutive:
                    self.filter_a_run = self.filter_b_run = 0
                if age <= 90 and not self.filter_a:
                    self.filter_a_run = self.filter_a_run + 1 if self.p30 - chosen.ask >= EARLY_DRAWDOWN else 0
                    self.filter_a = self.filter_a_run >= EARLY_DRAWDOWN_RUN
                if not self.filter_b:
                    self.filter_b_run = self.filter_b_run + 1 if opposite.ask >= OPPOSITE_WARNING_ASK else 0
                    self.filter_b = self.filter_b_run >= OPPOSITE_WARNING_RUN
                if age == ENTRY_AGE:
                    if self.filter_a or self.filter_b:
                        self.stage, self.reason = "REJECTED", "warning_filter"
                        events.append(self._event(
                            "rejected", age, side=self.selected_side, reason=self.reason,
                            filter_a=self.filter_a, filter_b=self.filter_b, ask=chosen.ask,
                        ))
                    elif chosen.ask < ENTRY_ASK_FLOOR:
                        self.stage, self.reason = "REJECTED", "entry_ask_below_0.88"
                        events.append(self._event(
                            "rejected", age, side=self.selected_side, reason=self.reason, ask=chosen.ask,
                        ))
                    else:
                        self.stage, self.entry_ask = "ENTERED", chosen.ask
                        self.shares = PAPER_NOTIONAL / chosen.ask
                        events.append(self._event(
                            "entry", age, side=self.selected_side, ask=self.entry_ask,
                            shares=self.shares, notional=PAPER_NOTIONAL,
                        ))

        elif self.stage == "ENTERED":
            assert self.selected_side is not None and self.entry_ask is not None and self.shares is not None
            chosen, opposite = books[self.selected_side], books[self._opposite(self.selected_side)]
            if self.switch_due_age is not None:
                if age == self.switch_due_age:
                    self.stage, self.switch_age = "SWITCHED", age
                    self.switch_bid, self.opposite_ask = chosen.bid, opposite.ask
                    self.extra_capital = max(ZERO, (opposite.ask - chosen.bid) * self.shares)
                    events.append(self._event(
                        "switch", age, old_side=self.selected_side,
                        new_side=self._opposite(self.selected_side), shares=self.shares,
                        sell_bid=self.switch_bid, buy_ask=self.opposite_ask,
                        extra_capital=self.extra_capital,
                    ))
                else:
                    self.switch_due_age = None
                    self.repair_run = 0
            if self.stage == "ENTERED" and age <= LAST_REPAIR_TRIGGER_AGE:
                if not consecutive:
                    self.repair_run = 0
                self.repair_run = self.repair_run + 1 if self.entry_ask - chosen.bid >= REPAIR_DRAWDOWN else 0
                if self.repair_run >= REPAIR_RUN:
                    self.switch_due_age = age + 1
                    events.append(self._event(
                        "switch_armed", age, side=self.selected_side,
                        entry_ask=self.entry_ask, bid=chosen.bid, due_age=self.switch_due_age,
                    ))

        self.last_age = age
        return tuple(events)

    def mark_recorded(self, age: int) -> None:
        if isinstance(age, bool) or not isinstance(age, int) or not 0 <= age < 300:
            raise ValueError("quality recorded age must be in [0,300)")
        if self.last_recorded_age is not None and age <= self.last_recorded_age:
            raise ValueError("quality recorded age must increase")
        self.last_recorded_age = age

    def settle(self, winner: str, resolved_at: int | None) -> dict[str, Any]:
        if winner not in {"UP", "DOWN"}:
            raise ValueError("quality winner must be UP or DOWN")
        if self.stage == "SETTLED":
            if self.winner != winner:
                raise ValueError("quality settlement is immutable")
            return self._event("settlement", 300, winner=self.winner, pnl=self.pnl, duplicate=True)
        self.winner = winner
        if self.entry_ask is not None and self.shares is not None and self.selected_side is not None:
            if self.switch_age is None:
                payout = self.shares if winner == self.selected_side else ZERO
                self.pnl = -PAPER_NOTIONAL + payout
            else:
                assert self.switch_bid is not None and self.opposite_ask is not None
                payout = self.shares if winner == self._opposite(self.selected_side) else ZERO
                self.pnl = (
                    -PAPER_NOTIONAL + self.switch_bid * self.shares
                    - self.opposite_ask * self.shares + payout
                )
        self.stage = "SETTLED"
        return self._event(
            "settlement", 300, winner=winner, resolved_at=resolved_at,
            selected_side=self.selected_side, switched=self.switch_age is not None,
            pnl=self.pnl, profitable=None if self.pnl is None else self.pnl > ZERO,
        )

    def snapshot(self) -> dict[str, Any]:
        def text(value: Decimal | None) -> str | None:
            return None if value is None else str(value)
        return {
            "version": VERSION, "market_id": self.market_id, "mkt_ts": self.mkt_ts,
            "symbol": self.symbol, "stage": self.stage, "reason": self.reason,
            "last_age": self.last_age, "last_recorded_age": self.last_recorded_age,
            "selected_side": self.selected_side,
            "p30": text(self.p30), "filter_a_run": self.filter_a_run,
            "filter_b_run": self.filter_b_run, "filter_a": self.filter_a,
            "filter_b": self.filter_b, "entry_ask": text(self.entry_ask),
            "shares": text(self.shares), "repair_run": self.repair_run,
            "switch_due_age": self.switch_due_age, "switch_age": self.switch_age,
            "switch_bid": text(self.switch_bid), "opposite_ask": text(self.opposite_ask),
            "extra_capital": text(self.extra_capital), "winner": self.winner,
            "pnl": text(self.pnl),
        }

    @classmethod
    def restore(cls, payload: Mapping[str, Any]) -> QualityShadowState:
        if payload.get("version") != VERSION:
            raise ValueError("quality snapshot version is unsupported")
        state = cls(str(payload["market_id"]), int(payload["mkt_ts"]), str(payload["symbol"]))
        stage = payload.get("stage")
        if stage not in {"WATCHING", "CANDIDATE", "NO_SIGNAL", "REJECTED", "MISSED", "ENTERED", "SWITCHED", "SETTLED"}:
            raise ValueError("quality snapshot stage is invalid")
        state.stage, state.reason = stage, payload.get("reason")
        state.last_age = payload.get("last_age")
        state.last_recorded_age = payload.get("last_recorded_age", state.last_age)
        state.selected_side = payload.get("selected_side")
        for name in ("p30", "entry_ask", "shares", "switch_bid", "opposite_ask", "extra_capital", "pnl"):
            raw = payload.get(name)
            setattr(state, name, None if raw is None else Decimal(str(raw)))
        for name in ("filter_a_run", "filter_b_run", "repair_run"):
            setattr(state, name, int(payload.get(name, 0)))
        for name in ("filter_a", "filter_b"):
            setattr(state, name, bool(payload.get(name, False)))
        for name in ("switch_due_age", "switch_age"):
            raw = payload.get(name); setattr(state, name, None if raw is None else int(raw))
        state.winner = payload.get("winner")
        return state


__all__ = ["QualityBook", "QualityShadowState"]
