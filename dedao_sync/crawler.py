from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urljoin, urlparse, urlunparse

from .extractor import TranscriptExtractor
from .models import AppConfig, ColumnConfig, ContentDetail, ContentItem


class CrawlerError(RuntimeError):
    pass


@dataclass
class CrawlResult:
    items: list[ContentItem]
    empty_but_valid: bool = False


class DedaoCrawler:
    def __init__(self, config: AppConfig, extractor: TranscriptExtractor | None = None):
        self.config = config
        self.extractor = extractor or TranscriptExtractor()

    def _sync_playwright(self):
        try:
            from playwright.sync_api import sync_playwright  # type: ignore
        except ImportError as exc:
            raise CrawlerError(
                "Playwright is not installed. Install dependencies and run: playwright install chromium"
            ) from exc
        return sync_playwright

    def _new_context(self, playwright):
        browser = playwright.chromium.launch(headless=self.config.dedao.headless)
        context = browser.new_context(storage_state=str(self.config.dedao.auth_state_path))
        return browser, context

    def check_login(self) -> bool:
        sync_playwright = self._sync_playwright()
        with sync_playwright() as playwright:
            browser, context = self._new_context(playwright)
            try:
                page = context.new_page()
                page.goto("https://www.dedao.cn/bought", wait_until="domcontentloaded", timeout=30000)
                page.wait_for_timeout(2000)
                url = page.url.lower()
                text = page.locator("body").inner_text(timeout=10000)
                login_markers = ("登录", "扫码", "验证码", "手机号")
                logged_in_markers = ("已购", "学习", "我的")
                if "login" in url or any(marker in text for marker in login_markers):
                    return False
                return any(marker in text for marker in logged_in_markers)
            finally:
                browser.close()

    def list_items(self, column: ColumnConfig) -> CrawlResult:
        sync_playwright = self._sync_playwright()
        with sync_playwright() as playwright:
            browser, context = self._new_context(playwright)
            try:
                page = context.new_page()
                page.goto(column.url, wait_until="domcontentloaded", timeout=45000)
                page.wait_for_timeout(int(self.config.dedao.request_interval_seconds * 1000))
                anchors = self._page_anchors(page)
                items = self.items_from_anchors(column, anchors)
                return CrawlResult(items=items, empty_but_valid=False)
            finally:
                browser.close()

    def fetch_detail(self, item: ContentItem) -> ContentDetail:
        sync_playwright = self._sync_playwright()
        with sync_playwright() as playwright:
            browser, context = self._new_context(playwright)
            try:
                page = context.new_page()
                page.goto(item.detail_url, wait_until="domcontentloaded", timeout=45000)
                page.wait_for_timeout(int(self.config.dedao.request_interval_seconds * 1000))
                title = page.title() or item.title
                html = page.content()
                if title and title != item.title:
                    item = ContentItem(
                        source_url=item.source_url,
                        detail_url=item.detail_url,
                        dedao_id=item.dedao_id,
                        column_name=item.column_name,
                        title=item.title,
                        published_at=item.published_at,
                        author=item.author,
                        content_type=item.content_type,
                    )
                detail = self.extractor.from_html(item, html)
                if not detail.has_transcript and self.config.dedao.save_failure_html:
                    path = self._save_failure_html(detail.item, html, detail.raw_html_hash)
                    detail = replace(detail, diagnostic_path=path)
                return detail
            finally:
                browser.close()
                time.sleep(self.config.dedao.request_interval_seconds)

    def _save_failure_html(self, item: ContentItem, html: str, raw_html_hash: str | None) -> Path:
        output_dir = self.config.dedao.failure_snapshot_dir
        output_dir.mkdir(parents=True, exist_ok=True)
        parsed = urlparse(item.detail_url or item.source_url)
        slug_source = "_".join(part for part in (parsed.netloc, parsed.path.strip("/"), item.dedao_id or item.title) if part)
        slug = re.sub(r"[^A-Za-z0-9._-]+", "_", slug_source).strip("_") or "detail"
        digest = (raw_html_hash or "").strip()[:12] or "nohash"
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        target = output_dir / f"{stamp}-{slug[:80]}-{digest}.html"
        target.write_text(html, encoding="utf-8")
        return target

    def inspect_page(self, url: str, output_dir: str | Path) -> Path:
        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)
        sync_playwright = self._sync_playwright()
        with sync_playwright() as playwright:
            browser, context = self._new_context(playwright)
            try:
                page = context.new_page()
                page.goto(url, wait_until="domcontentloaded", timeout=45000)
                page.wait_for_timeout(int(self.config.dedao.request_interval_seconds * 1000))
                slug = re.sub(r"[^A-Za-z0-9._-]+", "_", urlparse(url).netloc + urlparse(url).path).strip("_")
                if not slug:
                    slug = "page"
                target = output_dir / f"{slug}.html"
                target.write_text(page.content(), encoding="utf-8")
                text_target = output_dir / f"{slug}.txt"
                text_target.write_text(page.locator("body").inner_text(timeout=10000), encoding="utf-8")
                anchors_target = output_dir / f"{slug}.anchors.json"
                anchors_target.write_text(
                    json.dumps(self._page_anchors(page), ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
                return target
            finally:
                browser.close()

    @staticmethod
    def _page_anchors(page) -> list[dict[str, object]]:
        return page.eval_on_selector_all(
            "a",
            """els => els.map(a => ({
                href: a.href,
                text: (a.innerText || a.textContent || '').trim()
            }))""",
        )

    @classmethod
    def items_from_anchors(cls, column: ColumnConfig, anchors: list[dict[str, object]]) -> list[ContentItem]:
        items: list[ContentItem] = []
        seen: set[str] = set()
        column_url = cls._normalize_url(column.url, column.url)
        for anchor in anchors:
            href = str(anchor.get("href") or "")
            title = re.sub(r"\s+", " ", str(anchor.get("text") or "")).strip()
            if not href or not title or len(title) < 4:
                continue
            if not cls._looks_like_detail_url(href):
                continue
            detail_url = cls._normalize_url(href, column.url)
            if detail_url == column_url:
                continue
            if detail_url in seen:
                continue
            seen.add(detail_url)
            items.append(
                ContentItem(
                    source_url=detail_url,
                    detail_url=detail_url,
                    dedao_id=cls._extract_id(detail_url),
                    column_name=column.name,
                    title=title[:120],
                    content_type="web",
                )
            )
        return items

    @staticmethod
    def _looks_like_detail_url(url: str) -> bool:
        parsed = urlparse(url)
        if parsed.netloc and not parsed.netloc.endswith("dedao.cn"):
            return False
        text = url.lower()
        return any(marker in text for marker in ("detail", "course", "article", "audio", "video", "id="))

    @staticmethod
    def _normalize_url(url: str, base_url: str) -> str:
        absolute = urljoin(base_url, url)
        parsed = urlparse(absolute)
        query = urlencode(sorted(parse_qsl(parsed.query, keep_blank_values=True)), doseq=True)
        return urlunparse((parsed.scheme, parsed.netloc.lower(), parsed.path, "", query, ""))

    @staticmethod
    def _extract_id(url: str) -> str | None:
        match = re.search(r"[?&]id=([^&#]+)", url)
        if match:
            return match.group(1)
        path = urlparse(url).path.strip("/")
        return path.split("/")[-1] if path else None
