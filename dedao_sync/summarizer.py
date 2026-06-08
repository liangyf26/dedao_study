from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import replace
from typing import Any

from .models import ContentDetail, SummaryConfig, SummaryResult
from .security import redact


class SummaryError(RuntimeError):
    pass


class SummaryService:
    def summarize(self, detail: ContentDetail) -> SummaryResult:
        raise NotImplementedError


MAX_TRANSCRIPT_CHARS = 20000
DEFAULT_SUMMARY_TIMEOUT_SECONDS = 180
SUMMARY_API_USER_AGENT = "dedao-sync/0.1"
SUMMARY_MAX_TOKENS = 2200
SUMMARY_COMPACT_MAX_TOKENS = 900
COMPACT_TRANSCRIPT_CHARS = 8000
SUMMARY_ULTRA_COMPACT_MAX_TOKENS = 500
ULTRA_COMPACT_TRANSCRIPT_CHARS = 2500
SUMMARY_REQUEST_ATTEMPTS = 2
SUMMARY_TRANSIENT_ERROR_PATTERNS = (
    "sslv3_alert_bad_record_mac",
    "bad record mac",
    "connection reset",
    "econnreset",
    "temporarily unavailable",
    "timed out",
    "timeout",
)


class DisabledSummaryService(SummaryService):
    def summarize(self, detail: ContentDetail) -> SummaryResult:
        return SummaryResult.empty()


class OpenAICompatibleSummaryService(SummaryService):
    def __init__(self, config: SummaryConfig, *, timeout_seconds: int = DEFAULT_SUMMARY_TIMEOUT_SECONDS):
        self.config = config
        self.timeout_seconds = timeout_seconds

    def summarize(self, detail: ContentDetail) -> SummaryResult:
        base_url = os.environ.get(self.config.base_url_env, "").rstrip("/")
        api_key = os.environ.get(self.config.api_key_env, "")
        if not base_url or not api_key:
            raise SummaryError("summary API env is not configured")
        endpoint = chat_completions_url(base_url)
        try:
            return self._summarize_with_prompt(
                detail,
                endpoint,
                api_key,
                base_url,
                build_summary_prompt(detail),
                max_tokens=SUMMARY_MAX_TOKENS,
            )
        except SummaryError as exc:
            if not is_compact_retryable_summary_error(exc):
                raise
            try:
                return self._summarize_with_prompt(
                    detail,
                    endpoint,
                    api_key,
                    base_url,
                    build_compact_summary_prompt(detail),
                    max_tokens=SUMMARY_COMPACT_MAX_TOKENS,
                )
            except SummaryError as compact_exc:
                if not is_compact_retryable_summary_error(compact_exc):
                    raise
                return self._summarize_with_prompt(
                    detail,
                    endpoint,
                    api_key,
                    base_url,
                    build_ultra_compact_summary_prompt(detail),
                    max_tokens=SUMMARY_ULTRA_COMPACT_MAX_TOKENS,
                )

    def _summarize_with_prompt(
        self,
        detail: ContentDetail,
        endpoint: str,
        api_key: str,
        base_url: str,
        prompt: str,
        *,
        max_tokens: int,
    ) -> SummaryResult:
        payload = {
            "model": self.config.model,
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "你是严谨的个人学习笔记助手，只基于用户提供的原文整理。"
                        "涉及医学、政策、金融、法律等内容时，只做原文观点归纳，不输出专业建议。"
                    ),
                },
                {"role": "user", "content": prompt},
            ],
            "temperature": 0.2,
            "max_tokens": max_tokens,
        }
        request = urllib.request.Request(
            endpoint,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
                "Accept": "application/json",
                "User-Agent": SUMMARY_API_USER_AGENT,
            },
            method="POST",
        )
        last_error: SummaryError | None = None
        for attempt in range(1, SUMMARY_REQUEST_ATTEMPTS + 1):
            try:
                with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
                    body = response.read().decode("utf-8")
            except urllib.error.HTTPError as exc:
                raise self._http_error(exc, base_url) from exc
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                error = self._network_error(exc)
                if attempt < SUMMARY_REQUEST_ATTEMPTS and is_summary_transient_error(exc):
                    last_error = error
                    time.sleep(2 * attempt)
                    continue
                raise error from exc
            try:
                parsed = json.loads(body)
                content = parsed["choices"][0]["message"]["content"]
            except (KeyError, IndexError, json.JSONDecodeError) as exc:
                raise SummaryError(redact(body)) from exc
            if isinstance(content, str) and content.strip():
                return finalize_summary_result(detail, parse_summary_text(content))
            error = SummaryError("summary API returned empty message content")
            if attempt < SUMMARY_REQUEST_ATTEMPTS:
                last_error = error
                time.sleep(2 * attempt)
                continue
            raise error
        if last_error:
            raise last_error
        raise SummaryError("summary API failed without a response")

    def _http_error(self, exc: urllib.error.HTTPError, base_url: str) -> SummaryError:
        try:
            error_body = exc.read().decode("utf-8", errors="replace")
        except Exception:
            error_body = str(exc)
        context = summary_api_context(self.config, base_url)
        hint = summary_http_hint(exc.code, error_body)
        message = f"summary API HTTP {exc.code} ({context}): {redact(error_body)[:500]}"
        if hint:
            message = f"{message}; {hint}"
        return SummaryError(message)

    def _network_error(self, exc: Exception) -> SummaryError:
        if isinstance(exc, TimeoutError):
            return SummaryError(f"summary API timeout after {self.timeout_seconds}s: {redact(exc)}")
        return SummaryError(redact(exc))


