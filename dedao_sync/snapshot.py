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
        self._current_href: str | None = None
        self._text_parts: list[str] = []
        self.anchors: list[dict[str, object]] = []

    def handle_starttag(self, tag: str, attrs):
        if tag != "a":
            return
        attrs_dict = dict(attrs)
        href = attrs_dict.get("href")
        if href:
            self._current_href = urljoin(self.base_url, href)
            self._text_parts = []

    def handle_endtag(self, tag: str):
        if tag == "a" and self._current_href:
            text = " ".join(part.strip() for part in self._text_parts if part.strip())
            self.anchors.append({"href": self._current_href, "text": text})
            self._current_href = None
            self._text_parts = []

    def handle_data(self, data: str):
        if self._current_href:
            self._text_parts.append(data)


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
