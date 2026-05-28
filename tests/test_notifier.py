from __future__ import annotations

import io
import urllib.error
import unittest
from datetime import datetime
from unittest import mock

from dedao_sync.models import FeishuConfig, RunReport
from dedao_sync.notifier import (
    FeishuCredentials,
    FeishuNotifier,
    NotificationError,
    format_run_report,
    load_feishu_credentials,
    make_feishu_sign,
)
from dedao_sync.security import redact


class NotifierTests(unittest.TestCase):
    def test_feishu_sign_known_shape(self):
        sign = make_feishu_sign(1234567890, "secret")
        self.assertTrue(sign)
        self.assertNotIn("secret", sign)

    def test_disabled_feishu_ignores_webhook_env(self):
        with mock.patch.dict("os.environ", {"FEISHU_WEBHOOK_URL": "https://example.com/hook"}):
            credentials = load_feishu_credentials(
                FeishuConfig(
                    enabled=False,
                    webhook_url_env="FEISHU_WEBHOOK_URL",
                    secret_env="FEISHU_WEBHOOK_SECRET",
                )
            )

        self.assertIsNone(credentials)

    def test_payload_omits_full_content(self):
        report = RunReport(
            started_at=datetime(2026, 5, 26, 8, 0, 0),
            finished_at=datetime(2026, 5, 26, 8, 1, 0),
            status="success",
            total_columns=4,
            new_count=1,
            added_by_column={"栏目": ["标题"]},
        )
        payload = FeishuNotifier(FeishuCredentials("https://example.com/hook", "secret")).build_payload(report)
        text = payload["content"]["text"]
        self.assertIn("标题", text)
        self.assertNotIn("全文稿", text)

    def test_redact(self):
        text = redact(
            "Authorization: Bearer abc.def and "
            "Cookie: sessionid=secret-cookie; user=liang and "
            "api_key=sk-test and "
            "https://open.feishu.cn/open-apis/bot/v2/hook/abcdef"
        )
        self.assertIn("[REDACTED]", text)
        self.assertNotIn("abc.def", text)
        self.assertNotIn("secret-cookie", text)
        self.assertNotIn("sk-test", text)
        self.assertNotIn("abcdef", text)

    def test_report_format(self):
        report = RunReport(started_at=datetime.now(), finished_at=datetime.now(), status="success", total_columns=4)
        self.assertIn("得到同步完成", format_run_report(report))

    def test_report_includes_prd_required_fields(self):
        report = RunReport(
            started_at=datetime(2026, 5, 27, 8, 0, 0),
            finished_at=datetime(2026, 5, 27, 8, 0, 5),
            status="partial_failed",
            total_columns=4,
            discovered_count=8,
            new_count=2,
            skipped_count=6,
            request_count=9,
            failed_count=1,
            missing_transcript_count=1,
            summary_failed_count=1,
            failures=["Authorization: Bearer abc.def"],
            metadata={"host": "test-host"},
        )
        text = format_run_report(report)
        self.assertIn("得到同步部分失败", text)
        self.assertIn("执行时间：2026-05-27 08:00:05", text)
        self.assertIn("运行机器：test-host", text)
        self.assertIn("耗时：5s", text)
        self.assertIn("总栏目数：4", text)
        self.assertIn("新增文章数：2", text)
        self.assertIn("跳过文章数：6", text)
        self.assertIn("网页请求数：9", text)
        self.assertIn("失败文章数：1", text)
        self.assertIn("无文字稿文章数：1", text)
        self.assertIn("摘要失败数：1", text)
        self.assertNotIn("abc.def", text)

    def test_report_without_finished_time_uses_asia_shanghai_clock(self):
        report = RunReport(
            started_at=datetime(2026, 5, 27, 8, 0, 0),
            status="success",
            metadata={"host": "test-host"},
        )

        with mock.patch("dedao_sync.notifier.now_local", return_value=datetime(2026, 5, 27, 9, 30, 0)):
            text = format_run_report(report)

        self.assertIn("执行时间：2026-05-27 09:30:00", text)
        self.assertIn("耗时：unknown", text)

    def test_report_includes_actionable_partial_failure_details(self):
        report = RunReport(
            started_at=datetime(2026, 5, 27, 8, 0, 0),
            finished_at=datetime(2026, 5, 27, 8, 1, 0),
            status="partial_failed",
            missing_transcript_count=1,
            summary_failed_count=1,
            missing_by_column={"栏目A": ["无文字稿标题（too_short）"]},
            summary_failed_by_column={"栏目B": ["摘要失败标题（Authorization: Bearer abc.def）"]},
        )

        text = format_run_report(report)

        self.assertIn("无文字稿/待处理：", text)
        self.assertIn("栏目A：无文字稿标题", text)
        self.assertIn("摘要失败：", text)
        self.assertIn("栏目B：摘要失败标题", text)
        self.assertNotIn("abc.def", text)

    def test_report_can_hide_item_titles(self):
        report = RunReport(
            started_at=datetime(2026, 5, 27, 8, 0, 0),
            finished_at=datetime(2026, 5, 27, 8, 1, 0),
            status="partial_failed",
            new_count=1,
            added_by_column={"栏目A": ["敏感标题"]},
            failures=["栏目A/敏感标题: api failed"],
        )

        text = format_run_report(report, include_titles=False)

        self.assertIn("新增文章数：1", text)
        self.assertIn("已按配置隐藏标题", text)
        self.assertNotIn("敏感标题", text)
        self.assertNotIn("api failed", text)

    def test_report_redacts_added_titles(self):
        report = RunReport(
            started_at=datetime(2026, 5, 27, 8, 0, 0),
            finished_at=datetime(2026, 5, 27, 8, 1, 0),
            status="success",
            added_by_column={"栏目A": ["Authorization: Bearer abc.def"]},
        )

        text = format_run_report(report)

        self.assertIn("[REDACTED]", text)
        self.assertNotIn("abc.def", text)

    def test_report_shows_truncated_detail_count(self):
        report = RunReport(
            started_at=datetime(2026, 5, 27, 8, 0, 0),
            finished_at=datetime(2026, 5, 27, 8, 1, 0),
            status="partial_failed",
            added_by_column={"栏目A": [f"标题{i}" for i in range(12)]},
            failures=[f"失败{i}" for i in range(11)],
        )

        text = format_run_report(report)

        self.assertIn("还有 2 条，详见日志或 list 命令。", text)
        self.assertIn("还有 1 条，详见日志或 list 命令。", text)
        self.assertIn("标题9", text)
        self.assertNotIn("标题10", text)
        self.assertIn("失败9", text)
        self.assertNotIn("失败10", text)

    def test_http_error_body_is_redacted(self):
        report = RunReport(started_at=datetime.now(), finished_at=datetime.now(), status="success")
        error = urllib.error.HTTPError(
            "https://open.feishu.cn/open-apis/bot/v2/hook/abcdef",
            400,
            "Bad Request",
            hdrs=None,
            fp=io.BytesIO(b'{"msg":"api_key=sk-test Authorization: Bearer abc.def"}'),
        )

        with mock.patch("urllib.request.urlopen", side_effect=error):
            with self.assertRaises(NotificationError) as raised:
                FeishuNotifier(FeishuCredentials("https://example.com/hook")).send_run_report(report)

        message = str(raised.exception)
        self.assertIn("feishu HTTP 400", message)
        self.assertIn("[REDACTED]", message)
        self.assertNotIn("sk-test", message)
        self.assertNotIn("abc.def", message)


if __name__ == "__main__":
    unittest.main()
