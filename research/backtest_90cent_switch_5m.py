#!/usr/bin/env python3
"""Paper-only late-window 90-cent entry with one opposite-side switch."""

from __future__ import annotations

import argparse
import csv
import json
import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from research.backtest_90cent_5m import load_gamma_outcomes, taker_fee

ENTRY_PRICE = 0.90
POSITION_SIZE = 50.0
MIN_ENTRY_AGE = 150
MARKET_SECONDS = 300
MAX_GAP_SECONDS = 2
EXECUTION_MODELS = ("strict_50", "optimistic_touch")
EPSILON = 1e-9
SIDE_PREFIX = {"UP": "up", "DOWN": "dn"}


def _finite_price(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number) or number < 0.0 or number > 1.0:
        return None
    return number


def _finite_quantity(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number) or number < 0.0:
        return None
    return number


def _depth_ok(model: str, *quantities: float | None) -> bool:
    if any(quantity is None for quantity in quantities):
        return False
    required = POSITION_SIZE if model == "strict_50" else 0.0
    if model == "strict_50":
        return all(quantity >= required for quantity in quantities if quantity is not None)
    return all(quantity > required for quantity in quantities if quantity is not None)


@dataclass
class ExecutionTrack:
    model: str
    initial_filled: bool = False
    initial_side: str | None = None
    initial_price: float | None = None
    initial_ts: int | None = None
    initial_age: int | None = None
    switched: bool = False
    switch_side: str | None = None
    switch_sell_price: float | None = None
    switch_buy_price: float | None = None
    switch_ts: int | None = None
    switch_age: int | None = None
    switch_count: int = 0
    initial_depth_rejected: bool = False
    switch_depth_rejections: int = 0
    switch_price_rejections: int = 0


@dataclass
class MarketState:
    symbol: str
    mkt_ts: int
    tracks: dict[str, ExecutionTrack] = field(
        default_factory=lambda: {model: ExecutionTrack(model) for model in EXECUTION_MODELS}
    )
    signal_side: str | None = None
    signal_ts: int | None = None
    signal_age: int | None = None
    signal_ask: float | None = None
    ambiguous: bool = False
    previous_asks: dict[str, float] | None = None
    previous_book_ts: int | None = None
    rows: int = 0
    first_ts: int | None = None
    last_ts: int | None = None
    final_age: int | None = None

    def process_row(self, row: dict) -> None:
        ts = int(row["ts"])
        age = int(row.get("age", ts - self.mkt_ts))
        self.rows += 1
        self.first_ts = ts if self.first_ts is None else min(self.first_ts, ts)
        self.last_ts = ts if self.last_ts is None else max(self.last_ts, ts)
        self.final_age = age if self.final_age is None else max(self.final_age, age)

        asks = {
            side: _finite_price(row.get(f"{prefix}_ask"))
            for side, prefix in SIDE_PREFIX.items()
        }
        if any(price is None for price in asks.values()):
            self.previous_asks = None
            self.previous_book_ts = None
            return

        continuous = (
            self.previous_asks is not None
            and self.previous_book_ts is not None
            and 0 < ts - self.previous_book_ts <= MAX_GAP_SECONDS
        )
        crossings: list[str] = []
        if continuous:
            crossings = [
                side
                for side in ("UP", "DOWN")
                if self.previous_asks[side] < ENTRY_PRICE <= asks[side]
            ]

        if not self.ambiguous and self.signal_side is None and age >= MIN_ENTRY_AGE:
            if len(crossings) == 2:
                self.ambiguous = True
            elif len(crossings) == 1:
                side = crossings[0]
                self.signal_side = side
                self.signal_ts = ts
                self.signal_age = age
                self.signal_ask = asks[side]
                ask_quantity = _finite_quantity(row.get(f"{SIDE_PREFIX[side]}_askq"))
                at_limit = math.isclose(asks[side], ENTRY_PRICE, abs_tol=EPSILON)
                for track in self.tracks.values():
                    if at_limit and _depth_ok(track.model, ask_quantity):
                        track.initial_filled = True
                        track.initial_side = side
                        track.initial_price = ENTRY_PRICE
                        track.initial_ts = ts
                        track.initial_age = age
                    elif at_limit:
                        track.initial_depth_rejected = True
        elif self.signal_side is not None and len(crossings) == 1:
            opposite = "DOWN" if self.signal_side == "UP" else "UP"
            if crossings[0] == opposite and ts != self.signal_ts:
                opposite_ask = asks[opposite]
                held_prefix = SIDE_PREFIX[self.signal_side]
                opposite_prefix = SIDE_PREFIX[opposite]
                held_bid = _finite_price(row.get(f"{held_prefix}_bid"))
                opposite_ask_quantity = _finite_quantity(row.get(f"{opposite_prefix}_askq"))
                held_bid_quantity = _finite_quantity(row.get(f"{held_prefix}_bidq"))
                at_limit = math.isclose(opposite_ask, ENTRY_PRICE, abs_tol=EPSILON)
                for track in self.tracks.values():
                    if not track.initial_filled or track.switched:
                        continue
                    if not at_limit:
                        track.switch_price_rejections += 1
                        continue
                    if held_bid is None or not _depth_ok(
                        track.model, held_bid_quantity, opposite_ask_quantity
                    ):
                        track.switch_depth_rejections += 1
                        continue
                    track.switched = True
                    track.switch_side = opposite
                    track.switch_sell_price = held_bid
                    track.switch_buy_price = ENTRY_PRICE
                    track.switch_ts = ts
                    track.switch_age = age
                    track.switch_count += 1

        self.previous_asks = {side: float(price) for side, price in asks.items()}
        self.previous_book_ts = ts


