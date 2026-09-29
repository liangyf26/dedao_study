from __future__ import annotations

import json
import logging
import mimetypes
import os
import re
import shutil
import socket
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

from .models import MediaCandidate, TranscriptionConfig
from .policy import blocked_html_reason, blocked_media_reason
from .security import redact

LOGGER = logging.getLogger(__name__)

AUDIO_SAMPLE_RATE = 16000
AUDIO_CHANNELS = 1
AUDIO_SEGMENT_SECONDS = 300


class TranscriptionError(RuntimeError):
    pass


@dataclass(frozen=True)
class TranscriptionResult:
    text: str
    provider: str


class TranscriptionService:
    def transcribe(self, media_path: Path) -> str:
        raise NotImplementedError


class _TransientASRError(TranscriptionError):
    pass


class _FatalASRError(TranscriptionError):
    pass


class S3AITranscriptionService(TranscriptionService):
    def __init__(self, config: TranscriptionConfig):
        self.config = config
        self.selected_model: str | None = None

    @property
    def provider(self) -> str:
        return f"s3ai/{self.selected_model}" if self.selected_model else "s3ai"

    def transcribe(self, media_path: Path) -> str:
        if not media_path.is_file():
            raise TranscriptionError("transcription audio file does not exist")
        if not self.config.free_tier_confirmed:
            raise TranscriptionError("ASR cost entitlement is not explicitly confirmed")
        api_key = os.environ.get(self.config.api_key_env, "").strip()
        base_url = os.environ.get(self.config.endpoint_env, "").strip().rstrip("/")
        if not api_key:
            raise TranscriptionError(f"ASR API key env is missing: {self.config.api_key_env}")
        if not is_http_url(base_url):
            raise TranscriptionError("S3AI base URL must be an http(s) URL")
        if not self.config.models:
            raise TranscriptionError("S3AI model list is empty")

        last_error: Exception | None = None
        for model in self.config.models:
            for attempt in range(self.config.model_retries + 1):
                try:
                    text = self._request(base_url, api_key, model, media_path)
                    self.selected_model = model
                    return clean_transcript(text)
                except _FatalASRError:
                    raise
                except (_TransientASRError, TranscriptionError) as exc:
                    last_error = exc
                    if isinstance(exc, TranscriptionError) and not isinstance(exc, _TransientASRError):
                        raise
                    if attempt < self.config.model_retries:
                        time.sleep(min(2 ** attempt, 8))
                        continue
                    LOGGER.warning("S3AI ASR model failed model=%s error=%s", model, redact(exc))
                    break
        raise TranscriptionError(f"all S3AI ASR models failed: {redact(last_error)}")

    def _request(self, base_url: str, api_key: str, model: str, media_path: Path) -> str:
        boundary = f"----dedao-s3ai-{os.urandom(8).hex()}"
        audio = media_path.read_bytes()
        body = b"".join(
            [
                _multipart_field(boundary, "model", model),
                _multipart_field(boundary, "language", "zh"),
                _multipart_file(boundary, "file", media_path.name, audio),
                f"--{boundary}--\r\n".encode("utf-8"),
            ]
        )
        request = urllib.request.Request(
            f"{base_url}/audio/transcriptions",
            data=body,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": f"multipart/form-data; boundary={boundary}",
                "Accept": "application/json",
                "Connection": "close",
                "User-Agent": "dedao-sync/0.1",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.config.request_timeout_seconds) as response:
                status = int(response.status)
                raw = response.read(2_000_000).decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            raw = exc.read(2000).decode("utf-8", errors="replace")
            if exc.code == 429 or exc.code >= 500:
                raise _TransientASRError(f"S3AI ASR HTTP {exc.code}") from exc
            raise _FatalASRError(f"S3AI ASR HTTP {exc.code}: {redact(raw)[:300]}") from exc
        except (OSError, urllib.error.URLError, TimeoutError) as exc:
            raise _TransientASRError(f"S3AI ASR transport error: {type(exc).__name__}") from exc
        if status >= 500 or status == 429:
            raise _TransientASRError(f"S3AI ASR HTTP {status}")
        if status >= 400:
            raise _FatalASRError(f"S3AI ASR HTTP {status}: {redact(raw)[:300]}")
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise _TransientASRError("S3AI ASR returned invalid JSON") from exc
        text = extract_s3ai_text(payload)
        if not text:
            raise _TransientASRError("S3AI ASR returned empty text")
        return text


def _multipart_field(boundary: str, name: str, value: str) -> bytes:
    return (
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"\r\n\r\n{value}\r\n"
    ).encode("utf-8")


