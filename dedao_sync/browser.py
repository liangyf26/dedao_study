from __future__ import annotations

from pathlib import Path

from .models import AppConfig


class BrowserDependencyError(RuntimeError):
    pass


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
        return self.config.dedao.auth_state_path

    def verify_auth_state_file(self) -> bool:
        path = self.config.dedao.auth_state_path
        return path.exists() and path.stat().st_size > 20

