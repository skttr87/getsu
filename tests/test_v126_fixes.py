import importlib.util
import os
import random
import unittest

import numpy as np

from src.dsp import AdaptiveNoiseGate, HighPassFilter, soft_limit
from src.config_migration_v126 import migrate_v126

N = 480
QUIET = -80.0


def frame(value=0.1):
    return np.full(N, value, dtype=np.float32)


def make_gate(**kw):
    kw.setdefault("lookahead", True)
    kw.setdefault("floor_gain", 0.06)
    return AdaptiveNoiseGate(**kw)


def run(gate, probs, rms=None):
    gains = []
    for p in probs:
        _, g = gate.process(frame(), p, input_rms_db=rms, in_place=True)
        gains.append(g)
    return gains


class FlutterTests(unittest.TestCase):
    def test_short_voiced_blips_extend_cooldown(self):
        g = make_gate(calibrate_startup=False)
        self.assertEqual(g._current_cooldown_frames(), 15)
        for _ in range(3):
            run(g, [0.8] * 3 + [0.0] * 40)  # 30 ms voiced, then silence until close
        self.assertEqual(len(g._flutter_events), 3)
        self.assertEqual(g._current_cooldown_frames(), 30)

    def test_normal_speech_never_counts(self):
        g = make_gate(calibrate_startup=False)
        for _ in range(5):
            run(g, [0.9] * 40 + [0.0] * 45)  # 400 ms voiced
        self.assertEqual(len(g._flutter_events), 0)
        self.assertEqual(g._current_cooldown_frames(), 15)

    def test_cooldown_reverts_after_window(self):
        g = make_gate(calibrate_startup=False)
        for _ in range(3):
            run(g, [0.8] * 3 + [0.0] * 40)
        self.assertEqual(g._current_cooldown_frames(), 30)
        run(g, [0.0] * 301)
        self.assertEqual(g._current_cooldown_frames(), 15)

    def test_gate_open_time_is_never_short(self):
        """Documents why the old 'open < 150 ms' definition could not work."""
        g = make_gate(calibrate_startup=False)
        open_frames = 0
        for p in [0.8] + [0.0] * 60:
            g.process(frame(), p, in_place=True)
            if g.is_open:
                open_frames += 1
        self.assertGreaterEqual(open_frames, g.hangover_frames)


class CalibrationTests(unittest.TestCase):
    def test_rejects_speech_frames(self):
        g = make_gate()
        for i in range(120):
            speech = i % 4 == 0
            g.process(frame(), 0.9 if speech else 0.05,
                      input_rms_db=-25.0 if speech else -50.0, in_place=True)
            if g._calibrated:
                break
        self.assertTrue(g._calibrated)
        self.assertAlmostEqual(g.noise_floor_db, -50.0, delta=1.0)

    def test_clamps_range(self):
        g = make_gate()
        for _ in range(60):
            g.process(frame(), 0.0, input_rms_db=-90.0, in_place=True)
        self.assertEqual(g.noise_floor_db, -75.0)

    def test_timeout_without_ambient_keeps_default(self):
        g = make_gate()
        for _ in range(160):
            g.process(frame(), 0.9, input_rms_db=-25.0, in_place=True)
        self.assertTrue(g._calibrated)
        self.assertEqual(g.noise_floor_db, AdaptiveNoiseGate.FLOOR_INIT_DB)

    def test_recalibrate_restarts(self):
        g = make_gate()
        for _ in range(60):
            g.process(frame(), 0.0, input_rms_db=-50.0, in_place=True)
        self.assertTrue(g._calibrated)
        g.recalibrate()
        self.assertFalse(g._calibrated)
        self.assertEqual(g.noise_floor_db, AdaptiveNoiseGate.FLOOR_INIT_DB)


