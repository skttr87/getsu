"""
ctypes binding to the native speexdsp.dll (xiph SpeexDSP 1.2.1, float build with SSE).

Only the resampler and the preprocessor are exported by the DLL.

Sample convention: float32 arrays in the int16 scale used by the rest of the pipeline
(full scale is about -32768 .. 32767, the same convention as src/rnnoise.py).

Callback safety: every buffer, ctypes scalar and pointer argument is created in the
constructor. process() does not allocate numpy arrays or data buffers. The array
returned by process() is a view of a preallocated buffer and is valid until the next
process() call on the same object.
"""
import os
import sys
import ctypes
import numpy as np

_candidates = []
if getattr(sys, 'frozen', False):
    _candidates.append(os.path.join(getattr(sys, '_MEIPASS', ''), "src", "native", "speexdsp.dll"))
    _candidates.append(os.path.join(os.path.dirname(sys.executable), "src", "native", "speexdsp.dll"))
_candidates.append(os.path.join(os.path.dirname(__file__), "native", "speexdsp.dll"))
_candidates.append(os.path.abspath("src/native/speexdsp.dll"))

_dll_path = None
for _p in _candidates:
    if _p and os.path.exists(_p):
        _dll_path = _p
        break

if not _dll_path:
    raise FileNotFoundError("speexdsp.dll not found in search paths")

_lib = ctypes.CDLL(_dll_path)

_c_void_p = ctypes.c_void_p
_c_uint32_p = ctypes.POINTER(ctypes.c_uint32)
_c_int_p = ctypes.POINTER(ctypes.c_int)

_lib.speex_resampler_init.argtypes = [
    ctypes.c_uint32, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_int, _c_int_p,
]
_lib.speex_resampler_init.restype = _c_void_p

_lib.speex_resampler_destroy.argtypes = [_c_void_p]
_lib.speex_resampler_destroy.restype = None

_lib.speex_resampler_process_float.argtypes = [
    _c_void_p, ctypes.c_uint32,
    _c_void_p, _c_uint32_p,
    _c_void_p, _c_uint32_p,
]
_lib.speex_resampler_process_float.restype = ctypes.c_int

_lib.speex_resampler_reset_mem.argtypes = [_c_void_p]
_lib.speex_resampler_reset_mem.restype = ctypes.c_int

_lib.speex_resampler_skip_zeros.argtypes = [_c_void_p]
_lib.speex_resampler_skip_zeros.restype = ctypes.c_int

_lib.speex_preprocess_state_init.argtypes = [ctypes.c_int, ctypes.c_int]
_lib.speex_preprocess_state_init.restype = _c_void_p

_lib.speex_preprocess_state_destroy.argtypes = [_c_void_p]
_lib.speex_preprocess_state_destroy.restype = None

_lib.speex_preprocess_ctl.argtypes = [_c_void_p, ctypes.c_int, _c_int_p]
_lib.speex_preprocess_ctl.restype = ctypes.c_int

_lib.speex_preprocess_run.argtypes = [_c_void_p, _c_void_p]
_lib.speex_preprocess_run.restype = ctypes.c_int

# Request codes from include/speex/speex_preprocess.h
SPEEX_PREPROCESS_SET_DENOISE = 0
SPEEX_PREPROCESS_SET_AGC = 2
SPEEX_PREPROCESS_SET_NOISE_SUPPRESS = 18

RESAMPLER_QUALITY_DEFAULT = 5
_INT16_MIN = -32768.0
_INT16_MAX = 32767.0


