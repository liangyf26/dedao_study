from __future__ import annotations

import unittest
from datetime import datetime

from dedao_sync.models import RunReport
from dedao_sync.notifier import FeishuCredentials, FeishuNotifier, format_run_report, make_feishu_sign
from dedao_sync.security import redact


class NotifierTests(unittest.TestCase):
    def test_feishu_sign_known_shape(self):
        sign = make_feishu_sign(1234567890, "secret")
        self.assertTrue(sign)
        self.assertNotIn("secret", sign)

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
        text = redact("Authorization: Bearer abc.def and https://open.feishu.cn/open-apis/bot/v2/hook/abcdef")
        self.assertIn("[REDACTED]", text)
        self.assertNotIn("abc.def", text)

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
        self.assertIn("失败文章数：1", text)
        self.assertIn("无文字稿文章数：1", text)
        self.assertIn("摘要失败数：1", text)
        self.assertNotIn("abc.def", text)


if __name__ == "__main__":
    unittest.main()
