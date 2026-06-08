from __future__ import annotations

import io
import json
import urllib.error
import unittest
from unittest import mock

from dedao_sync.models import ContentDetail, ContentItem, SummaryResult
from dedao_sync.summarizer import (
    OpenAICompatibleSummaryService,
    SummaryError,
    build_summary_prompt,
    chat_completions_url,
    finalize_summary_result,
    parse_summary_text,
)


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
        self.assertLess(len(prompt), 22200)

    def test_parse_repairable_truncated_json_cards(self):
        result = parse_summary_text(
            """{
              "atomic_cards": [
                "人生有三件事算不准，但人们每天都在算。",
                "按价值观选择的人从不后悔。"
              ）"""
        )

        self.assertEqual(
            result.atomic_cards,
            (
                "人生有三件事算不准，但人们每天都在算。",
                "按价值观选择的人从不后悔。",
            ),
        )

    def test_parse_repairable_truncated_json_multiple_fields(self):
        result = parse_summary_text(
            """{
              "atomic_cards": ["卡片一", "卡片二"],
              "permanent_note": "永久笔记",
              "keywords": ["身份", "选择"
            """
        )

        self.assertEqual(result.atomic_cards, ("卡片一", "卡片二"))
        self.assertEqual(result.permanent_note, "永久笔记")
        self.assertEqual(result.keywords, ("身份", "选择"))

    def test_finalize_summary_adds_truncation_notice_when_model_omits_it(self):
        item = ContentItem("u", "栏目", "标题", "u")
        detail = ContentDetail(item=item, transcript_text="x" * 40000, has_transcript=True)
        result = SummaryResult(atomic_cards=("卡片",), permanent_note="永久笔记")

        finalized = finalize_summary_result(detail, result)

        self.assertIn("永久笔记", finalized.permanent_note)
        self.assertIn("基于截断原文", finalized.permanent_note)

    def test_finalize_summary_does_not_duplicate_truncation_notice(self):
        item = ContentItem("u", "栏目", "标题", "u")
        detail = ContentDetail(item=item, transcript_text="x" * 40000, has_transcript=True)
        result = SummaryResult(atomic_cards=("卡片",), permanent_note="摘要基于截断原文。")

        finalized = finalize_summary_result(detail, result)

        self.assertEqual(finalized.permanent_note.count("截断原文"), 1)

    def test_empty_or_unrecognized_summary_raises(self):
        with self.assertRaises(SummaryError):
            parse_summary_text("这不是结构化摘要。")

    def test_empty_json_summary_raises(self):
        with self.assertRaises(SummaryError):
            parse_summary_text('{"atomic_cards": [], "permanent_note": ""}')

    def test_openai_compatible_service_sends_chat_completion_request(self):
        config = make_summary_config()
        item = ContentItem("https://example.com/1", "栏目", "标题", "https://example.com/1")
        detail = ContentDetail(item=item, transcript_text="正文\n\n第一段\n\n第二段", has_transcript=True)
        captured = {}

        def fake_urlopen(request, timeout):
            captured["url"] = request.full_url
            captured["auth"] = request.get_header("Authorization")
            captured["content_type"] = request.get_header("Content-type")
            captured["accept"] = request.get_header("Accept")
            captured["user_agent"] = request.get_header("User-agent")
            captured["timeout"] = timeout
            captured["payload"] = json.loads(request.data.decode("utf-8"))
            return FakeHttpResponse(
                {
                    "choices": [
                        {
                            "message": {
                                "content": json.dumps(
                                    {
                                        "atomic_cards": ["卡片"],
                                        "permanent_note": "永久笔记",
                                        "keywords": ["关键词"],
                                    },
                                    ensure_ascii=False,
                                )
                            }
                        }
                    ]
                }
            )

        with mock.patch.dict(
            "os.environ",
            {"BASE": "https://api.example.com/v1/", "KEY": "sk-test-secret"},
            clear=False,
        ):
            with mock.patch("urllib.request.urlopen", side_effect=fake_urlopen):
                result = OpenAICompatibleSummaryService(config, timeout_seconds=7).summarize(detail)

        self.assertEqual(captured["url"], "https://api.example.com/v1/chat/completions")
        self.assertEqual(captured["auth"], "Bearer sk-test-secret")
        self.assertEqual(captured["content_type"], "application/json")
        self.assertEqual(captured["accept"], "application/json")
        self.assertEqual(captured["user_agent"], "dedao-sync/0.1")
        self.assertEqual(captured["timeout"], 7)
        self.assertEqual(captured["payload"]["model"], "deepseek-v4-pro")
        self.assertEqual(captured["payload"]["max_tokens"], 2200)
        self.assertIn("只输出 JSON", captured["payload"]["messages"][1]["content"])
        self.assertEqual(result.atomic_cards, ("卡片",))
        self.assertEqual(result.keywords, ("关键词",))

    def test_chat_completions_url_accepts_full_endpoint(self):
        self.assertEqual(
            chat_completions_url("https://api.example.com/v1/chat/completions"),
            "https://api.example.com/v1/chat/completions",
        )

    def test_openai_compatible_service_redacts_http_error_body(self):
        config = make_summary_config()
        item = ContentItem("https://example.com/1", "栏目", "标题", "https://example.com/1")
        detail = ContentDetail(item=item, transcript_text="正文", has_transcript=True)
        error = urllib.error.HTTPError(
            "https://api.example.com/v1/chat/completions",
            401,
            "Unauthorized",
            hdrs=None,
            fp=io.BytesIO(b'{"error":"api_key=sk-live-secret Authorization: Bearer abc.def"}'),
        )

        with mock.patch.dict("os.environ", {"BASE": "https://api.example.com/v1", "KEY": "sk-test-secret"}, clear=False):
            with mock.patch("urllib.request.urlopen", side_effect=error):
                with self.assertRaises(SummaryError) as raised:
                    OpenAICompatibleSummaryService(config).summarize(detail)

        message = str(raised.exception)
        self.assertIn("summary API HTTP 401", message)
        self.assertIn("[REDACTED]", message)
        self.assertNotIn("sk-live-secret", message)
        self.assertNotIn("abc.def", message)

    def test_openai_compatible_service_adds_provider_hint_for_403_1010(self):
        config = make_summary_config()
        item = ContentItem("https://example.com/1", "栏目", "标题", "https://example.com/1")
        detail = ContentDetail(item=item, transcript_text="正文", has_transcript=True)
        error = urllib.error.HTTPError(
            "https://opencode.ai/zen/go/v1/chat/completions",
            403,
            "Forbidden",
            hdrs=None,
            fp=io.BytesIO(b"error code: 1010"),
        )

        with mock.patch.dict("os.environ", {"BASE": "https://opencode.ai/zen/go/v1", "KEY": "sk-test-secret"}, clear=False):
            with mock.patch("urllib.request.urlopen", side_effect=error):
                with self.assertRaises(SummaryError) as raised:
                    OpenAICompatibleSummaryService(config).summarize(detail)

        message = str(raised.exception)
        self.assertIn("summary API HTTP 403", message)
        self.assertIn("provider=opencode_go", message)
        self.assertIn("model=deepseek-v4-pro", message)
        self.assertIn("error code: 1010", message)
        self.assertIn("provider denied or blocked", message)

    def test_openai_compatible_service_wraps_timeout_as_summary_error(self):
        config = make_summary_config()
        item = ContentItem("https://example.com/1", "栏目", "标题", "https://example.com/1")
        detail = ContentDetail(item=item, transcript_text="正文", has_transcript=True)

        with mock.patch.dict("os.environ", {"BASE": "https://api.example.com/v1", "KEY": "sk-test-secret"}, clear=False):
            with mock.patch("urllib.request.urlopen", side_effect=TimeoutError("read timed out")):
                with mock.patch("dedao_sync.summarizer.time.sleep"):
                    with self.assertRaises(SummaryError) as raised:
                        OpenAICompatibleSummaryService(config, timeout_seconds=12).summarize(detail)

        self.assertIn("summary API timeout after 12s", str(raised.exception))

    def test_openai_compatible_service_retries_empty_message_content(self):
        config = make_summary_config()
        item = ContentItem("https://example.com/1", "栏目", "标题", "https://example.com/1")
        detail = ContentDetail(item=item, transcript_text="正文", has_transcript=True)
        responses = [
            FakeHttpResponse({"choices": [{"message": {"content": ""}}]}),
            FakeHttpResponse(
                {
                    "choices": [
                        {
                            "message": {
                                "content": json.dumps(
                                    {"atomic_cards": ["卡片"], "permanent_note": "笔记"},
                                    ensure_ascii=False,
                                )
                            }
                        }
                    ]
                }
            ),
        ]

        with mock.patch.dict("os.environ", {"BASE": "https://api.example.com/v1", "KEY": "sk-test-secret"}, clear=False):
            with mock.patch("urllib.request.urlopen", side_effect=responses) as urlopen:
                with mock.patch("dedao_sync.summarizer.time.sleep"):
                    result = OpenAICompatibleSummaryService(config).summarize(detail)

        self.assertEqual(urlopen.call_count, 2)
        self.assertEqual(result.atomic_cards, ("卡片",))


def make_summary_config():
    from dedao_sync.models import SummaryConfig

    return SummaryConfig(True, "opencode_go", "deepseek-v4-pro", "BASE", "KEY")


class FakeHttpResponse:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def read(self):
        return json.dumps(self.payload, ensure_ascii=False).encode("utf-8")


if __name__ == "__main__":
    unittest.main()
