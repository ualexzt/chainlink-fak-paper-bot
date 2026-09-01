from __future__ import annotations

import json
import math
import os
import shutil
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal
from enum import Enum
from pathlib import Path
from typing import Any, TextIO

import zstandard as zstd

UTC = timezone.utc
_SENSITIVE_KEY_PARTS = (
    "PRIVATE" + "_KEY", "API" + "_KEY", "API" + "_SECRET",
    "PASS" + "PHRASE", "CREDEN" + "TIAL",
    "AUTHORIZATION", "COOKIE",
)


class JournalError(RuntimeError):
    """A durable journal operation failed and entry creation must pause."""


@dataclass(frozen=True)
class RawEvent:
    source: str
    receive_ts_ms: int
    source_ts_ms: int | None
    symbol: str | None
    token_id: str | None
    payload: Any


@dataclass(frozen=True)
class JournalCriticalState:
    reason: str


def _canonical_decimal(value: Decimal) -> str:
    if not value.is_finite():
        raise ValueError("journal Decimal must be finite")
    if value == 0:
        return "0"
    text = format(value, "f")
    return text.rstrip("0").rstrip(".") if "." in text else text


def _sensitive_key(key: str) -> bool:
    normalized = key.upper().replace("-", "_").replace(" ", "_")
    compact = normalized.replace("_", "")
    return any(part.replace("_", "") in compact for part in _SENSITIVE_KEY_PARTS)


