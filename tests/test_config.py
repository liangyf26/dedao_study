from __future__ import annotations

import unittest
from pathlib import Path

from dedao_sync.config import load_config


ROOT = Path(__file__).resolve().parents[1]


class ConfigTests(unittest.TestCase):
    def test_example_config_loads_without_pyyaml(self):
        config = load_config(ROOT / "config.example.yaml", root_dir=ROOT)
        self.assertEqual(config.obsidian.output_dir, "得到")
        self.assertEqual(len(config.dedao.columns), 4)
        self.assertEqual(config.dedao.columns[0].name, "快刀青衣·快刀广播站")
        self.assertFalse(config.transcription.enabled)


if __name__ == "__main__":
    unittest.main()

