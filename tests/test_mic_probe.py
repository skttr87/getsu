import io
import sys
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

import numpy as np

import src.mic_probe as probe

MIC_NAME = "Микрофон гарнитуры (Jabra EVOLVE 20 MS)"


def _env():
    hostapis = [
        {"name": "MME", "devices": [0, 1], "default_input_device": 0, "default_output_device": 1},
        {"name": "Windows DirectSound", "devices": [2, 3], "default_input_device": 2, "default_output_device": 3},
        {"name": "Windows WASAPI", "devices": [4, 5], "default_input_device": 4, "default_output_device": 5},
        {"name": "Windows WDM-KS", "devices": [6], "default_input_device": 6, "default_output_device": -1},
    ]
    devices = [
        {"name": MIC_NAME[:31], "hostapi": 0, "max_input_channels": 1, "max_output_channels": 0, "default_samplerate": 44100.0},
        {"name": "CABLE Input (VB-Audio Virtual Cable)", "hostapi": 0, "max_input_channels": 0, "max_output_channels": 8, "default_samplerate": 44100.0},
        {"name": MIC_NAME, "hostapi": 1, "max_input_channels": 1, "max_output_channels": 0, "default_samplerate": 44100.0},
        {"name": "CABLE Input (VB-Audio Virtual Cable)", "hostapi": 1, "max_input_channels": 0, "max_output_channels": 2, "default_samplerate": 44100.0},
        {"name": MIC_NAME, "hostapi": 2, "max_input_channels": 1, "max_output_channels": 0, "default_samplerate": 16000.0},
        {"name": "CABLE Input (VB-Audio Virtual Cable)", "hostapi": 2, "max_input_channels": 0, "max_output_channels": 2, "default_samplerate": 48000.0},
        {"name": "Mic in at front panel (black)", "hostapi": 3, "max_input_channels": 1, "max_output_channels": 0, "default_samplerate": 48000.0},
    ]
    return devices, hostapis


class FakeSd:
    """Minimal sounddevice stand-in. `fail` maps (api_name, rate) to an error message for opens."""

    __version__ = "0.5.6"

    def __init__(self, devices, hostapis, fail=None):
        self.devices = devices
        self.hostapis = hostapis
        self.fail = fail or {}
        self.opened = []

    def query_devices(self, index=None):
        return self.devices if index is None else self.devices[index]

    def query_hostapis(self, index=None):
        return self.hostapis if index is None else self.hostapis[index]

    def get_portaudio_version(self):
        return (1246976, "PortAudio V19-fake")

    def WasapiSettings(self, auto_convert=False):
        return ("wasapi", auto_convert)

    def _check(self, device, rate):
        api = self.hostapis[self.devices[device]["hostapi"]]["name"]
        self.opened.append((api, device, rate))
        for (fail_api, fail_rate), message in self.fail.items():
            if fail_api in api and (fail_rate is None or fail_rate == rate):
                raise RuntimeError(message)

    def InputStream(self, device, samplerate, channels, dtype, extra_settings=None):
        self._check(device, samplerate)
        return _FakeStream(channels, samplerate)

    def Stream(self, device, samplerate, blocksize, channels, dtype, latency, extra_settings=None):
        self._check(device[0], samplerate)
        return _FakeStream(channels[0], samplerate, out_channels=channels[1])


class _FakeStream:
    def __init__(self, channels, rate, out_channels=None):
        self.channels = channels
        self.rate = rate
        self.out_channels = out_channels

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self, frames):
        return np.full((frames, self.channels), 0.5, dtype=np.float32), False

    def write(self, data):
        return None


class TestPeak(unittest.TestCase):
    def test_silence_and_full_scale(self):
        self.assertEqual(probe.peak_dbfs(np.zeros(10, dtype=np.float32)), probe.SILENT_DB)
        self.assertAlmostEqual(probe.peak_dbfs(np.array([0.0, -1.0], dtype=np.float32)), 0.0, places=6)
        self.assertAlmostEqual(probe.peak_dbfs(np.full(4, 0.5, dtype=np.float32)), -6.0206, places=3)


