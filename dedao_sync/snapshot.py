from __future__ import annotations

from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urljoin

from .crawler import DedaoCrawler
from .extractor import CandidateDiagnostics, TranscriptExtractor, html_to_candidate_texts
from .models import ColumnConfig, ContentDetail, ContentItem


class AnchorParser(HTMLParser):
    def __init__(self, base_url: str):
        super().__init__()
        self.base_url = base_url
        self._current_anchor: dict[str, object] | None = None
        self._stack: list[dict[str, object]] = []
        self.anchors: list[dict[str, object]] = []

    def handle_starttag(self, tag: str, attrs):
        attrs_dict = dict(attrs)
        self._stack.append({"tag": tag, "attrs": attrs_dict, "text_parts": []})
        href = self._candidate_href(tag, attrs_dict)
        if not href:
            return
        self._current_anchor = {
            "href": urljoin(self.base_url, href),
            "title": (attrs_dict.get("title") or "").strip(),
            "aria_label": (attrs_dict.get("aria-label") or "").strip(),
            "data_title": (attrs_dict.get("data-title") or "").strip(),
            "start_index": len(self._stack) - 1,
        }

    def handle_endtag(self, tag: str):
        if self._current_anchor and self._stack:
            start_index = int(self._current_anchor.get("start_index", len(self._stack) - 1))
            if start_index != len(self._stack) - 1:
                if self._stack:
                    self._stack.pop()
                return
            elif tag != str(self._stack[-1].get("tag") or ""):
                if self._stack:
                    self._stack.pop()
                return
            text = self._entry_text(self._stack[start_index]) if start_index < len(self._stack) else ""
            card_text = self._nearest_card_text(start_index)
            self.anchors.append(
                {
                    "href": self._current_anchor["href"],
                    "text": text,
                    "title": self._current_anchor.get("title", ""),
                    "aria_label": self._current_anchor.get("aria_label", ""),
                    "data_title": self._current_anchor.get("data_title", ""),
                    "card_text": card_text,
                }
            )
            self._current_anchor = None
        if self._stack:
            self._stack.pop()

    def handle_data(self, data: str):
        if not data.strip():
            return
        for entry in self._stack:
            text_parts = entry["text_parts"]
            if isinstance(text_parts, list):
                text_parts.append(data)

    @staticmethod
    def _entry_text(entry: dict[str, object]) -> str:
        parts = entry.get("text_parts")
        if not isinstance(parts, list):
            return ""
        return " ".join(str(part).strip() for part in parts if str(part).strip())

    def _nearest_card_text(self, start_index: int) -> str:
        for entry in reversed(self._stack[:start_index]):
            tag = str(entry.get("tag") or "")
            attrs = entry.get("attrs")
            attrs_dict = attrs if isinstance(attrs, dict) else {}
            class_name = str(attrs_dict.get("class") or "")
            if tag in {"article", "li"} or any(token in class_name.lower() for token in ("card", "item", "course")):
                return self._entry_text(entry)
        return ""

    @staticmethod
    def _candidate_href(tag: str, attrs: dict[str, str | None]) -> str:
        if tag == "a" and attrs.get("href"):
            return str(attrs.get("href") or "")
        if attrs.get("role") != "link" and not any(
            key in attrs for key in ("data-url", "data-href", "data-link", "data-jump-url")
        ):
            return ""
        for key in ("data-url", "data-href", "data-link", "data-jump-url", "href"):
            value = attrs.get(key)
            if value:
                return str(value)
        return ""


@dataclass(frozen=True)
class SnapshotParseResult:
    detail: ContentDetail
    candidate_count: int
    transcript_candidates: tuple[CandidateDiagnostics, ...]
    item_candidates: tuple[ContentItem, ...]
    transcript_path: Path | None = None


def parse_snapshot(
    html_path: str | Path,
    *,
    title: str,
    column_name: str,
    source_url: str,
    write_transcript: bool = False,
) -> SnapshotParseResult:
    html_path = Path(html_path)
    html = html_path.read_text(encoding="utf-8")
    item = ContentItem(
        source_url=source_url,
        detail_url=source_url,
        dedao_id=DedaoCrawler._extract_id(source_url),
        column_name=column_name,
        title=title,
        content_type="snapshot",
    )
    extractor = TranscriptExtractor()
    detail = extractor.from_html(item, html)
    transcript_candidates = tuple(extractor.diagnose_candidates(item, html_to_candidate_texts(html)))

    parser = AnchorParser(source_url)
    parser.feed(html)
    column = ColumnConfig(column_name, source_url)
    item_candidates = tuple(DedaoCrawler.items_from_anchors(column, parser.anchors))

    transcript_path = None
    if write_transcript:
        transcript_path = html_path.with_suffix(".transcript.txt")
        transcript_path.write_text(detail.transcript_text, encoding="utf-8")
    return SnapshotParseResult(
        detail=detail,
        candidate_count=len(item_candidates),
        transcript_candidates=transcript_candidates,
        item_candidates=item_candidates,
        transcript_path=transcript_path,
    )