def _canonical(value: Any) -> Any:
    if isinstance(value, str):
        lowered = value.lstrip().lower()
        if lowered.startswith(("bearer ", "basic ")) or "private key-----" in lowered:
            raise ValueError("sensitive journal value is forbidden")
        return value
    if value is None or isinstance(value, (int, bool)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("journal float must be finite")
        return value
    if isinstance(value, Decimal):
        return _canonical_decimal(value)
    if isinstance(value, Enum):
        return _canonical(value.value)
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise TypeError("journal payload keys must be strings")
            if _sensitive_key(key):
                raise ValueError("sensitive journal key is forbidden")
            result[key] = _canonical(item)
        return result
    if isinstance(value, (list, tuple)):
        return [_canonical(item) for item in value]
    raise TypeError(f"unsupported journal value type: {type(value).__name__}")


def _integer_timestamp(value: Any, name: str, *, optional: bool = False) -> int | None:
    if optional and value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{name} must be a nonnegative integer")
    return value


def _event_row(event: RawEvent | Mapping[str, Any]) -> dict[str, Any]:
    if isinstance(event, RawEvent):
        raw = {
            "source": event.source,
            "receive_ts_ms": event.receive_ts_ms,
            "source_ts_ms": event.source_ts_ms,
            "symbol": event.symbol,
            "token_id": event.token_id,
            "payload": event.payload,
        }
    elif isinstance(event, Mapping):
        allowed = {"source", "receive_ts_ms", "source_ts_ms", "symbol", "token_id", "payload"}
        if set(event) != allowed:
            raise ValueError("journal event must contain exactly the public event fields")
        raw = dict(event)
    else:
        raise TypeError("event must be RawEvent or a mapping")

    source, symbol, token_id = raw["source"], raw["symbol"], raw["token_id"]
    if not isinstance(source, str) or not source:
        raise ValueError("journal source must be nonempty")
    if symbol is not None and (not isinstance(symbol, str) or not symbol):
        raise ValueError("journal symbol must be nonempty or None")
    if token_id is not None and (not isinstance(token_id, str) or not token_id):
        raise ValueError("journal token_id must be nonempty or None")
    if symbol is None and token_id is None:
        raise ValueError("journal event requires symbol or token_id")
    return {
        "payload": _canonical(raw["payload"]),
        "receive_ts_ms": _integer_timestamp(raw["receive_ts_ms"], "receive_ts_ms"),
        "source": source,
        "source_ts_ms": _integer_timestamp(raw["source_ts_ms"], "source_ts_ms", optional=True),
        "symbol": symbol,
        "token_id": token_id,
    }


class RawJournal:
    def __init__(
        self,
        root: str | Path,
        *,
        min_free_bytes: int = 100 * 1024 * 1024,
        on_critical: Callable[[JournalCriticalState], None] | None = None,
        disk_usage: Callable[[str | os.PathLike[str]], Any] = shutil.disk_usage,
    ) -> None:
        if isinstance(min_free_bytes, bool) or not isinstance(min_free_bytes, int) or min_free_bytes < 0:
            raise ValueError("min_free_bytes must be a nonnegative integer")
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.min_free_bytes = min_free_bytes
        self._on_critical = on_critical
        self._disk_usage = disk_usage
        self._critical: JournalCriticalState | None = None
        self._day: date | None = None
        self._file: TextIO | None = None

    @property
    def critical_state(self) -> JournalCriticalState | None:
        return self._critical

    def _fail(self, reason: str) -> JournalError:
        if self._critical is None:
            self._critical = JournalCriticalState(reason)
            if self._on_critical is not None:
                try:
                    self._on_critical(self._critical)
                except Exception:
                    pass
        return JournalError(reason)

    def _path(self, day: date) -> Path:
        return self.root / f"raw-events-{day.isoformat()}.jsonl"

    @staticmethod
    def _day_from_path(path: Path) -> date | None:
        prefix, suffix = "raw-events-", ".jsonl"
        if not path.name.startswith(prefix) or not path.name.endswith(suffix):
            return None
        try:
            return date.fromisoformat(path.name[len(prefix):-len(suffix)])
        except ValueError:
            return None

    def writable(self) -> bool:
        if self._critical is not None:
            return False
        try:
            free = self._disk_usage(self.root).free
        except OSError:
            self._fail("journal_disk_check_failed")
            return False
        if free < self.min_free_bytes:
            self._fail("journal_low_disk")
            return False
        return True

    def _open(self, day: date) -> None:
        if self._file is not None and self._day == day:
            return
        if self._file is not None:
            raise JournalError("journal day transition requires rotation")
        self._file = self._path(day).open("a", encoding="utf-8", buffering=1)
        self._day = day

    def append(self, event: RawEvent | Mapping[str, Any]) -> None:
        row = _event_row(event)
        if not self.writable():
            raise JournalError(self._critical.reason if self._critical else "journal_not_writable")
        try:
            event_day = datetime.fromtimestamp(row["receive_ts_ms"] / 1000, UTC).date()
        except (OverflowError, OSError, ValueError) as exc:
            raise ValueError("receive_ts_ms is outside the supported range") from exc
        try:
            self.rotate_if_needed(datetime.combine(event_day, datetime.min.time(), tzinfo=UTC))
            if self._day is not None and event_day < self._day:
                raise ValueError("journal receive day cannot move backwards")
            self._open(event_day)
            assert self._file is not None
            line = json.dumps(row, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False)
            self._file.write(line + "\n")
            self._file.flush()
        except (TypeError, ValueError):
            raise
        except Exception as exc:
            raise self._fail("journal_write_failed") from exc

    def _fsync_directory(self) -> None:
        descriptor = os.open(self.root, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def _compress(self, source: Path) -> None:
        archive = source.with_suffix(source.suffix + ".zst")
        temporary = archive.with_suffix(archive.suffix + ".tmp")
        if archive.exists():
            if not self._archive_matches_source(archive, source):
                raise JournalError("journal archive conflicts with source")
            source.unlink()
            self._fsync_directory()
            return
        try:
            with source.open("rb") as source_file, temporary.open("xb") as destination:
                with zstd.ZstdCompressor().stream_writer(destination, closefd=False) as writer:
                    shutil.copyfileobj(source_file, writer)
                destination.flush()
                os.fsync(destination.fileno())
            os.replace(temporary, archive)
            self._fsync_directory()
            source.unlink()
            self._fsync_directory()
        except Exception:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
            raise

    @staticmethod
    def _archive_matches_source(archive: Path, source: Path) -> bool:
        with (
            archive.open("rb") as compressed,
            zstd.ZstdDecompressor().stream_reader(compressed) as reader,
            source.open("rb") as uncompressed,
        ):
            while True:
                archived_chunk = reader.read(1024 * 1024)
                source_chunk = uncompressed.read(1024 * 1024)
                if archived_chunk != source_chunk:
                    return False
                if not archived_chunk:
                    return True

    def rotate_if_needed(self, now: datetime) -> None:
        if not isinstance(now, datetime):
            raise TypeError("now must be datetime")
        current_day = (now.astimezone(UTC) if now.tzinfo else now.replace(tzinfo=UTC)).date()
        try:
            if self._file is not None and self._day is not None and self._day < current_day:
                self._file.flush()
                os.fsync(self._file.fileno())
                self._file.close()
                self._file = None
            candidates = sorted(
                path for path in self.root.glob("raw-events-*.jsonl")
                if (day := self._day_from_path(path)) is not None and day < current_day
            )
            for source in candidates:
                self._compress(source)
            if self._day is not None and self._day < current_day:
                self._day = None
        except Exception as exc:
            raise self._fail("journal_rotation_failed") from exc

    def close(self) -> None:
        if self._file is None:
            return
        try:
            self._file.flush()
            os.fsync(self._file.fileno())
            self._file.close()
        except Exception as exc:
            raise self._fail("journal_close_failed") from exc
        finally:
            self._file = None

    def __enter__(self) -> RawJournal:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


__all__ = ["JournalCriticalState", "JournalError", "RawEvent", "RawJournal"]
