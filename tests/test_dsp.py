"""
Unit Tests for DSP Filter, Noise Gate, and RNNoise Inference.
"""
import unittest
import numpy as np
from src.dsp import (
    HighPassFilter,
    AdaptiveNoiseGate,
    calculate_levels,
    SpeechLeveler,
    process_mono_frame,
)
from src.rnnoise import RNNoise, FRAME_SIZE, SAMPLE_RATE


class TestDSP(unittest.TestCase):

    def test_high_pass_filter_attenuates_cooling_pad_rumble(self):
        """Verify 80Hz HPF strongly attenuates 40Hz cooling pad rumble while preserving voice."""
        hpf = HighPassFilter(cutoff_hz=80.0, sample_rate=float(SAMPLE_RATE))
        t = np.linspace(0, 0.1, int(SAMPLE_RATE * 0.1), False)

        # 40 Hz sub-bass cooling pad motor rumble
        rumble = np.sin(2 * np.pi * 40 * t).astype(np.float32)
        filtered_rumble = hpf.process(rumble)
        rumble_in_rms = np.sqrt(np.mean(rumble ** 2))
        rumble_out_rms = np.sqrt(np.mean(filtered_rumble[100:] ** 2))

        # Attenuation should be significant (> 10 dB)
        self.assertLess(rumble_out_rms, rumble_in_rms * 0.35, "HPF failed to attenuate 40Hz rumble")

        # 1000 Hz speech harmonic
        hpf.reset()
        speech = np.sin(2 * np.pi * 1000 * t).astype(np.float32)
        filtered_speech = hpf.process(speech)
        speech_in_rms = np.sqrt(np.mean(speech ** 2))
        speech_out_rms = np.sqrt(np.mean(filtered_speech[100:] ** 2))

        # 1000 Hz speech must pass through intact (> 98%)
        self.assertAlmostEqual(speech_out_rms, speech_in_rms, delta=0.05, msg="HPF distorted 1000Hz speech")

    def test_adaptive_noise_gate_hangover_and_decay(self):
        """Verify noise gate opens smoothly (15ms attack), holds open via hysteresis, and softly decays."""
        gate = AdaptiveNoiseGate(
            threshold=0.75,
            close_threshold=0.45,
            hangover_ms=40.0,
            decay_ms=20.0,
            attack_ms=15.0,
            frame_ms=10.0,
        )
        frame = np.ones(FRAME_SIZE, dtype=np.float32) * 0.5

        # Frame 1: Active speech (prob = 0.95) -> Gate begins smooth 15ms attack ramp (gain ~ 0.487)
        gated_frame, gain = gate.process(frame, 0.95)
        self.assertAlmostEqual(gain, 0.487, places=2, msg="First frame should follow 15ms exponential attack curve")

        # Frames 2-11: Continuing speech -> Gate smoothly reaches 1.0 by frame 11 (~100ms)
        for _ in range(10):
            gated_frame, gain = gate.process(frame, 0.95)
        self.assertEqual(gain, 1.0, "Gate should be fully open (1.0) after attack phase")

        # Frame 12: Trailing consonant / word ending (prob = 0.55 >= close_threshold) -> Must stay 100% open
        gated_frame, gain = gate.process(frame, 0.55)
        self.assertEqual(gain, 1.0, "Hysteresis failed to hold gate open during word ending")

        # Frames 13-16: Complete silence, in hangover window (40ms = 4 frames)
        for i in range(4):
            gated_frame, gain = gate.process(frame, 0.1)
            self.assertEqual(gain, 1.0, f"Hangover frame {i+1} closed prematurely")

        # Frame 17: Hangover expired -> Gate must begin smooth decay
        gated_frame, gain = gate.process(frame, 0.1)
        self.assertLess(gain, 1.0, "Gate failed to decay after hangover")

        # After several more frames, gate reaches pure silence (0.0)
        for _ in range(25):
            gated_frame, gain = gate.process(frame, 0.0)
        self.assertEqual(gain, 0.0, "Gate failed to reach complete silence")
        np.testing.assert_array_equal(gated_frame, np.zeros_like(frame))

    def test_adaptive_noise_gate_dual_key_onset_and_continuity(self):
        """Verify dual-key onset trigger fast-opens on low VAD with energy jump."""
        gate = AdaptiveNoiseGate(
            threshold=0.70,
            close_threshold=0.45,
            hangover_ms=360.0,
            decay_ms=80.0,
            attack_ms=8.0,
            frame_ms=10.0,
            onset_threshold=0.30,
            onset_snr_db=7.0,
        )
        frame = np.ones(FRAME_SIZE, dtype=np.float32) * 0.1

        # Frame 1-5: Background fan noise (-50 dB, prob 0.05) -> Gate must stay completely closed
        for _ in range(5):
            gated, gain = gate.process(frame, speech_prob=0.05, input_rms_db=-50.0)
            self.assertEqual(gain, 0.0, "Fan noise should not open gate")

        # Frame 6: Early consonant onset ('h', 's', 't') -> prob 0.35 (< 0.70 threshold), but -35 dB (> -50 + 7 dB)
        gated, gain = gate.process(frame, speech_prob=0.35, input_rms_db=-35.0)
        self.assertTrue(gate.is_open, "Dual-key onset trigger should open gate on consonant energy rise")
        self.assertGreater(gain, 0.0, "Gate should ramp open on initial frame")

        # Frame 7: Loud voiced vowel follows (prob 0.85, -20 dB) -> Smooth ramp towards 1.0
        gated, gain = gate.process(frame, speech_prob=0.85, input_rms_db=-20.0)
        self.assertGreaterEqual(gain, 0.90)

    def test_adaptive_noise_gate_floor_gain_continuity(self):
        """Verify floor_gain holds gain at minimum non-zero level and smoothly attacks without clicks."""
        floor = 0.06
        gate = AdaptiveNoiseGate(
            threshold=0.70,
            close_threshold=0.45,
            hangover_ms=40.0,
            decay_ms=20.0,
            frame_ms=10.0,
            floor_gain=floor,
            lookahead=True,
        )
        frame = np.ones(FRAME_SIZE, dtype=np.float32) * 0.5

        # 1. Steady silence: gain should be floor_gain, output scaled by floor_gain
        for _ in range(5):
            out, gain = gate.process(frame, speech_prob=0.0)
            self.assertAlmostEqual(gain, floor, places=4)
        np.testing.assert_allclose(out, frame * floor, atol=1e-5)

        # 2. Speech onset: gate opens, gain rises above floor_gain smoothly
        out, gain = gate.process(frame, speech_prob=0.95)
        self.assertTrue(gate.is_open)
        self.assertGreater(gain, floor)

        # 3. Hold open
        for _ in range(15):
            out, gain = gate.process(frame, speech_prob=0.95)
        self.assertAlmostEqual(gain, 1.0, places=2)

        # 4. Speech ceases, hangover expires, decay settles at floor_gain
        for _ in range(4):  # hangover frames
            gate.process(frame, speech_prob=0.0)

        # After decay, settles cleanly at floor_gain (not 0.0)
        for _ in range(30):
            out, gain = gate.process(frame, speech_prob=0.0)
        self.assertAlmostEqual(gain, floor, places=4)
        np.testing.assert_allclose(out, frame * floor, atol=1e-5)

    def test_septic_smootherstep_c3_properties(self):
        """Verify _S_CURVE_TABLE_480 is monotonically increasing with C3 ultra-smooth boundary continuity."""
        from src.dsp import _S_CURVE_TABLE_480
        self.assertEqual(len(_S_CURVE_TABLE_480), 480)
        self.assertAlmostEqual(_S_CURVE_TABLE_480[0], 0.0, places=5)
        self.assertAlmostEqual(_S_CURVE_TABLE_480[-1], 1.0, places=5)
        # Monotonically non-decreasing
        diff = np.diff(_S_CURVE_TABLE_480)
        self.assertTrue(np.all(diff >= 0.0), "S-curve must be monotonically increasing")
        # Near-zero boundary velocity (C1) and near-zero boundary acceleration (C2/C3)
        self.assertLess(diff[0], 1e-6)
        self.assertLess(diff[-1], 1e-6)

    def test_calculate_levels_no_inversion_dropout(self):
        """Verify calculate_levels does not drop to -90 dBFS when peak exceeds 1.0 (e.g. mic boost / output gain)."""
        # Normal frame with peak 0.5
        frame_normal = np.ones(FRAME_SIZE, dtype=np.float32) * 0.5
        peak_norm, _ = calculate_levels(frame_normal)
        self.assertAlmostEqual(peak_norm, -6.02, places=1)

        # Boosted frame with peak 1.08 (+0.7 dB output gain or mic boost)
        frame_boosted = np.ones(FRAME_SIZE, dtype=np.float32) * 1.08
        peak_boost, _ = calculate_levels(frame_boosted)
        self.assertGreater(peak_boost, 0.0, "Boosted peak should be positive dBFS, not inverted")
        self.assertAlmostEqual(peak_boost, 0.67, places=1)

    def test_adaptive_noise_gate_in_place_silence(self):
        """Verify in_place=True safely fills the buffer with zeros during silence."""
        gate = AdaptiveNoiseGate(threshold=0.75, decay_ms=10.0, frame_ms=10.0)
        frame = np.ones(FRAME_SIZE, dtype=np.float32)
        out_frame, gain = gate.process(frame, 0.0, in_place=True)
        self.assertEqual(gain, 0.0)
        self.assertIs(out_frame, frame, "in_place=True should return the same buffer")
        self.assertTrue(np.all(frame == 0.0), "Buffer should be filled with zeros")

    def test_high_pass_filter_buffer_reuse(self):
        """Verify HighPassFilter reuses its internal buffer across frames with zero reallocations."""
        hpf = HighPassFilter(cutoff_hz=80.0, sample_rate=48000.0)
        frame1 = np.ones(FRAME_SIZE, dtype=np.float32)
        out1 = hpf.process(frame1)
        frame2 = np.ones(FRAME_SIZE, dtype=np.float32)
        out2 = hpf.process(frame2)
        self.assertIs(out1, out2, "HighPassFilter should reuse the same output buffer")

    def test_rnnoise_inference_latency(self):
        """Verify RNNoise runs faster than real-time (< 1.5ms per 10ms frame)."""
        import time
        rnnoise = RNNoise()
        frame = np.random.randn(FRAME_SIZE).astype(np.float32) * 1000.0

        times = []
        for _ in range(100):
            t0 = time.perf_counter()
            out_frame, prob = rnnoise.process_frame(frame.copy())
            times.append(time.perf_counter() - t0)

        avg_ms = (sum(times) / len(times)) * 1000.0
        rnnoise.close()
        print(f"\n[BENCHMARK] Average RNNoise inference: {avg_ms:.3f} ms (Frame budget: 10.0 ms)")
        self.assertLess(avg_ms, 5.0, f"Inference took {avg_ms} ms, exceeding real-time budget")


    def test_adaptive_noise_gate_raised_cosine_s_curve_ramping(self):
        """Verify noise gate uses smooth Raised-Cosine (Hann) S-curve ramping without step discontinuities."""
        gate = AdaptiveNoiseGate()
        self.assertEqual(gate.floor_gain, 0.0)
        self.assertEqual(gate.onset_snr_db, 10.0)
        self.assertEqual(gate.attack_rate, 1.0 - np.exp(-10.0 / 28.0))

        # Transition frame from silence to open
        frame = np.ones(FRAME_SIZE, dtype=np.float32)
        out_frame, end_gain = gate.process(frame.copy(), speech_prob=0.9)
        self.assertGreater(end_gain, 0.0)

        # In Raised-Cosine, derivative at boundaries t=0 and t=N-1 is zero (no clicks)
        # Check first step is smaller than linear step: out_frame[1] - out_frame[0] < out_frame[240] - out_frame[239]
        slope_start = out_frame[1] - out_frame[0]
        slope_mid = out_frame[240] - out_frame[239]
        self.assertLess(slope_start, slope_mid, "S-curve ramp must have gentler slope at onset than at midpoint")

    def test_adaptive_noise_gate_lookahead_natural_onset(self):
        """Verify 1-frame pre-roll lookahead allows gate to ramp open on silence, passing speech at full gain."""
        gate = AdaptiveNoiseGate(lookahead=True)
        # Frame 1: Pure silence
        f_silence = np.zeros(FRAME_SIZE, dtype=np.float32)
        out1, g1 = gate.process(f_silence.copy(), speech_prob=0.0)
        self.assertEqual(g1, 0.0)
        np.testing.assert_array_equal(out1, np.zeros(FRAME_SIZE, dtype=np.float32))

        # Frame 2: Loud voice suddenly begins
        f_speech = np.ones(FRAME_SIZE, dtype=np.float32) * 0.75
        out2, g2 = gate.process(f_speech.copy(), speech_prob=0.95, input_rms_db=-20.0)
        # Out2 is the pre-roll silence buffer ramping open
        self.assertGreater(g2, 0.0)

        # Frame 3: Continuing voice
        f_speech2 = np.ones(FRAME_SIZE, dtype=np.float32) * 0.75
        out3, g3 = gate.process(f_speech2.copy(), speech_prob=0.95, input_rms_db=-20.0)
        # Out3 is the actual first speech frame, delivered at smooth high gain (>= 0.50)
        self.assertGreaterEqual(g3, 0.50)
        self.assertAlmostEqual(float(np.max(out3)), 0.75 * g3, places=2)

    def test_adaptive_noise_gate_rearm_cooldown_blocks_background_chatter(self):
        """Verify 150ms Gate Re-Arm Cooldown blocks low-confidence chatter but allows high-confidence voice."""
        gate = AdaptiveNoiseGate(
            threshold=0.70,
            close_threshold=0.55,
            hangover_ms=20.0,  # 2 frames hangover
            cooldown_ms=50.0,  # 5 frames cooldown
            onset_threshold=0.35,
            onset_snr_db=10.0,
            floor_gain=0.06,
        )
        frame = np.ones(FRAME_SIZE, dtype=np.float32) * 0.2

        # 1. User speaks with loud voice (prob 0.90) -> Gate opens
        gate.process(frame, speech_prob=0.90, input_rms_db=-20.0)
        self.assertTrue(gate.is_open)

        # 2. User stops, hangover expires (2 frames) -> Gate closes, cooldown starts
        gate.process(frame, speech_prob=0.10, input_rms_db=-65.0)
        gate.process(frame, speech_prob=0.10, input_rms_db=-65.0)
        gate.process(frame, speech_prob=0.10, input_rms_db=-65.0)
        self.assertFalse(gate.is_open, "Gate should be closed after hangover")

        # 3. Background chatter arrives during cooldown (prob 0.45, SNR +15dB) -> Must be blocked!
        out, gain = gate.process(frame, speech_prob=0.45, input_rms_db=-50.0)
        self.assertFalse(gate.is_open, "Background chatter during cooldown must NOT re-open gate")

        # 4. High-confidence user speech arrives (prob 0.85) -> Must bypass cooldown immediately!
        out, gain = gate.process(frame, speech_prob=0.85, input_rms_db=-20.0)
        self.assertTrue(gate.is_open, "High-confidence user voice must immediately bypass cooldown")

    def test_adaptive_noise_gate_energy_guarded_sustain_for_consonants(self):
        """Verify unvoiced consonants (s, t, k) with acoustic energy hold gate open without starting decay."""
        gate = AdaptiveNoiseGate(
            threshold=0.70,
            close_threshold=0.52,
            hangover_ms=40.0,  # 4 frames
            decay_ms=20.0,
            frame_ms=10.0,
            floor_gain=0.06,
            sustain_snr_db=6.0,
            sustain_threshold=0.25,
        )
        frame = np.ones(FRAME_SIZE, dtype=np.float32) * 0.2

        # 1. User speaks vowel (prob 0.90, -25 dB) -> Gate opens fully
        for _ in range(20):
            gate.process(frame, speech_prob=0.90, input_rms_db=-25.0)
        self.assertTrue(gate.is_open)
        self.assertEqual(gate.current_gain, 1.0)

        # 2. Mid-word unvoiced consonant (e.g. 's' in 'faster' or 't' in 'laptop'):
        # Low speech probability (0.35 < close_threshold 0.52), but loud voice energy (-35 dB > noise floor + 6 dB)
        for _ in range(8):  # 80ms duration (longer than 40ms hangover!)
            out, gain = gate.process(frame, speech_prob=0.35, input_rms_db=-35.0)
            self.assertTrue(gate.is_open, "Unvoiced consonant must sustain gate open via acoustic energy")
            self.assertAlmostEqual(gain, 1.0, places=2, msg="Gain must stay 1.0 without fading")
            self.assertEqual(gate.frames_since_speech, 0, "frames_since_speech must not drain during speech")

        # 3. Ambient fan noise follows (prob 0.05, -60 dB) -> Energy sustain must NOT trigger
        for _ in range(5):
            gate.process(frame, speech_prob=0.05, input_rms_db=-60.0)
        # After 4 frames hangover + 1 frame decay, gain must drop
        self.assertLess(gate.current_gain, 1.0, "Gate must decay once speech stops")

    def test_adaptive_noise_gate_sustain_capped_against_steady_noise(self):
        """Verify the sustain rule does not pin the gate open forever under continuous noise."""
        gate = AdaptiveNoiseGate(
            hangover_ms=320.0,
            calibrate_startup=False,
            close_threshold=0.52,
            sustain_snr_db=6.0,
            sustain_threshold=0.25,
        )
        gate.noise_floor_db = -60.0
        frame = np.ones(FRAME_SIZE, dtype=np.float32) * 0.1

        # 1. Open gate with clear speech
        gate.process(frame, speech_prob=0.85, input_rms_db=-30.0)
        self.assertTrue(gate.is_open)

        # 2. Feed steady noise (SNR = 10 dB, speech_prob = 0.30)
        # Gate must close after ~72 frames (40 frames sustain cap + 32 hangover frames) instead of running forever
        frames_stayed_open = 0
        for _ in range(500):
            gate.process(frame, speech_prob=0.30, input_rms_db=-50.0)
            if gate.is_open:
                frames_stayed_open += 1

        self.assertFalse(gate.is_open, "Gate must close when steady noise continues past the sustain window")
        self.assertLessEqual(frames_stayed_open, 75, "Sustain window must cap within ~72 frames")

    def test_adaptive_noise_gate_cooldown_breakthrough_tightened(self):
        """Verify tightened breakthrough (prob >= 0.55, SNR >= onset_snr_db + 8) rejects loud non-speech transients."""
        gate = AdaptiveNoiseGate(cooldown_ms=150.0, onset_snr_db=10.0, calibrate_startup=False)
        gate.noise_floor_db = -60.0
        frame = np.ones(FRAME_SIZE, dtype=np.float32)

        # Open and then immediately close gate
        gate.process(frame, speech_prob=0.90, input_rms_db=-25.0)
        for _ in range(gate.hangover_frames + 5):
            gate.process(frame, speech_prob=0.0, input_rms_db=-60.0)
        self.assertFalse(gate.is_open)
        self.assertLess(gate.frames_since_close, gate.cooldown_frames)

        # Loud transient (prob 0.45, SNR +15 dB) during cooldown must NOT break through
        gate.process(frame, speech_prob=0.45, input_rms_db=-45.0)
        self.assertFalse(gate.is_open, "Loud transient with prob < 0.55 must be rejected during cooldown")

        # Loud genuine speech (prob 0.60, SNR +20 dB) must break through immediately
        gate.process(frame, speech_prob=0.60, input_rms_db=-40.0)
        self.assertTrue(gate.is_open, "High-energy near-field speech must break through cooldown")

    def test_speech_leveler_freezes_during_silence(self):
        """Verify leveler estimate freezes during silence and low speech confidence (zero noise pumping)."""
        leveler = SpeechLeveler(target_rms_db=-24.0, max_boost_db=9.0, voicing_threshold=0.60)
        frame = np.zeros(FRAME_SIZE, dtype=np.float32)

        # Feed silence with speech_prob 0.10
        _, gain = leveler.process(frame, speech_prob=0.10, input_rms_db=-65.0)
        self.assertEqual(gain, 1.0)
        self.assertEqual(leveler.voiced_rms_db, -24.0)

        # Feed noise with speech_prob 0.40 (below 0.60)
        _, gain = leveler.process(frame, speech_prob=0.40, input_rms_db=-40.0)
        self.assertEqual(gain, 1.0)
        self.assertEqual(leveler.voiced_rms_db, -24.0)

    def test_speech_leveler_boosts_quiet_speech_up_to_clamp(self):
        """Verify leveler adapts upward on quiet speech and respects max boost clamp (+9.0 dB = ~2.82x)."""
        leveler = SpeechLeveler(target_rms_db=-24.0, max_boost_db=9.0, voicing_threshold=0.60, max_slew_up_db_per_sec=100.0)
        frame = np.ones(FRAME_SIZE, dtype=np.float32) * 0.05

        # Feed 100 frames (~1s) of quiet speech at -35 dBFS
        for _ in range(100):
            out, gain = leveler.process(frame.copy(), speech_prob=0.90, input_rms_db=-35.0)

        # Gain must rise and be clamped to <= 2.82x (+9.0 dB)
        self.assertGreater(gain, 1.5)
        self.assertLessEqual(gain, 2.83)

    def test_unified_process_mono_frame_pipeline(self):
        """Verify process_mono_frame executes full 7-stage chain without crashes."""
        gate = AdaptiveNoiseGate(lookahead=False, calibrate_startup=False)
        frame = np.ones(FRAME_SIZE, dtype=np.float32) * 0.1
        out, sp, peak, rms = process_mono_frame(
            frame_mono=frame.copy(),
            rnnoise=None,
            hpf=None,
            gate=gate,
            total_gain=1.0,
            denoise_enabled=False,
            leveler=None,
        )
        self.assertEqual(len(out), FRAME_SIZE)
        self.assertIsInstance(sp, float)
        self.assertIsInstance(peak, float)
        self.assertIsInstance(rms, float)


if __name__ == "__main__":
    unittest.main()


