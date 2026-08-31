#!/usr/bin/env python3
"""Quote-touch paper backtest for paired UP/DOWN maker orders."""

from __future__ import annotations

import argparse
import csv
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
import json
import math
from pathlib import Path
from typing import Iterable, Iterator


WINDOW_SECONDS = {"5m": 300, "15m": 900}
MODE_QUOTE_TOUCH = "quote_touch"
MODE_STRICT_FULL = "strict_full"
MODES = (MODE_QUOTE_TOUCH, MODE_STRICT_FULL)
ORDER_SIZE = 50.0
OFFSET_CENTS = 1.0
TICK = 0.01
REWARD_MIN_SIZE = 50.0
REWARD_MAX_SPREAD_CENTS = 1.5
EPSILON = 1e-9


@dataclass(frozen=True)
class Config:
    order_size: float = ORDER_SIZE
    offset_cents: float = OFFSET_CENTS
    tick: float = TICK
    min_reward_size: float = REWARD_MIN_SIZE
    max_reward_spread_cents: float = REWARD_MAX_SPREAD_CENTS
    order_min_size_usdc: float = 5.0


@dataclass
class MarketState:
    symbol: str
    tf: str
    mkt_ts: int
    step: int
    strike_cl: float | None = None
    final_cl: float | None = None
    final_age: int | None = None
    settled: bool = False
    winner: str | None = None
    up_filled: float = 0.0
    down_filled: float = 0.0
    up_cost: float = 0.0
    down_cost: float = 0.0
    up_fill_count: int = 0
    down_fill_count: int = 0
    up_first_fill_ts: int | None = None
    down_first_fill_ts: int | None = None
    up_last_fill_ts: int | None = None
    down_last_fill_ts: int | None = None
    up_order_price: float | None = None
    down_order_price: float | None = None
    up_remaining: float = ORDER_SIZE
    down_remaining: float = ORDER_SIZE
    initialized: bool = False
    quote_start_ts: int | None = None
    quote_observations: int = 0
    requote_count: int = 0
    reward_eligible_seconds: int = 0
    reward_relative_score: float = 0.0
    rows: int = 0
    duplicate_rows: int = 0

    def update_reward_seconds(
        self,
        row: dict,
        min_size: float = REWARD_MIN_SIZE,
        max_spread_cents: float = REWARD_MAX_SPREAD_CENTS,
    ) -> bool:
        """Count a two-sided reward-eligible observation and its relative score."""
        if self.up_order_price is None or self.down_order_price is None:
            return False
        if self.up_remaining + EPSILON < min_size or self.down_remaining + EPSILON < min_size:
            return False
        up_book = _book(row, "up")
        down_book = _book(row, "dn")
        if up_book is None or down_book is None:
            return False
        up_bid, up_ask, _up_q = up_book
        down_bid, down_ask, _down_q = down_book
        up_mid = (up_bid + up_ask) / 2.0
        down_mid = (down_bid + down_ask) / 2.0
        up_spread = abs(up_mid - self.up_order_price) * 100.0
        down_spread = abs(down_mid - self.down_order_price) * 100.0
        if up_spread > max_spread_cents + EPSILON or down_spread > max_spread_cents + EPSILON:
            return False
        self.reward_eligible_seconds += 1
        self.reward_relative_score += min(
            relative_order_score(max_spread_cents, up_spread),
            relative_order_score(max_spread_cents, down_spread),
        )
        return True


def _finite_number(value: object) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(float(value))
    )


def quote_price(midpoint: float, offset_cents: float = OFFSET_CENTS, tick: float = TICK) -> float:
    """Return midpoint-offset rounded down to the active price tick."""
    if not _finite_number(midpoint) or not _finite_number(offset_cents) or tick <= 0:
        return math.nan
    raw = float(midpoint) - float(offset_cents) / 100.0
    ticks = math.floor((raw + EPSILON) / tick)
    return round(ticks * tick, 10)


def relative_order_score(max_spread_cents: float, spread_cents: float) -> float:
    """Official quadratic score shape with the unknown multiplier set to one."""
    if max_spread_cents <= 0 or spread_cents < 0 or spread_cents > max_spread_cents:
        return 0.0
    return ((max_spread_cents - spread_cents) / max_spread_cents) ** 2


