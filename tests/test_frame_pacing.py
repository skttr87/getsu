import unittest

from src.frame_pacing import (
    VISIBLE_FRAME_INTERVAL_S,
    HIDDEN_FRAME_INTERVAL_S,
    frame_interval_s,
    frame_sleep_s,
)


class FramePacingTests(unittest.TestCase):
    def test_visible_window_is_capped_to_20_fps(self):
        self.assertAlmostEqual(frame_interval_s(True), 0.05)
        self.assertAlmostEqual(1.0 / frame_interval_s(True), 20.0)

    def test_hidden_window_waits_100_ms_between_frames(self):
        self.assertAlmostEqual(frame_interval_s(False), 0.10)

    def test_hidden_gap_never_exceeds_100_ms(self):
        # Message pumping depends on a frame at least every 100 ms.
        self.assertLessEqual(HIDDEN_FRAME_INTERVAL_S, 0.10)
        self.assertEqual(frame_interval_s(False), HIDDEN_FRAME_INTERVAL_S)

    def test_sleep_fills_the_rest_of_the_interval(self):
        self.assertAlmostEqual(frame_sleep_s(True, 0.002), VISIBLE_FRAME_INTERVAL_S - 0.002)
        self.assertAlmostEqual(frame_sleep_s(False, 0.002), HIDDEN_FRAME_INTERVAL_S - 0.002)

    def test_slow_frame_gives_no_sleep(self):
        self.assertEqual(frame_sleep_s(True, 0.08), 0.0)
        self.assertEqual(frame_sleep_s(False, 0.5), 0.0)

    def test_negative_elapsed_is_treated_as_zero(self):
        self.assertAlmostEqual(frame_sleep_s(True, -1.0), VISIBLE_FRAME_INTERVAL_S)


if __name__ == "__main__":
    unittest.main()
