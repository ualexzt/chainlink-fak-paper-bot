#!/usr/bin/env python3
"""Streaming paper-taker backtest for the live recorder JSONL data."""

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
MARGINS = (0.03, 0.04, 0.06)
FLOORS = (3.0, 8.0, 15.0)


@dataclass(frozen=True)
class Config:
    margin: float
    floor: float


CONFIGS = tuple(Config(margin, floor) for margin in MARGINS for floor in FLOORS)


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
    rows: int = 0
    last_ts: int | None = None
    duplicate_rows: int = 0
    fair_rows: int = 0
    candidates: dict[Config, dict] = field(default_factory=dict)


def _finite_number(value: object) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(float(value))
    )


def fee(price: float) -> float:
    """Existing research taker-fee model."""
    return 0.10 * min(price, 1.0 - price)


def pnl_for(price: float, won: bool) -> float:
    """Net PnL for one share bought at ``price`` and held to settlement."""
    return (1.0 - price - fee(price)) if won else (-price - fee(price))


def row_signal(
    row: dict, margin: float, floor: float
) -> tuple[str, float, float, float] | None:
    """Return a qualifying leader ask or ``None`` for a non-signal row."""
    age = row.get("age")
    leader = str(row.get("leader_cl") or "").upper()
    fair = row.get("fair_leader_lut")
    dist = row.get("dist_cl_bps")
    if not _finite_number(age) or float(age) < 59:
        return None
    if leader not in {"UP", "DOWN"}:
        return None
    if not _finite_number(fair) or not _finite_number(dist):
        return None
    fair_f = float(fair)
    dist_f = float(dist)
    if abs(dist_f) < floor:
        return None
    ask_value = row.get("up_ask" if leader == "UP" else "dn_ask")
    if not _finite_number(ask_value):
        return None
    ask = float(ask_value)
    if not 0.0 < ask < 1.0:
        return None
    if ask > fair_f - margin:
        return None
    return leader, ask, fair_f, dist_f


def settle_market(state: MarketState, final_cl: float | None) -> str | None:
    """Return the inferred winner using the recorder's Chainlink rule."""
    if not _finite_number(state.strike_cl) or float(state.strike_cl) <= 0:
        return None
    if not _finite_number(final_cl):
        return None
    return "UP" if float(final_cl) >= float(state.strike_cl) else "DOWN"


def iter_jsonl_files(data_dir: Path) -> Iterator[Path]:
    """Yield input files in deterministic date/name order."""
    yield from sorted(data_dir.glob("*.jsonl"))


def _new_config_stats(config: Config) -> dict:
    return {
        "margin": config.margin,
        "floor": config.floor,
        "signal_candidates": 0,
        "signal_markets": 0,
        "incomplete_signal_markets": 0,
        "fills": 0,
        "wins": 0,
        "win_rate": None,
        "win_rate_ci95": [None, None],
        "entry_sum": 0.0,
        "fair_sum": 0.0,
        "age_sum": 0.0,
        "average_entry": None,
        "average_fair": None,
        "average_age": None,
        "ev_per_share": None,
        "pnl_total": 0.0,
        "complete_markets": 0,
        "incomplete_markets": 0,
    }


def _wilson_interval(wins: int, total: int) -> tuple[float | None, float | None]:
    if total <= 0:
        return None, None
    z = 1.959963984540054
    p = wins / total
    denominator = 1.0 + z * z / total
    centre = (p + z * z / (2.0 * total)) / denominator
    spread = z * math.sqrt(p * (1.0 - p) / total + z * z / (4.0 * total * total)) / denominator
    return max(0.0, centre - spread), min(1.0, centre + spread)


def _utc_day(timestamp: int) -> str:
    return datetime.fromtimestamp(timestamp, tz=timezone.utc).strftime("%Y-%m-%d")


