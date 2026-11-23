"""Tray icon follows the taskbar theme: white glyph on a dark taskbar, near-black on a light one."""
import ctypes
import unittest
from unittest.mock import patch

import _isolation  # noqa: F401

from src import gui_tray, gui_win32, win_theme


def _glyph_pixel(img):
    """Colour in the middle of the tallest bar (grid x 7-9, y 2-14) of the 64 px icon."""
    return img.getpixel((32, 24))


def _luma(rgba):
    r, g, b = rgba[:3]
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


class TrayThemeTests(unittest.TestCase):
    def test_dark_taskbar_gets_a_light_glyph(self):
        self.assertGreater(_luma(_glyph_pixel(gui_tray.create_tray_icon_image(False, light_taskbar=False))), 200)

    def test_light_taskbar_gets_a_dark_glyph(self):
        self.assertLess(_luma(_glyph_pixel(gui_tray.create_tray_icon_image(False, light_taskbar=True))), 60)

    def test_glyph_contrasts_with_the_taskbar_it_is_drawn_for(self):
        # Windows 11 taskbar backgrounds: about (32, 32, 32) dark and (243, 243, 243) light.
        for light, bg in ((False, (32, 32, 32)), (True, (243, 243, 243))):
            glyph = _glyph_pixel(gui_tray.create_tray_icon_image(True, light_taskbar=light))
            self.assertGreater(abs(_luma(glyph) - _luma(bg)), 150, light)

    def test_default_reads_the_windows_setting(self):
        with patch.object(win_theme, "taskbar_uses_light_theme", return_value=True) as read:
            img = gui_tray.create_tray_icon_image(False)
        read.assert_called_once()
        self.assertLess(_luma(_glyph_pixel(img)), 60)

    def test_registry_value_maps_to_light_or_dark(self):
        import winreg

        class _Key:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        for value, expected in ((1, True), (0, False)):
            with patch.object(winreg, "OpenKey", return_value=_Key()), \
                    patch.object(winreg, "QueryValueEx", return_value=(value, winreg.REG_DWORD)):
                self.assertEqual(win_theme.taskbar_uses_light_theme(), expected)
        with patch.object(winreg, "OpenKey", side_effect=OSError("missing")):
            self.assertFalse(win_theme.taskbar_uses_light_theme())

    def test_theme_change_message_is_recognised(self):
        good = ctypes.create_unicode_buffer("ImmersiveColorSet")
        other = ctypes.create_unicode_buffer("Environment")
        self.assertTrue(gui_win32._is_theme_change(ctypes.addressof(good)))
        self.assertFalse(gui_win32._is_theme_change(ctypes.addressof(other)))
        self.assertFalse(gui_win32._is_theme_change(0))


if __name__ == "__main__":
    unittest.main()
