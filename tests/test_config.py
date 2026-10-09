import os
import sys
import json
import tempfile
import unittest
from unittest.mock import patch

from src.config import get_config_path, load_config, save_config, DEFAULT_CONFIG


class TestConfig(unittest.TestCase):
    def test_dev_mode_config_path(self):
        # In non-frozen dev mode, it points to project root config.json
        path = get_config_path()
        self.assertTrue(path.endswith("config.json"))
        self.assertTrue(os.path.isabs(path))

    @patch("sys.frozen", True, create=True)
    @patch("sys.executable", "C:\\Program Files\\Getsu\\getsu.exe")
    def test_frozen_installed_mode_config_path(self):
        # When sys.executable is in a non-writable folder, it should resolve to APPDATA\Getsu\config.json
        with patch("os.environ", {"APPDATA": tempfile.gettempdir()}):
            path = get_config_path()
            self.assertTrue(path.endswith("config.json"))
            self.assertIn("Getsu", path)
            self.assertTrue(path.startswith(tempfile.gettempdir()))

    def test_save_and_load_config(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            test_cfg_path = os.path.join(tmp_dir, "config.json")
            with patch("src.config.CONFIG_FILE", test_cfg_path):
                cfg = DEFAULT_CONFIG.copy()
                cfg["vad_threshold"] = 0.92
                cfg["rnnoise_enabled"] = False

                success = save_config(cfg)
                self.assertTrue(success)
                self.assertTrue(os.path.exists(test_cfg_path))

                loaded = load_config()
                self.assertEqual(loaded["vad_threshold"], 0.92)
                self.assertFalse(loaded["rnnoise_enabled"])

    def test_music_mode_default_and_migration(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            test_cfg_path = os.path.join(tmp_dir, "config.json")
            with patch("src.config.CONFIG_FILE", test_cfg_path):
                # Write config missing music_mode
                legacy = {"version": "1.2.9", "vad_threshold": 0.70}
                with open(test_cfg_path, "w", encoding="utf-8") as f:
                    json.dump(legacy, f)

                loaded = load_config()
                self.assertIn("music_mode", loaded)
                self.assertFalse(loaded["music_mode"])

    def test_legacy_keys_purged(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            test_cfg_path = os.path.join(tmp_dir, "config.json")
            with patch("src.config.CONFIG_FILE", test_cfg_path):
                # Write config containing legacy keys
                legacy = {"version": "1.2.9", "mute": True, "app_name": "Getsu", "vad_cold_start_gain": 0.0}
                with open(test_cfg_path, "w", encoding="utf-8") as f:
                    json.dump(legacy, f)

                loaded = load_config()
                self.assertNotIn("mute", loaded)
                self.assertNotIn("app_name", loaded)
                self.assertNotIn("vad_cold_start_gain", loaded)


if __name__ == "__main__":
    unittest.main()
