from __future__ import annotations

import json
import logging
import os
import shutil
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


class DisabledTranscriptionService(TranscriptionService):
    def transcribe(self, media_path: Path) -> str:
        raise TranscriptionError("Transcription is disabled")


class VolcengineDirectTranscriptionService(TranscriptionService):
    """Fail-closed direct audio adapter until the official API contract is verified."""

    def __init__(self, config: TranscriptionConfig):
        self.config = config

    def transcribe(self, media_path: Path) -> str:
        if not self.config.free_tier_confirmed:
            raise TranscriptionError("free ASR entitlement is not explicitly confirmed")
        if not os.environ.get(self.config.api_key_env):
            raise TranscriptionError(f"ASR API key env is missing: {self.config.api_key_env}")
        endpoint = os.environ.get(self.config.endpoint_env, "").strip()
        if not endpoint:
            raise TranscriptionError(f"ASR endpoint env is missing: {self.config.endpoint_env}")
        if not is_http_url(endpoint):
            raise TranscriptionError("ASR endpoint must be an http(s) URL")
        if not media_path.is_file():
            raise TranscriptionError("transcription audio file does not exist")
        raise TranscriptionError(
            "Volcengine ASR request protocol is not enabled until the official direct-upload contract is verified"
        )


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

    segments: list[Path] = []
    count = (int(duration) + AUDIO_SEGMENT_SECONDS - 1) // AUDIO_SEGMENT_SECONDS
    if count > config.max_segments:
        shutil.rmtree(work_dir, ignore_errors=True)
        raise TranscriptionError(f"media requires {count} segments; configured limit is {config.max_segments}")
    try:
        for index in range(count):
            start = index * AUDIO_SEGMENT_SECONDS
            segment_duration = min(AUDIO_SEGMENT_SECONDS, duration - start)
            target = work_dir / f"audio-{index + 1:03d}.mp3"
            subprocess.run(
                [
                    ffmpeg_path,
                    "-nostdin",
                    "-v", "error",
                    "-protocol_whitelist", "http,https,tcp,tls",
                    "-ss", str(start),
                    "-i", candidate.url,
                    "-t", str(segment_duration),
                    "-vn",
                    "-ac", str(AUDIO_CHANNELS),
                    "-ar", str(AUDIO_SAMPLE_RATE),
                    "-b:a", "48k",
                    "-y", str(target),
                ],
                check=True,
                capture_output=True,
                timeout=config.request_timeout_seconds,
            )
            if not target.is_file() or target.stat().st_size == 0:
                raise TranscriptionError("ffmpeg produced an empty audio segment")
            total_audio_bytes = sum(segment.stat().st_size for segment in segments) + target.stat().st_size
            if total_audio_bytes > config.max_audio_bytes:
                raise TranscriptionError("audio segments exceed configured total size limit")
            segments.append(target)
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
    return TranscriptionResult(text=transcript, provider=config.provider)


def create_transcription_service(config: TranscriptionConfig) -> TranscriptionService:
    if not config.enabled:
        return DisabledTranscriptionService()
    if config.provider == "volcengine":
        return VolcengineDirectTranscriptionService(config)
    raise TranscriptionError(f"unsupported transcription provider: {config.provider}")


def cleanup_audio_segments(segments: list[Path]) -> None:
    for parent in {segment.parent for segment in segments}:
        shutil.rmtree(parent, ignore_errors=True)
