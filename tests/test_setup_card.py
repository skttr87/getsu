"""
State rules of the first-run card (replaces the old VB-Cable modal) and the device glyph mapping.
The GUI object is created without __init__ (no DearPyGui context, no audio), so only the pure rules run here.
"""
import unittest

from src import gui as g
from src.devices import DeviceKind


def _gui(installed: bool, state=None, dismissed=False):
    obj = g.GetsuGUI.__new__(g.GetsuGUI)
    obj.is_vbcable_installed = installed
    obj._install_state = state
    obj._setup_dismissed = dismissed
    return obj


class FirstRunCardRuleTests(unittest.TestCase):

    def test_offer_shows_when_cable_missing(self):
        self.assertTrue(_gui(installed=False)._card_visible())

    def test_not_now_hides_card_for_the_session(self):
        self.assertFalse(_gui(installed=False, dismissed=True)._card_visible())

    def test_installed_cable_hides_card_and_shows_hero(self):
        self.assertFalse(_gui(installed=True)._card_visible())

    def test_installing_keeps_card_even_after_cable_appears(self):
        self.assertTrue(_gui(installed=True, state="installing")._card_visible())

    def test_failed_install_shows_card_even_when_dismissed(self):
        self.assertTrue(_gui(installed=False, state="failed", dismissed=True)._card_visible())


class DeviceGlyphTests(unittest.TestCase):

    def test_every_kind_has_a_glyph(self):
        for kind in (DeviceKind.BLUETOOTH_HEADSET, DeviceKind.USB_HEADSET, DeviceKind.WIRED_HEADSET,
                     DeviceKind.USB_MIC, DeviceKind.BUILT_IN, DeviceKind.LINE_IN, DeviceKind.OTHER):
            self.assertIn(kind, g._GLYPH_FOR_KIND)

    def test_usb_microphone_uses_the_mic_icon_and_wired_headset_the_headset_icon(self):
        self.assertEqual(g._GLYPH_FOR_KIND[DeviceKind.USB_MIC], "mic")
        self.assertEqual(g._GLYPH_FOR_KIND[DeviceKind.WIRED_HEADSET], "headset")
        self.assertEqual(g._GLYPH_FOR_KIND[DeviceKind.USB_HEADSET], "headset")
        self.assertEqual(g._GLYPH_FOR_KIND[DeviceKind.BUILT_IN], "builtin")


if __name__ == "__main__":
    unittest.main()
