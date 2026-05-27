from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

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


if __name__ == "__main__":
    unittest.main()