def _multipart_file(boundary: str, name: str, filename: str, data: bytes) -> bytes:
    content_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"
    header = (
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"; filename=\"{filename}\"\r\n"
        f"Content-Type: {content_type}\r\n\r\n"
    ).encode("utf-8")
    return header + data + b"\r\n"


def extract_s3ai_text(payload: object) -> str:
    if not isinstance(payload, dict):
        return ""
    result = payload.get("result")
    if isinstance(result, dict) and isinstance(result.get("text"), str):
        return result["text"].strip()
    if isinstance(payload.get("text"), str):
        return payload["text"].strip()
    return ""


def clean_transcript(text: str) -> str:
    text = re.sub(r"<\|[^|\n]+\|>", "", text)
    text = re.sub(r"[\x00-\x1f\x7f]", " ", text)
    text = re.sub(r"[\U0001F300-\U0001FAFF\u2600-\u27BF]", "", text)
    return re.sub(r"\s+", " ", text).strip()


class DisabledTranscriptionService(TranscriptionService):
    def transcribe(self, media_path: Path) -> str:
        raise TranscriptionError("Transcription is disabled")


def is_http_url(value: str) -> bool:
    parsed = urlparse(value)
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def ensure_unencrypted_hls(candidate: MediaCandidate, manifest_text: str) -> None:
    reason = blocked_media_reason(candidate) or blocked_html_reason(manifest_text)
    if reason:
        raise TranscriptionError(f"media is blocked by policy: {reason}")
    if not is_http_url(candidate.url):
        raise TranscriptionError("media URL must use http(s)")
    if "#EXTM3U" not in manifest_text:
        raise TranscriptionError("media candidate is not an HLS manifest")
    if "#EXT-X-KEY" in manifest_text.upper():
        raise TranscriptionError("encrypted HLS is not supported")


def parse_hls_duration(manifest_text: str) -> float:
    total = 0.0
    for line in manifest_text.splitlines():
        line = line.strip()
        if line.startswith("#EXTINF:"):
            value = line.partition(":")[2].partition(",")[0]
            try:
                total += float(value)
            except ValueError as exc:
                raise TranscriptionError("HLS manifest contains an invalid segment duration") from exc
    if total <= 0:
        raise TranscriptionError("HLS manifest has no usable segment durations")
    return total


def probe_duration(media_path: Path, *, ffprobe: str = "ffprobe", timeout_seconds: int = 20) -> float:
    executable = shutil.which(ffprobe)
    if not executable:
        raise TranscriptionError("ffprobe is required for bounded media processing")
    try:
        result = subprocess.run(
            [executable, "-v", "error", "-show_entries", "format=duration", "-of", "json", str(media_path)],
            check=True,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
        )
        duration = float(json.loads(result.stdout)["format"]["duration"])
    except (OSError, subprocess.SubprocessError, ValueError, KeyError, json.JSONDecodeError) as exc:
        raise TranscriptionError(f"unable to probe media duration: {redact(exc)}") from exc
    if duration <= 0:
        raise TranscriptionError("media duration is invalid")
    return duration


