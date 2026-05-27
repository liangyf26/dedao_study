from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from .models import ContentItem, RunReport


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class SyncRepository:
    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def migrate(self) -> None:
        with self.connect() as conn:
            conn.executescript(
                """
                PRAGMA journal_mode=WAL;

                CREATE TABLE IF NOT EXISTS items (
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  source_url TEXT NOT NULL,
                  dedao_id TEXT,
                  canonical_url TEXT,
                  column_name TEXT NOT NULL,
                  title TEXT NOT NULL,
                  published_at TEXT,
                  synced_at TEXT,
                  content_hash TEXT,
                  status TEXT NOT NULL,
                  file_path TEXT,
                  has_transcript INTEGER NOT NULL DEFAULT 0,
                  transcribed INTEGER NOT NULL DEFAULT 0,
                  summary_status TEXT,
                  error_message TEXT,
                  created_at TEXT NOT NULL,
                  updated_at TEXT NOT NULL
                );

                CREATE UNIQUE INDEX IF NOT EXISTS idx_items_source_url ON items(source_url);
                CREATE INDEX IF NOT EXISTS idx_items_dedao_id ON items(dedao_id);
                CREATE INDEX IF NOT EXISTS idx_items_dedao_column ON items(dedao_id, column_name);
                CREATE INDEX IF NOT EXISTS idx_items_content_hash ON items(content_hash);
                CREATE INDEX IF NOT EXISTS idx_items_column_name ON items(column_name);
                CREATE INDEX IF NOT EXISTS idx_items_status ON items(status);

                CREATE TABLE IF NOT EXISTS runs (
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  started_at TEXT NOT NULL,
                  finished_at TEXT,
                  status TEXT NOT NULL,
                  total_columns INTEGER NOT NULL DEFAULT 0,
                  discovered_count INTEGER NOT NULL DEFAULT 0,
                  new_count INTEGER NOT NULL DEFAULT 0,
                  skipped_count INTEGER NOT NULL DEFAULT 0,
                  success_count INTEGER NOT NULL DEFAULT 0,
                  failed_count INTEGER NOT NULL DEFAULT 0,
                  missing_transcript_count INTEGER NOT NULL DEFAULT 0,
                  summary_failed_count INTEGER NOT NULL DEFAULT 0,
                  log_path TEXT,
                  error_message TEXT
                );

                CREATE TABLE IF NOT EXISTS run_items (
                  run_id INTEGER NOT NULL,
                  item_id INTEGER NOT NULL,
                  action TEXT NOT NULL,
                  status TEXT NOT NULL,
                  message TEXT,
                  PRIMARY KEY (run_id, item_id)
                );
                """
            )

    def start_run(self, report: RunReport) -> int:
        with self.connect() as conn:
            cur = conn.execute(
                """
                INSERT INTO runs (
                    started_at, status, total_columns, log_path
                ) VALUES (?, ?, ?, ?)
                """,
                (
                    report.started_at.isoformat(timespec="seconds"),
                    report.status,
                    report.total_columns,
                    str(report.log_path) if report.log_path else None,
                ),
            )
            return int(cur.lastrowid)

    def finish_run(self, run_id: int, report: RunReport, error_message: str | None = None) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE runs
                SET finished_at = ?, status = ?, discovered_count = ?, new_count = ?,
                    skipped_count = ?, success_count = ?, failed_count = ?,
                    missing_transcript_count = ?, summary_failed_count = ?,
                    error_message = ?
                WHERE id = ?
                """,
                (
                    (report.finished_at or datetime.now()).isoformat(timespec="seconds"),
                    report.status,
                    report.discovered_count,
                    report.new_count,
                    report.skipped_count,
                    report.success_count,
                    report.failed_count,
                    report.missing_transcript_count,
                    report.summary_failed_count,
                    error_message,
                    run_id,
                ),
            )

    def find_existing(self, item: ContentItem, content_hash: str | None = None) -> sqlite3.Row | None:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM items WHERE source_url = ? LIMIT 1", (item.source_url,)).fetchone()
            if row:
                return row
            row = conn.execute("SELECT * FROM items WHERE canonical_url = ? LIMIT 1", (item.detail_url,)).fetchone()
            if row:
                return row
            if item.dedao_id:
                row = conn.execute(
                    "SELECT * FROM items WHERE dedao_id = ? AND column_name = ? LIMIT 1",
                    (item.dedao_id, item.column_name),
                ).fetchone()
                if row:
                    return row
            if content_hash:
                return conn.execute("SELECT * FROM items WHERE content_hash = ? LIMIT 1", (content_hash,)).fetchone()
        return None

    def upsert_item(
        self,
        item: ContentItem,
        *,
        status: str,
        content_hash: str | None = None,
        file_path: str | Path | None = None,
        has_transcript: bool = False,
        transcribed: bool = False,
        summary_status: str | None = None,
        error_message: str | None = None,
    ) -> int:
        now = utc_now_iso()
        existing = self.find_existing(item, content_hash)
        with self.connect() as conn:
            if existing:
                conn.execute(
                    """
                    UPDATE items
                    SET dedao_id = COALESCE(?, dedao_id),
                        canonical_url = COALESCE(?, canonical_url),
                        column_name = ?, title = ?, published_at = ?,
                        synced_at = CASE WHEN ? = 'synced' THEN ? ELSE synced_at END,
                        content_hash = COALESCE(?, content_hash), status = ?,
                        file_path = COALESCE(?, file_path), has_transcript = ?,
                        transcribed = ?, summary_status = ?, error_message = ?,
                        updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        item.dedao_id,
                        item.detail_url,
                        item.column_name,
                        item.title,
                        item.published_at,
                        status,
                        now,
                        content_hash,
                        status,
                        str(file_path) if file_path else None,
                        int(has_transcript),
                        int(transcribed),
                        summary_status,
                        error_message,
                        now,
                        existing["id"],
                    ),
                )
                return int(existing["id"])

            cur = conn.execute(
                """
                INSERT INTO items (
                    source_url, dedao_id, canonical_url, column_name, title, published_at,
                    synced_at, content_hash, status, file_path, has_transcript,
                    transcribed, summary_status, error_message, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    item.source_url,
                    item.dedao_id,
                    item.detail_url,
                    item.column_name,
                    item.title,
                    item.published_at,
                    now if status == "synced" else None,
                    content_hash,
                    status,
                    str(file_path) if file_path else None,
                    int(has_transcript),
                    int(transcribed),
                    summary_status,
                    error_message,
                    now,
                    now,
                ),
            )
            return int(cur.lastrowid)

    def add_run_item(self, run_id: int, item_id: int, action: str, status: str, message: str | None = None) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO run_items (run_id, item_id, action, status, message)
                VALUES (?, ?, ?, ?, ?)
                """,
                (run_id, item_id, action, status, message),
            )

    def list_items_by_status(self, statuses: tuple[str, ...], limit: int = 50) -> list[dict[str, Any]]:
        if not statuses:
            return []
        placeholders = ",".join("?" for _ in statuses)
        with self.connect() as conn:
            rows = conn.execute(
                f"SELECT * FROM items WHERE status IN ({placeholders}) ORDER BY updated_at ASC LIMIT ?",
                (*statuses, limit),
            ).fetchall()
            return [dict(row) for row in rows]

    def list_items_needing_summary(self, limit: int = 50) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM items
                WHERE has_transcript = 1
                  AND file_path IS NOT NULL
                  AND (summary_status IS NULL OR summary_status = 'summary_failed')
                ORDER BY updated_at ASC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
            return [dict(row) for row in rows]

    def list_items(self, limit: int = 20) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM items ORDER BY updated_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
            return [dict(row) for row in rows]

    def list_runs(self, limit: int = 20) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT *
                FROM runs
                ORDER BY started_at DESC, id DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
            return [dict(row) for row in rows]

    def list_run_items(self, run_id: int, limit: int = 100) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT
                    ri.run_id,
                    ri.item_id,
                    ri.action,
                    ri.status AS run_item_status,
                    ri.message,
                    i.column_name,
                    i.title,
                    i.source_url,
                    i.file_path,
                    i.error_message
                FROM run_items ri
                LEFT JOIN items i ON i.id = ri.item_id
                WHERE ri.run_id = ?
                ORDER BY ri.item_id ASC
                LIMIT ?
                """,
                (run_id, limit),
            ).fetchall()
            return [dict(row) for row in rows]


def row_to_content_item(row: dict[str, Any] | sqlite3.Row) -> ContentItem:
    return ContentItem(
        source_url=str(row["source_url"]),
        detail_url=str(row["canonical_url"] or row["source_url"]),
        dedao_id=row["dedao_id"],
        column_name=str(row["column_name"]),
        title=str(row["title"]),
        published_at=row["published_at"],
    )
