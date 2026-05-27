from __future__ import annotations

import unittest

from dedao_sync.extractor import TranscriptExtractor, html_to_candidate_texts, html_to_visible_text
from dedao_sync.models import ContentItem


class ExtractorTests(unittest.TestCase):
    def test_html_to_text_skips_script(self):
        text = html_to_visible_text("<html><script>bad()</script><p>你好</p></html>")
        self.assertIn("你好", text)
        self.assertNotIn("bad", text)

    def test_quality_rejects_short_text(self):
        item = ContentItem(source_url="u", detail_url="u", column_name="栏目", title="标题")
        detail = TranscriptExtractor(min_length=20, min_paragraphs=2).from_text(item, "短")
        self.assertFalse(detail.has_transcript)

    def test_quality_accepts_related_text(self):
        item = ContentItem(source_url="u", detail_url="u", column_name="栏目", title="健康 参考")
        text = "健康参考的标题。\n\n这是第一段，有足够多的内容说明问题。\n\n这是第二段，继续展开分析。\n\n这是第三段，补充边界。"
        detail = TranscriptExtractor(min_length=30, min_paragraphs=3).from_text(item, text)
        self.assertTrue(detail.has_transcript)

    def test_quality_rejects_login_noise(self):
        item = ContentItem(source_url="u", detail_url="u", column_name="栏目", title="健康 参考")
        text = "健康参考\n\n登录 登录 登录 登录 登录\n\n扫码 验证码 登录 手机号\n\n购买 分享 收藏 评论"
        detail = TranscriptExtractor(min_length=20, min_paragraphs=3).from_text(item, text)
        self.assertFalse(detail.has_transcript)
        self.assertEqual(detail.quality_reason, "too_much_ui_noise")

    def test_from_html_prefers_clean_article_over_noisy_page(self):
        item = ContentItem(source_url="u", detail_url="u", column_name="栏目", title="健康参考")
        article = """
        <article>
          <h1>健康参考</h1>
          <p>健康参考这一期讨论一个很具体的问题，先交代背景、事实和判断依据，形成足够完整的第一段正文内容。</p>
          <p>第二段继续展开核心观点，说明它适用的边界、可能的例外，以及为什么不能把这个结论简单套用到所有场景。</p>
          <p>第三段给出行动建议和复盘问题，帮助读者把内容转化成后续可以观察、记录和重新检查的笔记。</p>
        </article>
        """
        noise = "<nav>" + "登录 扫码 下载App 相关推荐 分享 收藏 评论 购买 加入学习 " * 20 + "</nav>"
        html = f"<html><body>{noise}{article}{noise}</body></html>"
        detail = TranscriptExtractor(min_length=120, min_paragraphs=3).from_html(item, html)
        self.assertTrue(detail.has_transcript)
        self.assertIn("健康参考这一期", detail.transcript_text)
        self.assertNotIn("下载App", detail.transcript_text)

    def test_html_candidate_texts_include_article_and_page(self):
        html = "<main><p>健康参考</p><p>第一段</p><p>第二段</p></main><footer>分享</footer>"
        candidates = html_to_candidate_texts(html)
        self.assertGreaterEqual(len(candidates), 2)


if __name__ == "__main__":
    unittest.main()
