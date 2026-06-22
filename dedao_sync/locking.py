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
        host = ""
        pid = 0
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            pid = int(raw.get("pid") or 0)
            host = str(raw.get("host") or "")
            acquired_at = datetime.fromisoformat(str(raw["acquired_at"]))
        except (OSError, KeyError, ValueError, json.JSONDecodeError):
            acquired_at = datetime.fromtimestamp(self.path.stat().st_mtime, timezone.utc)
        if acquired_at.tzinfo is None:
            acquired_at = acquired_at.replace(tzinfo=timezone.utc)
        if host == socket.gethostname() and pid and not is_process_running(pid):
            self.path.unlink(missing_ok=True)
            return
        if datetime.now(timezone.utc) - acquired_at > self.stale_after:
            self.path.unlink(missing_ok=True)


def is_process_running(pid: int) -> bool:
    if pid <= 0:
        return False
    if pid == os.getpid():
        return True
    if os.name == "nt":
        try:
            import ctypes
            from ctypes import wintypes

            process_query_limited_information = 0x1000
            still_active = 259
            handle = ctypes.windll.kernel32.OpenProcess(process_query_limited_information, False, pid)
            if not handle:
                return False
            try:
                exit_code = wintypes.DWORD()
                if not ctypes.windll.kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                    return False
                return exit_code.value == still_active
            finally:
                ctypes.windll.kernel32.CloseHandle(handle)
        except Exception:
            return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True

