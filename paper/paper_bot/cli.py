from __future__ import annotations

import argparse
import asyncio
import json
import os
import signal
import sqlite3
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from .config import Settings, load_settings
from .storage import Storage

_PUBLIC_SETTING_NAMES = {
    "SYMBOLS", "ENTRY_THRESHOLDS", "PAPER_NOTIONAL_USD", "RTDS_STALE_SEC", "DATA_DIR",
}


def _settings(environment: Mapping[str, str] | None = None) -> Settings:
    source = os.environ if environment is None else environment
    return load_settings({key: source[key] for key in _PUBLIC_SETTING_NAMES if key in source})


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="paper-bot")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("engine", help="run the strictly paper-only engine")
    commands.add_parser("status", help="print a read-only SQLite snapshot")
    commands.add_parser("check-db", help="run read-only SQLite integrity checks")
    watch = commands.add_parser("watch", help="attach a read-only terminal dashboard")
    watch.add_argument("--db", required=True, help="SQLite database path")
    watch.add_argument("--refresh", type=float, default=2.0, help="refresh interval in seconds")
    watch.add_argument("--view", choices=("overview", "performance", "activity"), default="overview")
    watch.add_argument("--asset", choices=("btc", "eth", "sol"))
    watch.add_argument("--threshold")
    watch.add_argument("--confirmation")
    watch.add_argument("--policy")
    return parser


def _database_path(settings: Settings) -> Path:
    return settings.data_dir / "paper.db"


def _status(settings: Settings) -> int:
    storage = Storage(_database_path(settings), read_only=True)
    try:
        storage.initialize()
        print(json.dumps(storage.dashboard_snapshot().__dict__, sort_keys=True, separators=(",", ":")))
        return 0
    except (OSError, sqlite3.Error, ValueError) as exc:
        print(f"status unavailable: {type(exc).__name__}")
        return 1
    finally:
        storage.close()


def _check_db(settings: Settings) -> int:
    storage = Storage(_database_path(settings), read_only=True)
    try:
        storage.initialize()
        assert storage.db is not None
        integrity = storage.db.execute("PRAGMA integrity_check").fetchone()[0]
        foreign_keys = tuple(storage.db.execute("PRAGMA foreign_key_check"))
        if integrity != "ok" or foreign_keys:
            print("database check failed")
            return 1
        print("ok")
        return 0
    except (OSError, sqlite3.Error, ValueError) as exc:
        print(f"database check unavailable: {type(exc).__name__}")
        return 1
    finally:
        storage.close()


async def _run_engine(settings: Settings) -> int:
    import aiohttp
    import websockets
    from .engine import PaperEngine
    from .gamma import GammaClient
    from .journal import RawJournal
    from .market_ws import MarketWsClient
    from .rtds import RtdsClient
    storage = Storage(_database_path(settings))
    journal = RawJournal(settings.data_dir / "raw-journal")
    async with aiohttp.ClientSession() as session:
        async def get_json(url: str, params: dict[str, str]) -> Any:
            async with session.get(url, params=params) as response:
                response.raise_for_status()
                return await response.json()

        async def connect(url: str) -> Any:
            return await websockets.connect(url, open_timeout=10, close_timeout=5)

        gamma = GammaClient(settings.gamma_url, get_json)

        async def settlement_fetcher(market: Any) -> Any:
            return await gamma.get_market_by_id(market.market_id)

        engine = PaperEngine(
            settings,
            gamma=gamma,
            storage=storage,
            journal=journal,
            market_ws=MarketWsClient(connect, url=settings.market_ws_url),
            rtds=RtdsClient(connect, url=settings.rtds_url, symbols=settings.symbols),
            settlement_fetcher=settlement_fetcher,
        )
        loop = asyncio.get_running_loop()
        installed: list[signal.Signals] = []
        for handled in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(handled, engine.stop)
                installed.append(handled)
            except NotImplementedError:
                pass
        try:
            await engine.initialize()
            await engine.run()
            return 0
        finally:
            for handled in installed:
                loop.remove_signal_handler(handled)
            journal.close()
            storage.close()


def main(argv: Sequence[str] | None = None, *, environment: Mapping[str, str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "watch":
        from .tui import watch
        return watch(
            args.db, args.refresh, asset=args.asset, threshold=args.threshold,
            confirmation=args.confirmation, policy=args.policy, view=args.view,
        )
    settings = _settings(environment)
    if args.command == "status":
        return _status(settings)
    if args.command == "check-db":
        return _check_db(settings)
    return asyncio.run(_run_engine(settings))


if __name__ == "__main__":
    raise SystemExit(main())
