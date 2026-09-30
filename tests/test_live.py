from __future__ import annotations

import io
import json
import unittest
import urllib.error
from unittest import mock

from dedao_sync.live import LiveCrawler, items_from_replay_entries, parse_srt
from dedao_sync.models import ColumnConfig, ContentItem


class FakeCaptionResponse:
    def __init__(self, text: str):
        self.text = text

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def read(self):
        return self.text.encode("utf-8")


class LiveTests(unittest.TestCase):
    def test_parse_srt_removes_timing_numbers_and_markup(self):
        text = """WEBVTT

1
00:00:01,000 --> 00:00:03,000
<v Speaker>第一句</v>

2
00:00:04.000 --> 00:00:06.000 line:90%
第二句
"""
        self.assertEqual(parse_srt(text), "第一句\n第二句")

    def test_items_filter_before_backfill_and_deduplicate(self):
        column = ColumnConfig(
            name="得到直播",
            url="https://www.dedao.cn/live/home",
            kind="live",
            backfill_since="2026-09-01",
        )
        entries = [
            {"title": "九月直播", "starttime": 1788220800, "share_url": "https://dedao.cn/replay/1", "room_id": "1"},
            {"title": "八月直播", "starttime": 1788134400, "share_url": "https://dedao.cn/replay/2", "room_id": "2"},
            {"title": "重复直播", "starttime": 1788220800, "share_url": "https://dedao.cn/replay/1", "room_id": "1"},
        ]
        items = items_from_replay_entries(column, entries)
        self.assertEqual([item.dedao_id for item in items], ["1"])
        self.assertEqual(items[0].content_type, "live_replay")

    def test_items_filter_until_inclusive(self):
        column = ColumnConfig(
            name="得到直播",
            url="https://www.dedao.cn/live/home",
            kind="live",
            backfill_since="2026-08-01",
            backfill_until="2026-08-31",
        )
        entries = [
            {"title": "八月初直播", "starttime": 1785542400, "share_url": "https://dedao.cn/replay/a", "room_id": "a"},
            {"title": "八月底直播", "starttime": 1788134400, "share_url": "https://dedao.cn/replay/b", "room_id": "b"},
            {"title": "九月初直播", "starttime": 1788220800, "share_url": "https://dedao.cn/replay/c", "room_id": "c"},
        ]
        items = items_from_replay_entries(column, entries)
        self.assertEqual([item.dedao_id for item in items], ["a", "b"])

        crawler = LiveCrawler.__new__(LiveCrawler)
        item = ContentItem(
            source_url="https://dedao.cn/replay/1",
            column_name="得到直播",
            title="直播标题",
            detail_url="https://dedao.cn/replay/1",
            dedao_id="1",
        )
        playback = {
            "caption": "https://cdn.example/caption.srt",
            "vtt_url": "https://cdn.example/caption.vtt",
            "sd": "https://cdn.example/video.m3u8",
        }
        responses = [urllib.error.URLError("caption unavailable"), FakeCaptionResponse("WEBVTT\n\n00:00:01.000 --> 00:00:02.000\n备用字幕")]
        with mock.patch("dedao_sync.live.urllib.request.urlopen", side_effect=responses) as urlopen:
            with mock.patch("dedao_sync.live.time.sleep"):
                detail = crawler._build_detail(item, playback)

        self.assertTrue(detail.has_transcript)
        self.assertEqual(detail.transcript_text, "备用字幕")
        self.assertEqual(len(detail.media_candidates), 1)
        self.assertEqual(urlopen.call_count, 2)

    def test_build_detail_without_caption_is_pending_when_media_exists(self):
        crawler = LiveCrawler.__new__(LiveCrawler)
        item = ContentItem("https://dedao.cn/replay/2", "得到直播", "无字幕直播", "https://dedao.cn/replay/2", dedao_id="2")
        detail = crawler._build_detail(item, {"sd": ["https://cdn.example/video.m3u8"], "playback_status": "1"})
        self.assertFalse(detail.has_transcript)
        self.assertEqual(detail.quality_reason, "caption_pending")


if __name__ == "__main__":
    unittest.main()
