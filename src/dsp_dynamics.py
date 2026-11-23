"""Level metering, soft pre-clip and limiter, transient suppressor and speech leveler."""

import math
from typing import Tuple, Optional
import numpy as np


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
