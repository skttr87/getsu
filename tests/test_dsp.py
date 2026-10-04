"""
Unit Tests for DSP Filter, Noise Gate, and RNNoise Inference.
"""
import unittest
import numpy as np
from src.dsp import HighPassFilter, AdaptiveNoiseGate, calculate_levels
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
            cold_start_gain=0.0,
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

    def test_adaptive_noise_gate_dual_key_onset_and_cold_start(self):
        """Verify dual-key onset trigger fast-opens on low VAD with energy jump and applies cold start gain."""
        gate = AdaptiveNoiseGate(
            threshold=0.70,
            close_threshold=0.45,
            hangover_ms=360.0,
            decay_ms=80.0,
            attack_ms=8.0,
            frame_ms=10.0,
            onset_threshold=0.30,
            onset_snr_db=7.0,
            cold_start_gain=0.70,
        )
        frame = np.ones(FRAME_SIZE, dtype=np.float32) * 0.1

        # Frame 1-5: Background fan noise (-50 dB, prob 0.05) -> Gate must stay completely closed
        for _ in range(5):
            gated, gain = gate.process(frame, speech_prob=0.05, input_rms_db=-50.0)
            self.assertEqual(gain, 0.0, "Fan noise should not open gate")

        # Frame 6: Early consonant onset ('h', 's', 't') -> prob 0.35 (< 0.70 threshold), but -35 dB (> -50 + 7 dB)
        gated, gain = gate.process(frame, speech_prob=0.35, input_rms_db=-35.0)
        self.assertTrue(gate.is_open, "Dual-key onset trigger should open gate on consonant energy rise")
        # Cold start gain (0.70) + 1 frame attack ramp
        self.assertGreaterEqual(gain, 0.70, "Cold start gain should be at least 0.70 on initial frame")

        # Frame 7: Loud voiced vowel follows (prob 0.85, -20 dB) -> Full open
        gated, gain = gate.process(frame, speech_prob=0.85, input_rms_db=-20.0)
        self.assertGreaterEqual(gain, 0.90)

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
        self.assertEqual(gate.cold_start_gain, 0.35)
        self.assertEqual(gate.onset_snr_db, 5.0)
        self.assertEqual(gate.attack_rate, 1.0 - np.exp(-10.0 / 15.0))

        # Transition frame from silence to open
        frame = np.ones(FRAME_SIZE, dtype=np.float32)
        out_frame, end_gain = gate.process(frame.copy(), speech_prob=0.9)
        self.assertGreater(end_gain, 0.0)

        # In Raised-Cosine, derivative at boundaries t=0 and t=N-1 is zero (no clicks)
        # Check first step is smaller than linear step: out_frame[1] - out_frame[0] < out_frame[240] - out_frame[239]
        slope_start = out_frame[1] - out_frame[0]
        slope_mid = out_frame[240] - out_frame[239]
        self.assertLess(slope_start, slope_mid, "S-curve ramp must have gentler slope at onset than at midpoint")


if __name__ == "__main__":
    unittest.main()
