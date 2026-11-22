import unittest

from src.device_labels import (
    base_subtitle,
    split_name,
    subtitle,
)
from src.devices import DeviceKind


class SplitNameTests(unittest.TestCase):
    def test_realtek_microphone(self):
        self.assertEqual(split_name("Microphone (Realtek(R) Audio)")["title"], "Realtek(R) Audio")

    def test_line_in_twin_keeps_its_own_prefix(self):
        parts = split_name("Line In (Realtek(R) Audio)")
        self.assertEqual(parts["title"], "Realtek(R) Audio")
        self.assertEqual(parts["prefix"], "Line In")

    def test_bluetooth_headset(self):
        self.assertEqual(split_name("Headset (HUAWEI FreeBuds SE 4 ANC)")["title"], "HUAWEI FreeBuds SE 4 ANC")

    def test_cyrillic_name(self):
        self.assertEqual(split_name("Микрофон гарнитуры (Jabra EVOLVE 20 MS)")["title"], "Jabra EVOLVE 20 MS")

    def test_name_without_parentheses_is_the_title(self):
        self.assertEqual(split_name("HUAWEI FreeBuds SE 4 ANC"), {"title": "HUAWEI FreeBuds SE 4 ANC", "prefix": ""})


class SubtitleTests(unittest.TestCase):
    def test_plain_subtitle_is_the_kind_label(self):
        names = {"Headset (HUAWEI FreeBuds SE 4 ANC)": DeviceKind.BLUETOOTH_HEADSET}
        self.assertEqual(subtitle("Headset (HUAWEI FreeBuds SE 4 ANC)", DeviceKind.BLUETOOTH_HEADSET, names),
                         "Bluetooth headset")

    def test_unknown_kind_uses_the_endpoint_word(self):
        names = {"Line In (Realtek(R) Audio)": DeviceKind.OTHER}
        self.assertEqual(subtitle("Line In (Realtek(R) Audio)", DeviceKind.OTHER, names), "Line In")

    def test_twin_entries_get_the_endpoint_word(self):
        names = {
            "Microphone (Realtek(R) Audio)": DeviceKind.BUILT_IN,
            "Line In (Realtek(R) Audio)": DeviceKind.BUILT_IN,
        }
        self.assertEqual(subtitle("Microphone (Realtek(R) Audio)", DeviceKind.BUILT_IN, names),
                         "Built-in microphone, Microphone")
        self.assertEqual(subtitle("Line In (Realtek(R) Audio)", DeviceKind.BUILT_IN, names),
                         "Built-in microphone, Line In")

    def test_different_kinds_with_same_title_need_no_endpoint_word(self):
        names = {
            "Microphone (Realtek(R) Audio)": DeviceKind.BUILT_IN,
            "Line In (Realtek(R) Audio)": DeviceKind.LINE_IN,
        }
        self.assertEqual(subtitle("Line In (Realtek(R) Audio)", DeviceKind.LINE_IN, names), "Line in")

    def test_base_subtitle_for_usb_headset(self):
        self.assertEqual(base_subtitle("Microphone (MPOW Wireless Gaming Headset)", DeviceKind.USB_HEADSET),
                         "USB headset")

    def test_wired_headset_label(self):
        self.assertEqual(base_subtitle("Headset (Realtek(R) Audio)", DeviceKind.WIRED_HEADSET), "Wired headset")


if __name__ == "__main__":
    unittest.main()
