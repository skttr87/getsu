import json
import os
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(__file__))
import _isolation  # noqa: F401  keeps config writes off the tracked config.json

from src import config as cfgmod


class TestSanitizeConfig(unittest.TestCase):
    def test_null_boost_becomes_zero(self):
        cfg = {"mic_boost_db": None, "vad_threshold": 0.7}
        self.assertTrue(cfgmod.sanitize_config(cfg))
        self.assertEqual(cfg["mic_boost_db"], 0)

    def test_valid_boost_kept(self):
        for db in (0, 5, 10, 15):
            cfg = {"mic_boost_db": db, "vad_threshold": 0.7}
            self.assertFalse(cfgmod.sanitize_config(cfg))
            self.assertEqual(cfg["mic_boost_db"], db)

    def test_float_boost_normalised_to_int(self):
        cfg = {"mic_boost_db": 10.0, "vad_threshold": 0.7}
        self.assertTrue(cfgmod.sanitize_config(cfg))
        self.assertEqual(cfg["mic_boost_db"], 10)
        self.assertIsInstance(cfg["mic_boost_db"], int)

    def test_bad_boost_values(self):
        for bad in ("10", 7, True, [], -5):
            cfg = {"mic_boost_db": bad, "vad_threshold": 0.7}
            cfgmod.sanitize_config(cfg)
            self.assertEqual(cfg["mic_boost_db"], 0, bad)

    def test_vad_threshold_clamped_to_probability(self):
        cfg = {"mic_boost_db": 0, "vad_threshold": -0.2}
        self.assertTrue(cfgmod.sanitize_config(cfg))
        self.assertAlmostEqual(cfg["vad_threshold"], 0.0)
        cfg = {"mic_boost_db": 0, "vad_threshold": 1.5}
        cfgmod.sanitize_config(cfg)
        self.assertAlmostEqual(cfg["vad_threshold"], 1.0)

    def test_vad_threshold_valid_values_kept(self):
        # Values outside the slider's 0.40-0.90 but valid probabilities are user settings and stay unchanged.
        for v in (0.1, 0.4, 0.7, 0.92):
            cfg = {"mic_boost_db": 0, "vad_threshold": v}
            self.assertFalse(cfgmod.sanitize_config(cfg), v)
            self.assertEqual(cfg["vad_threshold"], v)

    def test_vad_threshold_non_number(self):
        cfg = {"mic_boost_db": 0, "vad_threshold": None}
        self.assertTrue(cfgmod.sanitize_config(cfg))
        self.assertAlmostEqual(cfg["vad_threshold"], 0.70)

    def test_load_config_repairs_null_boost_file(self):
        """The owner's real failure: a saved config with "mic_boost_db": null must load as 0 and be rewritten."""
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "config.json")
            data = dict(cfgmod.DEFAULT_CONFIG)
            data["mic_boost_db"] = None
            data["vad_threshold"] = 0.4
            with open(path, "w", encoding="utf-8") as f:
                json.dump(data, f)
            with patch.object(cfgmod, "CONFIG_FILE", path):
                loaded = cfgmod.load_config()
                self.assertEqual(loaded["mic_boost_db"], 0)
                self.assertEqual(int(loaded["mic_boost_db"]), 0)
                self.assertEqual(loaded["vad_threshold"], 0.4)
                with open(path, encoding="utf-8") as f:
                    self.assertEqual(json.load(f)["mic_boost_db"], 0)


if __name__ == "__main__":
    unittest.main()
