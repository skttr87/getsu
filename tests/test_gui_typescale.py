"""
Layout checks for the type-scale proportions: power button, action buttons, microphone row, meter and card gaps,
panel head alignment, the first-run card, the voice-test label, and the window fit. Built headless, in process.
Rectangles are read after rendered frames, so they reflect the real text measurements.
"""
import unittest
from unittest.mock import patch

import _isolation  # noqa: F401  (keeps config writes off the tracked config.json)

import dearpygui.dearpygui as dpg

from src import gui as g


def _scale(app) -> float:
    return float(app.dpi_scale)


def _size(rect):
    (x0, y0), (x1, y1) = rect
    return x1 - x0, y1 - y0


class TypeScaleLayoutTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.patchers = [
            patch.object(g.GetsuGUI, "_refresh_devices"),
            patch.object(g.threading, "Thread"),
            patch.object(g, "SmartMicRouter"),
            patch.object(g.threading, "Timer"),
        ]
        for p in cls.patchers:
            p.start()
        try:
            app = g.GetsuGUI()
            app.device_map = {"USB Mic": 1, "Laptop Mic": 2}
            app.device_raw = {"USB Mic": "USB Mic", "Laptop Mic": "Laptop Mic"}
            app.is_vbcable_installed = True
            app.build_ui(headless=True)
        except Exception:
            for p in cls.patchers:
                p.stop()
            raise
        cls.app = app
        cls._frames(4)

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
        app.is_vbcable_installed = True
        app._install_state = None
        app._setup_dismissed = False
        app._poll_setup_card()
        self._frames(2)

    def test_power_button_is_96_logical_px(self):
        app = self.app
        w, h = _size(app._leaf_rect("power_drawlist"))
        self.assertAlmostEqual(w / _scale(app), g.POWER_D, delta=1)
        self.assertAlmostEqual(h / _scale(app), g.POWER_D, delta=1)

    def test_action_buttons_are_48_logical_px_tall(self):
        app = self.app
        for tag in ("btn_voice_test", "btn_settings", "btn_help"):
            _, h = _size(app._item_rect(tag))
            self.assertAlmostEqual(h / _scale(app), g.ACTION_H, delta=1, msg=tag)

    def test_microphone_row_is_68_logical_px_tall(self):
        _, h = _size(self.app._leaf_rect("mic_row"))
        self.assertAlmostEqual(h / _scale(self.app), g.MIC_ROW_H, delta=1)

    def test_meter_is_inside_the_hero_slot_and_20_px_above_the_card(self):
        app = self.app
        hero = app._leaf_rect("hero_box")
        meter = app._leaf_rect("meter")
        card = app._leaf_rect("mic_card")
        self.assertLessEqual(meter[1][1], hero[1][1] + 0.5, "the meter is not clipped by the hero slot")
        gap = (card[0][1] - meter[1][1]) / _scale(app)
        self.assertAlmostEqual(gap, 20, delta=1.5)

    def test_first_run_card_sits_20_px_above_the_microphone_card(self):
        app = self.app
        app.is_vbcable_installed = False
        app._setup_dismissed = False
        app._poll_setup_card()
        self._frames(3)
        self.assertIsNotNone(app._leaf_rect("setup_card"))
        card = app._leaf_rect("setup_card")
        mic = app._leaf_rect("mic_card")
        self.assertAlmostEqual((mic[0][1] - card[1][1]) / _scale(app), 20, delta=1.5)
        # The card keeps at least 14 logical px below its buttons (the three-line offer text).
        buttons = app._leaf_rect("setup_skip")
        self.assertGreaterEqual((card[1][1] - buttons[1][1]) / _scale(app), 14 - 1)

    def test_done_button_ends_on_the_card_edge(self):
        app = self.app
        app._open_panel("settings")
        self._frames(3)
        done = app._leaf_rect("sheet_done_settings")
        self.assertIsNotNone(done)
        expected_right = (g.CONTENT_PAD + g.CONTENT_W) * _scale(app)
        self.assertAlmostEqual(done[1][0], expected_right, delta=1.5)

    def test_voice_label_stays_inside_the_button_when_it_gets_longer(self):
        app = self.app
        width = _size(app._item_rect("btn_voice_test"))[0]
        try:
            app._set_button_label("btn_voice_test", "Playing back... (11s)")
            self._frames(2)
            pad_w = dpg.get_item_configuration("btn_voice_test_pad")["width"]
            content = app._btn_content["btn_voice_test"][1] + app._btn_content["btn_voice_test"][2] + \
                app._text_w("Playing back... (11s)", app.font_btn)
            self.assertGreaterEqual(pad_w, 0)
            self.assertLessEqual(pad_w + content, width + 1)
        finally:
            app._set_button_label("btn_voice_test", "Hear myself")
            self._frames(2)

    def test_window_fits_its_content_and_stays_within_640_logical_px(self):
        app = self.app
        bottom = dpg.get_item_rect_max("footer_creator_text")[1]
        client_h = dpg.get_viewport_client_height()
        self.assertLessEqual(client_h / _scale(app), 640)
        self.assertGreaterEqual(client_h, bottom)

    def test_microphone_list_shows_four_rows_without_scrolling(self):
        app = self.app
        saved_map, saved_raw = dict(app.device_map), dict(app.device_raw)
        try:
            app.device_map = {f"Mic {i}": i for i in range(1, 5)}
            app.device_raw = {k: k for k in app.device_map}
            app._open_panel("mics")
            self._frames(3)
            list_h = dpg.get_item_configuration("mic_list")["height"]
            needed = 4 * g.MIC_ROW_H + 3  # four rows and three 1 px dividers, logical
            self.assertGreaterEqual(list_h, _scale(app) * needed - 1)
        finally:
            app.device_map, app.device_raw = saved_map, saved_raw
            app._close_panels()
            self._frames(2)

    def test_boost_labels_are_centred_in_their_equal_segments(self):
        app = self.app
        app._open_panel("settings")
        self._frames(3)
        app._tick_controls()  # places the labels once the panel is rendered
        self._frames(2)
        self.assertFalse(app._boost_labels_pending)
        (x0, _), (x1, _) = app._leaf_rect("seg_boost_bg")
        (_, y0), (_, y1) = app._leaf_rect("seg_boost_bg")
        seg_w = (x1 - x0) / len(g.BOOST_DBS)
        for i, db in enumerate(g.BOOST_DBS):
            rect = app._leaf_rect(f"seg_lbl_{db}")
            self.assertIsNotNone(rect, db)
            cx = (rect[0][0] + rect[1][0]) / 2.0
            cy = (rect[0][1] + rect[1][1]) / 2.0
            self.assertLessEqual(abs(cx - (x0 + (i + 0.5) * seg_w)), 1.0, f"label {db} x")
            self.assertLessEqual(abs(cy - (y0 + y1) / 2.0), 1.0, f"label {db} y")

    def test_settings_panel_text_is_one_css_px_smaller_than_the_shared_scale(self):
        app = self.app
        self.assertEqual(g.TYPE_SET_TITLE, g.TYPE_TITLE - 1)
        self.assertEqual(g.TYPE_SET_LABEL, g.TYPE_BODY - 1)
        self.assertEqual(g.TYPE_SET_DETAIL, g.TYPE_SUB - 1)
        self.assertEqual(g.TYPE_SET_SEG, g.TYPE_SUB - 1)
        for name in ("font_set_title", "font_set_label", "font_set_detail", "font_set_seg"):
            self.assertIsNotNone(getattr(app, name), name)

    def test_sheet_head_keeps_done_inside_the_panel_on_the_microphone_panel(self):
        app = self.app
        app._open_panel("mics")
        self._frames(3)
        done = app._leaf_rect("sheet_done_mics")
        self.assertIsNotNone(done)
        self.assertLessEqual(done[1][0], dpg.get_viewport_client_width() + 0.5)


if __name__ == "__main__":
    unittest.main()
