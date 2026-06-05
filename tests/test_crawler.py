from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from dedao_sync.crawler import DedaoCrawler
from dedao_sync.models import (
    AppConfig,
    ColumnConfig,
    ContentItem,
    DedaoConfig,
    FeishuConfig,
    ObsidianConfig,
    SummaryConfig,
    TranscriptionConfig,
)


class CrawlerTests(unittest.TestCase):
    def _config(self, root: Path) -> AppConfig:
        return AppConfig(
            obsidian=ObsidianConfig(root / "vault", "得到", "{column}-{published_date}-{title}.md"),
            dedao=DedaoConfig(
                root / "auth.json",
                root / "profile",
                False,
                2,
                True,
                root / "debug_failures",
                (ColumnConfig("栏目", "https://example.com"),),
            ),
            summary=SummaryConfig(False, "opencode_go", "deepseek-v4-pro", "BASE", "KEY"),
            transcription=TranscriptionConfig(False, "faster_whisper", True, root / "media"),
            feishu=FeishuConfig(False, "WEBHOOK", "SECRET"),
            root_dir=root,
        )

    def test_new_context_reuses_persistent_profile_when_present(self):
        class FakeChromium:
            def __init__(self):
                self.calls = []

            def launch_persistent_context(self, **kwargs):
                self.calls.append(kwargs)
                return "context"

            def launch(self, **kwargs):
                raise AssertionError("storage_state context should not be used when profile exists")

        class FakePlaywright:
            def __init__(self):
                self.chromium = FakeChromium()

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = self._config(root)
            config.dedao.browser_profile_dir.mkdir(parents=True)
            playwright = FakePlaywright()

            browser, context = DedaoCrawler(config)._new_context(playwright)

            self.assertIsNone(browser)
            self.assertEqual(context, "context")
            self.assertEqual(
                playwright.chromium.calls,
                [{"user_data_dir": str(config.dedao.browser_profile_dir), "headless": False}],
            )

    def test_goto_page_uses_commit_wait_strategy(self):
        class FakePage:
            def __init__(self):
                self.calls = []

            def goto(self, url, **kwargs):
                self.calls.append((url, kwargs))

        page = FakePage()

        DedaoCrawler._goto_page(page, "https://aiquan.dedao.cn/courseList?type=1", timeout=12345)

        self.assertEqual(
            page.calls,
            [("https://aiquan.dedao.cn/courseList?type=1", {"wait_until": "commit", "timeout": 12345})],
        )

    def test_goto_page_falls_back_when_commit_wait_is_unsupported(self):
        class FakePage:
            def __init__(self):
                self.calls = []

            def goto(self, url, **kwargs):
                self.calls.append((url, kwargs))
                if len(self.calls) == 1:
                    raise ValueError("wait_until expected one of load, domcontentloaded, networkidle")

        page = FakePage()

        DedaoCrawler._goto_page(page, "https://www.dedao.cn/course/detail?id=abc", timeout=45000)

        self.assertEqual(
            page.calls,
            [
                ("https://www.dedao.cn/course/detail?id=abc", {"wait_until": "commit", "timeout": 45000}),
                ("https://www.dedao.cn/course/detail?id=abc", {"wait_until": "domcontentloaded", "timeout": 45000}),
            ],
        )

    def test_goto_page_does_not_fall_back_on_commit_timeout(self):
        class FakePage:
            def __init__(self):
                self.calls = []

            def goto(self, url, **kwargs):
                self.calls.append((url, kwargs))
                raise TimeoutError("Page.goto: Timeout 45000ms exceeded while waiting until commit")

        page = FakePage()

        with self.assertRaises(TimeoutError):
            DedaoCrawler._goto_page(page, "https://aiquan.dedao.cn/courseList?type=1", timeout=45000)

        self.assertEqual(len(page.calls), 1)
        self.assertEqual(page.calls[0][1]["wait_until"], "commit")

    def test_save_failure_html_uses_configured_debug_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = self._config(root)
            item = ContentItem(
                source_url="https://www.dedao.cn/course/detail?id=abc",
                detail_url="https://www.dedao.cn/course/detail?id=abc",
                dedao_id="abc",
                column_name="栏目",
                title="健康参考 标题",
            )

            path = DedaoCrawler(config)._save_failure_html(item, "<html>失败快照</html>", "abcdef1234567890")

            self.assertEqual(path.parent, root / "debug_failures")
            self.assertTrue(path.name.endswith("-abcdef123456.html"))
            self.assertIn("失败快照", path.read_text(encoding="utf-8"))

    def test_request_delay_uses_small_positive_jitter(self):
        with mock.patch("dedao_sync.crawler.random.uniform", return_value=0.25):
            self.assertEqual(DedaoCrawler.jittered_delay_seconds(2), 2.25)

    def test_zero_request_delay_has_no_jitter(self):
        with mock.patch("dedao_sync.crawler.random.uniform") as uniform:
            self.assertEqual(DedaoCrawler.jittered_delay_seconds(0), 0)
            uniform.assert_not_called()


if __name__ == "__main__":
    unittest.main()
