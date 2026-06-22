from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any


DEFAULT_PROMPT = "请用一句话回答：这个 OpenAI 兼容接口可以正常工作。"
DEFAULT_SYSTEM_PROMPT = "You are a concise API test assistant."
DEFAULT_TIMEOUT_SECONDS = 60
USER_AGENT = "openai-compatible-api-test/0.1"


class ApiTestError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        elapsed_ms: int | None = None,
        user_agent_suspected: str = "unknown",
        hint: str = "",
    ):
        super().__init__(message)
        self.elapsed_ms = elapsed_ms
        self.user_agent_suspected = user_agent_suspected
        self.hint = hint


def make_stream_unicode_safe(stream: Any) -> None:
    reconfigure = getattr(stream, "reconfigure", None)
    if not callable(reconfigure):
        return
    try:
        reconfigure(encoding="utf-8", errors="backslashreplace")
    except (OSError, ValueError):
        try:
            reconfigure(errors="backslashreplace")
        except (OSError, ValueError):
            return


def main(argv: list[str] | None = None) -> int:
    make_stream_unicode_safe(sys.stdout)
    make_stream_unicode_safe(sys.stderr)
    parser = argparse.ArgumentParser(description="Test an OpenAI-compatible chat completions API.")
    parser.add_argument("--config", required=True, help="Path to JSON/YAML config file.")
    parser.add_argument("--name", help="Only run one configured API by name.")
    parser.add_argument("--prompt", default=DEFAULT_PROMPT, help="User prompt sent to the model.")
    parser.add_argument("--system", default=DEFAULT_SYSTEM_PROMPT, help="System prompt sent to the model.")
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT_SECONDS, help="HTTP timeout seconds.")
    parser.add_argument("--user-agent", help="Override User-Agent for all API requests.")
    parser.add_argument("--raw", action="store_true", help="Print redacted raw JSON response.")
    args = parser.parse_args(argv)

    try:
        config_path = Path(args.config).resolve()
        load_env_file(config_path.parent / ".env")
        if config_path.parent != Path.cwd().resolve():
            load_env_file(Path.cwd() / ".env")
        configs = resolve_api_configs(load_config_data(config_path), only_name=args.name, user_agent=args.user_agent)
    except ApiTestError as exc:
        print(f"FAILED: {exc}", file=sys.stderr)
        return 1

    failed = 0
    for index, config in enumerate(configs, start=1):
        label = config.get("name") or f"api-{index}"
        print(f"== {label} ==")
        try:
            result = call_chat_completions(
                config,
                system_prompt=args.system,
                user_prompt=args.prompt,
                timeout_seconds=args.timeout,
            )
        except ApiTestError as exc:
            failed += 1
            print(f"FAILED: {exc}", file=sys.stderr)
            print(f"elapsed_ms: {exc.elapsed_ms if exc.elapsed_ms is not None else ''}", file=sys.stderr)
            print(f"user_agent: {config['user_agent']}", file=sys.stderr)
            print(f"user_agent_suspected: {exc.user_agent_suspected}", file=sys.stderr)
            if exc.hint:
                print(f"hint: {exc.hint}", file=sys.stderr)
            continue

        print("OK: API request succeeded")
        print(f"endpoint: {chat_completions_url(config['base_url'])}")
        print(f"model: {config['model']}")
        print(f"user_agent: {config['user_agent']}")
        print(f"elapsed_ms: {result['elapsed_ms']}")
        print(f"response_id: {result.get('id') or ''}")
        print(f"finish_reason: {result.get('finish_reason') or ''}")
        print("content:")
        print(result["content"])
        if args.raw:
            print("raw_response:")
            print(json.dumps(redact_secrets(result["raw"]), ensure_ascii=False, indent=2))
    return 1 if failed else 0


