from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from string import Formatter
from urllib.parse import urlparse

from .browser import check_playwright_chromium, validate_storage_state_file
from .models import AppConfig


@dataclass
class PreflightResult:
    ok: bool
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def add_error(self, message: str) -> None:
        self.errors.append(message)
        self.ok = False

    def add_warning(self, message: str) -> None:
        self.warnings.append(message)

    def extend(self, other: "PreflightResult") -> None:
        self.errors.extend(other.errors)
        self.warnings.extend(other.warnings)
        if not other.ok:
            self.ok = False


def check_config_semantics(config: AppConfig) -> PreflightResult:
    result = PreflightResult(ok=True)
    root_dir = config.root_dir.resolve(strict=False)
    output_dir = config.obsidian.output_dir.strip()
    if not output_dir:
        result.add_error("obsidian.output_dir must not be empty")
    else:
        output_path = Path(output_dir)
        if output_path.is_absolute():
            result.add_error("obsidian.output_dir must be relative to obsidian.vault_path")
        try:
            vault_root = config.obsidian.vault_path.resolve(strict=False)
            output_root = (config.obsidian.vault_path / output_path).resolve(strict=False)
        except OSError as exc:
            result.add_error(f"Invalid obsidian.output_dir: {exc}")
        else:
            if not output_root.is_relative_to(vault_root):
                result.add_error("obsidian.output_dir must stay inside obsidian.vault_path")

    _check_project_sensitive_path(
        result,
        config.dedao.auth_state_path,
        root_dir=root_dir,
        allowed_root=root_dir / "data" / "auth",
        field_name="dedao.auth_state_path",
    )
    _check_project_sensitive_path(
        result,
        config.dedao.browser_profile_dir,
        root_dir=root_dir,
        allowed_root=root_dir / "data" / "browser_profile",
        field_name="dedao.browser_profile_dir",
    )
    _check_project_sensitive_path(
        result,
        config.dedao.failure_snapshot_dir,
        root_dir=root_dir,
        allowed_root=root_dir / "data" / "page_failures",
        field_name="dedao.failure_snapshot_dir",
    )
    _check_project_sensitive_path(
        result,
        config.transcription.temp_dir,
        root_dir=root_dir,
        allowed_root=root_dir / "data" / "media_cache",
        field_name="transcription.temp_dir",
    )

    enabled_columns = [column for column in config.dedao.columns if column.enabled]
    if not enabled_columns:
        result.add_error("No enabled Dedao columns configured")

    seen_names: set[str] = set()
    duplicate_names: set[str] = set()
    for column in config.dedao.columns:
        if column.name in seen_names:
            duplicate_names.add(column.name)
        seen_names.add(column.name)
        parsed = urlparse(column.url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            result.add_error(f"Invalid Dedao column URL for {column.name}: {column.url}")
    for name in sorted(duplicate_names):
        result.add_error(f"Duplicate Dedao column name: {name}")

    if config.dedao.request_interval_seconds < 0:
        result.add_error("dedao.request_interval_seconds must be >= 0")

    if config.summary.enabled and config.summary.provider != "opencode_go":
        result.add_error(f"Unsupported summary provider: {config.summary.provider}")

    required_fields = {"column", "published_date", "title"}
    try:
        fields = {field_name for _, field_name, _, _ in Formatter().parse(config.obsidian.filename_pattern) if field_name}
    except ValueError as exc:
        result.add_error(f"Invalid filename_pattern: {exc}")
        return result
    missing_fields = required_fields - fields
    if missing_fields:
        result.add_error(f"filename_pattern missing fields: {', '.join(sorted(missing_fields))}")
    unsupported_fields = fields - required_fields
    if unsupported_fields:
        result.add_error(f"filename_pattern unsupported fields: {', '.join(sorted(unsupported_fields))}")
    return result


def is_http_url(value: str) -> bool:
    parsed = urlparse(value.strip())
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def _check_project_sensitive_path(
    result: PreflightResult,
    path: Path,
    *,
    root_dir: Path,
    allowed_root: Path,
    field_name: str,
) -> None:
    resolved = path.resolve(strict=False)
    if not resolved.is_relative_to(root_dir):
        return
    if not resolved.is_relative_to(allowed_root.resolve(strict=False)):
        result.add_error(f"{field_name} inside project must stay under {allowed_root.relative_to(root_dir)}")


class PreflightChecker:
    def __init__(
        self,
        config: AppConfig,
        *,
        require_auth: bool = True,
        require_feishu: bool = False,
        require_browser: bool = False,
        probe_vault_write: bool = False,
    ):
        self.config = config
        self.require_auth = require_auth
        self.require_feishu = require_feishu
        self.require_browser = require_browser
        self.probe_vault_write = probe_vault_write

    def check(self) -> PreflightResult:
        result = PreflightResult(ok=True)
        result.extend(check_config_semantics(self.config))
        if not self.config.obsidian.vault_path.exists():
            result.add_error(f"Obsidian vault path does not exist: {self.config.obsidian.vault_path}")
        elif not self.config.obsidian.vault_path.is_dir():
            result.add_error(f"Obsidian vault path is not a directory: {self.config.obsidian.vault_path}")
        elif self.probe_vault_write:
            self._check_output_writable(result)

        self.config.root_dir.joinpath("data").mkdir(exist_ok=True)
        self.config.root_dir.joinpath("logs").mkdir(exist_ok=True)
        self.config.dedao.auth_state_path.parent.mkdir(parents=True, exist_ok=True)
        if self.config.dedao.save_failure_html:
            self._check_failure_snapshot_dir(result)

        if self.require_auth:
            ok, message = validate_storage_state_file(self.config.dedao.auth_state_path)
            if not ok:
                result.add_error(f"Dedao auth state invalid, run login first: {message}")

        if self.require_browser:
            browser_ok, browser_message = check_playwright_chromium()
            if not browser_ok:
                result.add_error(f"{browser_message}; run: pip install -e .[dev] && playwright install chromium")

        if self.config.summary.enabled:
            if not os.environ.get(self.config.summary.api_key_env):
                result.add_warning(f"Summary API key env is missing: {self.config.summary.api_key_env}")
            base_url = os.environ.get(self.config.summary.base_url_env)
            if not base_url:
                result.add_warning(f"Summary base URL env is missing: {self.config.summary.base_url_env}")
            elif not is_http_url(base_url):
                result.add_warning(f"Summary base URL env is not a valid http(s) URL: {self.config.summary.base_url_env}")

        if self.config.feishu.enabled:
            webhook = os.environ.get(self.config.feishu.webhook_url_env)
            if not webhook:
                message = f"Feishu webhook env is missing: {self.config.feishu.webhook_url_env}"
                if self.require_feishu:
                    result.add_error(message)
                else:
                    result.add_warning(message)
            elif not is_http_url(webhook):
                message = f"Feishu webhook env is not a valid http(s) URL: {self.config.feishu.webhook_url_env}"
                if self.require_feishu:
                    result.add_error(message)
                else:
                    result.add_warning(message)

        if self.config.transcription.enabled:
            result.add_error("Transcription is not implemented in the current build; set transcription.enabled=false")
        return result

    def _check_output_writable(self, result: PreflightResult) -> None:
        output_root = self.config.output_root
        temp_path: Path | None = None
        try:
            output_root.mkdir(parents=True, exist_ok=True)
            fd, temp_name = tempfile.mkstemp(prefix=".dedao-sync-preflight.", suffix=".tmp", dir=str(output_root))
            temp_path = Path(temp_name)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write("ok")
                handle.flush()
        except OSError as exc:
            result.add_error(f"Obsidian output path is not writable: {output_root} ({exc})")
        finally:
            if temp_path:
                temp_path.unlink(missing_ok=True)

    def _check_failure_snapshot_dir(self, result: PreflightResult) -> None:
        snapshot_dir = self.config.dedao.failure_snapshot_dir
        try:
            snapshot_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            result.add_error(f"Failure HTML snapshot directory is not writable: {snapshot_dir} ({exc})")
            return
        if not snapshot_dir.is_dir():
            result.add_error(f"Failure HTML snapshot path is not a directory: {snapshot_dir}")
