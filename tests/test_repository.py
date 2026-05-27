from __future__ import annotations

import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from dedao_sync.models import ContentItem, RunReport, STATUS_SYNCED
from dedao_sync.repository import SyncRepository


class RepositoryTests(unittest.TestCase):
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
            self.assertEqual(rows[0]["log_path"], str(Path("logs/run.log")))


if __name__ == "__main__":
    unittest.main()
