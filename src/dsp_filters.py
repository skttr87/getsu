"""Biquad high-pass filter and the tilt FIR design used by the noise gate."""

import math
import numpy as np


BLOCK_FRAME_LEN = 480       # frame length the block HPF is precomputed for (10 ms at 48 kHz)
_ANTI_DENORMAL = 1e-15


def _biquad_response(coef, x_ext, ym2, ym1):
    """
    Direct Form I biquad with zero input history given by x_ext and output history (ym2, ym1).
    x_ext holds [x[-2], x[-1], x[0], ..., x[n-1]] (length n + 2). Returns y[0..n-1] as a list.
    """
    b0, b1, b2, a1, a2 = coef
    n = len(x_ext) - 2
    y = [ym2, ym1] + [0.0] * n
    for i in range(n):
        y[i + 2] = b0 * x_ext[i + 2] + b1 * x_ext[i + 1] + b2 * x_ext[i] - a1 * y[i + 1] - a2 * y[i]
    return y[2:]


class HighPassFilter:
    """
    2nd-order IIR Butterworth High-Pass Filter (Biquad).
    Cuts out sub-bass frequencies (< 80 Hz) like laptop cooling pad vibrations,
    desk thumps, and electrical hum before it reaches the AI model.

    For frames of BLOCK_FRAME_LEN samples the filter is evaluated as one block:
        y = T @ x + S @ [x1, x2, y1, y2]
    where T is the lower-triangular Toeplitz matrix of the biquad impulse response and S holds the
    responses to the four state values. T and S depend only on the coefficients and the frame length,
    so they are built once in the constructor. Other frame lengths use the per-sample loop.
    """

    def __init__(self, cutoff_hz: float = 80.0, sample_rate: float = 48000.0):
        self.cutoff_hz = cutoff_hz
        self.sample_rate = sample_rate
        self.reset()
        self._calculate_coefficients()
        self._block_n = 0
        self._prepare_block(BLOCK_FRAME_LEN)

    def _prepare_block(self, n: int):
        """Build T (n x n), S (n x 4) and the float64 scratch buffers for frame length n."""
        coef = (self.b0, self.b1, self.b2, self.a1, self.a2)
        zeros = [0.0] * n
        impulse = [0.0, 0.0, 1.0] + [0.0] * (n - 1)          # unit impulse at x[0]
        h = _biquad_response(coef, impulse, 0.0, 0.0)
        T = np.zeros((n, n), dtype=np.float64)
        for j in range(n):
            T[j:, j] = h[:n - j]                               # column j: impulse at sample j

        cols = [
            _biquad_response(coef, [0.0, 1.0] + zeros, 0.0, 0.0),   # x1 = x[-1] = 1
            _biquad_response(coef, [1.0, 0.0] + zeros, 0.0, 0.0),   # x2 = x[-2] = 1
            _biquad_response(coef, [0.0, 0.0] + zeros, 0.0, 1.0),   # y1 = y[-1] = 1
            _biquad_response(coef, [0.0, 0.0] + zeros, 1.0, 0.0),   # y2 = y[-2] = 1
        ]
        S = np.array(cols, dtype=np.float64).T
        self._T = T
        self._S = np.ascontiguousarray(S)
        # [T | S] applied to [x ; state] in one matrix-vector product
        self._Z = np.ascontiguousarray(np.concatenate([T, S], axis=1))
        self._z = np.zeros(n + 4, dtype=np.float64)
        self._zx = self._z[:n]
        self._zs = self._z[n:]
        self._y64 = np.zeros(n, dtype=np.float64)
        self._tmp64 = np.zeros(n, dtype=np.float64)
        self._mask = np.zeros(n, dtype=bool)
        self._block_n = n

    def _calculate_coefficients(self):
        w0 = 2.0 * math.pi * self.cutoff_hz / self.sample_rate
        cos_w0 = math.cos(w0)
        sin_w0 = math.sin(w0)
        q = 1.0 / math.sqrt(2.0)  # Butterworth Q = 0.7071
        alpha = sin_w0 / (2.0 * q)

        b0 = (1.0 + cos_w0) / 2.0
        b1 = -(1.0 + cos_w0)
        b2 = (1.0 + cos_w0) / 2.0
        a0 = 1.0 + alpha
        a1 = -2.0 * cos_w0
        a2 = 1.0 - alpha

        # Normalize by a0
        self.b0 = b0 / a0
        self.b1 = b1 / a0
        self.b2 = b2 / a0
        self.a1 = a1 / a0
        self.a2 = a2 / a0

    def reset_state(self):
        """Zero the filter memory only (keeps the output buffer; safe to call from the audio thread)."""
        self.x1 = 0.0
        self.x2 = 0.0
        self.y1 = 0.0
        self.y2 = 0.0

    def reset(self):
        self.reset_state()
        self._out_buffer: np.ndarray | None = None

    def process(self, frame: np.ndarray) -> np.ndarray:
        """
        Process a 1D float32 audio frame through the filter. Returns the internal output buffer
        (overwritten by the next call). Uses Direct Form I for numerical stability.
        """
        n = len(frame)
        if self._out_buffer is None or len(self._out_buffer) != n:
            self._out_buffer = np.empty(n, dtype=np.float32)

        if n == self._block_n and n >= 2:
            return self._process_block(frame)
        return self._process_loop(frame)

    def _process_block(self, frame) -> np.ndarray:
        """Block evaluation y = T x + S s with state update. Scratch buffers are reused (no per-frame allocation)."""
        out = self._out_buffer
        x = self._zx
        zs = self._zs
        y = self._y64
        np.copyto(x, frame)
        zs[0] = self.x1
        zs[1] = self.x2
        zs[2] = self.y1
        zs[3] = self.y2

        np.dot(self._Z, self._z, out=y)
        # Anti-denormal flush (same rule as the per-sample path)
        np.abs(y, out=self._tmp64)
        np.less(self._tmp64, _ANTI_DENORMAL, out=self._mask)
        np.copyto(y, 0.0, where=self._mask)

        np.copyto(out, y)
        self.x1 = float(x[-1])
        self.x2 = float(x[-2])
        self.y1 = float(y[-1])
        self.y2 = float(y[-2])
        return out

    def _process_loop(self, frame) -> np.ndarray:
        """Per-sample Direct Form I reference path, used for frame lengths other than BLOCK_FRAME_LEN."""
        n = len(frame)
        out = self._out_buffer
        b0, b1, b2 = self.b0, self.b1, self.b2
        a1, a2 = self.a1, self.a2
        x1, x2, y1, y2 = self.x1, self.x2, self.y1, self.y2

        frame_list = frame.tolist() if isinstance(frame, np.ndarray) else frame
        for i in range(n):
            x0 = frame_list[i]
            y0 = b0 * x0 + b1 * x1 + b2 * x2 - a1 * y1 - a2 * y2
            # Anti-denormal flush: prevent CPU microcode stalling on exponential decay silence
            if -_ANTI_DENORMAL < y0 < _ANTI_DENORMAL:
                y0 = 0.0
            out[i] = y0
            x2 = x1
            x1 = x0
            y2 = y1
            y1 = y0

        self.x1, self.x2, self.y1, self.y2 = x1, x2, y1, y2
        return out


TILT_FIR_TAPS = 9


def _design_tilt_fir(cutoff_hz: float, sample_rate: float, taps: int = TILT_FIR_TAPS) -> list:
    """
    Causal linear-phase low-pass (Hamming-windowed sinc, unity DC gain). Used only on the gate's closed path.
    A short FIR is used instead of a one-pole IIR because the block filter must stay vectorised: a one-pole
    needs a per-sample recursion, and a 480x480 matrix product would cost more than the whole budget allows.
    """
    fc = cutoff_hz / sample_rate
    m = np.arange(taps, dtype=np.float64) - (taps - 1) / 2.0
    h = 2.0 * fc * np.sinc(2.0 * fc * m) * (0.54 - 0.46 * np.cos(2.0 * np.pi * np.arange(taps) / (taps - 1)))
    h /= np.sum(h)
    return [float(v) for v in h]