class TestTargetResolution(unittest.TestCase):
    def setUp(self):
        self.devices, self.hostapis = _env()

    def test_index(self):
        self.assertEqual(probe.find_input_index(self.devices, self.hostapis, "4"), 4)

    def test_exact_name_prefers_wasapi(self):
        idx = probe.find_input_index(self.devices, self.hostapis, MIC_NAME)
        self.assertEqual(idx, 4)

    def test_cyrillic_similar_name(self):
        idx = probe.find_input_index(self.devices, self.hostapis, "Микрофон гарнитуры (Jabra EVOLVE 20 MS)")
        self.assertEqual(idx, 4)

    def test_output_index_rejected(self):
        with self.assertRaises(ValueError):
            probe.find_input_index(self.devices, self.hostapis, "1")

    def test_unknown_name_rejected(self):
        with self.assertRaises(ValueError):
            probe.find_input_index(self.devices, self.hostapis, "Nonexistent Turntable")


class TestRunProbe(unittest.TestCase):
    def setUp(self):
        self.devices, self.hostapis = _env()

    def _run(self, fake, target, duplex):
        lines = []
        with patch.dict(sys.modules, {"sounddevice": fake}):
            code = probe.run_probe(target, out=lines.append, duplex=duplex)
        return code, "\n".join(lines)

    def test_all_attempts_ok_and_table_printed(self):
        fake = FakeSd(self.devices, self.hostapis)
        code, text = self._run(fake, MIC_NAME, duplex=False)
        self.assertEqual(code, 0)
        self.assertIn("[1/4] WASAPI shared, auto_convert, 48000 Hz", text)
        self.assertIn("[4/4] MME, 48000 Hz", text)
        self.assertIn("4 of 4 attempts opened the microphone.", text)
        # WASAPI at the endpoint default rate (16 kHz here), with auto_convert off
        self.assertIn(("Windows WASAPI", 4, 16000), fake.opened)
        self.assertIn("peak dBFS", text)

    def test_error_code_is_reported_per_attempt(self):
        fake = FakeSd(self.devices, self.hostapis, fail={
            ("WASAPI", 48000): "Error opening InputStream: Invalid device [PaErrorCode -9996]",
            ("WASAPI", 16000): "Error opening InputStream: Invalid sample rate [PaErrorCode -9997]",
            ("DirectSound", None): "Error opening InputStream: Unanticipated host error [PaErrorCode -9999]",
        })
        code, text = self._run(fake, MIC_NAME, duplex=False)
        self.assertEqual(code, 0)   # MME still opens
        self.assertIn("PaErrorCode -9996", text)
        self.assertIn("PaErrorCode -9997", text)
        self.assertIn("PaErrorCode -9999", text)
        self.assertIn("1 of 4 attempts opened the microphone.", text)
        self.assertIn("MME, 48000 Hz", text)

    def test_all_fail_returns_one(self):
        fail_all = {(api, None): "Error opening InputStream: Device unavailable [PaErrorCode -9985]"
                    for api in ("WASAPI", "DirectSound", "MME")}
        fake = FakeSd(self.devices, self.hostapis, fail=fail_all)
        code, text = self._run(fake, MIC_NAME, duplex=False)
        self.assertEqual(code, 1)
        self.assertIn("0 of 4 attempts opened the microphone.", text)

    def test_duplex_step_uses_resolve_pair_without_router(self):
        fake = FakeSd(self.devices, self.hostapis)
        with patch("src.router.SmartMicRouter", side_effect=AssertionError("router must not be used")):
            code, text = self._run(fake, MIC_NAME, duplex=True)
        self.assertEqual(code, 0)
        self.assertIn("[5/5] duplex mic + CABLE Input, 48000 Hz", text)
        self.assertIn("5 of 5 attempts opened the microphone.", text)

    def test_duplex_skipped_without_cable(self):
        with patch.object(probe, "_find_cable_input", return_value=None):
            code, text = self._run(FakeSd(self.devices, self.hostapis), MIC_NAME, duplex=True)
        self.assertIn("CABLE Input not found", text)

    def test_bad_target_returns_two(self):
        fake = FakeSd(self.devices, self.hostapis)
        code, text = self._run(fake, "Nonexistent Turntable", duplex=False)
        self.assertEqual(code, 2)


if __name__ == "__main__":
    unittest.main()
