from __future__ import annotations

import logging
import socket
import time
from pathlib import Path

from .config import load_config
from .crawler import CrawlerError, DedaoCrawler
from .logging_utils import setup_logging
from .locking import RunLock, RunLockError
from .markdown import MarkdownWriter, content_hash, extract_transcript_from_note
from .models import (
    ContentDetail,
    RunReport,
    STATUS_EXTRACTOR_FAILED,
    STATUS_FAILED,
    STATUS_LOGIN_REQUIRED,
    STATUS_LOCKED,
    STATUS_MISSING_TRANSCRIPT,
    STATUS_POLICY_BLOCKED,
    STATUS_PREFLIGHT_FAILED,
    STATUS_SKIPPED,
    STATUS_SUMMARY_FAILED,
    STATUS_SYNCED,
    STATUS_TRANSCRIPTION_FAILED,
)
from .notifier import FeishuNotifier, load_feishu_credentials
from .preflight import PreflightChecker
from .repository import SyncRepository
from .repository import row_to_content_item
from .security import redact
from .summarizer import DisabledSummaryService, SummaryError, create_summary_service
from .time_utils import now_local


LOGGER = logging.getLogger(__name__)

TRANSIENT_ERROR_PATTERNS = (
    "sslv3_alert_bad_record_mac",
    "bad record mac",
    "connection reset",
    "econnreset",
    "net::err_http2_protocol_error",
    "net::err_connection",
    "temporarily unavailable",
)


def default_db_path(root_dir: Path) -> Path:
    return root_dir / "data" / "dedao_sync.sqlite3"


def default_lock_path(root_dir: Path) -> Path:
    return root_dir / "data" / "dedao_sync.lock"


def new_run_report(**kwargs) -> RunReport:
    report = RunReport(started_at=now_local(), **kwargs)
    report.metadata["host"] = socket.gethostname()
    return report


def final_run_status(report: RunReport) -> str:
    attention_count = report.failed_count + report.missing_transcript_count + report.summary_failed_count
    return "success" if attention_count == 0 else "partial_failed"


def detail_failure_message(detail: ContentDetail) -> str | None:
    parts = []
    if detail.quality_reason:
        parts.append(detail.quality_reason)
    if detail.media_candidates:
        labels = sorted({candidate.mime_type or candidate.label or "media" for candidate in detail.media_candidates})
        suffix = f" ({', '.join(labels[:3])})" if labels else ""
        parts.append(f"media_candidates={len(detail.media_candidates)}{suffix}")
    if detail.diagnostic_path:
        parts.append(f"diagnostic_html={detail.diagnostic_path}")
    return "; ".join(parts) or None


def is_policy_blocked(detail: ContentDetail) -> bool:
    return bool(detail.quality_reason and detail.quality_reason.startswith("policy_blocked:"))


def add_report_item(bucket: dict[str, list[str]], column: str, title: str, note: object | None = None) -> None:
    text = title
    if note:
        text = f"{title}（{redact(note)}）"
    bucket.setdefault(column, []).append(text)


def is_transient_error(exc: Exception) -> bool:
    message = str(exc).lower()
    return any(pattern in message for pattern in TRANSIENT_ERROR_PATTERNS)


def fetch_detail_with_retry(crawler: DedaoCrawler, item, report: RunReport, *, attempts: int = 2) -> ContentDetail:
    last_error: Exception | None = None
    for attempt in range(1, attempts + 1):
        report.request_count += 1
        try:
            LOGGER.info("fetching detail: %s - %s", item.column_name, item.title)
            return crawler.fetch_detail(item)
        except Exception as exc:
            last_error = exc
            if attempt >= attempts or not is_transient_error(exc):
                raise
            LOGGER.warning(
                "transient detail fetch failed, retrying: %s - %s: %s",
                item.column_name,
                item.title,
                redact(exc),
            )
            time.sleep(2 * attempt)
    if last_error:
        raise last_error
    raise RuntimeError("detail fetch failed without an exception")


