from __future__ import annotations

import json
import logging
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from dedao_sync.crawler import CrawlResult
from dedao_sync.locking import RunLock
from dedao_sync.models import (
    ContentDetail,
    ContentItem,
    MediaCandidate,
    SummaryResult,
    STATUS_MISSING_TRANSCRIPT,
    STATUS_POLICY_BLOCKED,
    STATUS_EXTRACTOR_FAILED,
    STATUS_LOGIN_REQUIRED,
    STATUS_LOCKED,
    STATUS_SUMMARY_FAILED,
    STATUS_SYNCED,
    STATUS_TRANSCRIPTION_FAILED,
)
from dedao_sync.repository import SyncRepository
from dedao_sync.summarizer import SummaryError
from dedao_sync.sync import default_db_path, default_lock_path, run_preflight, run_resummarize, run_retry_failed, run_sync


VALID_AUTH_STATE = '{"cookies":[{"name":"sid","value":"test","domain":".dedao.cn","path":"/"}],"origins":[]}'


def write_config(root: Path) -> Path:
    vault = root / "vault"
    vault.mkdir()
    config = {
        "obsidian": {
            "vault_path": str(vault),
            "output_dir": "得到",
            "filename_pattern": "{column}-{published_date}-{title}.md",
        },
        "dedao": {
            "auth_state_path": "data/auth/dedao_state.json",
            "browser_profile_dir": "data/browser_profile",
            "headless": False,
            "request_interval_seconds": 2,
            "columns": [{"name": "栏目", "url": "https://example.com", "enabled": True}],
        },
        "summary": {
            "enabled": False,
            "provider": "opencode_go",
            "model": "deepseek-v4-pro",
            "base_url_env": "BASE",
            "api_key_env": "KEY",
        },
        "transcription": {
            "enabled": False,
            "provider": "faster_whisper",
            "delete_media_after_transcription": True,
            "temp_dir": "data/media_cache",
        },
        "feishu": {
            "enabled": False,
            "webhook_url_env": "WEBHOOK",
            "secret_env": "SECRET",
        },
    }
    path = root / "config.json"
    path.write_text(json.dumps(config, ensure_ascii=False), encoding="utf-8")
    return path


def write_config_with_overrides(root: Path, overrides: dict) -> Path:
    path = write_config(root)
    config = json.loads(path.read_text(encoding="utf-8"))
    for section, values in overrides.items():
        config[section].update(values)
    path.write_text(json.dumps(config, ensure_ascii=False), encoding="utf-8")
    return path


