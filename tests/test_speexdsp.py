import unittest

import numpy as np

from src.speexdsp import Resampler, Preprocessor, RESAMPLER_QUALITY_DEFAULT


def _sine(freq, rate, seconds, amp=10000.0):
    t = np.arange(int(rate * seconds), dtype=np.float64) / rate
    return (amp * np.sin(2.0 * np.pi * freq * t)).astype(np.float32)


def _run_blocks(res, x, block):
    parts = []
    for i in range(0, len(x), block):
        y = res.process(np.ascontiguousarray(x[i:i + block]))
        parts.append(np.array(y, copy=True))
    return np.concatenate(parts)


class ResamplerTests(unittest.TestCase):

    def test_round_trip_48k_16k_48k_snr_above_60db(self):
        rate = 48000
        x = _sine(1000.0, rate, 1.0)
        down = Resampler(48000, 16000, quality=RESAMPLER_QUALITY_DEFAULT)
        up = Resampler(16000, 48000, quality=RESAMPLER_QUALITY_DEFAULT)
        mid = _run_blocks(down, x, 480)
        y = _run_blocks(up, mid, 160)
        # The round trip has a fixed filter delay. Find it by cross-correlation on the
        # steady-state region, then measure the error at that lag.
        guard = 2000
        ref = x[guard:-guard].astype(np.float64)
        best_lag, best_corr = 0, -np.inf
        for lag in range(0, 800):
            seg = y[guard + lag:guard + lag + len(ref)].astype(np.float64)
            corr = float(np.dot(seg, ref))
            if corr > best_corr:
                best_lag, best_corr = lag, corr
        aligned = y[guard + best_lag:guard + best_lag + len(ref)].astype(np.float64)
        err = ref - aligned
        snr_db = 10.0 * np.log10(np.sum(ref ** 2) / np.sum(err ** 2))
        self.assertGreater(snr_db, 60.0, f"SNR {snr_db:.1f} dB at lag {best_lag}")

    def test_44k1_to_48k_length_and_continuity(self):
        rate_in, rate_out = 44100, 48000
        blocks = 100  # 1 s of 10 ms blocks
        x = _sine(1000.0, rate_in, blocks * 0.01)
        res = Resampler(rate_in, rate_out)
        counts = []
        parts = []
        for i in range(blocks):
            y = res.process(np.ascontiguousarray(x[i * 441:(i + 1) * 441]))
            counts.append(len(y))
            parts.append(np.array(y, copy=True))
        out = np.concatenate(parts)
        self.assertLessEqual(abs(len(out) - 48000), 2, f"total {len(out)}")
        self.assertTrue(all(c in (479, 480, 481) for c in counts), f"block counts {set(counts)}")
        # 1 kHz at 48 kHz: the largest sample-to-sample step of a full-scale sine is
        # 2*pi*1000/48000 * amplitude. A block-boundary glitch would exceed that.
        max_step = 2.0 * np.pi * 1000.0 / 48000.0 * 10000.0
        steps = np.abs(np.diff(out[200:-200].astype(np.float64)))
        self.assertLess(float(steps.max()), 1.1 * max_step)

    def test_identity_of_returned_buffer(self):
        res = Resampler(16000, 48000)
        x = np.zeros(160, dtype=np.float32)
        first = res.process(x)
        second = res.process(x)
        self.assertIs(first.base, second.base)
        self.assertEqual(first.__array_interface__['data'][0], second.__array_interface__['data'][0])
        res.close()

    def test_rejects_wrong_dtype_instead_of_copying(self):
        res = Resampler(16000, 48000)
        with self.assertRaises(TypeError):
            res.process(np.zeros(160, dtype=np.float64))
        res.close()

    def test_lifecycle(self):
        res = Resampler(48000, 16000)
        res.process(np.zeros(480, dtype=np.float32))
        res.reset()
        res.close()
        res.close()  # second close is a no-op
        with self.assertRaises(RuntimeError):
            res.process(np.zeros(480, dtype=np.float32))


class PreprocessorTests(unittest.TestCase):

    def test_shape_and_no_nan_inf(self):
        pre = Preprocessor(480, 48000)
        pre.set_denoise(True)
        pre.set_noise_suppress_db(-30)
        pre.set_agc(False)
        rng = np.random.default_rng(1)
        sig = _sine(440.0, 48000, 1.0)
        noise = rng.normal(0.0, 300.0, size=sig.shape).astype(np.float32)
        x = (sig + noise).astype(np.float32)
        for i in range(0, len(x) - 480, 480):
            out = pre.process(np.ascontiguousarray(x[i:i + 480]))
            self.assertEqual(out.shape, (480,))
            self.assertEqual(out.dtype, np.float32)
            self.assertTrue(np.isfinite(out).all())
        pre.close()

    def test_output_on_int16_scale_and_clipping(self):
        pre = Preprocessor(480, 48000)
        pre.set_denoise(False)
        loud = np.full(480, 1.0e6, dtype=np.float32)  # far outside int16, must be clipped
        out = pre.process(loud)
        self.assertTrue(np.isfinite(out).all())
        self.assertLessEqual(float(np.abs(out).max()), 32768.0)
        pre.close()

    def test_output_buffer_is_reused(self):
        pre = Preprocessor(480, 48000)
        frame = np.zeros(480, dtype=np.float32)
        a = pre.process(frame)
        b = pre.process(frame)
        self.assertEqual(a.__array_interface__['data'][0], b.__array_interface__['data'][0])
        pre.close()

    def test_lifecycle(self):
        pre = Preprocessor(480, 48000)
        pre.set_agc(True)
        pre.close()
        pre.close()
        with self.assertRaises(RuntimeError):
            pre.process(np.zeros(480, dtype=np.float32))

    def test_wrong_frame_size_rejected(self):
        pre = Preprocessor(480, 48000)
        with self.assertRaises(ValueError):
            pre.process(np.zeros(240, dtype=np.float32))
        pre.close()


if __name__ == '__main__':
    unittest.main()