class Resampler:
    """Streaming float32 resampler (one or more channels, planar layout).

    process() takes a float32 C-contiguous array of shape (n,) for mono or (channels, n)
    for multi-channel input, with n <= max_block_in. It returns a view of an internal
    output buffer with shape (m,) or (channels, m).
    """

    def __init__(self, in_rate, out_rate, channels=1, quality=RESAMPLER_QUALITY_DEFAULT, max_block_in=None):
        if in_rate <= 0 or out_rate <= 0:
            raise ValueError("sample rates must be positive")
        if channels < 1:
            raise ValueError("channels must be >= 1")
        if max_block_in is None:
            max_block_in = int(in_rate) // 100  # 10 ms
        self.in_rate = int(in_rate)
        self.out_rate = int(out_rate)
        self.channels = int(channels)
        self.quality = int(quality)
        self.max_block_in = int(max_block_in)
        # Output capacity: ceil(n * out / in) plus margin for the filter phase.
        self.max_block_out = -(-self.max_block_in * self.out_rate // self.in_rate) + 8

        err = ctypes.c_int(0)
        self._state = _lib.speex_resampler_init(
            self.channels, self.in_rate, self.out_rate, self.quality, ctypes.byref(err))
        if not self._state or err.value != 0:
            raise RuntimeError(f"speex_resampler_init failed (error {err.value})")

        if self.channels == 1:
            self._out = np.zeros(self.max_block_out, dtype=np.float32)
        else:
            self._out = np.zeros((self.channels, self.max_block_out), dtype=np.float32)
        # Addresses and ctypes scalars used by every call, created once.
        self._out_base = self._out.ctypes.data
        self._out_row_bytes = self.max_block_out * 4
        self._out_addr = [_c_void_p(self._out_base + c * self._out_row_bytes) for c in range(self.channels)]
        self._in_len = ctypes.c_uint32(0)
        self._out_len = ctypes.c_uint32(0)
        self._in_len_p = ctypes.pointer(self._in_len)
        self._out_len_p = ctypes.pointer(self._out_len)
        self._in_addr = _c_void_p(0)
        self._produced = 0

    def process(self, block):
        """Resample one block. Returns a view of the internal output buffer."""
        if block.dtype != np.float32 or not block.flags.c_contiguous:
            raise TypeError("process() needs a C-contiguous float32 array")
        if self.channels == 1:
            if block.ndim != 1:
                raise ValueError("mono input must be 1-D")
            n = block.shape[0]
        else:
            if block.ndim != 2 or block.shape[0] != self.channels:
                raise ValueError(f"multi-channel input must have shape ({self.channels}, n)")
            n = block.shape[1]
        if n > self.max_block_in:
            raise ValueError(f"block of {n} samples exceeds max_block_in={self.max_block_in}")

        st = self._state
        if not st:
            raise RuntimeError("Resampler is closed")
        in_base = block.ctypes.data
        in_row_bytes = n * 4
        out_len = self._out_len
        for c in range(self.channels):
            # C-contiguous planar layout: channel c starts c rows after the base address.
            self._in_addr.value = in_base + c * in_row_bytes
            self._in_len.value = n
            out_len.value = self.max_block_out
            err = _lib.speex_resampler_process_float(
                st, c, self._in_addr, self._in_len_p, self._out_addr[c], self._out_len_p)
            if err != 0:
                raise RuntimeError(f"speex_resampler_process_float error {err}")
            if self._in_len.value != n:
                raise RuntimeError("resampler did not consume the whole block")
            if c == 0:
                self._produced = out_len.value
            elif out_len.value != self._produced:
                raise RuntimeError("channels produced different lengths")
        m = self._produced
        if self.channels == 1:
            return self._out[:m]
        return self._out[:, :m]

    def reset(self):
        """Clear the filter memory (for example after a stream restart)."""
        if self._state:
            _lib.speex_resampler_reset_mem(self._state)

    def close(self):
        st = self._state
        self._state = None
        if st:
            try:
                _lib.speex_resampler_destroy(st)
            except Exception:
                pass

    def __del__(self):
        self.close()


class Preprocessor:
    """SpeexDSP preprocessor (noise suppression, optional AGC) on fixed-size frames.

    Conversion: input float32 is clipped to the int16 range and rounded to int16 before
    speex_preprocess_run(), which works on int16 frames in the float build. The int16
    result is copied back to a float32 output buffer, so values stay on the int16 scale.
    """

    def __init__(self, frame_size, rate):
        if frame_size <= 0:
            raise ValueError("frame_size must be positive")
        self.frame_size = int(frame_size)
        self.rate = int(rate)
        self._state = _lib.speex_preprocess_state_init(self.frame_size, self.rate)
        if not self._state:
            raise RuntimeError("speex_preprocess_state_init failed")
        self._i16 = np.zeros(self.frame_size, dtype=np.int16)
        self._tmp = np.zeros(self.frame_size, dtype=np.float32)
        self._out = np.zeros(self.frame_size, dtype=np.float32)
        self._i16_addr = _c_void_p(self._i16.ctypes.data)
        self._int_arg = ctypes.c_int(0)
        self._int_ptr = ctypes.pointer(self._int_arg)

    def _ctl_int(self, request, value):
        self._int_arg.value = int(value)
        return _lib.speex_preprocess_ctl(self._state, request, self._int_ptr)

    def set_denoise(self, enabled):
        self._ctl_int(SPEEX_PREPROCESS_SET_DENOISE, 1 if enabled else 0)

    def set_noise_suppress_db(self, db):
        """Noise suppression level in dB (negative values, for example -30)."""
        self._ctl_int(SPEEX_PREPROCESS_SET_NOISE_SUPPRESS, int(db))

    def set_agc(self, enabled):
        self._ctl_int(SPEEX_PREPROCESS_SET_AGC, 1 if enabled else 0)

    def process(self, frame):
        """Process one frame of frame_size float32 samples. Returns a view of the output buffer."""
        if frame.dtype != np.float32 or not frame.flags.c_contiguous or frame.shape != (self.frame_size,):
            raise ValueError(f"process() needs a C-contiguous float32 array of shape ({self.frame_size},)")
        st = self._state
        if not st:
            raise RuntimeError("Preprocessor is closed")
        tmp = self._tmp
        np.clip(frame, _INT16_MIN, _INT16_MAX, out=tmp)
        np.rint(tmp, out=tmp)
        np.copyto(self._i16, tmp, casting='unsafe')
        _lib.speex_preprocess_run(st, self._i16_addr)
        np.copyto(self._out, self._i16)
        return self._out

    def close(self):
        st = self._state
        self._state = None
        if st:
            try:
                _lib.speex_preprocess_state_destroy(st)
            except Exception:
                pass

    def __del__(self):
        self.close()
