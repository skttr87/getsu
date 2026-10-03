"""
Ultra-low-overhead ctypes binding to native rnnoise.dll.
Processes 480-sample (10ms @ 48kHz) audio frames.
"""
import os
import sys
import ctypes
from typing import Tuple
import numpy as np

FRAME_SIZE = 480
SAMPLE_RATE = 48000
DTYPE = np.int16

_candidates = []
if getattr(sys, 'frozen', False):
    _candidates.append(os.path.join(getattr(sys, '_MEIPASS', ''), "src", "native", "rnnoise.dll"))
    _candidates.append(os.path.join(os.path.dirname(sys.executable), "src", "native", "rnnoise.dll"))
_candidates.append(os.path.join(os.path.dirname(__file__), "native", "rnnoise.dll"))
_candidates.append(os.path.abspath("src/native/rnnoise.dll"))

_dll_path = None
for _p in _candidates:
    if _p and os.path.exists(_p):
        _dll_path = _p
        break

if not _dll_path:
    raise FileNotFoundError("rnnoise.dll not found in search paths")

_lib = ctypes.CDLL(_dll_path)

_lib.rnnoise_create.argtypes = [ctypes.c_void_p]
_lib.rnnoise_create.restype = ctypes.c_void_p

_lib.rnnoise_destroy.argtypes = [ctypes.c_void_p]
_lib.rnnoise_destroy.restype = None

_lib.rnnoise_process_frame.argtypes = [
    ctypes.c_void_p,
    ctypes.POINTER(ctypes.c_float),
    ctypes.POINTER(ctypes.c_float),
]
_lib.rnnoise_process_frame.restype = ctypes.c_float
_lib.rnnoise_get_frame_size.restype = ctypes.c_int


class RNNoise:
    """Wrapper class managing RNNoise neural filter state."""

    def __init__(self):
        self._state = _lib.rnnoise_create(None)
        if not self._state:
            raise RuntimeError("Failed to create RNNoise state")

    def process_frame(self, frame_float32: np.ndarray) -> Tuple[np.ndarray, float]:
        """
        Process a single 480-sample float32 frame in-place or return a copy.
        Input should be 480 float32 samples (range approximately -32768 to 32767).
        Returns (denoised_frame_float32, speech_probability).
        """
        if len(frame_float32) != FRAME_SIZE:
            raise ValueError(f"Expected frame size {FRAME_SIZE}, got {len(frame_float32)}")

        # Ensure array is contiguous float32 in memory before pointer conversion
        if not frame_float32.flags.c_contiguous or frame_float32.dtype != np.float32:
            frame_float32 = np.ascontiguousarray(frame_float32, dtype=np.float32)

        st = self._state
        if not st:
            return frame_float32, 0.0

        ptr = frame_float32.ctypes.data_as(ctypes.POINTER(ctypes.c_float))
        speech_prob = _lib.rnnoise_process_frame(st, ptr, ptr)
        return frame_float32, float(speech_prob)

    def close(self):
        st = self._state
        self._state = None
        if st:
            try:
                _lib.rnnoise_destroy(st)
            except Exception:
                pass

    def __del__(self):
        self.close()