class SyncTests(unittest.TestCase):
    def test_preflight_success_without_auth_requirement(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_path = write_config(root)
            report, run_id = run_preflight(config_path, require_auth=False)
            self.assertEqual(report.status, "success")
            self.assertIsNotNone(run_id)
            self.assertTrue((root / "data" / "dedao_sync.sqlite3").exists())
            self.assertTrue((root / "logs").exists())
            logging.shutdown()

    def test_preflight_notification_failure_is_logged_but_not_fatal(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_path = write_config_with_overrides(
                root,
                {
                    "feishu": {
                        "enabled": True,
                        "webhook_url_env": "FEISHU_WEBHOOK_URL_TEST",
                    }
                },
            )

            with mock.patch.dict("os.environ", {"FEISHU_WEBHOOK_URL_TEST": "https://example.com/hook"}):
                with mock.patch("dedao_sync.sync.FeishuNotifier.send_run_report", side_effect=RuntimeError("network denied")):
                    with self.assertLogs("dedao_sync.sync", level="WARNING") as captured:
                        report, run_id = run_preflight(config_path, require_auth=False)

            self.assertEqual(report.status, "success")
            self.assertIsNotNone(run_id)
            self.assertTrue(any("feishu notification failed: network denied" in line for line in captured.output))
            logging.shutdown()

    def test_sync_writes_note_records_db_and_skips_second_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_path = write_config(root)
            auth = root / "data" / "auth" / "dedao_state.json"
            auth.parent.mkdir(parents=True)
            auth.write_text(VALID_AUTH_STATE, encoding="utf-8")
            item = ContentItem(
                source_url="https://example.com/item/1",
                detail_url="https://example.com/item/1",
                dedao_id="1",
                column_name="栏目",
                title="健康参考 标题",
                published_at="2026-05-27",
            )
            crawler = FakeCrawler([item], {"1": "健康参考 标题\n\n第一段内容很长，足够形成正文。\n\n第二段继续展开。\n\n第三段给出边界。"})
            report, _ = run_sync(config_path, crawler=crawler, summary_service=FakeSummary(), notifier=FakeNotifier())
            self.assertEqual(report.status, "success")
            self.assertEqual(report.success_count, 1)
            self.assertEqual(report.request_count, 3)
            notes = list((root / "vault" / "得到" / "栏目").glob("*.md"))
            self.assertEqual(len(notes), 1)
            self.assertIn("## 原子卡片", notes[0].read_text(encoding="utf-8"))
            rows = SyncRepository(default_db_path(root)).list_items()
            self.assertEqual(rows[0]["status"], STATUS_SYNCED)

            report2, _ = run_sync(config_path, crawler=crawler, summary_service=FakeSummary(), notifier=FakeNotifier())
            self.assertEqual(report2.skipped_count, 1)
            self.assertEqual(len(list((root / "vault" / "得到" / "栏目").glob("*.md"))), 1)
            logging.shutdown()

    def test_sync_records_detail_metadata_from_extractor(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_path = write_config(root)
            auth = root / "data" / "auth" / "dedao_state.json"
            auth.parent.mkdir(parents=True)
            auth.write_text(VALID_AUTH_STATE, encoding="utf-8")
            list_item = ContentItem(
                source_url="https://example.com/item/meta",
                detail_url="https://example.com/item/meta",
                dedao_id="meta",
                column_name="栏目",
                title="列表页标题",
            )
            detail_item = ContentItem(
                source_url=list_item.source_url,
                detail_url=list_item.detail_url,
                dedao_id=list_item.dedao_id,
                column_name=list_item.column_name,
                title="健康参考 真实标题",
                published_at="2026-05-27T08:00:00+08:00",
                author="尹烨",
                content_type=list_item.content_type,
            )
            crawler = DetailMetadataCrawler(list_item, detail_item)

            report, _ = run_sync(config_path, crawler=crawler, summary_service=FakeSummary(), notifier=FakeNotifier())

            self.assertEqual(report.status, "success")
            self.assertEqual(report.added_by_column["栏目"], ["健康参考 真实标题"])
            note = next((root / "vault" / "得到" / "栏目").glob("*.md"))
            body = note.read_text(encoding="utf-8")
            self.assertIn('title: "健康参考 真实标题"', body)
            self.assertIn('author: "尹烨"', body)
            self.assertIn('published: "2026-05-27T08:00:00+08:00"', body)
            rows = SyncRepository(default_db_path(root)).list_items()
            self.assertEqual(rows[0]["title"], "健康参考 真实标题")
            self.assertEqual(rows[0]["published_at"], "2026-05-27T08:00:00+08:00")
            logging.shutdown()

    def test_sync_skips_duplicate_content_hash_before_writing_second_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_path = write_config(root)
            auth = root / "data" / "auth" / "dedao_state.json"
            auth.parent.mkdir(parents=True)
            auth.write_text(VALID_AUTH_STATE, encoding="utf-8")
            first = ContentItem("https://example.com/a", "栏目", "健康参考 A", "https://example.com/a", dedao_id="a")
            second = ContentItem("https://example.com/b", "栏目", "健康参考 B", "https://example.com/b", dedao_id="b")
            transcript = "健康参考 A B\n\n第一段内容很长，足够形成正文。\n\n第二段继续展开。\n\n第三段给出边界。"
            crawler = FakeCrawler([first], {"a": transcript})
            report, _ = run_sync(config_path, crawler=crawler, summary_service=FakeSummary(), notifier=FakeNotifier())
            self.assertEqual(report.success_count, 1)

            crawler2 = FakeCrawler([second], {"b": transcript})
            report2, _ = run_sync(config_path, crawler=crawler2, summary_service=FakeSummary(), notifier=FakeNotifier())
            self.assertEqual(report2.skipped_count, 1)
            self.assertEqual(len(list((root / "vault" / "得到" / "栏目").glob("*.md"))), 1)
            logging.shutdown()

    def test_dry_run_does_not_pollute_item_table(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_path = write_config(root)
            auth = root / "data" / "auth" / "dedao_state.json"
            auth.parent.mkdir(parents=True)
            auth.write_text(VALID_AUTH_STATE, encoding="utf-8")
            item = ContentItem("https://example.com/dry", "栏目", "健康参考 Dry", "https://example.com/dry", dedao_id="dry")
            crawler = FakeCrawler([item], {"dry": "健康参考 Dry\n\n第一段内容很长。\n\n第二段继续展开。\n\n第三段补充。"})
            report, _ = run_sync(config_path, dry_run=True, crawler=crawler, summary_service=FakeSummary(), notifier=FakeNotifier())
            self.assertEqual(report.new_count, 1)
            self.assertEqual(report.skipped_count, 1)
            self.assertEqual(report.request_count, 2)
            self.assertEqual(SyncRepository(default_db_path(root)).list_items(), [])
            report2, _ = run_sync(config_path, crawler=crawler, summary_service=FakeSummary(), notifier=FakeNotifier())
            self.assertEqual(report2.success_count, 1)
            logging.shutdown()

    def test_sync_limit_stops_after_new_item_and_logs_progress(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_path = write_config(root)
            auth = root / "data" / "auth" / "dedao_state.json"
            auth.parent.mkdir(parents=True)
            auth.write_text(VALID_AUTH_STATE, encoding="utf-8")
            first = ContentItem("https://example.com/one", "栏目", "健康参考 One", "https://example.com/one", dedao_id="one")
            second = ContentItem("https://example.com/two", "栏目", "健康参考 Two", "https://example.com/two", dedao_id="two")
            crawler = FakeCrawler(
                [first, second],
                {
                    "one": "健康参考 One\n\n第一段内容很长。\n\n第二段继续展开。\n\n第三段补充。",
                    "two": "健康参考 Two\n\n第一段内容很长。\n\n第二段继续展开。\n\n第三段补充。",
                },
            )

            with self.assertLogs("dedao_sync.sync", level="INFO") as captured:
                report, _ = run_sync(
                    config_path,
                    crawler=crawler,
                    summary_service=FakeSummary(),
                    notifier=FakeNotifier(),
                    limit=1,
                )

            self.assertEqual(report.status, "success")
            self.assertEqual(report.new_count, 1)
            self.assertEqual(report.success_count, 1)
            rows = SyncRepository(default_db_path(root)).list_items()
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["dedao_id"], "one")
            logs = "\n".join(captured.output)
            self.assertIn("column discovered: 栏目 items=2", logs)
            self.assertIn("fetching detail: 栏目 - 健康参考 One", logs)
            self.assertIn("sync limit reached: 1 new item(s); stopping", logs)
            logging.shutdown()

    def test_run_sync_can_skip_notification_for_manual_check(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_path = write_config_with_overrides(
                root,
                {
                    "feishu": {
                        "enabled": True,
                        "webhook_url_env": "MISSING_FEISHU_WEBHOOK",
                    }
                },
            )
            auth = root / "data" / "auth" / "dedao_state.json"
            auth.parent.mkdir(parents=True)
            auth.write_text(VALID_AUTH_STATE, encoding="utf-8")
            item = ContentItem("https://example.com/check", "栏目", "健康参考 Check", "https://example.com/check", dedao_id="check")
            crawler = FakeCrawler([item], {"check": "健康参考 Check\n\n第一段内容很长。\n\n第二段继续展开。\n\n第三段补充。"})
            notifier = FakeNotifier()

            report, _ = run_sync(
                config_path,
                dry_run=True,
                crawler=crawler,
                summary_service=FakeSummary(),
                notifier=notifier,
                send_notification=False,
            )

            self.assertEqual(report.new_count, 1)
            self.assertNotEqual(report.status, "preflight_failed")
            self.assertEqual(notifier.reports, [])
            logging.shutdown()

    def test_run_sync_sends_notification_by_default(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_path = write_config(root)
            auth = root / "data" / "auth" / "dedao_state.json"
            auth.parent.mkdir(parents=True)
            auth.write_text(VALID_AUTH_STATE, encoding="utf-8")
            item = ContentItem("https://example.com/notify", "栏目", "健康参考 Notify", "https://example.com/notify", dedao_id="notify")
            crawler = FakeCrawler([item], {"notify": "健康参考 Notify\n\n第一段内容很长。\n\n第二段继续展开。\n\n第三段补充。"})
            notifier = FakeNotifier()

            report, _ = run_sync(config_path, crawler=crawler, summary_service=FakeSummary(), notifier=notifier)

            self.assertEqual(report.status, "success")
            self.assertEqual(len(notifier.reports), 1)
            logging.shutdown()

    def test_login_expired_is_recorded_and_notified(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_path = write_config(root)
            auth = root / "data" / "auth" / "dedao_state.json"
            auth.parent.mkdir(parents=True)
            auth.write_text(VALID_AUTH_STATE, encoding="utf-8")
            notifier = FakeNotifier()

            report, run_id = run_sync(
                config_path,
                crawler=ExpiredLoginCrawler(),
                summary_service=FakeSummary(),
                notifier=notifier,
            )

            self.assertEqual(report.status, STATUS_LOGIN_REQUIRED)
            self.assertEqual(report.failed_count, 1)
            self.assertEqual(report.request_count, 1)
            self.assertTrue(any("重新运行 dedao-sync login" in failure for failure in report.failures))
            self.assertEqual(len(notifier.reports), 1)
            self.assertEqual(notifier.reports[0].status, STATUS_LOGIN_REQUIRED)
            runs = SyncRepository(default_db_path(root)).list_runs()
            self.assertEqual(runs[0]["id"], run_id)
            self.assertEqual(runs[0]["status"], STATUS_LOGIN_REQUIRED)
            self.assertIn("dedao-sync login", runs[0]["error_message"])
            logging.shutdown()

    def test_empty_untrusted_crawl_result_is_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_path = write_config(root)
            auth = root / "data" / "auth" / "dedao_state.json"
            auth.parent.mkdir(parents=True)
            auth.write_text(VALID_AUTH_STATE, encoding="utf-8")
            report, _ = run_sync(config_path, crawler=FakeCrawler([], {}), summary_service=FakeSummary(), notifier=FakeNotifier())
            self.assertEqual(report.status, "partial_failed")
            self.assertEqual(report.failed_count, 1)
            self.assertIn("页面解析失败", report.failures[0])
            logging.shutdown()

    def test_unknown_column_filter_is_preflight_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_path = write_config(root)
            auth = root / "data" / "auth" / "dedao_state.json"
            auth.parent.mkdir(parents=True)
            auth.write_text(VALID_AUTH_STATE, encoding="utf-8")

            report, _ = run_sync(
                config_path,
                column_name="不存在的栏目",
                crawler=FakeCrawler([], {}),
                summary_service=FakeSummary(),
                notifier=FakeNotifier(),
            )

            self.assertEqual(report.status, "preflight_failed")
            self.assertEqual(report.failed_count, 1)
            self.assertIn("未找到启用的栏目", report.failures[0])
            logging.shutdown()

    def test_sync_requires_feishu_webhook_when_feishu_enabled(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_path = write_config_with_overrides(
                root,
                {
                    "feishu": {
                        "enabled": True,
                        "webhook_url_env": "MISSING_FEISHU_WEBHOOK",
                    }
                },
            )
            auth = root / "data" / "auth" / "dedao_state.json"
            auth.parent.mkdir(parents=True)
            auth.write_text(VALID_AUTH_STATE, encoding="utf-8")

            report, _ = run_sync(config_path, crawler=FakeCrawler([], {}), summary_service=FakeSummary(), notifier=FakeNotifier())

            self.assertEqual(report.status, "preflight_failed")
            self.assertTrue(any("Feishu webhook env is missing" in failure for failure in report.failures))
            logging.shutdown()

    def test_retry_failed_requires_feishu_webhook_when_feishu_enabled(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_path = write_config_with_overrides(
                root,
                {
                    "feishu": {
                        "enabled": True,
                        "webhook_url_env": "MISSING_FEISHU_WEBHOOK",
                    }
                },
            )
            auth = root / "data" / "auth" / "dedao_state.json"
            auth.parent.mkdir(parents=True)
            auth.write_text(VALID_AUTH_STATE, encoding="utf-8")

            report, _ = run_retry_failed(config_path, crawler=FakeCrawler([], {}), summary_service=FakeSummary(), notifier=FakeNotifier())

            self.assertEqual(report.status, "preflight_failed")
            self.assertTrue(any("Feishu webhook env is missing" in failure for failure in report.failures))
            logging.shutdown()

    def test_resummarize_requires_feishu_webhook_when_feishu_enabled(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_path = write_config_with_overrides(
                root,
                {
                    "feishu": {
                        "enabled": True,
                        "webhook_url_env": "MISSING_FEISHU_WEBHOOK",
                    }
                },
            )

            report, _ = run_resummarize(config_path, summary_service=FakeSummary(), notifier=FakeNotifier())

            self.assertEqual(report.status, "preflight_failed")
            self.assertTrue(any("Feishu webhook env is missing" in failure for failure in report.failures))
            logging.shutdown()

    def test_missing_transcript_marks_run_partial_failed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_path = write_config(root)
            auth = root / "data" / "auth" / "dedao_state.json"
            auth.parent.mkdir(parents=True)
            auth.write_text(VALID_AUTH_STATE, encoding="utf-8")
            item = ContentItem("https://example.com/m", "栏目", "健康参考 M", "https://example.com/m", dedao_id="m")
            crawler = MissingTranscriptCrawler(item)

            report, _ = run_sync(config_path, crawler=crawler, summary_service=FakeSummary(), notifier=FakeNotifier())

            self.assertEqual(report.status, "partial_failed")
            self.assertEqual(report.missing_transcript_count, 1)
            self.assertEqual(report.missing_by_column["栏目"], ["健康参考 M"])
            rows = SyncRepository(default_db_path(root)).list_items()
            self.assertEqual(rows[0]["status"], STATUS_MISSING_TRANSCRIPT)
            logging.shutdown()

    def test_missing_transcript_records_media_candidate_summary(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_path = write_config(root)
            auth = root / "data" / "auth" / "dedao_state.json"
            auth.parent.mkdir(parents=True)
            auth.write_text(VALID_AUTH_STATE, encoding="utf-8")
            item = ContentItem("https://example.com/media", "栏目", "健康参考 Media", "https://example.com/media", dedao_id="media")
            crawler = MissingTranscriptCrawler(
                item,
                media_candidates=(
                    MediaCandidate("https://example.com/audio.mp3", "audio/mpeg", "audio"),
                    MediaCandidate("https://example.com/video.mp4", "video/mp4", "video"),
                ),
            )

            report, run_id = run_sync(config_path, crawler=crawler, summary_service=FakeSummary(), notifier=FakeNotifier())

            self.assertEqual(report.status, "partial_failed")
            rows = SyncRepository(default_db_path(root)).list_items()
            self.assertIn("media_candidates=2", rows[0]["error_message"])
            run_items = SyncRepository(default_db_path(root)).list_run_items(run_id)
            self.assertIn("audio/mpeg", run_items[0]["message"])
            logging.shutdown()

    def test_extractor_failure_records_diagnostic_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_path = write_config(root)
            auth = root / "data" / "auth" / "dedao_state.json"
            auth.parent.mkdir(parents=True)
            auth.write_text(VALID_AUTH_STATE, encoding="utf-8")
            item = ContentItem("https://example.com/e", "栏目", "健康参考 E", "https://example.com/e", dedao_id="e")
            diagnostic_path = root / "data" / "page_failures" / "failed.html"
            crawler = ExtractorFailureCrawler(item, diagnostic_path)

            report, run_id = run_sync(config_path, crawler=crawler, summary_service=FakeSummary(), notifier=FakeNotifier())

            self.assertEqual(report.status, "partial_failed")
            self.assertEqual(report.missing_transcript_count, 1)
            self.assertIn("健康参考 E（too_short", report.missing_by_column["栏目"][0])
            repo = SyncRepository(default_db_path(root))
            rows = repo.list_items()
            self.assertEqual(rows[0]["status"], STATUS_EXTRACTOR_FAILED)
            self.assertIn("too_short", rows[0]["error_message"])
            self.assertIn(str(diagnostic_path), rows[0]["error_message"])
            run_items = repo.list_run_items(run_id)
            self.assertIn(str(diagnostic_path), run_items[0]["message"])
            logging.shutdown()

    def test_policy_blocked_item_is_not_written_as_missing_transcript(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_path = write_config(root)
            auth = root / "data" / "auth" / "dedao_state.json"
            auth.parent.mkdir(parents=True)
            auth.write_text(VALID_AUTH_STATE, encoding="utf-8")
            item = ContentItem("https://example.com/policy", "栏目", "健康参考 Policy", "https://example.com/policy", dedao_id="policy")
            crawler = PolicyBlockedCrawler(item)

            report, run_id = run_sync(config_path, crawler=crawler, summary_service=FakeSummary(), notifier=FakeNotifier())

            self.assertEqual(report.status, "partial_failed")
            self.assertEqual(report.failed_count, 1)
            self.assertEqual(report.missing_transcript_count, 0)
            self.assertTrue(any("policy_blocked:encrypted_hls_key" in failure for failure in report.failures))
            repo = SyncRepository(default_db_path(root))
            rows = repo.list_items()
            self.assertEqual(rows[0]["status"], STATUS_POLICY_BLOCKED)
            self.assertIn("policy_blocked:encrypted_hls_key", rows[0]["error_message"])
            self.assertEqual(list((root / "vault" / "得到" / "栏目").glob("*.md")), [])
            run_items = repo.list_run_items(run_id)
            self.assertEqual(run_items[0]["action"], "policy")
            self.assertEqual(run_items[0]["run_item_status"], STATUS_POLICY_BLOCKED)
            logging.shutdown()

    def test_summary_failure_preserves_note_and_marks_run_partial_failed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_path = write_config(root)
            auth = root / "data" / "auth" / "dedao_state.json"
            auth.parent.mkdir(parents=True)
            auth.write_text(VALID_AUTH_STATE, encoding="utf-8")
            item = ContentItem("https://example.com/sf", "栏目", "健康参考 SF", "https://example.com/sf", dedao_id="sf")
            crawler = FakeCrawler([item], {"sf": "健康参考 SF\n\n第一段内容很长。\n\n第二段继续展开。\n\n第三段补充。"})

            report, _ = run_sync(config_path, crawler=crawler, summary_service=FailingSummary(), notifier=FakeNotifier())

            self.assertEqual(report.status, "partial_failed")
            self.assertEqual(report.success_count, 1)
            self.assertEqual(report.summary_failed_count, 1)
            self.assertIn("健康参考 SF", report.summary_failed_by_column["栏目"][0])
            notes = list((root / "vault" / "得到" / "栏目").glob("*.md"))
            self.assertEqual(len(notes), 1)
            self.assertIn("## 全文稿", notes[0].read_text(encoding="utf-8"))
            rows = SyncRepository(default_db_path(root)).list_items()
            self.assertEqual(rows[0]["status"], STATUS_SYNCED)
            self.assertEqual(rows[0]["summary_status"], STATUS_SUMMARY_FAILED)
            self.assertEqual(rows[0]["error_message"], "summary api failed")
            self.assertEqual(rows[0]["has_transcript"], 1)
            self.assertIsNotNone(rows[0]["synced_at"])
            logging.shutdown()

    def test_sync_reports_locked_when_another_run_is_active(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_path = write_config(root)
            lock = RunLock(default_lock_path(root))
            lock.acquire()
            try:
                report, _ = run_sync(config_path, crawler=FakeCrawler([], {}), summary_service=FakeSummary(), notifier=FakeNotifier())
                self.assertEqual(report.status, STATUS_LOCKED)
                self.assertEqual(report.failed_count, 1)
            finally:
                lock.release()
                logging.shutdown()

    def test_retry_failed_rewrites_failed_item(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_path = write_config(root)
            auth = root / "data" / "auth" / "dedao_state.json"
            auth.parent.mkdir(parents=True)
            auth.write_text(VALID_AUTH_STATE, encoding="utf-8")
            repo = SyncRepository(default_db_path(root))
            repo.migrate()
            item = ContentItem("https://example.com/f", "栏目", "健康参考 F", "https://example.com/f", dedao_id="f")
            repo.upsert_item(item, status="failed", error_message="old")

            crawler = FakeCrawler([], {"f": "健康参考 F\n\n第一段内容很长，足够形成正文。\n\n第二段继续展开。\n\n第三段给出边界。"})
            report, _ = run_retry_failed(config_path, crawler=crawler, summary_service=FakeSummary(), notifier=FakeNotifier())
            self.assertEqual(report.success_count, 1)
            self.assertEqual(report.request_count, 2)
            self.assertEqual(SyncRepository(default_db_path(root)).list_items()[0]["status"], STATUS_SYNCED)
            logging.shutdown()

    def test_retry_failed_records_summary_error_after_fetch(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_path = write_config(root)
            auth = root / "data" / "auth" / "dedao_state.json"
            auth.parent.mkdir(parents=True)
            auth.write_text(VALID_AUTH_STATE, encoding="utf-8")
            repo = SyncRepository(default_db_path(root))
            repo.migrate()
            item = ContentItem("https://example.com/r-sf", "栏目", "健康参考 Retry SF", "https://example.com/r-sf", dedao_id="r-sf")
            repo.upsert_item(item, status="failed", error_message="old")

            crawler = FakeCrawler([], {"r-sf": "健康参考 Retry SF\n\n第一段内容很长，足够形成正文。\n\n第二段继续展开。\n\n第三段给出边界。"})
            report, run_id = run_retry_failed(config_path, crawler=crawler, summary_service=FailingSummary(), notifier=FakeNotifier())

            self.assertEqual(report.status, "partial_failed")
            self.assertEqual(report.summary_failed_count, 1)
            row = SyncRepository(default_db_path(root)).list_items()[0]
            self.assertEqual(row["status"], STATUS_SYNCED)
            self.assertEqual(row["summary_status"], STATUS_SUMMARY_FAILED)
            self.assertEqual(row["error_message"], "summary api failed")
            run_items = repo.list_run_items(run_id)
            self.assertEqual(run_items[0]["run_item_status"], STATUS_SUMMARY_FAILED)
            self.assertEqual(run_items[0]["message"], "summary api failed")
            logging.shutdown()

    def test_retry_failed_includes_transcription_failed_items(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_path = write_config(root)
            auth = root / "data" / "auth" / "dedao_state.json"
            auth.parent.mkdir(parents=True)
            auth.write_text(VALID_AUTH_STATE, encoding="utf-8")
            repo = SyncRepository(default_db_path(root))
            repo.migrate()
            item = ContentItem("https://example.com/t", "栏目", "健康参考 T", "https://example.com/t", dedao_id="t")
            repo.upsert_item(item, status=STATUS_TRANSCRIPTION_FAILED, error_message="asr failed")

            crawler = FakeCrawler([], {"t": "健康参考 T\n\n第一段内容很长，足够形成正文。\n\n第二段继续展开。\n\n第三段给出边界。"})
            report, _ = run_retry_failed(config_path, crawler=crawler, summary_service=FakeSummary(), notifier=FakeNotifier())

            self.assertEqual(report.discovered_count, 1)
            self.assertEqual(report.success_count, 1)
            self.assertEqual(SyncRepository(default_db_path(root)).list_items()[0]["status"], STATUS_SYNCED)
            logging.shutdown()

    def test_retry_failed_does_not_auto_retry_policy_blocked_items(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_path = write_config(root)
            auth = root / "data" / "auth" / "dedao_state.json"
            auth.parent.mkdir(parents=True)
            auth.write_text(VALID_AUTH_STATE, encoding="utf-8")
            repo = SyncRepository(default_db_path(root))
            repo.migrate()
            item = ContentItem("https://example.com/policy-retry", "栏目", "健康参考 Policy", "https://example.com/policy-retry", dedao_id="policy-retry")
            repo.upsert_item(item, status=STATUS_POLICY_BLOCKED, error_message="policy_blocked:drm_widevine")

            report, _ = run_retry_failed(config_path, crawler=NoFetchCrawler(), summary_service=FakeSummary(), notifier=FakeNotifier())

            self.assertEqual(report.discovered_count, 0)
            self.assertEqual(SyncRepository(default_db_path(root)).list_items()[0]["status"], STATUS_POLICY_BLOCKED)
            logging.shutdown()

    def test_retry_failed_skips_saved_summary_failures_without_duplicate_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_path = write_config(root)
            auth = root / "data" / "auth" / "dedao_state.json"
            auth.parent.mkdir(parents=True)
            auth.write_text(VALID_AUTH_STATE, encoding="utf-8")
            repo = SyncRepository(default_db_path(root))
            repo.migrate()
            note_dir = root / "vault" / "得到" / "栏目"
            note_dir.mkdir(parents=True)
            note = note_dir / "栏目-2026-05-27-健康参考.md"
            note.write_text(
                "# 健康参考\n\n## 原子卡片\n\n> 摘要尚未生成。\n\n## 全文稿\n\n健康参考\n\n第一段内容很长。\n\n第二段继续展开。\n\n第三段补充。",
                encoding="utf-8",
            )
            item = ContentItem("https://example.com/rs", "栏目", "健康参考", "https://example.com/rs", dedao_id="rs")
            repo.upsert_item(
                item,
                status=STATUS_SUMMARY_FAILED,
                content_hash="hash-rs",
                file_path=note,
                has_transcript=True,
                summary_status=STATUS_SUMMARY_FAILED,
            )

            report, run_id = run_retry_failed(
                config_path,
                crawler=NoFetchCrawler(),
                summary_service=FakeSummary(),
                notifier=FakeNotifier(),
            )

            self.assertEqual(report.status, "success")
            self.assertEqual(report.discovered_count, 0)
            self.assertEqual(report.success_count, 0)
            self.assertEqual(len(list(note_dir.glob("*.md"))), 1)
            self.assertIn("摘要尚未生成", note.read_text(encoding="utf-8"))
            rows = SyncRepository(default_db_path(root)).list_items()
            self.assertEqual(rows[0]["status"], STATUS_SYNCED)
            self.assertEqual(rows[0]["file_path"], str(note))
            run_items = repo.list_run_items(run_id)
            self.assertEqual(run_items, [])
            logging.shutdown()

    def test_resummarize_overwrites_existing_note(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_path = write_config(root)
            repo = SyncRepository(default_db_path(root))
            repo.migrate()
            note_dir = root / "vault" / "得到" / "栏目"
            note_dir.mkdir(parents=True)
            note = note_dir / "栏目-2026-05-27-健康参考.md"
            note.write_text("# 旧标题\n\n## 全文稿\n\n健康参考\n\n第一段内容很长。\n\n第二段继续展开。\n\n第三段补充。", encoding="utf-8")
            item = ContentItem("https://example.com/s", "栏目", "健康参考", "https://example.com/s", dedao_id="s")
            repo.upsert_item(
                item,
                status="summary_failed",
                content_hash="hash-s",
                file_path=note,
                has_transcript=True,
                summary_status="summary_failed",
            )
            report, _ = run_resummarize(config_path, summary_service=FakeSummary(), notifier=FakeNotifier())
            self.assertEqual(report.success_count, 1)
            body = note.read_text(encoding="utf-8")
            self.assertIn("卡片", body)
            self.assertIn("## 全文稿", body)
            self.assertEqual(SyncRepository(default_db_path(root)).list_items()[0]["status"], STATUS_SYNCED)
            logging.shutdown()

    def test_resummarize_failure_preserves_transcript_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_path = write_config(root)
            repo = SyncRepository(default_db_path(root))
            repo.migrate()
            note_dir = root / "vault" / "得到" / "栏目"
            note_dir.mkdir(parents=True)
            note = note_dir / "栏目-2026-05-27-健康参考.md"
            note.write_text("# 标题\n\n## 全文稿\n\n健康参考\n\n第一段内容很长。\n\n第二段继续展开。\n\n第三段补充。", encoding="utf-8")
            item = ContentItem("https://example.com/sf-rs", "栏目", "健康参考", "https://example.com/sf-rs", dedao_id="sf-rs")
            repo.upsert_item(
                item,
                status=STATUS_SUMMARY_FAILED,
                content_hash="hash-sf-rs",
                file_path=note,
                has_transcript=True,
                summary_status=STATUS_SUMMARY_FAILED,
            )

            report, _ = run_resummarize(config_path, summary_service=FailingSummary(), notifier=FakeNotifier())

            self.assertEqual(report.status, "partial_failed")
            row = SyncRepository(default_db_path(root)).list_items()[0]
            self.assertEqual(row["status"], STATUS_SYNCED)
            self.assertEqual(row["summary_status"], STATUS_SUMMARY_FAILED)
            self.assertEqual(row["has_transcript"], 1)
            self.assertEqual(row["file_path"], str(note))
            logging.shutdown()

    def test_resummarize_default_skips_successful_summaries(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_path = write_config(root)
            repo = SyncRepository(default_db_path(root))
            repo.migrate()
            note_dir = root / "vault" / "得到" / "栏目"
            note_dir.mkdir(parents=True)
            note = note_dir / "栏目-2026-05-27-健康参考.md"
            note.write_text("# 旧标题\n\n## 全文稿\n\n健康参考\n\n第一段内容很长。\n\n第二段继续展开。\n\n第三段补充。", encoding="utf-8")
            item = ContentItem("https://example.com/ok-summary", "栏目", "健康参考", "https://example.com/ok-summary", dedao_id="ok-summary")
            repo.upsert_item(
                item,
                status=STATUS_SYNCED,
                content_hash="hash-ok-summary",
                file_path=note,
                has_transcript=True,
                summary_status="ok",
            )

            report, _ = run_resummarize(config_path, summary_service=FakeSummary(), notifier=FakeNotifier())

            self.assertEqual(report.discovered_count, 0)
            self.assertNotIn("卡片", note.read_text(encoding="utf-8"))
            logging.shutdown()

    def test_resummarize_all_refreshes_successful_summaries(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_path = write_config(root)
            repo = SyncRepository(default_db_path(root))
            repo.migrate()
            note_dir = root / "vault" / "得到" / "栏目"
            note_dir.mkdir(parents=True)
            note = note_dir / "栏目-2026-05-27-健康参考.md"
            note.write_text("# 旧标题\n\n## 全文稿\n\n健康参考\n\n第一段内容很长。\n\n第二段继续展开。\n\n第三段补充。", encoding="utf-8")
            item = ContentItem("https://example.com/all-summary", "栏目", "健康参考", "https://example.com/all-summary", dedao_id="all-summary")
            repo.upsert_item(
                item,
                status=STATUS_SYNCED,
                content_hash="hash-all-summary",
                file_path=note,
                has_transcript=True,
                summary_status="ok",
            )

            report, _ = run_resummarize(
                config_path,
                include_synced=True,
                summary_service=FakeSummary(),
                notifier=FakeNotifier(),
            )

            self.assertEqual(report.discovered_count, 1)
            self.assertEqual(report.success_count, 1)
            self.assertIn("卡片", note.read_text(encoding="utf-8"))
            logging.shutdown()


class FakeCrawler:
    def __init__(self, items: list[ContentItem], transcripts: dict[str, str], *, empty_but_valid: bool = False):
        self.items = items
        self.transcripts = transcripts
        self.empty_but_valid = empty_but_valid

    def check_login(self) -> bool:
        return True

    def list_items(self, column):
        return CrawlResult(items=self.items, empty_but_valid=self.empty_but_valid)

    def fetch_detail(self, item: ContentItem) -> ContentDetail:
        key = item.dedao_id or item.source_url
        text = self.transcripts[key]
        return ContentDetail(item=item, transcript_text=text, has_transcript=True, raw_html_hash=f"html-{key}")


class MissingTranscriptCrawler:
    def __init__(self, item: ContentItem, media_candidates=()):
        self.item = item
        self.media_candidates = tuple(media_candidates)

    def check_login(self) -> bool:
        return True

    def list_items(self, column):
        return CrawlResult(items=[self.item])

    def fetch_detail(self, item: ContentItem) -> ContentDetail:
        return ContentDetail(
            item=item,
            transcript_text="",
            has_transcript=False,
            media_candidates=self.media_candidates,
            raw_html_hash="html-missing",
        )


class ExtractorFailureCrawler:
    def __init__(self, item: ContentItem, diagnostic_path: Path):
        self.item = item
        self.diagnostic_path = diagnostic_path

    def check_login(self) -> bool:
        return True

    def list_items(self, column):
        return CrawlResult(items=[self.item])

    def fetch_detail(self, item: ContentItem) -> ContentDetail:
        return ContentDetail(
            item=item,
            transcript_text="",
            has_transcript=False,
            raw_html_hash="html-extractor-failed",
            quality_reason="too_short",
            diagnostic_path=self.diagnostic_path,
        )


class PolicyBlockedCrawler:
    def __init__(self, item: ContentItem):
        self.item = item

    def check_login(self) -> bool:
        return True

    def list_items(self, column):
        return CrawlResult(items=[self.item])

    def fetch_detail(self, item: ContentItem) -> ContentDetail:
        return ContentDetail(
            item=item,
            transcript_text="",
            has_transcript=False,
            raw_html_hash="html-policy",
            quality_reason="policy_blocked:encrypted_hls_key",
            media_candidates=(MediaCandidate("https://example.com/protected.m3u8", "application/vnd.apple.mpegurl", "video"),),
        )


class DetailMetadataCrawler:
    def __init__(self, list_item: ContentItem, detail_item: ContentItem):
        self.list_item = list_item
        self.detail_item = detail_item

    def check_login(self) -> bool:
        return True

    def list_items(self, column):
        return CrawlResult(items=[self.list_item])

    def fetch_detail(self, item: ContentItem) -> ContentDetail:
        return ContentDetail(
            item=self.detail_item,
            transcript_text=(
                "健康参考 真实标题\n\n"
                "第一段内容很长，足够形成正文，并且包含详情页真实标题。\n\n"
                "第二段继续展开，用于验证同步流程会采用 extractor 合并后的元数据。\n\n"
                "第三段给出边界，确保正文可以被正常写入 Obsidian。"
            ),
            has_transcript=True,
            raw_html_hash="html-meta",
        )


class NoFetchCrawler:
    def check_login(self) -> bool:
        return True

    def fetch_detail(self, item: ContentItem) -> ContentDetail:
        raise AssertionError("fetch_detail should not be called for saved summary_failed notes")


class ExpiredLoginCrawler:
    def check_login(self) -> bool:
        return False


class FakeSummary:
    def summarize(self, detail: ContentDetail) -> SummaryResult:
        return SummaryResult(
            atomic_cards=("卡片",),
            permanent_note="永久笔记",
            links=("关联",),
            actions=("行动",),
            questions=("问题",),
            keywords=("关键词",),
        )


class FailingSummary:
    def summarize(self, detail: ContentDetail) -> SummaryResult:
        raise SummaryError("summary api failed")


class FakeNotifier:
    def __init__(self):
        self.reports = []

    def send_run_report(self, report):
        self.reports.append(report)
        return True


if __name__ == "__main__":
    unittest.main()
