#!/usr/bin/env python3
"""Paper backtest for 5m UP/DOWN entries around the 90-cent region."""

from __future__ import annotations

import argparse
import csv
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
import json
import math
from pathlib import Path
from typing import Iterable


THRESHOLDS = (0.88, 0.89, 0.90, 0.91, 0.92)
SYMBOLS = frozenset({"btc", "eth", "sol"})
TICK = 0.01
EPSILON = 1e-9
DEFAULT_TAKER_FEE_RATE = 0.07
DEFAULT_TAKER_FEE_EXPONENT = 1.0


def _finite_number(value: object) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(float(value))
    )


def _valid_ask(row: dict, side: str) -> float | None:
    field_name = "up_ask" if side == "UP" else "dn_ask"
    value = row.get(field_name)
    if not _finite_number(value):
        return None
    ask = float(value)
    return ask if 0.0 < ask <= 1.0 else None


def _positive_ask_quantity(row: dict, side: str) -> bool:
    field_name = "up_askq" if side == "UP" else "dn_askq"
    value = row.get(field_name)
    return _finite_number(value) and float(value) > 0.0


@dataclass
class EntryState:
    signal: bool = False
    ambiguous: bool = False
    side: str | None = None
    trigger_ts: int | None = None
    trigger_age: int | None = None
    trigger_ask: float | None = None
    taker_entry_price: float | None = None
    maker_limit: float | None = None
    maker_entry_price: float | None = None
    maker_fill_ts: int | None = None
    maker_fill_age: int | None = None

    @property
    def maker_time_to_fill(self) -> int | None:
        if self.trigger_ts is None or self.maker_fill_ts is None:
            return None
        return self.maker_fill_ts - self.trigger_ts


@dataclass
class MarketState:
    symbol: str
    mkt_ts: int
    thresholds: tuple[float, ...] = THRESHOLDS
    entries: dict[float, EntryState] = field(init=False)
    previous_asks: dict[str, float] = field(default_factory=dict)
    previous_book_ts: int | None = None
    strike_cl: float | None = None
    terminal_cl: float | None = None
    final_age: int | None = None
    rows: int = 0

    def __post_init__(self) -> None:
        self.thresholds = tuple(round(float(value), 2) for value in self.thresholds)
        self.entries = {threshold: EntryState() for threshold in self.thresholds}

    def process_row(self, row: dict) -> None:
        ts = int(row["ts"])
        age = int(row.get("age", ts - self.mkt_ts))
        self.rows += 1

        for entry in self.entries.values():
            if not entry.signal or entry.maker_entry_price is not None:
                continue
            if entry.side is None or entry.maker_limit is None or entry.trigger_ts is None:
                continue
            if ts <= entry.trigger_ts:
                continue
            ask = _valid_ask(row, entry.side)
            if (
                ask is not None
                and ask <= entry.maker_limit + EPSILON
                and _positive_ask_quantity(row, entry.side)
            ):
                entry.maker_entry_price = entry.maker_limit
                entry.maker_fill_ts = ts
                entry.maker_fill_age = age

        current = {side: _valid_ask(row, side) for side in ("UP", "DOWN")}
        if current["UP"] is None or current["DOWN"] is None:
            self.previous_asks.clear()
            self.previous_book_ts = None
            return
        if self.previous_book_ts is not None and ts != self.previous_book_ts + 1:
            self.previous_asks.clear()

        for threshold, entry in self.entries.items():
            if entry.signal or entry.ambiguous or not self.previous_asks:
                continue
            crossed = [
                side
                for side in ("UP", "DOWN")
                if self.previous_asks[side] < threshold - EPSILON
                and current[side] >= threshold - EPSILON
            ]
            if len(crossed) == 2:
                entry.ambiguous = True
                continue
            if len(crossed) == 1:
                side = crossed[0]
                entry.signal = True
                entry.side = side
                entry.trigger_ts = ts
                entry.trigger_age = age
                entry.trigger_ask = current[side]
                entry.taker_entry_price = (
                    current[side] if _positive_ask_quantity(row, side) else None
                )
                entry.maker_limit = round(threshold - TICK, 2)

        self.previous_asks = {"UP": current["UP"], "DOWN": current["DOWN"]}
        self.previous_book_ts = ts


