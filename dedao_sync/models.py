from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any


ItemStatus = str


STATUS_DISCOVERED = "discovered"
STATUS_SYNCED = "synced"
STATUS_MISSING_TRANSCRIPT = "missing_transcript"
STATUS_SUMMARY_FAILED = "summary_failed"
STATUS_TRANSCRIPTION_FAILED = "transcription_failed"
STATUS_POLICY_BLOCKED = "policy_blocked"
STATUS_EXTRACTOR_FAILED = "extractor_failed"
STATUS_PREFLIGHT_FAILED = "preflight_failed"
STATUS_LOGIN_REQUIRED = "login_required"
STATUS_LOCKED = "locked"
STATUS_FAILED = "failed"
STATUS_SKIPPED = "skipped"


@dataclass(frozen=True)
class ColumnConfig:
    name: str
    url: str
    enabled: bool = True


@dataclass(frozen=True)
class ObsidianConfig:
    vault_path: Path
    output_dir: str
    filename_pattern: str


@dataclass(frozen=True)
class DedaoConfig:
    auth_state_path: Path
    browser_profile_dir: Path
    headless: bool
    request_interval_seconds: float
    save_failure_html: bool
    failure_snapshot_dir: Path
    columns: tuple[ColumnConfig, ...]


@dataclass(frozen=True)
class SummaryConfig:
    enabled: bool
    provider: str
    model: str
    base_url_env: str
    api_key_env: str


@dataclass(frozen=True)
class TranscriptionConfig:
    enabled: bool
    provider: str
    delete_media_after_transcription: bool
    temp_dir: Path


@dataclass(frozen=True)
class FeishuConfig:
    enabled: bool
    webhook_url_env: str
    secret_env: str
    include_titles: bool = True


@dataclass(frozen=True)
class AppConfig:
    obsidian: ObsidianConfig
    dedao: DedaoConfig
    summary: SummaryConfig
    transcription: TranscriptionConfig
    feishu: FeishuConfig
    root_dir: Path

    @property
    def output_root(self) -> Path:
        return self.obsidian.vault_path / self.obsidian.output_dir


@dataclass(frozen=True)
class ContentItem:
    source_url: str
    column_name: str
    title: str
    detail_url: str
    dedao_id: str | None = None
    published_at: str | None = None
    author: str | None = None
    content_type: str = "unknown"


@dataclass(frozen=True)
class MediaCandidate:
    url: str
    mime_type: str | None = None
    label: str | None = None


@dataclass(frozen=True)
class ContentDetail:
    item: ContentItem
    transcript_text: str
    has_transcript: bool
    media_candidates: tuple[MediaCandidate, ...] = ()
    raw_html_hash: str | None = None
    quality_reason: str | None = None
    diagnostic_path: Path | None = None
    extracted_at: datetime | None = None


@dataclass(frozen=True)
class SummaryResult:
    atomic_cards: tuple[str, ...]
    permanent_note: str
    links: tuple[str, ...] = ()
    actions: tuple[str, ...] = ()
    questions: tuple[str, ...] = ()
    keywords: tuple[str, ...] = ()
    status: str = "ok"
    error_message: str | None = None

    @classmethod
    def empty(cls, status: str = "disabled") -> "SummaryResult":
        return cls(atomic_cards=(), permanent_note="", status=status)


@dataclass
class RunReport:
    started_at: datetime
    finished_at: datetime | None = None
    status: str = "running"
    total_columns: int = 0
    discovered_count: int = 0
    new_count: int = 0
    skipped_count: int = 0
    success_count: int = 0
    request_count: int = 0
    failed_count: int = 0
    missing_transcript_count: int = 0
    summary_failed_count: int = 0
    log_path: Path | None = None
    added_by_column: dict[str, list[str]] = field(default_factory=dict)
    missing_by_column: dict[str, list[str]] = field(default_factory=dict)
    summary_failed_by_column: dict[str, list[str]] = field(default_factory=dict)
    failures: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