def _book(row: dict, prefix: str) -> tuple[float, float, float | None] | None:
    bid = row.get(f"{prefix}_bid")
    ask = row.get(f"{prefix}_ask")
    qty = row.get(f"{prefix}_askq")
    if not _finite_number(bid) or not _finite_number(ask):
        return None
    bid_f = float(bid)
    ask_f = float(ask)
    if not (0.0 <= bid_f <= 1.0 and 0.0 <= ask_f <= 1.0 and bid_f <= ask_f):
        return None
    qty_f = float(qty) if _finite_number(qty) and float(qty) > 0 else None
    return bid_f, ask_f, qty_f


def _quote_pair(row: dict, config: Config) -> tuple[float, float] | None:
    up = _book(row, "up")
    down = _book(row, "dn")
    if up is None or down is None:
        return None
    up_mid = (up[0] + up[1]) / 2.0
    down_mid = (down[0] + down[1]) / 2.0
    up_price = quote_price(up_mid, config.offset_cents, config.tick)
    down_price = quote_price(down_mid, config.offset_cents, config.tick)
    if not _finite_number(up_price) or not _finite_number(down_price):
        return None
    if not (0.0 < up_price < 1.0 and 0.0 < down_price < 1.0):
        return None
    if up_price * config.order_size < config.order_min_size_usdc:
        return None
    if down_price * config.order_size < config.order_min_size_usdc:
        return None
    return up_price, down_price


def fee_for_maker(_price: float) -> float:
    """Current crypto market schedule is taker-only; makers pay zero."""
    return 0.0


def settle_pair(state: MarketState, winner: str | None) -> dict:
    """Settle filled shares at $1/$0 and expose unhedged one-leg risk."""
    cost = state.up_cost + state.down_cost
    if winner == "UP":
        payout = state.up_filled
    elif winner == "DOWN":
        payout = state.down_filled
    else:
        payout = 0.0
    paired = (
        state.up_filled + EPSILON >= ORDER_SIZE
        and state.down_filled + EPSILON >= ORDER_SIZE
    )
    return {
        "payout": payout,
        "cost": cost,
        "pnl": payout - cost,
        "paired": paired,
    }


def _fill_leg(state: MarketState, side: str, quantity: float, price: float, ts: int) -> None:
    if quantity <= 0:
        return
    if side == "up":
        state.up_filled += quantity
        state.up_remaining -= quantity
        state.up_cost += quantity * price + quantity * fee_for_maker(price)
        state.up_fill_count += 1
        state.up_first_fill_ts = ts if state.up_first_fill_ts is None else state.up_first_fill_ts
        state.up_last_fill_ts = ts
    else:
        state.down_filled += quantity
        state.down_remaining -= quantity
        state.down_cost += quantity * price + quantity * fee_for_maker(price)
        state.down_fill_count += 1
        state.down_first_fill_ts = ts if state.down_first_fill_ts is None else state.down_first_fill_ts
        state.down_last_fill_ts = ts
    if state.up_remaining < EPSILON:
        state.up_remaining = 0.0
    if state.down_remaining < EPSILON:
        state.down_remaining = 0.0


def _try_fill(
    state: MarketState,
    row: dict,
    side: str,
    mode: str,
    order_size: float,
) -> float:
    if side == "up":
        remaining = state.up_remaining
        order_price = state.up_order_price
        prefix = "up"
    else:
        remaining = state.down_remaining
        order_price = state.down_order_price
        prefix = "dn"
    if remaining <= EPSILON or order_price is None:
        return 0.0
    book = _book(row, prefix)
    if book is None:
        return 0.0
    _bid, ask, ask_qty = book
    if ask > order_price + EPSILON or ask_qty is None:
        return 0.0
    if mode == MODE_STRICT_FULL:
        if ask_qty + EPSILON < remaining:
            return 0.0
        quantity = remaining
    elif mode == MODE_QUOTE_TOUCH:
        quantity = min(remaining, ask_qty)
    else:
        raise ValueError(f"unknown mode: {mode}")
    _fill_leg(state, side, quantity, order_price, int(row["ts"]))
    return quantity


