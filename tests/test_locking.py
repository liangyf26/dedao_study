from __future__ import annotations

import json
import os
import socket
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from dedao_sync.locking import RunLock, RunLockError


class LockingTests(unittest.TestCase):
    def test_lock_rejects_second_active_owner(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "run.lock"
            first = RunLock(path)
            second = RunLock(path)
            first.acquire()
            try:
                with self.assertRaises(RunLockError):
                    second.acquire()
            finally:
                first.release()
            self.assertFalse(path.exists())

    def test_stale_lock_is_removed(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "run.lock"
            stale = datetime.now(timezone.utc) - timedelta(hours=12)
            path.write_text(json.dumps({"pid": 1, "host": "x", "acquired_at": stale.isoformat()}), encoding="utf-8")
            lock = RunLock(path, stale_after=timedelta(hours=1))
            lock.acquire()
            try:
                self.assertTrue(path.exists())
            finally:
                lock.release()

    def test_dead_same_host_process_lock_is_removed(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "run.lock"
            path.write_text(
                json.dumps(
                    {
                        "pid": os.getpid() + 100000,
                        "host": socket.gethostname(),
                        "acquired_at": datetime.now(timezone.utc).isoformat(),
                    }
                ),
                encoding="utf-8",
            )
            lock = RunLock(path)
            with mock.patch("dedao_sync.locking.is_process_running", return_value=False):
                lock.acquire()
            try:
                self.assertTrue(path.exists())
            finally:
                lock.release()


if __name__ == "__main__":
    unittest.main()

