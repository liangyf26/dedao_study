from __future__ import annotations

import os
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

from dedao_sync.models import MediaCandidate, TranscriptionConfig
from dedao_sync.transcriber import (
    S3AITranscriptionService,
    TranscriptionError,
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

    def test_s3ai_adapter_blocks_unconfirmed_costs(self):
        with tempfile.TemporaryDirectory() as tmp:
            audio = Path(tmp) / "part.mp3"
            audio.touch()
            config = TranscriptionConfig(True, "s3ai", True, Path(tmp))
            service = S3AITranscriptionService(config)
            with mock.patch.dict(os.environ, {config.api_key_env: "test", config.endpoint_env: "https://asr.example/api"}):
                with self.assertRaisesRegex(TranscriptionError, "cost entitlement"):
                    service.transcribe(audio)

    def test_s3ai_adapter_fails_over_response_schemas(self):
        with tempfile.TemporaryDirectory() as tmp:
            audio = Path(tmp) / "part.mp3"
            audio.write_bytes(b"audio")
            config = TranscriptionConfig(True, "s3ai", True, Path(tmp), free_tier_confirmed=True)
            service = S3AITranscriptionService(config)
            responses = [
                {"result": {"text": "第一段"}},
                {"text": "第二段"},
            ]
            def fake_request(base, key, model, path):
                return responses.pop(0)["result"]["text"] if model.startswith("whisper") else responses.pop(0)["text"]
            with mock.patch.dict(os.environ, {config.api_key_env: "test", config.endpoint_env: "https://asr.example/api"}):
                with mock.patch.object(service, "_request", side_effect=fake_request):
                    self.assertEqual(service.transcribe(audio), "第一段")
                    self.assertEqual(service.selected_model, config.models[0])

    def test_s3ai_request_uses_multipart_and_parses_whisper_envelope(self):
        with tempfile.TemporaryDirectory() as tmp:
            audio = Path(tmp) / "part.wav"
            audio.write_bytes(b"RIFF-audio")
            config = TranscriptionConfig(True, "s3ai", True, Path(tmp), free_tier_confirmed=True)
            service = S3AITranscriptionService(config)
            captured = {}

            class Response:
                status = 200
                def __enter__(self): return self
                def __exit__(self, *args): return False
                def read(self, limit):
                    return '{"result":{"text":"中文结果"}}'.encode("utf-8")

            def fake_urlopen(request, timeout):
                captured["url"] = request.full_url
                captured["content_type"] = request.get_header("Content-type")
                captured["auth"] = request.get_header("Authorization")
                captured["body"] = request.data
                captured["timeout"] = timeout
                return Response()

            with mock.patch.dict(os.environ, {config.api_key_env: "secret", config.endpoint_env: "https://s3ai.example/v1"}):
                with mock.patch("urllib.request.urlopen", side_effect=fake_urlopen):
                    self.assertEqual(service.transcribe(audio), "中文结果")
            self.assertEqual(captured["url"], "https://s3ai.example/v1/audio/transcriptions")
            self.assertIn("multipart/form-data", captured["content_type"])
            self.assertIn(b"whisper-large-v3-turbo", captured["body"])
            self.assertIn(b"language", captured["body"])
            self.assertNotIn(b"secret", captured["body"])

        from dedao_sync.transcriber import clean_transcript, extract_s3ai_text
        self.assertEqual(extract_s3ai_text({"result": {"text": "结果"}}), "结果")
        self.assertEqual(extract_s3ai_text({"text": "顶层"}), "顶层")
        self.assertEqual(clean_transcript("你好<|noise|> 😡\n世界"), "你好 世界")


if __name__ == "__main__":
    unittest.main()
