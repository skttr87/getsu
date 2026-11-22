"""
Regression: for 2-channel microphones, InputDownmixer returns one buffer that it reuses for every frame, and
process_mono_frame processes in place and returns that same buffer. Code that STORES frames (the GUI voice test)
must copy them, or every stored frame becomes the last frame and the voice test plays back silence.
"""
import ast
import os
import sys
import unittest

import numpy as np

sys.path.insert(0, os.path.dirname(__file__))
import _isolation  # noqa: F401

from src.dsp import AdaptiveNoiseGate, HighPassFilter, InputDownmixer, TransientSuppressor, process_mono_frame

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _stereo_frames(n=20, seed=3):
    rng = np.random.default_rng(seed)
    return [(rng.standard_normal((480, 2)) * 0.2).astype(np.float32) for _ in range(n)]


def _pipeline():
    return HighPassFilter(), AdaptiveNoiseGate(lookahead=True, floor_gain=0.06), TransientSuppressor()


class TestStoredFramesAreCopies(unittest.TestCase):
    def test_frames_alias_without_copy(self):
        """Documents why callers must copy: without a copy all stored frames are one buffer."""
        dm = InputDownmixer()
        hpf, gate, ts = _pipeline()
        stored = []
        for x in _stereo_frames():
            y, *_ = process_mono_frame(dm.process(x), None, hpf, gate, 1.0, False, None, ts, 0.0)
            stored.append(y)
        self.assertTrue(np.shares_memory(stored[0], stored[-1]))

    def test_copies_keep_distinct_frames(self):
        dm = InputDownmixer()
        hpf, gate, ts = _pipeline()
        stored = []
        for x in _stereo_frames():
            y, *_ = process_mono_frame(dm.process(x), None, hpf, gate, 1.0, False, None, ts, 0.0)
            stored.append(y.copy())
        unique = {f.tobytes() for f in stored}
        self.assertEqual(len(unique), len(stored))

    def test_voice_test_stores_copies(self):
        """The GUI voice test must append a copy of each processed frame (processed_chunks.append(x.copy()))."""
        with open(os.path.join(ROOT, "src", "gui.py"), encoding="utf-8") as f:
            tree = ast.parse(f.read())
        appends = [
            node for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "append"
            and isinstance(node.func.value, ast.Name) and node.func.value.id == "processed_chunks"
        ]
        self.assertTrue(appends, "voice test loop not found")
        for call in appends:
            arg = call.args[0]
            is_copy = (isinstance(arg, ast.Call) and isinstance(arg.func, ast.Attribute) and arg.func.attr == "copy")
            self.assertTrue(is_copy, "processed_chunks.append(...) must store a copy of the processed frame")


if __name__ == "__main__":
    unittest.main()
