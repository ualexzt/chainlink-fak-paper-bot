import subprocess
import sys
import tempfile
import unittest
import importlib.util
import os
import sqlite3
import time
from decimal import Decimal
from pathlib import Path

from paper_bot.config import load_settings


class ConfigSafetyTests(unittest.TestCase):
    def test_default_settings_define_fixed_experiment(self):
        settings = load_settings({})
        self.assertEqual(settings.symbols, ("btc", "eth", "sol"))
        self.assertEqual(settings.thresholds, tuple(map(Decimal, ("0.80", "0.85", "0.89", "0.90"))))
        self.assertEqual(settings.paper_notional_usd, Decimal("5.00"))
        self.assertEqual(settings.rtds_stale_seconds, Decimal("10"))

    def test_forbidden_credentials_fail_closed(self):
        with self.assertRaisesRegex(ValueError, "forbidden credential"):
            load_settings({"PRIVATE_KEY": "not-allowed"})

    def test_only_public_endpoints_are_configurable(self):
        settings = load_settings({})
        self.assertEqual(settings.gamma_url, "https://gamma-api.polymarket.com")
        self.assertEqual(settings.market_ws_url, "wss://ws-subscriptions-clob.polymarket.com/ws/market")
        self.assertEqual(settings.rtds_url, "wss://ws-live-data.polymarket.com")


class PackagingRuntimeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.repo_root = Path(__file__).resolve().parents[2]
        cls.paper = cls.repo_root / "paper"

    def test_example_environment_is_exactly_public_settings(self):
        lines = (self.paper / ".env.example").read_text().splitlines()
        self.assertEqual(lines, [
            "SYMBOLS=btc,eth,sol",
            "ENTRY_THRESHOLDS=0.80,0.85,0.89,0.90",
            "PAPER_NOTIONAL_USD=5.00",
            "RTDS_STALE_SEC=10",
            "DATA_DIR=/data",
            "LOG_LEVEL=INFO",
        ])

    def test_compose_has_one_safe_bounded_non_secret_daemon(self):
        text = (self.paper / "docker-compose.yml").read_text()
        service_names = [line.strip()[:-1] for line in text.splitlines()
                         if line.startswith("  ") and not line.startswith("    ") and line.rstrip().endswith(":")]
        self.assertEqual(service_names, ["paper-engine"])
        self.assertIn("source: ./runtime", text)
        self.assertIn("target: /data", text)
        self.assertIn("restart: unless-stopped", text)
        self.assertIn("driver: json-file", text)
        self.assertIn('max-size: "10m"', text)
        self.assertIn('max-file: "3"', text)
        self.assertIn("healthcheck.py", text)
        self.assertIn("--max-age", text)
        lowered = text.lower()
        for forbidden in ("private_key", "api_key", "api_secret", "passphrase", "credential",
                          "docker.sock", "wallet", ".ssh", ".env:"):
            self.assertNotIn(forbidden, lowered)

    def test_image_runs_as_non_root_and_contains_only_paper_runtime(self):
        dockerfile = (self.paper / "Dockerfile").read_text()
        users = [line.split(maxsplit=1)[1] for line in dockerfile.splitlines() if line.startswith("USER ")]
        self.assertEqual(users, ["paper"])
        self.assertIn("COPY --chown=paper:paper paper_bot /app/paper_bot", dockerfile)
        self.assertIn("COPY --chown=paper:paper tests /app/tests", dockerfile)
        self.assertNotIn("USER root", dockerfile)
        ignore = (self.paper / ".dockerignore").read_text()
        self.assertIn(".env", ignore)
        self.assertIn("runtime/", ignore)

    def test_wrappers_are_executable_exact_and_expose_no_trade_operation(self):
        manager = self.paper / "scripts" / "paper-bot"
        watcher = self.paper / "scripts" / "paper-watch"
        self.assertTrue(os.access(manager, os.X_OK))
        self.assertTrue(os.access(watcher, os.X_OK))
        manager_text, watcher_text = manager.read_text(), watcher.read_text()
        self.assertIn("set -euo pipefail", manager_text)
        self.assertIn('COMPOSE_FILE="${SCRIPT_DIR}/../docker-compose.yml"', manager_text)
        self.assertEqual(
            {line.strip()[:-1] for line in manager_text.splitlines()
             if line.startswith("  ") and line.strip().endswith(")") and line.strip() != "*)"},
            {"start", "stop", "status", "logs"},
        )
        rejected = subprocess.run([str(manager), "trade"], capture_output=True, text=True, check=False)
        self.assertEqual(rejected.returncode, 2)
        self.assertNotIn("docker ", rejected.stdout + rejected.stderr)
        self.assertIn("set -euo pipefail", watcher_text)
        self.assertIn('COMPOSE_FILE="${SCRIPT_DIR}/../docker-compose.yml"', watcher_text)
        self.assertIn("python -m paper_bot.cli watch --db /data/paper.db", watcher_text)

    def test_healthcheck_requires_fresh_database_write_progress_in_mode_ro(self):
        script = self.paper / "scripts" / "healthcheck.py"
        spec = importlib.util.spec_from_file_location("paper_healthcheck", script)
        module = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        spec.loader.exec_module(module)
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "paper.db"
            db = sqlite3.connect(path)
            db.execute("CREATE TABLE dashboard_snapshots(snapshot_id INTEGER PRIMARY KEY,snapshot_ts_ms INTEGER,payload_json TEXT)")
            now_ms = time.time_ns() // 1_000_000
            db.execute("INSERT INTO dashboard_snapshots VALUES (1,?,?)", (now_ms, "{}"))
            db.commit()
            db.close()
            before = path.read_bytes()
            self.assertTrue(module.healthy(path, 10, now_ms=now_ms + 9_999))
            self.assertFalse(module.healthy(path, 10, now_ms=now_ms + 10_001))
            self.assertFalse(module.healthy(path, 10, now_ms=now_ms - 1))
            self.assertEqual(before, path.read_bytes())


