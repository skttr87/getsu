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


def _band_ratio_db(y, fs=48000):
    """Energy above 6 kHz relative to energy below 1 kHz, in dB."""
    spec = np.abs(np.fft.rfft(y.astype(np.float64))) ** 2
    f = np.fft.rfftfreq(len(y), d=1.0 / fs)
    hi = np.sum(spec[f > 6000.0])
    lo = np.sum(spec[f < 1000.0]) + 1e-30
    return 10.0 * np.log10(hi / lo + 1e-30)


class GateTiltTests(unittest.TestCase):

    def test_fir_has_unity_dc_gain_and_cuts_highs(self):
        from src.dsp import _design_tilt_fir
        h = np.array(_design_tilt_fir(4000.0, 48000.0))
        self.assertAlmostEqual(float(np.sum(h)), 1.0, places=9)
        f = np.array([500.0, 4000.0, 8000.0, 12000.0])
        k = np.arange(len(h))
        resp = np.abs(np.exp(-2j * np.pi * np.outer(f, k) / 48000.0) @ h)
        self.assertAlmostEqual(float(resp[0]), 1.0, places=2)
        self.assertLess(20 * np.log10(resp[2]), -6.0)      # 8 kHz at least 6 dB down
        self.assertLess(20 * np.log10(resp[3]), -20.0)     # 12 kHz at least 20 dB down

    def test_closed_path_loses_more_highs_with_tilt(self):
        rng = np.random.default_rng(11)
        noise = (0.05 * rng.standard_normal(480 * 150)).astype(np.float32)

        def run(tilt_hz):
            gate = AdaptiveNoiseGate(lookahead=True, floor_gain=0.06, calibrate_startup=False,
                                     floor_tilt_hz=tilt_hz)
            outs = []
            for i in range(150):
                blk = noise[i * 480:(i + 1) * 480].copy()
                out, _ = gate.process(blk, 0.0, input_rms_db=-30.0, in_place=False)
                outs.append(np.array(out, dtype=np.float32))
            return np.concatenate(outs[30:])               # skip the attack/decay settling

        plain = _band_ratio_db(run(0.0))
        tilted = _band_ratio_db(run(4000.0))
        self.assertLess(tilted, plain - 3.0)

    def test_open_path_is_unchanged_by_tilt(self):
        rng = np.random.default_rng(5)
        x = (0.2 * rng.standard_normal(480 * 80)).astype(np.float32)
        gate = AdaptiveNoiseGate(lookahead=True, floor_gain=0.06, calibrate_startup=False, floor_tilt_hz=4000.0)
        outs = []
        for i in range(80):
            blk = x[i * 480:(i + 1) * 480].copy()
            out, _ = gate.process(blk, 0.95, input_rms_db=-14.0, in_place=False)
            outs.append(np.array(out, dtype=np.float32))
        y = np.concatenate(outs)
        # lookahead: output frame t is input frame t-1 once the gain has reached 1.0
        np.testing.assert_allclose(y[60 * 480:], x[59 * 480:79 * 480], rtol=1e-5, atol=1e-6)

    def test_transitions_stay_finite_and_bounded(self):
        rng = np.random.default_rng(9)
        gate = AdaptiveNoiseGate(lookahead=True, floor_gain=0.06, calibrate_startup=False, floor_tilt_hz=4000.0)
        peak_in = 0.0
        peak_out = 0.0
        for i in range(120):
            blk = (0.3 * rng.standard_normal(480)).astype(np.float32)
            sp = 0.9 if (i // 20) % 2 == 0 else 0.0          # open and close repeatedly
            out, _ = gate.process(blk.copy(), sp, input_rms_db=-14.0 if sp > 0.5 else -50.0, in_place=False)
            out = np.array(out, dtype=np.float32)
            self.assertTrue(np.all(np.isfinite(out)))
            peak_in = max(peak_in, float(np.max(np.abs(blk))))
            peak_out = max(peak_out, float(np.max(np.abs(out))))
        self.assertLessEqual(peak_out, peak_in + 1e-6)

    def test_reset_clears_filter_history(self):
        gate = AdaptiveNoiseGate(lookahead=True, floor_gain=0.06, calibrate_startup=False, floor_tilt_hz=4000.0)
        # lookahead delays the frame by one block, so feed two blocks before the history is non-zero
        gate.process(np.ones(480, dtype=np.float32), 0.0, input_rms_db=-50.0, in_place=False)
        gate.process(np.ones(480, dtype=np.float32), 0.0, input_rms_db=-50.0, in_place=False)
        self.assertGreater(float(np.max(np.abs(gate._tilt_hist))), 0.0)
        gate.reset()
        self.assertEqual(float(np.max(np.abs(gate._tilt_hist))), 0.0)

    def test_tilt_disabled_without_floor_or_with_zero_hz(self):
        self.assertIsNone(AdaptiveNoiseGate(floor_gain=0.06, floor_tilt_hz=0.0)._tilt_h)
        self.assertIsNone(AdaptiveNoiseGate(floor_gain=0.0, floor_tilt_hz=4000.0)._tilt_h)

    def test_default_config_tilt_is_off(self):
        self.assertEqual(DEFAULT_CONFIG["vad_floor_tilt_hz"], 0.0)


class _IdentityRNN:
    """Passes the frame through unchanged and reports a fixed speech probability."""

    def __init__(self, prob=0.95):
        self.prob = prob

    def process_frame(self, frame):
        return frame * 1.0, self.prob


class DryBlendTests(unittest.TestCase):

    def test_push_returns_frame_from_delay_frames_ago(self):
        from src.dsp import DryBlend
        db = DryBlend(mix_db=-24.0, delay_frames=3, frame_len=480)
        seen = []
        for i in range(6):
            out = db.push(np.full(480, float(i + 1), dtype=np.float32))
            seen.append(float(out[0]))
        self.assertEqual(seen, [0.0, 0.0, 0.0, 1.0, 2.0, 3.0])

    def test_gain_is_minus_24_db(self):
        from src.dsp import DryBlend
        self.assertAlmostEqual(DryBlend(mix_db=-24.0).gain, 10 ** (-24 / 20), places=9)

    def _run(self, dry_blend, speech_prob, frames, seed=21):
        rng = np.random.default_rng(seed)
        gate = AdaptiveNoiseGate(lookahead=True, floor_gain=0.06, calibrate_startup=False)
        inputs = []
        outs = []
        for _ in range(frames):
            fr = (0.1 * rng.standard_normal(480)).astype(np.float32)
            inputs.append(fr.copy())
            out, _, _, _ = process_mono_frame(fr.copy(), _IdentityRNN(speech_prob), None, gate, 1.0, True, None, None,
                                              0.0, dry_blend=dry_blend)
            outs.append(np.array(out, dtype=np.float32))
        return inputs, outs

    def test_mix_is_delayed_dry_scaled_while_gate_open(self):
        from src.dsp import DryBlend
        # Output-only comparison: the gate decisions do not depend on the dry path
        inputs, wet = self._run(None, 0.95, 80)
        _, mixed = self._run(DryBlend(mix_db=-24.0, delay_frames=3), 0.95, 80)
        w = 10 ** (-24 / 20)
        for t in range(70, 80):
            diff = mixed[t].astype(np.float64) - wet[t].astype(np.float64)
            expected = w * inputs[t - 3].astype(np.float64)
            np.testing.assert_allclose(diff, expected, rtol=1e-4, atol=1e-6)

    def test_no_mix_while_gate_closed(self):
        from src.dsp import DryBlend
        _, wet = self._run(None, 0.0, 40)
        _, mixed = self._run(DryBlend(mix_db=-24.0, delay_frames=3), 0.0, 40)
        for t in range(40):
            np.testing.assert_array_equal(mixed[t], wet[t])

    def test_engine_default_is_off_and_config_enables(self):
        from src.stream import AudioEngine
        with patch("src.stream.sd") as sd_mock:
            sd_mock.query_devices.return_value = {"max_input_channels": 2, "max_output_channels": 2, "name": "x"}
            self.assertIsNone(AudioEngine(0, 1)._dry_blend)
            self.assertIsNotNone(AudioEngine(0, 1, rnnoise_dry_mix_db=-24.0)._dry_blend)
        self.assertIsNone(DEFAULT_CONFIG["rnnoise_dry_mix_db"])


class CableRateTests(unittest.TestCase):

    def _engine(self, output_device, rates, installed=True):
        from src.stream import AudioEngine
        cable_in = {"index": 4, "name": "CABLE Input"}
        cable_out = {"index": 5, "name": "CABLE Output"}

        def query(idx=None, *a, **k):
            if idx is None:
                return []
            return {"max_input_channels": 2, "max_output_channels": 2, "name": "x",
                    "default_samplerate": float(rates.get(idx, 48000))}

        with patch("src.stream.sd") as sd_mock, \
                patch("src.devices.check_vbcable_status",
                      return_value=(installed, cable_in, cable_out)):
            sd_mock.query_devices.side_effect = query
            eng = AudioEngine(0, output_device)
            eng._check_cable_rate()
        return eng

    def test_cable_at_44100_is_reported(self):
        eng = self._engine(4, {4: 44100, 5: 48000})
        self.assertEqual(eng.cable_rate_mismatch, 44100)

    def test_cable_output_rate_reported_when_input_is_48k(self):
        eng = self._engine(4, {4: 48000, 5: 44100})
        self.assertEqual(eng.cable_rate_mismatch, 44100)

    def test_matching_rates_give_none(self):
        eng = self._engine(4, {4: 48000, 5: 48000})
        self.assertIsNone(eng.cable_rate_mismatch)

    def test_non_cable_output_gives_none(self):
        eng = self._engine(9, {9: 44100, 4: 44100, 5: 44100})
        self.assertIsNone(eng.cable_rate_mismatch)

    def test_cable_not_installed_gives_none(self):
        eng = self._engine(4, {4: 44100}, installed=False)
        self.assertIsNone(eng.cable_rate_mismatch)

    def test_failed_query_gives_none_and_no_exception(self):
        from src.stream import AudioEngine
        with patch("src.stream.sd") as sd_mock:
            sd_mock.query_devices.return_value = {"max_input_channels": 2, "max_output_channels": 2, "name": "x"}
            eng = AudioEngine(0, 4)
        with patch("src.devices.check_vbcable_status", side_effect=RuntimeError("boom")):
            eng._check_cable_rate()
        self.assertIsNone(eng.cable_rate_mismatch)

    def test_attribute_defaults_to_none(self):
        from src.stream import AudioEngine
        with patch("src.stream.sd") as sd_mock:
            sd_mock.query_devices.return_value = {"max_input_channels": 2, "max_output_channels": 2, "name": "x"}
            self.assertIsNone(AudioEngine(0, 1).cable_rate_mismatch)


if __name__ == "__main__":
    unittest.main()
