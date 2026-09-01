import subprocess
import sys
import tempfile
import unittest
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


if __name__ == "__main__":
    unittest.main()