def _record_fill(
    config: Config,
    state: MarketState,
    candidate: dict,
    winner: str,
    final_cl: float,
    fills: list[dict],
    stats: dict,
    group_fills: dict[tuple[str, str], list[dict]],
    day_fills: dict[str, list[dict]],
) -> None:
    won = candidate["leader"] == winner
    price = candidate["ask"]
    trade_fee = fee(price)
    pnl = pnl_for(price, won)
    fill = {
        "margin": config.margin,
        "floor": config.floor,
        "symbol": state.symbol,
        "tf": state.tf,
        "mkt_ts": state.mkt_ts,
        "entry_ts": candidate["entry_ts"],
        "entry_day": _utc_day(candidate["entry_ts"]),
        "entry_age": candidate["entry_age"],
        "leader": candidate["leader"],
        "ask": price,
        "fair": candidate["fair"],
        "dist_bps": candidate["dist_bps"],
        "strike_cl": state.strike_cl,
        "final_cl": final_cl,
        "winner": winner,
        "won": won,
        "fee": trade_fee,
        "pnl": pnl,
    }
    fills.append(fill)
    stats["fills"] += 1
    stats["wins"] += int(won)
    stats["pnl_total"] += pnl
    stats["entry_sum"] += price
    stats["fair_sum"] += candidate["fair"]
    stats["age_sum"] += candidate["entry_age"]
    group_fills[(state.symbol, state.tf)].append(fill)
    day_fills[fill["entry_day"]].append(fill)


def _aggregate_fills(
    fills: Iterable[dict], key_fields: tuple[str, ...], include_config: bool = True
) -> list[dict]:
    grouped: dict[tuple, list[dict]] = defaultdict(list)
    for fill in fills:
        key = tuple(fill[field] for field in key_fields)
        if include_config:
            key += (fill["margin"], fill["floor"])
        grouped[key].append(fill)
    result = []
    for key, rows in sorted(grouped.items(), key=lambda item: tuple(str(x) for x in item[0])):
        values = dict(zip(key_fields, key[: len(key_fields)]))
        config_offset = len(key_fields)
        if include_config:
            values["margin"] = key[config_offset]
            values["floor"] = key[config_offset + 1]
        n = len(rows)
        wins = sum(int(row["won"]) for row in rows)
        values.update(
            {
                "fills": n,
                "wins": wins,
                "win_rate": wins / n if n else None,
                "pnl_total": sum(row["pnl"] for row in rows),
                "ev_per_share": sum(row["pnl"] for row in rows) / n if n else None,
                "average_entry": sum(row["ask"] for row in rows) / n if n else None,
                "average_fair": sum(row["fair"] for row in rows) / n if n else None,
            }
        )
        result.append(values)
    return result


def _finalize_config_stats(
    stats: dict,
    total_markets: int,
    complete_markets: int,
    incomplete_markets: int,
) -> dict:
    fills = stats["fills"]
    stats["win_rate"] = stats["wins"] / fills if fills else None
    stats["win_rate_ci95"] = list(_wilson_interval(stats["wins"], fills))
    stats["average_entry"] = stats["entry_sum"] / fills if fills else None
    stats["average_fair"] = stats["fair_sum"] / fills if fills else None
    stats["average_age"] = stats["age_sum"] / fills if fills else None
    stats["ev_per_share"] = stats["pnl_total"] / fills if fills else None
    stats["markets_seen"] = total_markets
    stats["complete_markets"] = complete_markets
    stats["incomplete_markets"] = incomplete_markets
    stats.pop("entry_sum", None)
    stats.pop("fair_sum", None)
    stats.pop("age_sum", None)
    return stats


def _write_fills(path: Path, fills: list[dict]) -> None:
    fields = [
        "margin",
        "floor",
        "symbol",
        "tf",
        "mkt_ts",
        "entry_ts",
        "entry_day",
        "entry_age",
        "leader",
        "ask",
        "fair",
        "dist_bps",
        "strike_cl",
        "final_cl",
        "winner",
        "won",
        "fee",
        "pnl",
    ]
    with path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(fills)


