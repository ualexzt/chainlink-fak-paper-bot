from __future__ import annotations

import ast
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

FORBIDDEN_FILENAME_PATTERNS = (
    re.compile(r"(?i)(post_order|create_order|create_market_order|user[-_]?ws|private_key|api_key|api_secret|passphrase|credential)"),
)
FORBIDDEN_LINE_PATTERNS = (
    ("forbidden-assignment", re.compile(r"(?i)(?:private_key|api_key|api_secret|passphrase|credential).*[:=]")),
    ("credential-field-name", re.compile(r"(?i)(?:private_key|api_key|api_secret|passphrase|credential)")),
    ("order-endpoint", re.compile(r"(?i)\bpost\b.*?/order\b")),
    ("order-call-path", re.compile(r"(?i)\.\s*(?:request|post|put|patch|delete)\s*\([^#\n]*?/order")),
    ("order-client-call", re.compile(r"\b(?:post_order|create_order|create_market_order)\b")),
    ("authenticated-user-websocket", re.compile(r"(?i)(?:/ws/user|/user/ws|authenticated\s+user\s+websocket|user.*websocket|websocket.*user)")),
)
AIOHTTP_METHODS = {"request", "post", "put", "patch", "delete"}
ORDER_PATH_PATTERN = re.compile(r"(?i)\b(?:https?://[^\s'\"]+)?/order\b")


@dataclass(frozen=True)
class Finding:
    path: Path
    line: int
    rule: str


def iter_files(target: Path) -> Iterable[Path]:
    if target.is_file():
        yield target
        return
    scanner = Path(__file__).resolve()
    excluded_dirs = {".git", "__pycache__", "runtime", "tests"}
    for path in sorted(p for p in target.rglob("*") if p.is_file()):
        try:
            relative_parts = path.relative_to(target).parts[:-1]
        except ValueError:
            relative_parts = path.parts[:-1]
        if (path.resolve() == scanner or path.suffix.lower() in {".md", ".markdown"}
                or any(part in excluded_dirs for part in relative_parts)):
            continue
        yield path


def scan_filename(path: Path) -> list[Finding]:
    findings: list[Finding] = []
    as_text = str(path)
    for pattern in FORBIDDEN_FILENAME_PATTERNS:
        if pattern.search(as_text):
            findings.append(Finding(path=path, line=0, rule="forbidden-filename"))
            break
    return findings


def scan_text(path: Path, text: str) -> list[Finding]:
    findings: list[Finding] = []
    allowed_config_lines = {
        'FORBIDDEN_ENV_PARTS = ("PRIVATE_KEY", "API_KEY", "API_SECRET", "PASSPHRASE", "CREDENTIAL")',
        'raise ValueError("forbidden credential environment keys: " + ",".join(forbidden))',
    }
    for line_number, line in enumerate(text.splitlines(), start=1):
        stripped = line.strip()
        if path.name == "config.py" and path.parent.name == "paper_bot" and stripped in allowed_config_lines:
            continue
        for rule, pattern in FORBIDDEN_LINE_PATTERNS:
            if pattern.search(line):
                findings.append(Finding(path=path, line=line_number, rule=rule))
    return findings


def _iter_string_literals(node: ast.AST) -> Iterable[str]:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        yield node.value
        return
    if isinstance(node, ast.JoinedStr):
        parts = []
        for value in node.values:
            if isinstance(value, ast.Constant) and isinstance(value.value, str):
                parts.append(value.value)
            else:
                return
        if parts:
            yield "".join(parts)


def scan_ast(path: Path, text: str) -> list[Finding]:
    findings: list[Finding] = []
    if path.suffix != ".py":
        return findings
    try:
        tree = ast.parse(text, filename=str(path))
    except SyntaxError as exc:
        findings.append(Finding(path=path, line=getattr(exc, "lineno", 0) or 0, rule="syntax-error"))
        return findings

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not isinstance(func, ast.Attribute) or func.attr not in AIOHTTP_METHODS:
            continue

        string_literals = []
        for arg in node.args:
            string_literals.extend(_iter_string_literals(arg))
        for keyword in node.keywords:
            if keyword.value is not None:
                string_literals.extend(_iter_string_literals(keyword.value))

        if string_literals and any(ORDER_PATH_PATTERN.search(value) for value in string_literals):
            findings.append(Finding(path=path, line=getattr(node, "lineno", 0), rule="order-endpoint-call"))
            continue

        findings.append(Finding(path=path, line=getattr(node, "lineno", 0), rule="aiohttp-client-session-method"))
    return findings


def scan_path(target: Path) -> list[Finding]:
    findings: list[Finding] = []
    if not target.exists():
        findings.append(Finding(path=target, line=0, rule="missing-target"))
        return findings
    for path in iter_files(target):
        findings.extend(scan_filename(path))
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            if path.suffix == ".pyc":
                continue
            if path.suffix == ".py":
                findings.append(Finding(path=path, line=0, rule="unreadable-python-source"))
            else:
                findings.append(Finding(path=path, line=0, rule="unreadable-source"))
            continue
        findings.extend(scan_text(path, text))
        findings.extend(scan_ast(path, text))
    return findings


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args:
        print("usage: security_scan.py <path> [<path> ...]", file=sys.stderr)
        return 2

    findings: list[Finding] = []
    for raw in args:
        findings.extend(scan_path(Path(raw)))

    findings.sort(key=lambda item: (str(item.path), item.line, item.rule))
    for finding in findings:
        print(f"{finding.path}:{finding.line}:{finding.rule}")
    return 1 if findings else 0


if __name__ == "__main__":
    raise SystemExit(main())
