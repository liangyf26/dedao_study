from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from dedao_sync.browser import (
    check_playwright_chromium,
    is_dedao_logged_in_page,
    is_dedao_login_page,
    validate_storage_state_file,
)


VALID_STATE = '{"cookies":[{"name":"sid","value":"test","domain":".dedao.cn","path":"/"}],"origins":[]}'


class BrowserTests(unittest.TestCase):
    def test_dedao_login_detector_prefers_logged_in_markers(self):
        text = "我的 学习 已购 退出登录"

        self.assertTrue(is_dedao_logged_in_page("https://www.dedao.cn/bought", text))

    def test_dedao_login_detector_rejects_login_page(self):
        text = "扫码登录 手机号登录 验证码"

        self.assertFalse(is_dedao_logged_in_page("https://www.dedao.cn/login", text))

    def test_dedao_login_detector_rejects_bought_page_with_login_form(self):
        text = "得到一下 知识城邦 账户充值 登录 注册 首页我的学习直播 最近学习 课程 验证码登录 获取验证码"

        self.assertTrue(is_dedao_login_page("https://www.dedao.cn/bought", text))
        self.assertFalse(is_dedao_logged_in_page("https://www.dedao.cn/bought", text))

    def test_dedao_login_detector_does_not_reject_registered_course_name(self):
        text = "首页 我的学习 最近学习 已购 注册会计师训练营"

        self.assertTrue(is_dedao_logged_in_page("https://www.dedao.cn/bought", text))

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

    def test_check_playwright_chromium_reports_inaccessible_executable(self):
        class FakeChromium:
            executable_path = "C:/ms-playwright/chromium/chrome.exe"

        class FakePlaywright:
            chromium = FakeChromium()

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

        fake_module = mock.Mock(sync_playwright=mock.Mock(return_value=FakePlaywright()))

        with mock.patch.dict("sys.modules", {"playwright.sync_api": fake_module}):
            with mock.patch("pathlib.Path.exists", side_effect=PermissionError("denied")):
                ok, message = check_playwright_chromium()

        self.assertFalse(ok)
        self.assertIn("not accessible", message)


if __name__ == "__main__":
    unittest.main()
