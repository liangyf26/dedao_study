"""得到直播回放抓取：列表翻页、详情解析与官方字幕转文字稿。

得到在回放详情的 roomDetail 接口里直接提供官方 ASR 字幕（SRT/VTT），
因此主路径不下载视频：拉取 SRT 并转成纯文本即可得到全文稿。
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import time
import urllib.request
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

from .browser import is_dedao_login_page
from .crawler import CrawlResult, CrawlerError, DedaoCrawler
from .models import ColumnConfig, ContentDetail, ContentItem, MediaCandidate
from .time_utils import APP_TIMEZONE, now_local

LOGGER = logging.getLogger(__name__)

LIVE_HOME_URL = "https://www.dedao.cn/live/home"
LIVE_LIST_API = "/api/pc/ddlive/v2/pc/home/live/list"
LIVE_DETAIL_API_MARKER = "/pc/ddlive/v2/pc/live/roomDetail"
LIVE_REPLAY_TAB_TEXT = "直播回放"

LIVE_PAGE_SIZE = 20
LIVE_LIST_MAX_PAGES = 30
CAPTION_DOWNLOAD_ATTEMPTS = 2
CAPTION_DOWNLOAD_TIMEOUT_SECONDS = 30

QUALITY_CAPTION_PENDING = "caption_pending"
QUALITY_NO_WATCH_PERMISSION = "policy_blocked:live_no_watch_permission"

SRT_REQUEST_HEADERS = {"User-Agent": "dedao-sync/0.1"}


def _string_values(value: object) -> list[str]:
    if isinstance(value, str):
        value = value.strip()
        return [value] if value else []
    if isinstance(value, (list, tuple)):
        return [item.strip() for item in value if isinstance(item, str) and item.strip()]
    return []


def _unique_string_values(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value not in seen:
            seen.add(value)
            result.append(value)
    return result


class LiveError(CrawlerError):
    pass


def parse_srt(srt_text: str) -> str:
    """把 SRT/VTT 字幕转成纯文本：去掉序号行和时间轴，保留台词行。"""
    lines: list[str] = []
    for raw_line in srt_text.splitlines():
        line = raw_line.strip()
        if not line or line == "WEBVTT" or line.startswith(("NOTE", "STYLE", "REGION")):
            continue
        if line.isdigit():
            continue
        if "-->" in line:
            continue
        # WebVTT cue settings 可附在时间轴之后，已由上面的判断过滤。
        line = re.sub(r"<[^>]+>", "", line).strip()
        if line:
            lines.append(line)
    return "\n".join(lines)


def _format_publish_time(value: object) -> str | None:
    if value in (None, ""):
        return None
    try:
        timestamp = int(float(str(value)))
    except (TypeError, ValueError):
        return str(value).strip() or None
    if timestamp <= 0:
        return None
    return datetime.fromtimestamp(timestamp, APP_TIMEZONE).strftime("%Y-%m-%d")


def extract_replay_entries(payload: dict) -> list[dict]:
    container = payload.get("c") if isinstance(payload, dict) else None
    entries = container.get("list") if isinstance(container, dict) else None
    return [entry for entry in entries if isinstance(entry, dict)] if isinstance(entries, list) else []


def items_from_replay_entries(column: ColumnConfig, entries: list[dict]) -> list[ContentItem]:
    items: list[ContentItem] = []
    seen: set[str] = set()
    for entry in entries:
        title = re.sub(r"\s+", " ", str(entry.get("title") or "")).strip()
        if len(title) < 4:
            continue
        published_at = _format_publish_time(entry.get("starttime"))
        if column.backfill_since and published_at and published_at < column.backfill_since:
            continue
        alias_id = str(entry.get("alias_id") or "").strip()
        detail_url = str(entry.get("share_url") or "").strip()
        if not detail_url and alias_id:
            detail_url = f"https://www.dedao.cn/live/detail?id={alias_id}"
        if not detail_url:
            continue
        if detail_url in seen:
            continue
        seen.add(detail_url)
        room_id = str(entry.get("room_id") or entry.get("id") or "").strip()
        items.append(
            ContentItem(
                source_url=detail_url,
                detail_url=detail_url,
                dedao_id=room_id or alias_id or None,
                column_name=column.name,
                title=title[:120],
                published_at=published_at,
                content_type="live_replay",
            )
        )
    return items


def _fetch_list_payload(page, *, live_type: int, page_number: int) -> dict:
    result = page.evaluate(
        """async ({ liveType, page, pageSize }) => {
            const resp = await fetch('/api/pc/ddlive/v2/pc/home/live/list', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ live_type: liveType, page: page, page_size: pageSize })
            });
            return await resp.json();
        }""",
        {"liveType": live_type, "page": page_number, "pageSize": LIVE_PAGE_SIZE},
    )
    if not isinstance(result, dict):
        raise LiveError("live list API returned non-object payload")
    return result


class LiveCrawler:
    def __init__(self, config):
        self.config = config
        self._login_checker = DedaoCrawler(config)

    def check_login(self) -> bool:
        return self._login_checker.check_login()

    def list_items(self, column: ColumnConfig) -> CrawlResult:
        return self.list_replays(column, backfill_since=column.backfill_since)

    def fetch_detail(self, item: ContentItem) -> ContentDetail:
        return self.fetch_replay_detail(item)

    def _sync_playwright(self):
        try:
            from playwright.sync_api import sync_playwright  # type: ignore
        except ImportError as exc:
            raise LiveError(
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

    def _request_delay_ms(self) -> int:
        base = self.config.dedao.request_interval_seconds
        if base <= 0:
            return 0
        return int(base * 1000)

    def list_replays(self, column: ColumnConfig, *, backfill_since: str | None = None) -> CrawlResult:
        sync_playwright = self._sync_playwright()
        with sync_playwright() as playwright:
            browser, context = self._new_context(playwright)
            try:
                page = context.new_page()
                DedaoCrawler._goto_page(page, LIVE_HOME_URL, timeout=45000)
                page.wait_for_timeout(4000)
                entries = self._collect_replay_entries(page, backfill_since=backfill_since)
                items = items_from_replay_entries(column, entries)
                diagnostic_path = None
                if not items and self.config.dedao.save_failure_html:
                    diagnostic_path = self._save_list_failure_snapshot(column, page)
                return CrawlResult(items=items, empty_but_valid=False, diagnostic_path=diagnostic_path)
            finally:
                self._close_context(browser, context)
                time.sleep(self._request_delay_ms() / 1000)

    def _collect_replay_entries(self, page, *, backfill_since: str | None) -> list[dict]:
        entries: list[dict] = []
        seen_room_ids: set[str] = set()
        for page_number in range(1, LIVE_LIST_MAX_PAGES + 1):
            try:
                payload = _fetch_list_payload(page, live_type=3, page_number=page_number)
            except Exception as exc:
                if page_number == 1:
                    # 首页直接 fetch 失败时，点一次回放 tab 让应用自己发请求，再重试。
                    self._open_replay_tab(page)
                    try:
                        payload = _fetch_list_payload(page, live_type=3, page_number=page_number)
                    except Exception:
                        raise LiveError(f"live list API failed: {exc}") from exc
                else:
                    raise LiveError(f"live list API failed on page {page_number}: {exc}") from exc
            page_entries = extract_replay_entries(payload)
            if not page_entries:
                break
            fresh = 0
            for entry in page_entries:
                room_key = str(entry.get("room_id") or entry.get("id") or entry.get("alias_id") or "")
                if room_key and room_key in seen_room_ids:
                    continue
                if room_key:
                    seen_room_ids.add(room_key)
                entries.append(entry)
                fresh += 1
            if fresh == 0:
                break
            is_more = (payload.get("c") or {}).get("is_more")
            oldest = min(
                (_format_publish_time(entry.get("starttime")) or "9999-12-31" for entry in page_entries),
                default=None,
            )
            if backfill_since and oldest and oldest < backfill_since:
                LOGGER.info(
                    "live list pagination: page %d reached backfill cutoff (oldest=%s <= %s)",
                    page_number,
                    oldest,
                    backfill_since,
                )
                break
            if is_more != 1:
                break
            page.wait_for_timeout(max(500, self._request_delay_ms()))
        return entries

    @staticmethod
    def _open_replay_tab(page) -> None:
        for text in (LIVE_REPLAY_TAB_TEXT, "回放"):
            try:
                page.get_by_text(text, exact=False).first.click(timeout=5000)
                page.wait_for_timeout(3000)
                return
            except Exception:
                continue

    def fetch_replay_detail(self, item: ContentItem) -> ContentDetail:
        sync_playwright = self._sync_playwright()
        with sync_playwright() as playwright:
            browser, context = self._new_context(playwright)
            try:
                page = context.new_page()
                payloads: list[dict] = []

                def on_response(response) -> None:
                    if LIVE_DETAIL_API_MARKER not in response.url:
                        return
                    try:
                        payload = response.json()
                    except Exception:
                        return
                    if isinstance(payload, dict):
                        payloads.append(payload)

                page.on("response", on_response)
                DedaoCrawler._goto_page(page, item.detail_url, timeout=45000)
                deadline = time.time() + 30
                while time.time() < deadline and not payloads:
                    page.wait_for_timeout(1000)
                if not payloads:
                    raise LiveError(f"roomDetail API not captured for {item.title}")
                payload = payloads[-1]
                playback = self._extract_playback_info(payload)
                body_text = self._page_body_text(page)
                if is_dedao_login_page(page.url, body_text):
                    return ContentDetail(
                        item=item,
                        transcript_text="",
                        has_transcript=False,
                        raw_html_hash=hashlib.sha256(json.dumps(payloads[-1], ensure_ascii=False).encode("utf-8")).hexdigest(),
                        quality_reason="login_required",
                    )
                detail_item = self._enrich_item_from_payload(item, payload)
                return self._build_detail(detail_item, playback)
            finally:
                self._close_context(browser, context)
                time.sleep(self._request_delay_ms() / 1000)

    @staticmethod
    def _page_body_text(page) -> str:
        try:
            return page.locator("body").inner_text(timeout=5000)
        except Exception:
            return ""

    @staticmethod
    def _extract_playback_info(payload: dict) -> dict:
        container = payload.get("c")
        playback = container.get("playback_info") if isinstance(container, dict) else None
        if not isinstance(playback, dict):
            raise LiveError("roomDetail payload has no playback_info")
        return playback

    @staticmethod
    def _enrich_item_from_payload(item: ContentItem, payload: dict) -> ContentItem:
        container = payload.get("c") if isinstance(payload, dict) else None
        if not isinstance(container, dict):
            return item
        return replace(
            item,
            title=str(container.get("title") or item.title).strip()[:120],
            author=str(container.get("author") or item.author or "").strip() or item.author,
            published_at=str(container.get("starttime") or item.published_at or "").strip() or item.published_at,
        )

    def _build_detail(self, item: ContentItem, playback: dict) -> ContentDetail:
        media_candidates = tuple(
            MediaCandidate(url=url, mime_type="application/x-mpegURL", label="m3u8")
            for url in _string_values(playback.get("sd"))
        )
        raw_fingerprint = json.dumps(playback, ensure_ascii=False, sort_keys=True)
        duration_text = str(playback.get("duration_text") or "")
        caption_urls = _unique_string_values(
            [*(_string_values(playback.get("caption"))), *(_string_values(playback.get("vtt_url")))]
        )
        last_caption_error: Exception | None = None
        for caption_url in caption_urls:
            try:
                caption_text = self._download_caption(caption_url)
            except Exception as exc:
                last_caption_error = exc
                LOGGER.warning("caption download failed for %s url=%s: %s", item.title, caption_url, exc)
                continue
            transcript = parse_srt(caption_text)
            if transcript.strip():
                return ContentDetail(
                    item=item,
                    transcript_text=transcript,
                    has_transcript=True,
                    media_candidates=media_candidates,
                    raw_html_hash=hashlib.sha256(caption_text.encode("utf-8")).hexdigest(),
                    quality_reason=None,
                )
        if last_caption_error:
            LOGGER.warning("all caption downloads failed for %s: %s", item.title, last_caption_error)
        if media_candidates:
            reason = QUALITY_CAPTION_PENDING
        else:
            reason = QUALITY_CAPTION_PENDING if str(playback.get("playback_status")) == "0" else QUALITY_NO_WATCH_PERMISSION
        LOGGER.info(
            "live replay without caption: %s playback_status=%s duration=%s reason=%s",
            item.title,
            playback.get("playback_status"),
            duration_text,
            reason,
        )
        return ContentDetail(
            item=item,
            transcript_text="",
            has_transcript=False,
            media_candidates=media_candidates,
            raw_html_hash=hashlib.sha256(raw_fingerprint.encode("utf-8")).hexdigest(),
            quality_reason=reason,
        )

    @staticmethod
    def _download_caption(caption_url: str) -> str:
        last_error: Exception | None = None
        for attempt in range(1, CAPTION_DOWNLOAD_ATTEMPTS + 1):
            try:
                request = urllib.request.Request(caption_url, headers=SRT_REQUEST_HEADERS, method="GET")
                with urllib.request.urlopen(request, timeout=CAPTION_DOWNLOAD_TIMEOUT_SECONDS) as response:
                    return response.read().decode("utf-8", errors="replace")
            except Exception as exc:
                last_error = exc
                if attempt < CAPTION_DOWNLOAD_ATTEMPTS:
                    time.sleep(2 * attempt)
        raise LiveError(f"caption download failed: {last_error}")

    def _save_list_failure_snapshot(self, column: ColumnConfig, page) -> Path:
        output_dir = self.config.dedao.failure_snapshot_dir
        output_dir.mkdir(parents=True, exist_ok=True)
        parsed = urlparse(column.url)
        slug_source = "_".join(part for part in (parsed.netloc, parsed.path.strip("/"), column.name) if part)
        slug = re.sub(r"[^A-Za-z0-9._-]+", "_", slug_source).strip("_") or "live_list"
        stamp = now_local().strftime("%Y%m%d-%H%M%S")
        target = output_dir / f"{stamp}-live-list-{slug[:80]}.html"
        target.write_text(page.content(), encoding="utf-8")
        return target