def taker_fee(
    price: float,
    rate: float = DEFAULT_TAKER_FEE_RATE,
    exponent: float = DEFAULT_TAKER_FEE_EXPONENT,
) -> float:
    """Return the current Gamma fee-schedule amount for one filled share."""
    price_f = float(price)
    return float(rate) * (price_f * (1.0 - price_f)) ** float(exponent)


def trade_result(
    side: str,
    winner: str,
    price: float,
    variant: str,
    fee_rate: float = DEFAULT_TAKER_FEE_RATE,
    fee_exponent: float = DEFAULT_TAKER_FEE_EXPONENT,
) -> dict:
    """Return one-share settlement accounting for a filled paper trade."""
    if variant not in {"taker", "maker"}:
        raise ValueError(f"unknown variant: {variant}")
    fee = taker_fee(price, fee_rate, fee_exponent) if variant == "taker" else 0.0
    won = side == winner
    gross_pnl = (1.0 if won else 0.0) - float(price)
    return {
        "won": won,
        "gross_pnl": gross_pnl,
        "fee": fee,
        "net_pnl": gross_pnl - fee,
    }


def _wilson_interval(wins: int, total: int, z: float = 1.959963984540054) -> tuple[float | None, float | None]:
    if total <= 0:
        return None, None
    proportion = wins / total
    denominator = 1.0 + z * z / total
    centre = proportion + z * z / (2.0 * total)
    margin = z * math.sqrt(
        proportion * (1.0 - proportion) / total + z * z / (4.0 * total * total)
    )
    return (centre - margin) / denominator, (centre + margin) / denominator


def _percentile(values: Iterable[float], quantile: float) -> float | None:
    ordered = sorted(float(value) for value in values)
    if not ordered:
        return None
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * quantile
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    weight = position - lower
    return ordered[lower] * (1.0 - weight) + ordered[upper] * weight


def _mean(values: Iterable[float]) -> float | None:
    items = [float(value) for value in values]
    return sum(items) / len(items) if items else None


def _threshold_key(threshold: float) -> str:
    return f"{threshold:.2f}"


def _utc_day(timestamp: int | None) -> str | None:
    if timestamp is None:
        return None
    return datetime.fromtimestamp(int(timestamp), tz=timezone.utc).strftime("%Y-%m-%d")


def load_gamma_outcomes(path: Path) -> dict[tuple[str, int], dict]:
    """Strictly derive official winners and fee schedules from Gamma JSONL."""
    outcomes: dict[tuple[str, int], dict] = {}
    with Path(path).open() as handle:
        for line_number, raw in enumerate(handle, 1):
            if not raw.strip():
                continue
            item = json.loads(raw)
            symbol = str(item.get("symbol") or "").lower()
            mkt_ts = int(item["mkt_ts"])
            expected_slug = f"{symbol}-updown-5m-{mkt_ts}"
            if item.get("slug") != expected_slug:
                raise ValueError(f"line {line_number}: unexpected Gamma slug")
            if item.get("closed") is not True or str(
                item.get("umaResolutionStatus") or ""
            ).lower() != "resolved":
                raise ValueError(f"line {line_number}: market is not resolved")
            labels = item.get("outcomes")
            prices = item.get("outcomePrices")
            labels = json.loads(labels) if isinstance(labels, str) else labels
            prices = json.loads(prices) if isinstance(prices, str) else prices
            if not isinstance(labels, list) or not isinstance(prices, list) or len(labels) != len(prices):
                raise ValueError(f"line {line_number}: invalid outcomePrices")
            resolved_indexes = [
                index
                for index, price in enumerate(prices)
                if math.isclose(float(price), 1.0, abs_tol=EPSILON)
            ]
            if len(resolved_indexes) != 1:
                raise ValueError(f"line {line_number}: outcomePrices lack one winner")
            winner = str(labels[resolved_indexes[0]]).upper()
            if winner not in {"UP", "DOWN"}:
                raise ValueError(f"line {line_number}: unexpected outcome label")
            declared_winner = str(item.get("winner") or "").upper()
            if declared_winner and declared_winner != winner:
                raise ValueError(f"line {line_number}: winner disagreement")
            schedule = item.get("feeSchedule")
            if not isinstance(schedule, dict) or not _finite_number(schedule.get("rate")) or not _finite_number(schedule.get("exponent")):
                raise ValueError(f"line {line_number}: missing feeSchedule")
            outcomes[(symbol, mkt_ts)] = {
                "winner": winner,
                "fee_rate": float(schedule["rate"]),
                "fee_exponent": float(schedule["exponent"]),
            }
    return outcomes


