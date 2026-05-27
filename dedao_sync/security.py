from __future__ import annotations

import re


SECRET_PATTERNS = [
    re.compile(r"(Bearer\s+)[A-Za-z0-9._\-]+", re.IGNORECASE),
    re.compile(r"(access[_-]?token['\"]?\s*[:=]\s*['\"]?)[^'\"\s,]+", re.IGNORECASE),
    re.compile(r"(api[_-]?key['\"]?\s*[:=]\s*['\"]?)[^'\"\s,]+", re.IGNORECASE),
    re.compile(r"(secret['\"]?\s*[:=]\s*['\"]?)[^'\"\s,]+", re.IGNORECASE),
    re.compile(r"https://open\.feishu\.cn/open-apis/bot/v2/hook/[A-Za-z0-9._\-]+", re.IGNORECASE),
]


def redact(value: object) -> str:
    text = str(value)
    for pattern in SECRET_PATTERNS:
        if pattern.pattern.startswith("https://open"):
            text = pattern.sub("https://open.feishu.cn/open-apis/bot/v2/hook/[REDACTED]", text)
        else:
            text = pattern.sub(r"\1[REDACTED]", text)
    return text