def load_config_data(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise ApiTestError(f"config file not found: {path}")
    if path.suffix.lower() == ".json":
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ApiTestError(f"invalid JSON config: {exc}") from exc
    else:
        data = load_yaml_data(path)
    if not isinstance(data, dict):
        raise ApiTestError("config root must be an object/mapping")
    return data


def load_yaml_data(path: Path) -> dict[str, Any]:
    try:
        import yaml  # type: ignore
    except ImportError:
        yaml = None
    if yaml is not None:
        loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
        if not isinstance(loaded, dict):
            raise ApiTestError("YAML config root must be a mapping")
        return loaded
    return load_limited_yaml(path)


def load_limited_yaml(path: Path) -> dict[str, Any]:
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
                raise ApiTestError(f"unsupported YAML list placement: {raw_line}")
            item_text = line[2:].strip()
            item: dict[str, Any] = {}
            parent.append(item)
            if item_text:
                if ":" not in item_text:
                    raise ApiTestError(f"unsupported YAML list item: {raw_line}")
                key, value = item_text.split(":", 1)
                item[key.strip()] = parse_scalar(value.strip())
            stack.append((indent, item))
            continue

        if ":" not in line:
            raise ApiTestError(f"unsupported YAML line without ':': {raw_line}")
        key, value = line.split(":", 1)
        key = key.strip()
        value = value.strip()
        if not isinstance(parent, dict):
            raise ApiTestError(f"unsupported YAML mapping placement: {raw_line}")
        if value == "":
            child: dict[str, Any] = {}
            parent[key] = child
            pending_key = (indent, parent, key)
            stack.append((indent, child))
        else:
            parent[key] = parse_scalar(value)
            pending_key = None
    return root


def parse_scalar(value: str) -> Any:
    value = value.strip()
    if value in {"true", "True"}:
        return True
    if value in {"false", "False"}:
        return False
    if value in {"null", "None", "~"}:
        return None
    if (value.startswith('"') and value.endswith('"')) or (value.startswith("'") and value.endswith("'")):
        return value[1:-1]
    return value


def load_env_file(path: Path) -> None:
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def resolve_api_configs(
    data: dict[str, Any],
    *,
    only_name: str | None = None,
    user_agent: str | None = None,
) -> list[dict[str, str]]:
    raw_entries = data.get("apis")
    if raw_entries is None:
        raw_entries = data.get("providers")
    if isinstance(raw_entries, list):
        configs = [
            resolve_api_config(entry, default_name=f"api-{index}", user_agent=user_agent)
            for index, entry in enumerate(raw_entries, start=1)
        ]
    else:
        configs = [resolve_api_config(data, default_name=str(data.get("name") or "default"), user_agent=user_agent)]

    if only_name:
        configs = [config for config in configs if config.get("name") == only_name]
        if not configs:
            raise ApiTestError(f"no API config named: {only_name}")
    return configs


def resolve_api_config(
    data: dict[str, Any],
    *,
    default_name: str = "default",
    user_agent: str | None = None,
) -> dict[str, str]:
    if not isinstance(data, dict):
        raise ApiTestError("API config entry must be a mapping")
    section = data.get("openai") or data.get("api")
    summary = data.get("summary")
    if section is None and isinstance(summary, dict):
        section = summary
    if section is None:
        section = data
    if not isinstance(section, dict):
        raise ApiTestError("config section must be a mapping")

    base_url = value_from_config_or_env(section, "base_url", "base_url_env")
    api_key = value_from_config_or_env(section, "api_key", "api_key_env")
    model = str(section.get("model") or "").strip()
    if not base_url:
        raise ApiTestError("missing base_url or base_url_env")
    if not api_key:
        raise ApiTestError("missing api_key or api_key_env")
    if not model:
        raise ApiTestError("missing model")
    name = str(section.get("name") or data.get("name") or default_name).strip()
    resolved_user_agent = (
        user_agent
        or value_from_config_or_env(section, "user_agent", "user_agent_env")
        or value_from_config_or_env(data, "user_agent", "user_agent_env")
        or USER_AGENT
    )
    return {
        "name": name,
        "base_url": base_url.rstrip("/"),
        "api_key": api_key,
        "model": model,
        "user_agent": resolved_user_agent,
    }


def value_from_config_or_env(section: dict[str, Any], value_key: str, env_key: str) -> str:
    direct = str(section.get(value_key) or "").strip()
    if direct:
        return direct
    env_name = str(section.get(env_key) or "").strip()
    if env_name:
        return os.environ.get(env_name, "").strip()
    return ""


def chat_completions_url(base_url: str) -> str:
    base = base_url.rstrip("/")
    if base.endswith("/chat/completions"):
        return base
    return f"{base}/chat/completions"


def call_chat_completions(
    config: dict[str, str],
    *,
    system_prompt: str,
    user_prompt: str,
    timeout_seconds: int,
) -> dict[str, Any]:
    payload = {
        "model": config["model"],
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "temperature": 0,
        "max_tokens": 200,
    }
    request = urllib.request.Request(
        chat_completions_url(config["base_url"]),
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {config['api_key']}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": config.get("user_agent") or USER_AGENT,
        },
        method="POST",
    )
    started = time.perf_counter()
    try:
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            body = response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        elapsed_ms = int((time.perf_counter() - started) * 1000)
        error_body = exc.read().decode("utf-8", errors="replace")
        classification = classify_user_agent_failure(exc.code, error_body)
        raise ApiTestError(
            f"HTTP {exc.code}: {redact_text(error_body)[:1000]}",
            elapsed_ms=elapsed_ms,
            user_agent_suspected=classification["suspected"],
            hint=classification["hint"],
        ) from exc
    except urllib.error.URLError as exc:
        elapsed_ms = int((time.perf_counter() - started) * 1000)
        raise ApiTestError(f"network error: {exc}", elapsed_ms=elapsed_ms) from exc
    except TimeoutError as exc:
        elapsed_ms = int((time.perf_counter() - started) * 1000)
        raise ApiTestError(f"timeout after {timeout_seconds}s: {exc}", elapsed_ms=elapsed_ms) from exc

    elapsed_ms = int((time.perf_counter() - started) * 1000)
    try:
        parsed = json.loads(body)
    except json.JSONDecodeError as exc:
        classification = classify_non_json_failure(body)
        raise ApiTestError(
            f"response is not JSON: {redact_text(body)[:1000]}",
            elapsed_ms=elapsed_ms,
            user_agent_suspected=classification["suspected"],
            hint=classification["hint"],
        ) from exc

    try:
        choice = parsed["choices"][0]
        content = choice["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise ApiTestError(
            f"response has no choices[0].message.content: {redact_text(body)[:1000]}",
            elapsed_ms=elapsed_ms,
            user_agent_suspected="no",
            hint="The server returned JSON, but it does not match OpenAI chat completions format. Check endpoint path, model, or provider compatibility.",
        ) from exc

    if not isinstance(content, str) or not content.strip():
        raise ApiTestError(
            f"response content is empty: {redact_text(body)[:1000]}",
            elapsed_ms=elapsed_ms,
            user_agent_suspected="no",
            hint="The request reached the model API and returned OpenAI-style JSON, but message content is empty.",
        )

    return {
        "elapsed_ms": elapsed_ms,
        "id": parsed.get("id"),
        "finish_reason": choice.get("finish_reason"),
        "content": content.strip(),
        "raw": parsed,
    }


