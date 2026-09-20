from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from dedao_sync.archive import reconcile_archived_note_paths
from dedao_sync.markdown import content_hash
from dedao_sync.models import ContentItem
from dedao_sync.repository import SyncRepository


class ArchiveTests(unittest.TestCase):
    def test_archived_note_remains_accessible_from_database(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = SyncRepository(root / 'sync.sqlite3')
            repo.migrate()
            output = root / 'notes'
            original = output / '栏目' / '栏目-2026-09-20-标题.md'
            original.parent.mkdir(parents=True)
            original.write_text('# 标题\n\n## 全文稿\n正文\n', encoding='utf-8')
            item = ContentItem(source_url='https://example.com/1', detail_url='https://example.com/1', column_name='栏目', title='标题')
            repo.upsert_item(item, status='synced', file_path=original, has_transcript=True,
                             content_hash=content_hash('正文'), summary_status='ok')
            archived = original.parent / '2026' / original.name
            archived.parent.mkdir()
            original.rename(archived)
            self.assertEqual(reconcile_archived_note_paths(repo, output), 1)
            row = repo.find_existing(item)
            self.assertEqual(Path(row['file_path']), archived)
            self.assertTrue(Path(row['file_path']).is_file())
            self.assertEqual(row['summary_status'], 'ok')
            self.assertEqual(reconcile_archived_note_paths(repo, output), 0)

    def test_does_not_repoint_to_different_content_or_outside_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = SyncRepository(root / 'sync.sqlite3')
            repo.migrate()
            output = root / 'notes'
            for index, parent in enumerate((output / '栏目', root / 'other')):
                original = parent / '栏目-2026-09-20-标题.md'
                archived = parent / '2026' / original.name
                archived.parent.mkdir(parents=True)
                archived.write_text('\n## 全文稿\n' + ('不同正文' if index == 0 else '正文') + '\n', encoding='utf-8')
                item = ContentItem(source_url=f'https://example.com/{index}', detail_url=f'https://example.com/{index}', column_name='栏目', title='标题')
                repo.upsert_item(item, status='synced', file_path=original, has_transcript=True,
                                 content_hash=content_hash('正文'))
            self.assertEqual(reconcile_archived_note_paths(repo, output), 0)

    def test_user_edited_transcript_is_found_by_source_identity(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = SyncRepository(root / 'sync.sqlite3')
            repo.migrate()
            output = root / 'notes'
            original = output / '栏目' / '栏目-2026-09-20-标题.md'
            archived = original.parent / '2026' / original.name
            archived.parent.mkdir(parents=True)
            body = '---\nurl: "https://example.com/1"\ncolumn: "栏目"\n---\n\n## 全文稿\n编辑后的正文\n'
            archived.write_text(body, encoding='utf-8')
            item = ContentItem(source_url='https://example.com/1', detail_url='https://example.com/1', column_name='栏目', title='标题')
            repo.upsert_item(item, status='synced', file_path=original, has_transcript=True,
                             content_hash=content_hash('原文'))
            self.assertEqual(reconcile_archived_note_paths(repo, output), 1)
            self.assertEqual(archived.read_text(encoding='utf-8'), body)
