from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from dedao_sync.config import load_config


LIVE_URL = "https://www.dedao.cn/live/detail?id=Ag7Lr52RgE9yJKMmGAv8WVnXwlbebTX03D5AE6zoQd4jbYlx03NkeDz16ZaoBOqe"
KEYWORD_RE = re.compile(
    r"(transcript|subtitle|caption|subtitles|captions|vtt|srt|text|content|brief|desc|title|live|replay)",
    re.IGNORECASE,
)
MEDIA_RE = re.compile(r"(\.m3u8|\.mp4|\.m4a|\.aac|\.vtt|\.srt)(?:[?#]|$)", re.IGNORECASE)
SENSITIVE_QUERY_KEYS = {
    "token",
    "sign",
    "signature",
    "auth",
    "authorization",
    "access_token",
    "key",
    "secret",
    "expires",
    "expire",
    "ts",
}


def redact_url(raw_url: str) -> str:
    parts = urlsplit(raw_url)
    if not parts.query:
        return raw_url
    pairs = []
    for key, value in parse_qsl(parts.query, keep_blank_values=True):
        lowered = key.lower()
        if lowered in SENSITIVE_QUERY_KEYS or "token" in lowered or "sign" in lowered:
            pairs.append((key, "<redacted>"))
        elif len(value) > 80:
            pairs.append((key, f"<{len(value)} chars>"))
        else:
            pairs.append((key, value))
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(pairs), parts.fragment))


def preview(value: Any, limit: int = 180) -> str:
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) > limit:
        return text[:limit] + "..."
    return text


def top_keys(value: Any) -> list[str]:
    if isinstance(value, dict):
        return sorted(str(key) for key in value.keys())[:30]
    if isinstance(value, list) and value and isinstance(value[0], dict):
        return sorted(str(key) for key in value[0].keys())[:30]
    return []


def find_keyword_fields(value: Any, *, prefix: str = "", limit: int = 30) -> list[dict[str, str]]:
    found: list[dict[str, str]] = []

    def walk(current: Any, path: str) -> None:
        if len(found) >= limit:
            return
        if isinstance(current, dict):
            for key, child in current.items():
                child_path = f"{path}.{key}" if path else str(key)
                if KEYWORD_RE.search(str(key)):
                    found.append({"path": child_path, "preview": preview(child)})
                    if len(found) >= limit:
                        return
                walk(child, child_path)
        elif isinstance(current, list):
            for index, child in enumerate(current[:5]):
                walk(child, f"{path}[{index}]")

    walk(value, prefix)
    return found


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            reconfigure(encoding="utf-8", errors="backslashreplace")

    output_dir = Path("data/live_network")
    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / "dedao_live_network_summary.json"
    config = load_config("config.yaml")

    from playwright.sync_api import sync_playwright

    records: list[dict[str, Any]] = []

    def on_response(response) -> None:
        url = response.url
        if "dedao.cn" not in url and not MEDIA_RE.search(url):
            return
        headers = response.headers
        content_type = headers.get("content-type", "")
        record: dict[str, Any] = {
            "url": redact_url(url),
            "status": response.status,
            "content_type": content_type,
            "media_like": bool(MEDIA_RE.search(url)),
        }
        if "json" in content_type.lower():
            try:
                payload = response.json()
            except Exception as exc:
                record["json_error"] = type(exc).__name__
            else:
                record["top_keys"] = top_keys(payload)
                record["keyword_fields"] = find_keyword_fields(payload)
        elif record["media_like"] or "mpegurl" in content_type.lower() or "vtt" in content_type.lower():
            try:
                body = response.text()
            except Exception as exc:
                record["text_error"] = type(exc).__name__
            else:
                record["body_preview"] = preview(body, 500)
        records.append(record)

    with sync_playwright() as playwright:
        if config.dedao.browser_profile_dir.exists():
            context = playwright.chromium.launch_persistent_context(
                user_data_dir=str(config.dedao.browser_profile_dir),
                headless=config.dedao.headless,
            )
            browser = None
        else:
            browser = playwright.chromium.launch(headless=config.dedao.headless)
            context = browser.new_context(storage_state=str(config.dedao.auth_state_path))
        try:
            page = context.new_page()
            page.on("response", on_response)
            page.goto(LIVE_URL, wait_until="commit", timeout=45000)
            try:
                page.wait_for_load_state("networkidle", timeout=20000)
            except Exception:
                pass
            page.wait_for_timeout(5000)
            for point in ((520, 300), (360, 250), (760, 300)):
                try:
                    page.mouse.click(*point)
                    page.wait_for_timeout(3000)
                except Exception:
                    pass
            try:
                page.keyboard.press("Space")
                page.wait_for_timeout(3000)
            except Exception:
                pass
        finally:
            if browser is None:
                context.close()
            else:
                browser.close()

    output_path.write_text(json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8")
    interesting = [
        record
        for record in records
        if record.get("media_like") or record.get("keyword_fields") or "json" in str(record.get("content_type", "")).lower()
    ]
    print(f"records={len(records)}")
    print(f"interesting={len(interesting)}")
    print(f"saved={output_path.resolve()}")
    for record in interesting[:40]:
        fields = record.get("keyword_fields") or []
        media = " media" if record.get("media_like") else ""
        print(f"- {record['status']} {record['content_type']}{media} {record['url']}")
        for field in fields[:6]:
            print(f"  {field['path']}: {field['preview']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
