from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, fields, is_dataclass
from decimal import Decimal, InvalidOperation
from enum import Enum
from pathlib import Path
from typing import Any

from .accounting import LaneResult
from .config import Settings
from .domain import FakResult, FillLeg, InventoryLot, ReverseSequence
from .settlement import OfficialSettlement
from .strategy import Confirmation, LaneKey, PositionPolicy, StrategyEvent

TABLES = (
    "experiment_versions", "markets", "tokens", "resolver_observations",
    "book_generations", "signals", "paper_orders", "paper_fill_legs",
    "inventory_lots", "reverse_sequences", "settlements", "lane_results",
    "health_events", "dashboard_snapshots", "dashboard_market_metadata",
)
EXPERIMENT_SCHEMA_VERSION = "chainlink-fak-paper-v1"
ZERO = Decimal("0")


class StorageInvariantError(ValueError):
    pass


@dataclass(frozen=True)
class PersistedPosition:
    lane: LaneKey
    config_hash: str
    initial_side: str
    inventory_lots: tuple[InventoryLot, ...]
    reverse_attempted: bool
    entry: StrategyEvent
    reverse: ReverseSequence | None


@dataclass(frozen=True)
class PersistedMarketState:
    experiment_hash: str
    market_id: str
    mkt_ts: int
    close_ts: int
    attempted_lane_keys: tuple[LaneKey, ...]
    open_positions: tuple[PersistedPosition, ...]
    lane_records: tuple[PersistedPosition, ...]


@dataclass(frozen=True)
class DashboardSnapshot:
    markets: int
    open_positions: int
    signals: int
    settlements: int
    health_events: int


@dataclass(frozen=True)
class DashboardReadModel:
    snapshot: Mapping[str, Any] | None
    market_metadata: tuple[Mapping[str, Any], ...]
    entries: tuple[Mapping[str, Any], ...]
    inventory: tuple[Mapping[str, Any], ...]
    health_events: tuple[Mapping[str, Any], ...]


def canonical_decimal(value: Decimal) -> str:
    if not isinstance(value, Decimal) or not value.is_finite():
        raise StorageInvariantError("decimal must be finite")
    if value == 0:
        return "0"
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text


def _canonical(value: Any) -> Any:
    if isinstance(value, Decimal):
        return canonical_decimal(value)
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Path):
        return str(value)
    if is_dataclass(value):
        return {field.name: _canonical(getattr(value, field.name)) for field in fields(value)}
    if isinstance(value, Mapping):
        return {str(key): _canonical(item) for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))}
    if isinstance(value, (tuple, list)):
        return [_canonical(item) for item in value]
    if value is None or isinstance(value, (str, int, bool)):
        return value
    raise StorageInvariantError(f"unsupported canonical value type: {type(value).__name__}")


