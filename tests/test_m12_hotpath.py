"""
Tests for the M1.2 DSP hot-path changes:
- HighPassFilter block form (T @ x + S @ state) against the per-sample reference loop
- AdaptiveNoiseGate incremental 90th-percentile anchor against np.percentile
- TransientSuppressor vectorized envelope against the per-spike reference, and the speech_prob skip
- In-place helpers used by process_mono_frame
- AudioEngine callback without the watchdog lock
- Bus-type cache and its invalidation on device enumeration passes
"""
import time
import unittest
from unittest.mock import MagicMock, patch

import numpy as np

from src.dsp import (
    AdaptiveNoiseGate,
    HighPassFilter,
    SpeechLeveler,
    TransientSuppressor,
    TS_RADIUS,
    TS_SKIP_SPEECH_PROB,
    _TS_BELL,
    calculate_levels,
    process_mono_frame,
    soft_limit,
    soft_preclip,
)
from src.router import clear_device_bus_type_cache, native_get_device_bus_type_cached, _bus_type_cache
from src.devices import is_virtual_input_device, get_all_devices
from src.stream import AudioEngine

FRAME = 480
TOL = 1e-5


# ----------------------------------------------------------------------------- reference implementations

def reference_hpf(coef, frames, state=(0.0, 0.0, 0.0, 0.0)):
    """The pre-M1.2 per-sample Direct Form I loop, including the anti-denormal flush."""
    b0, b1, b2, a1, a2 = coef
    x1, x2, y1, y2 = state
    outs = []
    for fr in frames:
        out = np.empty(len(fr), dtype=np.float32)
        for i, x0 in enumerate(np.asarray(fr).tolist()):
            y0 = b0 * x0 + b1 * x1 + b2 * x2 - a1 * y1 - a2 * y2
            if -1e-15 < y0 < 1e-15:
                y0 = 0.0
            out[i] = y0
            x2 = x1
            x1 = x0
            y2 = y1
            y1 = y0
        outs.append(out)
    return outs


def reference_transient(frame, prev_sample, threshold_crest=4.5, max_attenuation_db=8.0):
    """The pre-M1.2 TransientSuppressor.process body (per-spike Python loop)."""
    n = len(frame)
    diff = np.diff(frame, prepend=prev_sample)
    rms_diff = float(np.sqrt(np.mean(diff ** 2)))
    if rms_diff < 1e-4:
        return frame.copy()
    abs_diff = np.abs(diff)
    crest = abs_diff / (rms_diff + 1e-6)
    spike_mask = crest >= threshold_crest
    if not np.any(spike_mask):
        return frame.copy()
    gain_env = np.ones(n, dtype=frame.dtype)
    for s_idx in np.where(spike_mask)[0]:
        peak_val = crest[s_idx]
        att_db = min(max_attenuation_db, 6.0 + (peak_val - threshold_crest) * 0.8)
        spike_gain = float(10.0 ** (-att_db / 20.0))
        i_start = max(0, s_idx - TS_RADIUS)
        i_end = min(n, s_idx + TS_RADIUS + 1)
        win_len = i_end - i_start
        t = np.linspace(0.0, np.pi, win_len, dtype=np.float32)
        bell = np.sin(t) ** 2
        local_env = 1.0 - (1.0 - spike_gain) * bell
        gain_env[i_start:i_end] = np.minimum(gain_env[i_start:i_end], local_env)
    return frame * gain_env


# ----------------------------------------------------------------------------- HPF

