from __future__ import annotations

from pathlib import Path


class TranscriptionService:
    def transcribe(self, media_path: Path) -> str:
        raise NotImplementedError


class DisabledTranscriptionService(TranscriptionService):
    def transcribe(self, media_path: Path) -> str:
        raise RuntimeError("Transcription is disabled in Phase 1")

