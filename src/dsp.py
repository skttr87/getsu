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


def calculate_levels(frame: np.ndarray):
    """Calculate Peak and RMS levels in dBFS using BLAS dot product."""
    if len(frame) == 0:
        return -100.0, -100.0
    # max(|x|) without an absolute-value temporary
    peak = max(float(frame.max()), -float(frame.min()))
    rms = float(np.sqrt(np.dot(frame, frame) / len(frame)))

    # Convert to dBFS (reference 1.0 for normalized float32; 32767 only if PCM range > 100)
    ref = 32767.0 if peak > 100.0 else 1.0
    peak_db = 20.0 * math.log10(max(peak / ref, 1e-5))
    rms_db = 20.0 * math.log10(max(rms / ref, 1e-5))
    return peak_db, rms_db


PRECLIP_KNEE_DEFAULT = 0.95   # a lower knee (0.75) saturates speech peaks before RNNoise


def soft_preclip(frame: np.ndarray, knee: float = PRECLIP_KNEE_DEFAULT) -> np.ndarray:
    """
    Smooths hard ADC rail flat-topping to protect RNNoise Bark-band features.
    Uses hyperbolic tangent saturation above the knee, so samples approach 1.0 smoothly.
    A knee of 1.0 or more disables the stage (the caller's final clip still applies).
    """
    if knee >= 1.0:
        return frame
    if frame.size == 0 or (frame.max() <= knee and frame.min() >= -knee):
        return frame                      # common case: nothing above the knee, no temporaries
    mask = np.abs(frame) > knee
    if np.any(mask):
        excess = np.abs(frame[mask]) - knee
        frame[mask] = np.sign(frame[mask]) * (knee + (1.0 - knee) * np.tanh(excess / (1.0 - knee)))
    return frame


def soft_limit(x: np.ndarray, threshold: float = 0.85, out: Optional[np.ndarray] = None) -> np.ndarray:
    """
    Smooth transparent soft-saturation limiter.
    Passes audio transparently (1:1) when |x| <= threshold.
    Softens loud screaming/laughter peaks using tanh saturation, eliminating hard digital clipping.
    With out=x the limiting is done in place; otherwise a copy is returned when samples are limited.
    """
    if x.size == 0 or (x.max() <= threshold and x.min() >= -threshold):
        if out is not None and out is not x:
            np.copyto(out, x)
            return out
        return x
    abs_x = np.abs(x)
    over = abs_x > threshold
    if not np.any(over):
        return x
    if out is None:
        out = x.copy()
    elif out is not x:
        np.copyto(out, x)
    scale = 1.0 - threshold
    out[over] = np.sign(x[over]) * (threshold + scale * np.tanh((abs_x[over] - threshold) / scale))
    return out


TS_RADIUS = 24                       # 0.5 ms each side = 1.0 ms dip at 48 kHz
TS_WINDOW_MAX = 2 * TS_RADIUS + 1
TS_SKIP_SPEECH_PROB = 0.20           # below this speech probability the stage is skipped (see TransientSuppressor)


def _build_bell_table() -> np.ndarray:
    """
    Row L holds sin^2 over a window of L samples (linspace 0..pi, float32), zero-padded to TS_WINDOW_MAX.
    Windows at a frame edge are shorter than 49 samples and are stretched over their own length, so each
    length needs its own row.
    """
    table = np.zeros((TS_WINDOW_MAX + 1, TS_WINDOW_MAX), dtype=np.float32)
    for length in range(1, TS_WINDOW_MAX + 1):
        t = np.linspace(0.0, np.pi, length, dtype=np.float32)
        table[length, :length] = np.sin(t) ** 2
    return table


_TS_BELL = _build_bell_table()
_TS_OFFSETS = np.arange(TS_WINDOW_MAX, dtype=np.int64)