def _process_row(state: MarketState, row: dict, config: Config, mode: str) -> None:
    age = int(row["age"])
    if age < 0 or age >= state.step or state.settled:
        return
    state.rows += 1
    if not state.initialized:
        pair = _quote_pair(row, config)
        if pair is None:
            return
        state.up_order_price, state.down_order_price = pair
        state.initialized = True
        state.quote_start_ts = int(row["ts"])
        state.quote_observations += 1
        state.update_reward_seconds(row, config.min_reward_size, config.max_reward_spread_cents)
    else:
        _try_fill(state, row, "up", mode, config.order_size)
        _try_fill(state, row, "down", mode, config.order_size)
        pair = _quote_pair(row, config)
        if pair is not None:
            if state.up_remaining > EPSILON and state.up_order_price != pair[0]:
                state.requote_count += 1
            if state.down_remaining > EPSILON and state.down_order_price != pair[1]:
                state.requote_count += 1
            if state.up_remaining > EPSILON:
                state.up_order_price = pair[0]
            if state.down_remaining > EPSILON:
                state.down_order_price = pair[1]
            state.quote_observations += 1
        state.update_reward_seconds(row, config.min_reward_size, config.max_reward_spread_cents)

    if age == state.step - 1:
        state.final_age = age
        final_cl = row.get("cl_twap60")
        state.final_cl = float(final_cl) if _finite_number(final_cl) else None
        strike = row.get("strike_cl")
        if state.strike_cl is None and _finite_number(strike) and float(strike) > 0:
            state.strike_cl = float(strike)
        state.winner = infer_winner(state)
        if state.winner is not None:
            state.settled = True


def infer_winner(state: MarketState) -> str | None:
    if not _finite_number(state.strike_cl) or float(state.strike_cl) <= 0:
        return None
    if not _finite_number(state.final_cl):
        return None
    return "UP" if float(state.final_cl) >= float(state.strike_cl) else "DOWN"


def simulate_market(
    symbol: str,
    tf: str,
    mkt_ts: int,
    rows: Iterable[dict],
    mode: str = MODE_QUOTE_TOUCH,
    config: Config = Config(),
) -> MarketState:
    """Simulate one market from rows ordered by timestamp."""
    if tf not in WINDOW_SECONDS:
        raise ValueError(f"unsupported timeframe: {tf}")
    state = MarketState(symbol, tf, int(mkt_ts), WINDOW_SECONDS[tf])
    ordered = sorted(rows, key=lambda row: (int(row["ts"]), int(row.get("age", 0))))
    for row in ordered:
        _process_row(state, row, config, mode)
        if state.settled:
            break
    return state


def _new_mode_stats() -> dict:
    return {
        "markets": 0,
        "complete_markets": 0,
        "incomplete_markets": 0,
        "quoted_markets": 0,
        "zero_leg_markets": 0,
        "one_leg_or_partial_markets": 0,
        "full_pair_markets": 0,
        "settled_zero_leg_markets": 0,
        "settled_one_leg_or_partial_markets": 0,
        "settled_full_pair_markets": 0,
        "up_order_count": 0,
        "down_order_count": 0,
        "up_fill_events": 0,
        "down_fill_events": 0,
        "up_filled_shares": 0.0,
        "down_filled_shares": 0.0,
        "filled_volume": 0.0,
        "settled_cost": 0.0,
        "settled_payout": 0.0,
        "settled_pnl": 0.0,
        "reward_eligible_seconds": 0,
        "reward_relative_score": 0.0,
        "quote_observations": 0,
        "requotes": 0,
    }