def _market_complete(state: MarketState, outcome: dict | None) -> bool:
    return (
        state.final_age == MARKET_SECONDS - 1
        and outcome is not None
        and outcome.get("winner") in {"UP", "DOWN"}
    )


def _utc_day(timestamp: int | None) -> str | None:
    if timestamp is None:
        return None
    return datetime.fromtimestamp(timestamp, tz=timezone.utc).strftime("%Y-%m-%d")


def trade_record(
    state: MarketState,
    model: str,
    outcome: dict | None,
) -> dict:
    track = state.tracks[model]
    complete = _market_complete(state, outcome)
    winner = outcome["winner"] if complete and outcome is not None else None
    fee_rate = float(outcome["fee_rate"]) if complete and outcome is not None else 0.07
    fee_exponent = float(outcome["fee_exponent"]) if complete and outcome is not None else 1.0

    initial_fee = None
    switch_sell_fee = None
    switch_buy_fee = None
    hold_net_per_share = None
    strategy_net_per_share = None
    initial_won = None
    strategy_won = None
    final_side = track.switch_side if track.switched else track.initial_side
    switch_effect = None

    if complete and track.initial_filled and winner is not None and track.initial_price is not None:
        initial_fee = taker_fee(track.initial_price, fee_rate, fee_exponent)
        initial_won = track.initial_side == winner
        hold_net_per_share = (1.0 if initial_won else 0.0) - track.initial_price - initial_fee
        strategy_net_per_share = hold_net_per_share
        strategy_won = final_side == winner
        if (
            track.switched
            and track.switch_sell_price is not None
            and track.switch_buy_price is not None
        ):
            switch_sell_fee = taker_fee(track.switch_sell_price, fee_rate, fee_exponent)
            switch_buy_fee = taker_fee(track.switch_buy_price, fee_rate, fee_exponent)
            strategy_net_per_share = (
                -track.initial_price
                - initial_fee
                + track.switch_sell_price
                - switch_sell_fee
                - track.switch_buy_price
                - switch_buy_fee
                + (1.0 if strategy_won else 0.0)
            )
            delta = strategy_net_per_share - hold_net_per_share
            if not initial_won and delta > EPSILON:
                switch_effect = "rescued"
            elif initial_won and delta < -EPSILON:
                switch_effect = "harmed"
            elif delta > EPSILON:
                switch_effect = "improved"
            elif delta < -EPSILON:
                switch_effect = "worsened"
            else:
                switch_effect = "neutral"

    hold_net_50 = hold_net_per_share * POSITION_SIZE if hold_net_per_share is not None else None
    strategy_net_50 = (
        strategy_net_per_share * POSITION_SIZE if strategy_net_per_share is not None else None
    )
    incremental_50 = (
        strategy_net_50 - hold_net_50
        if strategy_net_50 is not None and hold_net_50 is not None
        else None
    )
    return {
        "model": model,
        "symbol": state.symbol,
        "tf": "5m",
        "mkt_ts": state.mkt_ts,
        "mkt_day": _utc_day(state.mkt_ts),
        "complete": complete,
        "winner": winner,
        "settlement_source": "official_gamma_outcomePrices",
        "fee_rate": fee_rate,
        "fee_exponent": fee_exponent,
        "signal": state.signal_side is not None,
        "ambiguous": state.ambiguous,
        "signal_side": state.signal_side,
        "signal_ts": state.signal_ts,
        "signal_age": state.signal_age,
        "signal_ask": state.signal_ask,
        "initial_filled": track.initial_filled,
        "initial_depth_rejected": track.initial_depth_rejected,
        "initial_side": track.initial_side,
        "initial_price": track.initial_price,
        "initial_ts": track.initial_ts,
        "initial_age": track.initial_age,
        "switched": track.switched,
        "switch_side": track.switch_side,
        "switch_sell_price": track.switch_sell_price,
        "switch_buy_price": track.switch_buy_price,
        "switch_ts": track.switch_ts,
        "switch_age": track.switch_age,
        "switch_depth_rejections": track.switch_depth_rejections,
        "switch_price_rejections": track.switch_price_rejections,
        "final_side": final_side,
        "initial_won": initial_won,
        "strategy_won": strategy_won,
        "initial_buy_fee_per_share": initial_fee,
        "switch_sell_fee_per_share": switch_sell_fee,
        "switch_buy_fee_per_share": switch_buy_fee,
        "hold_net_pnl_per_share": hold_net_per_share,
        "strategy_net_pnl_per_share": strategy_net_per_share,
        "hold_net_pnl_50": hold_net_50,
        "strategy_net_pnl_50": strategy_net_50,
        "incremental_switch_pnl_50": incremental_50,
        "switch_effect": switch_effect,
        "market_rows": state.rows,
    }


