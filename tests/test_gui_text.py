"""No emoji or pictographic symbols in the GUI source strings (rules.md section 7)."""
import io
import os
import tokenize
import unittest

GUI_SOURCE = os.path.join(os.path.dirname(__file__), "..", "src", "gui.py")

# Emoji and pictograph blocks, plus the geometric and dingbat symbols the old UI used as status marks
# (for example the triangle, circle and check characters).
FORBIDDEN_RANGES = (
    (0x2190, 0x21FF),    # arrows
    (0x25A0, 0x25FF),    # geometric shapes (triangles, circles)
    (0x2600, 0x27BF),    # miscellaneous symbols and dingbats
    (0x2B00, 0x2BFF),    # miscellaneous symbols and arrows
    (0x1F000, 0x1FAFF),  # emoji and pictographs
)


class GuiStringTests(unittest.TestCase):

    def test_no_emoji_or_pictographs_in_gui_string_literals(self):
        with open(GUI_SOURCE, "rb") as f:
            source = f.read().decode("utf-8")
        found = []
        for tok in tokenize.generate_tokens(io.StringIO(source).readline):
            if tok.type != tokenize.STRING:
                continue
            for ch in tok.string:
                code = ord(ch)
                if any(lo <= code <= hi for lo, hi in FORBIDDEN_RANGES):
                    found.append((tok.start[0], hex(code), tok.string[:40]))
        self.assertEqual(found, [], f"pictographic symbols in gui.py strings: {found}")


if __name__ == "__main__":
    unittest.main()