class FloorTrackingTests(unittest.TestCase):
    def test_rise_is_capped_per_frame(self):
        g = make_gate(calibrate_startup=False)
        g.noise_floor_db = -60.0
        g.frames_since_close = 0  # inside cooldown
        before = g.noise_floor_db
        for _ in range(14):
            g.process(frame(), 0.05, input_rms_db=-25.0, in_place=True)  # loud non-speech click
        self.assertLessEqual(g.noise_floor_db - before, 14 * 0.5 + 1e-6)

    def test_fast_rise_only_in_cooldown_and_low_prob(self):
        slow = make_gate(calibrate_startup=False)
        fast = make_gate(calibrate_startup=False)
        for gt in (slow, fast):
            gt.noise_floor_db = -60.0
        slow.frames_since_close = 10_000     # not in cooldown
        fast.frames_since_close = 0          # in cooldown
        slow.process(frame(), 0.05, input_rms_db=-55.0, in_place=True)
        fast.process(frame(), 0.05, input_rms_db=-55.0, in_place=True)
        self.assertGreater(fast.noise_floor_db, slow.noise_floor_db)

    def test_fall_is_fast(self):
        g = make_gate(calibrate_startup=False)
        g.noise_floor_db = -40.0
        g.process(frame(), 0.0, input_rms_db=-70.0, in_place=True)
        self.assertAlmostEqual(g.noise_floor_db, -40.0 + (-30.0 * 0.15), places=6)


class ResetTests(unittest.TestCase):
    def test_reset_closes_gate_and_flushes_delay(self):
        g = make_gate(calibrate_startup=False)
        run(g, [0.9] * 10)
        self.assertTrue(g.is_open)
        g.reset()
        self.assertFalse(g.is_open)
        self.assertEqual(g.current_gain, g.floor_gain)
        out, gain = g.process(frame(0.5), 0.0, in_place=False)
        # first frame after reset must not contain the stale pre-reset audio
        self.assertAlmostEqual(float(np.max(np.abs(out))), 0.0, places=7)

    def test_reset_before_first_frame_does_not_raise(self):
        g = make_gate(calibrate_startup=False)
        g.reset()  # _delay_buffer is None here

    def test_hpf_reset_state_keeps_buffer(self):
        h = HighPassFilter()
        h.process(np.random.randn(N).astype(np.float32) * 0.1)
        buf = h._out_buffer
        h.reset_state()
        self.assertIs(h._out_buffer, buf)
        self.assertEqual((h.x1, h.x2, h.y1, h.y2), (0.0, 0.0, 0.0, 0.0))


class MigrationTests(unittest.TestCase):
    def test_stale_defaults_migrated(self):
        data = {"version": "1.2.5"}
        merged = {"vad_close_threshold": 0.55, "vad_hangover_ms": 280.0, "version": "1.2.5"}
        self.assertTrue(migrate_v126(data, merged))
        self.assertEqual(merged["vad_close_threshold"], 0.52)
        self.assertEqual(merged["vad_hangover_ms"], 320.0)
        self.assertEqual(merged["version"], "1.2.6")

    def test_customized_preserved(self):
        data = {"version": "1.2.5", "vad_customized": True}
        merged = {"vad_close_threshold": 0.55, "vad_hangover_ms": 280.0,
                  "vad_customized": True, "version": "1.2.5"}
        migrate_v126(data, merged)
        self.assertEqual(merged["vad_close_threshold"], 0.55)
        self.assertEqual(merged["vad_hangover_ms"], 280.0)
        self.assertEqual(merged["version"], "1.2.6")

    def test_runs_once(self):
        data = {"version": "1.2.6"}
        merged = {"vad_close_threshold": 0.55, "vad_hangover_ms": 280.0, "version": "1.2.6"}
        self.assertFalse(migrate_v126(data, merged))
        self.assertEqual(merged["vad_close_threshold"], 0.55)  # user's later choice survives

    def test_non_default_values_untouched(self):
        data = {"version": "1.2.5"}
        merged = {"vad_close_threshold": 0.60, "vad_hangover_ms": 450.0, "version": "1.2.5"}
        migrate_v126(data, merged)
        self.assertEqual(merged["vad_close_threshold"], 0.60)
        self.assertEqual(merged["vad_hangover_ms"], 450.0)


class SoftLimitTests(unittest.TestCase):
    def test_under_threshold_unmodified(self):
        sig = np.array([0.1, -0.5, 0.8, -0.84], dtype=np.float32)
        limited = soft_limit(sig, threshold=0.85)
        np.testing.assert_allclose(sig, limited, atol=1e-6)

    def test_over_threshold_compressed_below_one(self):
        sig = np.array([0.9, -1.2, 2.0, -5.0], dtype=np.float32)
        limited = soft_limit(sig, threshold=0.85)
        self.assertTrue(np.all(np.abs(limited) <= 1.0))
        self.assertTrue(np.all(np.abs(limited) >= 0.85))


if __name__ == "__main__":
    unittest.main(verbosity=2)

