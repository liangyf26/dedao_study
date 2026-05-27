from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

from dedao_sync.cli import main
from dedao_sync.models import ContentItem, RunReport, STATUS_FAILED, STATUS_SYNCED, SummaryResult
from dedao_sync.notifier import NotificationError
from dedao_sync.repository import SyncRepository
from dedao_sync.summarizer import SummaryError
from dedao_sync.sync import default_db_path


class CliTests(unittest.TestCase):
    def test_init_creates_config_without_overwriting(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.assertEqual(main(["init", "--root", str(root)]), 0)
            config = root / "config.yaml"
            env = root / ".env"
            self.assertTrue(config.exists())
            self.assertTrue(env.exists())
            config.write_text("custom", encoding="utf-8")
            self.assertEqual(main(["init", "--root", str(root)]), 0)
            self.assertEqual(config.read_text(encoding="utf-8"), "custom")

    def test_doctor_help(self):
        with self.assertRaises(SystemExit) as raised:
            main(["doctor", "--help"])
        self.assertEqual(raised.exception.code, 0)

    def test_doctor_json_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.assertEqual(main(["init", "--root", str(root)]), 0)
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                main(["doctor", "--config", str(root / "config.yaml"), "--no-auth", "--json"])
            parsed = json.loads(output.getvalue())
            self.assertTrue(isinstance(parsed, list))
            self.assertIn("name", parsed[0])

    def test_parse_snapshot_show_candidates(self):
        with tempfile.TemporaryDirectory() as tmp:
            html = Path(tmp) / "page.html"
            html.write_text(
                """
                <html><body><article>
                <h1>健康参考</h1>
                <p>健康参考第一段内容足够长，用于验证候选诊断输出。这里补充事实、判断和背景，形成自然段落，并继续说明问题出现的具体场景、上下文和读者需要留意的变量。</p>
                <p>第二段继续展开核心观点，说明适用边界和例外情况，让正文长度超过质量门槛。这里还补充一组对比，帮助判断这个观点什么时候成立，什么时候需要谨慎使用。</p>
                <p>第三段提供行动建议和复盘问题，保证候选可以被选择并输出诊断。最后补充后续观察信号，让这段内容更接近真实课程文稿。为了让测试样本足够接近长文稿，这里继续加入一段说明：用户可以把这些观察信号写进每日笔记，并在下一次复盘时检查判断是否仍然成立。还可以把不同栏目中的相近观点串联起来，形成一个更稳定的主题索引。</p>
                </article></body></html>
                """,
                encoding="utf-8",
            )
            code = main(
                [
                    "parse-snapshot",
                    str(html),
                    "--title",
                    "健康参考",
                    "--column",
                    "栏目",
                    "--url",
                    "https://www.dedao.cn/course/detail?id=x",
                    "--show-candidates",
                    "--show-items",
                ]
            )
            self.assertEqual(code, 0)

    def test_parse_snapshot_json_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            html = Path(tmp) / "page.html"
            html.write_text(
                """
                <html><body>
                <a href="/course/detail?id=abc">健康参考 标题</a>
                <article>
                <h1>健康参考 标题</h1>
                <p>健康参考 标题，这是第一段内容，长度足够用于质量判断。这里继续补充背景、事实和判断，让正文接近真实课程文稿的密度，并继续描述具体场景、判断依据和需要留意的变量。为了模拟真实栏目文稿，这一段还会说明一个健康议题如何从新闻事件进入日常生活，并解释读者为什么需要区分短期反应和长期趋势。</p>
                <p>第二段继续展开一个完整观点，解释原因、条件和限制，并保留足够多的自然语言段落。这里继续说明观点的适用边界：不是所有情形都能套用同一个结论，需要结合个体差异、时间尺度、检测方式和生活方式来理解。文稿还会给出一个反例，提醒读者不要把相关性误读成确定的因果关系。</p>
                <p>第三段补充边界和观察，形成可解析的正文。最后再加入行动建议、复盘问题和后续观察信号，确保质量门槛可以通过，并方便后续把快照解析结果用于回归测试。比如可以把睡眠、运动、饮食和情绪状态拆成四个观察维度，每周只复盘一个维度，避免把记录工作变成新的负担。</p>
                <p>第四段把内容转化为卡片笔记需要的结构：一个原子观点、一个适用边界、一个可观察信号和一个待验证问题。这样生成摘要时，模型不只是在压缩原文，而是在帮用户把栏目中的知识转成可以长期连接的主题节点。</p>
                </article>
                </body></html>
                """,
                encoding="utf-8",
            )

            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                code = main(
                    [
                        "parse-snapshot",
                        str(html),
                        "--title",
                        "健康参考 标题",
                        "--column",
                        "栏目",
                        "--url",
                        "https://www.dedao.cn/course/detail?id=course",
                        "--json",
                    ]
                )

            self.assertEqual(code, 0)
            parsed = json.loads(output.getvalue())
            self.assertTrue(parsed["has_transcript"])
            self.assertGreater(parsed["transcript_chars"], 400)
            self.assertEqual(parsed["candidate_items"], 1)
            self.assertEqual(parsed["item_candidates"][0]["dedao_id"], "abc")
            self.assertTrue(any(candidate["selected"] for candidate in parsed["transcript_candidates"]))

    def test_list_runs_outputs_recent_run_summary(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.assertEqual(main(["init", "--root", str(root)]), 0)
            repo = SyncRepository(default_db_path(root))
            repo.migrate()
            report = RunReport(
                started_at=datetime(2026, 5, 27, 8, 30, 0),
                total_columns=4,
                log_path=Path("logs/test.log"),
            )
            run_id = repo.start_run(report)
            report.status = "partial_failed"
            report.discovered_count = 6
            report.new_count = 2
            report.success_count = 1
            report.failed_count = 1
            report.finished_at = datetime(2026, 5, 27, 8, 31, 0)
            repo.finish_run(run_id, report)

            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                code = main(["list", "--config", str(root / "config.yaml"), "--runs"])

            self.assertEqual(code, 0)
            text = output.getvalue()
            self.assertIn(str(run_id), text)
            self.assertIn("partial_failed", text)
            self.assertIn("columns=4", text)
            self.assertIn("discovered=6", text)
            self.assertIn("new=2", text)
            self.assertIn("success=1", text)
            self.assertIn("failed=1", text)
            self.assertIn(str(Path("logs/test.log")), text)

    def test_list_failed_outputs_items_needing_attention(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.assertEqual(main(["init", "--root", str(root)]), 0)
            repo = SyncRepository(default_db_path(root))
            repo.migrate()
            failed = ContentItem(
                source_url="https://example.com/failed",
                detail_url="https://example.com/failed",
                dedao_id="failed",
                column_name="栏目",
                title="失败标题",
            )
            synced = ContentItem(
                source_url="https://example.com/synced",
                detail_url="https://example.com/synced",
                dedao_id="synced",
                column_name="栏目",
                title="成功标题",
            )
            repo.upsert_item(failed, status=STATUS_FAILED, error_message="页面结构变化")
            repo.upsert_item(synced, status=STATUS_SYNCED, file_path="note.md", has_transcript=True)

            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                code = main(["list", "--config", str(root / "config.yaml"), "--failed"])

            self.assertEqual(code, 0)
            text = output.getvalue()
            self.assertIn("失败标题", text)
            self.assertIn("页面结构变化", text)
            self.assertNotIn("成功标题", text)

    def test_list_run_id_outputs_item_actions(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.assertEqual(main(["init", "--root", str(root)]), 0)
            repo = SyncRepository(default_db_path(root))
            repo.migrate()
            report = RunReport(started_at=datetime(2026, 5, 27, 9, 0, 0), total_columns=1)
            run_id = repo.start_run(report)
            item = ContentItem(
                source_url="https://example.com/item",
                detail_url="https://example.com/item",
                dedao_id="item",
                column_name="栏目",
                title="动作标题",
            )
            item_id = repo.upsert_item(item, status=STATUS_FAILED, error_message="详情页失败")
            repo.add_run_item(run_id, item_id, "sync", STATUS_FAILED, "抓取失败")

            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                code = main(["list", "--config", str(root / "config.yaml"), "--run-id", str(run_id)])

            self.assertEqual(code, 0)
            text = output.getvalue()
            self.assertIn(str(run_id), text)
            self.assertIn(str(item_id), text)
            self.assertIn("sync", text)
            self.assertIn("failed", text)
            self.assertIn("动作标题", text)
            self.assertIn("抓取失败", text)

    def test_notify_test_reports_notification_error_without_traceback(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.assertEqual(main(["init", "--root", str(root)]), 0)
            stderr = io.StringIO()
            with mock.patch("dedao_sync.cli.FeishuNotifier.send_run_report", side_effect=NotificationError("network denied")):
                with contextlib.redirect_stderr(stderr):
                    code = main(["notify-test", "--config", str(root / "config.yaml")])

            self.assertEqual(code, 1)
            self.assertIn("notification failed: network denied", stderr.getvalue())
            self.assertNotIn("Traceback", stderr.getvalue())

    def test_summary_test_reports_success(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.assertEqual(main(["init", "--root", str(root)]), 0)

            class FakeSummaryService:
                def summarize(self, detail):
                    return SummaryResult(
                        atomic_cards=("卡片",),
                        permanent_note="永久笔记",
                        keywords=("测试", "摘要"),
                    )

            output = io.StringIO()
            with mock.patch("dedao_sync.cli.create_summary_service", return_value=FakeSummaryService()):
                with contextlib.redirect_stdout(output):
                    code = main(["summary-test", "--config", str(root / "config.yaml")])

            self.assertEqual(code, 0)
            text = output.getvalue()
            self.assertIn("summary ok", text)
            self.assertIn("atomic_cards=1", text)
            self.assertIn("keywords=测试,摘要", text)

    def test_summary_test_reports_summary_error_without_traceback(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.assertEqual(main(["init", "--root", str(root)]), 0)

            class FailingSummaryService:
                def summarize(self, detail):
                    raise SummaryError("api denied")

            stderr = io.StringIO()
            with mock.patch("dedao_sync.cli.create_summary_service", return_value=FailingSummaryService()):
                with contextlib.redirect_stderr(stderr):
                    code = main(["summary-test", "--config", str(root / "config.yaml")])

            self.assertEqual(code, 1)
            self.assertIn("summary failed: api denied", stderr.getvalue())
            self.assertNotIn("Traceback", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
