"""Sound-quality tests: pre-clip knee, stereo input downmix, gate floor tilt, RNNoise dry blend, CABLE rate check."""
import os
import tempfile
import unittest
from unittest.mock import patch

import numpy as np

from src.config import DEFAULT_CONFIG, load_config
import src.config as config_module
from src.dsp import PRECLIP_KNEE_DEFAULT, soft_preclip, process_mono_frame, AdaptiveNoiseGate, InputDownmixer


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


def _stereo_blocks(left_gain=1.0, right_gain=1.0, inverted=False, frames=300, seed=7):
    """Yields (n,2) float32 blocks: a speech-like noise signal on the left, scaled copy on the right."""
    rng = np.random.default_rng(seed)
    for _ in range(frames):
        base = (0.2 * rng.standard_normal(480)).astype(np.float32)
        l = base * left_gain
        r = base * right_gain * (-1.0 if inverted else 1.0)
        yield np.stack([l, r], axis=1).astype(np.float32)


class StereoDownmixTests(unittest.TestCase):

    def _run(self, dm, blocks):
        last = None
        for blk in blocks:
            last = dm.process(blk.copy())
        return last

    def test_average_mode_is_legacy_formula_bit_exact(self):
        dm = InputDownmixer(mode="average")
        blk = next(_stereo_blocks(right_gain=0.3, frames=1))
        out = dm.process(blk.copy())
        expected = ((blk[:, 0] + blk[:, 1]) * 0.5).astype(np.float32)
        np.testing.assert_array_equal(out, expected)

    def test_auto_with_normal_stereo_averages(self):
        dm = InputDownmixer(mode="auto")
        blocks = list(_stereo_blocks(right_gain=0.5, frames=200))
        out = self._run(dm, blocks)
        self.assertEqual(dm.state_name, "average")
        expected = ((blocks[-1][:, 0] + blocks[-1][:, 1]) * 0.5).astype(np.float32)
        np.testing.assert_array_equal(out, expected)

    def test_dead_right_channel_uses_left(self):
        dm = InputDownmixer(mode="auto")
        blocks = list(_stereo_blocks(right_gain=0.0, frames=200))
        out = self._run(dm, blocks)
        self.assertEqual(dm.state_name, "left")
        np.testing.assert_array_equal(out, blocks[-1][:, 0])

    def test_dead_left_channel_uses_right(self):
        dm = InputDownmixer(mode="auto")
        blocks = [np.stack([b[:, 1] * 0.0, b[:, 1]], axis=1) for b in _stereo_blocks(frames=200)]
        out = self._run(dm, blocks)
        self.assertEqual(dm.state_name, "right")
        np.testing.assert_array_equal(out, blocks[-1][:, 1])

    def test_inverted_channels_use_left(self):
        dm = InputDownmixer(mode="auto")
        blocks = list(_stereo_blocks(inverted=True, frames=200))
        out = self._run(dm, blocks)
        self.assertEqual(dm.state_name, "left (inverted)")
        np.testing.assert_array_equal(out, blocks[-1][:, 0])
        self.assertGreater(float(np.sqrt(np.mean(out ** 2))), 0.1)   # not cancelled

    def test_inversion_released_when_channels_agree_again(self):
        dm = InputDownmixer(mode="auto")
        self._run(dm, list(_stereo_blocks(inverted=True, frames=200)))
        self.assertEqual(dm.state_name, "left (inverted)")
        self._run(dm, list(_stereo_blocks(inverted=False, frames=200)))
        self.assertEqual(dm.state_name, "average")

    def test_dead_state_holds_inside_hysteresis_band(self):
        dm = InputDownmixer(mode="auto")
        self._run(dm, list(_stereo_blocks(right_gain=0.0, frames=200)))
        self.assertEqual(dm.state_name, "left")
        # -29 dB is inside the band (dead enter 30 dB, release 27 dB): the state must not flip
        self._run(dm, list(_stereo_blocks(right_gain=10 ** (-29 / 20), frames=300)))
        self.assertEqual(dm.state_name, "left")
        # -20 dB is above the release level: back to averaging
        self._run(dm, list(_stereo_blocks(right_gain=10 ** (-20 / 20), frames=300)))
        self.assertEqual(dm.state_name, "average")

    def test_silence_makes_no_decision(self):
        dm = InputDownmixer(mode="auto")
        zeros = [np.zeros((480, 2), dtype=np.float32) for _ in range(200)]
        self._run(dm, zeros)
        self.assertEqual(dm.state_name, "average")

    def test_output_buffer_is_preallocated_and_reused(self):
        dm = InputDownmixer(mode="auto")
        blocks = list(_stereo_blocks(frames=3))
        first = dm.process(blocks[0].copy())
        second = dm.process(blocks[1].copy())
        self.assertIs(first, second)
        self.assertEqual(second.dtype, np.float32)
        self.assertEqual(second.shape, (480,))

    def test_frame_length_change_reallocates(self):
        dm = InputDownmixer(frame_len=480, mode="auto")
        out = dm.process(np.zeros((160, 2), dtype=np.float32))
        self.assertEqual(out.shape, (160,))

    def test_unknown_mode_rejected(self):
        with self.assertRaises(ValueError):
            InputDownmixer(mode="sum")

    def test_engine_defaults_to_auto_and_accepts_average(self):
        from src.stream import AudioEngine
        with patch("src.stream.sd") as sd_mock:
            sd_mock.query_devices.return_value = {"max_input_channels": 2, "max_output_channels": 2, "name": "x"}
            eng = AudioEngine(0, 1)
            self.assertEqual(eng._downmixer.mode, "auto")
            eng2 = AudioEngine(0, 1, stereo_downmix="average")
            self.assertEqual(eng2._downmixer.mode, "average")

    def test_default_config_and_migration(self):
        self.assertEqual(DEFAULT_CONFIG["stereo_downmix"], "auto")
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "config.json")
            with open(path, "w", encoding="utf-8") as f:
                f.write('{"version": "1.3.1"}')
            with patch.object(config_module, "CONFIG_FILE", path):
                cfg = load_config()
            self.assertEqual(cfg["stereo_downmix"], "auto")


if __name__ == "__main__":
    unittest.main()
