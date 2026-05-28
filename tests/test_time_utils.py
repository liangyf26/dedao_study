from __future__ import annotations

import unittest

from dedao_sync.time_utils import now_local


class TimeUtilsTests(unittest.TestCase):
    def test_now_local_uses_asia_shanghai(self):
        current = now_local()

        timezone_name = getattr(current.tzinfo, "key", None) or current.tzname()
        self.assertEqual(timezone_name, "Asia/Shanghai")
        self.assertEqual(current.utcoffset().total_seconds(), 8 * 60 * 60)


if __name__ == "__main__":
    unittest.main()
