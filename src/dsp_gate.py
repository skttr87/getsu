"""Adaptive soft-knee noise gate with hangover, noise-floor tracking and the S-curve ramp table."""

import bisect
import math
from collections import deque
import numpy as np
from src.dsp_filters import (_design_tilt_fir)


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
        floor_tilt_hz: float = 0.0,
        sample_rate: float = 48000.0,
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

        # Spectral tilt of the closed path (0 Hz = off). Only active when the floor is above silence.
        self._tilt_h: list | None = None
        if floor_tilt_hz > 0.0 and 0.0 < floor_gain < 1.0:
            self._tilt_h = _design_tilt_fir(float(floor_tilt_hz), float(sample_rate))
            self._tilt_hist = np.zeros(len(self._tilt_h) - 1, dtype=np.float32)
            self._tilt_ext: np.ndarray | None = None
            self._tilt_lp: np.ndarray | None = None
            self._tilt_tmp: np.ndarray | None = None
            self._tilt_o: np.ndarray | None = None

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
        if self._tilt_h is not None:
            self._tilt_hist.fill(0.0)

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

    def _tilt_filter(self, x: np.ndarray) -> np.ndarray:
        """Low-passed copy of x (the closed-path signal). Its history is updated on every frame."""
        taps = self._tilt_h
        k1 = len(taps) - 1
        n = len(x)
        if self._tilt_lp is None or len(self._tilt_lp) != n:
            self._tilt_ext = np.zeros(k1 + n, dtype=x.dtype)
            self._tilt_lp = np.zeros(n, dtype=x.dtype)
            self._tilt_tmp = np.zeros(n, dtype=x.dtype)
            self._tilt_o = np.zeros(n, dtype=x.dtype)
        ext = self._tilt_ext
        lp = self._tilt_lp
        tmp = self._tilt_tmp
        ext[:k1] = self._tilt_hist
        ext[k1:] = x
        np.multiply(ext[k1:], taps[0], out=lp)
        for k in range(1, k1 + 1):
            np.multiply(ext[k1 - k:k1 - k + n], taps[k], out=tmp)
            lp += tmp
        self._tilt_hist[:] = ext[n:]
        return lp

    def _tilt_blend_scalar(self, x: np.ndarray, lp: np.ndarray, gain: float):
        """x <- gain * (lp + o * (x - lp)), where o = openness in [0, 1] derived from gain."""
        floor = self.floor_gain
        o = min(1.0, max(0.0, (gain - floor) / (1.0 - floor)))
        tmp = self._tilt_tmp
        np.subtract(x, lp, out=tmp)
        tmp *= o
        tmp += lp
        np.multiply(tmp, gain, out=x)

    def _tilt_blend_array(self, x: np.ndarray, lp: np.ndarray, gain: np.ndarray):
        """Per-sample version of _tilt_blend_scalar for the S-curve ramp."""
        floor = self.floor_gain
        o = self._tilt_o
        np.subtract(gain, floor, out=o)
        o *= 1.0 / (1.0 - floor)
        np.clip(o, 0.0, 1.0, out=o)
        tmp = self._tilt_tmp
        np.subtract(x, lp, out=tmp)
        tmp *= o
        tmp += lp
        np.multiply(tmp, gain, out=x)

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

        # Closed-path spectral tilt: computed on the unscaled frame so the filter history stays continuous
        tilt = self._tilt_h is not None
        if tilt:
            lp = self._tilt_filter(target_frame)

        # Fast path 1: Steady silence (gain is at floor_gain)
        if end_gain <= self.floor_gain + 0.0001 and start_gain <= self.floor_gain + 0.0001:
            if self.floor_gain > 0.0:
                if tilt:
                    np.multiply(lp, self.floor_gain, out=target_frame)
                else:
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
            if tilt:
                self._tilt_blend_array(target_frame, lp, ramp)
            else:
                target_frame *= ramp
        else:
            if tilt:
                self._tilt_blend_scalar(target_frame, lp, end_gain)
            else:
                target_frame *= end_gain

        if self.lookahead:
            if in_place:
                np.copyto(frame, target_frame)
                return frame, end_gain
            return target_frame, end_gain
        else:
            return target_frame, end_gain
