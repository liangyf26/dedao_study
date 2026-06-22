from __future__ import annotations

import tempfile
import unittest
import sqlite3
from datetime import datetime
from pathlib import Path

from dedao_sync.models import ContentItem, RunReport, STATUS_SUMMARY_FAILED, STATUS_SYNCED
from dedao_sync.repository import SyncRepository


class RepositoryTests(unittest.TestCase):
    def test_migrate_upgrades_legacy_items_schema(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "sync.sqlite3"
            conn = sqlite3.connect(db_path)
            try:
                conn.execute(
                    """
                    CREATE TABLE items (
                      id INTEGER PRIMARY KEY AUTOINCREMENT,
                      source_url TEXT NOT NULL,
                      dedao_id TEXT,
                      column_name TEXT NOT NULL,
                      title TEXT NOT NULL,
                      published_at TEXT,
                      synced_at TEXT,
                      content_hash TEXT,
                      status TEXT NOT NULL,
                      file_path TEXT,
                      has_transcript INTEGER NOT NULL DEFAULT 0,
                      transcribed INTEGER NOT NULL DEFAULT 0,
                      summary_status TEXT,
                      error_message TEXT,
                      created_at TEXT NOT NULL,
                      updated_at TEXT NOT NULL
                    )
                    """
                )
                conn.execute(
                    """
                    INSERT INTO items (
                        source_url, dedao_id, column_name, title, status,
                        has_transcript, transcribed, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, 0, 0, ?, ?)
                    """,
                    ("https://example.com/legacy", "legacy", "栏目", "旧标题", "synced", "", ""),
                )
                conn.execute(
                    """
                    CREATE TABLE runs (
                      id INTEGER PRIMARY KEY AUTOINCREMENT,
                      started_at TEXT NOT NULL,
                      status TEXT NOT NULL
                    )
                    """
                )
                conn.execute("CREATE TABLE run_items (run_id INTEGER NOT NULL, item_id INTEGER NOT NULL, action TEXT NOT NULL, status TEXT NOT NULL)")
                conn.commit()
            finally:
                conn.close()

            repo = SyncRepository(db_path)
            repo.migrate()

            rows = repo.list_items()
            self.assertEqual(rows[0]["canonical_url"], "https://example.com/legacy")
            self.assertTrue(rows[0]["created_at"])
            self.assertTrue(rows[0]["updated_at"])
            with repo.connect() as conn:
                run_columns = {row["name"] for row in conn.execute("PRAGMA table_info(runs)").fetchall()}
            self.assertIn("request_count", run_columns)
            item = ContentItem(
                source_url="https://example.com/other",
                detail_url="https://example.com/legacy",
                dedao_id="other",
                column_name="栏目",
                title="新标题",
            )
            self.assertEqual(repo.find_existing(item)["source_url"], "https://example.com/legacy")

    def test_migrate_and_upsert_are_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = SyncRepository(Path(tmp) / "sync.sqlite3")
            repo.migrate()
            item = ContentItem(
                source_url="https://example.com/a",
                detail_url="https://example.com/a",
                dedao_id="a",
                column_name="栏目",
                title="标题",
            )
            first = repo.upsert_item(item, status=STATUS_SYNCED, content_hash="hash", file_path="note.md", has_transcript=True)
            second = repo.upsert_item(item, status=STATUS_SYNCED, content_hash="hash", file_path="note.md", has_transcript=True)
            self.assertEqual(first, second)
            rows = repo.list_items()
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["status"], STATUS_SYNCED)

    def test_dedao_id_match_is_scoped_to_column(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = SyncRepository(Path(tmp) / "sync.sqlite3")
            repo.migrate()
            first = ContentItem(
                source_url="https://www.dedao.cn/course/detail?id=same",
                detail_url="https://www.dedao.cn/course/detail?id=same",
                dedao_id="same",
                column_name="栏目一",
                title="标题一",
            )
            second = ContentItem(
                source_url="https://aiquan.dedao.cn/item?id=same",
                detail_url="https://aiquan.dedao.cn/item?id=same",
                dedao_id="same",
                column_name="栏目二",
                title="标题二",
            )
            repo.upsert_item(first, status=STATUS_SYNCED, content_hash="hash-1", file_path="note-1.md", has_transcript=True)

            self.assertIsNone(repo.find_existing(second))

            second_id = repo.upsert_item(second, status=STATUS_SYNCED, content_hash="hash-2", file_path="note-2.md", has_transcript=True)
            self.assertGreater(second_id, 0)
            self.assertEqual(len(repo.list_items()), 2)

    def test_runs(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = SyncRepository(Path(tmp) / "sync.sqlite3")
            repo.migrate()
            report = RunReport(started_at=datetime.now(), total_columns=4, log_path=Path("logs/run.log"))
            run_id = repo.start_run(report)
            report.status = "success"
            report.finished_at = datetime.now()
            report.discovered_count = 3
            report.new_count = 2
            report.success_count = 2
            report.request_count = 6
            repo.finish_run(run_id, report)
            self.assertGreater(run_id, 0)
            rows = repo.list_runs()
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["id"], run_id)
            self.assertEqual(rows[0]["status"], "success")
            self.assertEqual(rows[0]["total_columns"], 4)
            self.assertEqual(rows[0]["discovered_count"], 3)
            self.assertEqual(rows[0]["new_count"], 2)
            self.assertEqual(rows[0]["success_count"], 2)
            self.assertEqual(rows[0]["request_count"], 6)
            self.assertEqual(rows[0]["log_path"], str(Path("logs/run.log")))

    def test_repository_redacts_sensitive_error_fields(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = SyncRepository(Path(tmp) / "sync.sqlite3")
            repo.migrate()
            item = ContentItem(
                source_url="https://example.com/a",
                detail_url="https://example.com/a",
                dedao_id="a",
                column_name="栏目",
                title="标题",
            )
            item_id = repo.upsert_item(
                item,
                status="failed",
                error_message="Authorization: Bearer abc.def and Cookie: sessionid=secret-cookie",
            )
            report = RunReport(started_at=datetime.now())
            run_id = repo.start_run(report)
            repo.add_run_item(run_id, item_id, "sync", "failed", "api_key=sk-test")
            report.status = "partial_failed"
            report.finished_at = datetime.now()
            repo.finish_run(run_id, report, "secret=run-secret")

            item_rows = repo.list_items()
            run_rows = repo.list_runs()
            run_item_rows = repo.list_run_items(run_id)
            stored = "\n".join(
                [
                    item_rows[0]["error_message"],
                    run_rows[0]["error_message"],
                    run_item_rows[0]["message"],
                ]
            )
            self.assertIn("[REDACTED]", stored)
            self.assertNotIn("abc.def", stored)
            self.assertNotIn("secret-cookie", stored)
            self.assertNotIn("sk-test", stored)
            self.assertNotIn("run-secret", stored)

    def test_summary_failed_with_saved_transcript_is_stored_as_synced(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = SyncRepository(Path(tmp) / "sync.sqlite3")
            repo.migrate()
            item = ContentItem(
                source_url="https://example.com/summary-failed",
                detail_url="https://example.com/summary-failed",
                dedao_id="summary-failed",
                column_name="栏目",
                title="标题",
            )

            repo.upsert_item(
                item,
                status=STATUS_SUMMARY_FAILED,
                content_hash="hash-summary-failed",
                file_path="note.md",
                has_transcript=True,
                summary_status=STATUS_SUMMARY_FAILED,
            )

            rows = repo.list_items()
            self.assertEqual(rows[0]["status"], STATUS_SYNCED)
            self.assertEqual(rows[0]["summary_status"], STATUS_SUMMARY_FAILED)
            self.assertEqual(rows[0]["has_transcript"], 1)
            self.assertIsNotNone(rows[0]["synced_at"])

    def test_migrate_normalizes_saved_summary_failed_items_as_synced(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "sync.sqlite3"
            repo = SyncRepository(db_path)
            repo.migrate()
            item = ContentItem(
                source_url="https://example.com/legacy-summary-failed",
                detail_url="https://example.com/legacy-summary-failed",
                dedao_id="legacy-summary-failed",
                column_name="栏目",
                title="标题",
            )
            with repo.connect() as conn:
                conn.execute(
                    """
                    INSERT INTO items (
                        source_url, dedao_id, canonical_url, column_name, title,
                        synced_at, status, file_path, has_transcript,
                        summary_status, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?)
                    """,
                    (
                        item.source_url,
                        item.dedao_id,
                        item.detail_url,
                        item.column_name,
                        item.title,
                        "2026-01-01T00:00:00+00:00",
                        STATUS_SUMMARY_FAILED,
                        "note.md",
                        STATUS_SUMMARY_FAILED,
                        "2026-01-01T00:00:00+00:00",
                        "2026-01-01T00:00:00+00:00",
                    ),
                )

            repo.migrate()

            rows = repo.list_items()
            self.assertEqual(rows[0]["status"], STATUS_SYNCED)
            self.assertEqual(rows[0]["summary_status"], STATUS_SUMMARY_FAILED)

    def test_summary_failed_without_saved_transcript_does_not_set_synced_at(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = SyncRepository(Path(tmp) / "sync.sqlite3")
            repo.migrate()
            item = ContentItem(
                source_url="https://example.com/resummary-failed",
                detail_url="https://example.com/resummary-failed",
                dedao_id="resummary-failed",
                column_name="栏目",
                title="标题",
            )

            repo.upsert_item(item, status=STATUS_SUMMARY_FAILED, summary_status=STATUS_SUMMARY_FAILED)

            rows = repo.list_items()
            self.assertEqual(rows[0]["status"], STATUS_SUMMARY_FAILED)
            self.assertIsNone(rows[0]["synced_at"])


if __name__ == "__main__":
    unittest.main()
