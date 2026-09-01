from __future__ import annotations

import json
import io
import shutil
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

import zstandard as zstd

from paper_bot.journal import JournalError, RawEvent, RawJournal

UTC = timezone.utc
DAY_1 = datetime(2026, 8, 31, 23, 59, 59, tzinfo=UTC)
DAY_2 = DAY_1 + timedelta(seconds=1)


def event(at: datetime = DAY_1, **changes):
    base = RawEvent(
        source="market_ws", receive_ts_ms=int(at.timestamp() * 1000),
        source_ts_ms=int(at.timestamp() * 1000) - 2, symbol="btc",
        token_id="public-token",
        payload={"price": Decimal("0.8000"), "levels": [1, "two"]},
    )
    return replace(base, **changes)


class JournalTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.journal = RawJournal(self.root, min_free_bytes=0)

    def tearDown(self):
        self.journal.close()
        self.temp.cleanup()

    def rows(self, path=None):
        target = path or next(self.root.glob("*.jsonl"))
        return [json.loads(line) for line in target.read_text().splitlines()]

    def test_canonical_public_row_is_flushed_and_complete(self):
        self.journal.append(event())
        path = next(self.root.glob("*.jsonl"))
        raw = path.read_text()
        self.assertEqual(raw.count("\n"), 1)
        self.assertNotIn(" ", raw)
        row = self.rows(path)[0]
        self.assertEqual(
            set(row), {"payload", "receive_ts_ms", "source", "source_ts_ms", "symbol", "token_id"}
        )
        self.assertEqual(row["payload"]["price"], "0.8")

    def test_restart_appends_to_open_utc_day_without_corruption(self):
        self.journal.append(event())
        self.journal.close()
        self.journal = RawJournal(self.root, min_free_bytes=0)
        self.journal.append(event(payload={"sequence": 2}))
        self.assertEqual([row["payload"] for row in self.rows()], [
            {"levels": [1, "two"], "price": "0.8"}, {"sequence": 2}
        ])

    def test_rotation_is_utc_and_archive_round_trips_exact_bytes(self):
        self.journal.append(event())
        source = next(self.root.glob("*.jsonl"))
        expected = source.read_bytes()
        self.journal.rotate_if_needed(DAY_2.astimezone(timezone(timedelta(hours=3))))
        archive = source.with_suffix(".jsonl.zst")
        self.assertFalse(source.exists())
        with zstd.ZstdDecompressor().stream_reader(io.BytesIO(archive.read_bytes())) as reader:
            self.assertEqual(reader.read(), expected)
        self.assertFalse(tuple(self.root.glob("*.tmp")))

    def test_append_on_next_day_rotates_then_opens_new_day(self):
        self.journal.append(event())
        self.journal.append(event(DAY_2, payload={"day": 2}))
        self.assertTrue((self.root / "raw-events-2026-08-31.jsonl.zst").exists())
        self.assertEqual(self.rows(self.root / "raw-events-2026-09-01.jsonl")[0]["payload"], {"day": 2})

    def test_restart_compresses_abandoned_prior_day(self):
        self.journal.append(event())
        self.journal.close()
        self.journal = RawJournal(self.root, min_free_bytes=0)
        self.journal.rotate_if_needed(DAY_2)
        self.assertTrue((self.root / "raw-events-2026-08-31.jsonl.zst").exists())

    def test_rotation_failure_is_sticky_and_preserves_source(self):
        states = []
        self.journal.close()
        self.journal = RawJournal(self.root, min_free_bytes=0, on_critical=states.append)
        self.journal.append(event())
        source = next(self.root.glob("*.jsonl"))
        expected = source.read_bytes()
        with patch("paper_bot.journal.os.replace", side_effect=OSError("injected")):
            with self.assertRaisesRegex(JournalError, "rotation"):
                self.journal.rotate_if_needed(DAY_2)
        self.assertEqual(source.read_bytes(), expected)
        self.assertFalse(tuple(self.root.glob("*.zst")))
        self.assertFalse(tuple(self.root.glob("*.tmp")))
        self.assertFalse(self.journal.writable())
        self.assertEqual([state.reason for state in states], ["journal_rotation_failed"])

    def test_restart_recovers_rename_before_source_delete_window(self):
        self.journal.append(event())
        source = next(self.root.glob("*.jsonl"))
        expected = source.read_bytes()
        self.journal.rotate_if_needed(DAY_2)
        source.write_bytes(expected)
        archive = source.with_suffix(".jsonl.zst")
        original_archive = archive.read_bytes()
        self.journal.close()
        self.journal = RawJournal(self.root, min_free_bytes=0)
        self.journal.rotate_if_needed(DAY_2)
        self.assertFalse(source.exists())
        self.assertEqual(archive.read_bytes(), original_archive)

    def test_write_failure_is_sticky_and_preserves_complete_prior_rows(self):
        states = []
        self.journal.close()
        self.journal = RawJournal(self.root, min_free_bytes=0, on_critical=states.append)
        self.journal.append(event())
        actual_file = self.journal._file
        assert actual_file is not None

        class FailingWriter:
            def write(self, _value):
                raise OSError("injected sensitive write detail")

            def flush(self):
                actual_file.flush()

            def fileno(self):
                return actual_file.fileno()

            def close(self):
                actual_file.close()

        self.journal._file = FailingWriter()
        with self.assertRaisesRegex(JournalError, "write_failed"):
            self.journal.append(event(payload={"sequence": 2}))
        self.assertFalse(self.journal.writable())
        self.assertEqual([state.reason for state in states], ["journal_write_failed"])
        self.assertEqual(len(self.rows()), 1)
        with self.assertRaisesRegex(JournalError, "write_failed"):
            self.journal.append(event(payload={"sequence": 3}))

    def test_low_disk_boundary_and_state_are_sticky(self):
        free = [100]
        states = []
        usage = lambda _path: shutil._ntuple_diskusage(1000, 900, free[0])
        self.journal.close()
        self.journal = RawJournal(self.root, min_free_bytes=100, disk_usage=usage, on_critical=states.append)
        self.assertEqual(self.journal.disk_free_bytes(), 100)
        self.assertTrue(self.journal.writable())
        free[0] = 99
        self.assertFalse(self.journal.writable())
        free[0] = 1000
        self.assertFalse(self.journal.writable())
        self.assertEqual([state.reason for state in states], ["journal_low_disk"])
        with self.assertRaisesRegex(JournalError, "low_disk"):
            self.journal.append(event())
        self.assertFalse(tuple(self.root.glob("*.jsonl")))

    def test_disk_check_error_is_critical_without_leaking_detail(self):
        states = []
        self.journal.close()
        self.journal = RawJournal(
            self.root, min_free_bytes=0,
            disk_usage=lambda _path: (_ for _ in ()).throw(OSError("sensitive detail")),
            on_critical=states.append,
        )
        self.assertFalse(self.journal.writable())
        self.assertIsNone(self.journal.disk_free_bytes())
        self.assertEqual(states[0].reason, "journal_disk_check_failed")
        self.assertNotIn("sensitive", repr(states[0]))

    def test_credential_like_keys_fail_before_any_write(self):
        for key in ("PRIVATE_KEY", "api-key", "ApiSecret", "Authorization", "Cookie"):
            with self.subTest(key=key):
                with self.assertRaisesRegex(ValueError, "sensitive"):
                    self.journal.append(event(payload={"nested": [{key: "must-not-appear"}]}))
        self.assertFalse(tuple(self.root.iterdir()))

    def test_credential_like_values_fail_before_any_write(self):
        for value in ("Bearer abc", "Basic abc", "-----BEGIN PRIVATE KEY-----"):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, "sensitive"):
                self.journal.append(event(payload={"value": value}))
        self.assertFalse(tuple(self.root.iterdir()))

    def test_unknown_top_level_fields_and_unsafe_values_fail_closed(self):
        raw = event().__dict__ | {"environment": {"PATH": "never serialize"}}
        with self.assertRaisesRegex(ValueError, "exactly"):
            self.journal.append(raw)
        for payload in ({1: "non-string key"}, {"value": object()}, {"value": float("nan")}):
            with self.subTest(payload=payload), self.assertRaises((TypeError, ValueError)):
                self.journal.append(event(payload=payload))
        self.assertFalse(tuple(self.root.iterdir()))

    def test_out_of_range_receive_timestamp_fails_before_write(self):
        with self.assertRaisesRegex(ValueError, "supported range"):
            self.journal.append(event(receive_ts_ms=10**30))
        self.assertTrue(self.journal.writable())
        self.assertFalse(tuple(self.root.iterdir()))

    def test_seven_days_rotate_without_retention_deletion(self):
        for offset in range(8):
            current = DAY_1 + timedelta(days=offset)
            self.journal.append(event(current, payload={"day": offset}))
        self.journal.close()
        self.assertEqual(len(tuple(self.root.glob("*.jsonl.zst"))), 7)
        self.assertEqual(len(tuple(self.root.glob("*.jsonl"))), 1)


if __name__ == "__main__":
    unittest.main()
