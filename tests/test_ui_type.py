import os
import struct
import unittest

from src import ui_type

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
INTER_DIR = os.path.join(ROOT, "assets", "fonts")


def _sfnt(upm: int, ascender: int, descender: int) -> bytes:
    """A minimal sfnt with a head and an hhea table carrying the given metrics."""
    head = bytearray(54)
    head[18:20] = struct.pack(">H", upm)
    hhea = bytearray(36)
    hhea[4:8] = struct.pack(">hh", ascender, descender)
    tables = [(b"head", bytes(head)), (b"hhea", bytes(hhea))]
    header = b"\x00\x01\x00\x00" + struct.pack(">HHHH", len(tables), 0, 0, 0)
    offset = 12 + 16 * len(tables)
    directory = b""
    body = b""
    for tag, payload in tables:
        directory += struct.pack(">4sIII", tag, 0, offset + len(body), len(payload))
        body += payload
    return header + directory + body


class LineHeightFactorTests(unittest.TestCase):

    def test_bundled_inter_factor_is_between_1_15_and_1_30(self):
        for name in ("Inter-Regular.ttf", "Inter-SemiBold.ttf"):
            factor = ui_type.read_line_height_factor(os.path.join(INTER_DIR, name))
            self.assertGreater(factor, 1.15, name)
            self.assertLess(factor, 1.30, name)

    def test_bundled_inter_factor_matches_its_metrics(self):
        # Inter: ascender 1984, descender -494, unitsPerEm 2048.
        factor = ui_type.read_line_height_factor(os.path.join(INTER_DIR, "Inter-Regular.ttf"))
        self.assertAlmostEqual(factor, (1984 + 494) / 2048.0, places=6)

    def test_synthetic_metrics_give_the_span_over_units_per_em(self):
        self.assertAlmostEqual(ui_type.line_height_factor_from_bytes(_sfnt(1000, 800, -200)), 1.0)
        self.assertAlmostEqual(ui_type.line_height_factor_from_bytes(_sfnt(2000, 1500, -500)), 1.0)

    def test_unreadable_data_is_rejected(self):
        with self.assertRaises(ValueError):
            ui_type.line_height_factor_from_bytes(b"")
        with self.assertRaises(ValueError):
            ui_type.line_height_factor_from_bytes(b"ttcf" + b"\x00" * 60)
        with self.assertRaises(ValueError):
            ui_type.line_height_factor_from_bytes(_sfnt(0, 800, -200))
        with self.assertRaises(ValueError):
            ui_type.line_height_factor_from_bytes(_sfnt(1000, -200, 800))

    def test_missing_file_raises_os_error(self):
        with self.assertRaises(OSError):
            ui_type.read_line_height_factor(os.path.join(INTER_DIR, "no-such-font.ttf"))


class FontPxTests(unittest.TestCase):

    def test_css_size_is_scaled_by_factor_and_dpi(self):
        self.assertEqual(ui_type.font_px(15, 1.21, 1.25), 23)   # row title at 125 %
        self.assertEqual(ui_type.font_px(20, 1.21, 1.25), 30)   # state title at 125 %
        self.assertEqual(ui_type.font_px(13, 1.21, 1.25), 20)   # footer at 125 %
        self.assertEqual(ui_type.font_px(15, 1.21, 1.0), 18)

    def test_factor_one_and_scale_one_keep_the_css_size(self):
        self.assertEqual(ui_type.font_px(14, 1.0, 1.0), 14)

    def test_result_is_never_below_one_pixel(self):
        self.assertEqual(ui_type.font_px(0.1, 1.0, 1.0), 1)


if __name__ == "__main__":
    unittest.main()
