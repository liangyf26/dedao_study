from __future__ import annotations

from dataclasses import dataclass
from pathlib import PurePosixPath
from urllib.parse import urlparse

from .models import MediaCandidate


BLOCKED_HTML_MARKERS = {
    "widevine": "drm_widevine",
    "fairplay": "drm_fairplay",
    "playready": "drm_playready",
    "encrypted-media": "encrypted_media_api",
    "eme-encryption-scheme": "encrypted_media_api",
    "#ext-x-key": "encrypted_hls_key",
    "com.widevine.alpha": "drm_widevine",
    "com.microsoft.playready": "drm_playready",
    "com.apple.fps": "drm_fairplay",
}

BLOCKED_MEDIA_MIME_TYPES = {
    "application/dash+xml": "dash_manifest",
}

BLOCKED_MEDIA_EXTENSIONS = {
    ".mpd": "dash_manifest",
}


@dataclass(frozen=True)
class PolicyDecision:
    allowed: bool
    reason: str | None = None


def check_page_policy(html: str, media_candidates: tuple[MediaCandidate, ...] = ()) -> PolicyDecision:
    reason = blocked_html_reason(html)
    if reason:
        return PolicyDecision(False, reason)
    for candidate in media_candidates:
        reason = blocked_media_reason(candidate)
        if reason:
            return PolicyDecision(False, reason)
    return PolicyDecision(True)


def blocked_html_reason(html: str) -> str | None:
    lowered = html.lower()
    for marker, reason in BLOCKED_HTML_MARKERS.items():
        if marker in lowered:
            return reason
    return None


def blocked_media_reason(candidate: MediaCandidate) -> str | None:
    mime_type = (candidate.mime_type or "").split(";", maxsplit=1)[0].strip().lower()
    if mime_type in BLOCKED_MEDIA_MIME_TYPES:
        return BLOCKED_MEDIA_MIME_TYPES[mime_type]
    path = PurePosixPath(urlparse(candidate.url).path)
    return BLOCKED_MEDIA_EXTENSIONS.get(path.suffix.lower())
