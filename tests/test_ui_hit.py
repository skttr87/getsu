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


if __name__ == "__main__":
    unittest.main()
