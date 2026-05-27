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
            item: dict[str, Any] = {}
            parent.append(item)
            if item_text:
                key, value = item_text.split(":", 1)
                item[key.strip()] = _parse_scalar(value)
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


def _load_config_data(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise ConfigError(f"Config file not found: {path}")
    if path.suffix.lower() == ".json":
        return json.loads(path.read_text(encoding="utf-8"))
    return _load_yaml_limited(path)


def _path(value: str, root_dir: Path) -> Path:
    path = Path(value)
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
            enabled=bool(item.get("enabled", True)),
        )
        for item in dedao.get("columns", [])
    )
    if not columns:
        raise ConfigError("At least one dedao column must be configured")

    return AppConfig(
        obsidian=ObsidianConfig(
            vault_path=_path(str(obsidian["vault_path"]), root),
            output_dir=str(obsidian.get("output_dir", "得到")),
            filename_pattern=str(obsidian.get("filename_pattern", "{column}-{published_date}-{title}.md")),
        ),
        dedao=DedaoConfig(
            auth_state_path=_path(str(dedao.get("auth_state_path", "data/auth/dedao_state.json")), root),
            browser_profile_dir=_path(str(dedao.get("browser_profile_dir", "data/browser_profile")), root),
            headless=bool(dedao.get("headless", False)),
            request_interval_seconds=float(dedao.get("request_interval_seconds", 2)),
            columns=columns,
        ),
        summary=SummaryConfig(
            enabled=bool(summary.get("enabled", True)),
            provider=str(summary.get("provider", "opencode_go")),
            model=str(summary.get("model", "deepseek-v4-pro")),
            base_url_env=str(summary.get("base_url_env", "OPENCODE_GO_BASE_URL")),
            api_key_env=str(summary.get("api_key_env", "OPENCODE_GO_API_KEY")),
        ),
        transcription=TranscriptionConfig(
            enabled=bool(transcription.get("enabled", False)),
            provider=str(transcription.get("provider", "faster_whisper")),
            delete_media_after_transcription=bool(transcription.get("delete_media_after_transcription", True)),
            temp_dir=_path(str(transcription.get("temp_dir", "data/media_cache")), root),
        ),
        feishu=FeishuConfig(
            enabled=bool(feishu.get("enabled", True)),
            webhook_url_env=str(feishu.get("webhook_url_env", "FEISHU_WEBHOOK_URL")),
            secret_env=str(feishu.get("secret_env", "FEISHU_WEBHOOK_SECRET")),
        ),
        root_dir=root,
    )
