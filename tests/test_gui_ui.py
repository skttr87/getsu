import math
import unittest
from unittest.mock import MagicMock, patch

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
