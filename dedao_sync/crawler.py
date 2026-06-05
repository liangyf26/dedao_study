from __future__ import annotations

import json
import random
import re
import time
from dataclasses import dataclass, replace
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urljoin, urlparse, urlunparse

from .browser import is_dedao_logged_in_page
from .extractor import TranscriptExtractor
from .models import AppConfig, ColumnConfig, ContentDetail, ContentItem
from .time_utils import now_local


class CrawlerError(RuntimeError):
    pass


@dataclass
class CrawlResult:
    items: list[ContentItem]
    empty_but_valid: bool = False
    diagnostic_path: Path | None = None


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
        if self.config.dedao.browser_profile_dir.exists():
            context = playwright.chromium.launch_persistent_context(
                user_data_dir=str(self.config.dedao.browser_profile_dir),
                headless=self.config.dedao.headless,
            )
            return None, context
        browser = playwright.chromium.launch(headless=self.config.dedao.headless)
        context = browser.new_context(storage_state=str(self.config.dedao.auth_state_path))
        return browser, context

    @staticmethod
    def _close_context(browser, context) -> None:
        if browser is None:
            context.close()
        else:
            browser.close()

    def check_login(self) -> bool:
        sync_playwright = self._sync_playwright()
        with sync_playwright() as playwright:
            browser, context = self._new_context(playwright)
            try:
                page = context.new_page()
                self._goto_page(page, "https://www.dedao.cn/bought", timeout=30000)
                page.wait_for_timeout(2000)
                text = page.locator("body").inner_text(timeout=10000)
                return is_dedao_logged_in_page(page.url, text)
            finally:
                self._close_context(browser, context)

    def list_items(self, column: ColumnConfig) -> CrawlResult:
        sync_playwright = self._sync_playwright()
        with sync_playwright() as playwright:
            browser, context = self._new_context(playwright)
            try:
                page = context.new_page()
                self._goto_page(page, column.url, timeout=45000)
                self._prepare_list_page(page)
                anchors = self._page_anchors(page)
                items = self.items_from_anchors(column, anchors)
                diagnostic_path = None
                if not items and self.config.dedao.save_failure_html:
                    diagnostic_path = self._save_list_failure_snapshot(column, page, anchors)
                return CrawlResult(items=items, empty_but_valid=False, diagnostic_path=diagnostic_path)
            finally:
                self._close_context(browser, context)

    def fetch_detail(self, item: ContentItem) -> ContentDetail:
        sync_playwright = self._sync_playwright()
        with sync_playwright() as playwright:
            browser, context = self._new_context(playwright)
            try:
                page = context.new_page()
                self._goto_page(page, item.detail_url, timeout=45000)
                page.wait_for_timeout(self._request_delay_ms())
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
                self._close_context(browser, context)
                time.sleep(self._request_delay_seconds())

    def _save_failure_html(self, item: ContentItem, html: str, raw_html_hash: str | None) -> Path:
        output_dir = self.config.dedao.failure_snapshot_dir
        output_dir.mkdir(parents=True, exist_ok=True)
        parsed = urlparse(item.detail_url or item.source_url)
        slug_source = "_".join(part for part in (parsed.netloc, parsed.path.strip("/"), item.dedao_id or item.title) if part)
        slug = re.sub(r"[^A-Za-z0-9._-]+", "_", slug_source).strip("_") or "detail"
        digest = (raw_html_hash or "").strip()[:12] or "nohash"
        stamp = now_local().strftime("%Y%m%d-%H%M%S")
        target = output_dir / f"{stamp}-{slug[:80]}-{digest}.html"
        target.write_text(html, encoding="utf-8")
        return target

    def _save_list_failure_snapshot(self, column: ColumnConfig, page, anchors: list[dict[str, object]]) -> Path:
        output_dir = self.config.dedao.failure_snapshot_dir
        output_dir.mkdir(parents=True, exist_ok=True)
        parsed = urlparse(column.url)
        slug_source = "_".join(part for part in (parsed.netloc, parsed.path.strip("/"), parsed.query, column.name) if part)
        slug = re.sub(r"[^A-Za-z0-9._-]+", "_", slug_source).strip("_") or "list"
        stamp = now_local().strftime("%Y%m%d-%H%M%S")
        target = output_dir / f"{stamp}-list-{slug[:80]}.html"
        target.write_text(page.content(), encoding="utf-8")
        try:
            target.with_suffix(".txt").write_text(page.locator("body").inner_text(timeout=10000), encoding="utf-8")
        except Exception:
            pass
        target.with_suffix(".anchors.json").write_text(json.dumps(anchors, ensure_ascii=False, indent=2), encoding="utf-8")
        return target

    def inspect_page(self, url: str, output_dir: str | Path) -> Path:
        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)
        sync_playwright = self._sync_playwright()
        with sync_playwright() as playwright:
            browser, context = self._new_context(playwright)
            try:
                page = context.new_page()
                self._goto_page(page, url, timeout=45000)
                self._prepare_list_page(page)
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
                self._close_context(browser, context)

    def _request_delay_seconds(self) -> float:
        return self.jittered_delay_seconds(self.config.dedao.request_interval_seconds)

    def _request_delay_ms(self) -> int:
        return int(self._request_delay_seconds() * 1000)

    @staticmethod
    def _goto_page(page, url: str, *, timeout: int) -> None:
        try:
            page.goto(url, wait_until="commit", timeout=timeout)
        except Exception as exc:
            message = str(exc).lower()
            unsupported_wait_state = (
                "expected one of" in message
                or (("wait_until" in message or "waituntil" in message) and ("invalid" in message or "unknown" in message))
            )
            if "timeout" in message or not unsupported_wait_state:
                raise
            page.goto(url, wait_until="domcontentloaded", timeout=timeout)

    def _prepare_list_page(self, page) -> None:
        try:
            page.wait_for_load_state("networkidle", timeout=15000)
        except Exception:
            pass
        page.wait_for_timeout(self._request_delay_ms())
        self._scroll_page(page)

    @staticmethod
    def _scroll_page(page, *, steps: int = 4, wait_ms: int = 800) -> None:
        for _ in range(steps):
            try:
                page.evaluate("window.scrollBy(0, Math.max(window.innerHeight, 800))")
                page.wait_for_timeout(wait_ms)
            except Exception:
                return

    @staticmethod
    def jittered_delay_seconds(base_seconds: float) -> float:
        if base_seconds <= 0:
            return 0
        return base_seconds + random.uniform(0, max(0.5, base_seconds * 0.25))

    @staticmethod
    def _page_anchors(page) -> list[dict[str, object]]:
        return page.eval_on_selector_all(
            "a, [role=link], [onclick], [data-url], [data-href], [data-link], [data-jump-url]",
            """els => els.map(a => ({
                href: (
                    a.href ||
                    a.getAttribute('href') ||
                    a.getAttribute('data-url') ||
                    a.getAttribute('data-href') ||
                    a.getAttribute('data-link') ||
                    a.getAttribute('data-jump-url') ||
                    a.dataset?.url ||
                    a.dataset?.href ||
                    a.dataset?.link ||
                    a.dataset?.jumpUrl ||
                    ''
                ),
                text: (a.innerText || a.textContent || '').trim(),
                title: (a.getAttribute('title') || '').trim(),
                aria_label: (a.getAttribute('aria-label') || '').trim(),
                data_title: (a.getAttribute('data-title') || a.dataset?.title || '').trim(),
                card_text: ((a.closest('article, li, [class*=card], [class*=item], [class*=course], [class*=list]') || a).innerText || '').trim(),
                dataset: Object.assign({}, a.dataset || {})
            }))""",
        )

    @classmethod
    def items_from_anchors(cls, column: ColumnConfig, anchors: list[dict[str, object]]) -> list[ContentItem]:
        items: list[ContentItem] = []
        seen: set[str] = set()
        column_url = cls._normalize_url(column.url, column.url)
        for anchor in anchors:
            href = str(anchor.get("href") or "")
            title = cls._anchor_title(anchor)
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
    def _anchor_title(anchor: dict[str, object]) -> str:
        fallback = ""
        for key in ("text", "title", "aria_label", "data_title", "card_text"):
            title = re.sub(r"\s+", " ", str(anchor.get(key) or "")).strip()
            if len(title) >= 4:
                return title[:120]
            if title and not fallback:
                fallback = title
        return fallback[:120]

    @staticmethod
    def _looks_like_detail_url(url: str) -> bool:
        parsed = urlparse(url)
        if parsed.netloc and not parsed.netloc.endswith("dedao.cn"):
            return False
        text = url.lower()
        return any(
            marker in text
            for marker in ("detail", "course", "article", "audio", "video", "content", "knowledge", "id=")
        )

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