def chat_completions_url(base_url: str) -> str:
    base = base_url.rstrip("/")
    if base.endswith("/chat/completions"):
        return base
    return f"{base}/chat/completions"


def summary_api_context(config: SummaryConfig, base_url: str) -> str:
    parsed = urllib.parse.urlsplit(base_url)
    if parsed.scheme and parsed.netloc:
        safe_base = urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, parsed.path.rstrip("/") or "/", "", ""))
    else:
        safe_base = base_url
    return f"provider={config.provider}, model={config.model}, base_url={redact(safe_base)}"


def summary_http_hint(status_code: int, error_body: str) -> str:
    normalized = error_body.lower()
    if status_code == 403 and "1010" in normalized:
        return (
            "hint=HTTP 403/error code 1010 usually means the summary provider denied or blocked this request; "
            "check the base URL, API key/account permissions, allowed model, IP/WAF rules, or provider quota"
        )
    if status_code == 401:
        return "hint=check the summary API key"
    if status_code == 403:
        return "hint=check summary API account permissions, model access, quota, or provider-side access rules"
    if status_code == 404:
        return "hint=check summary base URL; it should point to an OpenAI-compatible /v1 endpoint"
    return ""


def is_summary_transient_error(exc: Exception) -> bool:
    message = str(exc).lower()
    return any(pattern in message for pattern in SUMMARY_TRANSIENT_ERROR_PATTERNS)


def is_compact_retryable_summary_error(exc: SummaryError) -> bool:
    message = str(exc).lower()
    return any(
        marker in message
        for marker in (
            "empty message content",
            "recognizable note fields",
            "timeout after",
            "bad record mac",
        )
    )


