from __future__ import annotations

import hashlib
import os
import re
import tempfile
from datetime import datetime
from pathlib import Path

from .models import AppConfig, ContentDetail, SummaryResult


WINDOWS_RESERVED_NAMES = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}


def content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def sanitize_filename_part(value: str, *, max_length: int = 80) -> str:
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]', " ", value)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" .")
    if not cleaned:
        cleaned = "untitled"
    if cleaned.upper() in WINDOWS_RESERVED_NAMES:
        cleaned = f"_{cleaned}"
    if len(cleaned) > max_length:
        cleaned = cleaned[:max_length].rstrip(" .")
    return cleaned


def yaml_scalar(value: object) -> str:
    text = "" if value is None else str(value)
    escaped = text.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
    return f'"{escaped}"'


def render_frontmatter(fields: dict[str, object], tags: list[str]) -> str:
    lines = ["---"]
    for key, value in fields.items():
        lines.append(f"{key}: {yaml_scalar(value)}")
    lines.append("tags:")
    for tag in tags:
        lines.append(f"  - {yaml_scalar(tag)}")
    lines.append("---")
    return "\n".join(lines)


def render_note(detail: ContentDetail, summary: SummaryResult, *, sync_time: datetime | None = None) -> str:
    item = detail.item
    sync_time = sync_time or datetime.now()
    frontmatter = render_frontmatter(
        {
            "source": "dedao",
            "column": item.column_name,
            "title": item.title,
            "author": item.author or "",
            "published": item.published_at or "",
            "url": item.detail_url or item.source_url,
            "content_type": "transcript" if detail.has_transcript else "missing_transcript",
            "summary_style": "zettelkasten",
            "sync_time": sync_time.isoformat(timespec="seconds"),
        },
        ["得到", item.column_name],
    )

    card_lines = []
    if summary.atomic_cards:
        for index, card in enumerate(summary.atomic_cards, start=1):
            card_lines.append(f"### 卡片 {index}")
            card_lines.append("")
            card_lines.append(card.strip())
            card_lines.append("")
    else:
        card_lines.append("> 摘要尚未生成。")
        card_lines.append("")

    links = "\n".join(f"- {line}" for line in summary.links) or "- "
    actions = "\n".join(f"- {line}" for line in summary.actions) or "- "
    questions = "\n".join(f"- {line}" for line in summary.questions) or "- "
    keywords = "，".join(summary.keywords)

    transcript = detail.transcript_text.strip()
    if not detail.has_transcript:
        transcript = "> 未找到网页文字稿，等待后续转录。\n"

    return "\n".join(
        [
            frontmatter,
            "",
            f"# {item.title}",
            "",
            "## 原子卡片",
            "",
            "\n".join(card_lines).rstrip(),
            "",
            "## 永久笔记",
            "",
            summary.permanent_note.strip() or "> 摘要尚未生成。",
            "",
            "## 关联",
            "",
            links,
            "",
            "## 行动/观察",
            "",
            actions,
            "",
            "## 复习问题",
            "",
            questions,
            "",
            "## 关键词",
            "",
            keywords,
            "",
            "## 全文稿",
            "",
            transcript,
            "",
        ]
    )


class MarkdownWriter:
    def __init__(self, config: AppConfig):
        self.config = config

    def build_path(self, detail: ContentDetail, body: str) -> Path:
        item = detail.item
        published = item.published_at or "unknown-date"
        published_date = published[:10] if len(published) >= 10 else published
        values = {
            "column": sanitize_filename_part(item.column_name),
            "published_date": sanitize_filename_part(published_date, max_length=20),
            "title": sanitize_filename_part(item.title),
        }
        filename = self.config.obsidian.filename_pattern.format(**values)
        if not filename.endswith(".md"):
            filename = f"{filename}.md"
        column_dir = self.config.output_root / sanitize_filename_part(item.column_name)
        target = column_dir / filename
        if target.exists():
            digest = content_hash(body)[:8]
            target = column_dir / f"{target.stem}-{digest}.md"
        if len(str(target)) > 240:
            digest = content_hash(body)[:8]
            short_title = sanitize_filename_part(item.title, max_length=40)
            target = column_dir / f"{values['column']}-{values['published_date']}-{short_title}-{digest}.md"
        return target

    def write(self, detail: ContentDetail, summary: SummaryResult) -> Path:
        body = render_note(detail, summary)
        target = self.build_path(detail, body)
        self._atomic_write(target, body)
        return target

    def overwrite(self, target: str | Path, detail: ContentDetail, summary: SummaryResult) -> Path:
        target = Path(target)
        body = render_note(detail, summary)
        self._atomic_write(target, body)
        return target

    @staticmethod
    def _atomic_write(target: Path, body: str) -> None:
        target.parent.mkdir(parents=True, exist_ok=True)
        fd, temp_name = tempfile.mkstemp(prefix=f".{target.stem}.", suffix=".tmp", dir=str(target.parent), text=True)
        temp_path = Path(temp_name)
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
                handle.write(body)
                handle.flush()
            os.replace(temp_path, target)
        finally:
            if temp_path.exists():
                temp_path.unlink()


def extract_transcript_from_note(markdown: str) -> str:
    marker = "\n## 全文稿\n"
    if marker not in markdown:
        return ""
    return markdown.split(marker, 1)[1].strip()
