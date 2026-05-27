from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from dedao_sync.markdown import MarkdownWriter, content_hash, render_note, sanitize_filename_part
from dedao_sync.models import (
    AppConfig,
    ColumnConfig,
    ContentDetail,
    ContentItem,
    DedaoConfig,
    FeishuConfig,
    ObsidianConfig,
    SummaryConfig,
    SummaryResult,
    TranscriptionConfig,
)


def make_config(root: Path) -> AppConfig:
    return AppConfig(
        obsidian=ObsidianConfig(root, "得到", "{column}-{published_date}-{title}.md"),
        dedao=DedaoConfig(root / "auth.json", root / "profile", False, 2, (ColumnConfig("栏目", "https://example.com"),)),
        summary=SummaryConfig(False, "x", "x", "BASE", "KEY"),
        transcription=TranscriptionConfig(False, "x", True, root / "media"),
        feishu=FeishuConfig(False, "WEBHOOK", "SECRET"),
        root_dir=root,
    )


class MarkdownTests(unittest.TestCase):
    def test_sanitize_filename_part(self):
        self.assertEqual(sanitize_filename_part('a<b>c:d"e/f\\g|h?i*j'), "a b c d e f g h i j")
        self.assertEqual(sanitize_filename_part("CON"), "_CON")

    def test_render_and_atomic_write(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = make_config(root)
            item = ContentItem(
                source_url="https://example.com/1",
                detail_url="https://example.com/1",
                column_name="栏目",
                title='标题: "测试"',
                published_at="2026-05-26",
            )
            detail = ContentDetail(item=item, transcript_text="标题 测试\n\n第一段\n\n第二段", has_transcript=True)
            summary = SummaryResult(
                atomic_cards=("卡片内容",),
                permanent_note="永久笔记",
                links=("关联",),
                actions=("行动",),
                questions=("问题？",),
                keywords=("关键词",),
            )
            body = render_note(detail, summary)
            self.assertIn('title: "标题: \\"测试\\""', body)
            path = MarkdownWriter(config).write(detail, summary)
            self.assertTrue(path.exists())
            self.assertIn("## 全文稿", path.read_text(encoding="utf-8"))

    def test_content_hash_is_stable(self):
        self.assertEqual(content_hash("abc"), content_hash("abc"))
        self.assertNotEqual(content_hash("abc"), content_hash("abd"))


if __name__ == "__main__":
    unittest.main()