def build_summary_prompt(detail: ContentDetail) -> str:
    item = detail.item
    transcript = detail.transcript_text[:MAX_TRANSCRIPT_CHARS]
    truncation_note = ""
    if len(detail.transcript_text) > MAX_TRANSCRIPT_CHARS:
        truncation_note = f"\n注意：原文超过 {MAX_TRANSCRIPT_CHARS} 字，本次只提供前 {MAX_TRANSCRIPT_CHARS} 字，请在摘要中标注“基于截断原文”。\n"
    return f"""请把下面的得到内容整理成卡片笔记/Zettelkasten 风格。

要求：
1. 只基于原文，不要编造。
2. 分清事实、判断、启发。
3. 不要大段复制原文。
4. 如果有不确定内容，写“需要回看原文确认”。
5. 如果看到原文截断提示，必须在 `permanent_note` 中说明摘要基于截断原文。
6. 只输出 JSON，不要输出 Markdown，不要包裹代码块。
7. 控制长度：atomic_cards 最多 8 条，每条不超过 80 字；permanent_note 不超过 350 字；其他数组最多 6 条。
8. 涉及医学、政策、金融、法律时，只归纳原文观点和适用边界，不给诊断、治疗、投资、法律或政策执行建议。

JSON schema：
{{
  "atomic_cards": ["每条是一张可复用的原子卡片"],
  "permanent_note": "一段长期保存的永久笔记",
  "links": ["可关联主题或已有知识"],
  "actions": ["可行动建议或后续观察信号"],
  "questions": ["复习问题"],
  "keywords": ["关键词"]
}}

栏目：{item.column_name}
标题：{item.title}
发布日期：{item.published_at or ""}
{truncation_note}

原文：
{transcript}
"""


def build_compact_summary_prompt(detail: ContentDetail) -> str:
    item = detail.item
    transcript = detail.transcript_text[:COMPACT_TRANSCRIPT_CHARS]
    truncation_note = ""
    if len(detail.transcript_text) > COMPACT_TRANSCRIPT_CHARS:
        truncation_note = f"\n注意：原文超过 {COMPACT_TRANSCRIPT_CHARS} 字，本次只提供前 {COMPACT_TRANSCRIPT_CHARS} 字，请在 `permanent_note` 中标注“基于截断原文”。\n"
    return f"""请把下面内容整理成极短个人学习笔记。只输出可解析 JSON，不要 Markdown，不要解释。

规则：
1. 只基于原文。
2. 不输出医疗、投资、法律或政策执行建议。
3. atomic_cards 最多 4 条，每条不超过 60 字。
4. permanent_note 不超过 180 字。
5. keywords 最多 6 个。

JSON schema：
{{
  "atomic_cards": ["原子卡片"],
  "permanent_note": "永久笔记",
  "keywords": ["关键词"]
}}

栏目：{item.column_name}
标题：{item.title}
{truncation_note}

原文：
{transcript}
"""


def build_ultra_compact_summary_prompt(detail: ContentDetail) -> str:
    transcript = detail.transcript_text[:ULTRA_COMPACT_TRANSCRIPT_CHARS]
    return f"""只输出 JSON。不要解释，不要 Markdown。

任务：基于下列原文片段，写极简学习笔记；只归纳原文，不给建议。

JSON schema：
{{
  "atomic_cards": ["最多2条，每条50字以内"],
  "permanent_note": "100字以内，说明这是基于片段的笔记",
  "keywords": ["最多4个"]
}}

原文片段：
{transcript}
"""


def parse_summary_text(text: str) -> SummaryResult:
    parsed = _parse_summary_json(text)
    if parsed is not None:
        return _ensure_summary_has_content(parsed, text)
    parsed = _parse_repairable_summary_json(text)
    if parsed is not None:
        return _ensure_summary_has_content(parsed, text)
    return _ensure_summary_has_content(_parse_summary_markdown(text), text)


def finalize_summary_result(detail: ContentDetail, result: SummaryResult) -> SummaryResult:
    if len(detail.transcript_text) <= MAX_TRANSCRIPT_CHARS:
        return result
    note = result.permanent_note.strip()
    if "截断原文" in note:
        return result
    truncation_notice = "注：本文摘要基于截断原文生成。"
    permanent_note = f"{note}\n\n{truncation_notice}" if note else truncation_notice
    return replace(result, permanent_note=permanent_note)


def _ensure_summary_has_content(result: SummaryResult, raw_text: str) -> SummaryResult:
    if result.atomic_cards or result.permanent_note or result.links or result.actions or result.questions or result.keywords:
        return result
    raise SummaryError(f"summary response did not contain recognizable note fields: {redact(raw_text)[:200]}")


