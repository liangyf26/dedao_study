from __future__ import annotations

import json
from pathlib import Path

from .models import AppConfig


class BrowserDependencyError(RuntimeError):
    pass


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
    if not executable.exists():
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
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=False)
            context = browser.new_context()
            page = context.new_page()
            page.goto(url, wait_until="domcontentloaded")
            print("请在打开的浏览器中完成得到登录。登录完成后回到终端按 Enter 保存登录态。")
            input()
            context.storage_state(path=str(self.config.dedao.auth_state_path))
            browser.close()
        ok, message = validate_storage_state_file(self.config.dedao.auth_state_path)
        if not ok:
            raise BrowserDependencyError(message)
        return self.config.dedao.auth_state_path

    def verify_auth_state_file(self) -> bool:
        ok, _ = validate_storage_state_file(self.config.dedao.auth_state_path)
        return ok
