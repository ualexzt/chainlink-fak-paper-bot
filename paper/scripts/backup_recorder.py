from __future__ import annotations

import argparse
import hashlib
import json
import os
import stat
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

CHUNK_SIZE = 1024 * 1024
UTC = timezone.utc


class VerificationError(ValueError):
    pass


def _regular_files(root: Path) -> tuple[Path, ...]:
    root = root.absolute()
    if root.is_symlink() or not root.is_dir():
        raise VerificationError("backup root must be a real directory")
    found: list[Path] = []
    pending = [root]
    while pending:
        directory = pending.pop()
        with os.scandir(directory) as entries:
            for entry in entries:
                path = Path(entry.path)
                mode = entry.stat(follow_symlinks=False).st_mode
                if stat.S_ISLNK(mode):
                    raise VerificationError("symlinks are not allowed")
                if stat.S_ISDIR(mode):
                    pending.append(path)
                elif stat.S_ISREG(mode):
                    found.append(path)
                else:
                    raise VerificationError("special files are not allowed")
    return tuple(sorted(found, key=lambda path: path.relative_to(root).as_posix()))


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(CHUNK_SIZE):
            digest.update(chunk)
    return digest.hexdigest()


def _manifest(root: Path) -> dict[str, Any]:
    files = []
    for path in _regular_files(root):
        relative = path.relative_to(root).as_posix()
        size = path.stat().st_size
        files.append({"path": relative, "size": size, "sha256": _sha256(path)})
    return {"version": 1, "file_count": len(files), "total_bytes": sum(item["size"] for item in files), "files": files}


def _canonical_json(value: Mapping[str, Any]) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False) + "\n").encode()


def write_manifest(root: str | Path, output: str | Path) -> dict[str, Any]:
    root_path, output_path = Path(root).absolute(), Path(output).absolute()
    if output_path.exists() and output_path.is_symlink():
        raise VerificationError("manifest output symlink is not allowed")
    result = _manifest(root_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{output_path.name}.", dir=output_path.parent)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(_canonical_json(result))
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, output_path)
    return result


def _load_manifest(path: Path) -> Mapping[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise VerificationError("manifest cannot be read") from exc
    if not isinstance(value, Mapping) or value.get("version") != 1 or not isinstance(value.get("files"), list):
        raise VerificationError("manifest schema is invalid")
    return value


def verify(root: str | Path, manifest: str | Path, archive: str | Path) -> dict[str, Any]:
    root_path, manifest_path, archive_path = Path(root).absolute(), Path(manifest).absolute(), Path(archive).absolute()
    expected = _load_manifest(manifest_path)
    entries = expected["files"]
    if any(not isinstance(item, Mapping) or not isinstance(item.get("path"), str)
           or Path(item["path"]).is_absolute() or ".." in Path(item["path"]).parts
           or not isinstance(item.get("size"), int) or item["size"] < 0
           or not isinstance(item.get("sha256"), str) or len(item["sha256"]) != 64
           for item in entries):
        raise VerificationError("manifest file entry is invalid")
    actual = _manifest(root_path)
    if actual != dict(expected):
        raise VerificationError("extracted files do not match manifest")
    if archive_path.is_symlink() or not archive_path.is_file():
        raise VerificationError("archive must be a regular file")
    result = {"archive": archive_path.name, "archive_sha256": _sha256(archive_path),
              "file_count": actual["file_count"], "total_bytes": actual["total_bytes"], "verified": True}
    return result


def backup_destination(base: str | Path, at: datetime | None = None) -> Path:
    moment = datetime.now(UTC) if at is None else at
    if moment.tzinfo is None:
        raise ValueError("backup timestamp must be timezone-aware")
    stamp = moment.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")
    return Path(base) / "backups" / "recorder" / stamp


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    manifest_parser = subparsers.add_parser("manifest")
    manifest_parser.add_argument("--root", required=True)
    manifest_parser.add_argument("--output", required=True)
    verify_parser = subparsers.add_parser("verify")
    verify_parser.add_argument("--root", required=True)
    verify_parser.add_argument("--manifest", required=True)
    verify_parser.add_argument("--archive", required=True)
    args = parser.parse_args(argv)
    try:
        result = (write_manifest(args.root, args.output) if args.command == "manifest"
                  else verify(args.root, args.manifest, args.archive))
        print(json.dumps(result, sort_keys=True, separators=(",", ":")))
        return 0
    except (OSError, VerificationError, ValueError):
        print("backup verification failed", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