def run_preflight(config_path: str | Path = "config.yaml", *, require_auth: bool = True) -> tuple[RunReport, int | None]:
    config = load_config(config_path)
    log_path = setup_logging(config.root_dir)
    report = new_run_report(
        total_columns=sum(1 for column in config.dedao.columns if column.enabled),
        log_path=log_path,
    )
    repo = SyncRepository(default_db_path(config.root_dir))
    repo.migrate()
    run_id = repo.start_run(report)

    result = PreflightChecker(config, require_auth=require_auth).check()
    if not result.ok:
        report.status = STATUS_PREFLIGHT_FAILED
        report.failed_count = len(result.errors)
        report.failures.extend(result.errors)
        report.finished_at = now_local()
        repo.finish_run(run_id, report, "\n".join(result.errors))
    else:
        report.status = "success"
        report.finished_at = now_local()
        repo.finish_run(run_id, report)

    notifier = FeishuNotifier(load_feishu_credentials(config.feishu), include_titles=config.feishu.include_titles)
    try:
        notifier.send_run_report(report)
    except Exception as exc:
        # Notification must not affect the main flow.
        LOGGER.warning("feishu notification failed: %s", exc)
    return report, run_id


def run_sync(
    config_path: str | Path = "config.yaml",
    *,
    column_name: str | None = None,
    dry_run: bool = False,
    crawler: DedaoCrawler | None = None,
    summary_service=None,
    notifier: FeishuNotifier | None = None,
    send_notification: bool = True,
    limit: int | None = None,
    skip_summary: bool = False,
) -> tuple[RunReport, int]:
    config = load_config(config_path)
    log_path = setup_logging(config.root_dir)
    enabled_columns = [column for column in config.dedao.columns if column.enabled]
    if column_name:
        enabled_columns = [column for column in enabled_columns if column.name == column_name]
    report = new_run_report(total_columns=len(enabled_columns), log_path=log_path)

    repo = SyncRepository(default_db_path(config.root_dir))
    repo.migrate()
    run_id = repo.start_run(report)
    notifier = notifier or FeishuNotifier(load_feishu_credentials(config.feishu), include_titles=config.feishu.include_titles)
    lock = RunLock(default_lock_path(config.root_dir))

    try:
        if column_name and not enabled_columns:
            report.status = STATUS_PREFLIGHT_FAILED
            report.failed_count = 1
            report.failures.append(f"未找到启用的栏目：{column_name}")
            return report, run_id

        try:
            lock.acquire()
        except RunLockError as exc:
            report.status = STATUS_LOCKED
            report.failed_count = 1
            report.failures.append(redact(exc))
            return report, run_id

        preflight = PreflightChecker(
            config,
            require_auth=True,
            require_browser=crawler is None,
            require_feishu=config.feishu.enabled and send_notification,
        ).check()
        if not preflight.ok:
            report.status = STATUS_PREFLIGHT_FAILED
            report.failed_count = len(preflight.errors)
            report.failures.extend(preflight.errors)
            return report, run_id
        for warning in preflight.warnings:
            LOGGER.warning(warning)

        crawler = crawler or DedaoCrawler(config)
        try:
            report.request_count += 1
            if not crawler.check_login():
                report.status = STATUS_LOGIN_REQUIRED
                report.failed_count = 1
                report.failures.append("登录态失效，请重新运行 dedao-sync login")
                return report, run_id
        except CrawlerError as exc:
            report.status = STATUS_FAILED
            report.failed_count = 1
            report.failures.append(redact(exc))
            return report, run_id

        writer = MarkdownWriter(config)
        if skip_summary:
            LOGGER.info("summary disabled by command option")
            summary_service = DisabledSummaryService()
        else:
            summary_service = summary_service or create_summary_service(config.summary)

        processed_new_items = 0
        limit_reached = False

        for column in enabled_columns:
            LOGGER.info("checking column: %s", column.name)
            try:
                report.request_count += 1
                crawl_result = crawler.list_items(column)
            except Exception as exc:
                safe_error = redact(exc)
                report.failed_count += 1
                report.failures.append(redact(f"{column.name}: {exc}"))
                LOGGER.warning("column failed: %s: %s", column.name, safe_error)
                continue

            if not crawl_result.items and not crawl_result.empty_but_valid:
                report.failed_count += 1
                message = f"{column.name}: 页面解析失败，未发现内容列表"
                if crawl_result.diagnostic_path:
                    message = f"{message}; diagnostic_html={crawl_result.diagnostic_path}"
                else:
                    message = f"{message}; 可运行 inspect-page 保存页面快照后用 parse-snapshot 分析"
                report.failures.append(message)
                continue

            report.discovered_count += len(crawl_result.items)
            LOGGER.info("column discovered: %s items=%d", column.name, len(crawl_result.items))
            for index, item in enumerate(crawl_result.items, start=1):
                existing = repo.find_existing(item)
                if existing:
                    report.skipped_count += 1
                    repo.add_run_item(run_id, int(existing["id"]), "skip", STATUS_SKIPPED, "already synced")
                    LOGGER.info(
                        "item %d/%d skipped: %s - %s (already synced)",
                        index,
                        len(crawl_result.items),
                        column.name,
                        item.title,
                    )
                    continue

                if limit is not None and processed_new_items >= limit:
                    limit_reached = True
                    LOGGER.info("sync limit reached: %d new item(s); stopping", limit)
                    break

                processed_new_items += 1
                report.new_count += 1
                LOGGER.info("item %d/%d new: %s - %s", index, len(crawl_result.items), column.name, item.title)
                if dry_run:
                    report.skipped_count += 1
                    LOGGER.info("dry-run skipped write: %s - %s", column.name, item.title)
                    continue

                try:
                    detail = fetch_detail_with_retry(crawler, item, report)
                    synced_item = detail.item
                    if is_policy_blocked(detail):
                        failure_message = detail_failure_message(detail)
                        item_id = repo.upsert_item(
                            synced_item,
                            status=STATUS_POLICY_BLOCKED,
                            content_hash=detail.raw_html_hash,
                            has_transcript=False,
                            error_message=failure_message,
                        )
                        repo.add_run_item(run_id, item_id, "policy", STATUS_POLICY_BLOCKED, failure_message)
                        report.failed_count += 1
                        report.failures.append(redact(f"{synced_item.column_name}/{synced_item.title}: {failure_message}"))
                        LOGGER.warning("policy blocked: %s - %s: %s", synced_item.column_name, synced_item.title, failure_message)
                        continue
                    if not detail.has_transcript:
                        status = STATUS_EXTRACTOR_FAILED if detail.quality_reason else STATUS_MISSING_TRANSCRIPT
                        failure_message = detail_failure_message(detail)
                        item_id = repo.upsert_item(
                            synced_item,
                            status=status,
                            content_hash=detail.raw_html_hash,
                            has_transcript=False,
                            error_message=failure_message,
                        )
                        repo.add_run_item(run_id, item_id, "extract", status, failure_message)
                        report.missing_transcript_count += 1
                        add_report_item(report.missing_by_column, synced_item.column_name, synced_item.title, failure_message)
                        LOGGER.warning("missing transcript: %s - %s: %s", synced_item.column_name, synced_item.title, failure_message)
                        continue

                    digest = content_hash(detail.transcript_text)
                    existing_by_hash = repo.find_existing(synced_item, digest)
                    if existing_by_hash:
                        report.skipped_count += 1
                        repo.add_run_item(
                            run_id,
                            int(existing_by_hash["id"]),
                            "skip",
                            STATUS_SKIPPED,
                            "duplicate content_hash",
                        )
                        LOGGER.info("duplicate content skipped: %s - %s", synced_item.column_name, synced_item.title)
                        continue

                    summary_status = "disabled"
                    try:
                        LOGGER.info("summarizing: %s - %s chars=%d", synced_item.column_name, synced_item.title, len(detail.transcript_text))
                        summary = summary_service.summarize(detail)
                        summary_status = summary.status
                    except SummaryError as exc:
                        from .models import SummaryResult

                        summary = SummaryResult.empty(status=STATUS_SUMMARY_FAILED)
                        summary_status = STATUS_SUMMARY_FAILED
                        report.summary_failed_count += 1
                        add_report_item(report.summary_failed_by_column, synced_item.column_name, synced_item.title, exc)
                        LOGGER.warning("summary failed for %s: %s", item.title, exc)

                    LOGGER.info("writing note: %s - %s", synced_item.column_name, synced_item.title)
                    path = writer.write(detail, summary)
                    status = STATUS_SYNCED if summary_status != STATUS_SUMMARY_FAILED else STATUS_SUMMARY_FAILED
                    item_id = repo.upsert_item(
                        synced_item,
                        status=status,
                        content_hash=digest,
                        file_path=path,
                        has_transcript=True,
                        summary_status=summary_status,
                    )
                    repo.add_run_item(run_id, item_id, "sync", status, str(path))
                    report.success_count += 1
                    report.added_by_column.setdefault(column.name, []).append(synced_item.title)
                    LOGGER.info("synced: %s - %s -> %s", synced_item.column_name, synced_item.title, path)
                except Exception as exc:
                    safe_error = redact(exc)
                    item_id = repo.upsert_item(item, status=STATUS_FAILED, error_message=safe_error)
                    repo.add_run_item(run_id, item_id, "sync", STATUS_FAILED, safe_error)
                    report.failed_count += 1
                    report.failures.append(redact(f"{column.name}/{item.title}: {exc}"))
                    LOGGER.exception("item failed: %s", item.title)
            if limit_reached:
                break

        report.status = final_run_status(report)
        return report, run_id
    finally:
        lock.release()
        report.finished_at = now_local()
        repo.finish_run(run_id, report, "\n".join(report.failures) if report.failures else None)
        if send_notification:
            try:
                notifier.send_run_report(report)
            except Exception as exc:
                LOGGER.warning("feishu notification failed: %s", exc)


