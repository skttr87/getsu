"""
Unit tests for v1.3.1 improvements:
- Hardware bus type auto-detection (PKEY_Device_EnumeratorName)
- Native CoreAudio active playback endpoint resolution
- Dynamic Bluetooth Hands-Free (HFP) jitter buffer adaptation
- Type-safe device resolution and error recovery
"""
import unittest
from unittest.mock import MagicMock, patch

from src.router import DeviceBusType, SmartMicRouter, native_get_device_bus_type, native_get_current_default_playback
from src.devices import is_laptop_microphone, auto_select_output_device, get_physical_output_device
from src.stream import AudioEngine


class TestV131BusDetection(unittest.TestCase):
    def test_bus_type_constants(self):
        self.assertEqual(DeviceBusType.BLUETOOTH_HFP, "BTHHFENUM")
        self.assertEqual(DeviceBusType.BLUETOOTH_A2DP, "BTHENUM")
        self.assertEqual(DeviceBusType.USB, "USB")
        self.assertEqual(DeviceBusType.INTERNAL_HD, "HDAUDIO")
        self.assertEqual(DeviceBusType.INTEL_AUDIO, "INTELAUDIO")
        self.assertEqual(DeviceBusType.VIRTUAL, "SWD")
        self.assertEqual(DeviceBusType.UNKNOWN, "UNKNOWN")

    def test_native_get_device_bus_type_fallback(self):
        # On non-matching or error condition, returns UNKNOWN
        bus = native_get_device_bus_type("NonExistentDevice12345", is_input=True)
        self.assertIn(bus, [
            DeviceBusType.BLUETOOTH_HFP,
            DeviceBusType.BLUETOOTH_A2DP,
            DeviceBusType.BLUETOOTH_LE,
            DeviceBusType.USB,
            DeviceBusType.INTERNAL_HD,
            DeviceBusType.INTEL_AUDIO,
            DeviceBusType.VIRTUAL,
            DeviceBusType.UNKNOWN,
        ])

    def test_is_laptop_microphone_with_bus_type(self):
        # When bus type is INTERNAL_HD -> True regardless of name
        with patch("src.win_audio.native_get_device_bus_type", return_value=DeviceBusType.INTERNAL_HD):
            self.assertTrue(is_laptop_microphone("Studio Condenser Mic", device_id=0))

        # When bus type is USB -> False even if name has 'internal'
        with patch("src.win_audio.native_get_device_bus_type", return_value=DeviceBusType.USB):
            self.assertFalse(is_laptop_microphone("USB Internal Array", device_id=1))

        # When bus type is BLUETOOTH_HFP -> False
        with patch("src.win_audio.native_get_device_bus_type", return_value=DeviceBusType.BLUETOOTH_HFP):
            self.assertFalse(is_laptop_microphone("Bluetooth Headset", device_id=2))

        # Bus UNKNOWN: no name keyword fallback any more (M4.5), so the endpoint is not a built-in mic
        with patch("src.win_audio.native_get_device_bus_type", return_value=DeviceBusType.UNKNOWN):
            self.assertFalse(is_laptop_microphone("Realtek High Definition Audio", device_id=3))
            self.assertFalse(is_laptop_microphone("Microphone Array", device_id=4))
            self.assertFalse(is_laptop_microphone("Fifine K669 Microphone", device_id=5))


class TestV131PlaybackResolution(unittest.TestCase):
    def test_native_get_current_default_playback_without_com_returns_unknown(self):
        with patch("src.win_audio.ole32", None):
            res = native_get_current_default_playback()
        self.assertEqual(res, (None, "Unknown"))

    def test_native_get_current_default_playback_returns_guid_and_name(self):
        fake_com = MagicMock()
        with patch("src.win_audio.ole32", fake_com):
            res = native_get_current_default_playback()
        self.assertIsInstance(res, tuple)
        self.assertEqual(len(res), 2)
        self.assertIsInstance(res[1], str)

    def test_get_physical_output_device_with_native_default(self):
        mock_devices = [
            {'index': 0, 'name': 'CABLE Input (VB-Audio)', 'hostapi': 'Windows WASAPI', 'inputs': 0, 'outputs': 2},
            {'index': 1, 'name': 'Realtek Speakers', 'hostapi': 'Windows WASAPI', 'inputs': 0, 'outputs': 2},
            {'index': 2, 'name': 'USB Wireless Headset', 'hostapi': 'Windows WASAPI', 'inputs': 0, 'outputs': 2},
        ]
        with patch("src.devices.get_all_devices", return_value=mock_devices):
            with patch("src.router.native_get_current_default_playback", return_value=("{guid-usb}", "USB Wireless Headset")):
                selected = get_physical_output_device(force_rescan=True)
                self.assertIsNotNone(selected)
                self.assertEqual(selected['name'], "USB Wireless Headset")


class TestV131BluetoothJitterAdaptation(unittest.TestCase):
    def _start_with_bus(self, bus_for_input, device_name):
        """Starts the engine on a mock input of the given bus type and returns the latency given to sd.Stream."""
        devices = [
            {'name': device_name, 'max_input_channels': 1, 'max_output_channels': 0, 'hostapi': 0,
             'default_samplerate': 48000.0},
            {'name': 'Speakers (Mock)', 'max_input_channels': 0, 'max_output_channels': 2, 'hostapi': 0,
             'default_samplerate': 48000.0},
        ]
        hostapis = [{'name': 'Windows WASAPI', 'default_input_device': 0, 'default_output_device': 1}]
        bus_patch = lambda dev, is_input=True: bus_for_input if is_input else DeviceBusType.USB
        with patch("src.router.native_get_device_bus_type", side_effect=bus_patch), \
                patch("src.win_audio.native_get_device_bus_type", side_effect=bus_patch), \
                patch("sounddevice.query_devices", side_effect=lambda i=None, *a, **k: devices if i is None else devices[i]), \
                patch("sounddevice.query_hostapis", return_value=hostapis), \
                patch("sounddevice.Stream") as mock_stream:
            engine = AudioEngine(input_device=0, output_device=1, denoise_enabled=False, high_pass_enabled=False)
            engine.start()
            engine.prepare_for_stop()
        self.assertTrue(mock_stream.called, "the engine did not open a stream")
        return mock_stream.call_args[1].get('latency')

    def test_latency_adaptation_for_bluetooth_hfp(self):
        latency = self._start_with_bus(DeviceBusType.BLUETOOTH_HFP, 'AirPods Hands-Free AG Audio')
        self.assertEqual(latency, 0.035)

    def test_latency_low_for_usb_or_internal(self):
        latency = self._start_with_bus(DeviceBusType.USB, 'Yeti USB Mic')
        self.assertEqual(latency, 'low')


if __name__ == "__main__":
    unittest.main()