def extract_audio_segments(
    candidate: MediaCandidate,
    config: TranscriptionConfig,
    *,
    free_space_bytes: int | None = None,
    manifest_text: str | None = None,
    ffmpeg: str = "ffmpeg",
    ffprobe: str = "ffprobe",
) -> list[Path]:
    if not config.enabled:
        raise TranscriptionError("transcription is disabled")
    if not config.free_tier_confirmed:
        raise TranscriptionError("free ASR entitlement is not explicitly confirmed")
    if free_space_bytes is None:
        free_space_bytes = shutil.disk_usage(config.temp_dir.parent).free
    if free_space_bytes < config.min_free_disk_bytes:
        raise TranscriptionError("insufficient free disk space for media processing")
    if manifest_text is not None:
        ensure_unencrypted_hls(candidate, manifest_text)
    elif not is_http_url(candidate.url) or blocked_media_reason(candidate):
        raise TranscriptionError("media candidate is invalid or blocked by policy")

    ffmpeg_path = shutil.which(ffmpeg)
    if not ffmpeg_path:
        raise TranscriptionError("ffmpeg is required for media processing")
    config.temp_dir.mkdir(parents=True, exist_ok=True)
    work_dir = Path(tempfile.mkdtemp(prefix="dedao-asr-", dir=config.temp_dir))
    if manifest_text is None:
        try:
            request = urllib.request.Request(candidate.url, headers={"User-Agent": "dedao-sync/0.1"})
            with urllib.request.urlopen(request, timeout=config.request_timeout_seconds) as response:
                manifest_text = response.read(2_000_001).decode("utf-8", errors="replace")
        except (OSError, urllib.error.URLError) as exc:
            shutil.rmtree(work_dir, ignore_errors=True)
            raise TranscriptionError(f"unable to read HLS manifest: {redact(exc)}") from exc
    try:
        ensure_unencrypted_hls(candidate, manifest_text)
        duration = parse_hls_duration(manifest_text)
        if duration > config.max_duration_seconds:
            raise TranscriptionError(
                f"media duration {int(duration)}s exceeds configured limit {config.max_duration_seconds}s"
            )
    except Exception:
        shutil.rmtree(work_dir, ignore_errors=True)
        raise

    count = (int(duration) + AUDIO_SEGMENT_SECONDS - 1) // AUDIO_SEGMENT_SECONDS
    if count > config.max_segments:
        shutil.rmtree(work_dir, ignore_errors=True)
        raise TranscriptionError(f"media requires {count} segments; configured limit is {config.max_segments}")
    output_pattern = work_dir / "audio-%03d.mp3"
    try:
        subprocess.run(
            [
                ffmpeg_path,
                "-nostdin",
                "-v", "error",
                "-protocol_whitelist", "http,https,tcp,tls",
                "-i", candidate.url,
                "-vn",
                "-ac", str(AUDIO_CHANNELS),
                "-ar", str(AUDIO_SAMPLE_RATE),
                "-b:a", "48k",
                "-f", "segment",
                "-segment_time", str(AUDIO_SEGMENT_SECONDS),
                "-segment_format", "mp3",
                "-reset_timestamps", "1",
                "-y", str(output_pattern),
            ],
            check=True,
            capture_output=True,
            timeout=max(config.request_timeout_seconds, int(duration) + 60),
        )
        segments = sorted(work_dir.glob("audio-*.mp3"))
        if not segments:
            raise TranscriptionError("ffmpeg produced no audio segments")
        if len(segments) > config.max_segments:
            raise TranscriptionError(f"ffmpeg produced {len(segments)} segments; configured limit is {config.max_segments}")
        total_audio_bytes = 0
        for segment in segments:
            size = segment.stat().st_size
            if size <= 0:
                raise TranscriptionError("ffmpeg produced an empty audio segment")
            total_audio_bytes += size
        if total_audio_bytes > config.max_audio_bytes:
            raise TranscriptionError("audio segments exceed configured total size limit")
        return segments
    except Exception as exc:
        shutil.rmtree(work_dir, ignore_errors=True)
        if isinstance(exc, TranscriptionError):
            raise
        if isinstance(exc, (OSError, subprocess.SubprocessError)):
            raise TranscriptionError(f"audio extraction failed: {redact(exc)}") from exc
        raise


def transcribe_segments(service: TranscriptionService, segments: list[Path]) -> str:
    if not segments:
        raise TranscriptionError("no audio segments to transcribe")
    texts: list[str] = []
    for segment in segments:
        text = service.transcribe(segment).strip()
        if not text:
            raise TranscriptionError("ASR returned an empty transcript segment")
        texts.append(text)
    return "\n\n".join(texts)


def transcribe_detail_media(
    candidates: tuple[MediaCandidate, ...],
    config: TranscriptionConfig,
    *,
    service: TranscriptionService | None = None,
) -> TranscriptionResult:
    if not config.enabled:
        raise TranscriptionError("transcription is disabled")
    if not config.free_tier_confirmed:
        raise TranscriptionError("free ASR entitlement is not explicitly confirmed")
    candidate = next(
        (item for item in candidates if item.label == "m3u8" or "mpegurl" in (item.mime_type or "").lower()),
        None,
    )
    if candidate is None:
        raise TranscriptionError("no supported HLS media candidate is available")
    service = service or create_transcription_service(config)
    segments = extract_audio_segments(candidate, config)
    try:
        transcript = transcribe_segments(service, segments)
    finally:
        cleanup_audio_segments(segments)
    provider = getattr(service, "provider", config.provider)
    return TranscriptionResult(text=transcript, provider=provider)


def create_transcription_service(config: TranscriptionConfig) -> TranscriptionService:
    if not config.enabled:
        return DisabledTranscriptionService()
    if config.provider == "s3ai":
        return S3AITranscriptionService(config)
    raise TranscriptionError(f"unsupported transcription provider: {config.provider}; use s3ai")


def cleanup_audio_segments(segments: list[Path]) -> None:
    for parent in {segment.parent for segment in segments}:
        shutil.rmtree(parent, ignore_errors=True)
