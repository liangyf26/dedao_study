from __future__ import annotations

import hashlib
import os
import re
import tempfile
from datetime import datetime
from pathlib import Path

from .models import AppConfig, ContentDetail, SummaryResult
from .time_utils import now_local


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


def note_identity_hash(markdown: str) -> str:
    normalized = re.sub(r'(?m)^sync_time: ".*"$', 'sync_time: "<ignored>"', markdown)
    return content_hash(normalized)


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
    replacements = {
        "\\": "\\\\",
        '"': '\\"',
        "\b": "\\b",
        "\f": "\\f",
        "\n": "\\n",
        "\r": "\\r",
        "\t": "\\t",
    }
    escaped_parts: list[str] = []
    for char in text:
        if char in replacements:
            escaped_parts.append(replacements[char])
        elif ord(char) < 0x20:
            escaped_parts.append(f"\\x{ord(char):02x}")
        else:
            escaped_parts.append(char)
    escaped = "".join(escaped_parts)
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


def markdown_single_line(value: object) -> str:
    text = "" if value is None else str(value)
    text = re.sub(r"[\x00-\x1f]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def render_list_items(values: tuple[str, ...]) -> str:
    lines = [markdown_single_line(value) for value in values]
    lines = [line for line in lines if line]
    return "\n".join(f"- {line}" for line in lines) or "- "


def render_note(detail: ContentDetail, summary: SummaryResult, *, sync_time: datetime | None = None) -> str:
    item = detail.item
    sync_time = sync_time or now_local()
    source_url = item.detail_url or item.source_url
    title = markdown_single_line(item.title) or "untitled"
    frontmatter = render_frontmatter(
        {
            "source": "dedao",
            "column": item.column_name,
            "title": item.title,
            "author": item.author or "",
            "published": item.published_at or "",
            "url": source_url,
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

    related_items = tuple(summary.links) + tuple(f"可延伸问题：{question}" for question in summary.questions)
    links = render_list_items(related_items)
    actions = render_list_items(summary.actions)
    questions = render_list_items(summary.questions)
    keywords = "，".join(markdown_single_line(keyword) for keyword in summary.keywords if markdown_single_line(keyword))
    source_line = markdown_single_line(source_url)
    source = f"- <{source_line}>" if source_line else "- "

    transcript = detail.transcript_text.strip()
    if not detail.has_transcript:
        transcript = "> 未找到网页文字稿，等待后续转录。\n"

    return "\n".join(
        [
            frontmatter,
            "",
            f"# {title}",
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
            "## 来源",
            "",
            source,
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
            target = self._collision_safe_path(target, body)
        if len(str(target)) > 240:
            digest = content_hash(body)[:8]
            short_title = sanitize_filename_part(item.title, max_length=40)
            target = column_dir / f"{values['column']}-{values['published_date']}-{short_title}-{digest}.md"
            if target.exists():
                target = self._collision_safe_path(target, body)
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
    def _collision_safe_path(target: Path, body: str) -> Path:
        body_hash = note_identity_hash(body)
        try:
            if note_identity_hash(target.read_text(encoding="utf-8")) == body_hash:
                return target
        except OSError:
            pass
        digest = body_hash[:8]
        candidate = target.with_name(f"{target.stem}-{digest}{target.suffix}")
        counter = 2
        while candidate.exists():
            try:
                if note_identity_hash(candidate.read_text(encoding="utf-8")) == body_hash:
                    return candidate
            except OSError:
                pass
            candidate = target.with_name(f"{target.stem}-{digest}-{counter}{target.suffix}")
            counter += 1
        return candidate

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