def run_retry_failed(
    config_path: str | Path = "config.yaml",
    *,
    limit: int = 20,
    crawler: DedaoCrawler | None = None,
    summary_service=None,
    notifier: FeishuNotifier | None = None,
) -> tuple[RunReport, int]:
    config = load_config(config_path)
    log_path = setup_logging(config.root_dir)
    report = new_run_report(log_path=log_path)
    repo = SyncRepository(default_db_path(config.root_dir))
    repo.migrate()
    run_id = repo.start_run(report)
    notifier = notifier or FeishuNotifier(load_feishu_credentials(config.feishu), include_titles=config.feishu.include_titles)
    lock = RunLock(default_lock_path(config.root_dir))

    retry_rows = repo.list_items_by_status(
        (
            STATUS_FAILED,
            STATUS_EXTRACTOR_FAILED,
            STATUS_MISSING_TRANSCRIPT,
            STATUS_SUMMARY_FAILED,
            STATUS_TRANSCRIPTION_FAILED,
        ),
        limit=limit,
    )
    report.discovered_count = len(retry_rows)

    try:
        try:
            lock.acquire()
        except RunLockError as exc:
            report.status = STATUS_LOCKED
            report.failed_count = 1
            report.failures.append(redact(exc))
            return report, run_id

        preflight = PreflightChecker(
            config,
            require_auth=True,
            require_browser=crawler is None,
            require_feishu=config.feishu.enabled,
        ).check()
        if not preflight.ok:
            report.status = STATUS_PREFLIGHT_FAILED
            report.failed_count = len(preflight.errors)
            report.failures.extend(preflight.errors)
            return report, run_id

        crawler = crawler or DedaoCrawler(config)
        try:
            report.request_count += 1
            if not crawler.check_login():
                report.status = STATUS_LOGIN_REQUIRED
                report.failed_count = 1
                report.failures.append("登录态失效，请重新运行 dedao-sync login")
                return report, run_id
        except CrawlerError as exc:
            report.status = STATUS_FAILED
            report.failed_count = 1
            report.failures.append(redact(exc))
            return report, run_id

        writer = MarkdownWriter(config)
        summary_service = summary_service or create_summary_service(config.summary)
        for row in retry_rows:
            item = row_to_content_item(row)
            try:
                if row["status"] == STATUS_SUMMARY_FAILED and row["file_path"] and int(row["has_transcript"] or 0):
                    path = Path(str(row["file_path"]))
                    if path.exists():
                        transcript = extract_transcript_from_note(path.read_text(encoding="utf-8"))
                        if not transcript:
                            raise ValueError("全文稿 section not found")
                        detail = ContentDetail(
                            item=item,
                            transcript_text=transcript,
                            has_transcript=True,
                            raw_html_hash=row["content_hash"],
                        )
                        try:
                            summary = summary_service.summarize(detail)
                        except SummaryError as exc:
                            report.summary_failed_count += 1
                            add_report_item(report.summary_failed_by_column, item.column_name, item.title, exc)
                            repo.upsert_item(
                                item,
                                status=STATUS_SUMMARY_FAILED,
                                content_hash=row["content_hash"],
                                file_path=path,
                                has_transcript=True,
                                summary_status=STATUS_SUMMARY_FAILED,
                                error_message=redact(exc),
                            )
                            repo.add_run_item(run_id, int(row["id"]), "retry-summary", STATUS_SUMMARY_FAILED, redact(exc))
                            LOGGER.warning("summary failed for retry %s: %s", item.title, exc)
                            continue
                        writer.overwrite(path, detail, summary)
                        item_id = repo.upsert_item(
                            item,
                            status=STATUS_SYNCED,
                            content_hash=row["content_hash"],
                            file_path=path,
                            has_transcript=True,
                            summary_status=summary.status,
                        )
                        repo.add_run_item(run_id, item_id, "retry-summary", STATUS_SYNCED, str(path))
                        report.success_count += 1
                        report.added_by_column.setdefault(item.column_name, []).append(item.title)
                        continue

                detail = fetch_detail_with_retry(crawler, item, report)
                synced_item = detail.item
                if is_policy_blocked(detail):
                    failure_message = detail_failure_message(detail)
                    repo.upsert_item(
                        synced_item,
                        status=STATUS_POLICY_BLOCKED,
                        content_hash=detail.raw_html_hash,
                        error_message=failure_message,
                    )
                    repo.add_run_item(run_id, int(row["id"]), "policy", STATUS_POLICY_BLOCKED, failure_message)
                    report.failed_count += 1
                    report.failures.append(redact(f"{synced_item.column_name}/{synced_item.title}: {failure_message}"))
                    continue
                if not detail.has_transcript:
                    status = STATUS_EXTRACTOR_FAILED if detail.quality_reason else STATUS_MISSING_TRANSCRIPT
                    failure_message = detail_failure_message(detail)
                    repo.upsert_item(
                        synced_item,
                        status=status,
                        content_hash=detail.raw_html_hash,
                        error_message=failure_message,
                    )
                    repo.add_run_item(run_id, int(row["id"]), "retry", status, failure_message)
                    report.missing_transcript_count += 1
                    add_report_item(report.missing_by_column, synced_item.column_name, synced_item.title, failure_message)
                    continue
                digest = content_hash(detail.transcript_text)
                summary_status = "disabled"
                try:
                    summary = summary_service.summarize(detail)
                    summary_status = summary.status
                except SummaryError as exc:
                    from .models import SummaryResult

                    summary = SummaryResult.empty(status=STATUS_SUMMARY_FAILED)
                    summary_status = STATUS_SUMMARY_FAILED
                    report.summary_failed_count += 1
                    add_report_item(report.summary_failed_by_column, synced_item.column_name, synced_item.title, exc)
                    LOGGER.warning("summary failed for retry %s: %s", item.title, exc)
                path = writer.write(detail, summary)
                status = STATUS_SYNCED if summary_status != STATUS_SUMMARY_FAILED else STATUS_SUMMARY_FAILED
                item_id = repo.upsert_item(
                    synced_item,
                    status=status,
                    content_hash=digest,
                    file_path=path,
                    has_transcript=True,
                    summary_status=summary_status,
                )
                repo.add_run_item(run_id, item_id, "retry", status, str(path))
                report.success_count += 1
                report.added_by_column.setdefault(synced_item.column_name, []).append(synced_item.title)
            except Exception as exc:
                safe_error = redact(exc)
                repo.upsert_item(item, status=STATUS_FAILED, error_message=safe_error)
                repo.add_run_item(run_id, int(row["id"]), "retry", STATUS_FAILED, safe_error)
                report.failed_count += 1
                report.failures.append(redact(f"{item.column_name}/{item.title}: {exc}"))
        report.status = final_run_status(report)
        return report, run_id
    finally:
        lock.release()
        report.finished_at = now_local()
        repo.finish_run(run_id, report, "\n".join(report.failures) if report.failures else None)
        try:
            notifier.send_run_report(report)
        except Exception as exc:
            LOGGER.warning("feishu notification failed: %s", exc)


