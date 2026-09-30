from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from .models import (
    AppConfig,
    ColumnConfig,
    DedaoConfig,
    FeishuConfig,
    ObsidianConfig,
    SummaryConfig,
    TranscriptionConfig,
)


class ConfigError(ValueError):
    pass


def load_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value
        values[key] = value
    return values


def _parse_scalar(value: str) -> Any:
    value = value.strip()
    if value in {"true", "True"}:
        return True
    if value in {"false", "False"}:
        return False
    if value in {"null", "None", "~"}:
        return None
    if (value.startswith('"') and value.endswith('"')) or (value.startswith("'") and value.endswith("'")):
        return value[1:-1]
    try:
        if "." in value:
            return float(value)
        return int(value)
    except ValueError:
        return value


def _bool(value: Any, field_name: str) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"true", "yes", "1", "on"}:
            return True
        if normalized in {"false", "no", "0", "off", ""}:
            return False
    if isinstance(value, int) and value in {0, 1}:
        return bool(value)
    raise ConfigError(f"Invalid boolean value for {field_name}: {value!r}")


def _load_yaml_limited(path: Path) -> dict[str, Any]:
    try:
        import yaml  # type: ignore
    except ImportError:
        yaml = None
    if yaml is not None:
        loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
        if not isinstance(loaded, dict):
            raise ConfigError(f"Config root must be a mapping: {path}")
        return loaded

    # Tiny YAML subset parser for config.example.yaml. Install PyYAML for full YAML support.
    root: dict[str, Any] = {}
    stack: list[tuple[int, Any]] = [(-1, root)]
    pending_key: tuple[int, dict[str, Any], str] | None = None
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        if not raw_line.strip() or raw_line.lstrip().startswith("#"):
            continue
        indent = len(raw_line) - len(raw_line.lstrip(" "))
        line = raw_line.strip()

        while stack and indent <= stack[-1][0]:
            stack.pop()
        parent = stack[-1][1]

        if pending_key and indent > pending_key[0] and line.startswith("- "):
            _, owner, key = pending_key
            owner[key] = []
            stack.append((pending_key[0], owner[key]))
            parent = owner[key]
            pending_key = None

        if line.startswith("- "):
            if not isinstance(parent, list):
                raise ConfigError(f"Unsupported YAML list placement near: {raw_line}")
            item_text = line[2:].strip()
            if item_text:
                if ":" not in item_text:
                    parent.append(_parse_scalar(item_text))
                    continue
                item: dict[str, Any] = {}
                parent.append(item)
                key, value = item_text.split(":", 1)
                item[key.strip()] = _parse_scalar(value)
                stack.append((indent, item))
                continue
            item = {}
            parent.append(item)
            stack.append((indent, item))
            continue

        if ":" not in line:
            raise ConfigError(f"Unsupported YAML line: {raw_line}")
        key, value = line.split(":", 1)
        key = key.strip()
        value = value.strip()
        if not isinstance(parent, dict):
            raise ConfigError(f"Unsupported YAML mapping placement near: {raw_line}")
        if value == "":
            parent[key] = {}
            pending_key = (indent, parent, key)
            stack.append((indent, parent[key]))
        else:
            parent[key] = _parse_scalar(value)
            pending_key = None
    return root


def _transcription_models(value: Any) -> tuple[str, ...]:
    defaults = (
        "whisper-large-v3-turbo",
        "FunAudioLLM/SenseVoiceSmall",
        "TeleAI/TeleSpeechASR",
    )
    if value is None:
        return defaults
    if isinstance(value, str):
        models = tuple(part.strip() for part in value.split(",") if part.strip())
    elif isinstance(value, (list, tuple)):
        models = tuple(str(part).strip() for part in value if str(part).strip())
    else:
        raise ConfigError("transcription.models must be a list or comma-separated string")
    return models or defaults