class SecurityScanTests(unittest.TestCase):
    def test_security_scan_passes_on_paper_bot_package(self):
        repo_root = Path(__file__).resolve().parents[2]
        result = subprocess.run(
            [sys.executable, "paper/scripts/security_scan.py", "paper/paper_bot"],
            cwd=repo_root,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, msg=f"stdout={result.stdout!r} stderr={result.stderr!r}")

    def test_security_scan_rejects_filename_credential_substrings_without_echoing_content(self):
        repo_root = Path(__file__).resolve().parents[2]
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            (tmp_path / "private_key.py").write_text("x = 1\n", encoding="utf-8")
            (tmp_path / "api_secret.txt").write_text("x = 1\n", encoding="utf-8")
            result = subprocess.run(
                [sys.executable, "paper/scripts/security_scan.py", str(tmp_path)],
                cwd=repo_root,
                capture_output=True,
                text=True,
                check=False,
            )
        self.assertNotEqual(result.returncode, 0)
        combined = result.stdout + result.stderr
        self.assertIn("forbidden-filename", combined)
        self.assertNotIn("x = 1", combined)

    def test_security_scan_rejects_malformed_python_order_call_without_echoing_content(self):
        repo_root = Path(__file__).resolve().parents[2]
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            sample = tmp_path / "danger.py"
            sample.write_text('client.post("/order")\nif True:\n', encoding="utf-8")
            result = subprocess.run(
                [sys.executable, "paper/scripts/security_scan.py", str(tmp_path)],
                cwd=repo_root,
                capture_output=True,
                text=True,
                check=False,
            )
        self.assertNotEqual(result.returncode, 0)
        combined = result.stdout + result.stderr
        self.assertNotIn('client.post("/order")', combined)

    def test_security_scan_rejects_marker_prefixed_credentials_without_echoing_content(self):
        repo_root = Path(__file__).resolve().parents[2]
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            sample = tmp_path / "danger.py"
            secret = "SECRET12345"
            sample.write_text(f'FORBIDDEN_ENV_PARTS = 1; PRIVATE_KEY = "{secret}"\n', encoding="utf-8")
            result = subprocess.run(
                [sys.executable, "paper/scripts/security_scan.py", str(tmp_path)],
                cwd=repo_root,
                capture_output=True,
                text=True,
                check=False,
            )
        self.assertNotEqual(result.returncode, 0)
        combined = result.stdout + result.stderr
        self.assertNotIn(secret, combined)

    def test_security_scan_rejects_comment_hidden_credentials_without_echoing_content(self):
        repo_root = Path(__file__).resolve().parents[2]
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            sample = tmp_path / "danger.py"
            secret = "SECRET12345"
            sample.write_text(f'PRIVATE_KEY={secret}  # FORBIDDEN_ENV_PARTS\n', encoding="utf-8")
            result = subprocess.run(
                [sys.executable, "paper/scripts/security_scan.py", str(tmp_path)],
                cwd=repo_root,
                capture_output=True,
                text=True,
                check=False,
            )
        self.assertNotEqual(result.returncode, 0)
        combined = result.stdout + result.stderr
        self.assertNotIn(secret, combined)

    def test_security_scan_rejects_substring_credentials_without_echoing_content(self):
        repo_root = Path(__file__).resolve().parents[2]
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            sample = tmp_path / "danger.py"
            secret = "SECRET12345"
            sample.write_text(f'MY_PRIVATE_KEY="{secret}"\nPRIVATE_KEY_SUFFIX="{secret}"\n', encoding="utf-8")
            result = subprocess.run(
                [sys.executable, "paper/scripts/security_scan.py", str(tmp_path)],
                cwd=repo_root,
                capture_output=True,
                text=True,
                check=False,
            )
        self.assertNotEqual(result.returncode, 0)
        combined = result.stdout + result.stderr
        self.assertNotIn(secret, combined)

    def test_security_scan_rejects_invalid_utf8_python_sources(self):
        repo_root = Path(__file__).resolve().parents[2]
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            sample = tmp_path / "broken.py"
            sample.write_bytes(b'client.post("/order")\n\xff')
            result = subprocess.run(
                [sys.executable, "paper/scripts/security_scan.py", str(tmp_path)],
                cwd=repo_root,
                capture_output=True,
                text=True,
                check=False,
            )
        self.assertNotEqual(result.returncode, 0)

    def test_security_scan_rejects_missing_target(self):
        repo_root = Path(__file__).resolve().parents[2]
        missing = repo_root / "paper" / "__definitely_missing__"
        result = subprocess.run(
            [sys.executable, "paper/scripts/security_scan.py", str(missing)],
            cwd=repo_root,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertNotEqual(result.returncode, 0)

    def test_security_scan_rejects_lowercase_order_endpoint_without_echoing_content(self):
        repo_root = Path(__file__).resolve().parents[2]
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            sample = tmp_path / "danger.py"
            sample.write_text('client.post("/order")\n', encoding="utf-8")
            result = subprocess.run(
                [sys.executable, "paper/scripts/security_scan.py", str(tmp_path)],
                cwd=repo_root,
                capture_output=True,
                text=True,
                check=False,
            )
        self.assertNotEqual(result.returncode, 0)
        combined = result.stdout + result.stderr
        self.assertNotIn('client.post("/order")', combined)

    def test_security_scan_rejects_shell_post_order_without_echoing_content(self):
        repo_root = Path(__file__).resolve().parents[2]
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            sample = tmp_path / "danger.sh"
            sample.write_text('curl -X POST https://clob.polymarket.com/order\n', encoding="utf-8")
            result = subprocess.run(
                [sys.executable, "paper/scripts/security_scan.py", str(tmp_path)],
                cwd=repo_root,
                capture_output=True,
                text=True,
                check=False,
            )
        self.assertNotEqual(result.returncode, 0)
        combined = result.stdout + result.stderr
        self.assertNotIn('curl -X POST https://clob.polymarket.com/order', combined)

    def test_security_scan_rejects_unreadable_non_python_sources(self):
        repo_root = Path(__file__).resolve().parents[2]
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            sample = tmp_path / "broken.sh"
            sample.write_bytes(b"curl -X POST https://clob.polymarket.com/order\n\xff")
            result = subprocess.run(
                [sys.executable, "paper/scripts/security_scan.py", str(tmp_path)],
                cwd=repo_root,
                capture_output=True,
                text=True,
                check=False,
            )
        self.assertNotEqual(result.returncode, 0)

    def test_security_scan_rejects_forbidden_patterns_without_echoing_content(self):
        repo_root = Path(__file__).resolve().parents[2]
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            sample = tmp_path / "danger.py"
            secret = "SECRET12345"
            sample.write_text(f'PRIVATE_KEY={secret}\n', encoding="utf-8")
            result = subprocess.run(
                [sys.executable, "paper/scripts/security_scan.py", str(tmp_path)],
                cwd=repo_root,
                capture_output=True,
                text=True,
                check=False,
            )
        self.assertNotEqual(result.returncode, 0)
        combined = result.stdout + result.stderr
        self.assertNotIn(secret, combined)


class PackagingSafetyTests(unittest.TestCase):
    """Static safety gate for the paper-engine container packaging."""

    @classmethod
    def setUpClass(cls):
        cls.paper_dir = Path(__file__).resolve().parents[1]

    def _read(self, name: str) -> str:
        return (self.paper_dir / name).read_text(encoding="utf-8")

    def test_compose_has_one_paper_engine_daemon_and_data_bind_mount(self):
        compose = self._read("docker-compose.yml")
        self.assertIn("services:", compose)
        self.assertIn("paper-engine:", compose)
        self.assertEqual(compose.count("paper-engine:"), 1)
        self.assertRegex(compose, r"(?m)^\s*(?:-\s*\./runtime:/data(?::[a-z]+)?|source:\s*\./runtime\s*)$")
        self.assertIn("target: /data", compose)
        self.assertIn("restart: unless-stopped", compose)

    def test_compose_has_no_credentials_wallet_or_docker_socket_mounts(self):
        compose = self._read("docker-compose.yml")
        forbidden = ("PRIVATE_KEY", "API_KEY", "API_SECRET", "PASSPHRASE", "CREDENTIAL",
                     "wallet", "/.env", "/var/run/docker.sock", "docker.sock")
        for marker in forbidden:
            self.assertNotIn(marker.lower(), compose.lower(), marker)

    def test_compose_healthcheck_checks_fresh_heartbeat_and_database_progress(self):
        compose = self._read("docker-compose.yml")
        self.assertIn("healthcheck:", compose)
        health_section = compose.split("healthcheck:", 1)[1]
        health_script = (self.paper_dir / "scripts/healthcheck.py").read_text(encoding="utf-8")
        health_section += health_script
        self.assertRegex(health_section, r"(?i)healthcheck(?:\.py|\.py\s+)?")
        self.assertRegex(health_section, r"(?i)(dashboard_snapshots|snapshot_ts_ms)")
        self.assertRegex(health_section, r"(?i)(heartbeat|fresh|age|mtime)")
        self.assertRegex(health_section, r"(?i)(write.progress|write_progress|snapshot_ts_ms)")

    def test_dockerfile_runs_non_root(self):
        dockerfile = self._read("Dockerfile")
        self.assertRegex(dockerfile, r"(?m)^USER\s+(?!root\b)\S+")

    def test_paper_watch_is_read_only_watch_command(self):
        script = self._read("scripts/paper-watch")
        self.assertIn("set -euo pipefail", script)
        self.assertIn("docker-compose.yml", script)
        self.assertRegex(script, r"paper_bot\.cli\s+watch")
        self.assertNotRegex(script, r"(?i)\b(start|stop|trade|order|buy|sell)\b")

    def test_paper_bot_exposes_only_lifecycle_commands(self):
        script = self._read("scripts/paper-bot")
        self.assertIn("set -euo pipefail", script)
        self.assertIn("docker-compose.yml", script)
        for command in ("start", "stop", "status", "logs"):
            self.assertRegex(script, rf"(?m)\b{command}\b")
        self.assertNotRegex(script, r"(?m)\b(?:trade|order|buy|sell)\b")

    def test_env_example_contains_only_non_secret_settings(self):
        env = self._read(".env.example")
        expected = {
            "SYMBOLS", "ENTRY_THRESHOLDS", "PAPER_NOTIONAL_USD", "RTDS_STALE_SEC",
            "DATA_DIR", "LOG_LEVEL",
        }
        keys = {line.split("=", 1)[0] for line in env.splitlines()
                if line.strip() and not line.lstrip().startswith("#") and "=" in line}
        self.assertEqual(keys, expected)
        self.assertNotRegex(env, r"(?i)(private_key|api_key|api_secret|passphrase|credential)")


if __name__ == "__main__":
    unittest.main()