def _parse_summary_json(text: str) -> SummaryResult | None:
    raw = _strip_code_fence(text.strip())
    candidates = [raw]
    start = raw.find("{")
    end = raw.rfind("}")
    if start != -1 and end != -1 and end > start:
        candidates.append(raw[start : end + 1])
    for candidate in candidates:
        try:
            obj = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if not isinstance(obj, dict):
            continue
        return SummaryResult(
            atomic_cards=tuple(_as_list(obj.get("atomic_cards") or obj.get("原子卡片"))),
            permanent_note=_as_text(obj.get("permanent_note") or obj.get("永久笔记")),
            links=tuple(_as_list(obj.get("links") or obj.get("关联"))),
            actions=tuple(_as_list(obj.get("actions") or obj.get("行动/观察") or obj.get("行动"))),
            questions=tuple(_as_list(obj.get("questions") or obj.get("复习问题"))),
            keywords=tuple(_as_list(obj.get("keywords") or obj.get("关键词"))),
        )
    return None


def _parse_repairable_summary_json(text: str) -> SummaryResult | None:
    raw = _strip_code_fence(text.strip())
    if not _looks_like_summary_json(raw):
        return None
    return SummaryResult(
        atomic_cards=tuple(_jsonish_field_list(raw, ("atomic_cards", "原子卡片"))),
        permanent_note=_jsonish_field_text(raw, ("permanent_note", "永久笔记")),
        links=tuple(_jsonish_field_list(raw, ("links", "关联", "关联主题"))),
        actions=tuple(_jsonish_field_list(raw, ("actions", "行动/观察", "行动", "观察"))),
        questions=tuple(_jsonish_field_list(raw, ("questions", "复习问题", "问题"))),
        keywords=tuple(_jsonish_field_list(raw, ("keywords", "关键词"))),
    )


def _looks_like_summary_json(text: str) -> bool:
    if "{" not in text:
        return False
    return any(f'"{name}"' in text for name in ("atomic_cards", "permanent_note", "原子卡片", "永久笔记"))


def _jsonish_field_list(text: str, names: tuple[str, ...]) -> list[str]:
    value_start = _jsonish_value_start(text, names)
    if value_start is None:
        return []
    while value_start < len(text) and text[value_start].isspace():
        value_start += 1
    if value_start >= len(text):
        return []
    if text[value_start] == "[":
        return _jsonish_array_strings(text, value_start + 1)
    if text[value_start] == '"':
        value = _jsonish_string_at(text, value_start)
        return [value] if value else []
    end = _jsonish_scalar_end(text, value_start)
    return _as_list(text[value_start:end])


def _jsonish_field_text(text: str, names: tuple[str, ...]) -> str:
    value_start = _jsonish_value_start(text, names)
    if value_start is None:
        return ""
    while value_start < len(text) and text[value_start].isspace():
        value_start += 1
    if value_start >= len(text):
        return ""
    if text[value_start] == '"':
        return _jsonish_string_at(text, value_start)
    if text[value_start] == "[":
        return "\n".join(_jsonish_array_strings(text, value_start + 1))
    end = _jsonish_scalar_end(text, value_start)
    return text[value_start:end].strip(" \t\r\n,，;；)}）]")


def _jsonish_value_start(text: str, names: tuple[str, ...]) -> int | None:
    for name in names:
        match = re.search(rf'"{re.escape(name)}"\s*:', text)
        if match:
            return match.end()
    return None


def _jsonish_array_strings(text: str, start: int) -> list[str]:
    values: list[str] = []
    index = start
    while index < len(text):
        char = text[index]
        if char == "]":
            break
        if char == '"':
            value, next_index = _jsonish_string_at_with_end(text, index)
            if value:
                values.append(value)
            index = next_index
            continue
        if char == "}" and values:
            break
        index += 1
    return values


def _jsonish_string_at(text: str, start: int) -> str:
    value, _ = _jsonish_string_at_with_end(text, start)
    return value


