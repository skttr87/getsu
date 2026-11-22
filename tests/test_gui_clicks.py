"""
Click-path tests for the main window and its panels, run in process with DearPyGui built headless.

Clicks are simulated without any desktop input: dpg.get_mouse_pos and dpg.is_mouse_button_down are patched to
return chosen viewport points, then the app's own mouse handler (GetsuGUI._on_mouse_click) and the per-frame
drag tick (GetsuGUI._tick_controls) run. Item rectangles come from the real layout after rendered frames.
The DearPyGui viewport is shown while the tests run (it is required for layout); no input is sent to it.
"""
import unittest
from unittest.mock import MagicMock, patch

import _isolation  # noqa: F401  (keeps config writes off the tracked config.json)

import dearpygui.dearpygui as dpg

from src import gui as g

PANEL_NAMES = ("mics", "settings", "help")


def _center(rect):
    (x0, y0), (x1, y1) = rect
    return ((x0 + x1) / 2.0, (y0 + y1) / 2.0)


class _Mouse:
    """Stands in for the mouse: position and left button state."""

    def __init__(self):
        self.pos = (0.0, 0.0)
        self.down = False


class GuiClickPathTests(unittest.TestCase):

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
        try:
            app = g.GetsuGUI()
            app.device_map = {"USB Mic": 1, "Laptop Mic": 2}
            app.device_raw = {"USB Mic": "USB Mic", "Laptop Mic": "Laptop Mic"}
            app.build_ui(headless=True)
        except Exception:
            for p in cls.patchers:
                p.stop()
            raise
        cls.app = app
        cls._frames(3)

    @classmethod
    def tearDownClass(cls):
        dpg.destroy_context()
        for p in reversed(cls.patchers):
            p.stop()

    @staticmethod
    def _frames(n=2):
        for _ in range(n):
            dpg.render_dearpygui_frame()

    def setUp(self):
        app = self.app
        app._close_panels()
        app._drag = None
        app._slider_hovered = False
        app._seg_hover = None
        self.mouse.down = False
        self.mouse.pos = (-1000.0, -1000.0)
        app.set_mic_boost(0)
        app._set_vad(0.70)
        app.config["auto_route"] = True
        app.config["music_mode"] = False
        app._switch_set("auto_route", True)
        app._switches["music_mode"].set(False)
        self._frames()

    def _click(self, point):
        """A left click at point: press handler, then the frame that follows."""
        self.mouse.pos = point
        self.app._on_mouse_click()
        self._frames()

    def _click_rect(self, rect):
        self._click(_center(rect))

    def _open(self, name):
        self.app._open_panel(name)
        self._frames(3)
        self.assertEqual(self.app._open_panel_name, name)

    # --- mic boost -----------------------------------------------------------------------------

    def test_each_boost_segment_click_selects_it_and_saves_it(self):
        app = self.app
        self._open("settings")
        rect = app._item_rect("seg_boost")
        self.assertIsNotNone(rect)
        x0, x1 = rect[0][0], rect[1][0]
        y = (rect[0][1] + rect[1][1]) / 2.0
        seg_w = (x1 - x0) / 4.0
        for i, db in enumerate(g.BOOST_DBS):
            with patch.object(g, "save_config") as save:
                self._click((x0 + seg_w * (i + 0.5), y))
            self.assertEqual(app.mic_boost_db, db, f"segment {db}")
            self.assertEqual(app.config["mic_boost_db"], db, f"segment {db}")
            self.assertTrue(save.called, f"segment {db} is saved")

    def test_boost_click_outside_the_track_changes_nothing(self):
        app = self.app
        self._open("settings")
        rect = app._item_rect("seg_boost")
        self._click((rect[1][0] + 40, (rect[0][1] + rect[1][1]) / 2.0))
        self.assertEqual(app.mic_boost_db, 0)

    # --- sensitivity knob ----------------------------------------------------------------------

    def test_click_on_track_end_sets_minimum_and_maximum(self):
        app = self.app
        self._open("settings")
        rect = app._item_rect("slider_vad")
        y = (rect[0][1] + rect[1][1]) / 2.0
        self._click((rect[0][0], y))
        self.assertAlmostEqual(app._vad_value, g.VAD_MIN)
        self.assertAlmostEqual(app.config["vad_threshold"], g.VAD_MIN)
        self._click((rect[1][0] - 1, y))  # the max edge itself is outside the rectangle
        self.assertAlmostEqual(app._vad_value, g.VAD_MAX)
        self.assertAlmostEqual(app.config["vad_threshold"], g.VAD_MAX)

    def test_click_in_the_middle_sets_the_middle_value(self):
        app = self.app
        self._open("settings")
        rect = app._item_rect("slider_vad")
        self._click(_center(rect))
        self.assertAlmostEqual(app.config["vad_threshold"], 0.65, places=2)
        self.assertTrue(app.config["vad_customized"])

    def test_drag_moves_the_value_and_release_ends_it(self):
        app = self.app
        self._open("settings")
        rect = app._item_rect("slider_vad")
        y = (rect[0][1] + rect[1][1]) / 2.0
        self._click((rect[0][0] + (rect[1][0] - rect[0][0]) * 0.25, y))  # press at about 0.50
        self.assertEqual(app._drag, "vad")
        # Hold and drag to the right end, then past it (the value clamps at the maximum).
        self.mouse.down = True
        self.mouse.pos = (rect[1][0] + 30, y)
        app._tick_controls()
        self.assertAlmostEqual(app.config["vad_threshold"], g.VAD_MAX)
        # Drag back to the middle while held.
        self.mouse.pos = (_center(rect)[0], y)
        app._tick_controls()
        self.assertAlmostEqual(app.config["vad_threshold"], 0.65, places=2)
        # Release: the next tick ends the drag and later moves do nothing.
        self.mouse.down = False
        app._tick_controls()
        self.assertIsNone(app._drag)
        self.mouse.pos = (rect[1][0] - 1, y)
        app._tick_controls()
        self.assertAlmostEqual(app.config["vad_threshold"], 0.65, places=2)

    def test_knob_value_change_is_debounced_through_the_existing_save(self):
        app = self.app
        self._open("settings")
        rect = app._item_rect("slider_vad")
        with patch.object(g.threading, "Timer") as timer:
            self._click(_center(rect))
        self.assertTrue(timer.called, "the save is scheduled through threading.Timer as before")

    # --- switches -------------------------------------------------------------------------------

    def test_each_switch_click_toggles_it_and_calls_its_handler(self):
        app = self.app
        self._open("settings")
        with patch.object(app, "on_auto_route_toggled") as auto, patch.object(app, "on_music_mode_toggled") as music:
            rect = app._item_rect("switch_auto_route")
            self._click_rect(rect)
            self.assertFalse(app._switches["auto_route"].on)
            auto.assert_called_once_with(None, False)
            rect = app._item_rect("switch_music_mode")
            self._click_rect(rect)
            self.assertTrue(app._switches["music_mode"].on)
            music.assert_called_once_with(None, True)

    # --- main window ----------------------------------------------------------------------------

    def test_click_on_microphone_card_opens_the_microphone_panel(self):
        app = self.app
        rect = app._item_rect("mic_row")
        self.assertIsNotNone(rect)
        self._click_rect(rect)
        self.assertEqual(app._open_panel_name, "mics")

    def test_click_on_a_microphone_row_selects_that_microphone(self):
        app = self.app
        self._open("mics")
        calls = []
        with patch.object(app, "on_input_changed", side_effect=lambda s, label: calls.append(label)):
            rect = app._item_rect("mic_row_1")
            self.assertIsNotNone(rect)
            self._click_rect(rect)
        self.assertEqual(calls, ["Laptop Mic"])
        self.assertIsNone(app._open_panel_name)

    def test_click_on_help_button_opens_help(self):
        app = self.app
        rect = app._item_rect("btn_help")
        self.assertIsNotNone(rect)
        self._click_rect(rect)
        self.assertEqual(app._open_panel_name, "help")

    def test_click_on_scrim_closes_each_panel(self):
        app = self.app
        for name in ("settings", "help", "mics"):
            self._open(name)
            self._click((4.0, 4.0))  # above the sheet, on the scrim
            self.assertIsNone(app._open_panel_name, name)

    def test_click_inside_the_sheet_does_not_close_it(self):
        app = self.app
        self._open("settings")
        rect = app._item_rect("sheet_settings")
        self._click(_center(rect))
        self.assertEqual(app._open_panel_name, "settings")

    # --- layout ---------------------------------------------------------------------------------

    def test_settings_controls_are_stacked_in_order_inside_the_window(self):
        """Rows are stacked top to bottom (slider, boost, two switches) and every control is inside the window."""
        app = self.app
        self._open("settings")
        vp_w = dpg.get_viewport_client_width()
        vp_h = dpg.get_viewport_client_height()
        tops = []
        for tag in ("slider_vad", "seg_boost", "switch_auto_route", "switch_music_mode"):
            rect = app._item_rect(tag)
            self.assertIsNotNone(rect, tag)
            (x0, y0), (x1, y1) = rect
            self.assertTrue(0 <= x0 and x1 <= vp_w, tag)
            self.assertTrue(0 <= y0 and y1 <= vp_h, tag)
            tops.append(y0)
        self.assertEqual(tops, sorted(tops))
        self.assertEqual(len(set(tops)), len(tops))
    def test_hero_meter_to_microphone_card_gap_is_about_20_logical_px(self):
        meter = self.app._leaf_rect("meter")
        card = self.app._leaf_rect("mic_card")
        gap_physical = card[0][1] - meter[1][1]
        self.assertAlmostEqual(gap_physical / float(self.app.dpi_scale), 20, delta=1.5)


if __name__ == "__main__":
    unittest.main()


