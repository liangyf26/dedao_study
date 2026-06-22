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

    def test_fetch_detail_returns_login_required_for_login_page(self):
        class ExplodingExtractor:
            def from_ddarticle_payload(self, item, payload):
                raise AssertionError("login pages should not be parsed as ddarticle payloads")

            def from_html(self, item, html):
                raise AssertionError("login pages should not be parsed as article HTML")

        class TimeoutResponse:
            def __enter__(self):
                raise RuntimeError("Timeout 15000ms")

            def __exit__(self, exc_type, exc, tb):
                return False

        class FakeLocator:
            def inner_text(self, **kwargs):
                return "得到一下 知识城邦 账户充值 登录 注册 验证码登录 获取验证码 最近学习"

        class FakePage:
            def __init__(self):
                self.url = "about:blank"

            def on(self, *args, **kwargs):
                pass

            def expect_response(self, *args, **kwargs):
                return TimeoutResponse()

            def goto(self, url, **kwargs):
                self.url = url

            def wait_for_load_state(self, *args, **kwargs):
                pass

            def wait_for_timeout(self, *args, **kwargs):
                pass

            def title(self):
                return "得到APP - 知识就是力量，知识就在得到"

            def content(self):
                return "<html><body>验证码登录 获取验证码 最近学习</body></html>"

            def locator(self, selector):
                self.selector = selector
                return FakeLocator()

        class FakeContext:
            def __init__(self):
                self.page = FakePage()

            def new_page(self):
                return self.page

            def close(self):
                pass

        class FakePlaywrightManager:
            def __enter__(self):
                return object()

            def __exit__(self, exc_type, exc, tb):
                return False

        class FakeCrawler(DedaoCrawler):
            def _sync_playwright(self):
                return lambda: FakePlaywrightManager()

            def _new_context(self, playwright):
                return None, FakeContext()

        with tempfile.TemporaryDirectory() as tmp, mock.patch("dedao_sync.crawler.time.sleep"):
            crawler = FakeCrawler(self._config(Path(tmp)), extractor=ExplodingExtractor())
            item = ContentItem(
                source_url="https://aiquan.dedao.cn/courseList?type=1",
                detail_url="https://www.dedao.cn/course/article?id=abc",
                dedao_id="abc",
                column_name="快刀青衣·快刀广播站",
                title="688｜三年前Google发Code Red对抗OpenAI",
            )

            detail = crawler.fetch_detail(item)

        self.assertFalse(detail.has_transcript)
        self.assertEqual(detail.quality_reason, "login_required")
        self.assertEqual(detail.item.title, "688｜三年前Google发Code Red对抗OpenAI")
        self.assertTrue(detail.raw_html_hash)

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

    def test_items_from_vue_articles_builds_article_urls(self):
        items = DedaoCrawler.items_from_vue_articles(
            ColumnConfig("健康参考", "https://www.dedao.cn/course/detail?id=course"),
            [
                {
                    "title": "001｜不胖也要警惕内脏脂肪",
                    "enid": "article-enid",
                    "id": "119993",
                    "publishTime": 1780588800,
                }
            ],
        )

        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].detail_url, "https://www.dedao.cn/course/article?id=article-enid")
        self.assertEqual(items[0].dedao_id, "article-enid")
        self.assertEqual(items[0].title, "001｜不胖也要警惕内脏脂肪")
        self.assertEqual(items[0].published_at, "2026-06-05")
        self.assertEqual(items[0].column_name, "健康参考")

    def test_items_from_vue_articles_skips_entries_without_url_or_enid(self):
        items = DedaoCrawler.items_from_vue_articles(
            ColumnConfig("健康参考", "https://www.dedao.cn/course/detail?id=course"),
            [
                {"title": "只有标题没有链接", "id": "119993", "publishTime": 1780588800},
                {"title": "有外部 URL", "url": "/course/article?id=abc"},
            ],
        )

        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].detail_url, "https://www.dedao.cn/course/article?id=abc")

    def test_items_from_aiquan_articles_builds_www_article_urls(self):
        items = DedaoCrawler.items_from_aiquan_articles(
            ColumnConfig("快刀青衣·快刀广播站", "https://aiquan.dedao.cn/courseList?type=1"),
            [
                {
                    "title": "873｜周日荐文：Anthropic 万字长文",
                    "enid": "2Mo65zY4QZ3VnmWBP1KqEdNAa98jGB",
                    "id": "6000802",
                    "publishTime": 1780761600,
                }
            ],
        )

        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].detail_url, "https://www.dedao.cn/course/article?id=2Mo65zY4QZ3VnmWBP1KqEdNAa98jGB")
        self.assertEqual(items[0].dedao_id, "2Mo65zY4QZ3VnmWBP1KqEdNAa98jGB")
        self.assertEqual(items[0].published_at, "2026-06-07")
        self.assertEqual(items[0].column_name, "快刀青衣·快刀广播站")

    def test_items_from_aiquan_articles_accepts_free_article_list_fields(self):
        items = DedaoCrawler.items_from_aiquan_articles(
            ColumnConfig("快刀青衣·快刀广播站", "https://aiquan.dedao.cn/courseList?type=1"),
            [
                {
                    "title": "875｜顶尖AI分析体育比赛视频",
                    "enid": "kzlWERBr6meVb1WxWPK2j7LD4Od3Zp",
                    "id_str": "6000826",
                    "publish_time": 1780934400,
                }
            ],
        )

        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].detail_url, "https://www.dedao.cn/course/article?id=kzlWERBr6meVb1WxWPK2j7LD4Od3Zp")
        self.assertEqual(items[0].dedao_id, "kzlWERBr6meVb1WxWPK2j7LD4Od3Zp")
        self.assertEqual(items[0].published_at, "2026-06-09")

    def test_items_from_aiquan_articles_requires_enid(self):
        items = DedaoCrawler.items_from_aiquan_articles(
            ColumnConfig("快刀青衣·快刀广播站", "https://aiquan.dedao.cn/courseList?type=1"),
            [{"title": "873｜没有 enid", "id": "6000802"}],
        )

        self.assertEqual(items, [])

    def test_page_aiquan_audio_items_reads_vue_card_props(self):
        class FakePage:
            def eval_on_selector_all(self, selector, script):
                self.selector = selector
                self.script = script
                return [
                    {
                        "title": "875｜顶尖AI分析体育比赛视频",
                        "enid": "kzlWERBr6meVb1WxWPK2j7LD4Od3Zp",
                        "id": "6000826",
                        "publishTime": 1780934400,
                    },
                    {
                        "title": "874｜硅谷Box CEO暴论",
                        "enid": "y7GQpR6ndOgX6kYAYmK8eBvPzMN4lw",
                        "id": "6000812",
                        "publishTime": 1780848000,
                    },
                ]

        page = FakePage()
        items = DedaoCrawler._page_aiquan_audio_items(
            page,
            ColumnConfig("快刀青衣·快刀广播站", "https://aiquan.dedao.cn/courseList?type=1"),
        )

        self.assertEqual(page.selector, ".audio-card.audio-item")
        self.assertEqual(len(items), 2)
        self.assertEqual([item.dedao_id for item in items], ["kzlWERBr6meVb1WxWPK2j7LD4Od3Zp", "y7GQpR6ndOgX6kYAYmK8eBvPzMN4lw"])
        self.assertEqual(items[0].published_at, "2026-06-09")

    def test_capture_aiquan_article_response_keeps_minimal_fields(self):
        class FakeResponse:
            url = "https://aiquan.dedao.cn/aichannel/sphere/v1/app/special/article_list"

            def json(self):
                return {
                    "c": {
                        "article_list": [
                            {
                                "id": 6000802,
                                "en_id": "2Mo65zY4QZ3VnmWBP1KqEdNAa98jGB",
                                "title": "873｜周日荐文",
                                "publish_time": 1780761600,
                                "audio": {
                                    "mp3_play_url": "https://example.com/protected.m4a",
                                    "title": "音频标题",
                                },
                            }
                        ]
                    }
                }

        output = []
        DedaoCrawler._capture_aiquan_article_response(FakeResponse(), output)

        self.assertEqual(
            output,
            [
                {
                    "enid": "2Mo65zY4QZ3VnmWBP1KqEdNAa98jGB",
                    "id": 6000802,
                    "title": "873｜周日荐文",
                    "publishTime": 1780761600,
                }
            ],
        )

    def test_capture_aiquan_free_article_list_response_keeps_minimal_fields(self):
        class FakeResponse:
            url = "https://aiquan.dedao.cn/aichannel/class/free_article_list"

            def json(self):
                return {
                    "c": {
                        "article_list": [
                            {
                                "id_str": "6000826",
                                "enid": "kzlWERBr6meVb1WxWPK2j7LD4Od3Zp",
                                "title": "875｜顶尖AI分析体育比赛视频",
                                "publish_time": 1780934400,
                            }
                        ]
                    }
                }

        output = []
        DedaoCrawler._capture_aiquan_article_response(FakeResponse(), output)

        self.assertEqual(
            output,
            [
                {
                    "enid": "kzlWERBr6meVb1WxWPK2j7LD4Od3Zp",
                    "id": "6000826",
                    "title": "875｜顶尖AI分析体育比赛视频",
                    "publishTime": 1780934400,
                }
            ],
        )

    def test_capture_ddarticle_response_keeps_payload(self):
        payload = {"c": {"article": {}, "content": "[]"}}

        class FakeResponse:
            url = "https://www.dedao.cn/pc/ddarticle/v1/article/get/v2?token=abc"

            def json(self):
                return payload

        output = []
        DedaoCrawler._capture_ddarticle_response(FakeResponse(), output)

        self.assertEqual(output, [payload])


if __name__ == "__main__":
    unittest.main()
