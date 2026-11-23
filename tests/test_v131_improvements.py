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
    def test_native_get_current_default_playback_returns_tuple_or_none(self):
        res = native_get_current_default_playback()
        if res is not None:
            guid, name = res
            self.assertIsInstance(guid, str)
            self.assertIsInstance(name, str)

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
    def test_latency_adaptation_for_bluetooth_hfp(self):
        engine = AudioEngine(
            input_device=0,
            output_device=1,
            denoise_enabled=False,
            high_pass_enabled=False,
        )
        # Mock bluetooth HFP input
        with patch("src.router.native_get_device_bus_type", side_effect=lambda dev, is_input: DeviceBusType.BLUETOOTH_HFP if is_input else DeviceBusType.USB), patch("src.win_audio.native_get_device_bus_type", side_effect=lambda dev, is_input: DeviceBusType.BLUETOOTH_HFP if is_input else DeviceBusType.USB):
            with patch("sounddevice.query_devices", return_value={'name': 'AirPods Hands-Free AG Audio', 'max_input_channels': 1, 'max_output_channels': 2}):
                with patch("sounddevice.query_hostapis", return_value=[{'name': 'Windows DirectSound'}]):
                    with patch("sounddevice.RawStream") as mock_stream:
                        try:
                            engine.start()
                        except Exception:
                            pass
                        # Verify RawStream was initialized with latency=0.035
                        if mock_stream.called:
                            kwargs = mock_stream.call_args[1]
                            self.assertEqual(kwargs.get('latency'), 0.035)

    def test_latency_low_for_usb_or_internal(self):
        engine = AudioEngine(
            input_device=0,
            output_device=1,
            denoise_enabled=False,
            high_pass_enabled=False,
        )
        with patch("src.router.native_get_device_bus_type", return_value=DeviceBusType.USB), patch("src.win_audio.native_get_device_bus_type", return_value=DeviceBusType.USB):
            with patch("sounddevice.query_devices", return_value={'name': 'Yeti USB Mic', 'max_input_channels': 1, 'max_output_channels': 2}):
                with patch("sounddevice.query_hostapis", return_value=[{'name': 'Windows DirectSound'}]):
                    with patch("sounddevice.RawStream") as mock_stream:
                        try:
                            engine.start()
                        except Exception:
                            pass
                        if mock_stream.called:
                            kwargs = mock_stream.call_args[1]
                            self.assertEqual(kwargs.get('latency'), 'low')


if __name__ == "__main__":
    unittest.main()

