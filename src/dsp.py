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


# Precomputed 480-sample Septic Smootherstep (Perlin C3) curve for zero-jerk, zero-allocation slew ramping
_T_480 = np.linspace(0.0, 1.0, 480, dtype=np.float64)
_S_CURVE_F64 = _T_480**4 * (35.0 + _T_480 * (-84.0 + _T_480 * (70.0 - 20.0 * _T_480)))
_S_CURVE_TABLE_480 = np.maximum.accumulate(np.clip(_S_CURVE_F64, 0.0, 1.0).astype(np.float32))


class AdaptiveNoiseGate:
    """
    Adaptive Dual-Threshold (Hysteresis) Soft-Knee Noise Gate with Dynamic Onset Detection.

    Defaults (v1.2.6):
    - Open threshold 0.70: standard high-confidence RNNoise speech probability.
    - Onset trigger: opens early at speech_prob >= 0.35 when the input level is at least
      onset_snr_db (10 dB) above the tracked ambient noise floor. Inhibited during the
      post-close cooldown.
    - Close threshold 0.52 + hangover 320 ms: keeps the gate open across soft word endings,
      breaths and natural pauses.
    - floor_gain: gain applied while closed. The engine passes 0.06, i.e. 20*log10(0.06) = -24.4 dB
      of attenuation (not silence), which keeps wireless DAC amplifiers energized.
      floor_gain=0.0 gives true digital silence.
    - Exponential attack (attack_ms) / decay (decay_ms) with a septic smootherstep (C3) ramp
      inside each frame to avoid clicks.
    - Optional 1-frame (10 ms) lookahead so the gate decision leads the audio.

    Flutter protection:
    - A "burst" is one open period of the gate. Its length is the number of VOICED frames
      (speech_prob >= close_threshold), not the gate-open time, because the hangover alone keeps the
      gate open for >= hangover_ms.
    - If >= 3 bursts shorter than 150 ms of voiced audio occur within 3.0 s, the cooldown that blocks
      the onset trigger is extended from 150 ms to 300 ms. It reverts by itself once 3.0 s pass
      without new flutter events. (The cooldown only blocks the onset path; probabilities
      >= threshold can still reopen the gate.)

    Noise floor:
    - Startup profiler: median input level of non-speech frames (speech_prob < 0.20) over the first
      ~0.5 s, clamped to [-75, -36] dBFS. Falls back to the default seed when the timeout (1.5 s)
      passes with too few ambient frames. Call recalibrate() after a device or room change.
    - Tracking while closed: fast fall (0.15), slow rise (0.02), and a faster rise (0.10) only during
      the post-close cooldown with speech_prob < 0.20. Every rise is capped at 0.5 dB per frame so
      impulsive non-speech sounds (keyboard, clicks, breaths) cannot yank the floor upward, and the
      floor never falls below -75 dBFS.

    cold_start_gain is kept for backward compatibility; it only has an effect when floor_gain == 0.
    """

    FLOOR_INIT_DB = -60.0
    FLOOR_MIN_DB = -75.0
    FLOOR_MAX_DB = -36.0
    AMBIENT_PROB_MAX = 0.20
    FLOOR_FALL_RATE = 0.15
    FLOOR_SLOW_RISE_RATE = 0.02
    FLOOR_FAST_RISE_RATE = 0.10
    FLOOR_RISE_CAP_DB = 0.5

    CALIBRATION_TARGET = 50       # frames of ambient audio (~500 ms)
    CALIBRATION_TIMEOUT = 150     # frames (~1.5 s)
    CALIBRATION_MIN_SAMPLES = 10

    FLUTTER_BURST_FRAMES = 15     # < 150 ms voiced audio counts as a flutter blip
    FLUTTER_EVENT_THRESHOLD = 3
    FLUTTER_WINDOW_FRAMES = 300   # 3.0 s

    _IDLE_FRAMES = 10**6

    def __init__(
        self,
        threshold: float = 0.70,
        close_threshold: float = 0.52,
        hangover_ms: float = 320.0,
        decay_ms: float = 80.0,
        attack_ms: float = 28.0,
        frame_ms: float = 10.0,
        onset_threshold: float = 0.35,
        onset_snr_db: float = 10.0,
        lookahead: bool = False,
        floor_gain: float = 0.0,
        cooldown_ms: float = 150.0,
        flutter_cooldown_ms: float = 300.0,
        calibrate_startup: bool = True,
        sustain_snr_db: float = 6.0,
        sustain_threshold: float = 0.25,
    ):
        self.threshold = threshold
        self.close_threshold = close_threshold
        self.hangover_frames = max(1, int(hangover_ms / frame_ms))
        decay_factor = frame_ms / decay_ms
        self.decay_rate = math.exp(-decay_factor)
        attack_factor = frame_ms / attack_ms
        self.attack_rate = 1.0 - math.exp(-attack_factor)

        self.onset_threshold = onset_threshold
        self.onset_snr_db = onset_snr_db
        self.sustain_snr_db = sustain_snr_db
        self.sustain_threshold = sustain_threshold
        self.noise_floor_db = self.FLOOR_INIT_DB
        self.lookahead = lookahead
        self.floor_gain = floor_gain

        self.cooldown_frames = max(1, int(cooldown_ms / frame_ms))
        self.max_cooldown_frames = max(self.cooldown_frames, int(flutter_cooldown_ms / frame_ms))
        self.frames_since_close = self._IDLE_FRAMES

        self.is_open = False
        self.current_gain = floor_gain
        self.frames_since_speech = self.hangover_frames + 10
        self._zero_buffer: np.ndarray | None = None
        self._delay_buffer: np.ndarray | None = None
        self._work_buffer: np.ndarray | None = None
        self._ramp_buf: np.ndarray | None = None

        # Flutter state
        self._frame_idx = 0
        self._burst_voiced = 0
        self._frames_since_voiced = 1000
        self._flutter_events: deque = deque()

        # Startup profiler state
        self._calibrate_startup = calibrate_startup
        self._calibrated = not calibrate_startup
        self._cal_elapsed = 0
        self._cal_buffer: list = []

        # Near-field voiced speech anchor state (eliminates distant cubicle chatter)
        self.user_anchor_db: float = -24.0
        self.nearfield_margin_db: float = 14.0
        self._voiced_anchor_buffer: deque = deque(maxlen=150)
        self._anchor_sorted: list = []   # same values as the deque, kept sorted for the 90th percentile
        self._frames_since_nearfield: int = 1000

    # ------------------------------------------------------------------ control

    def reset(self):
        """
        Return the gate to a clean closed state (call once on the mute transition).
        Keeps the learned noise floor. Zeroes the lookahead buffer so unmute cannot
        emit stale audio, and clears flutter history.
        """
        self.is_open = False
        self.current_gain = self.floor_gain
        self.frames_since_speech = self.hangover_frames + 10
        self.frames_since_close = self._IDLE_FRAMES
        self._burst_voiced = 0
        self._frames_since_voiced = 1000
        self._frames_since_nearfield = 1000
        self.user_anchor_db = -24.0
        self._clear_anchor_buffer()
        self._flutter_events.clear()
        if self._delay_buffer is not None:
            self._delay_buffer.fill(0.0)

    def recalibrate(self):
        """Restart the startup ambient profiler (call on stream start / device change)."""
        self.noise_floor_db = self.FLOOR_INIT_DB
        self._cal_elapsed = 0
        self._cal_buffer = []
        self._calibrated = not self._calibrate_startup
        self.user_anchor_db = -24.0
        self._clear_anchor_buffer()
        self._frames_since_nearfield = 1000

    # ------------------------------------------------------------------ helpers

    def _clear_anchor_buffer(self):
        self._voiced_anchor_buffer.clear()
        self._anchor_sorted.clear()

    def _anchor_push(self, value: float):
        """Append a voiced level to the 150-frame anchor window, keeping the sorted copy in step."""
        buf = self._voiced_anchor_buffer
        if len(buf) == buf.maxlen:
            oldest = buf[0]
            del self._anchor_sorted[bisect.bisect_left(self._anchor_sorted, oldest)]
        buf.append(value)
        bisect.insort(self._anchor_sorted, value)

    def _anchor_percentile90(self) -> float:
        """90th percentile with linear interpolation (same definition as np.percentile default)."""
        srt = self._anchor_sorted
        n = len(srt)
        pos = 0.9 * (n - 1)
        lo = int(pos)
        if lo + 1 >= n:
            return float(srt[lo])
        frac = pos - lo
        return float(srt[lo] + (srt[lo + 1] - srt[lo]) * frac)

    def _ramp_scratch(self, n: int, dtype) -> np.ndarray:
        buf = self._ramp_buf
        if buf is None or buf.shape[0] != n or buf.dtype != dtype:
            buf = np.empty(n, dtype=dtype)
            self._ramp_buf = buf
        return buf

    def _current_cooldown_frames(self) -> int:
        ev = self._flutter_events
        while ev and self._frame_idx - ev[0] > self.FLUTTER_WINDOW_FRAMES:
            ev.popleft()
        if len(ev) >= self.FLUTTER_EVENT_THRESHOLD:
            return self.max_cooldown_frames
        return self.cooldown_frames

    def _finish_calibration(self, value_db: float):
        self.noise_floor_db = max(self.FLOOR_MIN_DB, min(self.FLOOR_MAX_DB, value_db))
        self._calibrated = True
        self._cal_buffer = []

    def _calibrate_step(self, input_rms_db, speech_prob: float):
        self._cal_elapsed += 1
        if (
            input_rms_db is not None
            and -100.0 < input_rms_db < -20.0
            and speech_prob < self.AMBIENT_PROB_MAX
        ):
            self._cal_buffer.append(float(input_rms_db))

        if len(self._cal_buffer) >= self.CALIBRATION_TARGET:
            self._finish_calibration(float(np.median(self._cal_buffer)))
        elif self._cal_elapsed >= self.CALIBRATION_TIMEOUT:
            if len(self._cal_buffer) >= self.CALIBRATION_MIN_SAMPLES:
                self._finish_calibration(float(np.percentile(self._cal_buffer, 25)))
            else:
                # Not enough clean ambient frames: keep the default seed
                self._calibrated = True
                self._cal_buffer = []

    def _track_floor(self, input_rms_db, speech_prob: float, in_cooldown: bool):
        if input_rms_db is None or not (-100.0 < input_rms_db < -20.0):
            return
        diff = input_rms_db - self.noise_floor_db
        if diff < 0.0:
            self.noise_floor_db = max(
                self.FLOOR_MIN_DB, self.noise_floor_db + diff * self.FLOOR_FALL_RATE
            )
        else:
            if in_cooldown and speech_prob < self.AMBIENT_PROB_MAX:
                rate = self.FLOOR_FAST_RISE_RATE
            else:
                rate = self.FLOOR_SLOW_RISE_RATE
            self.noise_floor_db += min(diff * rate, self.FLOOR_RISE_CAP_DB)

    # ------------------------------------------------------------------ main

    def process(
        self,
        frame: np.ndarray,
        speech_prob: float,
        input_rms_db: float | None = None,
        in_place: bool = False
    ) -> tuple[np.ndarray, float]:
        """
        Applies hysteresis soft-knee gating based on RNNoise speech probability and dynamic onset SNR.
        Sample-accurate S-curve ramping eliminates clicks, pops, and harsh transients.
        Optional 1-frame (10ms) pre-roll lookahead buffer enables natural, unclipped voice onset.
        Returns the gated audio frame and current applied gain.
        """
        self._frame_idx += 1
        if not self._calibrated:
            self._calibrate_step(input_rms_db, speech_prob)

        if self.lookahead:
            n = len(frame)
            if self._delay_buffer is None or len(self._delay_buffer) != n:
                self._delay_buffer = np.zeros(n, dtype=frame.dtype)
            if self._work_buffer is None or len(self._work_buffer) != n:
                self._work_buffer = np.zeros(n, dtype=frame.dtype)

            # Pre-roll: copy delayed frame into work buffer
            np.copyto(self._work_buffer, self._delay_buffer)
            # Store current incoming frame into delay buffer for next cycle
            np.copyto(self._delay_buffer, frame)
            target_frame = self._work_buffer
        else:
            target_frame = frame if in_place else frame.copy()

        start_gain = self.current_gain

        # 1. Voiced Speech Anchor Tracking (adapts to primary near-field user speaking level)
        if input_rms_db is not None and speech_prob >= 0.80:
            snr = input_rms_db - self.noise_floor_db
            if snr >= 15.0:
                self._anchor_push(input_rms_db)
                if len(self._voiced_anchor_buffer) >= 10:
                    self.user_anchor_db = min(-12.0, max(-36.0, self._anchor_percentile90()))

        # 2. Near-Field Qualification
        is_nearfield = (input_rms_db is None) or (input_rms_db >= (self.user_anchor_db - self.nearfield_margin_db))
        if is_nearfield and speech_prob >= self.close_threshold:
            self._frames_since_nearfield = 0
        else:
            self._frames_since_nearfield += 1

        if not self.is_open:
            self.frames_since_close += 1
            in_cooldown = self.frames_since_close <= self._current_cooldown_frames()

            # Ambient noise floor tracking during silence (paused until startup calibration completes)
            if self._calibrated:
                self._track_floor(input_rms_db, speech_prob, in_cooldown)

            # Dual-Key Trigger evaluation:
            # 1. Standard high-confidence VAD trigger
            # 2. Fast onset trigger: energy rise >= onset_snr_db above noise floor with early speech cue
            is_onset = False
            if input_rms_db is not None:
                snr = input_rms_db - self.noise_floor_db
                can_trigger_onset = (not in_cooldown) or (snr >= self.onset_snr_db + 8.0 and speech_prob >= 0.55)
                if can_trigger_onset and snr >= self.onset_snr_db and speech_prob >= self.onset_threshold:
                    is_onset = True

            # Near-field qualifying check:
            # Guarded by nearfield qualification or warm hangover buffer to protect soft phrase beginnings
            is_qualified = is_nearfield or (self._frames_since_nearfield <= 40) or (len(self._voiced_anchor_buffer) < 10 and (input_rms_db is None or input_rms_db >= -38.0))

            if (speech_prob >= self.threshold or is_onset) and is_qualified:
                self.is_open = True
                self.frames_since_speech = 0
                self._frames_since_voiced = 0 if speech_prob >= self.close_threshold else 1000
                self._burst_voiced = 1 if speech_prob >= self.close_threshold else 0
                target_gain = 1.0
            else:
                target_gain = self.floor_gain
        else:
            # Mic is active: stay open during trailing word endings and unvoiced consonants
            # Dual-condition sustain:
            # 1. High RNNoise speech probability (voiced phonemes)
            # 2. Acoustic energy sustain (unvoiced consonants/stops): input level >= sustain_snr_db
            #    above noise floor within 400 ms (40 frames) of a truly voiced frame.
            voiced = (speech_prob >= self.close_threshold)
            self._frames_since_voiced = 0 if voiced else self._frames_since_voiced + 1
            is_sustained = voiced
            if not voiced and input_rms_db is not None and self._frames_since_voiced <= 40:
                snr = input_rms_db - self.noise_floor_db
                if snr >= self.sustain_snr_db and speech_prob >= self.sustain_threshold:
                    is_sustained = True

            if is_sustained:
                self.frames_since_speech = 0
                if voiced:
                    self._burst_voiced += 1
                # Primary near-field voice receives full 1.0x gain
                if is_nearfield or (self._frames_since_nearfield <= 40):
                    target_gain = 1.0
                else:
                    # Distant cubicle chatter in sentence pauses: downward expander attenuates bleed
                    exp_db = input_rms_db - (self.user_anchor_db - self.nearfield_margin_db) if input_rms_db is not None else -14.0
                    target_gain = min(0.15, max(self.floor_gain, 0.15 * (10.0 ** (exp_db / 20.0))))
            else:
                self.frames_since_speech += 1
                if self.frames_since_speech <= self.hangover_frames:
                    # In hangover window: hold gate 100% open
                    target_gain = 1.0
                else:
                    # Speech ended: close gate and begin fade to silence
                    self.is_open = False
                    self.frames_since_close = 0
                    if self._burst_voiced < self.FLUTTER_BURST_FRAMES:
                        self._flutter_events.append(self._frame_idx)
                    target_gain = self.floor_gain

        # Smooth gain transition (exponential attack, exponential decay)
        if target_gain > self.current_gain:
            self.current_gain += (target_gain - self.current_gain) * self.attack_rate
            if self.current_gain >= 0.999:
                self.current_gain = 1.0
        elif target_gain < self.current_gain:
            self.current_gain = self.floor_gain + (self.current_gain - self.floor_gain) * self.decay_rate
            if self.current_gain <= self.floor_gain + 0.001:
                self.current_gain = self.floor_gain

        end_gain = self.current_gain

        # Fast path 1: Steady silence (gain is at floor_gain)
        if end_gain <= self.floor_gain + 0.0001 and start_gain <= self.floor_gain + 0.0001:
            if self.floor_gain > 0.0:
                target_frame *= self.floor_gain
                if self.lookahead and in_place:
                    np.copyto(frame, target_frame)
                    return frame, self.floor_gain
                return target_frame, self.floor_gain
            else:
                if self.lookahead:
                    target_frame.fill(0.0)
                    if in_place:
                        np.copyto(frame, target_frame)
                        return frame, 0.0
                    return target_frame, 0.0
                elif in_place:
                    frame.fill(0.0)
                    return frame, 0.0
                else:
                    if self._zero_buffer is None or len(self._zero_buffer) != len(frame):
                        self._zero_buffer = np.zeros(len(frame), dtype=frame.dtype)
                    return self._zero_buffer, 0.0

        # Fast path 2: Steady speech (gain is 1.0)
        if end_gain >= 0.999 and start_gain >= 0.999:
            if self.lookahead:
                if in_place:
                    np.copyto(frame, target_frame)
                    return frame, 1.0
                return target_frame, 1.0
            return target_frame, 1.0

        # Transition path: Septic Smootherstep (C3) S-curve ramping eliminates clicks, pops, & boundary jerk
        if abs(end_gain - start_gain) > 1e-5 and len(target_frame) > 0:
            if len(target_frame) == 480:
                s_curve = _S_CURVE_TABLE_480
            else:
                t = np.linspace(0.0, 1.0, len(target_frame), dtype=np.float64)
                s_f64 = t**4 * (35.0 + t * (-84.0 + t * (70.0 - 20.0 * t)))
                s_curve = np.maximum.accumulate(np.clip(s_f64, 0.0, 1.0).astype(target_frame.dtype))
            ramp = self._ramp_scratch(len(target_frame), target_frame.dtype)
            np.multiply(s_curve, end_gain - start_gain, out=ramp)
            ramp += start_gain
            target_frame *= ramp
        else:
            target_frame *= end_gain

        if self.lookahead:
            if in_place:
                np.copyto(frame, target_frame)
                return frame, end_gain
            return target_frame, end_gain
        else:
            return target_frame, end_gain


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


def soft_preclip(frame: np.ndarray, knee: float = 0.75) -> np.ndarray:
    """
    Smooths hard ADC rail flat-topping to protect RNNoise Bark-band features.
    Uses hyperbolic tangent saturation above knee (default 0.75), eliminating
    square-wave splatter from unshielded plosives and breath blasts.
    """
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
    frame_mono = soft_preclip(frame_mono, knee=0.75)
    np.clip(frame_mono, -1.0, 1.0, out=frame_mono)

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
    frame_mono, _ = gate.process(frame_mono, speech_prob, input_rms_db=input_rms_db, in_place=True)

    # 8. Soft Saturation Limiter (in place)
    frame_mono = soft_limit(frame_mono, threshold=0.85, out=frame_mono)

    return frame_mono, speech_prob, input_peak_db, input_rms_db


