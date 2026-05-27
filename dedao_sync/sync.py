from __future__ import annotations

import logging
import socket
from datetime import datetime
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
from .summarizer import SummaryError, create_summary_service


LOGGER = logging.getLogger(__name__)


def default_db_path(root_dir: Path) -> Path:
    return root_dir / "data" / "dedao_sync.sqlite3"


def default_lock_path(root_dir: Path) -> Path:
    return root_dir / "data" / "dedao_sync.lock"


def new_run_report(**kwargs) -> RunReport:
    report = RunReport(started_at=datetime.now(), **kwargs)
    report.metadata["host"] = socket.gethostname()
    return report


def final_run_status(report: RunReport) -> str:
    attention_count = report.failed_count + report.missing_transcript_count + report.summary_failed_count
    return "success" if attention_count == 0 else "partial_failed"


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
        report.finished_at = datetime.now()
        repo.finish_run(run_id, report, "\n".join(result.errors))
    else:
        report.status = "success"
        report.finished_at = datetime.now()
        repo.finish_run(run_id, report)

    notifier = FeishuNotifier(load_feishu_credentials(config.feishu))
    try:
        notifier.send_run_report(report)
    except Exception:
        # Notification must not affect the main flow.
        pass
    return report, run_id


def run_sync(
    config_path: str | Path = "config.yaml",
    *,
    column_name: str | None = None,
    dry_run: bool = False,
    crawler: DedaoCrawler | None = None,
    summary_service=None,
    notifier: FeishuNotifier | None = None,
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
    notifier = notifier or FeishuNotifier(load_feishu_credentials(config.feishu))
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
            report.failures.append(str(exc))
            return report, run_id

        preflight = PreflightChecker(config, require_auth=True, require_browser=crawler is None).check()
        if not preflight.ok:
            report.status = STATUS_PREFLIGHT_FAILED
            report.failed_count = len(preflight.errors)
            report.failures.extend(preflight.errors)
            return report, run_id
        for warning in preflight.warnings:
            LOGGER.warning(warning)

        crawler = crawler or DedaoCrawler(config)
        try:
            if not crawler.check_login():
                report.status = STATUS_LOGIN_REQUIRED
                report.failed_count = 1
                report.failures.append("登录态失效，请重新运行 dedao-sync login")
                return report, run_id
        except CrawlerError as exc:
            report.status = STATUS_FAILED
            report.failed_count = 1
            report.failures.append(str(exc))
            return report, run_id

        writer = MarkdownWriter(config)
        summary_service = summary_service or create_summary_service(config.summary)

        for column in enabled_columns:
            LOGGER.info("checking column: %s", column.name)
            try:
                crawl_result = crawler.list_items(column)
            except Exception as exc:
                report.failed_count += 1
                report.failures.append(f"{column.name}: {exc}")
                LOGGER.exception("column failed: %s", column.name)
                continue

            if not crawl_result.items and not crawl_result.empty_but_valid:
                report.failed_count += 1
                report.failures.append(f"{column.name}: 页面解析失败，未发现内容列表")
                continue

            report.discovered_count += len(crawl_result.items)
            for item in crawl_result.items:
                existing = repo.find_existing(item)
                if existing:
                    report.skipped_count += 1
                    repo.add_run_item(run_id, int(existing["id"]), "skip", STATUS_SKIPPED, "already synced")
                    continue

                report.new_count += 1
                if dry_run:
                    report.skipped_count += 1
                    continue

                try:
                    detail = crawler.fetch_detail(item)
                    if not detail.has_transcript:
                        status = STATUS_EXTRACTOR_FAILED if detail.quality_reason else STATUS_MISSING_TRANSCRIPT
                        item_id = repo.upsert_item(
                            item,
                            status=status,
                            content_hash=detail.raw_html_hash,
                            has_transcript=False,
                            error_message=detail.quality_reason,
                        )
                        repo.add_run_item(run_id, item_id, "extract", status, detail.quality_reason)
                        report.missing_transcript_count += 1
                        continue

                    digest = content_hash(detail.transcript_text)
                    existing_by_hash = repo.find_existing(item, digest)
                    if existing_by_hash:
                        report.skipped_count += 1
                        repo.add_run_item(
                            run_id,
                            int(existing_by_hash["id"]),
                            "skip",
                            STATUS_SKIPPED,
                            "duplicate content_hash",
                        )
                        continue

                    summary_status = "disabled"
                    try:
                        summary = summary_service.summarize(detail)
                        summary_status = summary.status
                    except SummaryError as exc:
                        from .models import SummaryResult

                        summary = SummaryResult.empty(status=STATUS_SUMMARY_FAILED)
                        summary_status = STATUS_SUMMARY_FAILED
                        report.summary_failed_count += 1
                        LOGGER.warning("summary failed for %s: %s", item.title, exc)

                    path = writer.write(detail, summary)
                    status = STATUS_SYNCED if summary_status != STATUS_SUMMARY_FAILED else STATUS_SUMMARY_FAILED
                    item_id = repo.upsert_item(
                        item,
                        status=status,
                        content_hash=digest,
                        file_path=path,
                        has_transcript=True,
                        summary_status=summary_status,
                    )
                    repo.add_run_item(run_id, item_id, "sync", status, str(path))
                    report.success_count += 1
                    report.added_by_column.setdefault(column.name, []).append(item.title)
                except Exception as exc:
                    item_id = repo.upsert_item(item, status=STATUS_FAILED, error_message=str(exc))
                    repo.add_run_item(run_id, item_id, "sync", STATUS_FAILED, str(exc))
                    report.failed_count += 1
                    report.failures.append(f"{column.name}/{item.title}: {exc}")
                    LOGGER.exception("item failed: %s", item.title)

        report.status = final_run_status(report)
        return report, run_id
    finally:
        lock.release()
        report.finished_at = datetime.now()
        repo.finish_run(run_id, report, "\n".join(report.failures) if report.failures else None)
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
    notifier = notifier or FeishuNotifier(load_feishu_credentials(config.feishu))
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
            report.failures.append(str(exc))
            return report, run_id

        preflight = PreflightChecker(config, require_auth=True, require_browser=crawler is None).check()
        if not preflight.ok:
            report.status = STATUS_PREFLIGHT_FAILED
            report.failed_count = len(preflight.errors)
            report.failures.extend(preflight.errors)
            return report, run_id

        crawler = crawler or DedaoCrawler(config)
        try:
            if not crawler.check_login():
                report.status = STATUS_LOGIN_REQUIRED
                report.failed_count = 1
                report.failures.append("登录态失效，请重新运行 dedao-sync login")
                return report, run_id
        except CrawlerError as exc:
            report.status = STATUS_FAILED
            report.failed_count = 1
            report.failures.append(str(exc))
            return report, run_id

        writer = MarkdownWriter(config)
        summary_service = summary_service or create_summary_service(config.summary)
        for row in retry_rows:
            item = row_to_content_item(row)
            try:
                detail = crawler.fetch_detail(item)
                if not detail.has_transcript:
                    status = STATUS_EXTRACTOR_FAILED if detail.quality_reason else STATUS_MISSING_TRANSCRIPT
                    repo.upsert_item(item, status=status, content_hash=detail.raw_html_hash, error_message=detail.quality_reason)
                    repo.add_run_item(run_id, int(row["id"]), "retry", status, detail.quality_reason)
                    report.missing_transcript_count += 1
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
                    LOGGER.warning("summary failed for retry %s: %s", item.title, exc)
                path = writer.write(detail, summary)
                status = STATUS_SYNCED if summary_status != STATUS_SUMMARY_FAILED else STATUS_SUMMARY_FAILED
                item_id = repo.upsert_item(
                    item,
                    status=status,
                    content_hash=digest,
                    file_path=path,
                    has_transcript=True,
                    summary_status=summary_status,
                )
                repo.add_run_item(run_id, item_id, "retry", status, str(path))
                report.success_count += 1
                report.added_by_column.setdefault(item.column_name, []).append(item.title)
            except Exception as exc:
                repo.upsert_item(item, status=STATUS_FAILED, error_message=str(exc))
                repo.add_run_item(run_id, int(row["id"]), "retry", STATUS_FAILED, str(exc))
                report.failed_count += 1
                report.failures.append(f"{item.column_name}/{item.title}: {exc}")
        report.status = final_run_status(report)
        return report, run_id
    finally:
        lock.release()
        report.finished_at = datetime.now()
        repo.finish_run(run_id, report, "\n".join(report.failures) if report.failures else None)
        try:
            notifier.send_run_report(report)
        except Exception as exc:
            LOGGER.warning("feishu notification failed: %s", exc)


def run_resummarize(
    config_path: str | Path = "config.yaml",
    *,
    limit: int = 20,
    summary_service=None,
    notifier: FeishuNotifier | None = None,
) -> tuple[RunReport, int]:
    config = load_config(config_path)
    log_path = setup_logging(config.root_dir)
    report = new_run_report(log_path=log_path)
    repo = SyncRepository(default_db_path(config.root_dir))
    repo.migrate()
    run_id = repo.start_run(report)
    notifier = notifier or FeishuNotifier(load_feishu_credentials(config.feishu))
    writer = MarkdownWriter(config)
    summary_service = summary_service or create_summary_service(config.summary)
    rows = repo.list_items_needing_summary(limit=limit)
    report.discovered_count = len(rows)
    lock = RunLock(default_lock_path(config.root_dir))

    try:
        try:
            lock.acquire()
        except RunLockError as exc:
            report.status = STATUS_LOCKED
            report.failed_count = 1
            report.failures.append(str(exc))
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
                repo.upsert_item(item, status=STATUS_SUMMARY_FAILED, error_message=str(exc))
                repo.add_run_item(run_id, int(row["id"]), "resummarize", STATUS_SUMMARY_FAILED, str(exc))
                report.failed_count += 1
                report.summary_failed_count += 1
                report.failures.append(f"{item.column_name}/{item.title}: {exc}")
        report.status = final_run_status(report)
        return report, run_id
    finally:
        lock.release()
        report.finished_at = datetime.now()
        repo.finish_run(run_id, report, "\n".join(report.failures) if report.failures else None)
        try:
            notifier.send_run_report(report)
        except Exception as exc:
            LOGGER.warning("feishu notification failed: %s", exc)
