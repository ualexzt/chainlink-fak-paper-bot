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
from rich import box
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from .storage import DashboardReadModel, Storage

ZERO = Decimal("0")
BASE_CONFIRMATIONS = {"BOOK_ONLY", "CHAINLINK_DIRECTION", "CHAINLINK_CONFIRMED"}
MC_MODEL = "twap-first-valid-window-bootstrap-v3"


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

    def __init__(self, db_path: str | Path, refresh: float = 2.0, *,
                 asset: str | None = None, threshold: str | None = None,
                 confirmation: str | None = None, policy: str | None = None,
                 view: str = "overview",
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
            "MC_BOOTSTRAP_90_V1", "MC_BOOTSTRAP_60_V1", "MC_BOOTSTRAP_30_V1",
            "MC_BOOTSTRAP_90_V2", "MC_BOOTSTRAP_60_V2", "MC_BOOTSTRAP_30_V2",
            "MC_BOOTSTRAP_90_V3", "MC_BOOTSTRAP_60_V3", "MC_BOOTSTRAP_30_V3",
        }:
            raise ValueError("confirmation filter is invalid")
        if policy is not None and policy not in {"HOLD", "IMMEDIATE_REVERSE", "CHAINLINK_REVERSE"}:
            raise ValueError("policy filter is invalid")
        if view not in {"overview", "performance", "activity"}:
            raise ValueError("view must be overview, performance, or activity")
        self.db_path, self.refresh = Path(db_path), float(refresh)
        self.asset = None if asset is None else asset.lower()
        self.threshold, self.confirmation, self.policy = threshold, confirmation, policy
        self.view = view
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
    def _panel(title: str, columns: tuple[str, ...], rows: list[tuple[Any, ...]], *,
               subtitle: str | None = None) -> Panel:
        table = Table(expand=True, box=None, padding=(0, 1), header_style="bold bright_cyan")
        for index, column in enumerate(columns):
            table.add_column(column, overflow="ellipsis", no_wrap=index != len(columns) - 1)
        for row in rows or [("—",) * len(columns)]:
            table.add_row(*(value if isinstance(value, Text) else str(value) for value in row))
        return Panel(
            table, title=f"[bold]{title}[/bold]", title_align="left",
            subtitle=subtitle, subtitle_align="right",
            border_style="bright_black", box=box.ROUNDED, padding=(0, 1),
        )

    def _current_markets(self, snapshot: Mapping[str, Any]) -> list[Mapping[str, Any]]:
        """Return only the nearest open round per asset for a calm overview."""
        now_s = int(self._clock_s())
        nearest: dict[str, Mapping[str, Any]] = {}
        for market in snapshot.get("markets", ()):
            if not isinstance(market, Mapping):
                continue
            symbol = str(market.get("symbol", "")).lower()
            if not symbol or (self.asset and symbol != self.asset):
                continue
            if bool(market.get("inactive")) or int(market.get("close_ts", 0)) <= now_s:
                continue
            previous = nearest.get(symbol)
            if previous is None or int(market["close_ts"]) < int(previous["close_ts"]):
                nearest[symbol] = market
        return sorted(nearest.values(), key=lambda row: str(row.get("symbol")))

    @staticmethod
    def _status(label: str, state: str) -> Text:
        styles = {
            "OK": "bold green", "LIVE": "bold green", "CONFIRMED": "bold green",
            "WAIT": "dim", "BASE ONLY": "yellow", "MC ONLY": "cyan",
            "CONFLICT": "bold red", "STALE": "yellow", "WARN": "yellow",
            "BLOCKED": "bold red", "CRITICAL": "bold red", "EVENT": "dim cyan",
        }
        return Text(label, style=styles.get(state, "white"))

    def _pulse_rows(self, snapshot: Mapping[str, Any]) -> list[tuple[Any, ...]]:
        now_s, now_ms = int(self._clock_s()), int(self._clock_s() * 1000)
        resolvers = {
            str(row.get("symbol", "")).lower(): row for row in snapshot.get("resolver", ())
            if isinstance(row, Mapping)
        }
        rows: list[tuple[Any, ...]] = []
        for market in self._current_markets(snapshot):
            symbol = str(market.get("symbol", "?"))
            remaining = max(0, int(market["close_ts"]) - now_s)
            books = market.get("books", {})
            up = books.get("UP", {}) if isinstance(books, Mapping) else {}
            down = books.get("DOWN", {}) if isinstance(books, Mapping) else {}
            resolver = resolvers.get(symbol, {})
            observed = resolver.get("observation_ts_ms")
            age = None if observed is None else max(0, now_ms - int(observed)) / 1000
            books_ok = bool(up.get("valid")) and bool(down.get("valid"))
            resolver_ok = age is not None and age <= 10
            feed_state = "LIVE" if books_ok and resolver_ok else "BLOCKED"
            rows.append((
                symbol.upper(), f"{remaining // 60:02d}:{remaining % 60:02d}",
                f"{_fmt(up.get('best_bid'), 3)} / {_fmt(up.get('best_ask'), 3)}",
                f"{_fmt(down.get('best_bid'), 3)} / {_fmt(down.get('best_ask'), 3)}",
                resolver.get("leader") or "—", _fmt(resolver.get("distance_bps"), 1),
                "—" if age is None else f"{age:.1f}s", self._status(feed_state, feed_state),
            ))
        return rows

    def _signal_rows(self, data: DashboardReadModel, snapshot: Mapping[str, Any]) -> list[tuple[Any, ...]]:
        """Derive observational consensus from persisted base lanes and MC forecasts."""
        resolver_by_symbol = {
            str(row.get("symbol", "")).lower(): row for row in snapshot.get("resolver", ())
            if isinstance(row, Mapping)
        }
        rows: list[tuple[Any, ...]] = []
        for market in self._current_markets(snapshot):
            market_id, symbol = str(market["market_id"]), str(market["symbol"]).lower()
            base_votes: dict[tuple[str, str, str], str] = {}
            for entry in data.entries:
                if str(entry["market_id"]) != market_id or entry["confirmation"] not in BASE_CONFIRMATIONS:
                    continue
                if self.threshold is not None and entry["threshold"] != self.threshold:
                    continue
                if self.confirmation is not None and entry["confirmation"] != self.confirmation:
                    continue
                if self.policy is not None and entry["policy"] != self.policy:
                    continue
                key = (str(entry["threshold"]), str(entry["confirmation"]), str(entry["side"]))
                base_votes[key] = str(entry["side"])
            base_counts = {side: sum(value == side for value in base_votes.values()) for side in ("UP", "DOWN")}
            base_side = max(base_counts, key=base_counts.get) if max(base_counts.values(), default=0) else None
            if base_counts["UP"] == base_counts["DOWN"]:
                base_side = None

            observed, mc_enter = [], []
            for forecast in data.monte_carlo_forecasts:
                if str(forecast["market_id"]) != market_id or forecast["model_version"] != MC_MODEL:
                    continue
                try:
                    payload = json.loads(forecast["payload_json"])
                except (TypeError, json.JSONDecodeError):
                    payload = {}
                observed.append((forecast, payload))
                if forecast["decision"] == "ENTER" and payload.get("side") in {"UP", "DOWN"}:
                    mc_enter.append((forecast, payload))
            mc_counts = {side: sum(payload.get("side") == side for _, payload in mc_enter) for side in ("UP", "DOWN")}
            mc_side = max(mc_counts, key=mc_counts.get) if max(mc_counts.values(), default=0) else None
            if mc_counts["UP"] == mc_counts["DOWN"]:
                mc_side = None

            base_split = base_counts["UP"] > 0 and base_counts["DOWN"] > 0
            mc_split = mc_counts["UP"] > 0 and mc_counts["DOWN"] > 0
            if base_split or mc_split:
                state, side = "CONFLICT", "—"
                detail = (
                    f"mixed evidence · base UP/DOWN {base_counts['UP']}/{base_counts['DOWN']} "
                    f"· MC {mc_counts['UP']}/{mc_counts['DOWN']}"
                )
            elif base_side and mc_side and base_side == mc_side:
                state, side, detail = "CONFIRMED", base_side, "base lanes + Monte Carlo agree"
            elif base_side and mc_side:
                state, side, detail = "CONFLICT", "—", f"base {base_side} vs MC {mc_side}"
            elif mc_side:
                state, side, detail = "MC ONLY", mc_side, "no base trigger yet"
            elif base_side:
                reasons = list(dict.fromkeys(str(item[0]["reason"]) for item in observed))
                state, side = "BASE ONLY", base_side
                detail = "MC: " + (", ".join(reasons[:2]) if reasons else "awaiting first window")
            else:
                reasons = list(dict.fromkeys(str(item[0]["reason"]) for item in observed))
                state, side = "WAIT", "—"
                detail = ", ".join(reasons[:2]) if reasons else "awaiting base and MC observations"

            probabilities = [_decimal(payload["probability"]) for _, payload in mc_enter if payload.get("probability") is not None]
            edges = [_decimal(payload["edge"]) for _, payload in mc_enter if payload.get("edge") is not None]
            leader = resolver_by_symbol.get(symbol, {}).get("leader") or "—"
            signal = self._status(f"{state} {side}".rstrip(), state)
            base_text = (
                f"UP {base_counts['UP']} / DOWN {base_counts['DOWN']}" if base_split
                else "—" if not base_side else f"{base_counts[base_side]} {base_side}"
            )
            mc_text = (
                f"UP {mc_counts['UP']} / DOWN {mc_counts['DOWN']}" if mc_split
                else "—" if not mc_side else f"{mc_counts[mc_side]}/{len(observed)} {mc_side}"
            )
            rows.append((
                symbol.upper(), signal, base_text, mc_text,
                "—" if not probabilities else _fmt(sum(probabilities, ZERO) / len(probabilities), 3),
                "—" if not edges else _fmt(max(edges), 3), leader, detail,
            ))
        return rows

    def _visible(self, row: Mapping[str, Any], symbols: Mapping[str, str]) -> bool:
        return not any((self.asset is not None and symbols.get(str(row["market_id"])) != self.asset,
                        self.threshold is not None and row["threshold"] != self.threshold,
                        self.confirmation is not None and row["confirmation"] != self.confirmation,
                        self.policy is not None and row["policy"] != self.policy))

    def _monte_carlo_rows(
        self, data: DashboardReadModel, symbols: Mapping[str, str]
    ) -> list[tuple[Any, ...]]:
        rows = []
        for row in data.monte_carlo_forecasts:
            symbol = symbols.get(str(row["market_id"]), "?")
            if self.asset is not None and symbol != self.asset:
                continue
            try:
                payload = json.loads(row["payload_json"])
            except (TypeError, json.JSONDecodeError):
                payload = {}
            outcome = "NO TRADE" if row["decision"] != "ENTER" else (
                "—" if row["winner"] is None else
                "WIN" if payload.get("side") == row["winner"] else "LOSS"
            )
            rows.append((
                symbol.upper(), f"{row['horizon_seconds']}s", payload.get("side") or "—",
                _fmt(payload.get("best_ask"), 3), _fmt(payload.get("probability"), 3),
                _fmt(payload.get("break_even_probability"), 3), _fmt(payload.get("edge"), 3),
                row["decision"], row["reason"], outcome,
            ))
        return rows

    def _position_summary_rows(
        self, data: DashboardReadModel, symbols: Mapping[str, str], snapshot: Mapping[str, Any]
    ) -> list[tuple[Any, ...]]:
        now_s = int(self._clock_s())
        market_views = {str(row.get("market_id")): row for row in snapshot.get("markets", ())}
        grouped: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
        for lot in data.inventory:
            symbol = symbols.get(str(lot["market_id"]), "?")
            if self.asset is None or symbol == self.asset:
                grouped[str(lot["market_id"])].append(lot)
        rows = []
        for market_id, lots in grouped.items():
            market = market_views.get(market_id, {})
            remaining = int(market.get("close_ts", now_s)) - now_s
            countdown = "settling" if remaining <= 0 else f"{remaining // 60:02d}:{remaining % 60:02d}"
            totals = {
                side: sum((_decimal(lot["shares"]) for lot in lots if lot["side"] == side), ZERO)
                for side in ("UP", "DOWN")
            }
            lane_count = len({(lot["threshold"], lot["confirmation"], lot["policy"]) for lot in lots})
            rows.append((
                symbols.get(market_id, "?").upper(), market_id, countdown, lane_count,
                _fmt(totals["UP"], 2), _fmt(totals["DOWN"], 2),
                self._status("OPEN PAPER", "LIVE"),
            ))
        return sorted(rows, key=lambda row: (row[2] == "settling", row[0]))[:4]

    def _scoreboard_rows(self, data: DashboardReadModel, symbols: Mapping[str, str]) -> list[tuple[Any, ...]]:
        grouped: dict[tuple[str, str], list[Mapping[str, Any]]] = defaultdict(list)
        for row in data.entries:
            if self._visible(row, symbols):
                grouped[(str(row["confirmation"]), str(row["policy"]))].append(row)
        rows = []
        for (confirmation, policy), entries in sorted(grouped.items()):
            filled = [row for row in entries if row["status"] in {"full", "partial"}]
            settled = [row for row in entries if row["net_pnl"] is not None]
            pnls = [_decimal(row["net_pnl"]) for row in settled]
            shares = sum((_decimal(row["filled_shares"]) for row in settled), ZERO)
            net = sum(pnls, ZERO)
            rows.append((
                confirmation.replace("CHAINLINK_", "CL ").replace("MC_BOOTSTRAP_", "MC "),
                policy.replace("IMMEDIATE_REVERSE", "FAST REVERSE").replace("CHAINLINK_REVERSE", "CL REVERSE"),
                len(entries), len(filled), len(settled),
                f"{sum(value > 0 for value in pnls)} / {sum(value < 0 for value in pnls)}",
                _fmt(net), "—" if shares == 0 else _fmt(net / shares),
            ))
        return rows

    def _mc_summary_rows(self, data: DashboardReadModel, symbols: Mapping[str, str]) -> list[tuple[Any, ...]]:
        grouped: dict[int, list[tuple[Mapping[str, Any], Mapping[str, Any]]]] = defaultdict(list)
        for row in data.monte_carlo_forecasts:
            symbol = symbols.get(str(row["market_id"]), "?")
            if self.asset is not None and symbol != self.asset:
                continue
            try:
                payload = json.loads(row["payload_json"])
            except (TypeError, json.JSONDecodeError):
                payload = {}
            grouped[int(row["horizon_seconds"])].append((row, payload))
        rows = []
        for horizon, forecasts in sorted(grouped.items(), reverse=True):
            entered = [(row, payload) for row, payload in forecasts if row["decision"] == "ENTER"]
            settled = [(row, payload) for row, payload in entered if row["winner"] is not None]
            wins = sum(payload.get("side") == row["winner"] for row, payload in settled)
            rows.append((
                f"{horizon}s", len(forecasts), len(entered), len(forecasts) - len(entered),
                len(settled), f"{wins} / {len(settled) - wins}",
            ))
        return rows

    def _health_rows(
        self, data: DashboardReadModel, snapshot: Mapping[str, Any], *, include_events: bool = False
    ) -> list[tuple[Any, ...]]:
        health, resolver = snapshot.get("health", {}), snapshot.get("resolver", ())
        active_markets = self._current_markets(snapshot)
        reconnect = not active_markets or any(
            not book.get("valid", False)
            for market in active_markets for book in market.get("books", {}).values()
        )
        now_ms = int(self._clock_s() * 1000)
        active_symbols = {str(market.get("symbol", "")).lower() for market in active_markets}
        relevant_resolver = [
            view for view in resolver if isinstance(view, Mapping)
            and str(view.get("symbol", "")).lower() in active_symbols
        ]
        stale = len(relevant_resolver) != len(active_symbols) or any(
            view.get("observation_ts_ms") is None
            or now_ms - int(view["observation_ts_ms"]) < 0
            or now_ms - int(view["observation_ts_ms"]) > 10_000
            for view in relevant_resolver
        )
        reasons = [health.get(key) for key in ("storage", "dashboard", "processing", "discovery", "settlement") if health.get(key)]
        free, minimum = health.get("disk_free_bytes"), health.get("disk_min_free_bytes")
        snapshot_ts = snapshot.get("snapshot_ts_ms")
        lag_ms = None if snapshot_ts is None else max(0, now_ms - int(snapshot_ts))
        database_bad = bool(reasons or health.get("pending_storage") or lag_ms is None
                            or lag_ms > max(5_000, int(self.refresh * 3_000)))
        rows = [("market books", self._status("BLOCKED" if reconnect else "OK", "BLOCKED" if reconnect else "OK"),
                 "active round awaiting a valid snapshot" if reconnect else "all active UP/DOWN books valid"),
                ("Chainlink resolver", self._status("STALE" if stale else "OK", "STALE" if stale else "OK"),
                 "selected TWAP-60 must be ≤10s old"),
                ("database", self._status("CRITICAL" if database_bad else "OK", "CRITICAL" if database_bad else "OK"),
                 ",".join(reasons) or ("snapshot unavailable" if lag_ms is None else f"snapshot lag={lag_ms}ms")),
                ("disk / journal", self._status("CRITICAL" if not health.get("journal_writable", False) else "OK",
                                                "CRITICAL" if not health.get("journal_writable", False) else "OK"),
                 f"free={free if free is not None else 'unknown'} min={minimum if minimum is not None else 'unknown'} reason={health.get('journal_reason') or 'none'}")]
        for event in data.health_events[:3] if include_events else ():
            try:
                payload = json.loads(event["payload_json"])
            except (TypeError, json.JSONDecodeError):
                payload = {}
            status = str(payload.get("status", "observed")).upper()
            state = "OK" if status in {"OK", "RECOVERED", "CONNECTED"} else "EVENT"
            detail = payload.get("reason") or payload.get("detail") or f"at {event['event_ts_ms']}"
            rows.append((event["kind"], self._status(state, state), detail))
        return rows

    def _banner(self, snapshot: Mapping[str, Any]) -> Panel:
        now_ms = int(self._clock_s() * 1000)
        snapshot_ts = snapshot.get("snapshot_ts_ms")
        age = None if snapshot_ts is None else max(0, now_ms - int(snapshot_ts)) / 1000
        health = snapshot.get("health", {})
        reasons = [health.get(key) for key in ("storage", "dashboard", "processing", "discovery", "settlement") if health.get(key)]
        state = "HEALTHY" if not reasons and age is not None and age <= max(5, self.refresh * 3) else "ATTENTION"
        color = "green" if state == "HEALTHY" else "yellow"
        filters = [self.asset.upper() if self.asset else "ALL ASSETS"]
        if self.threshold:
            filters.append(f"threshold {self.threshold}")
        if self.confirmation:
            filters.append(self.confirmation)
        if self.policy:
            filters.append(self.policy)
        line = Text()
        line.append(" PAPER ONLY · NO ORDERS ", style="bold white on dark_green")
        line.append(f"  {self.view.upper()}  ", style="bold bright_cyan")
        line.append(f"● {state}", style=f"bold {color}")
        line.append(f"   DB {age:.1f}s" if age is not None else "   DB —", style="dim")
        line.append("   " + " · ".join(filters), style="white")
        line.append("   views: --view overview|performance|activity", style="dim")
        return Panel(line, border_style="bright_black", box=box.ROUNDED, padding=(0, 0))

    def render(self) -> Layout:
        data = self.data()
        snapshot = data.snapshot or {"markets": (), "resolver": (), "health": {}}
        symbols = {str(row["market_id"]): str(row["symbol"]) for row in data.market_metadata}
        symbols.update({str(row.get("market_id")): str(row.get("symbol")) for row in snapshot.get("markets", ())})
        layout = Layout(name="root")
        layout.split_column(Layout(name="banner", size=3), Layout(name="content"))
        layout["banner"].update(self._banner(snapshot))
        content = layout["content"]
        if self.view == "overview":
            content.split_column(
                Layout(name="pulse", size=6), Layout(name="signals", size=6),
                Layout(name="positions", size=7), Layout(name="health", size=7),
            )
            content["pulse"].update(self._panel(
                "Market pulse", ("asset", "closes", "UP bid / ask", "DOWN bid / ask", "CL leader", "move bps", "age", "feed"),
                self._pulse_rows(snapshot), subtitle="nearest open round",
            ))
            content["signals"].update(self._panel(
                "Our signals", ("asset", "signal", "base support", "MC support", "P(win)", "edge", "CL", "why"),
                self._signal_rows(data, snapshot), subtitle="observational · never sends orders",
            ))
            content["positions"].update(self._panel(
                "Open paper inventory", ("asset", "market", "closes", "lanes", "UP shares", "DOWN shares", "state"),
                self._position_summary_rows(data, symbols, snapshot), subtitle="virtual fills only",
            ))
            content["health"].update(self._panel(
                "System state", ("component", "state", "current detail"), self._health_rows(data, snapshot),
                subtitle="current state only",
            ))
        elif self.view == "performance":
            content.split_column(Layout(name="score", ratio=2), Layout(name="mc", ratio=1))
            content["score"].update(self._panel(
                "Strategy scoreboard", ("confirmation", "policy", "signals", "filled", "settled", "W / L", "net PnL", "EV/share"),
                self._scoreboard_rows(data, symbols), subtitle="settled results are authoritative",
            ))
            content["mc"].update(self._panel(
                "Monte Carlo outcomes", ("window", "observed", "entered", "rejected", "settled", "W / L"),
                self._mc_summary_rows(data, symbols), subtitle="recent 100 persisted forecasts",
            ))
        else:
            content.split_column(Layout(name="mc", ratio=2), Layout(name="health", ratio=1))
            content["mc"].update(self._panel(
                "Monte Carlo decision log", ("asset", "window", "side", "ask", "P(win)", "break-even", "edge", "decision", "reason", "result"),
                self._monte_carlo_rows(data, symbols), subtitle="newest first · persisted immutable observations",
            ))
            content["health"].update(self._panel(
                "Feed and storage timeline", ("component", "state", "current detail"),
                self._health_rows(data, snapshot, include_events=True),
            ))
        return layout

    async def run(self, stop_event: asyncio.Event | None = None, *, iterations: int | None = None) -> None:
        stop_event, count = stop_event or asyncio.Event(), 0
        console = Console()
        try:
            with Live(
                self.render(), console=console, auto_refresh=False,
                screen=console.is_terminal, transient=False, vertical_overflow="crop",
            ) as live:
                while not stop_event.is_set() and (iterations is None or count < iterations):
                    if count:
                        live.update(self.render(), refresh=True)
                    else:
                        live.refresh()
                    count += 1
                    try:
                        await asyncio.wait_for(stop_event.wait(), timeout=self.refresh)
                    except asyncio.TimeoutError:
                        pass
        finally:
            self.close()


def watch(db_path: str | Path, refresh: float = 2.0, **filters: str | None) -> int:
    try:
        asyncio.run(PaperDashboard(db_path, refresh, **filters).run())
    except KeyboardInterrupt:
        return 0
    return 0


__all__ = ["PaperDashboard", "watch"]
