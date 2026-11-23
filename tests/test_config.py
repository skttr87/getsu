import os
import sys
import json
import tempfile
import unittest
from unittest.mock import patch

import os as _os
import sys as _sys
_sys.path.insert(0, _os.path.dirname(__file__))
import _isolation  # noqa: E402,F401  (keeps config writes off the tracked config.json)
from src.config import get_config_path, load_config, save_config, DEFAULT_CONFIG


class TestConfig(unittest.TestCase):
    def test_dev_mode_config_path_is_outside_the_repo(self):
        # Dev mode writes %APPDATA%\Getsu-dev\config.json, never the tracked repo-root config.json.
        with tempfile.TemporaryDirectory() as appdata:
            with patch.dict(os.environ, {"APPDATA": appdata}):
                path = get_config_path()
            repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
            self.assertEqual(path, os.path.join(appdata, "Getsu-dev", "config.json"))
            self.assertTrue(os.path.isdir(os.path.join(appdata, "Getsu-dev")))
            self.assertFalse(os.path.abspath(path).startswith(repo_root))

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

    def test_last_physical_playback_id_default_and_migration(self):
        self.assertIn("last_physical_playback_id", DEFAULT_CONFIG)
        self.assertIsNone(DEFAULT_CONFIG["last_physical_playback_id"])
        with tempfile.TemporaryDirectory() as tmp_dir:
            test_cfg_path = os.path.join(tmp_dir, "config.json")
            with patch("src.config.CONFIG_FILE", test_cfg_path):
                # A config saved before the cable guard existed gets the key, and the file is rewritten with it.
                legacy = {"version": "1.3.1", "vad_threshold": 0.70, "was_running_on_exit": True}
                with open(test_cfg_path, "w", encoding="utf-8") as f:
                    json.dump(legacy, f)

                loaded = load_config()
                self.assertIn("last_physical_playback_id", loaded)
                self.assertIsNone(loaded["last_physical_playback_id"])
                self.assertTrue(loaded["was_running_on_exit"])
                with open(test_cfg_path, "r", encoding="utf-8") as f:
                    self.assertIsNone(json.load(f)["last_physical_playback_id"])

                # A value already stored is kept as it is.
                loaded["last_physical_playback_id"] = "{0.0.0.00000000}.{5f6e7d8c-2222-4b3c-8d9e-aabbccddeeff}"
                save_config(loaded)
                reloaded = load_config()
                self.assertEqual(reloaded["last_physical_playback_id"], loaded["last_physical_playback_id"])

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
