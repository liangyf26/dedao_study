from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import socket
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any

from .models import FeishuConfig, RunReport
from .security import redact
from .time_utils import now_local


STATUS_TITLES = {
    "success": "得到同步完成",
    "partial_failed": "得到同步部分失败",
    "locked": "得到同步跳过：已有任务运行中",
    "login_required": "得到同步失败：需要重新登录",
    "preflight_failed": "得到同步失败：预检查未通过",
}

MAX_DETAIL_LINES_PER_SECTION = 10


@dataclass(frozen=True)
class FeishuCredentials:
    webhook_url: str
    secret: str | None = None


class NotificationError(RuntimeError):
    pass


def make_feishu_sign(timestamp: int, secret: str) -> str:
    string_to_sign = f"{timestamp}\n{secret}".encode("utf-8")
    digest = hmac.new(string_to_sign, b"", digestmod=hashlib.sha256).digest()
    return base64.b64encode(digest).decode("utf-8")


def load_feishu_credentials(config: FeishuConfig) -> FeishuCredentials | None:
    if not config.enabled:
        return None
    webhook = os.environ.get(config.webhook_url_env, "").strip()
    if not webhook:
        return None
    secret = os.environ.get(config.secret_env, "").strip() or None
    return FeishuCredentials(webhook_url=webhook, secret=secret)


def format_run_report(report: RunReport, *, include_titles: bool = True) -> str:
    finished = report.finished_at or now_local()
    duration = ""
    if report.finished_at:
        seconds = max(0, int((report.finished_at - report.started_at).total_seconds()))
        duration = f"{seconds}s"
    lines = [
        STATUS_TITLES.get(report.status, "得到同步异常"),
        "",
        f"执行时间：{finished.strftime('%Y-%m-%d %H:%M:%S')}",
        f"运行机器：{report.metadata.get('host') or socket.gethostname()}",
        f"耗时：{duration or 'unknown'}",
        f"状态：{report.status}",
        f"总栏目数：{report.total_columns}",
        f"发现条目数：{report.discovered_count}",
        f"新增文章数：{report.new_count}",
        f"跳过文章数：{report.skipped_count}",
        f"成功文章数：{report.success_count}",
        f"网页请求数：{report.request_count}",
        f"失败文章数：{report.failed_count}",
        f"无文字稿文章数：{report.missing_transcript_count}",
        f"摘要失败数：{report.summary_failed_count}",
    ]
    has_item_details = bool(report.added_by_column or report.missing_by_column or report.summary_failed_by_column or report.failures)
    if not include_titles and has_item_details:
        lines.extend(["", "明细：已按配置隐藏标题；请在本机用 list --runs / list --failed 查看。"])
    if include_titles and report.added_by_column:
        lines.extend(["", "新增内容："])
        _append_bucket_lines(lines, report.added_by_column)
    if include_titles and report.missing_by_column:
        lines.extend(["", "无文字稿/待处理："])
        _append_bucket_lines(lines, report.missing_by_column)
    if include_titles and report.summary_failed_by_column:
        lines.extend(["", "摘要失败："])
        _append_bucket_lines(lines, report.summary_failed_by_column)
    if include_titles and report.failures:
        lines.extend(["", "失败："])
        for failure in report.failures[:MAX_DETAIL_LINES_PER_SECTION]:
            lines.append(f"- {redact(failure)}")
        if len(report.failures) > MAX_DETAIL_LINES_PER_SECTION:
            lines.append(f"- 还有 {len(report.failures) - MAX_DETAIL_LINES_PER_SECTION} 条，详见日志或 list 命令。")
    if report.log_path:
        lines.extend(["", f"日志：{report.log_path}"])
    return "\n".join(lines)


def _append_bucket_lines(lines: list[str], bucket: dict[str, list[str]]) -> None:
    emitted = 0
    total = sum(len(titles) for titles in bucket.values())
    for column, titles in bucket.items():
        for title in titles:
            if emitted >= MAX_DETAIL_LINES_PER_SECTION:
                lines.append(f"- 还有 {total - emitted} 条，详见日志或 list 命令。")
                return
            lines.append(f"- {column}：{redact(title)}")
            emitted += 1


class FeishuNotifier:
    def __init__(self, credentials: FeishuCredentials | None, *, timeout_seconds: int = 10, include_titles: bool = True):
        self.credentials = credentials
        self.timeout_seconds = timeout_seconds
        self.include_titles = include_titles

    def build_payload(self, report: RunReport) -> dict[str, Any] | None:
        if self.credentials is None:
            return None
        payload: dict[str, Any] = {
            "msg_type": "text",
            "content": {"text": format_run_report(report, include_titles=self.include_titles)},
        }
        if self.credentials.secret:
            timestamp = int(time.time())
            payload["timestamp"] = str(timestamp)
            payload["sign"] = make_feishu_sign(timestamp, self.credentials.secret)
        return payload

    def send_run_report(self, report: RunReport) -> bool:
        if self.credentials is None:
            return False
        payload = self.build_payload(report)
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            self.credentials.webhook_url,
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
                body = response.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            try:
                error_body = exc.read().decode("utf-8", errors="replace")
            except Exception:
                error_body = str(exc)
            raise NotificationError(f"feishu HTTP {exc.code}: {redact(error_body)[:500]}") from exc
        except urllib.error.URLError as exc:
            raise NotificationError(redact(exc)) from exc
        try:
            parsed = json.loads(body)
        except json.JSONDecodeError:
            parsed = {}
        if parsed.get("code", 0) not in (0, None):
            raise NotificationError(redact(body))
        return True
