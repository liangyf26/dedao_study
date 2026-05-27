from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from dedao_sync.config import load_config
from dedao_sync.preflight import PreflightChecker


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
            auth.write_text("{}", encoding="utf-8")

            with mock.patch("dedao_sync.preflight.importlib.util.find_spec", return_value=None):
                result = PreflightChecker(config, require_browser=True).check()

            self.assertFalse(result.ok)
            self.assertTrue(any("Playwright is not installed" in error for error in result.errors))

    def test_browser_dependency_is_optional_for_fake_crawler_tests(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = load_config(write_config(root))
            auth = root / "data" / "auth" / "dedao_state.json"
            auth.parent.mkdir(parents=True)
            auth.write_text("{}", encoding="utf-8")

            with mock.patch("dedao_sync.preflight.importlib.util.find_spec", return_value=None):
                result = PreflightChecker(config, require_browser=False).check()

            self.assertTrue(result.ok)

    def test_output_dir_is_created_and_probe_file_is_removed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = load_config(write_config(root))
            auth = root / "data" / "auth" / "dedao_state.json"
            auth.parent.mkdir(parents=True)
            auth.write_text("{}", encoding="utf-8")

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
            auth.write_text("{}", encoding="utf-8")

            with mock.patch("dedao_sync.preflight.tempfile.mkstemp", side_effect=OSError("locked")):
                result = PreflightChecker(config, probe_vault_write=True).check()

            self.assertFalse(result.ok)
            self.assertTrue(any("Obsidian output path is not writable" in error for error in result.errors))

    def test_enabled_transcription_is_blocked_until_implemented(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = load_config(write_config(root, transcription_enabled=True))
            auth = root / "data" / "auth" / "dedao_state.json"
            auth.parent.mkdir(parents=True)
            auth.write_text("{}", encoding="utf-8")

            result = PreflightChecker(config).check()

            self.assertFalse(result.ok)
            self.assertTrue(any("Transcription is not implemented" in error for error in result.errors))

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
            auth.write_text("{}", encoding="utf-8")

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
            auth.write_text("{}", encoding="utf-8")

            result = PreflightChecker(config).check()

            self.assertFalse(result.ok)
            self.assertTrue(any("No enabled Dedao columns" in error for error in result.errors))


if __name__ == "__main__":
    unittest.main()
