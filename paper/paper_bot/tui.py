"""Attachable, strictly read-only terminal dashboard for the paper engine."""
from __future__ import annotations

import asyncio
import json
import time
from collections import defaultdict
from collections.abc import Callable, Mapping
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from rich.table import Table

from .storage import DashboardReadModel, Storage

ZERO = Decimal("0")


def _decimal(value: Any) -> Decimal:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError("dashboard Decimal is invalid") from exc
    if not result.is_finite():
        raise ValueError("dashboard Decimal is not finite")
    return result


def _fmt(value: Any, places: int = 4) -> str:
    if value is None:
        return "—"
    if isinstance(value, bool):
        return "yes" if value else "no"
    try:
        decimal = _decimal(value)
    except ValueError:
        return str(value)
    return f"{decimal:.{places}f}".rstrip("0").rstrip(".")


class PaperDashboard:
    """Render live telemetry and exact accounting through SQLite ``mode=ro``."""

    def __init__(self, db_path: str | Path, refresh: float = 1.0, *,
                 asset: str | None = None, threshold: str | None = None,
                 confirmation: str | None = None, policy: str | None = None,
                 clock_s: Callable[[], float] = time.time) -> None:
        if refresh <= 0:
            raise ValueError("refresh must be positive")
        if asset is not None and asset.lower() not in {"btc", "eth", "sol"}:
            raise ValueError("asset filter must be BTC, ETH, or SOL")
        if threshold is not None:
            normalized_threshold = _decimal(threshold)
            if not ZERO < normalized_threshold < Decimal("1"):
                raise ValueError("threshold filter must be in (0,1)")
            threshold = format(normalized_threshold, "f").rstrip("0").rstrip(".")
        if confirmation is not None and confirmation not in {
            "BOOK_ONLY", "CHAINLINK_DIRECTION", "CHAINLINK_CONFIRMED",
        }:
            raise ValueError("confirmation filter is invalid")
        if policy is not None and policy not in {"HOLD", "IMMEDIATE_REVERSE", "CHAINLINK_REVERSE"}:
            raise ValueError("policy filter is invalid")
        self.db_path, self.refresh = Path(db_path), float(refresh)
        self.asset = None if asset is None else asset.lower()
        self.threshold, self.confirmation, self.policy = threshold, confirmation, policy
        self._clock_s, self._storage = clock_s, None

    def open(self) -> None:
        if self._storage is None:
            storage = Storage(self.db_path, read_only=True)
            storage.initialize()
            self._storage = storage

    def close(self) -> None:
        if self._storage is not None:
            self._storage.close()
            self._storage = None

    def __enter__(self) -> PaperDashboard:
        self.open()
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def data(self) -> DashboardReadModel:
        self.open()
        assert self._storage is not None
        return self._storage.load_dashboard_read_model()

    @staticmethod
    def _panel(title: str, columns: tuple[str, ...], rows: list[tuple[Any, ...]]) -> Panel:
        table = Table(title=title, expand=True)
        for column in columns:
            table.add_column(column, overflow="fold")
        for row in rows or [("—",) * len(columns)]:
            table.add_row(*(str(value) for value in row))
        return Panel(table, border_style="blue")

    def _market_rows(self, snapshot: Mapping[str, Any], entries: tuple[Mapping[str, Any], ...]) -> list[tuple[Any, ...]]:
        now_s = int(self._clock_s())
        thresholds = sorted({_decimal(row["threshold"]) for row in entries})
        rows = []
        for market in snapshot.get("markets", ()):
            if not isinstance(market, Mapping) or (self.asset and market.get("symbol") != self.asset):
                continue
            remaining = int(market["close_ts"]) - now_s
            countdown = "closed" if remaining <= 0 else f"{remaining // 60:02d}:{remaining % 60:02d}"
            books = market.get("books", {})
            parts, crosses = [], []
            for side in ("UP", "DOWN"):
                book = books.get(side, {}) if isinstance(books, Mapping) else {}
                valid = bool(book.get("valid"))
                parts.append(f"{side} {'OK' if valid else 'INVALID'} g{book.get('generation', 0)} "
                             f"{_fmt(book.get('best_bid'), 3)}/{_fmt(book.get('best_ask'), 3)} "
                             f"d={_fmt(book.get('bid_depth'), 2)}/{_fmt(book.get('ask_depth'), 2)}")
                if valid and book.get("best_ask") is not None:
                    ask = _decimal(book["best_ask"])
                    crosses.extend(f"{side}>={_fmt(level, 2)}" for level in thresholds if ask >= level)
            rows.append((str(market.get("symbol", "?")).upper(), market.get("slug", "—"), countdown,
                         " | ".join(parts), ",".join(crosses) or "—"))
        return rows

    def _resolver_rows(self, snapshot: Mapping[str, Any]) -> list[tuple[Any, ...]]:
        now_ms, rows = int(self._clock_s() * 1000), []
        for view in snapshot.get("resolver", ()):
            if not isinstance(view, Mapping) or (self.asset and view.get("symbol") != self.asset):
                continue
            observed = view.get("observation_ts_ms")
            age_ms = None if observed is None else max(0, now_ms - int(observed))
            fresh = age_ms is not None and age_ms <= 10_000
            rows.append((str(view.get("symbol", "?")).upper(), _fmt(view.get("start"), 2),
                         _fmt(view.get("current"), 2), _fmt(view.get("distance"), 2),
                         _fmt(view.get("distance_bps"), 2), view.get("leader") or "—",
                         _fmt(view.get("momentum_5s_bps"), 2),
                         "—" if age_ms is None else f"{age_ms / 1000:.1f}s {'OK' if fresh else 'STALE'}"))
        return rows

    def _visible(self, row: Mapping[str, Any], symbols: Mapping[str, str]) -> bool:
        return not any((self.asset is not None and symbols.get(str(row["market_id"])) != self.asset,
                        self.threshold is not None and row["threshold"] != self.threshold,
                        self.confirmation is not None and row["confirmation"] != self.confirmation,
                        self.policy is not None and row["policy"] != self.policy))

    def _strategy_rows(self, data: DashboardReadModel, symbols: Mapping[str, str]) -> list[tuple[Any, ...]]:
        grouped: dict[tuple[str, str, str], list[Mapping[str, Any]]] = defaultdict(list)
        for row in data.entries:
            if self._visible(row, symbols):
                grouped[(row["threshold"], row["confirmation"], row["policy"])].append(row)
        rows = []
        for lane, entries in sorted(grouped.items()):
            statuses = {name: sum(row["status"] == name for row in entries) for name in ("full", "partial", "zero")}
            settled = [row for row in entries if row["net_pnl"] is not None]
            pnls = [_decimal(row["net_pnl"]) for row in settled]
            net, shares = sum(pnls, ZERO), sum((_decimal(row["filled_shares"]) for row in settled), ZERO)
            equity = peak = drawdown = ZERO
            for value in pnls:
                equity += value
                peak, drawdown = max(peak, equity), max(drawdown, peak - equity)
            reverses = [row["reverse_json"] for row in entries if row["reverse_json"] is not None]
            complete = 0
            for raw in reverses:
                try:
                    complete += json.loads(raw).get("status") == "COMPLETE"
                except (AttributeError, TypeError, json.JSONDecodeError):
                    pass
            rows.append(("/".join(lane), len(entries), f"{statuses['full']}/{statuses['partial']}/{statuses['zero']}",
                         len(settled), f"{sum(v > 0 for v in pnls)}/{sum(v < 0 for v in pnls)}", _fmt(net),
                         "—" if shares == 0 else _fmt(net / shares), _fmt(drawdown), f"{len(reverses)}/{complete}"))
        return rows

    def _position_rows(self, data: DashboardReadModel, symbols: Mapping[str, str]) -> list[tuple[Any, ...]]:
        entries = {(r["market_id"], r["threshold"], r["confirmation"], r["policy"]): r for r in data.entries}
        grouped: dict[tuple[str, str, str, str], list[Mapping[str, Any]]] = defaultdict(list)
        for lot in data.inventory:
            key = (lot["market_id"], lot["threshold"], lot["confirmation"], lot["policy"])
            if key in entries and self._visible(entries[key], symbols):
                grouped[key].append(lot)
        rows = []
        for key, lots in sorted(grouped.items()):
            entry = entries[key]
            filled, quote = _decimal(entry["filled_shares"]), _decimal(entry["quote_amount"])
            requested = entry["requested_quote"] if entry["requested_quote"] is not None else entry["requested_shares"]
            totals = {side: sum((_decimal(lot["shares"]) for lot in lots if lot["side"] == side), ZERO)
                      for side in ("UP", "DOWN")}
            initial = entry["side"]
            old, new = totals[initial], totals["DOWN" if initial == "UP" else "UP"]
            cash = quote + _decimal(entry["fee"])
            if entry["reverse_json"] is not None:
                try:
                    reverse = json.loads(entry["reverse_json"])
                    sell, buy = reverse["sell"], reverse.get("buy")
                    cash += _decimal(sell["fee"]) - _decimal(sell["quote_amount"])
                    if buy is not None:
                        cash += _decimal(buy["quote_amount"]) + _decimal(buy["fee"])
                except (KeyError, TypeError, json.JSONDecodeError, ValueError):
                    cash = Decimal("NaN")
            rows.append((symbols.get(key[0], "?").upper(), "/".join(key[1:]), initial,
                         f"{_fmt(requested)}/{_fmt(filled)}", "—" if filled == 0 else _fmt(quote / filled),
                         f"{_fmt(old)}/{_fmt(new)}", "invalid" if not cash.is_finite() else _fmt(cash),
                         f"UP {_fmt(totals['UP'])} | DOWN {_fmt(totals['DOWN'])}"))
        return rows

    def _health_rows(self, data: DashboardReadModel, snapshot: Mapping[str, Any]) -> list[tuple[Any, ...]]:
        health, markets, resolver = snapshot.get("health", {}), snapshot.get("markets", ()), snapshot.get("resolver", ())
        reconnect = not markets or any(
            not book.get("valid", False) for market in markets for book in market.get("books", {}).values()
        )
        now_ms = int(self._clock_s() * 1000)
        stale = not resolver or any(
            view.get("observation_ts_ms") is None
            or now_ms - int(view["observation_ts_ms"]) < 0
            or now_ms - int(view["observation_ts_ms"]) > 10_000
            for view in resolver
        )
        reasons = [health.get(key) for key in ("storage", "dashboard", "processing", "discovery", "settlement") if health.get(key)]
        free, minimum = health.get("disk_free_bytes"), health.get("disk_min_free_bytes")
        snapshot_ts = snapshot.get("snapshot_ts_ms")
        lag_ms = None if snapshot_ts is None else max(0, now_ms - int(snapshot_ts))
        database_bad = bool(reasons or health.get("pending_storage") or lag_ms is None
                            or lag_ms > max(5_000, int(self.refresh * 3_000)))
        rows = [("reconnect/book", "WARN" if reconnect else "OK", "invalid book awaiting snapshot" if reconnect else "connected books valid"),
                ("stale resolver", "WARN" if stale else "OK", "TWAP observation freshness"),
                ("database", "CRITICAL" if database_bad else "OK",
                 ",".join(reasons) or ("snapshot unavailable" if lag_ms is None else f"snapshot lag={lag_ms}ms")),
                ("disk/journal", "CRITICAL" if not health.get("journal_writable", False) else "OK",
                 f"free={free if free is not None else 'unknown'} min={minimum if minimum is not None else 'unknown'} reason={health.get('journal_reason') or 'none'}")]
        rows.extend((event["kind"], "EVENT", f"at {event['event_ts_ms']}") for event in data.health_events)
        return rows

    def render(self) -> Layout:
        data = self.data()
        snapshot = data.snapshot or {"markets": (), "resolver": (), "health": {}}
        symbols = {str(row["market_id"]): str(row["symbol"]) for row in data.market_metadata}
        symbols.update({str(row.get("market_id")): str(row.get("symbol")) for row in snapshot.get("markets", ())})
        layout = Layout(name="root")
        layout.split_column(
            Layout(name="banner", size=3), Layout(name="top", size=12),
            Layout(name="matrix", size=8), Layout(name="positions", size=8),
            Layout(name="health", size=12),
        )
        layout["banner"].update(Panel("PAPER ONLY — NO ORDERS", style="bold white on dark_green"))
        top = Layout()
        top.split_row(Layout(self._panel("Markets / book health", ("asset", "market", "countdown", "UP/DOWN bid/ask + depth", "cross"), self._market_rows(snapshot, data.entries))),
                      Layout(self._panel("Chainlink TWAP-60 resolver", ("asset", "start", "current", "distance", "bps", "leader", "mom 5s", "age"), self._resolver_rows(snapshot))))
        matrix = self._panel("Strategy matrix — threshold × confirmation × policy filters",
                             ("lane", "signals", "full/partial/zero", "resolved", "W/L", "net PnL", "EV/share", "drawdown", "reverse done"), self._strategy_rows(data, symbols))
        positions = self._panel("Open old/new inventory and projected payouts",
                                ("asset", "lane", "initial", "requested/filled", "VWAP", "old/new", "net cash flow", "payout scenarios"), self._position_rows(data, symbols))
        health = self._panel("Events / reconnect / stale / database / disk", ("source", "state", "detail"), self._health_rows(data, snapshot))
        layout["top"].update(top)
        layout["matrix"].update(matrix)
        layout["positions"].update(positions)
        layout["health"].update(health)
        return layout

    async def run(self, stop_event: asyncio.Event | None = None, *, iterations: int | None = None) -> None:
        stop_event, count = stop_event or asyncio.Event(), 0
        try:
            with Live(self.render(), refresh_per_second=max(1, int(1 / self.refresh)), screen=False) as live:
                while not stop_event.is_set() and (iterations is None or count < iterations):
                    if count:
                        live.update(self.render())
                    count += 1
                    try:
                        await asyncio.wait_for(stop_event.wait(), timeout=self.refresh)
                    except asyncio.TimeoutError:
                        pass
        finally:
            self.close()


def watch(db_path: str | Path, refresh: float = 1.0, **filters: str | None) -> int:
    try:
        asyncio.run(PaperDashboard(db_path, refresh, **filters).run())
    except KeyboardInterrupt:
        return 0
    return 0


__all__ = ["PaperDashboard", "watch"]
