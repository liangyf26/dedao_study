from __future__ import annotations

import importlib.util
import os
import sys
from dataclasses import dataclass
from pathlib import Path

from .config import ConfigError, load_config
from .preflight import check_config_semantics


@dataclass(frozen=True)
class DoctorCheck:
    name: str
    status: str
    message: str

    @property
    def ok(self) -> bool:
        return self.status == "ok"


CORE_DEPENDENCIES = ("yaml", "playwright")


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
    checks.append(
        DoctorCheck(
            "auth_state",
            "ok" if config.dedao.auth_state_path.exists() else ("error" if require_auth else "warn"),
            str(config.dedao.auth_state_path),
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
            checks.append(
                DoctorCheck(
                    f"env:{env_name}",
                    "ok" if os.environ.get(env_name) else "warn",
                    "set" if os.environ.get(env_name) else "missing",
                )
            )

    if config.feishu.enabled:
        checks.append(
            DoctorCheck(
                f"env:{config.feishu.webhook_url_env}",
                "ok" if os.environ.get(config.feishu.webhook_url_env) else "warn",
                "set" if os.environ.get(config.feishu.webhook_url_env) else "missing",
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
