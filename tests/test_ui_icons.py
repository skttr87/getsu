import math
import unittest

from src import ui_icons as icons


def _inside_grid(points, lo=-0.01, hi=24.01):
    return all(lo <= x <= hi and lo <= y <= hi for x, y in points)


class IconGeometryTests(unittest.TestCase):
    def test_grid_shapes_stay_inside_the_24_unit_box(self):
        shapes = [icons.POWER_ARC, icons.PLAY, icons.CHEVRON, icons.CHECK, icons.HELP_RING,
                  icons.MIC_BODY, icons.MIC_CRADLE, icons.LAPTOP_SCREEN, icons.HEADSET_BAND,
                  icons.HEADSET_LEFT, icons.BUD_LEFT, icons.BUD_RIGHT]
        for shape in shapes:
            self.assertTrue(_inside_grid(shape))

    def test_power_arc_leaves_a_gap_at_the_top(self):
        # The mockup power symbol has its opening at the top, where the stem sits.
        near_top = [(x, y) for x, y in icons.POWER_ARC if abs(x - 12.0) < 1.0 and y < 6.0]
        self.assertEqual(near_top, [])

    def test_to_px_maps_the_box(self):
        self.assertEqual(icons.to_px([(0.0, 0.0), (24.0, 24.0)], 10.0, 20.0, 48.0), [(10.0, 20.0), (58.0, 68.0)])

    def test_arc_points_radius(self):
        for x, y in icons.arc_points(0.0, 0.0, 5.0, 0.0, 270.0, 12):
            self.assertAlmostEqual(math.hypot(x, y), 5.0)

    def test_rounded_rect_is_closed_to_its_size(self):
        pts = icons.rounded_rect_points(0.0, 0.0, 10.0, 4.0, 1.0)
        xs = [p[0] for p in pts]
        ys = [p[1] for p in pts]
        self.assertAlmostEqual(min(xs), 0.0)
        self.assertAlmostEqual(max(xs), 10.0)
        self.assertAlmostEqual(min(ys), 0.0)
        self.assertAlmostEqual(max(ys), 4.0)


if __name__ == "__main__":
    unittest.main()
