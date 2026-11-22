import ast
import math
import os
import struct
import sys
import unittest
from unittest.mock import MagicMock, patch

from PIL import Image

from src import gui


class MusicModeToggleTests(unittest.TestCase):
    """on_music_mode_toggled recomputes the live gate decay with math.exp."""

    def _gui_with_engine(self):
        fake = MagicMock()
        fake.engine = MagicMock()
        fake.engine.gate = MagicMock()
        fake.config = {"denoise_enabled": True, "vad_hangover_ms": 320.0, "vad_decay_ms": 80.0}
        return fake

    def test_math_is_imported_by_gui_module(self):
        self.assertIs(gui.math, math)

    def test_enabling_music_mode_sets_long_decay(self):
        fake = self._gui_with_engine()
        with patch("src.gui.save_config"):
            gui.GetsuGUI.on_music_mode_toggled(fake, "chk_music_mode", True)
        self.assertAlmostEqual(fake.engine.gate.decay_rate, math.exp(-10.0 / 250.0))
        self.assertEqual(fake.engine.gate.hangover_frames, 80)
        self.assertFalse(fake.engine.denoise_enabled)

    def test_disabling_music_mode_restores_configured_decay(self):
        fake = self._gui_with_engine()
        with patch("src.gui.save_config"):
            gui.GetsuGUI.on_music_mode_toggled(fake, "chk_music_mode", False)
        self.assertAlmostEqual(fake.engine.gate.decay_rate, math.exp(-10.0 / 80.0))
        self.assertEqual(fake.engine.gate.hangover_frames, 32)
        self.assertTrue(fake.engine.denoise_enabled)


def _colorref_to_rgb(value):
    return (value & 0xFF, (value >> 8) & 0xFF, (value >> 16) & 0xFF)


class TitleBarTests(unittest.TestCase):
    def test_title_bar_colours_match_mockup(self):
        self.assertEqual(_colorref_to_rgb(gui.TITLE_CAPTION_COLOR), (28, 28, 30))
        self.assertEqual(_colorref_to_rgb(gui.TITLE_TEXT_COLOR), (161, 161, 166))
        self.assertEqual(_colorref_to_rgb(gui.TITLE_BORDER_COLOR), (44, 44, 46))

    @unittest.skipUnless(sys.platform == "win32", "DWM is Windows only")
    def test_apply_sends_mockup_colours_to_dwm(self):
        with patch("src.gui.ctypes.windll") as windll:
            gui.apply_dwm_title_bar(1234)
        sent = {}
        for call in windll.dwmapi.DwmSetWindowAttribute.call_args_list:
            attr = call.args[1]
            sent[attr] = call.args[2]._obj.value
        self.assertEqual(_colorref_to_rgb(sent[35]), (28, 28, 30))
        self.assertEqual(_colorref_to_rgb(sent[36]), (161, 161, 166))
        self.assertEqual(_colorref_to_rgb(sent[34]), (44, 44, 46))


class TrayIconTests(unittest.TestCase):
    def test_tray_icon_is_64_px(self):
        self.assertEqual(gui.create_tray_icon_image(True).size, (64, 64))

    def test_status_dot_is_blue_when_on(self):
        self.assertEqual(gui.create_tray_icon_image(True).getpixel((50, 50))[:3], (10, 132, 255))

    def test_status_dot_is_grey_when_off(self):
        self.assertEqual(gui.create_tray_icon_image(False).getpixel((50, 50))[:3], (142, 142, 147))

    def test_glyph_bars_are_white(self):
        # Centre bar of the five-bar glyph (grid x 7..9, y 2..14).
        self.assertEqual(gui.create_tray_icon_image(True).getpixel((32, 32))[:3], (255, 255, 255))


class AppIconFileTests(unittest.TestCase):
    def test_ico_contains_all_sizes(self):
        with open(gui.ICON_ICO_PATH, "rb") as fh:
            data = fh.read()
        reserved, kind, count = struct.unpack("<HHH", data[:6])
        self.assertEqual((reserved, kind), (0, 1))
        sizes = []
        for i in range(count):
            width, height = struct.unpack("<BB", data[6 + 16 * i:8 + 16 * i])
            sizes.append(width or 256)
        self.assertEqual(sizes, [16, 20, 24, 32, 40, 48, 64, 128, 256])

    def test_png_is_256_px(self):
        with Image.open(gui.ICON_PNG_PATH) as img:
            self.assertEqual(img.size, (256, 256))

    def test_runtime_does_not_regenerate_icon_from_png(self):
        with patch("src.gui.Image") as image_module:
            gui.ensure_app_icon_ico()
        image_module.open.assert_not_called()
        image_module.open.return_value.save.assert_not_called()


GUI_SOURCE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src", "gui.py")

# Emoji, pictographs, arrows, geometric shapes, misc symbols, and the bullet character.
FORBIDDEN_CODEPOINT_RANGES = (
    (0x2190, 0x2BFF),
    (0x1F000, 0x1FAFF),
    (0x2022, 0x2022),
)


class NoPictographTests(unittest.TestCase):
    def test_gui_string_literals_have_no_pictographic_symbols(self):
        with open(GUI_SOURCE, encoding="utf-8") as fh:
            tree = ast.parse(fh.read())
        hits = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                for ch in node.value:
                    cp = ord(ch)
                    if any(lo <= cp <= hi for lo, hi in FORBIDDEN_CODEPOINT_RANGES):
                        hits.append(f"line {node.lineno}: U+{cp:04X} in {node.value[:40]!r}")
        self.assertEqual(hits, [])


class FeatureFlagTests(unittest.TestCase):
    """Controls whose backend is not built yet stay hidden in phase B."""

    def test_modes_hidden_by_default(self):
        self.assertIs(gui.FEATURE_MODES, False)

    def test_earbud_hint_hidden_by_default(self):
        self.assertIs(gui.FEATURE_EARBUD_HINT, False)

    def test_learn_my_room_hidden_by_default(self):
        self.assertIs(gui.FEATURE_LEARN_ROOM, False)

    def test_mode_names_match_the_mockup(self):
        self.assertEqual(gui.MODE_NAMES, ("Game", "Meeting", "Session", "Music"))


class PaletteTests(unittest.TestCase):
    """Colours follow the approved mockup: one blue accent, red for errors, orange for warnings, no green."""

    def test_accent_is_apple_system_blue(self):
        self.assertEqual(gui.COL_ACCENT, (10, 132, 255))

    def test_error_and_warning_colours(self):
        self.assertEqual(gui.COL_ERROR, (255, 69, 58))
        self.assertEqual(gui.COL_WARNING, (255, 159, 10))

    def test_no_green_colour_in_palette(self):
        for name in dir(gui):
            if not name.startswith("COL_"):
                continue
            r, g, b = getattr(gui, name)[:3]
            self.assertFalse(g > r and g > b, f"{name} is green-dominant")


if __name__ == "__main__":
    unittest.main()
