"""Level meter bars stay inside the meter at every display scale (the last bar was clipped at 125 %)."""
import unittest

from src import gui_hero


class _Scaled(gui_hero.HeroMixin):
    def __init__(self, dpi_scale):
        self.dpi_scale = dpi_scale

    def s(self, val):  # same rounding as GetsuGUI.s
        return max(1, int(round(val * self.dpi_scale)))


class MeterGeometryTests(unittest.TestCase):
    SCALES = (1.0, 1.25, 1.5, 1.75, 2.0, 2.25, 2.5, 3.0)

    def test_every_bar_fits_inside_the_meter(self):
        for scale in self.SCALES:
            obj = _Scaled(scale)
            width = obj.s(gui_hero.METER_W)
            for i in range(gui_hero.METER_BARS):
                x0, x1 = obj._meter_bar_x(i)
                self.assertGreaterEqual(x0, 0, (scale, i))
                self.assertLessEqual(x1, width, (scale, i))
                self.assertGreater(x1, x0, (scale, i))

    def test_last_bar_ends_on_the_meter_edge_and_bars_do_not_touch(self):
        for scale in self.SCALES:
            obj = _Scaled(scale)
            self.assertEqual(obj._meter_bar_x(gui_hero.METER_BARS - 1)[1], obj.s(gui_hero.METER_W), scale)
            for i in range(gui_hero.METER_BARS - 1):
                self.assertLess(obj._meter_bar_x(i)[1], obj._meter_bar_x(i + 1)[0], (scale, i))

    def test_meter_width_is_95_logical_px(self):
        self.assertEqual(gui_hero.METER_W, 95)


if __name__ == "__main__":
    unittest.main()
