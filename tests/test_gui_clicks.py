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
from src import gui_theme as theme

PANEL_NAMES = ("mics", "settings", "help")


def _center(rect):
    (x0, y0), (x1, y1) = rect
    return ((x0 + x1) / 2.0, (y0 + y1) / 2.0)


def _is_shown(tag):
    """True when the item and every group or window around it is shown (a hidden group hides its children)."""
    item = tag
    while item:
        if not dpg.is_item_shown(item):
            return False
        item = dpg.get_item_parent(item)
    return True


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
            with patch("src.gui_settings.save_config") as save:
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

    def test_done_text_button_closes_the_panel(self):
        app = self.app
        self._open("settings")
        rect = app._item_rect("sheet_done_settings")
        self.assertIsNotNone(rect)
        self._click_rect(rect)
        self.assertIsNone(app._open_panel_name)

    def test_done_hit_area_is_where_the_word_is_drawn(self):
        """The click rectangle must come from the rendered text, not from the child window's sheet-relative position."""
        app = self.app
        for name in ("settings", "mics"):
            self._open(name)
            label = f"sheet_done_{name}_label"
            drawn = (tuple(dpg.get_item_rect_min(label)), tuple(dpg.get_item_rect_max(label)))
            rect = app._item_rect(f"sheet_done_{name}")
            self.assertIsNotNone(rect, name)
            cx, cy = _center(drawn)
            self.assertTrue(rect[0][0] <= cx <= rect[1][0] and rect[0][1] <= cy <= rect[1][1], (name, rect, drawn))
            sheet = app._item_rect(f"sheet_{name}")
            self.assertGreaterEqual(rect[0][1], sheet[0][1], name)
            app._close_panels()

    # --- every clickable control ---------------------------------------------------------------

    @staticmethod
    def _drawn_rects(tag):
        """Viewport rectangles of the visible items drawn inside tag (text, drawlists, buttons; windows have none)."""
        out = []
        stack = [tag]
        while stack:
            item = stack.pop()
            for children in (dpg.get_item_children(item) or {}).values():
                stack.extend(children)
            if item == tag:
                continue
            try:
                state = dpg.get_item_state(item)
                if not state.get("visible", False) or "rect_min" not in state:
                    continue
                rmin, rmax = tuple(state["rect_min"]), tuple(state["rect_max"])
            except Exception:
                continue
            if rmax[0] > rmin[0] and rmax[1] > rmin[1]:
                out.append((item, (rmin, rmax)))
        return out

    def _assert_targets_match_drawing(self, targets, where):
        checked = 0
        for tag in targets:
            if not _is_shown(tag):
                continue  # controls behind a feature flag are not drawn and have no click area
            rect = self.app._item_rect(tag)
            self.assertIsNotNone(rect, f"{where}: {tag} has no click rectangle")
            for item, drawn in self._drawn_rects(tag):
                if dpg.get_item_type(item).endswith("mvSpacer"):
                    continue
                cx, cy = _center(drawn)
                inside = rect[0][0] - 1 <= cx <= rect[1][0] + 1 and rect[0][1] - 1 <= cy <= rect[1][1] + 1
                self.assertTrue(inside, f"{where}: {tag} click area {rect} misses drawn item "
                                        f"{dpg.get_item_alias(item) or item} at {drawn}")
                checked += 1
        return checked

    def test_every_click_area_covers_what_is_drawn(self):
        """Main-window controls and every panel's controls: the click area covers the control as rendered."""
        app = self.app
        checked = self._assert_targets_match_drawing(list(app._click_targets), "main")
        for name in PANEL_NAMES:
            self._open(name)
            targets = list(app._panel_targets.get(name, {}))
            self.assertTrue(targets, name)
            checked += self._assert_targets_match_drawing(targets, name)
            sheet = app._item_rect(f"sheet_{name}")
            for tag in targets:
                rect = app._item_rect(tag)
                self.assertGreaterEqual(rect[0][1], sheet[0][1] - 1, f"{name}: {tag} above its sheet")
            app._close_panels()
            self._frames()
        self.assertGreater(checked, 0)

    def test_every_click_target_runs_its_action(self):
        """A click in the middle of each control's area reaches that control and nothing else."""
        app = self.app
        app._rebuild_mic_list()  # the real app rebuilds the rows on every device refresh after the panels exist
        expected = {"mics": "sheet_done_mics", "settings": "sheet_done_settings", "help": "help_got_it"}
        for name, close_tag in expected.items():
            self.assertIn(close_tag, app._panel_targets.get(name, {}), f"{name}: {close_tag} is not clickable")
        for name in PANEL_NAMES:
            self._open(name)
            targets = app._panel_targets.get(name, {})
            for tag in list(targets):
                self._open(name) if app._open_panel_name != name else None
                calls = []
                with patch.dict(targets, {t: (lambda t=t: calls.append(t)) for t in targets}):
                    self.mouse.pos = _center(app._item_rect(tag))
                    app._on_mouse_click()
                self.assertEqual(calls, [tag], f"{name}: click on {tag}")
            app._close_panels()
            self._frames()
        targets = app._click_targets
        for tag in list(targets):
            if not _is_shown(tag):
                continue  # controls behind a feature flag (the built-in mic link) are not drawn and have no area
            calls = []
            with patch.dict(targets, {t: (lambda t=t: calls.append(t)) for t in targets}):
                self.mouse.pos = _center(app._item_rect(tag))
                app._on_mouse_click()
            self.assertEqual(calls, [tag], f"main: click on {tag}")

    def test_mic_list_rebuild_keeps_done_clickable(self):
        """Device refreshes rebuild the microphone rows; Done must survive that and still close the panel."""
        app = self.app
        app._rebuild_mic_list()
        app._rebuild_mic_list()
        self._open("mics")
        targets = app._panel_targets["mics"]
        self.assertIn("sheet_done_mics", targets)
        self.assertEqual(sorted(t for t in targets if t.startswith("mic_row_")),
                         [f"mic_row_{i}" for i in range(len(app._mic_entries()))])
        self._click_rect(app._item_rect("sheet_done_mics"))
        self.assertIsNone(app._open_panel_name)

    def test_done_hover_changes_only_the_text_colour(self):
        app = self.app
        self._open("settings")
        rect = app._item_rect("sheet_done_settings")
        label = "sheet_done_settings_label"
        self.mouse.pos = _center(rect)
        app._update_text_links()
        self.assertEqual(self._text_color(label), theme.COL_LINK_HOVER)
        self.mouse.down = True
        app._update_text_links()
        self.assertEqual(self._text_color(label), theme.COL_LINK_PRESSED)
        self.mouse.down = False
        self.mouse.pos = (-1000.0, -1000.0)
        app._update_text_links()
        self.assertEqual(self._text_color(label), theme.COL_ACCENT)

    def test_sponsor_link_is_a_text_link_with_no_fill(self):
        app = self.app
        self.assertEqual(dpg.get_item_theme("sponsor_link_btn"), app.theme_flat)

    def test_sponsor_link_hover_changes_only_the_text_colour(self):
        app = self.app
        rect = app._item_rect("sponsor_link_btn")
        self.assertIsNotNone(rect)
        self.mouse.pos = _center(rect)
        app._update_text_links()
        self.assertEqual(self._text_color("sponsor_link_btn_label"), theme.COL_LINK_HOVER)
        self.mouse.pos = (-1000.0, -1000.0)
        app._update_text_links()
        self.assertEqual(self._text_color("sponsor_link_btn_label"), theme.COL_ACCENT)

    def test_sponsor_link_does_not_hover_while_a_panel_is_open(self):
        app = self.app
        self._open("settings")
        self.mouse.pos = (-1000.0, -1000.0)
        app._update_text_links()
        rect = app._item_rect("sponsor_link_btn")
        self.mouse.pos = _center(rect) if rect is not None else (0.0, 0.0)
        app._update_text_links()
        self.assertNotEqual(self._text_color("sponsor_link_btn_label"), theme.COL_LINK_HOVER)

    def test_sponsor_click_opens_the_site(self):
        app = self.app
        rect = app._item_rect("sponsor_link_btn")
        with patch.object(g.webbrowser, "open") as opened:
            self._click_rect(rect)
        opened.assert_called_once_with("https://www.instagram.com/kopireman/")

    def test_built_in_mic_link_is_a_text_link_with_no_fill(self):
        self.assertEqual(dpg.get_item_theme("btn_use_builtin"), self.app.theme_flat)

    @staticmethod
    def _text_color(tag):
        # DearPyGui reports colours as floats in 0..1.
        return tuple(int(round(c * 255)) for c in dpg.get_item_configuration(tag)["color"][:3])

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