def _resolved_market(
    state: MarketState,
    outcome: dict | None,
) -> tuple[bool, str | None, float, float]:
    if (
        state.final_age != 299
        or outcome is None
        or outcome.get("winner") not in {"UP", "DOWN"}
    ):
        return False, None, DEFAULT_TAKER_FEE_RATE, DEFAULT_TAKER_FEE_EXPONENT
    return (
        True,
        outcome["winner"],
        float(outcome["fee_rate"]),
        float(outcome["fee_exponent"]),
    )


def _record(
    state: MarketState,
    threshold: float,
    variant: str,
    outcome: dict | None,
) -> dict:
    entry = state.entries[threshold]
    complete, winner, fee_rate, fee_exponent = _resolved_market(state, outcome)
    if variant == "taker":
        filled = entry.taker_entry_price is not None
        entry_price = entry.taker_entry_price
        entry_ts = entry.trigger_ts if filled else None
        entry_age = entry.trigger_age if filled else None
        time_to_fill = 0 if filled else None
    else:
        filled = entry.maker_entry_price is not None
        entry_price = entry.maker_entry_price
        entry_ts = entry.maker_fill_ts
        entry_age = entry.maker_fill_age
        time_to_fill = entry.maker_time_to_fill
    accounting = (
        trade_result(
            entry.side,
            winner,
            entry_price,
            variant,
            fee_rate=fee_rate,
            fee_exponent=fee_exponent,
        )
        if complete and filled and entry.side is not None and winner is not None and entry_price is not None
        else None
    )
    return {
        "variant": variant,
        "threshold": threshold,
        "threshold_key": _threshold_key(threshold),
        "symbol": state.symbol,
        "tf": "5m",
        "mkt_ts": state.mkt_ts,
        "mkt_day": _utc_day(state.mkt_ts),
        "complete": complete,
        "strike_cl": state.strike_cl,
        "terminal_cl": state.terminal_cl,
        "winner": winner,
        "settlement_source": "official_gamma_outcomePrices",
        "fee_rate": fee_rate,
        "fee_exponent": fee_exponent,
        "signal": entry.signal,
        "ambiguous": entry.ambiguous,
        "side": entry.side,
        "trigger_ts": entry.trigger_ts,
        "trigger_age": entry.trigger_age,
        "trigger_ask": entry.trigger_ask,
        "maker_limit": entry.maker_limit,
        "filled": filled,
        "entry_ts": entry_ts,
        "entry_age": entry_age,
        "entry_price": entry_price,
        "time_to_fill_seconds": time_to_fill,
        "won": accounting["won"] if accounting else None,
        "gross_pnl": accounting["gross_pnl"] if accounting else None,
        "fee": accounting["fee"] if accounting else None,
        "net_pnl": accounting["net_pnl"] if accounting else None,
        "market_rows": state.rows,
    }


def _stats(records: list[dict]) -> dict:
    complete = [record for record in records if record["complete"]]
    signals = [record for record in complete if record["signal"]]
    fills = [record for record in complete if record["filled"]]
    wins = sum(record["won"] is True for record in fills)
    losses = sum(record["won"] is False for record in fills)
    ci_low, ci_high = _wilson_interval(wins, len(fills))
    entry_prices = [record["entry_price"] for record in fills]
    fees = [record["fee"] for record in fills]
    trigger_ages = [record["trigger_age"] for record in signals if record["trigger_age"] is not None]
    fill_times = [
        record["time_to_fill_seconds"]
        for record in fills
        if record["time_to_fill_seconds"] is not None
    ]
    gross_total = sum(record["gross_pnl"] for record in fills)
    net_total = sum(record["net_pnl"] for record in fills)
    return {
        "markets": len(records),
        "complete_markets": len(complete),
        "incomplete_markets": len(records) - len(complete),
        "signals_all_markets": sum(record["signal"] for record in records),
        "signals": len(signals),
        "ambiguous_markets": sum(record["ambiguous"] for record in complete),
        "no_signal_markets": sum(not record["signal"] for record in complete),
        "signal_rate": len(signals) / len(complete) if complete else None,
        "fills": len(fills),
        "no_fills": len(signals) - len(fills),
        "fill_rate": len(fills) / len(signals) if signals else None,
        "wins": wins,
        "losses": losses,
        "win_rate": wins / len(fills) if fills else None,
        "win_rate_ci95": [ci_low, ci_high],
        "average_entry": _mean(entry_prices),
        "minimum_entry": min(entry_prices) if entry_prices else None,
        "maximum_entry": max(entry_prices) if entry_prices else None,
        "average_fee": _mean(fees),
        "break_even_win_rate": _mean(
            record["entry_price"] + record["fee"] for record in fills
        ),
        "gross_pnl_total_1share": gross_total,
        "gross_ev_per_fill": gross_total / len(fills) if fills else None,
        "modeled_net_pnl_total_1share": net_total,
        "modeled_net_ev_per_fill": net_total / len(fills) if fills else None,
        "trigger_age_p50_seconds": _percentile(trigger_ages, 0.50),
        "trigger_age_p90_seconds": _percentile(trigger_ages, 0.90),
        "time_to_fill_p50_seconds": _percentile(fill_times, 0.50),
        "time_to_fill_p90_seconds": _percentile(fill_times, 0.90),
    }


