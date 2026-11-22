import unittest
from unittest.mock import MagicMock, patch

import numpy as np

import src.stream as stream_mod
from src.stream import AudioEngine, LADDER_CODES, refresh_input_index
from src.host_api_resolver import reresolve_index, resolve_pair, DeviceResolutionError
from src.speexdsp import Resampler

JABRA = "Микрофон гарнитуры (Jabra EVOLVE 20 MS)"
CABLE = "CABLE Input (VB-Audio Virtual Cable)"


def make_env(mic_rate=16000):
    """
    Host APIs: 0 MME (unused), 1 DirectSound, 2 WASAPI, 3 WDM-KS (unused).
    Devices: 0 DS mic, 1 DS CABLE Input, 2 WASAPI mic (selected), 3 WASAPI CABLE Input.
    """
    devices = [
        {"name": JABRA, "hostapi": 1, "max_input_channels": 1, "max_output_channels": 0, "default_samplerate": float(mic_rate)},
        {"name": CABLE, "hostapi": 1, "max_input_channels": 0, "max_output_channels": 2, "default_samplerate": 48000.0},
        {"name": JABRA, "hostapi": 2, "max_input_channels": 1, "max_output_channels": 0, "default_samplerate": float(mic_rate)},
        {"name": CABLE, "hostapi": 2, "max_input_channels": 0, "max_output_channels": 2, "default_samplerate": 48000.0},
    ]
    hostapis = [
        {"name": "MME", "devices": [], "default_input_device": -1, "default_output_device": -1},
        {"name": "Windows DirectSound", "devices": [0, 1], "default_input_device": 0, "default_output_device": 1},
        {"name": "Windows WASAPI", "devices": [2, 3], "default_input_device": 2, "default_output_device": 3},
        {"name": "Windows WDM-KS", "devices": [], "default_input_device": -1, "default_output_device": -1},
    ]
    return devices, hostapis


class FakeSd:
    """sounddevice stand-in for AudioEngine. `plan(samplerate, device)` returns an error message or None."""

    def __init__(self, mic_rate=16000, plan=None):
        self.devices, self.hostapis = make_env(mic_rate)
        self.plan = plan or (lambda rate, device: None)
        self.opened = []
        self.default = MagicMock()

    def query_devices(self, index=None):
        return self.devices if index is None else self.devices[index]

    def query_hostapis(self, index=None):
        return self.hostapis if index is None else self.hostapis[index]

    def WasapiSettings(self, auto_convert=False):
        return ("wasapi", auto_convert)

    def Stream(self, samplerate, blocksize, device, channels, dtype, latency, extra_settings, callback):
        self.opened.append({"rate": samplerate, "block": blocksize, "device": tuple(device),
                            "channels": tuple(channels), "callback": callback})
        message = self.plan(samplerate, tuple(device))
        if message:
            raise RuntimeError(message)
        stream = MagicMock()
        stream.active = True
        return stream


def fail(code):
    return f"Error opening Stream: Invalid device [PaErrorCode {code}]"


