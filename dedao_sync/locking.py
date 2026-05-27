from __future__ import annotations

import json
import os
import socket
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path


class RunLockError(RuntimeError):
    pass


@dataclass(frozen=True)
class LockInfo:
    pid: int
    host: str
    acquired_at: str


class RunLock:
    def __init__(self, path: str | Path, *, stale_after: timedelta = timedelta(hours=6)):
        self.path = Path(path)
        self.stale_after = stale_after
        self.acquired = False

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._remove_if_stale()
        info = LockInfo(
            pid=os.getpid(),
            host=socket.gethostname(),
            acquired_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        )
        flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
        try:
            fd = os.open(str(self.path), flags)
        except FileExistsError as exc:
            raise RunLockError(f"Another dedao-sync run is active: {self.path}") from exc
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(info.__dict__, handle, ensure_ascii=False, indent=2)
        self.acquired = True

    def release(self) -> None:
        if not self.acquired:
            return
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass
        finally:
            self.acquired = False

    def _remove_if_stale(self) -> None:
        if not self.path.exists():
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            acquired_at = datetime.fromisoformat(str(raw["acquired_at"]))
        except (OSError, KeyError, ValueError, json.JSONDecodeError):
            acquired_at = datetime.fromtimestamp(self.path.stat().st_mtime, timezone.utc)
        if acquired_at.tzinfo is None:
            acquired_at = acquired_at.replace(tzinfo=timezone.utc)
        if datetime.now(timezone.utc) - acquired_at > self.stale_after:
            self.path.unlink(missing_ok=True)

