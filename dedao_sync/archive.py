"""Keep database paths usable after the scheduled year-directory archive step."""
from __future__ import annotations

import re
from pathlib import Path

from .markdown import content_hash, extract_transcript_from_note, yaml_scalar
from .repository import SyncRepository


def reconcile_archived_note_paths(repo: SyncRepository, output_root: Path) -> int:
    """Repoint missing flat notes after checking content or source identity."""
    output_root = output_root.resolve()
    updated = 0
    with repo.connect() as conn:
        rows = conn.execute(
            "SELECT id, file_path, content_hash, source_url, canonical_url, column_name FROM items WHERE file_path IS NOT NULL"
        ).fetchall()
        for row in rows:
            original = Path(row['file_path']).resolve()
            if original.exists() or not original.is_relative_to(output_root):
                continue
            # The scheduler archives only notes directly inside a column folder.
            if len(original.relative_to(output_root).parts) != 2:
                continue
            match = re.search(r'-(20\d{2})-\d{2}-\d{2}-', original.name)
            if not match:
                continue
            archived = original.parent / match[1] / original.name
            if not archived.is_file() or not archived.resolve().is_relative_to(output_root):
                continue
            body = archived.read_text(encoding='utf-8')
            transcript = extract_transcript_from_note(body)
            same_content = bool(transcript and content_hash(transcript) == row['content_hash'])
            metadata_lines = []
            if body.startswith('---\n') and '\n---\n' in body[4:]:
                metadata_lines = body[4:].split('\n---\n', 1)[0].splitlines()
            # Compare the exact frontmatter format emitted by MarkdownWriter.
            # User edits may change the transcript hash while source identity is stable.
            same_source = (
                f"column: {yaml_scalar(row['column_name'])}" in metadata_lines
                and any(url and f"url: {yaml_scalar(url)}" in metadata_lines
                        for url in (row['source_url'], row['canonical_url']))
            )
            if not same_content and not same_source:
                continue
            conn.execute('UPDATE items SET file_path = ? WHERE id = ?', (str(archived), row['id']))
            updated += 1
    return updated
