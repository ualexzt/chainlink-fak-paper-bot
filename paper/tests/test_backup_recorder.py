from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import tarfile
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("backup_recorder", ROOT / "scripts" / "backup_recorder.py")
backup = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(backup)


class BackupRecorderTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "recorder" / "data"
        self.root.mkdir(parents=True)
        (self.root / "b.jsonl").write_text("b\n", encoding="utf-8")
        nested = self.root / "nested"
        nested.mkdir()
        (nested / "a.jsonl").write_bytes(b"a\x00\n")

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_manifest_is_sorted_canonical_and_stream_hashed(self) -> None:
        output = Path(self.temp.name) / "manifest.json"
        result = backup.write_manifest(self.root, output)
        self.assertEqual([item["path"] for item in result["files"]], ["b.jsonl", "nested/a.jsonl"])
        self.assertEqual(result["file_count"], 2)
        self.assertEqual(result["total_bytes"], 5)
        self.assertEqual(json.loads(output.read_text()), result)
        self.assertEqual(output.read_text(), json.dumps(result, sort_keys=True, separators=(",", ":")) + "\n")

    def test_symlinks_and_non_directories_are_rejected(self) -> None:
        outside = Path(self.temp.name) / "outside"
        outside.write_text("secret", encoding="utf-8")
        os.symlink(outside, self.root / "escape")
        with self.assertRaisesRegex(backup.VerificationError, "symlinks"):
            backup.write_manifest(self.root, Path(self.temp.name) / "manifest.json")

    def test_verify_archive_hash_and_exact_extracted_reconciliation(self) -> None:
        manifest_path = Path(self.temp.name) / "manifest.json"
        manifest = backup.write_manifest(self.root, manifest_path)
        archive = Path(self.temp.name) / "recorder-data.tar"
        with tarfile.open(archive, "w") as tar:
            tar.add(self.root, arcname="data")
        result = backup.verify(self.root, manifest_path, archive)
        self.assertTrue(result["verified"])
        self.assertEqual(result["archive_sha256"], hashlib.sha256(archive.read_bytes()).hexdigest())
        self.assertEqual(result["file_count"], manifest["file_count"])
        (self.root / "b.jsonl").write_text("changed\n", encoding="utf-8")
        with self.assertRaisesRegex(backup.VerificationError, "match manifest"):
            backup.verify(self.root, manifest_path, archive)

    def test_manifest_path_escape_and_archive_symlink_are_rejected(self) -> None:
        manifest_path = Path(self.temp.name) / "manifest.json"
        backup.write_manifest(self.root, manifest_path)
        value = json.loads(manifest_path.read_text())
        value["files"][0]["path"] = "../escape"
        manifest_path.write_text(json.dumps(value), encoding="utf-8")
        archive = Path(self.temp.name) / "archive.tar"
        archive.write_bytes(b"archive")
        with self.assertRaisesRegex(backup.VerificationError, "manifest file entry"):
            backup.verify(self.root, manifest_path, archive)

    def test_destination_uses_utc_timestamp_and_script_never_removes_source(self) -> None:
        destination = backup.backup_destination(
            self.temp.name, datetime(2026, 9, 1, 1, 2, 3, tzinfo=timezone.utc)
        )
        self.assertEqual(destination, Path(self.temp.name) / "backups/recorder/20260901T010203Z")
        before = sorted(path.relative_to(self.root) for path in self.root.rglob("*"))
        self.assertTrue(backup.main(["manifest", "--root", str(self.root), "--output", str(Path(self.temp.name) / "m.json")]) == 0)
        after = sorted(path.relative_to(self.root) for path in self.root.rglob("*"))
        self.assertEqual(before, after)
        source = (ROOT / "scripts" / "backup_recorder.py").read_text()
        self.assertNotIn("rmtree", source)
        self.assertNotIn("unlink(", source)


if __name__ == "__main__":
    unittest.main()
