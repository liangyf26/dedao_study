from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from dedao_sync.browser import check_playwright_chromium, validate_storage_state_file


VALID_STATE = '{"cookies":[{"name":"sid","value":"test","domain":".dedao.cn","path":"/"}],"origins":[]}'


class BrowserTests(unittest.TestCase):
    def test_validate_storage_state_accepts_playwright_shape_with_auth_data(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            path.write_text(VALID_STATE, encoding="utf-8")

            ok, message = validate_storage_state_file(path)

            self.assertTrue(ok)
            self.assertEqual(message, "ok")

    def test_validate_storage_state_rejects_empty_or_invalid_shape(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            path.write_text('{"foo":1}', encoding="utf-8")

            ok, message = validate_storage_state_file(path)

            self.assertFalse(ok)
            self.assertIn("cookies/origins", message)

    def test_validate_storage_state_rejects_no_auth_data(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            path.write_text('{"cookies":[],"origins":[]}', encoding="utf-8")

            ok, message = validate_storage_state_file(path)

            self.assertFalse(ok)
            self.assertIn("no cookies or origins", message)

    def test_check_playwright_chromium_reports_missing_package(self):
        with mock.patch.dict("sys.modules", {"playwright.sync_api": None}):
            ok, message = check_playwright_chromium()

        self.assertFalse(ok)
        self.assertIn("Playwright Python package is missing", message)


if __name__ == "__main__":
    unittest.main()