class LadderTests(unittest.TestCase):
    def _engine(self, fake):
        with patch("src.stream.sd", fake), \
                patch("src.devices.check_vbcable_status", return_value=(False, None, None)), \
                patch("src.router.native_get_device_bus_type", side_effect=RuntimeError("no COM in tests")), \
                patch("src.stream.time.sleep"):
            engine = AudioEngine(input_device=2, output_device=3, denoise_enabled=False)
            return engine

    def _start(self, engine, fake):
        with patch("src.stream.sd", fake), \
                patch("src.devices.check_vbcable_status", return_value=(False, None, None)), \
                patch("src.router.native_get_device_bus_type", side_effect=RuntimeError("no COM in tests")), \
                patch("src.stream.time.sleep"):
            engine.start()

    def test_step1_open_at_48k_when_it_works(self):
        fake = FakeSd()
        engine = self._engine(fake)
        self._start(engine, fake)
        self.assertEqual(len(fake.opened), 1)
        self.assertEqual((fake.opened[0]["rate"], fake.opened[0]["block"]), (48000, 480))
        self.assertEqual(fake.opened[0]["callback"].__func__, AudioEngine._audio_callback)
        self.assertTrue(engine.is_active())

    def test_step2_native_rate_with_resampling(self):
        fake = FakeSd(mic_rate=16000, plan=lambda rate, dev: fail(-9996) if rate == 48000 else None)
        engine = self._engine(fake)
        self._start(engine, fake)
        rates = [(o["rate"], o["block"], o["device"]) for o in fake.opened]
        self.assertEqual(rates, [(48000, 480, (2, 3)), (16000, 160, (2, 3))])
        self.assertEqual(fake.opened[1]["callback"].__func__, AudioEngine._audio_callback_resampled)
        self.assertEqual(engine.resampling_native_rate, 16000)
        self.assertEqual(engine.input_device, 2)
        self.assertTrue(engine.is_active())

    def test_step2_blocks_for_common_rates(self):
        for rate, block in ((8000, 80), (16000, 160), (32000, 320), (44100, 441)):
            with self.subTest(rate=rate):
                fake = FakeSd(mic_rate=rate, plan=lambda r, d: fail(-9997) if r == 48000 else None)
                engine = self._engine(fake)
                self._start(engine, fake)
                self.assertEqual((fake.opened[1]["rate"], fake.opened[1]["block"]), (rate, block))

    def test_step3_directsound_after_native_rate_fails(self):
        def plan(rate, device):
            if device == (2, 3):
                return fail(-9999)
            return None
        fake = FakeSd(mic_rate=16000, plan=plan)
        engine = self._engine(fake)
        self._start(engine, fake)
        devices_tried = [(o["rate"], o["device"]) for o in fake.opened]
        self.assertEqual(devices_tried[0], (48000, (2, 3)))
        self.assertEqual(devices_tried[1], (16000, (2, 3)))
        self.assertEqual(devices_tried[2], (48000, (0, 1)))
        self.assertEqual(engine.input_device, 0)
        self.assertEqual(engine.output_device, 1)
        self.assertTrue(engine.is_active())

    def test_native_48k_skips_step2(self):
        def plan(rate, device):
            return fail(-9996) if device == (2, 3) else None
        fake = FakeSd(mic_rate=48000, plan=plan)
        engine = self._engine(fake)
        self._start(engine, fake)
        rates = [(o["rate"], o["block"], o["device"]) for o in fake.opened]
        self.assertEqual(rates, [(48000, 480, (2, 3)), (48000, 480, (0, 1))])

    def test_all_steps_fail_raises_first_error_and_restores_devices(self):
        fake = FakeSd(mic_rate=16000, plan=lambda rate, device: fail(-9996))
        engine = self._engine(fake)
        with self.assertRaises(RuntimeError) as cm:
            self._start(engine, fake)
        self.assertIn("-9996", str(cm.exception))
        self.assertEqual((engine.input_device, engine.output_device), (2, 3))
        self.assertFalse(engine.is_active())
        self.assertEqual(engine.last_start_error.code, -9996)
        self.assertGreaterEqual(len(fake.opened), 3)

    def test_non_ladder_error_is_not_retried(self):
        fake = FakeSd(plan=lambda rate, device: fail(-9998))
        engine = self._engine(fake)
        with self.assertRaises(RuntimeError):
            self._start(engine, fake)
        self.assertEqual(len(fake.opened), 1)

    def test_busy_code_is_retried_inside_a_step(self):
        calls = {"n": 0}

        def plan(rate, device):
            calls["n"] += 1
            return "Error [PaErrorCode -9985]" if calls["n"] == 1 else None
        fake = FakeSd(plan=plan)
        engine = self._engine(fake)
        self._start(engine, fake)
        self.assertEqual(len(fake.opened), 2)
        self.assertEqual((fake.opened[1]["rate"], fake.opened[1]["block"]), (48000, 480))

    def test_ladder_codes_are_the_documented_three(self):
        self.assertEqual(set(LADDER_CODES), {-9996, -9997, -9999})