def run_backtest(data_dir: Path, out_dir: Path) -> dict:
    """Run the nine-configuration streaming backtest and write compact artifacts."""
    data_dir = Path(data_dir)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    files = list(iter_jsonl_files(data_dir))
    if not files:
        raise FileNotFoundError(f"no *.jsonl files found in {data_dir}")

    states: dict[tuple[str, str, int], MarketState] = {}
    config_stats = {config: _new_config_stats(config) for config in CONFIGS}
    fills: list[dict] = []
    group_fills: dict[tuple[str, str], list[dict]] = defaultdict(list)
    day_fills: dict[str, list[dict]] = defaultdict(list)
    counters = Counter()
    field_counters = Counter()
    tf_markets: Counter[str] = Counter()
    tf_complete: Counter[str] = Counter()
    tf_fair: Counter[str] = Counter()
    symbol_tf_markets: Counter[tuple[str, str]] = Counter()
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
                symbol = str(row.get("symbol") or "").lower()
                tf = str(row.get("tf") or "")
                if tf not in WINDOW_SECONDS or not symbol:
                    counters["unsupported_rows"] += 1
                    continue
                if not _finite_number(row.get("mkt_ts")) or not _finite_number(row.get("ts")):
                    counters["malformed_rows"] += 1
                    continue
                mkt_ts = int(row["mkt_ts"])
                ts = int(row["ts"])
                age_value = row.get("age")
                if _finite_number(age_value):
                    age = int(age_value)
                else:
                    age = ts - mkt_ts
                    counters["missing_age_rows"] += 1
                counters["valid_rows"] += 1
                min_ts = ts if min_ts is None else min(min_ts, ts)
                max_ts = ts if max_ts is None else max(max_ts, ts)
                key = (symbol, tf, mkt_ts)
                state = states.get(key)
                if state is None:
                    state = MarketState(symbol, tf, mkt_ts, WINDOW_SECONDS[tf])
                    states[key] = state
                    tf_markets[tf] += 1
                    symbol_tf_markets[(symbol, tf)] += 1
                if state.last_ts == ts:
                    state.duplicate_rows += 1
                    counters["duplicate_rows"] += 1
                    continue
                if state.last_ts is not None and ts < state.last_ts:
                    counters["out_of_order_rows"] += 1
                state.last_ts = ts
                state.rows += 1

                for field_name in ("up_ask", "dn_ask"):
                    value = row.get(field_name)
                    if value is None:
                        field_counters[f"{field_name}_missing"] += 1
                    elif not _finite_number(value):
                        field_counters[f"{field_name}_invalid"] += 1
                    else:
                        field_counters[f"{field_name}_present"] += 1
                        if float(value) == 0.0:
                            field_counters[f"{field_name}_zero"] += 1
                fair = row.get("fair_leader_lut")
                if _finite_number(fair):
                    field_counters["fair_present"] += 1
                    state.fair_rows += 1
                else:
                    field_counters["fair_missing"] += 1
                if _finite_number(row.get("strike_cl")) and float(row["strike_cl"]) > 0:
                    if state.strike_cl is None and age >= 59:
                        state.strike_cl = float(row["strike_cl"])
                if age >= state.step:
                    counters["post_expiry_rows"] += 1

                if 59 <= age < state.step:
                    for config in CONFIGS:
                        signal = row_signal(row, config.margin, config.floor)
                        if signal is None:
                            continue
                        config_stats[config]["signal_candidates"] += 1
                        if config in state.candidates:
                            continue
                        leader, ask, fair_value, dist_bps = signal
                        state.candidates[config] = {
                            "entry_ts": ts,
                            "entry_age": age,
                            "leader": leader,
                            "ask": ask,
                            "fair": fair_value,
                            "dist_bps": dist_bps,
                        }
                        config_stats[config]["signal_markets"] += 1

                is_final = age == state.step - 1
                if is_final:
                    state.final_age = age
                    final_value = row.get("cl_twap60")
                    state.final_cl = float(final_value) if _finite_number(final_value) else None
                    winner = settle_market(state, state.final_cl)
                    if winner is not None and not state.settled:
                        state.settled = True
                        tf_complete[tf] += 1
                        for config, candidate in state.candidates.items():
                            _record_fill(
                                config,
                                state,
                                candidate,
                                winner,
                                state.final_cl,
                                fills,
                                config_stats[config],
                                group_fills,
                                day_fills,
                            )

    for state in states.values():
        if not state.settled:
            counters["incomplete_markets"] += 1
            if state.candidates:
                for config in state.candidates:
                    config_stats[config]["incomplete_signal_markets"] += 1
        else:
            counters["complete_markets"] += 1
        if state.fair_rows:
            tf_fair[state.tf] += 1

    # A market can only be settled once, so this is equivalent to the per-tf counter
    # while keeping the summary robust if a future input format changes.
    complete_by_tf = Counter()
    incomplete_by_tf = Counter()
    fair_by_tf = Counter()
    for state in states.values():
        (complete_by_tf if state.settled else incomplete_by_tf)[state.tf] += 1
        if state.fair_rows:
            fair_by_tf[state.tf] += 1

    config_results = [
        _finalize_config_stats(
            config_stats[config],
            len(states),
            counters["complete_markets"],
            counters["incomplete_markets"],
        )
        for config in CONFIGS
    ]
    for config_result in config_results:
        config_result["timeframe_scope"] = "15m only (current LUT)"

    row_count = counters["valid_rows"]
    coverage = {
        "rows": row_count,
        "malformed_rows": counters["malformed_rows"],
        "blank_rows": counters["blank_rows"],
        "unsupported_rows": counters["unsupported_rows"],
        "duplicate_rows": counters["duplicate_rows"],
        "out_of_order_rows": counters["out_of_order_rows"],
        "post_expiry_rows": counters["post_expiry_rows"],
        "up_ask_present": field_counters["up_ask_present"],
        "up_ask_missing": field_counters["up_ask_missing"],
        "up_ask_invalid": field_counters["up_ask_invalid"],
        "up_ask_zero": field_counters["up_ask_zero"],
        "dn_ask_present": field_counters["dn_ask_present"],
        "dn_ask_missing": field_counters["dn_ask_missing"],
        "dn_ask_invalid": field_counters["dn_ask_invalid"],
        "dn_ask_zero": field_counters["dn_ask_zero"],
        "fair_present": field_counters["fair_present"],
        "fair_missing": field_counters["fair_missing"],
        "missing_age_rows": counters["missing_age_rows"],
    }
    for key, value in list(coverage.items()):
        if isinstance(value, float) and not math.isfinite(value):
            coverage[key] = None
    summary = {
        "paper_only": True,
        "settlement": "inferred_chainlink_terminal_twap",
        "data_dir": str(data_dir),
        "files": [path.name for path in files],
        "date_range_utc": [_utc_day(min_ts), _utc_day(max_ts)] if min_ts is not None and max_ts is not None else [None, None],
        "row_range_utc": [min_ts, max_ts],
        "symbols": sorted({symbol for symbol, _tf in symbol_tf_markets}),
        "timeframes": sorted(tf_markets),
        "coverage": coverage,
        "markets": {
            "seen": len(states),
            "complete": counters["complete_markets"],
            "incomplete": counters["incomplete_markets"],
            "by_timeframe": {
                tf: {
                    "seen": tf_markets[tf],
                    "complete": complete_by_tf[tf],
                    "incomplete": incomplete_by_tf[tf],
                    "fair_available": fair_by_tf[tf],
                }
                for tf in sorted(tf_markets)
            },
        },
        "configs": config_results,
        "by_symbol_tf": _aggregate_fills(fills, ("symbol", "tf")),
        "by_day": _aggregate_fills(fills, ("entry_day",)),
        "fills": len(fills),
        "limitations": [
            "paper fills use best ask and do not prove execution",
            "only best quote is recorded; no full depth or queue position",
            "settlement is inferred from the terminal recorded Chainlink TWAP",
            "seven calendar days are a pilot sample, not proof of durable profitability",
            "the current fair-value LUT is available for 15m only; 5m is audited but not scored",
        ],
    }
    summary_path = out_dir / "live_chainlink_backtest_summary.json"
    fills_path = out_dir / "live_chainlink_backtest_fills.csv"
    summary["artifacts"] = {
        "summary": str(summary_path),
        "fills": str(fills_path),
    }
    summary_path.write_text(json.dumps(summary, indent=2, sort_keys=True, allow_nan=False) + "\n")
    _write_fills(fills_path, fills)
    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--out-dir", type=Path, required=True)
    args = parser.parse_args(argv)
    summary = run_backtest(args.data_dir, args.out_dir)
    print(
        "backtest complete: files=%d rows=%d markets=%d fills=%d summary=%s"
        % (
            len(summary["files"]),
            summary["coverage"]["rows"],
            summary["markets"]["seen"],
            summary["fills"],
            summary["artifacts"]["summary"],
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
