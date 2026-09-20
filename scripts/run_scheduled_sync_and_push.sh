#!/usr/bin/env bash
set -Eeuo pipefail

PROJECT_DIR=/home/lyf/project/dedao_study
VAULT_DIR=/home/lyf/biji/openclaw-vault
NOTES_PATH='5-收件箱(Inbox)/得到'
SYNC_BIN="$PROJECT_DIR/.venv/bin/dedao-sync"
CONFIG_PATH="$PROJECT_DIR/config.yaml"

# The sync command has its own lock; this lock protects the vault Git operation.
exec 9>"$PROJECT_DIR/data/dedao_sync_git.lock"
if ! /usr/bin/flock -n 9; then
    echo 'dedao sync Git step is already running' >&2
    exit 75
fi

sync_status=0
"$SYNC_BIN" sync --config "$CONFIG_PATH" || sync_status=$?
if (( sync_status != 0 )); then
    echo "dedao sync failed; skipping Git commit/push (exit=$sync_status)" >&2
    exit "$sync_status"
fi

cd "$VAULT_DIR"

# 年份目录整理：新生成的扁平笔记移进 YYYY 子目录（与全库年份结构一致，2026-09-10 起）
organize_notes_into_years() {
    local column_dir f year
    for column_dir in "$VAULT_DIR/$NOTES_PATH"/*/; do
        [[ -d "$column_dir" ]] || continue
        case "$(basename "$column_dir")" in
            20[0-9][0-9]) continue ;;   # 已是年份目录，跳过
        esac
        for f in "$column_dir"*.md; do
            [[ -f "$f" ]] || continue
            year=$(basename "$f" | /usr/bin/sed -nE 's/^[^-]+-([0-9]{4})-[0-9]{2}-[0-9]{2}-.*/\1/p')
            [[ -n "$year" ]] || continue
            /usr/bin/mkdir -p "$column_dir$year"
            /usr/bin/mv "$f" "$column_dir$year/"
        done
    done
}
organize_notes_into_years

# Keep retries/resummarization pointed at the notes after year-directory moves.
PYTHONPATH="$PROJECT_DIR" "$PROJECT_DIR/.venv/bin/python" - "$CONFIG_PATH" <<'PYTHON'
import sys
from dedao_sync.archive import reconcile_archived_note_paths
from dedao_sync.config import load_config
from dedao_sync.locking import RunLock
from dedao_sync.repository import SyncRepository
from dedao_sync.sync import default_db_path, default_lock_path

config = load_config(sys.argv[1])
lock = RunLock(default_lock_path(config.root_dir))
lock.acquire()
try:
    count = reconcile_archived_note_paths(SyncRepository(default_db_path(config.root_dir)), config.output_root)
    print(f"dedao archive reconciled {count} database path(s)")
finally:
    lock.release()
PYTHON

pathspec_file=$(/usr/bin/mktemp)
trap 'rm -f "$pathspec_file"' EXIT

# Only newly generated notes are staged. Existing tracked notes or unrelated
# user changes in the vault are left untouched.
/usr/bin/git ls-files --others --exclude-standard -z -- "$NOTES_PATH" > "$pathspec_file"
if [[ -s "$pathspec_file" ]]; then
    /usr/bin/git add --pathspec-from-file="$pathspec_file" --pathspec-file-nul
    /usr/bin/git commit --only --pathspec-from-file="$pathspec_file" --pathspec-file-nul \
        -m "sync: dedao notes $(/usr/bin/date '+%Y-%m-%d %H:%M:%S %z')"
else
    echo 'dedao sync produced no new notes'
fi

# VPS3 不再直推 GitHub：笔记经坚果云 bisync → VPS1 统一聚合推送（2026-09-10 起）
# 保留本地 commit 作历史，push 职责已移交 VPS1