class TestHighPassBlockForm(unittest.TestCase):

    def _coef(self, hpf):
        return (hpf.b0, hpf.b1, hpf.b2, hpf.a1, hpf.a2)

    def test_block_matches_reference_loop_across_cutoffs_and_frames(self):
        rng = np.random.default_rng(11)
        for cutoff in (30.0, 80.0, 200.0):
            for amp in (0.05, 0.6):
                hpf = HighPassFilter(cutoff_hz=cutoff, sample_rate=48000.0)
                sig = (rng.standard_normal(FRAME * 25) * amp).astype(np.float32)
                frames = [sig[i * FRAME:(i + 1) * FRAME] for i in range(25)]
                ref = reference_hpf(self._coef(hpf), frames)
                for i, fr in enumerate(frames):
                    got = hpf.process(fr)
                    diff = float(np.max(np.abs(got.astype(np.float64) - ref[i].astype(np.float64))))
                    self.assertLess(diff, TOL, f"cutoff={cutoff} amp={amp} frame={i}")

    def test_state_carries_across_frames_and_silence_flushes_to_zero(self):
        hpf = HighPassFilter(cutoff_hz=80.0, sample_rate=48000.0)
        rng = np.random.default_rng(3)
        burst = (rng.standard_normal(FRAME * 2) * 0.7).astype(np.float32)
        silence = np.zeros(FRAME * 40, dtype=np.float32)
        full = np.concatenate([burst, silence])
        frames = [full[i * FRAME:(i + 1) * FRAME] for i in range(len(full) // FRAME)]
        ref = reference_hpf(self._coef(hpf), frames)
        last = None
        for i, fr in enumerate(frames):
            last = hpf.process(fr).copy()
            self.assertLess(float(np.max(np.abs(last - ref[i]))), TOL)
        # Anti-denormal flush: after a long silence the output is exactly zero, as in the reference loop
        self.assertTrue(np.all(last == 0.0))
        self.assertTrue(np.all(ref[-1] == 0.0))

    def test_reset_state_equals_fresh_filter(self):
        rng = np.random.default_rng(5)
        a = HighPassFilter()
        b = HighPassFilter()
        warm = (rng.standard_normal(FRAME) * 0.4).astype(np.float32)
        a.process(warm)
        a.reset_state()
        probe = (rng.standard_normal(FRAME) * 0.4).astype(np.float32)
        np.testing.assert_allclose(a.process(probe), b.process(probe), atol=TOL, rtol=0)

    def test_other_frame_lengths_use_reference_loop(self):
        rng = np.random.default_rng(9)
        hpf = HighPassFilter(cutoff_hz=80.0, sample_rate=48000.0)
        frames = [(rng.standard_normal(n) * 0.3).astype(np.float32) for n in (240, 240, 100)]
        ref = reference_hpf(self._coef(hpf), frames)
        for fr, expected in zip(frames, ref):
            got = hpf.process(fr)
            self.assertLess(float(np.max(np.abs(got - expected))), TOL)
            self.assertEqual(len(got), len(fr))


# ----------------------------------------------------------------------------- noise gate anchor

class TestGateAnchor(unittest.TestCase):

    def test_incremental_percentile_matches_numpy_over_sliding_window(self):
        rng = np.random.default_rng(21)
        gate = AdaptiveNoiseGate(calibrate_startup=False)
        window = []
        for _ in range(600):
            value = float(rng.uniform(-40.0, -10.0))
            gate._anchor_push(value)
            window.append(value)
            window = window[-150:]
            self.assertEqual(sorted(gate._voiced_anchor_buffer), gate._anchor_sorted)
            if len(window) >= 10:
                expected = float(np.percentile(window, 90))
                self.assertAlmostEqual(gate._anchor_percentile90(), expected, places=12)

    def test_voiced_frames_update_anchor_like_numpy_definition(self):
        gate = AdaptiveNoiseGate(calibrate_startup=False)
        rng = np.random.default_rng(4)
        frame = np.zeros(FRAME, dtype=np.float32)
        for _ in range(40):
            level = float(rng.uniform(-34.0, -18.0))
            gate.process(frame, speech_prob=0.9, input_rms_db=level, in_place=True)
        self.assertEqual(len(gate._voiced_anchor_buffer), len(gate._anchor_sorted))
        expected = min(-12.0, max(-36.0, float(np.percentile(list(gate._voiced_anchor_buffer), 90))))
        self.assertAlmostEqual(gate.user_anchor_db, expected, places=12)

    def test_reset_and_recalibrate_clear_anchor_state(self):
        gate = AdaptiveNoiseGate(calibrate_startup=False)
        for v in (-30.0, -28.0, -26.0, -24.0, -22.0, -20.0, -18.0, -16.0, -14.0, -20.0):
            gate._anchor_push(v)
        gate.reset()
        self.assertEqual(len(gate._anchor_sorted), 0)
        self.assertEqual(len(gate._voiced_anchor_buffer), 0)
        for v in (-30.0, -28.0):
            gate._anchor_push(v)
        gate.recalibrate()
        self.assertEqual(len(gate._anchor_sorted), 0)
        self.assertEqual(len(gate._voiced_anchor_buffer), 0)


# ----------------------------------------------------------------------------- transient suppressor

class TestTransientVectorized(unittest.TestCase):

    def test_bell_table_rows_match_linspace_definition(self):
        for length in (1, 2, 25, 37, 49):
            t = np.linspace(0.0, np.pi, length, dtype=np.float32)
            np.testing.assert_array_equal(_TS_BELL[length, :length], np.sin(t) ** 2)
            self.assertTrue(np.all(_TS_BELL[length, length:] == 0.0))

    def test_vectorized_matches_reference_including_frame_edges(self):
        rng = np.random.default_rng(7)
        worst = 0.0
        spike_frames = 0
        for trial in range(200):
            x = (rng.standard_normal(FRAME) * rng.uniform(0.01, 0.3)).astype(np.float32)
            for _ in range(int(rng.integers(0, 6))):
                x[rng.integers(0, FRAME)] += rng.choice([-1.0, 1.0]) * rng.uniform(0.2, 0.9)
            if trial % 5 == 0:
                x[0] += 0.9
                x[1] -= 0.8
                x[-1] += 0.7
            prev = float(rng.uniform(-0.1, 0.1))
            ts = TransientSuppressor(skip_when_no_speech=False)
            ts._prev_sample = prev
            ref = reference_transient(x, prev)
            if not np.array_equal(ref, x):
                spike_frames += 1
            got = ts.process(x.copy(), 0.9)
            worst = max(worst, float(np.max(np.abs(got.astype(np.float64) - ref.astype(np.float64)))))
        self.assertGreater(spike_frames, 20, "test signal must exercise the spike path")
        self.assertLess(worst, TOL)

    def test_process_writes_into_frame_and_returns_it(self):
        ts = TransientSuppressor(skip_when_no_speech=False)
        x = np.zeros(FRAME, dtype=np.float32)
        x[100] = 0.9
        out = ts.process(x, 0.9)
        self.assertIs(out, x)

    def test_speech_skip_is_on_by_default_and_leaves_frame_untouched(self):
        ts = TransientSuppressor()
        self.assertTrue(ts.skip_when_no_speech)
        x = np.zeros(FRAME, dtype=np.float32)
        x[100] = 0.9
        before = x.copy()
        out = ts.process(x, TS_SKIP_SPEECH_PROB - 0.01)
        np.testing.assert_array_equal(out, before)

    def test_skip_keeps_previous_sample_state_for_next_frame(self):
        rng = np.random.default_rng(12)
        first = (rng.standard_normal(FRAME) * 0.2).astype(np.float32)
        first[-1] = 0.31
        second = (rng.standard_normal(FRAME) * 0.2).astype(np.float32)
        second[0] = 0.9
        ts = TransientSuppressor()
        ts.process(first, 0.05)                    # skipped, but its last sample must still be remembered
        got = ts.process(second.copy(), 0.9)
        ref = reference_transient(second, float(np.float32(0.31)))
        self.assertLess(float(np.max(np.abs(got - ref))), TOL)

    def test_skip_disabled_runs_stage_on_low_speech_probability(self):
        x = np.zeros(FRAME, dtype=np.float32)
        x[100] = 0.9
        ts = TransientSuppressor(skip_when_no_speech=False)
        out = ts.process(x.copy(), 0.05)
        self.assertFalse(np.array_equal(out, x))
        np.testing.assert_allclose(out, reference_transient(x, 0.0), atol=TOL, rtol=0)


# ----------------------------------------------------------------------------- in-place helpers

class TestInPlaceHelpers(unittest.TestCase):

    def test_calculate_levels_peak_matches_abs_definition(self):
        rng = np.random.default_rng(2)
        for _ in range(20):
            x = (rng.standard_normal(FRAME) * rng.uniform(0.01, 2.0)).astype(np.float32)
            peak_db, rms_db = calculate_levels(x)
            ref_peak = float(np.max(np.abs(x)))
            ref_ref = 32767.0 if ref_peak > 100.0 else 1.0
            self.assertAlmostEqual(peak_db, 20.0 * np.log10(max(ref_peak / ref_ref, 1e-5)), places=9)

    def test_soft_limit_in_place_equals_copy_version(self):
        rng = np.random.default_rng(8)
        x = (rng.standard_normal(FRAME) * 0.8).astype(np.float32)
        expected = soft_limit(x.copy(), threshold=0.85)
        work = x.copy()
        out = soft_limit(work, threshold=0.85, out=work)
        self.assertIs(out, work)
        np.testing.assert_array_equal(out, expected)

    def test_soft_limit_below_threshold_is_identity(self):
        x = np.linspace(-0.8, 0.8, FRAME, dtype=np.float32)
        self.assertIs(soft_limit(x, threshold=0.85), x)

    def test_soft_preclip_below_knee_returns_same_values(self):
        x = np.linspace(-0.7, 0.7, FRAME, dtype=np.float32)
        before = x.copy()
        out = soft_preclip(x, knee=0.75)
        np.testing.assert_array_equal(out, before)

    def test_process_mono_frame_matches_composed_stages(self):
        """process_mono_frame (in-place stages) equals the same stages applied one by one on copies."""

        class FakeRNN:
            def process_frame(self, frame):
                return frame * 0.5, 0.9

        rng = np.random.default_rng(30)
        frames = [(rng.standard_normal(FRAME) * 0.4).astype(np.float32) for _ in range(12)]
        frames[4][50] += 0.9          # force a transient spike and a pre-clip peak
        gate_a = AdaptiveNoiseGate(calibrate_startup=False, lookahead=True, floor_gain=0.06)
        gate_b = AdaptiveNoiseGate(calibrate_startup=False, lookahead=True, floor_gain=0.06)
        ts_a = TransientSuppressor()
        ts_b = TransientSuppressor()
        rn = FakeRNN()
        for fr in frames:
            out_a, sp_a, _, rms_a = process_mono_frame(
                fr.copy(), rn, None, gate_a, 1.08, True, None, ts_a, 0.0)

            x = fr.copy()
            _, rms_b = calculate_levels(x)
            x = soft_preclip(x, knee=0.95)
            x = np.clip(x, -1.0, 1.0)
            rn_out, sp_b = rn.process_frame(x * 32767.0)
            x = rn_out / 32767.0
            x = ts_b.process(x, sp_b)
            x = x * 1.08
            x, _ = gate_b.process(x, sp_b, input_rms_db=rms_b, in_place=True)
            x = soft_limit(x, threshold=0.85)

            self.assertAlmostEqual(sp_a, sp_b)
            self.assertLess(float(np.max(np.abs(out_a - x))), 1e-7)
            self.assertAlmostEqual(rms_a, rms_b)

    def test_leveler_applies_gain_in_place(self):
        lev = SpeechLeveler()
        lev.current_gain = 1.5
        x = np.full(FRAME, 0.1, dtype=np.float32)
        out, gain = lev.process(x, speech_prob=0.0, input_rms_db=-60.0)
        self.assertIs(out, x)
        self.assertAlmostEqual(float(x[0]), 0.1 * gain, places=6)


# ----------------------------------------------------------------------------- stream watchdog

class TestWatchdogWithoutLock(unittest.TestCase):

    @patch("src.stream.sd.query_devices")
    def test_callback_updates_timestamp_without_lock(self, mock_query):
        mock_query.return_value = {'name': 'Mock Mic', 'max_input_channels': 1, 'max_output_channels': 1}
        engine = AudioEngine(input_device=0, output_device=0, denoise_enabled=False)
        self.assertFalse(hasattr(engine, "_watchdog_lock"))

        engine._running = True
        engine._stream = MagicMock(active=True)
        indata = np.zeros((FRAME, 1), dtype=np.float32)
        outdata = np.zeros((FRAME, 1), dtype=np.float32)
        engine._last_callback_time = 0.0
        engine._audio_callback(indata, outdata, FRAME, None, None)
        self.assertGreater(engine._last_callback_time, 0.0)
        self.assertEqual(engine.callback_error_count, 0)
        self.assertTrue(engine.is_stream_alive())

        engine._last_callback_time = time.monotonic() - 5.0
        self.assertFalse(engine.is_stream_alive())


# ----------------------------------------------------------------------------- bus-type cache

class TestBusTypeCache(unittest.TestCase):

    def setUp(self):
        clear_device_bus_type_cache()

    def tearDown(self):
        clear_device_bus_type_cache()

    @patch("src.win_audio.native_get_device_bus_type", return_value="USB")
    def test_repeated_lookup_queries_windows_once(self, raw):
        self.assertEqual(native_get_device_bus_type_cached("Mic A", is_input=True), "USB")
        self.assertEqual(native_get_device_bus_type_cached("Mic A", is_input=True), "USB")
        self.assertEqual(raw.call_count, 1)

    @patch("src.win_audio.native_get_device_bus_type", return_value="USB")
    def test_key_includes_direction(self, raw):
        native_get_device_bus_type_cached("Mic A", is_input=True)
        native_get_device_bus_type_cached("Mic A", is_input=False)
        self.assertEqual(raw.call_count, 2)

    @patch("src.win_audio.native_get_device_bus_type", return_value="USB")
    def test_clear_forces_requery(self, raw):
        native_get_device_bus_type_cached("Mic A", is_input=True)
        clear_device_bus_type_cache()
        self.assertEqual(len(_bus_type_cache), 0)
        native_get_device_bus_type_cached("Mic A", is_input=True)
        self.assertEqual(raw.call_count, 2)

    @patch("src.win_audio.native_get_device_bus_type", return_value="SWD")
    def test_enumeration_classifier_uses_cache(self, raw):
        self.assertTrue(is_virtual_input_device("Some Microphone"))
        self.assertTrue(is_virtual_input_device("Some Microphone"))
        self.assertEqual(raw.call_count, 1)

    @patch("src.win_audio.native_get_device_bus_type", return_value="SWD")
    @patch("src.devices.sd")
    def test_new_enumeration_pass_clears_cache(self, mock_sd, raw):
        mock_sd.query_devices.return_value = []
        mock_sd.query_hostapis.return_value = []
        is_virtual_input_device("Some Microphone")
        self.assertIn(("Some Microphone", True), _bus_type_cache)
        get_all_devices()
        self.assertEqual(len(_bus_type_cache), 0)
        is_virtual_input_device("Some Microphone")
        self.assertEqual(raw.call_count, 2)


if __name__ == "__main__":
    unittest.main()