def _grouped_stats(records: list[dict], field_name: str) -> dict:
    grouped: dict[tuple[str, float, str], list[dict]] = defaultdict(list)
    for record in records:
        grouped[(record["variant"], record["threshold"], str(record[field_name]))].append(record)
    result: dict[str, dict[str, list[dict]]] = {
        variant: {_threshold_key(threshold): [] for threshold in THRESHOLDS}
        for variant in ("taker", "maker")
    }
    for (variant, threshold, group_value), rows in sorted(grouped.items()):
        item = {field_name: group_value}
        item.update(_stats(rows))
        result[variant][_threshold_key(threshold)].append(item)
    return result


def _write_trades(path: Path, records: list[dict]) -> None:
    fields = [
        "variant", "threshold", "threshold_key", "symbol", "tf", "mkt_ts", "mkt_day",
        "complete", "strike_cl", "terminal_cl", "winner", "settlement_source", "fee_rate",
        "fee_exponent", "signal", "ambiguous", "side",
        "trigger_ts", "trigger_age", "trigger_ask", "maker_limit", "filled", "entry_ts",
        "entry_age", "entry_price", "time_to_fill_seconds", "won", "gross_pnl", "fee",
        "net_pnl", "market_rows",
    ]
    with path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(records)


def run_backtest(
    data_dir: Path,
    out_dir: Path,
    outcomes: dict[tuple[str, int], dict],
) -> dict:
    """Stream recorder JSONL and write 5m rising-cross entry results."""
    data_dir = Path(data_dir)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    files = sorted(data_dir.glob("*.jsonl"))
    if not files:
        raise FileNotFoundError(f"no *.jsonl files found in {data_dir}")

    states: dict[tuple[str, int], MarketState] = {}
    last_ts: dict[tuple[str, int], int] = {}
    counters = Counter()
    min_ts: int | None = None
    max_ts: int | None = None

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
                if str(row.get("tf") or "") != "5m":
                    counters["non_5m_rows"] += 1
                    continue
                symbol = str(row.get("symbol") or "").lower()
                if not symbol or not _finite_number(row.get("ts")) or not _finite_number(row.get("mkt_ts")):
                    counters["malformed_rows"] += 1
                    continue
                if symbol not in SYMBOLS:
                    counters["unsupported_symbol_rows"] += 1
                    continue
                ts = int(row["ts"])
                mkt_ts = int(row["mkt_ts"])
                key = (symbol, mkt_ts)
                if key in last_ts and ts == last_ts[key]:
                    counters["duplicate_rows"] += 1
                    continue
                if key in last_ts and ts < last_ts[key]:
                    counters["out_of_order_rows"] += 1
                    continue
                last_ts[key] = ts
                age = int(row["age"]) if _finite_number(row.get("age")) else ts - mkt_ts
                if age < 0 or age >= 300:
                    counters["outside_window_rows"] += 1
                    continue
                counters["valid_5m_rows"] += 1
                min_ts = ts if min_ts is None else min(min_ts, ts)
                max_ts = ts if max_ts is None else max(max_ts, ts)
                state = states.get(key)
                if state is None:
                    state = MarketState(symbol, mkt_ts)
                    states[key] = state
                strike = row.get("strike_cl")
                if _finite_number(strike) and float(strike) > 0.0:
                    state.strike_cl = float(strike)
                normalized = dict(row)
                normalized["ts"] = ts
                normalized["age"] = age
                state.process_row(normalized)
                if age == 299:
                    terminal = row.get("cl_twap60")
                    state.terminal_cl = float(terminal) if _finite_number(terminal) else None
                    state.final_age = 299

    records = [
        _record(
            state,
            threshold,
            variant,
            outcome=outcomes.get((state.symbol, state.mkt_ts)),
        )
        for variant in ("taker", "maker")
        for threshold in THRESHOLDS
        for state in states.values()
    ]
    records.sort(key=lambda item: (item["variant"], item["threshold"], item["symbol"], item["mkt_ts"]))
    configs = {
        variant: {
            _threshold_key(threshold): _stats(
                [
                    record
                    for record in records
                    if record["variant"] == variant and record["threshold"] == threshold
                ]
            )
            for threshold in THRESHOLDS
        }
        for variant in ("taker", "maker")
    }
    complete_markets = sum(
        _resolved_market(state, outcomes.get((state.symbol, state.mkt_ts)))[0]
        for state in states.values()
    )
    summary_path = out_dir / "summary.json"
    trades_path = out_dir / "trades.csv"
    summary = {
        "paper_only": True,
        "strategy": "first 5m UP/DOWN rising-cross at 88-92 cents",
        "data_dir": str(data_dir),
        "files": [path.name for path in files],
        "date_range_utc": [_utc_day(min_ts), _utc_day(max_ts)],
        "row_range_utc": [min_ts, max_ts],
        "symbols": sorted({state.symbol for state in states.values()}),
        "timeframe": "5m",
        "settlement_source": "official_gamma_outcomePrices",
        "thresholds": list(THRESHOLDS),
        "variants": {
            "taker": "buy at current best ask on first rising cross",
            "maker": "after rising cross, fixed post-only bid one tick below threshold; later quote-touch fill",
        },
        "fee_model": {
            "taker": "feeSchedule.rate * (price * (1-price)) ** feeSchedule.exponent",
            "default_rate": DEFAULT_TAKER_FEE_RATE,
            "default_exponent": DEFAULT_TAKER_FEE_EXPONENT,
            "maker": 0,
        },
        "coverage": {
            "physical_lines": counters["physical_lines"],
            "valid_5m_rows": counters["valid_5m_rows"],
            "non_5m_rows": counters["non_5m_rows"],
            "malformed_rows": counters["malformed_rows"],
            "blank_rows": counters["blank_rows"],
            "duplicate_rows": counters["duplicate_rows"],
            "out_of_order_rows": counters["out_of_order_rows"],
            "outside_window_rows": counters["outside_window_rows"],
            "unsupported_symbol_rows": counters["unsupported_symbol_rows"],
            "official_outcomes_loaded": len(outcomes),
            "official_outcomes_matched": sum(
                (state.symbol, state.mkt_ts) in outcomes for state in states.values()
            ),
        },
        "markets": {
            "seen": len(states),
            "complete": complete_markets,
            "incomplete": len(states) - complete_markets,
        },
        "configs": configs,
        "by_symbol": _grouped_stats(records, "symbol"),
        "by_day": _grouped_stats(records, "mkt_day"),
        "limitations": [
            "one-second snapshots cannot resolve sub-second crossing or slippage",
            "taker fills assume execution at recorded best ask with positive displayed quantity",
            "maker fills are optimistic later quote-touch proxies without queue position or trade prints",
            "fees use each resolved market's Gamma feeSchedule but are not exchange ledger entries",
        ],
        "artifacts": {"summary": summary_path.name, "trades": trades_path.name},
    }
    summary_path.write_text(json.dumps(summary, indent=2, sort_keys=True, allow_nan=False) + "\n")
    _write_trades(trades_path, records)
    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", required=True, type=Path)
    parser.add_argument("--out-dir", required=True, type=Path)
    parser.add_argument("--outcomes-file", required=True, type=Path)
    args = parser.parse_args(argv)
    outcomes = load_gamma_outcomes(args.outcomes_file)
    summary = run_backtest(args.data_dir, args.out_dir, outcomes=outcomes)
    taker = summary["configs"]["taker"]["0.90"]
    maker = summary["configs"]["maker"]["0.90"]
    print(
        "90-cent 5m backtest complete: rows=%d markets=%d complete=%d "
        "taker_fills=%d taker_wr=%s maker_fills=%d maker_wr=%s"
        % (
            summary["coverage"]["valid_5m_rows"],
            summary["markets"]["seen"],
            summary["markets"]["complete"],
            taker["fills"],
            taker["win_rate"],
            maker["fills"],
            maker["win_rate"],
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