def _json(value: Any) -> str:
    return json.dumps(_canonical(value), sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _hash(*parts: str) -> str:
    return hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()


def _decimal_or_none(value: Decimal | None) -> str | None:
    return None if value is None else canonical_decimal(value)


def _sqlite_decimal_nonnegative(raw: Any) -> int:
    if not isinstance(raw, str):
        return 0
    try:
        value = Decimal(raw)
        return int(value.is_finite() and value >= 0 and canonical_decimal(value) == raw)
    except (InvalidOperation, StorageInvariantError):
        return 0


def _stored_mapping(raw: Any, name: str) -> Mapping[str, Any]:
    if not isinstance(raw, Mapping):
        raise StorageInvariantError(f"stored {name} must be a mapping")
    return raw


def _stored_string(raw: Any, name: str) -> str:
    if not isinstance(raw, str) or not raw:
        raise StorageInvariantError(f"stored {name} must be nonempty")
    return raw


def _stored_int(raw: Any, name: str, *, optional: bool = False) -> int | None:
    if optional and raw is None:
        return None
    if isinstance(raw, bool) or not isinstance(raw, int) or raw < 0:
        raise StorageInvariantError(f"stored {name} must be a nonnegative integer")
    return raw


def _stored_decimal(raw: Any, name: str, *, optional: bool = False) -> Decimal | None:
    if optional and raw is None:
        return None
    if not isinstance(raw, str):
        raise StorageInvariantError(f"stored {name} must be a canonical Decimal string")
    try:
        value = Decimal(raw)
    except InvalidOperation as exc:
        raise StorageInvariantError(f"stored {name} is not Decimal") from exc
    if not value.is_finite() or canonical_decimal(value) != raw:
        raise StorageInvariantError(f"stored {name} is not canonical")
    return value


def _stored_lane(raw: Any) -> LaneKey:
    lane = _stored_mapping(raw, "lane")
    try:
        return LaneKey(
            _stored_decimal(lane.get("threshold"), "lane threshold"),
            Confirmation(_stored_string(lane.get("confirmation"), "lane confirmation")),
            PositionPolicy(_stored_string(lane.get("policy"), "lane policy")),
        )
    except ValueError as exc:
        raise StorageInvariantError("stored lane enum is invalid") from exc


def _stored_fak(raw: Any) -> FakResult:
    value = _stored_mapping(raw, "FAK")
    raw_legs = value.get("legs")
    if not isinstance(raw_legs, list):
        raise StorageInvariantError("stored FAK legs must be a list")
    legs: list[FillLeg] = []
    for raw_leg in raw_legs:
        leg = _stored_mapping(raw_leg, "fill leg")
        legs.append(FillLeg(
            _stored_decimal(leg.get("price"), "leg price"),
            _stored_decimal(leg.get("shares"), "leg shares"),
            _stored_decimal(leg.get("quote"), "leg quote"),
            _stored_decimal(leg.get("fee"), "leg fee"),
        ))
    return FakResult(
        requested_quote=_stored_decimal(value.get("requested_quote"), "requested quote", optional=True),
        requested_shares=_stored_decimal(value.get("requested_shares"), "requested shares", optional=True),
        submitted_maker_amount=_stored_decimal(value.get("submitted_maker_amount"), "maker amount"),
        submitted_taker_amount=_stored_decimal(value.get("submitted_taker_amount"), "taker amount"),
        filled_shares=_stored_decimal(value.get("filled_shares"), "filled shares"),
        quote_amount=_stored_decimal(value.get("quote_amount"), "quote amount"),
        unfilled_quote=_stored_decimal(value.get("unfilled_quote"), "unfilled quote", optional=True),
        unfilled_shares=_stored_decimal(value.get("unfilled_shares"), "unfilled shares", optional=True),
        fee=_stored_decimal(value.get("fee"), "FAK fee"),
        legs=tuple(legs),
        status=_stored_string(value.get("status"), "FAK status"),
    )


def _stored_inventory(raw: Any) -> tuple[InventoryLot, ...]:
    if not isinstance(raw, list):
        raise StorageInvariantError("stored inventory must be a list")
    result: list[InventoryLot] = []
    for item in raw:
        lot = _stored_mapping(item, "inventory lot")
        result.append(InventoryLot(
            _stored_string(lot.get("token_id"), "lot token"),
            _stored_string(lot.get("side"), "lot side"),
            _stored_decimal(lot.get("shares"), "lot shares"),
            _stored_string(lot.get("source"), "lot source"),
        ))
    return tuple(result)


def _stored_entry(raw_json: str) -> StrategyEvent:
    try:
        raw = json.loads(raw_json)
    except json.JSONDecodeError as exc:
        raise StorageInvariantError("stored entry JSON is invalid") from exc
    value = _stored_mapping(raw, "entry")
    event = StrategyEvent(
        lane=_stored_lane(value.get("lane")),
        kind=_stored_string(value.get("kind"), "entry kind"),
        market_id=_stored_string(value.get("market_id"), "entry market"),
        mkt_ts=_stored_int(value.get("mkt_ts"), "entry market timestamp"),
        token_id=_stored_string(value.get("token_id"), "entry token"),
        side=_stored_string(value.get("side"), "entry side"),
        event_ts_ms=_stored_int(value.get("event_ts_ms"), "entry event timestamp"),
        book_generation=_stored_int(value.get("book_generation"), "entry generation"),
        config_hash=_stored_string(value.get("config_hash"), "entry config"),
        fak=_stored_fak(value.get("fak")),
    )
    Storage._validate_fak(event.fak)
    return event


def _stored_reverse(raw_json: str) -> ReverseSequence:
    try:
        raw = json.loads(raw_json)
    except json.JSONDecodeError as exc:
        raise StorageInvariantError("stored reverse JSON is invalid") from exc
    value = _stored_mapping(raw, "reverse")
    transitions = value.get("transitions")
    if not isinstance(transitions, list) or any(not isinstance(item, str) for item in transitions):
        raise StorageInvariantError("stored reverse transitions are invalid")
    buy_raw = value.get("buy")
    return ReverseSequence(
        lane=_stored_lane(value.get("lane")),
        market_id=_stored_string(value.get("market_id"), "reverse market"),
        mkt_ts=_stored_int(value.get("mkt_ts"), "reverse market timestamp"),
        config_hash=_stored_string(value.get("config_hash"), "reverse config"),
        old_side=_stored_string(value.get("old_side"), "reverse old side"),
        new_side=_stored_string(value.get("new_side"), "reverse new side"),
        status=_stored_string(value.get("status"), "reverse status"),
        outcome=_stored_string(value.get("outcome"), "reverse outcome"),
        transitions=tuple(transitions),
        requested_shares=_stored_decimal(value.get("requested_shares"), "reverse requested shares"),
        sold_shares=_stored_decimal(value.get("sold_shares"), "reverse sold shares"),
        old_residual_shares=_stored_decimal(value.get("old_residual_shares"), "reverse residual"),
        submission_dust_shares=_stored_decimal(value.get("submission_dust_shares"), "reverse dust"),
        opposite_shares=_stored_decimal(value.get("opposite_shares"), "reverse opposite shares"),
        expected_quote=_stored_decimal(value.get("expected_quote"), "reverse expected quote"),
        sell=_stored_fak(value.get("sell")),
        buy=None if buy_raw is None else _stored_fak(buy_raw),
        inventory_lots=_stored_inventory(value.get("inventory_lots")),
        sell_book_generation=_stored_int(value.get("sell_book_generation"), "reverse sell generation"),
        buy_book_generation=_stored_int(value.get("buy_book_generation"), "reverse buy generation", optional=True),
        trigger_ts_ms=_stored_int(value.get("trigger_ts_ms"), "reverse trigger timestamp"),
        leg_elapsed_ms=_stored_int(value.get("leg_elapsed_ms"), "reverse elapsed", optional=True),
    )
SCHEMA = """
CREATE TABLE IF NOT EXISTS experiment_versions(
    experiment_hash TEXT PRIMARY KEY CHECK(length(experiment_hash)=64),
    settings_json TEXT NOT NULL UNIQUE,
    created_at INTEGER NOT NULL DEFAULT (unixepoch())
);
CREATE TABLE IF NOT EXISTS markets(
    experiment_hash TEXT NOT NULL REFERENCES experiment_versions(experiment_hash),
    market_id TEXT NOT NULL,
    mkt_ts INTEGER NOT NULL CHECK(mkt_ts>=0),
    close_ts INTEGER NOT NULL CHECK(close_ts>=mkt_ts),
    status TEXT NOT NULL DEFAULT 'OPEN' CHECK(status IN ('OPEN','SETTLED')),
    PRIMARY KEY(experiment_hash, market_id)
);
CREATE TABLE IF NOT EXISTS tokens(
    experiment_hash TEXT NOT NULL,
    market_id TEXT NOT NULL,
    token_id TEXT NOT NULL,
    side TEXT NOT NULL CHECK(side IN ('UP','DOWN')),
    PRIMARY KEY(experiment_hash, market_id, token_id),
    FOREIGN KEY(experiment_hash,market_id) REFERENCES markets(experiment_hash,market_id)
);
CREATE TABLE IF NOT EXISTS resolver_observations(
    id INTEGER PRIMARY KEY,
    experiment_hash TEXT NOT NULL REFERENCES experiment_versions(experiment_hash),
    symbol TEXT NOT NULL,
    observation_ts_ms INTEGER NOT NULL,
    receive_ts_ms INTEGER NOT NULL,
    value TEXT NOT NULL CHECK(decimal_nonnegative(value)=1)
);
CREATE TABLE IF NOT EXISTS book_generations(
    experiment_hash TEXT NOT NULL,
    market_id TEXT NOT NULL,
    token_id TEXT NOT NULL,
    generation INTEGER NOT NULL CHECK(generation>0),
    event_ts_ms INTEGER NOT NULL CHECK(event_ts_ms>=0),
    PRIMARY KEY(experiment_hash,market_id,token_id,generation),
    FOREIGN KEY(experiment_hash,market_id,token_id) REFERENCES tokens(experiment_hash,market_id,token_id)
);
CREATE TABLE IF NOT EXISTS signals(
    signal_id TEXT PRIMARY KEY CHECK(length(signal_id)=64),
    experiment_hash TEXT NOT NULL,
    market_id TEXT NOT NULL,
    threshold TEXT NOT NULL CHECK(decimal_nonnegative(threshold)=1),
    confirmation TEXT NOT NULL,
    policy TEXT NOT NULL,
    phase TEXT NOT NULL CHECK(phase IN ('ENTRY','REVERSE')),
    side TEXT NOT NULL CHECK(side IN ('UP','DOWN')),
    token_id TEXT,
    event_ts_ms INTEGER NOT NULL CHECK(event_ts_ms>=0),
    book_generation INTEGER NOT NULL CHECK(book_generation>0),
    payload_json TEXT NOT NULL,
    UNIQUE(experiment_hash,market_id,threshold,confirmation,policy,phase),
    FOREIGN KEY(experiment_hash,market_id) REFERENCES markets(experiment_hash,market_id)
);
CREATE TABLE IF NOT EXISTS paper_orders(
    order_id TEXT PRIMARY KEY CHECK(length(order_id)=64),
    signal_id TEXT NOT NULL REFERENCES signals(signal_id) ON DELETE CASCADE,
    role TEXT NOT NULL CHECK(role IN ('ENTRY','REVERSE_SELL','REVERSE_BUY')),
    requested_quote TEXT CHECK(requested_quote IS NULL OR decimal_nonnegative(requested_quote)=1),
    requested_shares TEXT CHECK(requested_shares IS NULL OR decimal_nonnegative(requested_shares)=1),
    submitted_maker_amount TEXT NOT NULL CHECK(decimal_nonnegative(submitted_maker_amount)=1),
    submitted_taker_amount TEXT NOT NULL CHECK(decimal_nonnegative(submitted_taker_amount)=1),
    filled_shares TEXT NOT NULL CHECK(decimal_nonnegative(filled_shares)=1),
    quote_amount TEXT NOT NULL CHECK(decimal_nonnegative(quote_amount)=1),
    unfilled_quote TEXT CHECK(unfilled_quote IS NULL OR decimal_nonnegative(unfilled_quote)=1),
    unfilled_shares TEXT CHECK(unfilled_shares IS NULL OR decimal_nonnegative(unfilled_shares)=1),
    fee TEXT NOT NULL CHECK(decimal_nonnegative(fee)=1),
    status TEXT NOT NULL CHECK(status IN ('zero','partial','full')),
    UNIQUE(signal_id,role)
);
CREATE TABLE IF NOT EXISTS paper_fill_legs(
    order_id TEXT NOT NULL REFERENCES paper_orders(order_id) ON DELETE CASCADE,
    leg_index INTEGER NOT NULL CHECK(leg_index>=0),
    price TEXT NOT NULL CHECK(decimal_nonnegative(price)=1),
    shares TEXT NOT NULL CHECK(decimal_nonnegative(shares)=1),
    quote TEXT NOT NULL CHECK(decimal_nonnegative(quote)=1),
    fee TEXT NOT NULL CHECK(decimal_nonnegative(fee)=1),
    PRIMARY KEY(order_id,leg_index)
);
CREATE TABLE IF NOT EXISTS inventory_lots(
    lot_id TEXT PRIMARY KEY CHECK(length(lot_id)=64),
    experiment_hash TEXT NOT NULL,
    market_id TEXT NOT NULL,
    threshold TEXT NOT NULL CHECK(decimal_nonnegative(threshold)=1),
    confirmation TEXT NOT NULL,
    policy TEXT NOT NULL,
    token_id TEXT NOT NULL,
    side TEXT NOT NULL CHECK(side IN ('UP','DOWN')),
    shares TEXT NOT NULL CHECK(decimal_nonnegative(shares)=1),
    source TEXT NOT NULL,
    open INTEGER NOT NULL DEFAULT 1 CHECK(open IN (0,1)),
    FOREIGN KEY(experiment_hash,market_id,token_id) REFERENCES tokens(experiment_hash,market_id,token_id)
);
CREATE TABLE IF NOT EXISTS reverse_sequences(
    reverse_id TEXT PRIMARY KEY CHECK(length(reverse_id)=64),
    signal_id TEXT NOT NULL UNIQUE REFERENCES signals(signal_id),
    payload_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS settlements(
    market_id TEXT PRIMARY KEY,
    winner TEXT NOT NULL CHECK(winner IN ('UP','DOWN')),
    resolved_at INTEGER,
    payload_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS lane_results(
    experiment_hash TEXT NOT NULL,
    market_id TEXT NOT NULL REFERENCES settlements(market_id),
    threshold TEXT NOT NULL CHECK(decimal_nonnegative(threshold)=1),
    confirmation TEXT NOT NULL,
    policy TEXT NOT NULL,
    net_pnl TEXT NOT NULL CHECK(decimal_nonnegative(net_pnl)=1 OR substr(net_pnl,1,1)='-'),
    result_json TEXT NOT NULL,
    PRIMARY KEY(experiment_hash,market_id,threshold,confirmation,policy),
    FOREIGN KEY(experiment_hash,market_id) REFERENCES markets(experiment_hash,market_id)
);
CREATE TABLE IF NOT EXISTS health_events(
    id INTEGER PRIMARY KEY,
    event_ts_ms INTEGER NOT NULL CHECK(event_ts_ms>=0),
    kind TEXT NOT NULL,
    payload_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS dashboard_snapshots(
    snapshot_id INTEGER PRIMARY KEY CHECK(snapshot_id=1),
    snapshot_ts_ms INTEGER NOT NULL CHECK(snapshot_ts_ms>=0),
    payload_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS dashboard_market_metadata(
    experiment_hash TEXT NOT NULL,
    market_id TEXT NOT NULL,
    symbol TEXT NOT NULL CHECK(symbol IN ('btc','eth','sol')),
    slug TEXT NOT NULL,
    PRIMARY KEY(experiment_hash,market_id)
);
"""


class Storage:
    def __init__(self, path: str | Path, read_only: bool = False, **legacy: Any) -> None:
        if "readonly" in legacy:
            read_only = bool(legacy.pop("readonly"))
        if legacy:
            raise TypeError("unexpected Storage arguments")
        self.path = Path(path)
        self.read_only = read_only
        self.db: sqlite3.Connection | None = None
        self._experiment_hash: str | None = None

    def initialize(self) -> None:
        if self.db is not None:
            return
        if self.read_only:
            uri = f"file:{self.path.absolute()}?mode=ro"
            self.db = sqlite3.connect(uri, uri=True, isolation_level=None)
        else:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.db = sqlite3.connect(self.path, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.create_function("decimal_nonnegative", 1, _sqlite_decimal_nonnegative, deterministic=True)
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.execute("PRAGMA busy_timeout=5000")
        if self.read_only:
            self.db.execute("PRAGMA query_only=ON")
            missing = set(TABLES) - {
                row[0] for row in self.db.execute("SELECT name FROM sqlite_master WHERE type='table'")
            }
            # Dashboard tables are additive; an old database remains safely
            # attachable in read-only mode until the engine migrates it.
            missing.difference_update({"dashboard_snapshots", "dashboard_market_metadata"})
            if missing:
                raise StorageInvariantError("storage schema is incomplete")
            return
        mode = self.db.execute("PRAGMA journal_mode=WAL").fetchone()[0]
        if str(mode).lower() != "wal":
            raise StorageInvariantError("SQLite WAL mode unavailable")
        self.db.executescript(SCHEMA)

    def close(self) -> None:
        if self.db is not None:
            self.db.close()
            self.db = None

    def __enter__(self) -> Storage:
        self.initialize()
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _connection(self) -> sqlite3.Connection:
        if self.db is None:
            raise RuntimeError("storage is not initialized")
        return self.db

    def _writable(self) -> sqlite3.Connection:
        db = self._connection()
        if self.read_only:
            raise sqlite3.OperationalError("storage is read-only")
        return db

    def ensure_experiment(self, settings: Settings) -> str:
        if not isinstance(settings, Settings):
            raise TypeError("settings must be Settings")
        identity = {
            "version": EXPERIMENT_SCHEMA_VERSION,
            "symbols": settings.symbols,
            "thresholds": settings.thresholds,
            "paper_notional_usd": settings.paper_notional_usd,
            "rtds_stale_seconds": settings.rtds_stale_seconds,
        }
        raw = _json(identity)
        digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()
        db = self._writable()
        db.execute("BEGIN IMMEDIATE")
        try:
            db.execute(
                "INSERT OR IGNORE INTO experiment_versions(experiment_hash,settings_json) VALUES (?,?)",
                (digest, raw),
            )
            stored = db.execute(
                "SELECT settings_json FROM experiment_versions WHERE experiment_hash=?", (digest,)
            ).fetchone()
            if stored is None or stored[0] != raw:
                raise StorageInvariantError("experiment hash collision")
            db.commit()
        except Exception:
            db.rollback()
            raise
        self._experiment_hash = digest
        return digest

    def record_strategy_events(self, events: Iterable[StrategyEvent | ReverseSequence]) -> None:
        db = self._writable()
        experiment_hash = self._require_experiment()
        materialized = tuple(events)
        db.execute("BEGIN IMMEDIATE")
        try:
            for event in materialized:
                if isinstance(event, StrategyEvent):
                    self._record_entry(db, experiment_hash, event)
                elif isinstance(event, ReverseSequence):
                    self._record_reverse(db, experiment_hash, event)
                else:
                    raise TypeError("unsupported strategy event")
            db.commit()
        except Exception:
            db.rollback()
            raise

    def _require_experiment(self) -> str:
        if self._experiment_hash is None:
            raise StorageInvariantError("ensure_experiment must be called before writes")
        return self._experiment_hash

    @staticmethod
    def _lane_columns(lane: LaneKey) -> tuple[str, str, str]:
        return canonical_decimal(lane.threshold), lane.confirmation.value, lane.policy.value

    def _signal_id(self, experiment_hash: str, market_id: str, lane: LaneKey, phase: str) -> str:
        return _hash(experiment_hash, market_id, *self._lane_columns(lane), phase)

    @staticmethod
    def _validate_fak(result: FakResult) -> None:
        if not isinstance(result, FakResult):
            raise TypeError("order evidence must be FakResult")
        shares = sum((leg.shares for leg in result.legs), ZERO)
        quote = sum((leg.quote for leg in result.legs), ZERO)
        fee = sum((leg.fee for leg in result.legs), ZERO)
        if (shares, quote, fee) != (result.filled_shares, result.quote_amount, result.fee):
            raise StorageInvariantError("order totals do not equal fill legs")
        values = (
            result.submitted_maker_amount, result.submitted_taker_amount,
            result.filled_shares, result.quote_amount, result.fee,
        )
        if any(not isinstance(value, Decimal) or not value.is_finite() or value < 0 for value in values):
            raise StorageInvariantError("invalid order Decimal")

    def _ensure_market(self, db: sqlite3.Connection, experiment_hash: str, market_id: str, mkt_ts: int) -> None:
        db.execute(
            "INSERT OR IGNORE INTO markets(experiment_hash,market_id,mkt_ts,close_ts) VALUES (?,?,?,?)",
            (experiment_hash, market_id, mkt_ts, mkt_ts + 300),
        )
        row = db.execute(
            "SELECT mkt_ts,status FROM markets WHERE experiment_hash=? AND market_id=?",
            (experiment_hash, market_id),
        ).fetchone()
        if row is None or row[0] != mkt_ts:
            raise StorageInvariantError("market identity conflict")
        if row[1] != "OPEN":
            raise StorageInvariantError("market is already settled")

    @staticmethod
    def _ensure_token(
        db: sqlite3.Connection, experiment_hash: str, market_id: str, token_id: str, side: str
    ) -> None:
        db.execute(
            "INSERT OR IGNORE INTO tokens(experiment_hash,market_id,token_id,side) VALUES (?,?,?,?)",
            (experiment_hash, market_id, token_id, side),
        )
        row = db.execute(
            "SELECT side FROM tokens WHERE experiment_hash=? AND market_id=? AND token_id=?",
            (experiment_hash, market_id, token_id),
        ).fetchone()
        if row is None or row[0] != side:
            raise StorageInvariantError("token identity conflict")

    @staticmethod
    def _ensure_book_generation(
        db: sqlite3.Connection,
        experiment_hash: str,
        market_id: str,
        token_id: str,
        generation: int,
        event_ts_ms: int,
    ) -> None:
        db.execute(
            """INSERT OR IGNORE INTO book_generations
               (experiment_hash,market_id,token_id,generation,event_ts_ms) VALUES (?,?,?,?,?)""",
            (experiment_hash, market_id, token_id, generation, event_ts_ms),
        )
        row = db.execute(
            """SELECT 1 FROM book_generations WHERE experiment_hash=? AND market_id=?
               AND token_id=? AND generation=?""",
            (experiment_hash, market_id, token_id, generation),
        ).fetchone()
        if row is None:
            raise StorageInvariantError("book generation insert failed")

    def _insert_signal(
        self, db: sqlite3.Connection, experiment_hash: str, event: Any, phase: str,
        side: str, token_id: str | None, generation: int, event_ts_ms: int,
    ) -> tuple[str, bool]:
        threshold, confirmation, policy = self._lane_columns(event.lane)
        signal_id = self._signal_id(experiment_hash, event.market_id, event.lane, phase)
        payload = _json(event)
        existing = db.execute("SELECT payload_json FROM signals WHERE signal_id=?", (signal_id,)).fetchone()
        if existing is not None:
            if existing[0] != payload:
                raise StorageInvariantError("conflicting duplicate signal")
            return signal_id, False
        db.execute(
            """INSERT INTO signals(signal_id,experiment_hash,market_id,threshold,confirmation,policy,
               phase,side,token_id,event_ts_ms,book_generation,payload_json)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (signal_id, experiment_hash, event.market_id, threshold, confirmation, policy,
             phase, side, token_id, event_ts_ms, generation, payload),
        )
        return signal_id, True

    def _insert_order(self, db: sqlite3.Connection, signal_id: str, role: str, result: FakResult) -> None:
        self._validate_fak(result)
        order_id = _hash(signal_id, role)
        db.execute(
            """INSERT INTO paper_orders VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (order_id, signal_id, role, _decimal_or_none(result.requested_quote),
             _decimal_or_none(result.requested_shares), canonical_decimal(result.submitted_maker_amount),
             canonical_decimal(result.submitted_taker_amount), canonical_decimal(result.filled_shares),
             canonical_decimal(result.quote_amount), _decimal_or_none(result.unfilled_quote),
             _decimal_or_none(result.unfilled_shares), canonical_decimal(result.fee), result.status),
        )
        for index, leg in enumerate(result.legs):
            db.execute(
                "INSERT INTO paper_fill_legs VALUES (?,?,?,?,?,?)",
                (order_id, index, canonical_decimal(leg.price), canonical_decimal(leg.shares),
                 canonical_decimal(leg.quote), canonical_decimal(leg.fee)),
            )

    def _record_entry(self, db: sqlite3.Connection, experiment_hash: str, event: StrategyEvent) -> None:
        if event.config_hash != experiment_hash or event.kind != "entry_attempt":
            raise StorageInvariantError("entry experiment or kind mismatch")
        self._validate_fak(event.fak)
        self._ensure_market(db, experiment_hash, event.market_id, event.mkt_ts)
        self._ensure_token(db, experiment_hash, event.market_id, event.token_id, event.side)
        self._ensure_book_generation(
            db, experiment_hash, event.market_id, event.token_id,
            event.book_generation, event.event_ts_ms,
        )
        signal_id, inserted = self._insert_signal(
            db, experiment_hash, event, "ENTRY", event.side, event.token_id,
            event.book_generation, event.event_ts_ms,
        )
        if not inserted:
            return
        self._insert_order(db, signal_id, "ENTRY", event.fak)
        if event.fak.filled_shares > 0:
            threshold, confirmation, policy = self._lane_columns(event.lane)
            lot_id = _hash(signal_id, event.token_id, "entry")
            db.execute(
                """INSERT INTO inventory_lots VALUES (?,?,?,?,?,?,?,?,?,?,1)""",
                (lot_id, experiment_hash, event.market_id, threshold, confirmation, policy,
                 event.token_id, event.side, canonical_decimal(event.fak.filled_shares), "entry"),
            )

    def _record_reverse(self, db: sqlite3.Connection, experiment_hash: str, event: ReverseSequence) -> None:
        if event.config_hash != experiment_hash or event.status != "COMPLETE":
            raise StorageInvariantError("reverse experiment or status mismatch")
        self._ensure_market(db, experiment_hash, event.market_id, event.mkt_ts)
        entry_id = self._signal_id(experiment_hash, event.market_id, event.lane, "ENTRY")
        entry = db.execute("SELECT signal_id FROM signals WHERE signal_id=?", (entry_id,)).fetchone()
        if entry is None:
            raise StorageInvariantError("reverse has no persisted entry")
        old_token_row = db.execute(
            "SELECT token_id FROM signals WHERE signal_id=?", (entry_id,)
        ).fetchone()
        old_token = old_token_row[0]
        new_lots = tuple(event.inventory_lots)
        for lot in new_lots:
            self._ensure_token(db, experiment_hash, event.market_id, lot.token_id, lot.side)
        new_token = next((lot.token_id for lot in new_lots if lot.side == event.new_side), None)
        self._ensure_book_generation(
            db, experiment_hash, event.market_id, old_token,
            event.sell_book_generation, event.trigger_ts_ms,
        )
        if new_token is not None and event.buy_book_generation is not None:
            self._ensure_book_generation(
                db, experiment_hash, event.market_id, new_token,
                event.buy_book_generation, event.trigger_ts_ms,
            )
        signal_id, inserted = self._insert_signal(
            db, experiment_hash, event, "REVERSE", event.new_side, new_token,
            event.sell_book_generation, event.trigger_ts_ms,
        )
        if not inserted:
            return
        self._validate_fak(event.sell)
        if event.buy is not None:
            self._validate_fak(event.buy)
        if event.sell.filled_shares != event.sold_shares:
            raise StorageInvariantError("reverse sell total mismatch")
        old_open = db.execute(
            """SELECT lot_id,shares FROM inventory_lots WHERE experiment_hash=? AND market_id=?
               AND threshold=? AND confirmation=? AND policy=? AND open=1""",
            (experiment_hash, event.market_id, *self._lane_columns(event.lane)),
        ).fetchall()
        old_total = sum((Decimal(row[1]) for row in old_open), ZERO)
        if old_total != event.requested_shares:
            raise StorageInvariantError("reverse inventory input mismatch")
        old_output = sum((lot.shares for lot in new_lots if lot.side == event.old_side), ZERO)
        new_output = sum((lot.shares for lot in new_lots if lot.side == event.new_side), ZERO)
        new_tokens = {lot.token_id for lot in new_lots if lot.side == event.new_side}
        if (
            event.requested_shares - event.sold_shares != event.old_residual_shares
            or old_output != event.old_residual_shares
            or any(lot.token_id != old_token for lot in new_lots if lot.side == event.old_side)
            or new_output != event.opposite_shares
            or len(new_tokens) > 1
            or (event.buy is None and event.sold_shares != ZERO)
            or (event.buy is None and event.opposite_shares != ZERO)
            or (event.buy is not None and event.buy.filled_shares != event.opposite_shares)
        ):
            raise StorageInvariantError("reverse inventory output mismatch")
        db.execute(
            """UPDATE inventory_lots SET open=0 WHERE experiment_hash=? AND market_id=?
               AND threshold=? AND confirmation=? AND policy=? AND open=1""",
            (experiment_hash, event.market_id, *self._lane_columns(event.lane)),
        )
        self._insert_order(db, signal_id, "REVERSE_SELL", event.sell)
        if event.buy is not None:
            self._insert_order(db, signal_id, "REVERSE_BUY", event.buy)
        for index, lot in enumerate(new_lots):
            lot_id = _hash(signal_id, lot.token_id, lot.source, str(index))
            db.execute(
                """INSERT INTO inventory_lots VALUES (?,?,?,?,?,?,?,?,?,?,1)""",
                (lot_id, experiment_hash, event.market_id, *self._lane_columns(event.lane),
                 lot.token_id, lot.side, canonical_decimal(lot.shares), lot.source),
            )
        reverse_id = _hash(experiment_hash, event.market_id, *self._lane_columns(event.lane), "REVERSE")
        db.execute(
            "INSERT INTO reverse_sequences(reverse_id,signal_id,payload_json) VALUES (?,?,?)",
            (reverse_id, signal_id, _json(event)),
        )

    def load_open_market_states(self) -> tuple[PersistedMarketState, ...]:
        db = self._connection()
        rows = db.execute(
            "SELECT experiment_hash,market_id,mkt_ts,close_ts FROM markets WHERE status='OPEN' ORDER BY mkt_ts,market_id"
        ).fetchall()
        states: list[PersistedMarketState] = []
        for market in rows:
            args = (market["experiment_hash"], market["market_id"])
            signals = db.execute(
                """SELECT threshold,confirmation,policy,side,payload_json FROM signals
                   WHERE experiment_hash=? AND market_id=? AND phase='ENTRY'
                   ORDER BY threshold,confirmation,policy""", args,
            ).fetchall()
            attempted = tuple(
                LaneKey(Decimal(row["threshold"]), Confirmation(row["confirmation"]), PositionPolicy(row["policy"]))
                for row in signals
            )
            positions: list[PersistedPosition] = []
            lane_records: list[PersistedPosition] = []
            for row in signals:
                lane = LaneKey(Decimal(row["threshold"]), Confirmation(row["confirmation"]), PositionPolicy(row["policy"]))
                lane_args = args + self._lane_columns(lane)
                entry = _stored_entry(row["payload_json"])
                reverse_row = db.execute(
                    """SELECT payload_json FROM signals WHERE experiment_hash=? AND market_id=?
                       AND threshold=? AND confirmation=? AND policy=? AND phase='REVERSE'""",
                    lane_args,
                ).fetchone()
                reverse = None if reverse_row is None else _stored_reverse(reverse_row["payload_json"])
                lot_rows = db.execute(
                    """SELECT token_id,side,shares,source FROM inventory_lots WHERE experiment_hash=?
                       AND market_id=? AND threshold=? AND confirmation=? AND policy=? AND open=1
                       ORDER BY lot_id""", lane_args,
                ).fetchall()
                lots = tuple(
                    InventoryLot(item["token_id"], item["side"], Decimal(item["shares"]), item["source"])
                    for item in lot_rows
                )
                record = PersistedPosition(
                    lane, market["experiment_hash"], row["side"], lots,
                    reverse is not None, entry, reverse,
                )
                lane_records.append(record)
                if lots:
                    positions.append(record)
            states.append(
                PersistedMarketState(
                    market["experiment_hash"], market["market_id"], market["mkt_ts"], market["close_ts"],
                    attempted, tuple(positions), tuple(lane_records),
                )
            )
        return tuple(states)

    def record_settlement(
        self, market_id: str, settlement: OfficialSettlement, results: Iterable[LaneResult]
    ) -> None:
        db = self._writable()
        experiment_hash = self._require_experiment()
        if not market_id or not isinstance(settlement, OfficialSettlement):
            raise TypeError("invalid settlement input")
        materialized = tuple(results)
        if not materialized:
            raise StorageInvariantError("settlement requires lane results")
        if any(
            not isinstance(result, LaneResult) or not result.settled
            or result.market_id != market_id or result.config_hash != experiment_hash
            or result.net_pnl is None
            for result in materialized
        ):
            raise StorageInvariantError("invalid lane settlement result")
        settlement_payload = _json(settlement)
        result_payloads = {
            self._lane_columns(result.lane): (_json(result), canonical_decimal(result.net_pnl))
            for result in materialized
        }
        if len(result_payloads) != len(materialized):
            raise StorageInvariantError("duplicate lane settlement result")
        db.execute("BEGIN IMMEDIATE")
        try:
            existing = db.execute(
                "SELECT winner,resolved_at,payload_json FROM settlements WHERE market_id=?", (market_id,)
            ).fetchone()
            expected = (settlement.winner, settlement.resolved_at, settlement_payload)
            if existing is not None and tuple(existing) != expected:
                raise StorageInvariantError("settlement is immutable")
            existing_results = db.execute(
                "SELECT threshold,confirmation,policy,result_json,net_pnl FROM lane_results WHERE experiment_hash=? AND market_id=?",
                (experiment_hash, market_id),
            ).fetchall()
            if existing_results:
                stored = {
                    (row["threshold"], row["confirmation"], row["policy"]):
                    (row["result_json"], row["net_pnl"])
                    for row in existing_results
                }
                if stored != result_payloads:
                    raise StorageInvariantError("lane settlement results are immutable")
                db.commit()
                return
            if existing is None:
                db.execute(
                    "INSERT INTO settlements VALUES (?,?,?,?)",
                    (market_id, settlement.winner, settlement.resolved_at, settlement_payload),
                )
            market = db.execute(
                "SELECT 1 FROM markets WHERE experiment_hash=? AND market_id=?",
                (experiment_hash, market_id),
            ).fetchone()
            if market is None:
                raise StorageInvariantError("settlement market is unknown")
            for (threshold, confirmation, policy), (payload, net_pnl) in result_payloads.items():
                signal = db.execute(
                    """SELECT 1 FROM signals WHERE experiment_hash=? AND market_id=? AND threshold=?
                       AND confirmation=? AND policy=? AND phase='ENTRY'""",
                    (experiment_hash, market_id, threshold, confirmation, policy),
                ).fetchone()
                if signal is None:
                    raise StorageInvariantError("lane result has no persisted entry")
                db.execute(
                    "INSERT INTO lane_results VALUES (?,?,?,?,?,?,?)",
                    (experiment_hash, market_id, threshold, confirmation, policy, net_pnl, payload),
                )
            db.execute(
                "UPDATE markets SET status='SETTLED' WHERE experiment_hash=? AND market_id=?",
                (experiment_hash, market_id),
            )
            db.execute(
                "UPDATE inventory_lots SET open=0 WHERE experiment_hash=? AND market_id=?",
                (experiment_hash, market_id),
            )
            db.commit()
        except Exception:
            db.rollback()
            raise

    def dashboard_snapshot(self) -> DashboardSnapshot:
        db = self._connection()
        scalar = lambda sql: int(db.execute(sql).fetchone()[0])
        return DashboardSnapshot(
            markets=scalar("SELECT COUNT(*) FROM markets"),
            open_positions=scalar(
                """SELECT COUNT(*) FROM (SELECT 1 FROM inventory_lots WHERE open=1
                   GROUP BY experiment_hash,market_id,threshold,confirmation,policy)"""
            ),
            signals=scalar("SELECT COUNT(*) FROM signals"),
            settlements=scalar("SELECT COUNT(*) FROM settlements"),
            health_events=scalar("SELECT COUNT(*) FROM health_events"),
        )

    def write_dashboard_snapshot(self, snapshot: Mapping[str, Any], snapshot_ts_ms: int) -> None:
        """Atomically replace the latest sanitized, canonical dashboard view."""
        db = self._writable()
        if isinstance(snapshot_ts_ms, bool) or not isinstance(snapshot_ts_ms, int) or snapshot_ts_ms < 0:
            raise StorageInvariantError("snapshot timestamp must be a nonnegative integer")
        if not isinstance(snapshot, Mapping):
            raise TypeError("snapshot must be a mapping")
        payload = _json(snapshot)
        db.execute("BEGIN IMMEDIATE")
        try:
            db.execute(
                "INSERT INTO dashboard_snapshots(snapshot_id,snapshot_ts_ms,payload_json) VALUES (1,?,?) "
                "ON CONFLICT(snapshot_id) DO UPDATE SET snapshot_ts_ms=excluded.snapshot_ts_ms,payload_json=excluded.payload_json",
                (snapshot_ts_ms, payload),
            )
            experiment_hash, markets = snapshot.get("experiment_hash"), snapshot.get("markets", ())
            if experiment_hash is not None:
                if (not isinstance(experiment_hash, str) or len(experiment_hash) != 64
                        or not isinstance(markets, (list, tuple))):
                    raise StorageInvariantError("snapshot market metadata is invalid")
                for raw_market in markets:
                    if not isinstance(raw_market, Mapping):
                        raise StorageInvariantError("snapshot market must be a mapping")
                    market_id, symbol, slug = (
                        raw_market.get("market_id"), raw_market.get("symbol"), raw_market.get("slug")
                    )
                    if not all(isinstance(value, str) and value for value in (market_id, symbol, slug)):
                        raise StorageInvariantError("snapshot market identity is invalid")
                    db.execute(
                        "INSERT OR IGNORE INTO dashboard_market_metadata VALUES (?,?,?,?)",
                        (experiment_hash, market_id, symbol, slug),
                    )
                    identity = db.execute(
                        "SELECT symbol,slug FROM dashboard_market_metadata WHERE experiment_hash=? AND market_id=?",
                        (experiment_hash, market_id),
                    ).fetchone()
                    if identity is None or tuple(identity) != (symbol, slug):
                        raise StorageInvariantError("snapshot market identity changed")
            db.commit()
        except Exception:
            db.rollback()
            raise

    def load_dashboard_snapshot(self) -> Mapping[str, Any] | None:
        """Return the last complete snapshot, or None for an old/empty DB."""
        db = self._connection()
        try:
            row = db.execute(
                "SELECT snapshot_ts_ms,payload_json FROM dashboard_snapshots WHERE snapshot_id=1"
            ).fetchone()
        except sqlite3.OperationalError as exc:
            if "no such table" in str(exc).lower():
                return None
            raise
        if row is None:
            return None
        try:
            payload = json.loads(row["payload_json"])
        except (TypeError, json.JSONDecodeError) as exc:
            raise StorageInvariantError("dashboard snapshot JSON is invalid") from exc
        if not isinstance(payload, Mapping):
            raise StorageInvariantError("dashboard snapshot must be a mapping")
        return {"snapshot_ts_ms": row["snapshot_ts_ms"], **payload}

    def load_dashboard_read_model(self) -> DashboardReadModel:
        """Load one read-only accounting/telemetry view for the terminal UI."""
        db = self._connection()
        db.execute("BEGIN")
        try:
            result = self._load_dashboard_read_model(db)
            db.commit()
            return result
        except Exception:
            db.rollback()
            raise

    def _load_dashboard_read_model(self, db: sqlite3.Connection) -> DashboardReadModel:
        snapshot = self.load_dashboard_snapshot()
        experiment_hash = None if snapshot is None else snapshot.get("experiment_hash")
        if experiment_hash is not None and not isinstance(experiment_hash, str):
            raise StorageInvariantError("dashboard experiment hash is invalid")
        experiment_filter = "" if experiment_hash is None else " AND s.experiment_hash=?"
        args: tuple[Any, ...] = () if experiment_hash is None else (experiment_hash,)
        try:
            metadata = tuple(dict(row) for row in db.execute(
                "SELECT experiment_hash,market_id,symbol,slug FROM dashboard_market_metadata" +
                ("" if experiment_hash is None else " WHERE experiment_hash=?") +
                " ORDER BY market_id", args,
            ))
        except sqlite3.OperationalError as exc:
            if "no such table" not in str(exc).lower():
                raise
            metadata = ()
        entries = tuple(dict(row) for row in db.execute(
            """SELECT s.experiment_hash,s.market_id,s.threshold,s.confirmation,s.policy,s.side,
                      m.close_ts,o.status,o.requested_quote,o.requested_shares,
                      o.filled_shares,o.quote_amount,o.fee,
                      lr.net_pnl,lr.result_json,rr.payload_json AS reverse_json
               FROM signals s
               JOIN markets m ON m.experiment_hash=s.experiment_hash AND m.market_id=s.market_id
               JOIN paper_orders o ON o.signal_id=s.signal_id AND o.role='ENTRY'
               LEFT JOIN lane_results lr ON lr.experiment_hash=s.experiment_hash
                    AND lr.market_id=s.market_id AND lr.threshold=s.threshold
                    AND lr.confirmation=s.confirmation AND lr.policy=s.policy
               LEFT JOIN signals sr ON sr.experiment_hash=s.experiment_hash
                    AND sr.market_id=s.market_id AND sr.threshold=s.threshold
                    AND sr.confirmation=s.confirmation AND sr.policy=s.policy AND sr.phase='REVERSE'
               LEFT JOIN reverse_sequences rr ON rr.signal_id=sr.signal_id
               WHERE s.phase='ENTRY'""" + experiment_filter +
            " ORDER BY s.threshold,s.confirmation,s.policy,m.close_ts,s.market_id", args,
        ))
        inventory = tuple(dict(row) for row in db.execute(
            """SELECT experiment_hash,market_id,threshold,confirmation,policy,
                      token_id,side,shares,source
               FROM inventory_lots WHERE open=1""" +
            ("" if experiment_hash is None else " AND experiment_hash=?") +
            " ORDER BY market_id,threshold,confirmation,policy,lot_id", args,
        ))
        health = tuple(dict(row) for row in db.execute(
            "SELECT event_ts_ms,kind,payload_json FROM health_events ORDER BY id DESC LIMIT 30"
        ))
        return DashboardReadModel(snapshot, metadata, entries, inventory, health)


__all__ = [
    "DashboardReadModel", "DashboardSnapshot", "PersistedMarketState", "PersistedPosition", "Storage",
    "StorageInvariantError", "TABLES", "canonical_decimal",
]
