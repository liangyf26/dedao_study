from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from dedao_sync.doctor import doctor_checks_to_dicts, doctor_exit_code, run_doctor


VALID_AUTH_STATE = '{"cookies":[{"name":"sid","value":"test","domain":".dedao.cn","path":"/"}],"origins":[]}'


def write_config(root: Path, *, overrides: dict | None = None) -> Path:
    vault = root / "vault"
    vault.mkdir()
    config = {
        "obsidian": {
            "vault_path": str(vault),
            "output_dir": "得到",
            "filename_pattern": "{column}-{published_date}-{title}.md",
        },
        "dedao": {
            "auth_state_path": "data/auth/dedao_state.json",
            "browser_profile_dir": "data/browser_profile",
            "headless": False,
            "request_interval_seconds": 2,
            "columns": [{"name": "栏目", "url": "https://example.com", "enabled": True}],
        },
        "summary": {
            "enabled": False,
            "provider": "opencode_go",
            "model": "deepseek-v4-pro",
            "base_url_env": "BASE",
            "api_key_env": "KEY",
        },
        "transcription": {
            "enabled": False,
            "provider": "faster_whisper",
            "delete_media_after_transcription": True,
            "temp_dir": "data/media_cache",
        },
        "feishu": {
            "enabled": False,
            "webhook_url_env": "WEBHOOK",
            "secret_env": "SECRET",
        },
    }
    if overrides:
        for section, values in overrides.items():
            config[section].update(values)
    path = root / "config.json"
    path.write_text(json.dumps(config, ensure_ascii=False), encoding="utf-8")
    return path


class DoctorTests(unittest.TestCase):
    def test_doctor_reports_missing_auth_as_warning_when_not_required(self):
        with tempfile.TemporaryDirectory() as tmp:
            config_path = write_config(Path(tmp))
            checks = run_doctor(config_path, require_auth=False)
            by_name = {check.name: check for check in checks}
            self.assertEqual(by_name["config_load"].status, "ok")
            self.assertEqual(by_name["obsidian_vault"].status, "ok")
            self.assertEqual(by_name["auth_state"].status, "warn")
            self.assertEqual(doctor_exit_code(checks), 0)

    def test_doctor_reports_missing_auth_as_error_when_required(self):
        with tempfile.TemporaryDirectory() as tmp:
            config_path = write_config(Path(tmp))
            checks = run_doctor(config_path, require_auth=True)
            by_name = {check.name: check for check in checks}
            self.assertEqual(by_name["auth_state"].status, "error")
            self.assertEqual(doctor_exit_code(checks), 1)

    def test_doctor_reports_valid_auth_as_ok(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_path = write_config(root)
            auth = root / "data" / "auth" / "dedao_state.json"
            auth.parent.mkdir(parents=True)
            auth.write_text(VALID_AUTH_STATE, encoding="utf-8")

            checks = run_doctor(config_path, require_auth=True)

            by_name = {check.name: check for check in checks}
            self.assertEqual(by_name["auth_state"].status, "ok")

    def test_doctor_reports_invalid_auth_as_error_when_required(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_path = write_config(root)
            auth = root / "data" / "auth" / "dedao_state.json"
            auth.parent.mkdir(parents=True)
            auth.write_text("{}", encoding="utf-8")

            checks = run_doctor(config_path, require_auth=True)

            by_name = {check.name: check for check in checks}
            self.assertEqual(by_name["auth_state"].status, "error")
            self.assertIn("auth state", by_name["auth_state"].message)
            self.assertEqual(doctor_exit_code(checks), 1)

    def test_doctor_checks_to_dicts(self):
        with tempfile.TemporaryDirectory() as tmp:
            config_path = write_config(Path(tmp))
            checks = run_doctor(config_path, require_auth=False)
            rows = doctor_checks_to_dicts(checks)
            self.assertTrue(rows)
            self.assertIn("name", rows[0])
            self.assertIn("status", rows[0])
            self.assertIn("message", rows[0])

    def test_doctor_reports_config_semantic_errors(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_path = write_config(
                root,
                overrides={
                    "dedao": {
                        "request_interval_seconds": -1,
                        "columns": [{"name": "栏目", "url": "not-a-url", "enabled": False}],
                    },
                    "obsidian": {"filename_pattern": "{title}.md"},
                },
            )

            checks = run_doctor(config_path, require_auth=False)

            semantic_errors = [check.message for check in checks if check.name == "config_semantics" and check.status == "error"]
            self.assertTrue(any("No enabled Dedao columns" in message for message in semantic_errors))
            self.assertTrue(any("Invalid Dedao column URL" in message for message in semantic_errors))
            self.assertTrue(any("request_interval_seconds" in message for message in semantic_errors))
            self.assertTrue(any("filename_pattern missing fields" in message for message in semantic_errors))
            self.assertEqual(doctor_exit_code(checks), 1)


if __name__ == "__main__":
    unittest.main()
