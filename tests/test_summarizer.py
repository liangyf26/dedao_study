from __future__ import annotations

import unittest

from dedao_sync.models import ContentDetail, ContentItem
from dedao_sync.summarizer import SummaryError, build_summary_prompt, parse_summary_text


class SummarizerTests(unittest.TestCase):
    def test_parse_json_summary(self):
        result = parse_summary_text(
            """
            {
              "atomic_cards": ["卡片一", "卡片二"],
              "permanent_note": "永久笔记",
              "links": ["主题A"],
              "actions": ["行动A"],
              "questions": ["问题A"],
              "keywords": ["健康", "风险"]
            }
            """
        )
        self.assertEqual(result.atomic_cards, ("卡片一", "卡片二"))
        self.assertEqual(result.permanent_note, "永久笔记")
        self.assertEqual(result.keywords, ("健康", "风险"))

    def test_parse_chinese_key_json_in_code_fence(self):
        result = parse_summary_text(
            """```json
            {
              "原子卡片": ["卡片"],
              "永久笔记": "笔记",
              "关联": ["关联"],
              "行动/观察": ["观察"],
              "复习问题": ["问题"],
              "关键词": "健康；风险"
            }
            ```"""
        )
        self.assertEqual(result.atomic_cards, ("卡片",))
        self.assertEqual(result.actions, ("观察",))
        self.assertEqual(result.keywords, ("健康", "风险"))

    def test_parse_numbered_markdown_sections(self):
        result = parse_summary_text(
            """
            1. 原子卡片
            - 卡片一
            - 卡片二

            2. 永久笔记
            这是一条长期笔记。

            3. 关联主题
            - 主题A

            4. 行动观察
            - 观察A

            5. 复习问题
            - 问题A

            6. 关键词
            健康，风险、决策
            """
        )
        self.assertEqual(result.atomic_cards, ("卡片一", "卡片二"))
        self.assertEqual(result.links, ("主题A",))
        self.assertEqual(result.actions, ("观察A",))
        self.assertEqual(result.questions, ("问题A",))
        self.assertEqual(result.keywords, ("健康", "风险", "决策"))

    def test_prompt_requires_json_and_limits_transcript(self):
        item = ContentItem("u", "栏目", "标题", "u")
        detail = ContentDetail(item=item, transcript_text="x" * 40000, has_transcript=True)
        prompt = build_summary_prompt(detail)
        self.assertIn("只输出 JSON", prompt)
        self.assertIn("基于截断原文", prompt)
        self.assertLess(len(prompt), 32200)

    def test_empty_or_unrecognized_summary_raises(self):
        with self.assertRaises(SummaryError):
            parse_summary_text("这不是结构化摘要。")

    def test_empty_json_summary_raises(self):
        with self.assertRaises(SummaryError):
            parse_summary_text('{"atomic_cards": [], "permanent_note": ""}')


if __name__ == "__main__":
    unittest.main()
