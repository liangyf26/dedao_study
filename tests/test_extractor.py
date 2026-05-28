from __future__ import annotations

import unittest

from dedao_sync.extractor import (
    TranscriptExtractor,
    extract_media_candidates,
    extract_metadata,
    html_to_candidate_texts,
    html_to_visible_text,
)
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

    def test_from_html_merges_detail_page_metadata(self):
        item = ContentItem(
            source_url="https://example.com/list-title",
            detail_url="https://example.com/detail",
            column_name="栏目",
            title="列表页标题",
        )
        html = """
        <html>
          <head>
            <meta property="og:title" content="健康参考 真实标题 - 得到">
            <meta name="author" content="尹烨">
            <meta property="article:published_time" content="2026-05-27T08:00:00+08:00">
          </head>
          <body>
            <article>
              <h1>健康参考 真实标题</h1>
              <p>健康参考 真实标题这一期先交代背景、事实和判断依据，形成足够完整的第一段正文内容。</p>
              <p>第二段继续展开核心观点，说明适用边界、可能例外，以及为什么不能把这个结论简单套用到所有场景。</p>
              <p>第三段给出行动建议和复盘问题，帮助读者把内容转化成后续可以观察、记录和重新检查的笔记。</p>
            </article>
          </body>
        </html>
        """
        detail = TranscriptExtractor(min_length=120, min_paragraphs=3).from_html(item, html)

        self.assertTrue(detail.has_transcript)
        self.assertEqual(detail.item.title, "健康参考 真实标题")
        self.assertEqual(detail.item.author, "尹烨")
        self.assertEqual(detail.item.published_at, "2026-05-27T08:00:00+08:00")

    def test_from_html_extracts_media_candidates(self):
        item = ContentItem(
            source_url="https://www.dedao.cn/course/detail?id=1",
            detail_url="https://www.dedao.cn/course/detail?id=1",
            column_name="栏目",
            title="健康参考",
        )
        html = """
        <html>
          <head>
            <meta property="og:audio" content="/media/audio.mp3">
            <meta property="og:video:url" content="https://static.dedao.cn/video.mp4">
          </head>
          <body>
            <audio src="/media/audio.mp3" type="audio/mpeg"></audio>
            <video><source src="https://static.dedao.cn/video.mp4" type="video/mp4"></video>
          </body>
        </html>
        """

        candidates = extract_media_candidates(html, item.detail_url)
        detail = TranscriptExtractor(min_length=20, min_paragraphs=1).from_html(item, html)

        self.assertEqual(len(candidates), 2)
        self.assertEqual(candidates[0].url, "https://www.dedao.cn/media/audio.mp3")
        self.assertEqual(candidates[1].mime_type, None)
        self.assertEqual(len(detail.media_candidates), 2)

    def test_from_html_blocks_drm_or_encrypted_media_signals(self):
        item = ContentItem(
            source_url="https://www.dedao.cn/course/detail?id=drm",
            detail_url="https://www.dedao.cn/course/detail?id=drm",
            column_name="栏目",
            title="健康参考",
        )
        html = """
        <html>
          <head>
            <meta property="og:video" content="https://static.dedao.cn/protected.mpd">
          </head>
          <body>
            <article>
              <h1>健康参考</h1>
              <p>健康参考这一期有足够正文，原本可以通过质量门槛，但页面暴露了 DRM 媒体信号。</p>
              <p>第二段继续展开，让内容长度和段落数满足要求，避免测试只因为正文太短而失败。</p>
              <p>第三段补充行动建议和边界，确保 extractor 的质量判断本身不是阻断原因。</p>
            </article>
            <script>navigator.requestMediaKeySystemAccess('com.widevine.alpha', [])</script>
          </body>
        </html>
        """

        detail = TranscriptExtractor(min_length=120, min_paragraphs=3).from_html(item, html)

        self.assertFalse(detail.has_transcript)
        self.assertEqual(detail.transcript_text, "")
        self.assertEqual(detail.quality_reason, "policy_blocked:drm_widevine")

    def test_extract_metadata_falls_back_to_time_h1_and_title(self):
        html = """
        <html>
          <head><title>健康参考 H1 标题 | 得到</title></head>
          <body>
            <time datetime="2026-05-27">今天</time>
            <h1>健康参考 H1 标题</h1>
          </body>
        </html>
        """
        metadata = extract_metadata(html)
        self.assertEqual(metadata.title, "健康参考 H1 标题")
        self.assertEqual(metadata.published_at, "2026-05-27")

    def test_extract_metadata_reads_jsonld(self):
        html = """
        <html>
          <head>
            <script type="application/ld+json">
            {
              "@context": "https://schema.org",
              "@type": "Article",
              "headline": "马江博 政经参考 真实标题 - 得到",
              "author": [{"name": "马江博"}, {"name": "嘉宾"}],
              "datePublished": "2026-05-28T07:30:00+08:00"
            }
            </script>
          </head>
          <body></body>
        </html>
        """

        metadata = extract_metadata(html)

        self.assertEqual(metadata.title, "马江博 政经参考 真实标题")
        self.assertEqual(metadata.author, "马江博，嘉宾")
        self.assertEqual(metadata.published_at, "2026-05-28T07:30:00+08:00")

    def test_html_candidate_texts_include_article_and_page(self):
        html = "<main><p>健康参考</p><p>第一段</p><p>第二段</p></main><footer>分享</footer>"
        candidates = html_to_candidate_texts(html)
        self.assertGreaterEqual(len(candidates), 2)


if __name__ == "__main__":
    unittest.main()
