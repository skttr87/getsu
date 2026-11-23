"""
Digital Signal Processing (DSP) Module for Getsu.
Features:
- Biquad High-Pass Filter (80Hz Butterworth rumble filter for cooling pads / desk bumps)
- Adaptive Soft-Knee Noise Gate with Hangover (eliminates cooling pad fan hiss between words)
- RMS / Peak level metering
- Smooth Peak Soft-Limiter

v1.2.6 gate changes:
- Voiced-burst flutter detection (tracks voiced frames < 150 ms inside bursts).
- AdaptiveNoiseGate.reset() / recalibrate() for clean mute and device-change handling.
- Speech-guarded startup ambient profiler (median of non-speech frames).
- Guarded dual-rate noise-floor tracking with a per-frame rise cap (0.5 dB) and lower clamp (-75 dBFS).
"""
import bisect
import math
import threading
from collections import deque
from typing import Tuple, Optional, Any
import numpy as np

from src.dsp_filters import (
    BLOCK_FRAME_LEN, _ANTI_DENORMAL, _biquad_response, HighPassFilter, TILT_FIR_TAPS,
    _design_tilt_fir
)
from src.dsp_gate import (_T_480, _S_CURVE_F64, _S_CURVE_TABLE_480, AdaptiveNoiseGate)
from src.dsp_dynamics import (
    calculate_levels, PRECLIP_KNEE_DEFAULT, soft_preclip, soft_limit, TS_RADIUS, TS_WINDOW_MAX,
    TS_SKIP_SPEECH_PROB, _build_bell_table, _TS_BELL, _TS_OFFSETS, TransientSuppressor,
    SpeechLeveler
)


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


_tls = threading.local()


def _scale_scratch(n: int) -> np.ndarray:
    """Per-thread float32 scratch for the RNNoise input scaling (the live engine and the voice test run on different threads)."""
    buf = getattr(_tls, "scale", None)
    if buf is None or buf.shape[0] != n:
        buf = np.empty(n, dtype=np.float32)
        _tls.scale = buf
    return buf


def process_mono_frame(
    frame_mono: np.ndarray,
    rnnoise: Optional[Any],
    hpf: Optional[HighPassFilter],
    gate: AdaptiveNoiseGate,
    total_gain: float = 1.0,
    denoise_enabled: bool = True,
    leveler: Optional[SpeechLeveler] = None,
    transient_suppressor: Optional[TransientSuppressor] = None,
    mic_boost_db: float = 0.0,
    preclip_knee: float = PRECLIP_KNEE_DEFAULT,
    dry_blend: Optional[DryBlend] = None,
) -> Tuple[np.ndarray, float, float, float]:
    """
    Authoritative single-frame (10ms) DSP processing pipeline shared by both the live stream
    engine and the GUI Voice Test thread.

    Pipeline:
      1. High-Pass Filter (80Hz rumble cut)
      2. Dual-Stage Metering & Headroom Pre-clip (soft-knee ADC plosive protection)
      3. RNNoise Neural Suppression (or energy-derived probability in bypass)
      4. In-Speech Transient De-Clicking (mechanical keyboard / desk thump clamp)
      5. Optional SpeechLeveler (de-stacked voiced auto-gain)
      6. Post-RNNoise Gain Staging
      7. AdaptiveNoiseGate (zero-allocation downward expander)
      8. Transparent Soft-Limiter

    Returns:
      (processed_frame, speech_prob, input_peak_db, input_rms_db)
    """
    # 1. High-Pass Filter
    if hpf is not None:
        frame_mono = hpf.process(frame_mono)

    # 2. Dual-Stage Metering & Headroom Pre-clip
    input_peak_db, input_rms_db = calculate_levels(frame_mono)
    frame_mono = soft_preclip(frame_mono, knee=preclip_knee)
    np.clip(frame_mono, -1.0, 1.0, out=frame_mono)
    dry_delayed = dry_blend.push(frame_mono) if dry_blend is not None else None

    # 3. RNNoise Neural Core (scaled into a per-thread scratch buffer, result written back in place)
    speech_prob = 0.0
    if denoise_enabled and rnnoise is not None:
        frame_rn = np.multiply(frame_mono, 32767.0, out=_scale_scratch(len(frame_mono)))
        frame_rn, speech_prob = rnnoise.process_frame(frame_rn)
        np.divide(frame_rn, 32767.0, out=frame_mono)
    else:
        speech_prob = min(1.0, max(0.0, (input_rms_db + 45.0) / 20.0))

    # 4. In-Speech Transient Suppression (in place)
    if transient_suppressor is not None:
        frame_mono = transient_suppressor.process(frame_mono, speech_prob)

    # 5. Dynamic Speech Leveler (if enabled, de-stacking manual mic boost; in place)
    if leveler is not None:
        frame_mono, _ = leveler.process(frame_mono, speech_prob, input_rms_db, mic_boost_db=mic_boost_db)

    # 6. Gain Staging
    if total_gain != 1.0:
        frame_mono *= total_gain

    # 7. Soft-Knee Adaptive Noise Gate
    frame_mono, gate_gain = gate.process(frame_mono, speech_prob, input_rms_db=input_rms_db, in_place=True)

    # 7b. Optional RNNoise dry blend (only while the gate is open; only when RNNoise produced the wet path)
    if dry_delayed is not None and denoise_enabled and rnnoise is not None:
        dry_blend.mix_into(frame_mono, dry_delayed, gate_gain, gate.floor_gain)

    # 8. Soft Saturation Limiter (in place)
    frame_mono = soft_limit(frame_mono, threshold=0.85, out=frame_mono)

    return frame_mono, speech_prob, input_peak_db, input_rms_db
