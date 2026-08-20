"""将 vault 中已有的笔记注册进 SQLite 状态库。

用于环境迁移：Windows 时期同步的笔记在 vault 里、但去重库是新的，
全量 sync 会把旧内容当作"新"条目重抓。本脚本解析笔记 frontmatter
（url/column/title/published），按爬虫相同的方式构造 ContentItem
（canonical_url / dedao_id），以 synced 状态 upsert 进状态库，
使后续 sync 直接跳过这些条目。

用法：
    python scripts/import_existing_notes.py --config config.yaml --dry-run
    python scripts/import_existing_notes.py --config config.yaml
"""

from __future__ import annotations

import argparse
import re
from pathlib import Path

from dedao_sync.config import load_config
from dedao_sync.crawler import DedaoCrawler
from dedao_sync.markdown import extract_transcript_from_note
from dedao_sync.models import STATUS_SYNCED, ContentItem
from dedao_sync.repository import SyncRepository
from dedao_sync.sync import default_db_path

FRONTMATTER_KEY_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*):\s*(.*)$")
DATE_RE = re.compile(r"(\d{4}-\d{2}-\d{2})")


def parse_frontmatter(text: str) -> dict[str, str]:
    if not text.startswith("---"):
        return {}
    end = text.find("\n---", 3)
    if end < 0:
        return {}
    fields: dict[str, str] = {}
    for line in text[3:end].splitlines():
        match = FRONTMATTER_KEY_RE.match(line)
        if match:
            fields[match.group(1)] = match.group(2).strip().strip('"').strip("'")
    return fields


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--dry-run", action="store_true", help="只统计，不写库")
    args = parser.parse_args()

    config = load_config(args.config)
    repo = SyncRepository(default_db_path(config.root_dir))
    repo.migrate()

    columns = {column.name: column for column in config.dedao.columns if column.enabled}
    root = config.output_root

    imported = 0
    updated = 0
    missing_meta = 0
    no_transcript = 0
    no_summary = 0
    for name, column in columns.items():
        column_dir = root / name
        if not column_dir.is_dir():
            print(f"[skip] vault 下没有该栏目目录：{column_dir}")
            continue
        notes = sorted(column_dir.glob("*.md"))
        print(f"[scan] {name}: {len(notes)} 篇")
        for note in notes:
            text = note.read_text(encoding="utf-8")
            fields = parse_frontmatter(text)
            url = fields.get("url", "")
            title = fields.get("title", "")
            note_column = fields.get("column", "")
            if not url or not title:
                print(f"[warn] frontmatter 缺 url/title，跳过：{note.name}")
                missing_meta += 1
                continue
            if note_column and note_column != name:
                print(f"[warn] frontmatter column({note_column}) 与目录({name}) 不一致：{note.name}")
            detail_url = DedaoCrawler._normalize_url(url, column.url)
            dedao_id = DedaoCrawler._extract_id(detail_url)
            date_match = DATE_RE.search(fields.get("published", ""))
            published_at = date_match.group(1) if date_match else None
            transcript = extract_transcript_from_note(text)
            has_transcript = bool(transcript.strip())
            has_summary = bool(re.search(r"### 卡片 1", text))
            if not has_transcript:
                no_transcript += 1
            if not has_summary:
                no_summary += 1
            item = ContentItem(
                source_url=detail_url,
                column_name=name,
                title=title[:120],
                detail_url=detail_url,
                dedao_id=dedao_id,
                published_at=published_at,
                content_type="web",
            )
            if args.dry_run:
                imported += 1
                continue
            existing = repo.find_existing(item, None)
            row_id = repo.upsert_item(
                item,
                status=STATUS_SYNCED,
                content_hash=None,
                file_path=note,
                has_transcript=has_transcript,
                summary_status="success" if has_summary else None,
            )
            if existing:
                updated += 1
            else:
                imported += 1
            if imported % 50 == 0:
                print(f"  ... 已处理 {imported} 篇")

    mode = "dry-run" if args.dry_run else "导入"
    print(
        f"[done] {mode}完成：新增 {imported}，更新 {updated}，"
        f"缺元数据 {missing_meta}，无全文稿 {no_transcript}，无摘要 {no_summary}"
    )
    if not args.dry_run:
        print(f"状态库：{repo.db_path}")


if __name__ == "__main__":
    main()