def classify_user_agent_failure(status_code: int, body: str) -> dict[str, str]:
    normalized = body.lower()
    if status_code == 403 and (
        "error 1010" in normalized
        or "browser_signature_banned" in normalized
        or "browser's signature" in normalized
        or "user-agent has been banned" in normalized
    ):
        return {
            "suspected": "yes",
            "hint": "Cloudflare 1010/browser_signature_banned indicates the provider blocked this request based on browser signature or User-Agent. Try a known-good User-Agent, or use curl/Postman to compare headers.",
        }
    if status_code in {403, 406, 418, 429, 456} and (
        "_guard/" in normalized
        or "captcha" in normalized
        or "cloudflare" in normalized
        or "browser" in normalized
        or "user-agent" in normalized
    ):
        return {
            "suspected": "possible",
            "hint": "The response looks like an anti-bot/browser guard page. User-Agent may be part of it, but TLS/browser fingerprint or missing required headers may also be involved.",
        }
    return {
        "suspected": "no",
        "hint": "This HTTP error does not look primarily caused by User-Agent. Check base_url, endpoint path, API key, model name, quota, and provider permissions.",
    }


def classify_non_json_failure(body: str) -> dict[str, str]:
    normalized = body.lower()
    if "browser_signature_banned" in normalized or "error 1010" in normalized:
        return {
            "suspected": "yes",
            "hint": "The non-JSON response explicitly reports browser signature/User-Agent blocking.",
        }
    if "_guard/" in normalized or "captcha" in normalized or "cloudflare" in normalized:
        return {
            "suspected": "possible",
            "hint": "The non-JSON response looks like a browser guard page. User-Agent may be involved; also compare TLS/browser fingerprint and required headers.",
        }
    if "<html" in normalized or "<!doctype html" in normalized:
        return {
            "suspected": "no",
            "hint": "The server returned an HTML page, not an API JSON response. This usually means base_url/path is wrong, for example missing /v1.",
        }
    return {
        "suspected": "unknown",
        "hint": "The response is not JSON, but it does not match known User-Agent or endpoint-path patterns.",
    }


def redact_secrets(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: redact_secrets(item) for key, item in value.items()}
    if isinstance(value, list):
        return [redact_secrets(item) for item in value]
    if isinstance(value, str):
        return redact_text(value)
    return value


def redact_text(text: str) -> str:
    for key, value in os.environ.items():
        if not value or len(value) < 8:
            continue
        if "KEY" in key.upper() or "TOKEN" in key.upper() or "SECRET" in key.upper():
            text = text.replace(value, "[REDACTED]")
    return text


if __name__ == "__main__":
    raise SystemExit(main())
