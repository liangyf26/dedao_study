from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

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

    def test_doctor_reports_unsupported_filename_pattern_fields(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_path = write_config(
                root,
                overrides={
                    "obsidian": {"filename_pattern": "{column}-{published_date}-{title}-{dedao_id}.md"},
                },
            )

            checks = run_doctor(config_path, require_auth=False)

            semantic_errors = [check.message for check in checks if check.name == "config_semantics" and check.status == "error"]
            self.assertTrue(any("filename_pattern unsupported fields: dedao_id" in message for message in semantic_errors))
            self.assertEqual(doctor_exit_code(checks), 1)

    def test_doctor_reports_output_dir_outside_vault(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_path = write_config(
                root,
                overrides={
                    "obsidian": {"output_dir": str(root / "outside-vault")},
                },
            )

            checks = run_doctor(config_path, require_auth=False)

            semantic_errors = [check.message for check in checks if check.name == "config_semantics" and check.status == "error"]
            self.assertTrue(any("obsidian.output_dir must be relative" in message for message in semantic_errors))
            self.assertEqual(doctor_exit_code(checks), 1)

    def test_doctor_reports_sensitive_runtime_path_inside_project(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_path = write_config(
                root,
                overrides={
                    "dedao": {"auth_state_path": "dedao_state.json"},
                },
            )

            checks = run_doctor(config_path, require_auth=False)

            semantic_errors = [check.message for check in checks if check.name == "config_semantics" and check.status == "error"]
            self.assertTrue(any("dedao.auth_state_path inside project must stay under" in message for message in semantic_errors))
            self.assertEqual(doctor_exit_code(checks), 1)

    def test_doctor_reports_playwright_chromium_check(self):
        with tempfile.TemporaryDirectory() as tmp:
            config_path = write_config(Path(tmp))

            with mock.patch(
                "dedao_sync.doctor.check_playwright_chromium",
                return_value=(False, "Playwright Chromium executable is missing"),
            ):
                checks = run_doctor(config_path, require_auth=False)

            by_name = {check.name: check for check in checks}
            self.assertEqual(by_name["dep:playwright_chromium"].status, "warn")
            self.assertIn("Chromium executable", by_name["dep:playwright_chromium"].message)

    def test_doctor_reports_invalid_env_urls(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_path = write_config(
                root,
                overrides={
                    "summary": {"enabled": True, "base_url_env": "SUMMARY_BASE_URL_TEST"},
                    "feishu": {"enabled": True, "webhook_url_env": "FEISHU_WEBHOOK_URL_TEST"},
                },
            )

            with mock.patch.dict(
                "os.environ",
                {
                    "SUMMARY_BASE_URL_TEST": "api.example.com",
                    "BASE": "https://unused.example.com",
                    "KEY": "sk-test",
                    "FEISHU_WEBHOOK_URL_TEST": "not-a-url",
                },
            ):
                checks = run_doctor(config_path, require_auth=False)

            by_name = {check.name: check for check in checks}
            self.assertEqual(by_name["env:SUMMARY_BASE_URL_TEST"].status, "warn")
            self.assertEqual(by_name["env:SUMMARY_BASE_URL_TEST"].message, "invalid URL")
            self.assertEqual(by_name["env:FEISHU_WEBHOOK_URL_TEST"].status, "warn")
            self.assertEqual(by_name["env:FEISHU_WEBHOOK_URL_TEST"].message, "invalid URL")

    def test_doctor_treats_pyyaml_as_optional(self):
        with tempfile.TemporaryDirectory() as tmp:
            config_path = write_config(Path(tmp))

            with mock.patch("dedao_sync.doctor._check_import", side_effect=lambda name: False):
                checks = run_doctor(config_path, require_auth=False)

            by_name = {check.name: check for check in checks}
            self.assertEqual(by_name["dep:pyyaml"].status, "ok")
            self.assertIn("built-in limited YAML parser", by_name["dep:pyyaml"].message)
            self.assertEqual(by_name["dep:playwright"].status, "warn")


if __name__ == "__main__":
    unittest.main()