def run_resummarize(
    config_path: str | Path = "config.yaml",
    *,
    limit: int = 20,
    include_synced: bool = False,
    summary_service=None,
    notifier: FeishuNotifier | None = None,
) -> tuple[RunReport, int]:
    config = load_config(config_path)
    log_path = setup_logging(config.root_dir)
    report = new_run_report(log_path=log_path)
    repo = SyncRepository(default_db_path(config.root_dir))
    repo.migrate()
    run_id = repo.start_run(report)
    notifier = notifier or FeishuNotifier(load_feishu_credentials(config.feishu), include_titles=config.feishu.include_titles)
    writer = MarkdownWriter(config)
    summary_service = summary_service or create_summary_service(config.summary)
    rows = repo.list_items_needing_summary(limit=limit, include_synced=include_synced)
    report.discovered_count = len(rows)
    lock = RunLock(default_lock_path(config.root_dir))

    try:
        try:
            lock.acquire()
        except RunLockError as exc:
            report.status = STATUS_LOCKED
            report.failed_count = 1
            report.failures.append(redact(exc))
            return report, run_id

        preflight = PreflightChecker(
            config,
            require_auth=False,
            require_browser=False,
            require_feishu=config.feishu.enabled,
        ).check()
        if not preflight.ok:
            report.status = STATUS_PREFLIGHT_FAILED
            report.failed_count = len(preflight.errors)
            report.failures.extend(preflight.errors)
            return report, run_id

        for row in rows:
            item = row_to_content_item(row)
            path = Path(str(row["file_path"]))
            try:
                if not path.exists():
                    raise FileNotFoundError(path)
                transcript = extract_transcript_from_note(path.read_text(encoding="utf-8"))
                if not transcript:
                    raise ValueError("全文稿 section not found")
                detail = ContentDetail(
                    item=item,
                    transcript_text=transcript,
                    has_transcript=True,
                    raw_html_hash=row["content_hash"],
                )
                summary = summary_service.summarize(detail)
                writer.overwrite(path, detail, summary)
                item_id = repo.upsert_item(
                    item,
                    status=STATUS_SYNCED,
                    content_hash=row["content_hash"],
                    file_path=path,
                    has_transcript=True,
                    summary_status=summary.status,
                )
                repo.add_run_item(run_id, item_id, "resummarize", STATUS_SYNCED, str(path))
                report.success_count += 1
                report.added_by_column.setdefault(item.column_name, []).append(item.title)
            except Exception as exc:
                safe_error = redact(exc)
                repo.upsert_item(
                    item,
                    status=STATUS_SUMMARY_FAILED,
                    content_hash=row["content_hash"],
                    file_path=path,
                    has_transcript=bool(row["has_transcript"] or path.exists()),
                    summary_status=STATUS_SUMMARY_FAILED,
                    error_message=safe_error,
                )
                repo.add_run_item(run_id, int(row["id"]), "resummarize", STATUS_SUMMARY_FAILED, safe_error)
                report.failed_count += 1
                report.summary_failed_count += 1
                add_report_item(report.summary_failed_by_column, item.column_name, item.title, exc)
                report.failures.append(redact(f"{item.column_name}/{item.title}: {exc}"))
        report.status = final_run_status(report)
        return report, run_id
    finally:
        lock.release()
        report.finished_at = now_local()
        repo.finish_run(run_id, report, "\n".join(report.failures) if report.failures else None)
        try:
            notifier.send_run_report(report)
        except Exception as exc:
            LOGGER.warning("feishu notification failed: %s", exc)