def _jsonish_string_at_with_end(text: str, start: int) -> tuple[str, int]:
    if start >= len(text) or text[start] != '"':
        return "", start
    parts: list[str] = []
    escaped = False
    index = start + 1
    while index < len(text):
        char = text[index]
        if escaped:
            parts.append(_decode_jsonish_escape(char))
            escaped = False
        elif char == "\\":
            escaped = True
        elif char == '"':
            return "".join(parts).strip(), index + 1
        else:
            parts.append(char)
        index += 1
    return "".join(parts).strip(), index


def _decode_jsonish_escape(char: str) -> str:
    mapping = {
        '"': '"',
        "\\": "\\",
        "/": "/",
        "b": "\b",
        "f": "\f",
        "n": "\n",
        "r": "\r",
        "t": "\t",
    }
    return mapping.get(char, char)


def _jsonish_scalar_end(text: str, start: int) -> int:
    candidates = [pos for pos in (text.find(",", start), text.find("\n", start), text.find("}", start)) if pos != -1]
    return min(candidates) if candidates else len(text)


def _parse_summary_markdown(text: str) -> SummaryResult:
    cards: list[str] = []
    permanent_lines: list[str] = []
    links: list[str] = []
    actions: list[str] = []
    questions: list[str] = []
    keywords: list[str] = []
    current = ""
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        heading = _heading_name(line)
        if heading:
            current = heading
            continue
        item = _clean_list_item(line)
        section = _canonical_section(current)
        if section == "atomic_cards":
            cards.append(item)
        elif section == "permanent_note":
            permanent_lines.append(item)
        elif section == "links":
            links.append(item)
        elif section == "actions":
            actions.append(item)
        elif section == "questions":
            questions.append(item)
        elif section == "keywords":
            keywords.extend(_split_keywords(item))
    return SummaryResult(
        atomic_cards=tuple(cards),
        permanent_note="\n".join(permanent_lines),
        links=tuple(links),
        actions=tuple(actions),
        questions=tuple(questions),
        keywords=tuple(keywords),
    )


def _strip_code_fence(text: str) -> str:
    if text.startswith("```"):
        lines = text.splitlines()
        if len(lines) >= 3 and lines[-1].strip() == "```":
            return "\n".join(lines[1:-1]).strip()
    return text


def _as_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, list):
        return "\n".join(str(item).strip() for item in value if str(item).strip())
    return str(value).strip()


def _as_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    text = str(value).strip()
    if not text:
        return []
    return [part.strip() for part in re.split(r"[\n;；]", text) if part.strip()]


def _heading_name(line: str) -> str | None:
    if line.startswith("#"):
        return line.lstrip("#").strip()
    match = re.match(r"^(?:\d+[.、]\s*)?([^：:]+)[：:]?\s*$", line)
    if match:
        name = match.group(1).strip()
        if _canonical_section(name):
            return name
    return None


def _canonical_section(name: str) -> str:
    normalized = re.sub(r"^[\d.、\s]+", "", name).strip().lower()
    normalized = normalized.replace(" ", "")
    mapping = {
        "原子卡片": "atomic_cards",
        "atomiccards": "atomic_cards",
        "卡片": "atomic_cards",
        "永久笔记": "permanent_note",
        "permanentnote": "permanent_note",
        "长期笔记": "permanent_note",
        "关联": "links",
        "关联主题": "links",
        "links": "links",
        "行动/观察": "actions",
        "行动观察": "actions",
        "行动": "actions",
        "观察": "actions",
        "actions": "actions",
        "复习问题": "questions",
        "问题": "questions",
        "questions": "questions",
        "关键词": "keywords",
        "keywords": "keywords",
    }
    return mapping.get(normalized, "")


def _clean_list_item(line: str) -> str:
    line = re.sub(r"^[-*]\s+", "", line)
    line = re.sub(r"^\d+[.、]\s+", "", line)
    return line.strip()


def _split_keywords(text: str) -> list[str]:
    return [part.strip() for part in re.split(r"[,，、;；\s]+", text) if part.strip()]


def create_summary_service(config: SummaryConfig) -> SummaryService:
    if not config.enabled:
        return DisabledSummaryService()
    return OpenAICompatibleSummaryService(config)