def _load_config_data(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise ConfigError(f"Config file not found: {path}")
    if path.suffix.lower() == ".json":
        return json.loads(path.read_text(encoding="utf-8"))
    return _load_yaml_limited(path)


def _path(value: str, root_dir: Path) -> Path:
    path = Path(value).expanduser()
    if path.is_absolute():
        return path
    return root_dir / path


def load_config(path: str | Path = "config.yaml", *, root_dir: str | Path | None = None) -> AppConfig:
    config_path = Path(path)
    if root_dir is not None:
        root = Path(root_dir).resolve()
    elif config_path.is_absolute():
        root = config_path.parent.resolve()
    else:
        root = Path.cwd().resolve()
    path = _path(str(path), root)
    load_env_file(root / ".env")
    data = _load_config_data(path)

    try:
        obsidian = data["obsidian"]
        dedao = data["dedao"]
        summary = data["summary"]
        transcription = data["transcription"]
        feishu = data["feishu"]
    except KeyError as exc:
        raise ConfigError(f"Missing required config section: {exc.args[0]}") from exc

    columns = tuple(
        ColumnConfig(
            name=str(item["name"]),
            url=str(item["url"]),
            enabled=_bool(item.get("enabled", True), "dedao.columns[].enabled"),
            kind=str(item.get("kind") or ("live" if "/live/" in str(item.get("url", "")) else "column")),
            backfill_since=(str(item["backfill_since"]).strip() or None) if item.get("backfill_since") else None,
            backfill_until=(str(item["backfill_until"]).strip() or None) if item.get("backfill_until") else None,
        )
        for item in dedao.get("columns", [])
    )
    if not columns:
        raise ConfigError("At least one dedao column must be configured")

    return AppConfig(
        obsidian=ObsidianConfig(
            vault_path=_path(str(obsidian["vault_path"]), root),
            output_dir=str(obsidian.get("output_dir", "得到")),
            year_subfolders=_bool(obsidian.get("year_subfolders", False), "obsidian.year_subfolders"),
            filename_pattern=str(obsidian.get("filename_pattern", "{column}-{published_date}-{title}.md")),
        ),
        dedao=DedaoConfig(
            auth_state_path=_path(str(dedao.get("auth_state_path", "data/auth/dedao_state.json")), root),
            browser_profile_dir=_path(str(dedao.get("browser_profile_dir", "data/browser_profile")), root),
            headless=_bool(dedao.get("headless", False), "dedao.headless"),
            request_interval_seconds=float(dedao.get("request_interval_seconds", 2)),
            save_failure_html=_bool(dedao.get("save_failure_html", False), "dedao.save_failure_html"),
            failure_snapshot_dir=_path(str(dedao.get("failure_snapshot_dir", "data/page_failures")), root),
            columns=columns,
        ),
        summary=SummaryConfig(
            enabled=_bool(summary.get("enabled", True), "summary.enabled"),
            provider=str(summary.get("provider", "opencode_go")),
            model=str(summary.get("model", "deepseek-v4-pro")),
            base_url_env=str(summary.get("base_url_env", "OPENCODE_GO_BASE_URL")),
            api_key_env=str(summary.get("api_key_env", "OPENCODE_GO_API_KEY")),
        ),
        transcription=TranscriptionConfig(
            enabled=_bool(transcription.get("enabled", False), "transcription.enabled"),
            provider=str(transcription.get("provider", "s3ai")),
            delete_media_after_transcription=_bool(
                transcription.get("delete_media_after_transcription", True),
                "transcription.delete_media_after_transcription",
            ),
            temp_dir=_path(str(transcription.get("temp_dir", "data/media_cache")), root),
            free_tier_confirmed=_bool(
                transcription.get("free_tier_confirmed", False),
                "transcription.free_tier_confirmed",
            ),
            api_key_env=str(transcription.get("api_key_env", "S3AI_API_KEY")),
            endpoint_env=str(transcription.get("endpoint_env", "S3AI_BASE_URL")),
            models=_transcription_models(transcription.get("models")),
            model_retries=int(transcription.get("model_retries", 1)),
            asr_concurrency=int(transcription.get("asr_concurrency", 2)),
            checkpoint_enabled=_bool(
                transcription.get("checkpoint_enabled", True),
                "transcription.checkpoint_enabled",
            ),
            model_circuit_breaker_threshold=int(
                transcription.get("model_circuit_breaker_threshold", 2)
            ),
            max_duration_seconds=int(transcription.get("max_duration_seconds", 14400)),
            max_audio_bytes=int(transcription.get("max_audio_bytes", 100_000_000)),
            request_timeout_seconds=int(transcription.get("request_timeout_seconds", 120)),
            max_segments=int(transcription.get("max_segments", 24)),
            min_free_disk_bytes=int(transcription.get("min_free_disk_bytes", 1_000_000_000)),
        ),
        feishu=FeishuConfig(
            enabled=_bool(feishu.get("enabled", True), "feishu.enabled"),
            webhook_url_env=str(feishu.get("webhook_url_env", "FEISHU_WEBHOOK_URL")),
            secret_env=str(feishu.get("secret_env", "FEISHU_WEBHOOK_SECRET")),
            include_titles=_bool(feishu.get("include_titles", True), "feishu.include_titles"),
        ),
        root_dir=root,
    )