def _pair_record(state: MarketState, mode: str) -> dict:
    settlement = settle_pair(state, state.winner) if state.settled else None
    up_avg = state.up_cost / state.up_filled if state.up_filled else None
    down_avg = state.down_cost / state.down_filled if state.down_filled else None
    if state.up_filled <= EPSILON and state.down_filled <= EPSILON:
        classification = "zero_leg"
    elif state.up_filled + EPSILON >= ORDER_SIZE and state.down_filled + EPSILON >= ORDER_SIZE:
        classification = "full_pair"
    else:
        classification = "one_leg_or_partial"
    return {
        "mode": mode,
        "symbol": state.symbol,
        "tf": state.tf,
        "mkt_ts": state.mkt_ts,
        "mkt_day": _utc_day(state.mkt_ts),
        "step": state.step,
        "settled": state.settled,
        "winner": state.winner,
        "strike_cl": state.strike_cl,
        "final_cl": state.final_cl,
        "final_age": state.final_age,
        "quoted": state.initialized,
        "quote_start_ts": state.quote_start_ts,
        "up_initial_price": state.up_order_price if state.initialized else None,
        "down_initial_price": state.down_order_price if state.initialized else None,
        "up_filled": state.up_filled,
        "down_filled": state.down_filled,
        "up_fill_count": state.up_fill_count,
        "down_fill_count": state.down_fill_count,
        "up_first_fill_ts": state.up_first_fill_ts,
        "down_first_fill_ts": state.down_first_fill_ts,
        "up_last_fill_ts": state.up_last_fill_ts,
        "down_last_fill_ts": state.down_last_fill_ts,
        "up_average_fill_price": up_avg,
        "down_average_fill_price": down_avg,
        "up_cost": state.up_cost,
        "down_cost": state.down_cost,
        "cost": settlement["cost"] if settlement else None,
        "payout": settlement["payout"] if settlement else None,
        "pnl": settlement["pnl"] if settlement else None,
        "paired": settlement["paired"] if settlement else False,
        "classification": classification,
        "reward_eligible_seconds": state.reward_eligible_seconds,
        "reward_relative_score": state.reward_relative_score,
        "quote_observations": state.quote_observations,
        "requotes": state.requote_count,
    }


def _update_mode_stats(stats: dict, record: dict) -> None:
    stats["markets"] += 1
    stats["complete_markets"] += int(record["settled"])
    stats["incomplete_markets"] += int(not record["settled"])
    stats["quoted_markets"] += int(record["quoted"])
    stats[f"{record['classification']}_markets"] += 1
    if record["settled"]:
        stats[f"settled_{record['classification']}_markets"] += 1
    stats["up_order_count"] += int(record["quoted"])
    stats["down_order_count"] += int(record["quoted"])
    stats["up_fill_events"] += record["up_fill_count"]
    stats["down_fill_events"] += record["down_fill_count"]
    stats["up_filled_shares"] += record["up_filled"]
    stats["down_filled_shares"] += record["down_filled"]
    stats["filled_volume"] += record["up_filled"] + record["down_filled"]
    stats["reward_eligible_seconds"] += record["reward_eligible_seconds"]
    stats["reward_relative_score"] += record["reward_relative_score"]
    stats["quote_observations"] += record["quote_observations"]
    stats["requotes"] += record["requotes"]
    if record["settled"]:
        stats["settled_cost"] += record["cost"]
        stats["settled_payout"] += record["payout"]
        stats["settled_pnl"] += record["pnl"]


def _aggregate_records(records: list[dict], keys: tuple[str, ...]) -> list[dict]:
    groups: dict[tuple, list[dict]] = defaultdict(list)
    for record in records:
        groups[tuple(record[key] for key in keys)].append(record)
    result = []
    for key, rows in sorted(groups.items(), key=lambda item: tuple(str(x) for x in item[0])):
        settled = [row for row in rows if row["settled"]]
        values = dict(zip(keys, key))
        values.update(
            {
                "markets": len(rows),
                "complete_markets": len(settled),
                "incomplete_markets": len(rows) - len(settled),
                "quoted_markets": sum(int(row["quoted"]) for row in rows),
                "zero_leg_markets": sum(row["classification"] == "zero_leg" for row in rows),
                "one_leg_or_partial_markets": sum(row["classification"] == "one_leg_or_partial" for row in rows),
                "full_pair_markets": sum(row["classification"] == "full_pair" for row in rows),
                "up_filled_shares": sum(row["up_filled"] for row in rows),
                "down_filled_shares": sum(row["down_filled"] for row in rows),
                "filled_volume": sum(row["up_filled"] + row["down_filled"] for row in rows),
                "settled_cost": sum(row["cost"] or 0.0 for row in settled),
                "settled_payout": sum(row["payout"] or 0.0 for row in settled),
                "settled_pnl": sum(row["pnl"] or 0.0 for row in settled),
                "reward_eligible_seconds": sum(row["reward_eligible_seconds"] for row in rows),
                "reward_relative_score": sum(row["reward_relative_score"] for row in rows),
            }
        )
        result.append(values)
    return result


