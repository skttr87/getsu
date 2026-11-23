"""Endpoint listing helpers: bus enumerator mapping, classification from known bus and form factor."""
import unittest
from unittest.mock import patch

from src import win_audio
from src.devices import DeviceKind, kind_for_endpoint, is_virtual_endpoint
from src.win_audio import DeviceBusType, EndpointFormFactor, _bus_from_enumerator, native_list_active_endpoints


class TestBusFromEnumerator(unittest.TestCase):
    def test_known_enumerators_map_to_bus_types(self):
        cases = {
            "BTHHFENUM\\{0000}": DeviceBusType.BLUETOOTH_HFP,
            "BTHENUM\\DEV_1": DeviceBusType.BLUETOOTH_A2DP,
            "BTHLEENUM\\DEV_2": DeviceBusType.BLUETOOTH_LE,
            "USB\\VID_1234&PID_5678": DeviceBusType.USB,
            "HDAUDIO\\FUNC_01": DeviceBusType.INTERNAL_HD,
            "INTELAUDIO\\FUNC_01": DeviceBusType.INTEL_AUDIO,
            "SWD\\MMDEVAPI\\{0.0.0}": DeviceBusType.VIRTUAL,
            "ROOT\\MEDIA\\0000": DeviceBusType.ROOT,
        }
        for enumerator, expected in cases.items():
            self.assertEqual(_bus_from_enumerator(enumerator), expected, enumerator)

    def test_unknown_enumerator_is_returned_upper_cased(self):
        self.assertEqual(_bus_from_enumerator("  pci\\ven_8086 "), "PCI\\VEN_8086")


class TestKindForEndpoint(unittest.TestCase):
    def test_built_in_speakers_and_jack_headphones(self):
        self.assertEqual(kind_for_endpoint("Speakers (Realtek(R) Audio)", "HDAUDIO", EndpointFormFactor.SPEAKERS),
                         DeviceKind.BUILT_IN)
        self.assertEqual(kind_for_endpoint("Headphones (Realtek(R) Audio)", "HDAUDIO", EndpointFormFactor.HEADPHONES),
                         DeviceKind.WIRED_HEADSET)
        self.assertEqual(kind_for_endpoint("Line (Realtek(R) Audio)", "HDAUDIO", EndpointFormFactor.LINE_LEVEL),
                         DeviceKind.LINE_IN)

    def test_virtual_cable_is_virtual_on_both_sides(self):
        self.assertEqual(kind_for_endpoint("CABLE Input (VB-Audio Virtual Cable)", "SWD", None), DeviceKind.VIRTUAL)
        self.assertEqual(kind_for_endpoint("CABLE Output (VB-Audio Virtual Cable)", "SWD", None), DeviceKind.VIRTUAL)

    def test_usb_and_bluetooth(self):
        self.assertEqual(kind_for_endpoint("Headset", "USB", EndpointFormFactor.HEADSET), DeviceKind.USB_HEADSET)
        self.assertEqual(kind_for_endpoint("Mic", "USB", EndpointFormFactor.MICROPHONE), DeviceKind.USB_MIC)
        self.assertEqual(kind_for_endpoint("Earbuds", "BTHENUM", None), DeviceKind.BLUETOOTH_HEADSET)

    def test_empty_name_and_unknown_bus(self):
        self.assertEqual(kind_for_endpoint("", "HDAUDIO", None), DeviceKind.OTHER)
        self.assertEqual(kind_for_endpoint("Something", "UNKNOWN", None), DeviceKind.OTHER)

    def test_virtual_detection_from_name_and_bus(self):
        self.assertTrue(is_virtual_endpoint("CABLE Input (VB-Audio Virtual Cable)", "UNKNOWN"))
        self.assertTrue(is_virtual_endpoint("Anything", DeviceBusType.ROOT))
        self.assertFalse(is_virtual_endpoint("Speakers (Realtek(R) Audio)", "HDAUDIO"))
        self.assertFalse(is_virtual_endpoint("", "HDAUDIO"))


class TestNativeListActiveEndpoints(unittest.TestCase):
    def test_returns_empty_without_com(self):
        with patch.object(win_audio, "ole32", None):
            self.assertEqual(native_list_active_endpoints(is_input=True), [])
            self.assertEqual(native_list_active_endpoints(is_input=False), [])


if __name__ == "__main__":
    unittest.main()
