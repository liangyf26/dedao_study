from __future__ import annotations

import importlib.util
import os
import sys
from dataclasses import dataclass
from pathlib import Path

from .browser import check_playwright_chromium, validate_storage_state_file
from .config import ConfigError, load_config
from .preflight import check_config_semantics, is_http_url


@dataclass(frozen=True)
class DoctorCheck:
    name: str
    status: str
    message: str

    @property
    def ok(self) -> bool:
        return self.status == "ok"


CORE_DEPENDENCIES = ("playwright",)


def _check_import(module_name: str) -> bool:
    return importlib.util.find_spec(module_name) is not None


def run_doctor(config_path: str | Path = "config.yaml", *, require_auth: bool = True) -> list[DoctorCheck]:
    checks: list[DoctorCheck] = []
    python_ok = sys.version_info >= (3, 11)
    checks.append(
        DoctorCheck(
            "python",
            "ok" if python_ok else "error",
            f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}",
        )
    )

    config_path = Path(config_path)
    checks.append(
        DoctorCheck(
            "config_file",
            "ok" if config_path.exists() else "error",
            str(config_path.resolve() if config_path.exists() else config_path),
        )
    )

    try:
        config = load_config(config_path)
    except ConfigError as exc:
        checks.append(DoctorCheck("config_load", "error", str(exc)))
        return checks

    checks.append(DoctorCheck("config_load", "ok", "loaded"))
    semantic_result = check_config_semantics(config)
    if semantic_result.ok:
        checks.append(DoctorCheck("config_semantics", "ok", "valid"))
    else:
        for error in semantic_result.errors:
            checks.append(DoctorCheck("config_semantics", "error", error))
        for warning in semantic_result.warnings:
            checks.append(DoctorCheck("config_semantics", "warn", warning))
    checks.append(
        DoctorCheck(
            "obsidian_vault",
            "ok" if config.obsidian.vault_path.is_dir() else "error",
            str(config.obsidian.vault_path),
        )
    )
    auth_ok, auth_message = validate_storage_state_file(config.dedao.auth_state_path)
    checks.append(
        DoctorCheck(
            "auth_state",
            "ok" if auth_ok else ("error" if require_auth else "warn"),
            str(config.dedao.auth_state_path) if auth_ok else auth_message,
        )
    )
    checks.append(
        DoctorCheck(
            "env_file",
            "ok" if (config.root_dir / ".env").exists() else "warn",
            str(config.root_dir / ".env"),
        )
    )

    if config.summary.enabled:
        for env_name in (config.summary.base_url_env, config.summary.api_key_env):
            env_value = os.environ.get(env_name)
            if env_name == config.summary.base_url_env and env_value and not is_http_url(env_value):
                checks.append(DoctorCheck(f"env:{env_name}", "warn", "invalid URL"))
                continue
            checks.append(
                DoctorCheck(
                    f"env:{env_name}",
                    "ok" if env_value else "warn",
                    "set" if env_value else "missing",
                )
            )

    if config.feishu.enabled:
        webhook = os.environ.get(config.feishu.webhook_url_env)
        webhook_status = "ok" if webhook else "warn"
        webhook_message = "set" if webhook else "missing"
        if webhook and not is_http_url(webhook):
            webhook_status = "warn"
            webhook_message = "invalid URL"
        checks.append(
            DoctorCheck(
                f"env:{config.feishu.webhook_url_env}",
                webhook_status,
                webhook_message,
            )
        )
        checks.append(
            DoctorCheck(
                f"env:{config.feishu.secret_env}",
                "ok" if os.environ.get(config.feishu.secret_env) else "warn",
                "set" if os.environ.get(config.feishu.secret_env) else "missing or not required",
            )
        )

    for dependency in CORE_DEPENDENCIES:
        checks.append(
            DoctorCheck(
                f"dep:{dependency}",
                "ok" if _check_import(dependency) else "warn",
                "installed" if _check_import(dependency) else "missing",
            )
        )
    checks.append(
        DoctorCheck(
            "dep:pyyaml",
            "ok",
            "installed" if _check_import("yaml") else "not installed; using built-in limited YAML parser",
        )
    )
    browser_ok, browser_message = check_playwright_chromium()
    checks.append(
        DoctorCheck(
            "dep:playwright_chromium",
            "ok" if browser_ok else "warn",
            browser_message,
        )
    )

    checks.append(
        DoctorCheck(
            "venv",
            "ok" if (config.root_dir / ".venv").exists() else "warn",
            str(config.root_dir / ".venv"),
        )
    )
    return checks


def doctor_exit_code(checks: list[DoctorCheck]) -> int:
    return 1 if any(check.status == "error" for check in checks) else 0


def doctor_checks_to_dicts(checks: list[DoctorCheck]) -> list[dict[str, str]]:
    return [{"name": check.name, "status": check.status, "message": check.message} for check in checks]
