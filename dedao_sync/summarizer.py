from __future__ import annotations

import json
import os
import re
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


MAX_TRANSCRIPT_CHARS = 30000
DEFAULT_SUMMARY_TIMEOUT_SECONDS = 180
SUMMARY_API_USER_AGENT = "dedao-sync/0.1"


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
        prompt = build_summary_prompt(detail)
        payload = {
            "model": self.config.model,
            "messages": [
                {"role": "system", "content": "你是严谨的卡片笔记助手，只基于用户提供的原文整理。"},
                {"role": "user", "content": prompt},
            ],
            "temperature": 0.2,
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
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
                body = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            try:
                error_body = exc.read().decode("utf-8", errors="replace")
            except Exception:
                error_body = str(exc)
            context = summary_api_context(self.config, base_url)
            hint = summary_http_hint(exc.code, error_body)
            message = f"summary API HTTP {exc.code} ({context}): {redact(error_body)[:500]}"
            if hint:
                message = f"{message}; {hint}"
            raise SummaryError(message) from exc
        except urllib.error.URLError as exc:
            raise SummaryError(redact(exc)) from exc
        except TimeoutError as exc:
            raise SummaryError(f"summary API timeout after {self.timeout_seconds}s: {redact(exc)}") from exc
        try:
            parsed = json.loads(body)
            content = parsed["choices"][0]["message"]["content"]
        except (KeyError, IndexError, json.JSONDecodeError) as exc:
            raise SummaryError(redact(body)) from exc
        return finalize_summary_result(detail, parse_summary_text(content))


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


def parse_summary_text(text: str) -> SummaryResult:
    parsed = _parse_summary_json(text)
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