class ResampledCallbackTests(unittest.TestCase):
    def _engine(self, in_channels=1, out_channels=1):
        fake = FakeSd()
        with patch("src.stream.sd", fake), patch("src.devices.check_vbcable_status", return_value=(False, None, None)):
            engine = AudioEngine(input_device=2, output_device=3, denoise_enabled=False)
        engine.in_channels = in_channels
        engine.out_channels = out_channels
        return engine

    def _tone(self, rate, block, channels, blocks, amp=0.2, freq=1000.0):
        t = np.arange(block * blocks) / float(rate)
        mono = (amp * np.sin(2 * np.pi * freq * t)).astype(np.float32)
        return mono.reshape(blocks, block)

    def test_mono_16k_round_trip_keeps_block_size_and_is_error_free(self):
        engine = self._engine()
        engine._setup_resampling(16000, 160)
        tone = self._tone(16000, 160, 1, 60)
        out_all = []
        for b in range(60):
            indata = tone[b].reshape(160, 1).copy()
            outdata = np.full((160, 1), 9.0, dtype=np.float32)
            engine._audio_callback_resampled(indata, outdata, 160, None, None)
            out_all.append(outdata[:, 0].copy())
        self.assertEqual(engine.callback_error_count, 0, engine.last_callback_error)
        self.assertEqual(engine.resample_length_mismatch, 0)
        self.assertTrue(all(np.all(np.isfinite(o)) for o in out_all))
        self.assertTrue(np.max(np.abs(np.concatenate(out_all[30:]))) > 1e-3)
        self.assertLessEqual(np.max(np.abs(np.concatenate(out_all))), 1.0)

    def test_stereo_input_and_output(self):
        engine = self._engine(in_channels=2, out_channels=2)
        engine._setup_resampling(16000, 160)
        tone = self._tone(16000, 160, 2, 40)
        for b in range(40):
            indata = np.column_stack([tone[b], tone[b]]).astype(np.float32)
            outdata = np.zeros((160, 2), dtype=np.float32)
            engine._audio_callback_resampled(indata, outdata, 160, None, None)
        self.assertEqual(engine.callback_error_count, 0, engine.last_callback_error)
        self.assertEqual(outdata.shape, (160, 2))

    def test_muted_outputs_silence(self):
        engine = self._engine()
        engine._setup_resampling(16000, 160)
        engine.is_muted = True
        outdata = np.full((160, 1), 0.5, dtype=np.float32)
        engine._audio_callback_resampled(np.ones((160, 1), dtype=np.float32), outdata, 160, None, None)
        self.assertTrue(np.all(outdata == 0.0))

    def test_setup_and_close_resampling(self):
        engine = self._engine()
        engine._setup_resampling(44100, 441)
        self.assertEqual(engine._rs_up.in_rate, 44100)
        self.assertEqual(engine._rs_up.out_rate, 48000)
        self.assertEqual(engine._rs_down.in_rate, 48000)
        self.assertEqual(engine._rs_down.out_rate, 44100)
        engine._close_resampling()
        self.assertIsNone(engine._rs_up)
        self.assertIsNone(engine._rs_down)


