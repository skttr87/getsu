"""Build guard and fallback behaviour of the dark tray menu switch (src/win_theme.py)."""
import unittest
from unittest.mock import MagicMock, patch

from src import win_theme


class BuildGuardTests(unittest.TestCase):

    def test_builds_before_1903_are_not_supported(self):
        self.assertFalse(win_theme.supports_dark_menus(17763))   # Windows 10 1809: ordinal 135 means something else
        self.assertFalse(win_theme.supports_dark_menus(18361))

    def test_1903_and_later_are_supported(self):
        self.assertTrue(win_theme.supports_dark_menus(18362))
        self.assertTrue(win_theme.supports_dark_menus(26100))

    def test_unknown_build_is_not_supported(self):
        self.assertFalse(win_theme.supports_dark_menus(0))


class EnableDarkMenusTests(unittest.TestCase):

    def test_old_build_never_touches_uxtheme(self):
        with patch("src.win_theme.ctypes.WinDLL") as windll:
            self.assertFalse(win_theme.enable_dark_menus(build=17763))
            windll.assert_not_called()

    def test_unreadable_build_leaves_menus_light(self):
        with patch("src.win_theme.windows_build", side_effect=RuntimeError("boom")):
            self.assertFalse(win_theme.enable_dark_menus())

    def test_missing_uxtheme_leaves_menus_light(self):
        with patch("src.win_theme.ctypes.WinDLL", side_effect=OSError("no uxtheme")):
            self.assertFalse(win_theme.enable_dark_menus(build=26100))

    def test_missing_ordinal_leaves_menus_light(self):
        fake_dll = MagicMock()
        with patch("src.win_theme.ctypes.WinDLL", return_value=fake_dll), \
                patch("src.win_theme._ordinal_function", return_value=None):
            self.assertFalse(win_theme.enable_dark_menus(build=26100))

    def test_supported_build_sets_force_dark_then_flushes(self):
        calls = []
        set_mode = MagicMock(side_effect=lambda mode: calls.append(("set", mode)) or 0)
        flush = MagicMock(side_effect=lambda: calls.append(("flush",)))

        def fake_ordinal(dll, ordinal, prototype):
            return set_mode if ordinal == win_theme.ORDINAL_SET_PREFERRED_APP_MODE else flush

        with patch("src.win_theme.ctypes.WinDLL", return_value=MagicMock()), \
                patch("src.win_theme._ordinal_function", side_effect=fake_ordinal):
            self.assertTrue(win_theme.enable_dark_menus(build=26100))
        self.assertEqual(calls, [("set", win_theme.APP_MODE_FORCE_DARK), ("flush",)])


class WindowsBuildTests(unittest.TestCase):

    def test_not_windows_returns_zero(self):
        with patch("src.win_theme.sys.platform", "linux"):
            self.assertEqual(win_theme.windows_build(), 0)


if __name__ == "__main__":
    unittest.main()
