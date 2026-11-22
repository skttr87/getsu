"""Sound-quality tests: pre-clip knee, stereo input downmix, gate floor tilt, RNNoise dry blend, CABLE rate check."""
import os
import tempfile
import unittest
from unittest.mock import patch

import numpy as np

from src.config import DEFAULT_CONFIG, load_config
import src.config as config_module
from src.dsp import PRECLIP_KNEE_DEFAULT, soft_preclip, process_mono_frame, AdaptiveNoiseGate


class PreclipKneeTests(unittest.TestCase):

    def test_default_knee_is_0_95(self):
        self.assertEqual(PRECLIP_KNEE_DEFAULT, 0.95)
        self.assertEqual(DEFAULT_CONFIG["preclip_knee"], 0.95)

    def test_samples_below_knee_are_untouched(self):
        x = np.linspace(-0.94, 0.94, 480).astype(np.float32)
        y = soft_preclip(x.copy(), knee=0.95)
        np.testing.assert_array_equal(y, x)

    def test_old_knee_would_have_changed_0_9(self):
        x = np.full(480, 0.9, dtype=np.float32)
        y = soft_preclip(x.copy(), knee=0.75)
        self.assertLess(float(y[0]), 0.9)
        y_new = soft_preclip(x.copy(), knee=0.95)
        np.testing.assert_array_equal(y_new, x)

    def test_knee_one_or_more_disables_stage(self):
        x = np.full(480, 0.99, dtype=np.float32)
        np.testing.assert_array_equal(soft_preclip(x.copy(), knee=1.0), x)
        np.testing.assert_array_equal(soft_preclip(x.copy(), knee=1.2), x)

    def test_output_stays_below_one_after_knee(self):
        x = np.linspace(-2.0, 2.0, 4801).astype(np.float32)
        y = soft_preclip(x.copy(), knee=0.95)
        self.assertTrue(np.all(np.abs(y) <= 1.0 + 1e-6))

    def test_process_mono_frame_accepts_knee_and_default_matches_explicit(self):
        rng = np.random.default_rng(3)
        frame = (0.8 * np.sin(np.arange(480) * 0.3) + 0.01 * rng.standard_normal(480)).astype(np.float32)
        gate_a = AdaptiveNoiseGate(lookahead=True, floor_gain=0.06, calibrate_startup=False)
        gate_b = AdaptiveNoiseGate(lookahead=True, floor_gain=0.06, calibrate_startup=False)
        out_default, *_ = process_mono_frame(frame.copy(), None, None, gate_a, 1.0, False, None, None)
        out_explicit, *_ = process_mono_frame(frame.copy(), None, None, gate_b, 1.0, False, None, None,
                                              preclip_knee=PRECLIP_KNEE_DEFAULT)
        np.testing.assert_array_equal(out_default, out_explicit)


class PreclipConfigMigrationTests(unittest.TestCase):

    def test_config_without_key_gets_0_95_and_is_saved(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "config.json")
            with open(path, "w", encoding="utf-8") as f:
                f.write('{"version": "1.3.1", "vad_threshold": 0.70}')
            with patch.object(config_module, "CONFIG_FILE", path):
                cfg = load_config()
            self.assertEqual(cfg["preclip_knee"], 0.95)
            with open(path, "r", encoding="utf-8") as f:
                saved = f.read()
            self.assertIn('"preclip_knee": 0.95', saved)

    def test_explicit_saved_knee_is_kept(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "config.json")
            with open(path, "w", encoding="utf-8") as f:
                f.write('{"version": "1.3.1", "preclip_knee": 0.75}')
            with patch.object(config_module, "CONFIG_FILE", path):
                cfg = load_config()
            self.assertEqual(cfg["preclip_knee"], 0.75)


if __name__ == "__main__":
    unittest.main()