class ResamplerBlockSizeTests(unittest.TestCase):
    """Real SpeexDSP resamplers, no device: every 10 ms block must map to exactly 480 and back."""

    def test_common_rates_give_whole_blocks(self):
        for rate, block in ((8000, 80), (16000, 160), (24000, 240), (32000, 320), (44100, 441)):
            with self.subTest(rate=rate):
                up = Resampler(rate, 48000, channels=1, max_block_in=block)
                down = Resampler(48000, rate, channels=1, max_block_in=480)
                rng = np.random.default_rng(rate)
                for _ in range(200):
                    x = rng.standard_normal(block).astype(np.float32) * 0.1
                    y = up.process(x)
                    self.assertEqual(y.shape[0], 480)
                    z = down.process(np.ascontiguousarray(y))
                    self.assertEqual(z.shape[0], block)
                up.close()
                down.close()

    def test_round_trip_latency_is_measured_and_bounded(self):
        # Cross-correlation of white noise through native -> 48 kHz -> native. Measured: 80 native samples
        # (5 ms at 16 kHz, 10 ms at 8 kHz, 1.8 ms at 44.1 kHz). The bound guards against a larger filter.
        def lag(x, y, maxlag):
            n = len(x) + len(y)
            nfft = 1 << (n - 1).bit_length()
            c = np.fft.irfft(np.fft.rfft(y, nfft) * np.conj(np.fft.rfft(x, nfft)), nfft)
            return int(np.argmax(c[:maxlag]))

        for rate, block in ((8000, 80), (16000, 160)):
            with self.subTest(rate=rate):
                rng = np.random.default_rng(rate)
                blocks = 200
                x = rng.standard_normal(block * blocks).astype(np.float32) * 0.3
                up = Resampler(rate, 48000, channels=1, max_block_in=block)
                down = Resampler(48000, rate, channels=1, max_block_in=480)
                y48 = np.concatenate([np.array(up.process(x[b * block:(b + 1) * block]), copy=True) for b in range(blocks)])
                z = np.concatenate([np.array(down.process(y48[b * 480:(b + 1) * 480]), copy=True)
                                    for b in range(len(y48) // 480)])[:len(x)]
                self.assertLessEqual(lag(x, z, 400) / rate * 1000.0, 12.0)
                up.close()
                down.close()

    def test_stereo_planar_blocks(self):
        up = Resampler(16000, 48000, channels=2, max_block_in=160)
        for _ in range(50):
            y = up.process(np.random.default_rng(1).standard_normal((2, 160)).astype(np.float32))
            self.assertEqual(y.shape, (2, 480))
        up.close()


class ResolverHelperTests(unittest.TestCase):
    def test_reresolve_keeps_index_when_name_matches(self):
        devices, hostapis = make_env()
        self.assertEqual(reresolve_index(devices, hostapis, JABRA, 2), 2)

    def test_reresolve_finds_shifted_index_on_same_host_api(self):
        devices, hostapis = make_env()
        # Stored index 0 is the DirectSound mic; the name also exists on WASAPI (2). Same API as stored first.
        self.assertEqual(reresolve_index(devices, hostapis, JABRA, 0), 0)
        # Index pointing at a CABLE output now: the input with that name is used instead.
        self.assertEqual(reresolve_index(devices, hostapis, JABRA, 3), 2)

    def test_reresolve_without_name_or_match_keeps_index(self):
        devices, hostapis = make_env()
        self.assertEqual(reresolve_index(devices, hostapis, None, 2), 2)
        self.assertEqual(reresolve_index(devices, hostapis, "Nothing like this", 2), 2)

    def test_reresolve_fuzzy_name_when_stored_index_is_not_an_input(self):
        devices, hostapis = make_env()
        devices[2]["name"] = JABRA + " (2)"
        devices[0]["name"] = "Some other microphone"
        self.assertEqual(reresolve_index(devices, hostapis, JABRA, 3), 2)

    def test_refresh_input_index_helper(self):
        fake = FakeSd()
        with patch("src.stream.sd", fake):
            self.assertEqual(refresh_input_index(2, JABRA), 2)
            self.assertEqual(refresh_input_index(None, JABRA), None)

    def test_resolve_pair_only_api(self):
        devices, hostapis = make_env()
        pair = resolve_pair(devices, hostapis, 2, 3, only_api="DIRECTSOUND")
        self.assertEqual((pair.input, pair.output), (0, 1))
        self.assertIn("DirectSound", pair.host_name)

    def test_resolve_pair_only_api_missing_raises(self):
        devices, hostapis = make_env()
        with self.assertRaises(DeviceResolutionError):
            resolve_pair(devices, hostapis, 2, 3, only_api="MME")


if __name__ == "__main__":
    unittest.main()
