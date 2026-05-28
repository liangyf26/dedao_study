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
    def test_save_failure_html_uses_configured_debug_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = AppConfig(
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