def _write_pairs(path: Path, records: list[dict]) -> None:
    fields = [
        "mode", "symbol", "tf", "mkt_ts", "mkt_day", "step", "settled", "winner",
        "strike_cl", "final_cl", "final_age", "quoted", "quote_start_ts",
        "up_initial_price", "down_initial_price", "up_filled", "down_filled",
        "up_fill_count", "down_fill_count", "up_first_fill_ts", "down_first_fill_ts",
        "up_last_fill_ts", "down_last_fill_ts", "up_average_fill_price",
        "down_average_fill_price", "up_cost", "down_cost", "cost", "payout", "pnl",
        "paired", "classification", "reward_eligible_seconds", "reward_relative_score",
        "quote_observations", "requotes",
    ]
    with path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(records)


def _finalize_mode_stats(stats: dict) -> dict:
    settled = stats["complete_markets"]
    stats["full_pair_rate_of_settled"] = (
        stats["settled_full_pair_markets"] / settled if settled else None
    )
    stats["one_leg_rate_of_settled"] = (
        stats["settled_one_leg_or_partial_markets"] / settled if settled else None
    )
    stats["zero_leg_rate_of_settled"] = (
        stats["settled_zero_leg_markets"] / settled if settled else None
    )
    stats["pnl_per_settled_market"] = stats["settled_pnl"] / settled if settled else None
    return stats


