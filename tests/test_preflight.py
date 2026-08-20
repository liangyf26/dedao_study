from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from dedao_sync.config import load_config
from dedao_sync.preflight import PreflightChecker


VALID_AUTH_STATE = '{"cookies":[{"name":"sid","value":"test","domain":".dedao.cn","path":"/"}],"origins":[]}'


def write_config(root: Path, *, transcription_enabled: bool = False, overrides: dict | None = None) -> Path:
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
            "enabled": transcription_enabled,
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


class PreflightTests(unittest.TestCase):
    def test_browser_dependency_can_be_required(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = load_config(write_config(root))
            auth = root / "data" / "auth" / "dedao_state.json"
            auth.parent.mkdir(parents=True)
            auth.write_text(VALID_AUTH_STATE, encoding="utf-8")

            with mock.patch("dedao_sync.preflight.check_playwright_chromium", return_value=(False, "Playwright Chromium executable is missing")):
                result = PreflightChecker(config, require_browser=True).check()

            self.assertFalse(result.ok)
            self.assertTrue(any("Playwright Chromium executable is missing" in error for error in result.errors))

    def test_browser_dependency_is_optional_for_fake_crawler_tests(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = load_config(write_config(root))
            auth = root / "data" / "auth" / "dedao_state.json"
            auth.parent.mkdir(parents=True)
            auth.write_text(VALID_AUTH_STATE, encoding="utf-8")

            with mock.patch("dedao_sync.preflight.check_playwright_chromium", return_value=(False, "Playwright Chromium executable is missing")):
                result = PreflightChecker(config, require_browser=False).check()

            self.assertTrue(result.ok)

    def test_invalid_auth_state_is_error_when_auth_required(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = load_config(write_config(root))
            auth = root / "data" / "auth" / "dedao_state.json"
            auth.parent.mkdir(parents=True)
            auth.write_text("{}", encoding="utf-8")

            result = PreflightChecker(config, require_auth=True).check()

            self.assertFalse(result.ok)
            self.assertTrue(any("Dedao auth state invalid" in error for error in result.errors))

    def test_output_dir_is_created_and_probe_file_is_removed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = load_config(write_config(root))
            auth = root / "data" / "auth" / "dedao_state.json"
            auth.parent.mkdir(parents=True)
            auth.write_text(VALID_AUTH_STATE, encoding="utf-8")

            result = PreflightChecker(config, probe_vault_write=True).check()

            self.assertTrue(result.ok)
            self.assertTrue(config.output_root.is_dir())
            leftovers = list(config.output_root.glob(".dedao-sync-preflight.*.tmp"))
            self.assertEqual(leftovers, [])

    def test_unwritable_output_dir_is_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = load_config(write_config(root))
            auth = root / "data" / "auth" / "dedao_state.json"
            auth.parent.mkdir(parents=True)
            auth.write_text(VALID_AUTH_STATE, encoding="utf-8")

            with mock.patch("dedao_sync.preflight.tempfile.mkstemp", side_effect=OSError("locked")):
                result = PreflightChecker(config, probe_vault_write=True).check()

            self.assertFalse(result.ok)
            self.assertTrue(any("Obsidian output path is not writable" in error for error in result.errors))

    def test_output_dir_cannot_escape_vault(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = load_config(
                write_config(
                    root,
                    overrides={
                        "obsidian": {"output_dir": "../outside-vault"},
                    },
                )
            )
            auth = root / "data" / "auth" / "dedao_state.json"
            auth.parent.mkdir(parents=True)
            auth.write_text(VALID_AUTH_STATE, encoding="utf-8")

            result = PreflightChecker(config).check()

            self.assertFalse(result.ok)
            self.assertTrue(any("obsidian.output_dir must stay inside obsidian.vault_path" in error for error in result.errors))

    def test_enabled_transcription_is_blocked_until_implemented(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = load_config(write_config(root, transcription_enabled=True))
            auth = root / "data" / "auth" / "dedao_state.json"
            auth.parent.mkdir(parents=True)
            auth.write_text(VALID_AUTH_STATE, encoding="utf-8")

            result = PreflightChecker(config).check()

            self.assertFalse(result.ok)
            self.assertTrue(any("Transcription is not implemented" in error for error in result.errors))

    def test_failure_snapshot_dir_is_checked_when_enabled(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bad_path = root / "not-a-dir"
            bad_path.write_text("file", encoding="utf-8")
            config = load_config(
                write_config(
                    root,
                    overrides={
                        "dedao": {
                            "save_failure_html": True,
                            "failure_snapshot_dir": str(bad_path),
                        },
                    },
                )
            )
            auth = root / "data" / "auth" / "dedao_state.json"
            auth.parent.mkdir(parents=True)
            auth.write_text(VALID_AUTH_STATE, encoding="utf-8")

            result = PreflightChecker(config).check()

            self.assertFalse(result.ok)
            self.assertTrue(any("Failure HTML snapshot" in error for error in result.errors))

    def test_required_feishu_webhook_is_error_when_enabled(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = load_config(
                write_config(
                    root,
                    overrides={
                        "feishu": {
                            "enabled": True,
                            "webhook_url_env": "MISSING_FEISHU_WEBHOOK",
                        },
                    },
                )
            )
            auth = root / "data" / "auth" / "dedao_state.json"
            auth.parent.mkdir(parents=True)
            auth.write_text(VALID_AUTH_STATE, encoding="utf-8")

            result = PreflightChecker(config, require_feishu=True).check()

            self.assertFalse(result.ok)
            self.assertTrue(any("Feishu webhook env is missing" in error for error in result.errors))

    def test_required_feishu_webhook_must_be_http_url(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = load_config(
                write_config(
                    root,
                    overrides={
                        "feishu": {
                            "enabled": True,
                            "webhook_url_env": "FEISHU_WEBHOOK_URL_TEST",
                        },
                    },
                )
            )
            auth = root / "data" / "auth" / "dedao_state.json"
            auth.parent.mkdir(parents=True)
            auth.write_text(VALID_AUTH_STATE, encoding="utf-8")

            with mock.patch.dict("os.environ", {"FEISHU_WEBHOOK_URL_TEST": "not-a-url"}):
                result = PreflightChecker(config, require_feishu=True).check()

            self.assertFalse(result.ok)
            self.assertTrue(any("Feishu webhook env is not a valid http(s) URL" in error for error in result.errors))

    def test_summary_base_url_format_is_warned(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = load_config(
                write_config(
                    root,
                    overrides={
                        "summary": {
                            "enabled": True,
                            "base_url_env": "SUMMARY_BASE_URL_TEST",
                            "api_key_env": "SUMMARY_KEY_TEST",
                        },
                    },
                )
            )
            auth = root / "data" / "auth" / "dedao_state.json"
            auth.parent.mkdir(parents=True)
            auth.write_text(VALID_AUTH_STATE, encoding="utf-8")

            with mock.patch.dict("os.environ", {"SUMMARY_BASE_URL_TEST": "api.example.com", "SUMMARY_KEY_TEST": "sk-test"}):
                result = PreflightChecker(config).check()

            self.assertTrue(result.ok)
            self.assertTrue(any("Summary base URL env is not a valid http(s) URL" in warning for warning in result.warnings))

    def test_config_semantics_are_checked(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = load_config(
                write_config(
                    root,
                    overrides={
                        "obsidian": {"filename_pattern": "{column}-{title}.md"},
                        "dedao": {
                            "request_interval_seconds": -1,
                            "columns": [
                                {"name": "重复", "url": "not-a-url", "enabled": True},
                                {"name": "重复", "url": "https://example.com", "enabled": False},
                            ],
                        },
                        "summary": {"enabled": True, "provider": "unknown"},
                    },
                )
            )
            auth = root / "data" / "auth" / "dedao_state.json"
            auth.parent.mkdir(parents=True)
            auth.write_text(VALID_AUTH_STATE, encoding="utf-8")

            result = PreflightChecker(config).check()

            self.assertFalse(result.ok)
            text = "\n".join(result.errors)
            self.assertIn("Duplicate Dedao column name", text)
            self.assertIn("Invalid Dedao column URL", text)
            self.assertIn("request_interval_seconds", text)
            self.assertIn("Unsupported summary provider", text)
            self.assertIn("filename_pattern missing fields", text)

    def test_no_enabled_columns_is_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = load_config(
                write_config(
                    root,
                    overrides={
                        "dedao": {
                            "columns": [{"name": "栏目", "url": "https://example.com", "enabled": False}],
                        },
                    },
                )
            )
            auth = root / "data" / "auth" / "dedao_state.json"
            auth.parent.mkdir(parents=True)
            auth.write_text(VALID_AUTH_STATE, encoding="utf-8")

            result = PreflightChecker(config).check()

            self.assertFalse(result.ok)
            self.assertTrue(any("No enabled Dedao columns" in error for error in result.errors))

    def test_filename_pattern_rejects_unknown_fields(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = load_config(
                write_config(
                    root,
                    overrides={
                        "obsidian": {"filename_pattern": "{column}-{published_date}-{title}-{dedao_id}.md"},
                    },
                )
            )
            auth = root / "data" / "auth" / "dedao_state.json"
            auth.parent.mkdir(parents=True)
            auth.write_text(VALID_AUTH_STATE, encoding="utf-8")

            result = PreflightChecker(config).check()

            self.assertFalse(result.ok)
            self.assertTrue(any("filename_pattern unsupported fields: dedao_id" in error for error in result.errors))

    def test_sensitive_runtime_paths_inside_project_must_stay_ignored(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = load_config(
                write_config(
                    root,
                    overrides={
                        "dedao": {
                            "auth_state_path": "dedao_state.json",
                            "browser_profile_dir": "browser-profile",
                            "failure_snapshot_dir": "page-failures",
                        },
                        "transcription": {"temp_dir": "media-cache"},
                    },
                )
            )
            auth = root / "data" / "auth" / "dedao_state.json"
            auth.parent.mkdir(parents=True)
            auth.write_text(VALID_AUTH_STATE, encoding="utf-8")

            result = PreflightChecker(config).check()

            self.assertFalse(result.ok)
            text = "\n".join(result.errors)
            auth_dir = str(Path("data") / "auth")
            profile_dir = str(Path("data") / "browser_profile")
            failures_dir = str(Path("data") / "page_failures")
            media_dir = str(Path("data") / "media_cache")
            self.assertIn(f"dedao.auth_state_path inside project must stay under {auth_dir}", text)
            self.assertIn(f"dedao.browser_profile_dir inside project must stay under {profile_dir}", text)
            self.assertIn(f"dedao.failure_snapshot_dir inside project must stay under {failures_dir}", text)
            self.assertIn(f"transcription.temp_dir inside project must stay under {media_dir}", text)


if __name__ == "__main__":
    unittest.main()
