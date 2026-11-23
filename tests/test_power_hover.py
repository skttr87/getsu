"""
Power button hover (Apple style, as in the approved mockup): the fill lightens one step on hover, eased over 0.2 s,
dims while pressed, and does nothing while starting / stopping or with a panel open. Built headless in process; the
mouse is patched, no desktop input.
"""
import unittest
from unittest.mock import patch

import _isolation  # noqa: F401

import dearpygui.dearpygui as dpg

from src import gui as g
from src import gui_hero as hero
from src import gui_theme as theme


class PowerColorTests(unittest.TestCase):
    def test_rest_colours_are_unchanged(self):
        self.assertEqual(hero.power_colors(False, 0.0, False), (theme.COL_CARD, hero.POWER_GLYPH_OFF))
        self.assertEqual(hero.power_colors(True, 0.0, False), (theme.COL_ACCENT, theme.COL_WHITE))

    def test_full_hover_uses_the_mockup_hover_colours(self):
        self.assertEqual(hero.power_colors(False, 1.0, False)[0], theme.COL_CARD_HOVER)
        self.assertEqual(hero.power_colors(False, 1.0, False)[1], tuple(theme.COL_TEXT_2))
        self.assertEqual(hero.power_colors(True, 1.0, False)[0], theme.COL_ACCENT_HOVER)

    def test_pressed_dims(self):
        self.assertEqual(hero.power_colors(True, 1.0, True)[0], theme.COL_ACCENT_PRESSED)
        on_pressed = sum(hero.power_colors(True, 1.0, True)[0])
        self.assertLess(on_pressed, sum(theme.COL_ACCENT))
        self.assertLess(sum(hero.power_colors(False, 1.0, True)[0]), sum(theme.COL_CARD))

    def test_half_hover_is_between_rest_and_hover(self):
        mid = hero.power_colors(False, 0.5, False)[0]
        for a, m, b in zip(theme.COL_CARD, mid, theme.COL_CARD_HOVER):
            self.assertTrue(min(a, b) <= m <= max(a, b))


class _Mouse:
    pos = (-1000.0, -1000.0)
    down = False


class PowerHoverTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mouse = _Mouse()
        cls.patchers = [
            patch.object(g.GetsuGUI, "_refresh_devices"),
            patch.object(g.threading, "Thread"),
            patch.object(g, "SmartMicRouter"),
            patch.object(g.threading, "Timer"),
            patch.object(dpg, "get_mouse_pos", side_effect=lambda local=True: cls.mouse.pos),
            patch.object(dpg, "is_mouse_button_down", side_effect=lambda button=0: cls.mouse.down),
        ]
        for p in cls.patchers:
            p.start()
        app = g.GetsuGUI()
        app.device_map = {"USB Mic": 1}
        app.device_raw = {"USB Mic": "USB Mic"}
        app.is_vbcable_installed = True
        app.build_ui(headless=True)
        cls.app = app
        for _ in range(4):
            dpg.render_dearpygui_frame()
        app._apply_setup_card(False)
        for _ in range(2):
            dpg.render_dearpygui_frame()

    @classmethod
    def tearDownClass(cls):
        dpg.destroy_context()
        for p in reversed(cls.patchers):
            p.stop()

    def setUp(self):
        app = self.app
        app._close_panels()
        app._busy = None
        app.is_running = False
        app._power_hover = 0.0
        app._power_pressed = False
        app._power_anim_t = None
        self.mouse.pos = (-1000.0, -1000.0)
        self.mouse.down = False

    def _centre(self):
        (x0, y0), (x1, y1) = self.app._leaf_rect("power_drawlist")
        return (x0 + x1) / 2.0, (y0 + y1) / 2.0, (x1 - x0) / 2.0

    def _run(self, start, seconds, step=0.05):
        t = start
        while t <= start + seconds + 1e-9:
            self.app._update_power_hover(t)
            t += step
        return t

    def test_hover_eases_in_over_about_200_ms_and_out_again(self):
        cx, cy, _ = self._centre()
        self.mouse.pos = (cx, cy)
        self.app._update_power_hover(10.0)
        self.app._update_power_hover(10.05)
        self.assertGreater(self.app._power_hover, 0.0)
        self.assertLess(self.app._power_hover, 1.0)  # still easing after 50 ms
        self._run(10.1, 0.2)
        self.assertEqual(self.app._power_hover, 1.0)
        self.mouse.pos = (-1000.0, -1000.0)
        self._run(11.0, 0.3)
        self.assertEqual(self.app._power_hover, 0.0)

    def test_square_corners_outside_the_circle_are_not_hover(self):
        cx, cy, r = self._centre()
        self.assertFalse(self.app._power_is_hovered((cx + r * 0.9, cy + r * 0.9)))  # inside the square, outside the circle
        self.assertTrue(self.app._power_is_hovered((cx + r * 0.6, cy)))

    def test_pressed_dims_the_button(self):
        cx, cy, _ = self._centre()
        self.mouse.pos = (cx, cy)
        self.mouse.down = True
        self._run(20.0, 0.3)
        self.assertTrue(self.app._power_pressed)
        self.mouse.down = False
        self._run(21.0, 0.1)
        self.assertFalse(self.app._power_pressed)

    def test_no_hover_while_starting_or_with_a_panel_open(self):
        cx, cy, _ = self._centre()
        self.app._busy = "starting"
        self.assertFalse(self.app._power_is_hovered((cx, cy)))
        self.app._busy = None
        self.app._open_panel("settings")
        self.assertFalse(self.app._power_is_hovered((cx, cy)))

    def test_no_redraw_while_nothing_changes(self):
        with patch.object(g.GetsuGUI, "_draw_power") as draw:
            self._run(30.0, 0.5)
        draw.assert_not_called()


if __name__ == "__main__":
    unittest.main()