def _mean(values: Iterable[float]) -> float | None:
    data = list(values)
    return sum(data) / len(data) if data else None


def _stats(records: list[dict]) -> dict:
    complete = [record for record in records if record["complete"]]
    signals = [record for record in complete if record["signal"]]
    fills = [record for record in complete if record["initial_filled"]]
    switches = [record for record in fills if record["switched"]]
    hold_wins = sum(record["initial_won"] is True for record in fills)
    strategy_wins = sum(record["strategy_won"] is True for record in fills)
    return {
        "complete_markets": len(complete),
        "signals": len(signals),
        "ambiguous_markets": sum(record["ambiguous"] for record in complete),
        "initial_fills": len(fills),
        "initial_no_fills": len(signals) - len(fills),
        "initial_fill_rate": len(fills) / len(signals) if signals else None,
        "switches": len(switches),
        "switch_rate_per_fill": len(switches) / len(fills) if fills else None,
        "hold_wins": hold_wins,
        "hold_losses": len(fills) - hold_wins,
        "hold_win_rate": hold_wins / len(fills) if fills else None,
        "strategy_wins": strategy_wins,
        "strategy_losses": len(fills) - strategy_wins,
        "strategy_win_rate": strategy_wins / len(fills) if fills else None,
        "switched_side_wins": sum(record["strategy_won"] is True for record in switches),
        "rescued_initial_losses": sum(record["switch_effect"] == "rescued" for record in switches),
        "harmed_initial_winners": sum(record["switch_effect"] == "harmed" for record in switches),
        "positive_switch_deltas": sum(
            record["incremental_switch_pnl_50"] > EPSILON for record in switches
        ),
        "negative_switch_deltas": sum(
            record["incremental_switch_pnl_50"] < -EPSILON for record in switches
        ),
        "hold_net_pnl_total_50": sum(record["hold_net_pnl_50"] for record in fills),
        "strategy_net_pnl_total_50": sum(
            record["strategy_net_pnl_50"] for record in fills
        ),
        "incremental_switch_pnl_total_50": sum(
            record["incremental_switch_pnl_50"] for record in fills
        ),
        "hold_net_pnl_average_50": _mean(record["hold_net_pnl_50"] for record in fills),
        "strategy_net_pnl_average_50": _mean(
            record["strategy_net_pnl_50"] for record in fills
        ),
        "incremental_switch_pnl_average_per_switch_50": _mean(
            record["incremental_switch_pnl_50"] for record in switches
        ),
        "average_initial_age": _mean(
            float(record["initial_age"])
            for record in fills
            if record["initial_age"] is not None
        ),
        "average_switch_age": _mean(
            float(record["switch_age"])
            for record in switches
            if record["switch_age"] is not None
        ),
        "total_modeled_fees_50": sum(
            POSITION_SIZE
            * sum(
                fee or 0.0
                for fee in (
                    record["initial_buy_fee_per_share"],
                    record["switch_sell_fee_per_share"],
                    record["switch_buy_fee_per_share"],
                )
            )
            for record in fills
        ),
    }


