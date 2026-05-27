from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from html.parser import HTMLParser

from .models import ContentDetail, ContentItem


UI_NOISE_PATTERNS = (
    "登录",
    "扫码",
    "下载App",
    "相关推荐",
    "分享",
    "收藏",
    "评论",
    "购买",
    "加入学习",
)

CONTAINER_TAGS = {"article", "main", "section"}

TITLE_META_KEYS = {"og:title", "twitter:title", "title", "headline"}
AUTHOR_META_KEYS = {"author", "article:author", "og:article:author"}
PUBLISHED_META_KEYS = {
    "article:published_time",
    "article:modified_time",
    "date",
    "datepublished",
    "publishdate",
    "pubdate",
    "publish_time",
    "published_time",
}


def _clean_visible_text(text: str) -> str:
    text = re.sub(r"[ \t\r\f\v]+", " ", text)
    text = re.sub(r" *\n *", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _clean_title(title: str) -> str:
    title = re.sub(r"\s+", " ", title).strip()
    title = re.split(r"\s*[-_|]\s*得到(?:App|网页版)?", title, maxsplit=1)[0].strip()
    return title


class VisibleTextParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self._skip_depth = 0
        self.parts: list[str] = []

    def handle_starttag(self, tag: str, attrs):
        if tag in {"script", "style", "noscript", "svg", "canvas"}:
            self._skip_depth += 1
        if tag in {"p", "div", "section", "article", "br", "li", "h1", "h2", "h3"}:
            self.parts.append("\n")

    def handle_endtag(self, tag: str):
        if tag in {"script", "style", "noscript", "svg", "canvas"} and self._skip_depth:
            self._skip_depth -= 1
        if tag in {"p", "div", "section", "article", "li"}:
            self.parts.append("\n")

    def handle_data(self, data: str):
        if self._skip_depth:
            return
        text = data.strip()
        if text:
            self.parts.append(text)

    def text(self) -> str:
        text = " ".join(self.parts)
        return _clean_visible_text(text)


def html_to_visible_text(html: str) -> str:
    parser = VisibleTextParser()
    parser.feed(html)
    return parser.text()


class TextBlockParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self._skip_depth = 0
        self._capture_stack: list[tuple[str, list[str]]] = []
        self.blocks: list[str] = []

    def handle_starttag(self, tag: str, attrs):
        if tag in {"script", "style", "noscript", "svg", "canvas"}:
            self._skip_depth += 1
            return
        if tag in CONTAINER_TAGS:
            self._capture_stack.append((tag, []))
        if tag in {"p", "div", "br", "li", "h1", "h2", "h3"}:
            self._append("\n")

    def handle_endtag(self, tag: str):
        if tag in {"script", "style", "noscript", "svg", "canvas"} and self._skip_depth:
            self._skip_depth -= 1
            return
        if tag in {"p", "div", "li"}:
            self._append("\n")
        if self._capture_stack and self._capture_stack[-1][0] == tag:
            _, parts = self._capture_stack.pop()
            text = _clean_visible_text(" ".join(parts))
            if text:
                self.blocks.append(text)

    def handle_data(self, data: str):
        if self._skip_depth:
            return
        text = data.strip()
        if text:
            self._append(text)

    def _append(self, text: str) -> None:
        for _, parts in self._capture_stack:
            parts.append(text)


class MetadataParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.title: str | None = None
        self.author: str | None = None
        self.published_at: str | None = None
        self._capture: str | None = None
        self._title_parts: list[str] = []
        self._h1_parts: list[str] = []

    def handle_starttag(self, tag: str, attrs):
        attrs_dict = {str(key).lower(): str(value) for key, value in attrs if value is not None}
        if tag == "meta":
            key = (
                attrs_dict.get("property")
                or attrs_dict.get("name")
                or attrs_dict.get("itemprop")
                or ""
            ).lower()
            content = attrs_dict.get("content", "").strip()
            if not content:
                return
            compact_key = key.replace("_", "").replace("-", "")
            if not self.title and key in TITLE_META_KEYS:
                self.title = _clean_title(content)
            elif not self.author and key in AUTHOR_META_KEYS:
                self.author = content
            elif not self.published_at and (key in PUBLISHED_META_KEYS or compact_key in PUBLISHED_META_KEYS):
                self.published_at = content
            return
        if tag == "time" and not self.published_at:
            timestamp = attrs_dict.get("datetime") or attrs_dict.get("content")
            if timestamp:
                self.published_at = timestamp.strip()
        if tag in {"title", "h1"}:
            self._capture = tag

    def handle_endtag(self, tag: str):
        if tag == "title" and self._capture == "title":
            if not self.title:
                self.title = _clean_title(" ".join(self._title_parts))
            self._capture = None
        elif tag == "h1" and self._capture == "h1":
            if not self.title:
                self.title = _clean_title(" ".join(self._h1_parts))
            self._capture = None

    def handle_data(self, data: str):
        text = data.strip()
        if not text:
            return
        if self._capture == "title":
            self._title_parts.append(text)
        elif self._capture == "h1":
            self._h1_parts.append(text)


@dataclass(frozen=True)
class ExtractedMetadata:
    title: str | None = None
    author: str | None = None
    published_at: str | None = None


def extract_metadata(html: str) -> ExtractedMetadata:
    parser = MetadataParser()
    parser.feed(html)
    return ExtractedMetadata(
        title=parser.title or None,
        author=parser.author or None,
        published_at=parser.published_at or None,
    )


def html_to_candidate_texts(html: str) -> list[str]:
    parser = TextBlockParser()
    parser.feed(html)
    candidates = [normalize_transcript(block) for block in parser.blocks]
    candidates.append(normalize_transcript(html_to_visible_text(html)))
    unique: list[str] = []
    seen: set[str] = set()
    for candidate in candidates:
        if candidate and candidate not in seen:
            seen.add(candidate)
            unique.append(candidate)
    return unique


def normalize_transcript(text: str) -> str:
    lines = []
    for raw_line in text.splitlines():
        line = re.sub(r"\s+", " ", raw_line).strip()
        if line:
            lines.append(line)
    return "\n\n".join(lines)


@dataclass(frozen=True)
class QualityResult:
    ok: bool
    reason: str | None = None
    length: int = 0
    paragraph_count: int = 0
    noise_hits: int = 0


@dataclass(frozen=True)
class CandidateDiagnostics:
    index: int
    selected: bool
    ok: bool
    reason: str | None
    length: int
    paragraph_count: int
    noise_hits: int
    preview: str


class TranscriptExtractor:
    def __init__(self, *, min_length: int = 300, min_paragraphs: int = 3, max_noise_ratio: float = 0.08):
        self.min_length = min_length
        self.min_paragraphs = min_paragraphs
        self.max_noise_ratio = max_noise_ratio

    def from_html(self, item: ContentItem, html: str) -> ContentDetail:
        item = self._merge_metadata(item, extract_metadata(html))
        text, quality = self.select_best_candidate(item, html_to_candidate_texts(html))
        return ContentDetail(
            item=item,
            transcript_text=text if quality.ok else "",
            has_transcript=quality.ok,
            raw_html_hash=hashlib.sha256(html.encode("utf-8")).hexdigest(),
            quality_reason=quality.reason,
        )

    @staticmethod
    def _merge_metadata(item: ContentItem, metadata: ExtractedMetadata) -> ContentItem:
        return ContentItem(
            source_url=item.source_url,
            detail_url=item.detail_url,
            dedao_id=item.dedao_id,
            column_name=item.column_name,
            title=metadata.title or item.title,
            published_at=metadata.published_at or item.published_at,
            author=metadata.author or item.author,
            content_type=item.content_type,
        )

    def from_text(self, item: ContentItem, text: str) -> ContentDetail:
        transcript = normalize_transcript(text)
        quality = self.check_quality(item, transcript)
        return ContentDetail(
            item=item,
            transcript_text=transcript if quality.ok else "",
            has_transcript=quality.ok,
            raw_html_hash=hashlib.sha256(text.encode("utf-8")).hexdigest(),
            quality_reason=quality.reason,
        )

    def check_quality(self, item: ContentItem, text: str) -> QualityResult:
        length = len(text)
        paragraphs = [line for line in text.split("\n\n") if line.strip()]
        paragraph_count = len(paragraphs)
        noise_hits = sum(text.count(pattern) for pattern in UI_NOISE_PATTERNS)
        noise_ratio = noise_hits / max(1, paragraph_count)
        if length < self.min_length:
            return QualityResult(False, "too_short", length, paragraph_count, noise_hits)
        if paragraph_count < self.min_paragraphs:
            return QualityResult(False, "too_few_paragraphs", length, paragraph_count, noise_hits)
        if noise_ratio > self.max_noise_ratio:
            return QualityResult(False, "too_much_ui_noise", length, paragraph_count, noise_hits)
        title_terms = [term for term in re.split(r"[\s·\-—：:，,。]+", item.title) if len(term) >= 2]
        if title_terms and not any(term in text for term in title_terms[:4]):
            return QualityResult(False, "title_not_related", length, paragraph_count, noise_hits)
        return QualityResult(True, None, length, paragraph_count, noise_hits)

    def select_best_candidate(self, item: ContentItem, candidates: list[str]) -> tuple[str, QualityResult]:
        if not candidates:
            return "", QualityResult(False, "empty", 0, 0, 0)
        scored: list[tuple[int, str, QualityResult]] = []
        for candidate in candidates:
            quality = self.check_quality(item, candidate)
            scored.append((self._quality_score(quality), candidate, quality))
        scored.sort(key=lambda row: row[0], reverse=True)
        for _, candidate, quality in scored:
            if quality.ok:
                return candidate, quality
        _, candidate, quality = scored[0]
        return candidate, quality

    def diagnose_candidates(self, item: ContentItem, candidates: list[str]) -> list[CandidateDiagnostics]:
        selected_text, _ = self.select_best_candidate(item, candidates)
        diagnostics: list[CandidateDiagnostics] = []
        for index, candidate in enumerate(candidates, start=1):
            quality = self.check_quality(item, candidate)
            preview = re.sub(r"\s+", " ", candidate).strip()[:120]
            diagnostics.append(
                CandidateDiagnostics(
                    index=index,
                    selected=candidate == selected_text,
                    ok=quality.ok,
                    reason=quality.reason,
                    length=quality.length,
                    paragraph_count=quality.paragraph_count,
                    noise_hits=quality.noise_hits,
                    preview=preview,
                )
            )
        return diagnostics

    @staticmethod
    def _quality_score(quality: QualityResult) -> int:
        score = quality.length + quality.paragraph_count * 50 - quality.noise_hits * 200
        if quality.ok:
            score += 10000
        return score
