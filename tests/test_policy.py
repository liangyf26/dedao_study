from __future__ import annotations

import unittest

from dedao_sync.models import MediaCandidate
from dedao_sync.policy import blocked_media_reason, check_page_policy


class PolicyTests(unittest.TestCase):
    def test_allows_plain_audio_candidate(self):
        decision = check_page_policy(
            "<html></html>",
            (MediaCandidate("https://static.example.com/audio.mp3", "audio/mpeg", "audio"),),
        )

        self.assertTrue(decision.allowed)

    def test_blocks_encrypted_hls_marker(self):
        decision = check_page_policy("#EXTM3U\n#EXT-X-KEY:METHOD=AES-128")

        self.assertFalse(decision.allowed)
        self.assertEqual(decision.reason, "encrypted_hls_key")

    def test_blocks_dash_manifest_candidate(self):
        reason = blocked_media_reason(MediaCandidate("https://static.example.com/video.mpd", None, "video"))

        self.assertEqual(reason, "dash_manifest")


if __name__ == "__main__":
    unittest.main()