def _write_trades(path: Path, records: list[dict]) -> None:
    if not records:
        path.write_text("")
        return
    with path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(records[0]))
        writer.writeheader()
        writer.writerows(records)


def run_backtest(
    data_dir: Path,
    out_dir: Path,
    outcomes: dict[tuple[str, int], dict],
) -> dict:
    if not outcomes:
        raise ValueError("official Gamma outcomes are required")
    files = sorted(data_dir.glob("*.jsonl"))
    if not files:
        raise ValueError(f"no JSONL files found in {data_dir}")

    coverage = {
        "physical_lines": 0,
        "blank_rows": 0,
        "malformed_rows": 0,
        "non_5m_rows": 0,
        "unsupported_symbol_rows": 0,
        "duplicate_rows": 0,
        "out_of_order_rows": 0,
        "outside_window_rows": 0,
        "valid_5m_rows": 0,
        "official_outcomes_loaded": len(outcomes),
    }
    states: dict[tuple[str, int], MarketState] = {}
    seen_timestamps: dict[tuple[str, int], set[int]] = {}
    last_timestamps: dict[tuple[str, int], int] = {}
    min_ts: int | None = None
    max_ts: int | None = None

    for path in files:
        with path.open() as handle:
            for line in handle:
                coverage["physical_lines"] += 1
                if not line.strip():
                    coverage["blank_rows"] += 1
                    continue
                try:
                    row = json.loads(line)
                except (json.JSONDecodeError, TypeError):
                    coverage["malformed_rows"] += 1
                    continue
                if row.get("tf") != "5m":
                    coverage["non_5m_rows"] += 1
                    continue
                symbol = str(row.get("symbol", "")).lower()
                if symbol not in {"btc", "eth", "sol"}:
                    coverage["unsupported_symbol_rows"] += 1
                    continue
                try:
                    ts = int(row["ts"])
                    mkt_ts = int(row["mkt_ts"])
                except (KeyError, TypeError, ValueError):
                    coverage["malformed_rows"] += 1
                    continue
                if ts < mkt_ts or ts >= mkt_ts + MARKET_SECONDS:
                    coverage["outside_window_rows"] += 1
                    continue
                key = (symbol, mkt_ts)
                seen = seen_timestamps.setdefault(key, set())
                if ts in seen:
                    coverage["duplicate_rows"] += 1
                    continue
                if key in last_timestamps and ts < last_timestamps[key]:
                    coverage["out_of_order_rows"] += 1
                    continue
                seen.add(ts)
                last_timestamps[key] = ts
                state = states.setdefault(key, MarketState(symbol, mkt_ts))
                state.process_row(row)
                coverage["valid_5m_rows"] += 1
                min_ts = ts if min_ts is None else min(min_ts, ts)
                max_ts = ts if max_ts is None else max(max_ts, ts)

    records = [
        trade_record(state, model, outcomes.get((state.symbol, state.mkt_ts)))
        for model in EXECUTION_MODELS
        for state in states.values()
    ]
    records.sort(key=lambda record: (record["model"], record["symbol"], record["mkt_ts"]))
    complete_markets = sum(
        _market_complete(state, outcomes.get((state.symbol, state.mkt_ts)))
        for state in states.values()
    )
    coverage["official_outcomes_matched"] = complete_markets
    out_dir.mkdir(parents=True, exist_ok=True)
    summary = {
        "paper_only": True,
        "strategy": "90-cent entry in final 150s with at most one opposite 90-cent switch",
        "position_size_shares": POSITION_SIZE,
        "entry_price": ENTRY_PRICE,
        "minimum_entry_age": MIN_ENTRY_AGE,
        "symbols": sorted({state.symbol for state in states.values()}),
        "timeframe": "5m",
        "settlement_source": "official_gamma_outcomePrices",
        "execution_models": list(EXECUTION_MODELS),
        "files": [path.name for path in files],
        "date_range_utc": [_utc_day(min_ts), _utc_day(max_ts)],
        "row_range_utc": [min_ts, max_ts],
        "coverage": coverage,
        "markets": {
            "seen": len(states),
            "complete": complete_markets,
            "incomplete": len(states) - complete_markets,
        },
        "models": {
            model: _stats([record for record in records if record["model"] == model])
            for model in EXECUTION_MODELS
        },
        "by_symbol": {
            model: {
                symbol: _stats(
                    [
                        record
                        for record in records
                        if record["model"] == model and record["symbol"] == symbol
                    ]
                )
                for symbol in ("btc", "eth", "sol")
            }
            for model in EXECUTION_MODELS
        },
        "artifacts": {"summary": "summary.json", "trades": "trades.csv"},
        "limitations": [
            "one-second snapshots do not prove atomic switch execution",
            "strict top depth is necessary but not sufficient for real fills",
            "optimistic touch models 50 shares from any positive displayed quantity",
            "historical Chainlink confirmation excluded because recorded observations were stale",
        ],
    }
    summary_path = out_dir / "summary.json"
    trades_path = out_dir / "trades.csv"
    summary_path.write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n")
    _write_trades(trades_path, records)
    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--out-dir", type=Path, required=True)
    parser.add_argument("--outcomes-file", type=Path, required=True)
    args = parser.parse_args(argv)
    outcomes = load_gamma_outcomes(args.outcomes_file)
    summary = run_backtest(args.data_dir, args.out_dir, outcomes)
    strict = summary["models"]["strict_50"]
    optimistic = summary["models"]["optimistic_touch"]
    print(
        "90-cent switch backtest complete: "
        f"rows={summary['coverage']['valid_5m_rows']} "
        f"markets={summary['markets']['seen']} complete={summary['markets']['complete']} "
        f"strict_fills={strict['initial_fills']} strict_switches={strict['switches']} "
        f"optimistic_fills={optimistic['initial_fills']} "
        f"optimistic_switches={optimistic['switches']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
