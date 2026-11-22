import unittest

from src.device_labels import (
    KIND_BLUETOOTH,
    KIND_BUILTIN,
    KIND_OTHER,
    KIND_USB,
    base_subtitle,
    kind_from_bus_type,
    split_name,
    subtitle,
)


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


class KindTests(unittest.TestCase):
    def test_bus_types_map_to_kinds(self):
        self.assertEqual(kind_from_bus_type("BTHHFENUM"), KIND_BLUETOOTH)
        self.assertEqual(kind_from_bus_type("USB"), KIND_USB)
        self.assertEqual(kind_from_bus_type("HDAUDIO"), KIND_BUILTIN)
        self.assertEqual(kind_from_bus_type("INTELAUDIO"), KIND_BUILTIN)

    def test_unknown_bus_types_are_other(self):
        for bus in ("BTHENUM", "SWD", "UNKNOWN", None):
            self.assertEqual(kind_from_bus_type(bus), KIND_OTHER)


class SubtitleTests(unittest.TestCase):
    def test_plain_subtitle_is_the_kind_label(self):
        names = {"Headset (HUAWEI FreeBuds SE 4 ANC)": KIND_BLUETOOTH}
        self.assertEqual(subtitle("Headset (HUAWEI FreeBuds SE 4 ANC)", KIND_BLUETOOTH, names), "Bluetooth headset")

    def test_unknown_kind_uses_the_endpoint_word(self):
        names = {"Line In (Realtek(R) Audio)": KIND_OTHER}
        self.assertEqual(subtitle("Line In (Realtek(R) Audio)", KIND_OTHER, names), "Line In")

    def test_twin_entries_get_the_endpoint_word(self):
        names = {
            "Microphone (Realtek(R) Audio)": KIND_BUILTIN,
            "Line In (Realtek(R) Audio)": KIND_BUILTIN,
        }
        self.assertEqual(subtitle("Microphone (Realtek(R) Audio)", KIND_BUILTIN, names),
                         "Built-in microphone, Microphone")
        self.assertEqual(subtitle("Line In (Realtek(R) Audio)", KIND_BUILTIN, names),
                         "Built-in microphone, Line In")

    def test_base_subtitle_for_usb(self):
        self.assertEqual(base_subtitle("Microphone (MPOW Wireless Gaming Headset)", KIND_USB), "USB headset")


if __name__ == "__main__":
    unittest.main()
