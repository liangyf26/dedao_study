from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

from dedao_sync.markdown import (
    MarkdownWriter,
    content_hash,
    extract_transcript_from_note,
    markdown_single_line,
    note_identity_hash,
    render_note,
    sanitize_filename_part,
    yaml_scalar,
)
from dedao_sync.models import (
    AppConfig,
    ColumnConfig,
    ContentDetail,
    ContentItem,
    DedaoConfig,
    FeishuConfig,
    ObsidianConfig,
    SummaryConfig,
    SummaryResult,
    TranscriptionConfig,
)


def make_config(root: Path) -> AppConfig:
    return AppConfig(
        obsidian=ObsidianConfig(root, "得到", "{column}-{published_date}-{title}.md"),
        dedao=DedaoConfig(
            root / "auth.json",
            root / "profile",
            False,
            2,
            False,
            root / "page_failures",
            (ColumnConfig("栏目", "https://example.com"),),
        ),
        summary=SummaryConfig(False, "x", "x", "BASE", "KEY"),
        transcription=TranscriptionConfig(False, "x", True, root / "media"),
        feishu=FeishuConfig(False, "WEBHOOK", "SECRET"),
        root_dir=root,
    )


class MarkdownTests(unittest.TestCase):
    def test_sanitize_filename_part(self):
        self.assertEqual(sanitize_filename_part('a<b>c:d"e/f\\g|h?i*j'), "a b c d e f g h i j")
        self.assertEqual(sanitize_filename_part("CON"), "_CON")

    def test_render_marks_machine_transcription(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = make_config(root)
            item = ContentItem("u", "得到直播", "机器转写", "u", published_at="2026-09-01")
            detail = ContentDetail(
                item=item,
                transcript_text="机器转写正文",
                has_transcript=True,
                transcribed=True,
                transcription_provider="volcengine",
            )
            body = render_note(detail, SummaryResult(atomic_cards=(), permanent_note=""))
            self.assertIn('content_type: "transcribed"', body)
            self.assertIn('transcription_provider: "volcengine"', body)
            self.assertIn("机器转写，可能存在识别错误。", body)

    def test_render_and_atomic_write(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = make_config(root)
            item = ContentItem(
                source_url="https://example.com/1",
                detail_url="https://example.com/1",
                column_name="栏目",
                title='标题: "测试"',
                published_at="2026-05-26",
            )
            detail = ContentDetail(item=item, transcript_text="标题 测试\n\n第一段\n\n第二段", has_transcript=True)
            summary = SummaryResult(
                atomic_cards=("卡片内容",),
                permanent_note="永久笔记",
                links=("关联",),
                actions=("行动",),
                questions=("问题？",),
                keywords=("关键词",),
            )
            body = render_note(detail, summary)
            self.assertIn('title: "标题: \\"测试\\""', body)
            self.assertIn("## 来源", body)
            self.assertIn("- <https://example.com/1>", body)
            path = MarkdownWriter(config).write(detail, summary)
            self.assertTrue(path.exists())
            written = path.read_text(encoding="utf-8")
            self.assertIn("## 全文稿", written)
            self.assertEqual(extract_transcript_from_note(written), "标题 测试\n\n第一段\n\n第二段")

    def test_year_subfolders_follow_article_date_and_preserve_idempotency(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = make_config(root)
            config = replace(config, obsidian=replace(config.obsidian, year_subfolders=True))
            writer = MarkdownWriter(config)
            for column, date, year in (("得到头条", "2025-12-31", "2025"),
                                       ("得到精选", "2026-09-20T10:00:00+08:00", "2026")):
                item = ContentItem(source_url="https://example.com/1", detail_url="https://example.com/1",
                                   column_name=column, title="测试", published_at=date)
                detail = ContentDetail(item=item, transcript_text="正文", has_transcript=True)
                path = writer.write(detail, SummaryResult(atomic_cards=(), permanent_note=""))
                self.assertEqual(path.parent, config.output_root / column / year)
                self.assertEqual(writer.write(detail, SummaryResult(atomic_cards=(), permanent_note="")), path)
                self.assertTrue(path.is_file())

    def test_year_subfolders_do_not_invent_date_for_invalid_or_missing_dates(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = make_config(Path(tmp))
            config = replace(config, obsidian=replace(config.obsidian, year_subfolders=True))
            writer = MarkdownWriter(config)
            for date in (None, "unknown-date", "2026-02-30"):
                item = ContentItem(source_url="https://example.com/1", detail_url="https://example.com/1",
                                   column_name="得到头条", title="测试", published_at=date)
                detail = ContentDetail(item=item, transcript_text="正文", has_transcript=True)
                self.assertEqual(writer.write(detail, SummaryResult(atomic_cards=(), permanent_note="")).parent, config.output_root / "得到头条")

    def test_frontmatter_escapes_yaml_special_characters(self):
        self.assertEqual(
            yaml_scalar('标题: "测试"\n第二行\t尾部\r回车\\路径\x07'),
            '"标题: \\"测试\\"\\n第二行\\t尾部\\r回车\\\\路径\\x07"',
        )
        item = ContentItem(
            source_url="https://example.com/1",
            detail_url="https://example.com/1",
            column_name="栏目\n带换行",
            title='标题: "测试"\n第二行',
            published_at="2026-05-26",
        )
        detail = ContentDetail(item=item, transcript_text="标题 测试\n\n第一段\n\n第二段", has_transcript=True)
        body = render_note(detail, SummaryResult(atomic_cards=(), permanent_note=""))
        frontmatter = body.split("---", 2)[1]

        self.assertIn('title: "标题: \\"测试\\"\\n第二行"', frontmatter)
        self.assertIn('column: "栏目\\n带换行"', frontmatter)
        self.assertNotIn("title: \"标题: \\\"测试\\\"\n第二行", frontmatter)

    def test_default_sync_time_uses_asia_shanghai_offset(self):
        item = ContentItem(
            source_url="https://example.com/1",
            detail_url="https://example.com/1",
            column_name="栏目",
            title="标题",
            published_at="2026-05-26",
        )
        detail = ContentDetail(item=item, transcript_text="标题\n\n第一段\n\n第二段", has_transcript=True)

        body = render_note(detail, SummaryResult(atomic_cards=(), permanent_note=""))

        frontmatter = body.split("---", 2)[1]
        self.assertIn("+08:00", frontmatter)
        self.assertRegex(frontmatter, r'sync_time: "\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\+08:00"')

    def test_short_markdown_fields_cannot_break_note_structure(self):
        self.assertEqual(markdown_single_line("标题\n\t第二行\x07"), "标题 第二行")
        item = ContentItem(
            source_url="https://example.com/1\n## injected",
            detail_url="https://example.com/1\n## injected",
            column_name="栏目",
            title="标题\n## injected",
            published_at="2026-05-26",
        )
        detail = ContentDetail(item=item, transcript_text="第一段\n\n第二段", has_transcript=True)
        summary = SummaryResult(
            atomic_cards=("卡片",),
            permanent_note="永久笔记",
            links=("主题\n## injected",),
            actions=("行动\n- injected",),
            questions=("问题\n## injected",),
            keywords=("关键\n词",),
        )

        body = render_note(detail, summary)

        self.assertIn("# 标题 ## injected", body)
        self.assertIn("- 主题 ## injected", body)
        self.assertIn("- 可延伸问题：问题 ## injected", body)
        self.assertIn("- 行动 - injected", body)
        self.assertIn("关键 词", body)
        self.assertIn("- <https://example.com/1 ## injected>", body)
        before_transcript = body.split("## 全文稿", 1)[0]
        self.assertNotIn("\n## injected", before_transcript)

    def test_write_does_not_overwrite_existing_hash_collision_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = make_config(root)
            item = ContentItem(
                source_url="https://example.com/1",
                detail_url="https://example.com/1",
                column_name="栏目",
                title="同名标题",
                published_at="2026-05-26",
            )
            detail = ContentDetail(item=item, transcript_text="正文 A", has_transcript=True)
            summary = SummaryResult(atomic_cards=("卡片 A",), permanent_note="永久笔记 A")
            writer = MarkdownWriter(config)
            body = render_note(detail, summary)
            digest = note_identity_hash(body)[:8]
            column_dir = root / "得到" / "栏目"
            column_dir.mkdir(parents=True)
            original = column_dir / "栏目-2026-05-26-同名标题.md"
            original.write_text("existing original", encoding="utf-8")
            hash_collision = column_dir / f"栏目-2026-05-26-同名标题-{digest}.md"
            hash_collision.write_text("different existing hash file", encoding="utf-8")

            path = writer.write(detail, summary)

            self.assertEqual(path.name, f"栏目-2026-05-26-同名标题-{digest}-2.md")
            self.assertEqual(original.read_text(encoding="utf-8"), "existing original")
            self.assertEqual(hash_collision.read_text(encoding="utf-8"), "different existing hash file")
            self.assertIn("卡片 A", path.read_text(encoding="utf-8"))

    def test_write_reuses_existing_identical_target_after_db_interruption(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = make_config(root)
            item = ContentItem(
                source_url="https://example.com/1",
                detail_url="https://example.com/1",
                column_name="栏目",
                title="同名标题",
                published_at="2026-05-26",
            )
            detail = ContentDetail(item=item, transcript_text="正文 A", has_transcript=True)
            summary = SummaryResult(atomic_cards=("卡片 A",), permanent_note="永久笔记 A")
            writer = MarkdownWriter(config)

            first_path = writer.write(detail, summary)
            second_path = writer.write(detail, summary)

            self.assertEqual(second_path, first_path)
            self.assertEqual(list(first_path.parent.glob("*.md")), [first_path])

    def test_write_reuses_existing_target_when_only_sync_time_differs(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = make_config(root)
            item = ContentItem(
                source_url="https://example.com/1",
                detail_url="https://example.com/1",
                column_name="栏目",
                title="同名标题",
                published_at="2026-05-26",
            )
            detail = ContentDetail(item=item, transcript_text="正文 A", has_transcript=True)
            summary = SummaryResult(atomic_cards=("卡片 A",), permanent_note="永久笔记 A")
            writer = MarkdownWriter(config)
            first_body = render_note(
                detail,
                summary,
                sync_time=datetime(2026, 5, 26, 8, 0, 0, tzinfo=timezone(timedelta(hours=8))),
            )
            second_body = render_note(
                detail,
                summary,
                sync_time=datetime(2026, 5, 27, 8, 0, 0, tzinfo=timezone(timedelta(hours=8))),
            )
            self.assertNotEqual(content_hash(first_body), content_hash(second_body))
            self.assertEqual(note_identity_hash(first_body), note_identity_hash(second_body))
            first_path = writer.build_path(detail, first_body)
            writer._atomic_write(first_path, first_body)

            second_path = writer.build_path(detail, second_body)

            self.assertEqual(second_path, first_path)
            self.assertEqual(list(first_path.parent.glob("*.md")), [first_path])

    def test_content_hash_is_stable(self):
        self.assertEqual(content_hash("abc"), content_hash("abc"))
        self.assertNotEqual(content_hash("abc"), content_hash("abd"))


if __name__ == "__main__":
    unittest.main()
