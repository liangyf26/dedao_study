from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from html.parser import HTMLParser
from urllib.parse import urljoin, urlparse

from .models import ContentDetail, ContentItem, MediaCandidate
from .policy import check_page_policy


UI_NOISE_PATTERNS = (
    # 只保留 UI 特征词；"分享/评论/购买" 等口播文案常见词不在此列，避免误伤正文
    "登录",
    "扫码",
    "下载App",
    "相关推荐",
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

MEDIA_META_PREFIXES = ("og:audio", "og:video", "twitter:player")
GENERIC_DEDAO_TITLE_MARKERS = (
    "得到app",
    "得到 app",
    "知识就是力量",
    "知识就在得到",
)


def _clean_visible_text(text: str) -> str:
    text = re.sub(r"[ \t\r\f\v]+", " ", text)
    text = re.sub(r" *\n *", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _clean_title(title: str) -> str:
    title = re.sub(r"\s+", " ", title).strip()
    title = re.split(r"\s*[-_|]\s*得到(?:App|网页版)?", title, maxsplit=1)[0].strip()
    return title


def _is_generic_dedao_title(title: str) -> bool:
    normalized = title.strip().lower()
    if normalized in {"得到", "得到app", "得到 app", "得到网页版"}:
        return True
    return any(marker in normalized for marker in GENERIC_DEDAO_TITLE_MARKERS)


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
        self._jsonld_parts: list[str] = []

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
        if tag == "script" and "ld+json" in attrs_dict.get("type", "").lower():
            self._capture = "jsonld"
        elif tag in {"title", "h1"}:
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
        elif tag == "script" and self._capture == "jsonld":
            self._merge_jsonld_metadata(" ".join(self._jsonld_parts))
            self._jsonld_parts = []
            self._capture = None

    def handle_data(self, data: str):
        text = data.strip()
        if not text:
            return
        if self._capture == "jsonld":
            self._jsonld_parts.append(text)
        elif self._capture == "title":
            self._title_parts.append(text)
        elif self._capture == "h1":
            self._h1_parts.append(text)

    def _merge_jsonld_metadata(self, text: str) -> None:
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            return
        for obj in _iter_jsonld_objects(payload):
            if not isinstance(obj, dict):
                continue
            if not self.title:
                title = obj.get("headline") or obj.get("name")
                if title:
                    self.title = _clean_title(str(title))
            if not self.author:
                author = _jsonld_author_name(obj.get("author") or obj.get("creator"))
                if author:
                    self.author = author
            if not self.published_at:
                published = obj.get("datePublished") or obj.get("dateModified") or obj.get("uploadDate")
                if published:
                    self.published_at = str(published).strip()


def _iter_jsonld_objects(value):
    if isinstance(value, list):
        for item in value:
            yield from _iter_jsonld_objects(item)
    elif isinstance(value, dict):
        yield value
        graph = value.get("@graph")
        if isinstance(graph, list):
            for item in graph:
                yield from _iter_jsonld_objects(item)


def _jsonld_author_name(value) -> str | None:
    if isinstance(value, str):
        return value.strip() or None
    if isinstance(value, dict):
        name = value.get("name")
        return str(name).strip() if name else None
    if isinstance(value, list):
        names = [name for item in value if (name := _jsonld_author_name(item))]
        return "，".join(names) if names else None
    return None


class MediaCandidateParser(HTMLParser):
    def __init__(self, base_url: str):
        super().__init__()
        self.base_url = base_url
        self.candidates: list[MediaCandidate] = []
        self._seen: set[str] = set()

    def handle_starttag(self, tag: str, attrs):
        attrs_dict = {str(key).lower(): str(value) for key, value in attrs if value is not None}
        if tag in {"audio", "video", "source"}:
            self._add(
                attrs_dict.get("src"),
                mime_type=attrs_dict.get("type"),
                label=tag,
            )
            return
        if tag == "meta":
            key = (attrs_dict.get("property") or attrs_dict.get("name") or "").lower()
            if not key.startswith(MEDIA_META_PREFIXES):
                return
            self._add(attrs_dict.get("content"), label=key)

    def _add(self, raw_url: str | None, *, mime_type: str | None = None, label: str | None = None) -> None:
        if not raw_url:
            return
        absolute = urljoin(self.base_url, raw_url.strip())
        parsed = urlparse(absolute)
        if parsed.scheme not in {"http", "https"}:
            return
        if absolute in self._seen:
            return
        self._seen.add(absolute)
        self.candidates.append(MediaCandidate(url=absolute, mime_type=mime_type, label=label))


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


def extract_media_candidates(html: str, base_url: str) -> tuple[MediaCandidate, ...]:
    parser = MediaCandidateParser(base_url)
    parser.feed(html)
    return tuple(parser.candidates)


def html_to_candidate_texts(html: str) -> list[str]:
    parser = TextBlockParser()
    parser.feed(html)
    visible_text = html_to_visible_text(html)
    candidates = [normalize_transcript(block) for block in parser.blocks]
    transcript_section = extract_dedao_transcript_section(visible_text)
    if transcript_section:
        candidates.insert(0, normalize_transcript(transcript_section))
    candidates.append(normalize_transcript(visible_text))
    unique: list[str] = []
    seen: set[str] = set()
    for candidate in candidates:
        if candidate and candidate not in seen:
            seen.add(candidate)
            unique.append(candidate)
    return unique


def extract_dedao_transcript_section(text: str) -> str:
    lines = [line.strip() for line in text.splitlines()]
    start = _first_line_index(lines, "全文稿")
    if start is None:
        # 新版页面无“全文稿”标题，转录直接跟在“转述师：xx”行之后
        for index, line in enumerate(lines):
            if "转述师" in line:
                start = index
                break
    if start is None:
        return ""
    end_markers = {
        "发布",
        "公开",
        "写留言，与作者互动",
        "我的留言",
        "用户留言",
        "上一篇",
        "联系我们：",
        "相关链接：",
        "了解更多：",
    }
    contains_end_markers = ("0 / 5000", "收起 取消 写笔记")
    end = len(lines)
    for index in range(start + 1, len(lines)):
        line = lines[index]
        if line in end_markers or any(marker in line for marker in contains_end_markers):
            end = index
            break
    return "\n".join(line for line in lines[start + 1 : end] if line)


def _first_line_index(lines: list[str], value: str) -> int | None:
    for index, line in enumerate(lines):
        if line == value:
            return index
    return None


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
        media_candidates = extract_media_candidates(html, item.detail_url or item.source_url)
        visible_text = html_to_visible_text(html)
        transcript_section = normalize_transcript(extract_dedao_transcript_section(visible_text))
        section_quality = (
            self.check_quality(item, transcript_section, require_title_related=False, max_noise_ratio=0.3)
            if transcript_section
            else QualityResult(False, "empty", 0, 0, 0)
        )
        text, quality = self.select_best_candidate(item, html_to_candidate_texts(html))
        policy = check_page_policy(html, media_candidates)
        if not policy.allowed:
            return ContentDetail(
                item=item,
                transcript_text="",
                has_transcript=False,
                media_candidates=media_candidates,
                raw_html_hash=hashlib.sha256(html.encode("utf-8")).hexdigest(),
                quality_reason=f"policy_blocked:{policy.reason}",
            )
        if section_quality.ok:
            return ContentDetail(
                item=item,
                transcript_text=transcript_section,
                has_transcript=True,
                media_candidates=media_candidates,
                raw_html_hash=hashlib.sha256(html.encode("utf-8")).hexdigest(),
                quality_reason=None,
            )
        return ContentDetail(
            item=item,
            transcript_text=text if quality.ok else "",
            has_transcript=quality.ok,
            media_candidates=media_candidates,
            raw_html_hash=hashlib.sha256(html.encode("utf-8")).hexdigest(),
            quality_reason=quality.reason,
        )

    def from_ddarticle_payload(self, item: ContentItem, payload: dict) -> ContentDetail:
        transcript = ddarticle_payload_to_transcript(payload)
        quality = self.check_quality(item, transcript, require_title_related=False)
        return ContentDetail(
            item=item,
            transcript_text=transcript if quality.ok else "",
            has_transcript=quality.ok,
            raw_html_hash=hashlib.sha256(json.dumps(payload, ensure_ascii=False).encode("utf-8")).hexdigest(),
            quality_reason=quality.reason,
        )

    @staticmethod
    def _merge_metadata(item: ContentItem, metadata: ExtractedMetadata) -> ContentItem:
        title = metadata.title if metadata.title and not _is_generic_dedao_title(metadata.title) else item.title
        return ContentItem(
            source_url=item.source_url,
            detail_url=item.detail_url,
            dedao_id=item.dedao_id,
            column_name=item.column_name,
            title=title,
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

    def check_quality(
        self,
        item: ContentItem,
        text: str,
        *,
        require_title_related: bool = True,
        max_noise_ratio: float | None = None,
    ) -> QualityResult:
        length = len(text)
        paragraphs = [line for line in text.split("\n\n") if line.strip()]
        paragraph_count = len(paragraphs)
        noise_hits = sum(text.count(pattern) for pattern in UI_NOISE_PATTERNS)
        noise_ratio = noise_hits / max(1, paragraph_count)
        if length < self.min_length:
            return QualityResult(False, "too_short", length, paragraph_count, noise_hits)
        if paragraph_count < self.min_paragraphs:
            return QualityResult(False, "too_few_paragraphs", length, paragraph_count, noise_hits)
        allowed_noise_ratio = self.max_noise_ratio if max_noise_ratio is None else max_noise_ratio
        if noise_ratio > allowed_noise_ratio:
            return QualityResult(False, "too_much_ui_noise", length, paragraph_count, noise_hits)
        title_terms = [term for term in re.split(r"[\s·\-—：:，,。？！!?｜|《》【】（）()]+", item.title) if len(term) >= 2]
        if require_title_related and title_terms and not any(term in text for term in title_terms[:4]):
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


def ddarticle_payload_to_transcript(payload: dict) -> str:
    container = payload.get("c")
    if not isinstance(container, dict):
        return ""
    raw_content = container.get("content")
    blocks = _parse_ddarticle_blocks(raw_content)
    lines: list[str] = []
    for block in blocks:
        text = _ddarticle_block_text(block)
        if text:
            lines.append(text)
    return normalize_transcript("\n\n".join(lines))


def _parse_ddarticle_blocks(raw_content: object) -> list[object]:
    if isinstance(raw_content, list):
        return raw_content
    if isinstance(raw_content, str):
        raw_content = raw_content.strip()
        if not raw_content:
            return []
        try:
            parsed = json.loads(raw_content)
        except json.JSONDecodeError:
            return [{"type": "paragraph", "text": raw_content}]
        if isinstance(parsed, list):
            return parsed
        if isinstance(parsed, dict):
            return [parsed]
    if isinstance(raw_content, dict):
        return [raw_content]
    return []


def _ddarticle_block_text(block: object) -> str:
    if isinstance(block, str):
        return block.strip()
    if not isinstance(block, dict):
        return ""
    block_type = str(block.get("type") or "").strip().lower()
    if block_type in {"audio", "image", "video", "divider"}:
        return ""
    if block_type == "salutation":
        return ""
    text = block.get("text")
    if isinstance(text, str) and text.strip():
        return _clean_ddarticle_text(text)
    contents = block.get("contents")
    if isinstance(contents, list):
        parts = [_ddarticle_inline_text(part) for part in contents]
        return _clean_ddarticle_text("".join(part for part in parts if part))
    return ""


def _ddarticle_inline_text(value: object) -> str:
    if isinstance(value, str):
        return value
    if not isinstance(value, dict):
        return ""
    text = value.get("text")
    if isinstance(text, str):
        return text
    if isinstance(text, dict):
        content = text.get("content")
        if isinstance(content, str):
            return content
    contents = value.get("contents")
    if isinstance(contents, list):
        return "".join(_ddarticle_inline_text(part) for part in contents)
    return ""


def _clean_ddarticle_text(text: str) -> str:
    text = text.replace("$_IGET_USER_NAME_$", "").strip()
    text = re.sub(r"\s+", " ", text)
    return text.strip()
