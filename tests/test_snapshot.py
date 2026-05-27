from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from dedao_sync.crawler import DedaoCrawler
from dedao_sync.models import ColumnConfig
from dedao_sync.snapshot import parse_snapshot


class SnapshotTests(unittest.TestCase):
    def test_parse_snapshot_extracts_transcript_and_candidates(self):
        with tempfile.TemporaryDirectory() as tmp:
            html_path = Path(tmp) / "page.html"
            html_path.write_text(
                """
                <html><body>
                <a href="/course/detail?id=abc">健康参考 标题</a>
                <article>
                  <h1>健康参考 标题</h1>
                  <p>健康参考 标题，这是第一段内容，长度足够用于质量判断。这里继续补充背景、事实和判断，让正文接近真实课程文稿的密度。为了模拟真实文稿，这一段还会描述问题出现的场景、判断依据，以及读者需要特别留意的变量。</p>
                  <p>第二段继续展开一个完整观点，避免被判断为太短。它会解释原因、条件和限制，并保留足够多的自然语言段落。这里继续说明观点的适用边界：不是所有情形都能套用同一个结论，需要结合个体差异和时间尺度来理解。</p>
                  <p>第三段补充边界和观察，形成可解析的正文。最后再加入行动建议、复盘问题和后续观察信号，确保质量门槛可以通过。这个段落还会补充一个长期跟踪指标，用来帮助读者把内容转化成后续可以复盘的笔记。</p>
                </article>
                </body></html>
                """,
                encoding="utf-8",
            )
            result = parse_snapshot(
                html_path,
                title="健康参考 标题",
                column_name="栏目",
                source_url="https://www.dedao.cn/course/detail?id=course",
                write_transcript=True,
            )
            self.assertTrue(result.detail.has_transcript)
            self.assertEqual(result.candidate_count, 1)
            self.assertEqual(len(result.item_candidates), 1)
            self.assertEqual(result.item_candidates[0].title, "健康参考 标题")
            self.assertGreaterEqual(len(result.transcript_candidates), 1)
            self.assertTrue(any(candidate.selected for candidate in result.transcript_candidates))
            self.assertTrue(result.transcript_path and result.transcript_path.exists())

    def test_items_from_anchors_filters_external_urls(self):
        column = ColumnConfig("栏目", "https://www.dedao.cn/course/detail?id=course")
        items = DedaoCrawler.items_from_anchors(
            column,
            [
                {"href": "https://evil.example/detail?id=1", "text": "外部链接标题"},
                {"href": "/course/detail?id=1", "text": "得到内部标题"},
            ],
        )
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].dedao_id, "1")

    def test_items_from_anchors_normalizes_urls_and_skips_column_page(self):
        column = ColumnConfig("栏目", "https://www.dedao.cn/course/detail?id=course")
        items = DedaoCrawler.items_from_anchors(
            column,
            [
                {"href": "https://www.dedao.cn/course/detail?id=course#catalog", "text": "当前栏目页面"},
                {"href": "/course/detail?b=2&id=1#comments", "text": "得到内部标题"},
                {"href": "https://www.dedao.cn/course/detail?id=1&b=2", "text": "得到内部标题重复"},
            ],
        )
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].detail_url, "https://www.dedao.cn/course/detail?b=2&id=1")


if __name__ == "__main__":
    unittest.main()