class TransientSuppressor:
    """
    Zero-latency In-Speech Transient De-Clicker.
    Attenuates sharp mechanical keyboard switch clicks (fast rise time, high HF energy)
    and impulsive desk thumps occurring during active speech by 6-10 dB,
    using smooth S-curve / Hann envelope windowing to eliminate harmonic step clicks.

    skip_when_no_speech (default True): when speech_prob is below TS_SKIP_SPEECH_PROB (0.20) the
    stage returns the frame unchanged. Its state (previous sample) is still updated. Set it to False
    to run the stage on every frame.
    """

    def __init__(
        self,
        threshold_crest: float = 4.5,
        max_attenuation_db: float = 8.0,
        sample_rate: int = 48000,
        skip_when_no_speech: bool = True,
    ):
        self.threshold_crest = threshold_crest
        self.max_attenuation_db = max_attenuation_db
        self.sample_rate = sample_rate
        self.skip_when_no_speech = skip_when_no_speech
        self.min_gain = float(10.0 ** (-max_attenuation_db / 20.0))
        self._prev_sample = 0.0
        self._buf_n = 0
        self._buf_dtype = None
        self._diff: Optional[np.ndarray] = None

    def reset(self):
        """Reset internal filter memory."""
        self._prev_sample = 0.0

    def _ensure_buffers(self, n: int, dtype):
        if self._buf_n != n or self._buf_dtype != dtype:
            self._diff = np.zeros(n, dtype=dtype)
            self._sq = np.zeros(n, dtype=dtype)
            self._crest = np.zeros(n, dtype=dtype)
            self._mask = np.zeros(n, dtype=bool)
            self._gain = np.ones(n, dtype=dtype)
            self._buf_n = n
            self._buf_dtype = dtype

    def process(self, frame: np.ndarray, speech_prob: float = 1.0) -> np.ndarray:
        """
        Detects high-frequency impulsive spikes and smoothly suppresses them.
        The result is written into frame and frame is returned.
        """
        n = len(frame)
        if n == 0:
            return frame

        if self.skip_when_no_speech and speech_prob < TS_SKIP_SPEECH_PROB:
            self._prev_sample = float(frame[-1])
            return frame

        self._ensure_buffers(n, frame.dtype)
        diff = self._diff
        # High-frequency derivative across frame (same values as np.diff(frame, prepend=prev))
        diff[0] = frame[0] - frame.dtype.type(self._prev_sample)
        np.subtract(frame[1:], frame[:-1], out=diff[1:])
        self._prev_sample = float(frame[-1])

        # Energy of high-frequency differential
        sq = self._sq
        np.square(diff, out=sq)
        rms_diff = float(np.sqrt(np.mean(sq)))
        if rms_diff < 1e-4:
            return frame

        # Instantaneous HF crest factor
        crest = self._crest
        np.abs(diff, out=crest)
        crest /= (rms_diff + 1e-6)
        mask = self._mask
        np.greater_equal(crest, self.threshold_crest, out=mask)
        spikes = np.flatnonzero(mask)
        if spikes.size == 0:
            return frame

        # Per-spike attenuation and window placement, all spikes at once
        peak = crest[spikes].astype(np.float64)
        att_db = np.minimum(self.max_attenuation_db, 6.0 + (peak - self.threshold_crest) * 0.8)
        spike_gain = 10.0 ** (-att_db / 20.0)
        i_start = np.maximum(0, spikes - TS_RADIUS)
        i_end = np.minimum(n, spikes + TS_RADIUS + 1)
        lengths = i_end - i_start

        bell = _TS_BELL[lengths]                                   # (spikes, 49), zero padded
        local = 1.0 - (1.0 - spike_gain)[:, None] * bell           # padding entries stay 1.0
        valid = _TS_OFFSETS[None, :] < lengths[:, None]
        positions = i_start[:, None] + _TS_OFFSETS[None, :]

        gain_env = self._gain
        gain_env.fill(1.0)
        np.minimum.at(gain_env, positions[valid], local[valid].astype(gain_env.dtype))

        np.multiply(frame, gain_env, out=frame)
        return frame


class SpeechLeveler:
    """
    Dynamic Target Speech Leveler for quiet microphones (Bluetooth TWS, USB headsets, laptop arrays).
    - Only tracks voiced speech RMS when speech_prob >= voicing_threshold (0.60).
    - Freezes level estimation during silence, breathing, or background noise (zero noise pumping).
    - Normalizes speech toward target_rms_db (-24.0 dBFS) with max boost clamp (+9.0 dB).
    - De-stacks manual mic boost: shifts target level by mic_boost_db to protect headroom.
    - Asymmetric slew rate: fast recovery downward to avoid clipping, smooth gentle upward ramp.
    """

    def __init__(
        self,
        target_rms_db: float = -24.0,
        max_boost_db: float = 9.0,
        voicing_threshold: float = 0.60,
        frame_ms: float = 10.0,
        attack_ms: float = 40.0,
        decay_ms: float = 500.0,
        max_slew_up_db_per_sec: float = 5.0,
    ):
        self.target_rms_db = target_rms_db
        self.max_boost_mult = 10.0 ** (max_boost_db / 20.0)
        self.voicing_threshold = voicing_threshold
        self.frame_ms = frame_ms

        self.alpha_attack = math.exp(-frame_ms / attack_ms)
        self.alpha_decay = math.exp(-frame_ms / decay_ms)
        self.max_slew_up = (max_slew_up_db_per_sec * (frame_ms / 1000.0))

        # Initial state: nominal 1.0x gain, running voiced RMS seeded at target
        self.voiced_rms_db: float = target_rms_db
        self.current_gain: float = 1.0

    def reset(self):
        """Reset running leveler estimate back to default target level."""
        self.voiced_rms_db = self.target_rms_db
        self.current_gain = 1.0

    def process(
        self,
        frame: np.ndarray,
        speech_prob: float,
        input_rms_db: float,
        mic_boost_db: float = 0.0,
    ) -> Tuple[np.ndarray, float]:
        """
        Calculates and applies leveler gain on a 10ms audio frame.
        Dynamically adjusts effective target level by mic_boost_db to prevent gain compounding.
        Returns: (processed_frame, current_leveler_gain)
        """
        # Voiced speech gating: freeze estimation during silence/noise
        if speech_prob >= self.voicing_threshold and input_rms_db > -60.0:
            if input_rms_db > self.voiced_rms_db:
                self.voiced_rms_db = (
                    self.alpha_attack * self.voiced_rms_db + (1.0 - self.alpha_attack) * input_rms_db
                )
            else:
                self.voiced_rms_db = (
                    self.alpha_decay * self.voiced_rms_db + (1.0 - self.alpha_decay) * input_rms_db
                )

        # De-stack target by user manual mic boost
        effective_target_db = self.target_rms_db - mic_boost_db
        error_db = effective_target_db - self.voiced_rms_db
        desired_gain_db = max(0.0, min(20.0 * math.log10(self.max_boost_mult), error_db))
        desired_gain = 10.0 ** (desired_gain_db / 20.0)

        # Asymmetric slew rate limiting: rapid downward, smooth upward
        if desired_gain < self.current_gain:
            # Immediate downward step to protect headroom
            self.current_gain = desired_gain
        else:
            current_gain_db = 20.0 * math.log10(max(1e-4, self.current_gain))
            current_gain_db = min(desired_gain_db, current_gain_db + self.max_slew_up)
            self.current_gain = 10.0 ** (current_gain_db / 20.0)

        if self.current_gain != 1.0:
            np.multiply(frame, self.current_gain, out=frame)
            return frame, self.current_gain
        return frame, 1.0


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
