from __future__ import annotations

import os
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

from dedao_sync.models import MediaCandidate, TranscriptionConfig
from dedao_sync.transcriber import (
    TranscriptionError,
    VolcengineDirectTranscriptionService,
    cleanup_audio_segments,
    ensure_unencrypted_hls,
    parse_hls_duration,
    transcribe_segments,
)


class StubTranscriber:
    def __init__(self, values):
        self.values = iter(values)

    def transcribe(self, media_path: Path) -> str:
        return next(self.values)


class TranscriberTests(unittest.TestCase):
    def test_hls_policy_rejects_encrypted_manifest(self):
        candidate = MediaCandidate("https://cdn.example/live.m3u8", "application/x-mpegURL", "m3u8")
        with self.assertRaisesRegex(TranscriptionError, "encrypted"):
            ensure_unencrypted_hls(candidate, "#EXTM3U\n#EXT-X-KEY:METHOD=AES-128\n")

    def test_hls_duration_sums_segments(self):
        self.assertEqual(parse_hls_duration("#EXTM3U\n#EXTINF:10.5,\na.ts\n#EXTINF:9.5,\nb.ts"), 20.0)

    def test_hls_duration_requires_segments(self):
        with self.assertRaisesRegex(TranscriptionError, "no usable"):
            parse_hls_duration("#EXTM3U\n#EXT-X-ENDLIST")

    def test_segments_are_transcribed_in_order(self):
        with tempfile.TemporaryDirectory() as tmp:
            first = Path(tmp) / "1.mp3"
            second = Path(tmp) / "2.mp3"
            first.touch()
            second.touch()
            text = transcribe_segments(StubTranscriber(["第一段", "第二段"]), [first, second])
            self.assertEqual(text, "第一段\n\n第二段")

    def test_empty_asr_result_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            segment = Path(tmp) / "part.mp3"
            segment.touch()
            with self.assertRaisesRegex(TranscriptionError, "empty transcript"):
                transcribe_segments(StubTranscriber([""]), [segment])

    def test_cleanup_removes_each_work_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp) / "job"
            work.mkdir()
            segment = work / "part.mp3"
            segment.touch()
            cleanup_audio_segments([segment])
            self.assertFalse(work.exists())

    def test_volcengine_adapter_does_not_call_without_free_entitlement(self):
        with tempfile.TemporaryDirectory() as tmp:
            audio = Path(tmp) / "part.mp3"
            audio.touch()
            config = TranscriptionConfig(True, "volcengine", True, Path(tmp))
            service = VolcengineDirectTranscriptionService(config)
            with mock.patch.dict(os.environ, {config.api_key_env: "test", config.endpoint_env: "https://asr.example/api"}):
                with self.assertRaisesRegex(TranscriptionError, "free ASR entitlement"):
                    service.transcribe(audio)

    def test_volcengine_adapter_fails_closed_until_protocol_is_verified(self):
        with tempfile.TemporaryDirectory() as tmp:
            audio = Path(tmp) / "part.mp3"
            audio.touch()
            config = TranscriptionConfig(True, "volcengine", True, Path(tmp), free_tier_confirmed=True)
            service = VolcengineDirectTranscriptionService(config)
            with mock.patch.dict(os.environ, {config.api_key_env: "test", config.endpoint_env: "https://asr.example/api"}):
                with mock.patch("urllib.request.urlopen") as urlopen:
                    with self.assertRaisesRegex(TranscriptionError, "protocol is not enabled"):
                        service.transcribe(audio)
            urlopen.assert_not_called()


if __name__ == "__main__":
    unittest.main()
