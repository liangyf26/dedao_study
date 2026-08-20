from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class GitignoreTests(unittest.TestCase):
    def test_sensitive_runtime_paths_are_ignored(self):
        ignored = set((ROOT / ".gitignore").read_text(encoding="utf-8").splitlines())

        required = {
            ".env",
            "config.yaml",
            "data/auth/",
            "data/browser_profile/",
            "data/media_cache/",
            "data/page_snapshots/",
            "data/page_failures/",
            "data/*.sqlite3-*",
            "logs/",
        }

        self.assertTrue(required <= ignored)
        # 状态库随仓库同步（迁移时保留去重状态），不应被忽略
        self.assertNotIn("data/*.sqlite3", ignored)


if __name__ == "__main__":
    unittest.main()