def run_backtest(data_dir: Path, out_dir: Path, config: Config = Config()) -> dict:
    """Stream all recorder rows and write paired maker summary/artifacts."""
    data_dir = Path(data_dir)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    files = sorted(data_dir.glob("*.jsonl"))
    if not files:
        raise FileNotFoundError(f"no *.jsonl files found in {data_dir}")

    states: dict[str, dict[tuple[str, str, int], MarketState]] = {
        mode: {} for mode in MODES
    }
    last_ts: dict[tuple[str, str, int], int] = {}
    records: list[dict] = []
    counters = Counter()
    symbol_tf_keys: set[tuple[str, str]] = set()
    min_ts = None
    max_ts = None

    for path in files:
        with path.open() as handle:
            for raw in handle:
                counters["physical_lines"] += 1
                if not raw.strip():
                    counters["blank_rows"] += 1
                    continue
                try:
                    row = json.loads(raw)
                except (TypeError, ValueError, json.JSONDecodeError):
                    counters["malformed_rows"] += 1
                    continue
                if not isinstance(row, dict):
                    counters["malformed_rows"] += 1
                    continue
                symbol = str(row.get("symbol") or "").lower()
                tf = str(row.get("tf") or "")
                if tf not in WINDOW_SECONDS or not symbol:
                    counters["unsupported_rows"] += 1
                    continue
                if not _finite_number(row.get("ts")) or not _finite_number(row.get("mkt_ts")):
                    counters["malformed_rows"] += 1
                    continue
                ts = int(row["ts"])
                mkt_ts = int(row["mkt_ts"])
                key = (symbol, tf, mkt_ts)
                if key in last_ts and last_ts[key] == ts:
                    counters["duplicate_rows"] += 1
                    continue
                if key in last_ts and ts < last_ts[key]:
                    counters["out_of_order_rows"] += 1
                last_ts[key] = ts
                counters["valid_rows"] += 1
                min_ts = ts if min_ts is None else min(min_ts, ts)
                max_ts = ts if max_ts is None else max(max_ts, ts)
                symbol_tf_keys.add((symbol, tf))
                age = int(row["age"]) if _finite_number(row.get("age")) else ts - mkt_ts
                row["ts"] = ts
                row["mkt_ts"] = mkt_ts
                row["age"] = age
                for mode in MODES:
                    state = states[mode].get(key)
                    if state is None:
                        state = MarketState(symbol, tf, mkt_ts, WINDOW_SECONDS[tf])
                        states[mode][key] = state
                    _process_row(state, row, config, mode)

    mode_records: dict[str, list[dict]] = {mode: [] for mode in MODES}
    for mode in MODES:
        for state in states[mode].values():
            record = _pair_record(state, mode)
            mode_records[mode].append(record)
            records.append(record)

    mode_results = {}
    for mode in MODES:
        stats = _new_mode_stats()
        for record in mode_records[mode]:
            _update_mode_stats(stats, record)
        mode_results[mode] = _finalize_mode_stats(stats)

    coverage = {
        "physical_lines": counters["physical_lines"],
        "valid_rows": counters["valid_rows"],
        "malformed_rows": counters["malformed_rows"],
        "blank_rows": counters["blank_rows"],
        "unsupported_rows": counters["unsupported_rows"],
        "duplicate_rows": counters["duplicate_rows"],
        "out_of_order_rows": counters["out_of_order_rows"],
    }
    summary = {
        "paper_only": True,
        "strategy": "BUY UP 50 + BUY DOWN 50, each one cent below midpoint",
        "data_dir": str(data_dir),
        "files": [path.name for path in files],
        "date_range_utc": [_utc_day(min_ts), _utc_day(max_ts)] if min_ts is not None and max_ts is not None else [None, None],
        "row_range_utc": [min_ts, max_ts],
        "symbols": sorted({symbol for symbol, _tf in symbol_tf_keys}),
        "timeframes": sorted({tf for _symbol, tf in symbol_tf_keys}),
        "config": {
            "order_size": config.order_size,
            "offset_cents": config.offset_cents,
            "tick": config.tick,
            "rewards_min_size": config.min_reward_size,
            "rewards_max_spread_cents": config.max_reward_spread_cents,
            "order_min_size_usdc": config.order_min_size_usdc,
            "fill_modes": list(MODES),
        },
        "official_conditions": {
            "rewards_min_size_shares": 50,
            "rewards_max_spread_cents": 1.5,
            "order_price_tick": 0.01,
            "maker_fee": 0,
            "maker_rebate": "variable/pro-rata; not monetized from recorder data",
            "liquidity_reward_dollars": "variable/pro-rata; not monetized from recorder data",
            "docs": [
                "https://docs.polymarket.com/programs/maker-rebates",
                "https://docs.polymarket.com/programs/liquidity-rewards",
                "https://docs.polymarket.com/market-data/market-details#liquidity-reward-settings",
            ],
        },
        "coverage": coverage,
        "markets": {
            "seen": len(states[MODE_QUOTE_TOUCH]),
            "by_mode": {
                mode: {
                    "seen": result["markets"],
                    "complete": result["complete_markets"],
                    "incomplete": result["incomplete_markets"],
                }
                for mode, result in mode_results.items()
            },
        },
        "modes": mode_results,
        "by_symbol_tf": {
            mode: _aggregate_records(mode_records[mode], ("symbol", "tf"))
            for mode in MODES
        },
        "by_day": {
            mode: _aggregate_records(mode_records[mode], ("mkt_day",))
            for mode in MODES
        },
        "limitations": [
            "paper quote-touch conditions are not actual maker executions",
            "recorder has best bid/ask only, without queue position or trade prints",
            "ask quantity is used as available touch quantity in quote_touch mode",
            "strict_full mode requires the displayed ask quantity to cover the remaining 50-share order",
            "liquidity reward dollars require the pool and all-maker normalization denominator",
            "maker rebate dollars require actual executed maker volume and market fee accounting",
            "50 shares is exactly the current minimum; a partial fill can reduce remaining size below it",
        ],
    }
    summary_path = out_dir / "paired_maker_summary.json"
    pairs_path = out_dir / "paired_maker_pairs.csv"
    summary["artifacts"] = {"summary": str(summary_path), "pairs": str(pairs_path)}
    summary_path.write_text(json.dumps(summary, indent=2, sort_keys=True, allow_nan=False) + "\n")
    _write_pairs(pairs_path, records)
    return summary


def _utc_day(timestamp: int | None) -> str | None:
    if timestamp is None:
        return None
    return datetime.fromtimestamp(int(timestamp), tz=timezone.utc).strftime("%Y-%m-%d")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--out-dir", type=Path, required=True)
    args = parser.parse_args(argv)
    summary = run_backtest(args.data_dir, args.out_dir)
    print(
        "paired maker backtest complete: files=%d rows=%d markets=%d quote_touch_pnl=%s strict_full_pnl=%s"
        % (
            len(summary["files"]),
            summary["coverage"]["valid_rows"],
            summary["markets"]["seen"],
            summary["modes"][MODE_QUOTE_TOUCH]["settled_pnl"],
            summary["modes"][MODE_STRICT_FULL]["settled_pnl"],
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
