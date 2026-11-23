"""Input downmix and dry-signal blend used around the processing chain."""

import math
import numpy as np


class InputDownmixer:
    """
    Stereo to mono downmix for the capture callback, with dead and polarity-inverted channel detection.

    mode "average": (L + R) / 2 for every frame.
    mode "auto" (default): tracks per-channel power and the L/R correlation over about 0.5 s and
      - uses the live channel when one channel is at least 30 dB below the other ("dead"),
      - uses the left channel when the channels are polarity-inverted (correlation < -0.5), since the
        average would cancel the signal,
      - otherwise averages.
    Hysteresis: a dead channel is released only when the level gap falls below 27 dB; inversion is
    released only when correlation rises above -0.3. Decisions are skipped while both channels are
    near silence (no change of state).

    The output buffer is allocated once and returned on every call (overwritten by the next call).
    process() only uses preallocated buffers and numpy calls with out= arguments.
    """

    MODE_AVERAGE = "average"
    MODE_AUTO = "auto"

    _STATE_AVG = 0
    _STATE_LEFT = 1       # right channel dead: use left
    _STATE_RIGHT = 2      # left channel dead: use right

    DEAD_ENTER_DB = 30.0
    DEAD_EXIT_DB = 27.0
    INVERT_ENTER_CORR = -0.5
    INVERT_EXIT_CORR = -0.3
    MIN_POWER = 1e-8      # sum of both channel powers below -80 dBFS: no decision
    _EPS = 1e-12

    def __init__(self, frame_len: int = 480, sample_rate: int = 48000, mode: str = MODE_AUTO, window_s: float = 0.5):
        if mode not in (self.MODE_AVERAGE, self.MODE_AUTO):
            raise ValueError(f"unknown stereo downmix mode: {mode!r}")
        self.mode = mode
        self.sample_rate = int(sample_rate)
        self.window_s = float(window_s)
        self._alloc(int(frame_len))
        self.reset()

    def _alloc(self, n: int):
        self.frame_len = n
        self._alpha = 1.0 - math.exp(-n / (self.window_s * self.sample_rate))
        self._l = np.zeros(n, dtype=np.float32)
        self._r = np.zeros(n, dtype=np.float32)
        self._out = np.zeros(n, dtype=np.float32)

    def reset(self):
        """Forget the running statistics and return to averaging."""
        self._pl = 0.0
        self._pr = 0.0
        self._lr = 0.0
        self._state = self._STATE_AVG
        self._inverted = False

    @property
    def state_name(self) -> str:
        """Human-readable current choice (for logs and tests)."""
        if self._state == self._STATE_LEFT:
            return "left"
        if self._state == self._STATE_RIGHT:
            return "right"
        if self._inverted:
            return "left (inverted)"
        return "average"

    def process(self, indata: np.ndarray) -> np.ndarray:
        """indata: (n, >=2) float32 block. Returns the mono float32 buffer (internal, reused)."""
        n = indata.shape[0]
        if n != self.frame_len:
            self._alloc(n)
        np.copyto(self._l, indata[:, 0])
        np.copyto(self._r, indata[:, 1])
        out = self._out
        if self.mode == self.MODE_AUTO:
            self._update_state()
        if self._state == self._STATE_LEFT or (self._state == self._STATE_AVG and self._inverted):
            np.copyto(out, self._l)
        elif self._state == self._STATE_RIGHT:
            np.copyto(out, self._r)
        else:
            np.add(self._l, self._r, out=out)
            np.multiply(out, 0.5, out=out)
        return out

    def _update_state(self):
        l, r = self._l, self._r
        a = self._alpha
        inv_n = 1.0 / self.frame_len
        pl_now = float(np.dot(l, l)) * inv_n
        pr_now = float(np.dot(r, r)) * inv_n
        lr_now = float(np.dot(l, r)) * inv_n
        self._pl += a * (pl_now - self._pl)
        self._pr += a * (pr_now - self._pr)
        self._lr += a * (lr_now - self._lr)

        if self._pl + self._pr < self.MIN_POWER:
            return

        ratio_db = 10.0 * math.log10((self._pr + self._EPS) / (self._pl + self._EPS))
        if self._state == self._STATE_LEFT and ratio_db > -self.DEAD_EXIT_DB:
            self._state = self._STATE_AVG
        elif self._state == self._STATE_RIGHT and ratio_db < self.DEAD_EXIT_DB:
            self._state = self._STATE_AVG
        if self._state == self._STATE_AVG:
            if ratio_db <= -self.DEAD_ENTER_DB:
                self._state = self._STATE_LEFT
            elif ratio_db >= self.DEAD_ENTER_DB:
                self._state = self._STATE_RIGHT

        corr = self._lr / math.sqrt(self._pl * self._pr + self._EPS)
        if not self._inverted and corr < self.INVERT_ENTER_CORR:
            self._inverted = True
        elif self._inverted and corr > self.INVERT_EXIT_CORR:
            self._inverted = False


class DryBlend:
    """
    Optional RNNoise dry blend. Keeps a copy of the pre-RNNoise frame (after high-pass and pre-clip),
    delayed by the pipeline latency (delay_frames x 10 ms), and mixes it back at mix_db while the gate is open.
    The delay matches the measured RNNoise + lookahead latency (about 1440 samples at 48 kHz), so the dry
    path lines up with the processed path and does not comb-filter.
    Storage is a preallocated ring of frames; push() performs two copies and no allocation.
    """

    DEFAULT_DELAY_FRAMES = 3

    def __init__(self, mix_db: float = -24.0, delay_frames: int = DEFAULT_DELAY_FRAMES, frame_len: int = 480):
        self.mix_db = float(mix_db)
        self.gain = 10.0 ** (self.mix_db / 20.0)
        self._alloc(int(frame_len), int(delay_frames))

    def _alloc(self, frame_len: int, delay_frames: int):
        self._ring = np.zeros((delay_frames, frame_len), dtype=np.float32)
        self._idx = 0
        self._out = np.zeros(frame_len, dtype=np.float32)
        self._scaled = np.zeros(frame_len, dtype=np.float32)

    def reset(self):
        self._ring.fill(0.0)
        self._idx = 0

    def push(self, frame: np.ndarray) -> np.ndarray:
        """Store frame and return the frame from delay_frames calls ago (internal buffer, reused)."""
        if frame.shape[0] != self._out.shape[0]:
            self._alloc(frame.shape[0], self._ring.shape[0])
        slot = self._ring[self._idx]
        np.copyto(self._out, slot)
        np.copyto(slot, frame)
        self._idx = (self._idx + 1) % self._ring.shape[0]
        return self._out

    def mix_into(self, frame: np.ndarray, dry: np.ndarray, gate_gain: float, gate_floor: float):
        """frame += dry * gain * openness, where openness is 0 at the gate floor and 1 when fully open."""
        if gate_floor >= 1.0:
            return
        openness = min(1.0, max(0.0, (gate_gain - gate_floor) / (1.0 - gate_floor)))
        w = self.gain * openness
        if w <= 0.0:
            return
        np.multiply(dry, w, out=self._scaled)
        frame += self._scaled
