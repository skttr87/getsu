"""Geometric hit testing for the main window clicks (src/ui_hit.py)."""
import unittest

from src import ui_hit


SHEET = ((0, 200), (420, 640))          # the bottom sheet of the microphone panel
ROW_0 = ((16, 240), (404, 292))          # a microphone row inside the sheet
LABEL_IN_ROW_0 = ((60, 250), (250, 280))  # the nested label child window of that row


class PointInRectTests(unittest.TestCase):

    def test_min_edge_inside_max_edge_outside(self):
        rect = ((10, 10), (20, 20))
        self.assertTrue(ui_hit.point_in_rect((10, 10), rect))
        self.assertTrue(ui_hit.point_in_rect((19.9, 19.9), rect))
        self.assertFalse(ui_hit.point_in_rect((20, 15), rect))
        self.assertFalse(ui_hit.point_in_rect((9.9, 15), rect))


class PickTargetTests(unittest.TestCase):

    def test_click_in_nested_label_selects_the_row(self):
        # The label is a child of the row, and only the row is registered as a target.
        targets = [("mic_row_0", ROW_0)]
        self.assertEqual(ui_hit.pick_target((120, 265), targets), "mic_row_0")

    def test_topmost_target_wins_when_targets_overlap(self):
        targets = [("under", ROW_0), ("over", LABEL_IN_ROW_0)]
        self.assertEqual(ui_hit.pick_target((120, 265), targets), "over")

    def test_no_target_under_point_returns_none(self):
        self.assertIsNone(ui_hit.pick_target((5, 5), [("mic_row_0", ROW_0)]))

    def test_targets_without_rect_never_match(self):
        self.assertIsNone(ui_hit.pick_target((120, 265), [("mic_row_0", None)]))
        self.assertEqual(ui_hit.pick_target((120, 265), [("gone", None), ("mic_row_0", ROW_0)]), "mic_row_0")


class ClickOutsideTests(unittest.TestCase):

    def test_click_inside_sheet_does_not_close(self):
        self.assertFalse(ui_hit.click_is_outside((120, 265), SHEET))

    def test_click_on_scrim_closes(self):
        self.assertTrue(ui_hit.click_is_outside((120, 100), SHEET))
        self.assertTrue(ui_hit.click_is_outside((120, 640), SHEET))  # below the sheet edge

    def test_unmeasured_sheet_keeps_panel_open(self):
        self.assertFalse(ui_hit.click_is_outside((120, 100), None))


class ControlRectangleTests(unittest.TestCase):
    """GUI._item_rect: a control made of child windows is measured by the union of its leaf drawlists."""

    def _gui(self, parts, rects):
        from unittest.mock import patch
        from src import gui as g
        obj = g.GetsuGUI.__new__(g.GetsuGUI)
        obj._hit_parts = parts
        patcher = patch.object(g.GetsuGUI, "_leaf_rect", side_effect=lambda tag: rects.get(tag))
        return obj, patcher

    def test_mic_row_rectangle_is_union_of_icon_and_tick(self):
        rects = {"row_icon": ((16, 240), (32, 300)), "row_tick": ((360, 240), (376, 300))}
        obj, patcher = self._gui({"mic_row_0": ["row_icon", "row_tick"]}, rects)
        with patcher:
            self.assertEqual(obj._item_rect("mic_row_0"), ((16, 240), (376, 300)))

    def test_control_without_parts_uses_its_own_rectangle(self):
        obj, patcher = self._gui({}, {"sheet_mics": ((0, 200), (420, 640))})
        with patcher:
            self.assertEqual(obj._item_rect("sheet_mics"), ((0, 200), (420, 640)))

    def test_no_measurable_part_gives_no_rectangle(self):
        obj, patcher = self._gui({"mic_row_0": ["gone"]}, {})
        with patcher:
            self.assertIsNone(obj._item_rect("mic_row_0"))

if __name__ == "__main__":
    unittest.main()
