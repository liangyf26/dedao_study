from __future__ import annotations

import json
from pathlib import Path

from .models import AppConfig


class BrowserDependencyError(RuntimeError):
    pass


def is_dedao_logged_in_page(url: str, text: str) -> bool:
    if "login" in url.lower():
        return False
    logged_in_markers = ("已购", "学习", "我的")
    if any(marker in text for marker in logged_in_markers):
        return True
    login_markers = ("扫码登录", "验证码", "手机号登录", "请登录", "登录/注册")
    if any(marker in text for marker in login_markers):
        return False
    return False


def validate_storage_state_file(path: str | Path) -> tuple[bool, str]:
    path = Path(path)
    if not path.exists():
        return False, f"auth state not found: {path}"
    if path.stat().st_size <= 2:
        return False, f"auth state is empty or too small: {path}"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return False, f"auth state is not valid JSON: {path} ({exc})"
    if not isinstance(data, dict):
        return False, f"auth state root must be an object: {path}"
    cookies = data.get("cookies")
    origins = data.get("origins")
    if not isinstance(cookies, list) or not isinstance(origins, list):
        return False, f"auth state must contain Playwright cookies/origins lists: {path}"
    if not cookies and not origins:
        return False, f"auth state has no cookies or origins, run login again: {path}"
    return True, "ok"


def check_playwright_chromium() -> tuple[bool, str]:
    try:
        from playwright.sync_api import sync_playwright  # type: ignore
    except ImportError:
        return False, "Playwright Python package is missing"
    try:
        with sync_playwright() as playwright:
            executable = Path(playwright.chromium.executable_path)
    except Exception as exc:
        return False, f"Playwright Chromium check failed: {exc}"
    try:
        exists = executable.exists()
    except OSError as exc:
        return False, f"Playwright Chromium executable is not accessible: {executable} ({exc})"
    if not exists:
        return False, f"Playwright Chromium executable is missing: {executable}"
    return True, str(executable)


class BrowserSession:
    def __init__(self, config: AppConfig):
        self.config = config

    def _sync_playwright(self):
        try:
            from playwright.sync_api import sync_playwright  # type: ignore
        except ImportError as exc:
            raise BrowserDependencyError(
                "Playwright is not installed. Install project dependencies and run: playwright install chromium"
            ) from exc
        return sync_playwright

    def login(self, url: str = "https://www.dedao.cn/") -> Path:
        sync_playwright = self._sync_playwright()
        self.config.dedao.auth_state_path.parent.mkdir(parents=True, exist_ok=True)
        self.config.dedao.browser_profile_dir.mkdir(parents=True, exist_ok=True)
        with sync_playwright() as playwright:
            context = playwright.chromium.launch_persistent_context(
                user_data_dir=str(self.config.dedao.browser_profile_dir),
                headless=False,
            )
            page = context.pages[0] if context.pages else context.new_page()
            self._goto_page(page, url, timeout=45000)
            print("请在打开的浏览器中完成得到登录。确认页面显示已登录后，回到终端按 Enter 保存登录态。")
            input()
            context.storage_state(path=str(self.config.dedao.auth_state_path))
            verified = self._verify_login_context(context)
            context.close()
        ok, message = validate_storage_state_file(self.config.dedao.auth_state_path)
        if not ok:
            raise BrowserDependencyError(message)
        if not verified:
            raise BrowserDependencyError(
                "登录态已保存，但复用浏览器上下文访问已购页仍未通过登录校验；"
                "请确认网页登录完成后再按 Enter，或删除 data/browser_profile 后重试。"
            )
        return self.config.dedao.auth_state_path

    def verify_auth_state_file(self) -> bool:
        ok, _ = validate_storage_state_file(self.config.dedao.auth_state_path)
        return ok

    def _verify_login_context(self, context) -> bool:
        page = context.new_page()
        self._goto_page(page, "https://www.dedao.cn/bought", timeout=30000)
        page.wait_for_timeout(2000)
        text = page.locator("body").inner_text(timeout=10000)
        return is_dedao_logged_in_page(page.url, text)

    @staticmethod
    def _goto_page(page, url: str, *, timeout: int) -> None:
        try:
            page.goto(url, wait_until="commit", timeout=timeout)
        except Exception as exc:
            message = str(exc).lower()
            unsupported_wait_state = (
                "expected one of" in message
                or (("wait_until" in message or "waituntil" in message) and ("invalid" in message or "unknown" in message))
            )
            if "timeout" in message or not unsupported_wait_state:
                raise
            page.goto(url, wait_until="domcontentloaded", timeout=timeout)
