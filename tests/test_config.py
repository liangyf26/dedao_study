from __future__ import annotations

import unittest
import tempfile
from pathlib import Path

from dedao_sync.config import load_config


ROOT = Path(__file__).resolve().parents[1]


class ConfigTests(unittest.TestCase):
    def test_example_config_loads_without_pyyaml(self):
        config = load_config(ROOT / "config.example.yaml", root_dir=ROOT)
        self.assertEqual(config.obsidian.output_dir, "得到")
        self.assertEqual(len(config.dedao.columns), 4)
        self.assertEqual(config.dedao.columns[0].name, "快刀青衣·快刀广播站")
        self.assertFalse(config.dedao.save_failure_html)
        self.assertEqual(config.dedao.failure_snapshot_dir, ROOT / "data" / "page_failures")
        self.assertFalse(config.transcription.enabled)
        self.assertTrue(config.feishu.include_titles)

    def test_quoted_false_values_parse_as_false(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_path = root / "config.yaml"
            config_path.write_text(
                """
obsidian:
  vault_path: "vault"
  output_dir: "得到"
  filename_pattern: "{column}-{published_date}-{title}.md"
dedao:
  headless: "false"
  save_failure_html: "false"
  columns:
    - name: "栏目"
      url: "https://example.com"
      enabled: "false"
summary:
  enabled: "false"
  provider: "opencode_go"
transcription:
  enabled: "false"
  delete_media_after_transcription: "false"
feishu:
  enabled: "false"
  include_titles: "false"
""",
                encoding="utf-8",
            )

            config = load_config(config_path, root_dir=root)

            self.assertFalse(config.dedao.headless)
            self.assertFalse(config.dedao.save_failure_html)
            self.assertFalse(config.dedao.columns[0].enabled)
            self.assertFalse(config.summary.enabled)
            self.assertFalse(config.transcription.enabled)
            self.assertFalse(config.transcription.delete_media_after_transcription)
            self.assertFalse(config.feishu.enabled)
            self.assertFalse(config.feishu.include_titles)


if __name__ == "__main__":
    unittest.main()
