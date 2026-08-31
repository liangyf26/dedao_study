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

GIT_TERMINAL_PROMPT=0 /usr/bin/git push origin HEAD:main
