from __future__ import annotations

import argparse
import json
import shutil
import sys
from datetime import datetime
from pathlib import Path

from .browser import BrowserDependencyError, BrowserSession
from .config import ConfigError, load_config
from .crawler import CrawlerError, DedaoCrawler
from .doctor import doctor_checks_to_dicts, doctor_exit_code, run_doctor
from .models import (
    ContentDetail,
    ContentItem,
    RunReport,
    STATUS_EXTRACTOR_FAILED,
    STATUS_FAILED,
    STATUS_MISSING_TRANSCRIPT,
    STATUS_SUMMARY_FAILED,
    STATUS_TRANSCRIPTION_FAILED,
)
from .notifier import FeishuNotifier, NotificationError, load_feishu_credentials
from .preflight import PreflightChecker
from .repository import SyncRepository
from .sync import default_db_path, run_preflight, run_resummarize, run_retry_failed, run_sync
from .snapshot import parse_snapshot
from .summarizer import SummaryError, create_summary_service


ROOT_DIR = Path(__file__).resolve().parents[1]


def _add_config_arg(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--config", default="config.yaml", help="Path to config file")


def cmd_init(args: argparse.Namespace) -> int:
    root = Path(args.root).resolve()
    created: list[Path] = []
    for source_name, target_name in (("config.example.yaml", "config.yaml"), (".env.example", ".env")):
        source = ROOT_DIR / source_name
        target = root / target_name
        if target.exists() and not args.force:
            print(f"exists, skipped: {target}")
            continue
        shutil.copyfile(source, target)
        created.append(target)
        print(f"created: {target}")
    (root / "data" / "auth").mkdir(parents=True, exist_ok=True)
    (root / "logs").mkdir(exist_ok=True)
    if not created:
        print("nothing created; use --force to overwrite config.yaml/.env")
    return 0


def cmd_login(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    try:
        path = BrowserSession(config).login()
    except BrowserDependencyError as exc:
        print(exc, file=sys.stderr)
        return 2
    print(f"登录态已保存：{path}")
    return 0


def cmd_preflight(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    result = PreflightChecker(
        config,
        require_auth=not args.no_auth,
        require_browser=not args.no_browser,
        probe_vault_write=args.probe_vault_write,
    ).check()
    for warning in result.warnings:
        print(f"WARNING: {warning}")
    for error in result.errors:
        print(f"ERROR: {error}", file=sys.stderr)
    return 0 if result.ok else 1


def cmd_doctor(args: argparse.Namespace) -> int:
    checks = run_doctor(args.config, require_auth=not args.no_auth)
    if args.json:
        print(json.dumps(doctor_checks_to_dicts(checks), ensure_ascii=False, indent=2))
    else:
        for check in checks:
            label = check.status.upper()
            print(f"{label:5} {check.name}: {check.message}")
    return doctor_exit_code(checks)


def cmd_check(args: argparse.Namespace) -> int:
    report, _ = run_sync(args.config, column_name=args.column, dry_run=True)
    print(f"check status: {report.status}")
    for failure in report.failures:
        print(f"ERROR: {failure}", file=sys.stderr)
    return 0 if report.status == "success" else 1


def cmd_sync(args: argparse.Namespace) -> int:
    report, _ = run_sync(args.config, column_name=args.column, dry_run=args.dry_run)
    print(f"sync status: {report.status}")
    for failure in report.failures:
        print(f"ERROR: {failure}", file=sys.stderr)
    return 0 if report.status == "success" else 1


def cmd_retry_failed(args: argparse.Namespace) -> int:
    report, _ = run_retry_failed(args.config, limit=args.limit)
    print(f"retry status: {report.status}")
    for failure in report.failures:
        print(f"ERROR: {failure}", file=sys.stderr)
    return 0 if report.status == "success" else 1


def cmd_resummarize(args: argparse.Namespace) -> int:
    report, _ = run_resummarize(args.config, limit=args.limit)
    print(f"resummarize status: {report.status}")
    for failure in report.failures:
        print(f"ERROR: {failure}", file=sys.stderr)
    return 0 if report.status == "success" else 1


def cmd_notify_test(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    notifier = FeishuNotifier(load_feishu_credentials(config.feishu))
    report = RunReport(
        started_at=datetime.now(),
        finished_at=datetime.now(),
        status="success",
        total_columns=sum(1 for column in config.dedao.columns if column.enabled),
        log_path=Path("logs/notify-test.log"),
    )
    try:
        sent = notifier.send_run_report(report)
    except NotificationError as exc:
        print(f"notification failed: {exc}", file=sys.stderr)
        return 1
    print("notification sent" if sent else "notification skipped: webhook not configured")
    return 0


def cmd_summary_test(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    if not config.summary.enabled:
        print("summary skipped: summary.enabled=false")
        return 0
    detail = ContentDetail(
        item=ContentItem(
            source_url="summary-test",
            detail_url="summary-test",
            column_name="摘要测试",
            title="摘要服务连通性测试",
            published_at=datetime.now().strftime("%Y-%m-%d"),
        ),
        transcript_text=(
            "摘要服务连通性测试。\n\n"
            "第一段说明：这是本地构造的短文本，用于验证模型接口能返回结构化卡片笔记。\n\n"
            "第二段说明：好的输出应该包含原子卡片、永久笔记、关联主题、行动观察、复习问题和关键词。\n\n"
            "第三段说明：如果接口不可用、密钥错误、网络被拦截，命令应清楚失败，不打印完整 traceback。"
        ),
        has_transcript=True,
    )
    try:
        summary = create_summary_service(config.summary).summarize(detail)
    except SummaryError as exc:
        print(f"summary failed: {exc}", file=sys.stderr)
        return 1
    print("summary ok")
    print(f"atomic_cards={len(summary.atomic_cards)}")
    print(f"keywords={','.join(summary.keywords[:8])}")
    return 0


def cmd_inspect(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    output_dir = Path(args.output_dir)
    if not output_dir.is_absolute():
        output_dir = config.root_dir / output_dir
    try:
        path = DedaoCrawler(config).inspect_page(args.url, output_dir)
    except CrawlerError as exc:
        print(exc, file=sys.stderr)
        return 2
    print(f"page html saved: {path}")
    print(f"visible text saved: {path.with_suffix('.txt')}")
    return 0


def cmd_parse_snapshot(args: argparse.Namespace) -> int:
    result = parse_snapshot(
        args.html_path,
        title=args.title,
        column_name=args.column,
        source_url=args.url,
        write_transcript=args.write_transcript,
    )
    detail = result.detail
    if args.json:
        payload = {
            "has_transcript": detail.has_transcript,
            "quality_reason": detail.quality_reason or "",
            "transcript_chars": len(detail.transcript_text),
            "candidate_items": result.candidate_count,
            "transcript_path": str(result.transcript_path) if result.transcript_path else None,
            "transcript_candidates": [
                {
                    "index": candidate.index,
                    "selected": candidate.selected,
                    "ok": candidate.ok,
                    "reason": candidate.reason or "ok",
                    "chars": candidate.length,
                    "paragraphs": candidate.paragraph_count,
                    "noise": candidate.noise_hits,
                    "preview": candidate.preview,
                }
                for candidate in result.transcript_candidates
            ],
            "item_candidates": [
                {
                    "index": index,
                    "dedao_id": item.dedao_id or "",
                    "title": item.title,
                    "url": item.detail_url,
                }
                for index, item in enumerate(result.item_candidates, start=1)
            ],
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0 if detail.has_transcript else 1

    print(f"has_transcript: {detail.has_transcript}")
    print(f"quality_reason: {detail.quality_reason or ''}")
    print(f"transcript_chars: {len(detail.transcript_text)}")
    print(f"candidate_items: {result.candidate_count}")
    if args.show_candidates:
        print("transcript_candidates:")
        for candidate in result.transcript_candidates:
            marker = "*" if candidate.selected else "-"
            reason = candidate.reason or "ok"
            print(
                f"{marker} #{candidate.index} ok={candidate.ok} reason={reason} "
                f"chars={candidate.length} paragraphs={candidate.paragraph_count} "
                f"noise={candidate.noise_hits} preview={candidate.preview}"
            )
    if args.show_items:
        print("item_candidates:")
        for index, item in enumerate(result.item_candidates, start=1):
            print(f"- #{index} id={item.dedao_id or ''} title={item.title} url={item.detail_url}")
    if result.transcript_path:
        print(f"transcript saved: {result.transcript_path}")
    return 0 if detail.has_transcript else 1


def cmd_list(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    repo = SyncRepository(default_db_path(config.root_dir))
    repo.migrate()
    if args.run_id is not None:
        for row in repo.list_run_items(args.run_id, limit=args.limit):
            message = row["message"] or row["error_message"] or row["file_path"] or ""
            print(
                f"{row['run_id']}\t{row['item_id']}\t{row['action']}\t{row['run_item_status']}\t"
                f"{row['column_name'] or ''}\t{row['title'] or ''}\t{message}"
            )
        return 0
    if args.runs:
        for row in repo.list_runs(limit=args.limit):
            print(
                f"{row['id']}\t{row['started_at']}\t{row['status']}\t"
                f"columns={row['total_columns']}\tdiscovered={row['discovered_count']}\t"
                f"new={row['new_count']}\tsuccess={row['success_count']}\tfailed={row['failed_count']}\t"
                f"log={row['log_path'] or ''}"
            )
        return 0
    statuses: list[str] = []
    if args.failed:
        statuses.extend(
            [
                STATUS_FAILED,
                STATUS_EXTRACTOR_FAILED,
                STATUS_MISSING_TRANSCRIPT,
                STATUS_SUMMARY_FAILED,
                STATUS_TRANSCRIPTION_FAILED,
            ]
        )
    statuses.extend(args.status or [])
    if statuses:
        rows = repo.list_items_by_status(tuple(dict.fromkeys(statuses)), limit=args.limit)
    else:
        rows = repo.list_items(limit=args.limit)
    for row in rows:
        message = row["error_message"] or row["file_path"] or ""
        print(f"{row['id']}\t{row['status']}\t{row['column_name']}\t{row['title']}\t{message}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="dedao-sync")
    sub = parser.add_subparsers(dest="command", required=True)

    init = sub.add_parser("init", help="Create config.yaml, .env, and local runtime directories")
    init.add_argument("--root", default=".")
    init.add_argument("--force", action="store_true", help="Overwrite existing config.yaml/.env")
    init.set_defaults(func=cmd_init)

    login = sub.add_parser("login", help="Open browser and save Dedao login state")
    _add_config_arg(login)
    login.set_defaults(func=cmd_login)

    preflight = sub.add_parser("preflight", help="Validate local config and runtime prerequisites")
    _add_config_arg(preflight)
    preflight.add_argument("--no-auth", action="store_true", help="Do not require saved login state")
    preflight.add_argument("--no-browser", action="store_true", help="Do not require Playwright during preflight")
    preflight.add_argument("--probe-vault-write", action="store_true", help="Create and remove a temp file in the Obsidian output directory")
    preflight.set_defaults(func=cmd_preflight)

    doctor = sub.add_parser("doctor", help="Show runtime diagnostics for config, auth, env vars, and dependencies")
    _add_config_arg(doctor)
    doctor.add_argument("--no-auth", action="store_true", help="Do not require saved login state")
    doctor.add_argument("--json", action="store_true", help="Output machine-readable JSON")
    doctor.set_defaults(func=cmd_doctor)

    check = sub.add_parser("check", help="Check safety prerequisites before crawling")
    _add_config_arg(check)
    check.add_argument("--column", help="Limit check to one column")
    check.set_defaults(func=cmd_check)

    sync = sub.add_parser("sync", help="Run sync workflow")
    _add_config_arg(sync)
    sync.add_argument("--column", help="Limit sync to one column")
    sync.add_argument("--dry-run", action="store_true", help="Discover and record without writing notes")
    sync.set_defaults(func=cmd_sync)

    retry_failed = sub.add_parser("retry-failed", help="Retry failed items")
    _add_config_arg(retry_failed)
    retry_failed.add_argument("--limit", type=int, default=20)
    retry_failed.set_defaults(func=cmd_retry_failed)

    resummarize = sub.add_parser("resummarize", help="Regenerate summaries")
    _add_config_arg(resummarize)
    resummarize.add_argument("--limit", type=int, default=20)
    resummarize.set_defaults(func=cmd_resummarize)

    notify = sub.add_parser("notify-test", help="Send a Feishu test notification")
    _add_config_arg(notify)
    notify.set_defaults(func=cmd_notify_test)

    summary_test = sub.add_parser("summary-test", help="Send a small sample transcript to the configured summary model")
    _add_config_arg(summary_test)
    summary_test.set_defaults(func=cmd_summary_test)

    inspect = sub.add_parser("inspect-page", help="Save a Dedao page HTML/text snapshot for selector debugging")
    _add_config_arg(inspect)
    inspect.add_argument("url")
    inspect.add_argument("--output-dir", default="data/page_snapshots")
    inspect.set_defaults(func=cmd_inspect)

    parse_snapshot_cmd = sub.add_parser("parse-snapshot", help="Parse a saved page snapshot offline")
    parse_snapshot_cmd.add_argument("html_path")
    parse_snapshot_cmd.add_argument("--title", required=True)
    parse_snapshot_cmd.add_argument("--column", required=True)
    parse_snapshot_cmd.add_argument("--url", required=True)
    parse_snapshot_cmd.add_argument("--write-transcript", action="store_true")
    parse_snapshot_cmd.add_argument("--show-candidates", action="store_true")
    parse_snapshot_cmd.add_argument("--show-items", action="store_true")
    parse_snapshot_cmd.add_argument("--json", action="store_true", help="Output machine-readable parse diagnostics")
    parse_snapshot_cmd.set_defaults(func=cmd_parse_snapshot)

    list_cmd = sub.add_parser("list", help="List recent sync records")
    _add_config_arg(list_cmd)
    list_cmd.add_argument("--limit", type=int, default=20)
    list_cmd.add_argument("--runs", action="store_true", help="List recent workflow runs instead of content items")
    list_cmd.add_argument("--run-id", type=int, help="List item actions recorded for one workflow run")
    list_cmd.add_argument("--failed", action="store_true", help="List items that need manual attention or retry")
    list_cmd.add_argument("--status", action="append", help="List items matching a specific status; can be repeated")
    list_cmd.set_defaults(func=cmd_list)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
