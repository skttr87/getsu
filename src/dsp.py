"""
Digital Signal Processing (DSP) Module for Getsu.
Features:
- Biquad High-Pass Filter (80Hz Butterworth rumble filter for cooling pads / desk bumps)
- Adaptive Soft-Knee Noise Gate with Hangover (eliminates cooling pad fan hiss between words)
- RMS / Peak level metering
"""
import math
import numpy as np


class HighPassFilter:
    """
    2nd-order IIR Butterworth High-Pass Filter (Biquad).
    Cuts out sub-bass frequencies (< 80 Hz) like laptop cooling pad vibrations,
    desk thumps, and electrical hum before it reaches the AI model.
    """

    def __init__(self, cutoff_hz: float = 80.0, sample_rate: float = 48000.0):
        self.cutoff_hz = cutoff_hz
        self.sample_rate = sample_rate
        self.reset()
        self._calculate_coefficients()

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

    def reset(self):
        self.x1 = 0.0
        self.x2 = 0.0
        self.y1 = 0.0
        self.y2 = 0.0
        self._out_buffer: np.ndarray | None = None

    def process(self, frame: np.ndarray) -> np.ndarray:
        """
        Process a 1D float32 audio frame through the filter.
        Uses Direct Form I for numerical stability with zero heap allocations.
        """
        n = len(frame)
        if self._out_buffer is None or len(self._out_buffer) != n:
            self._out_buffer = np.empty(n, dtype=np.float32)

        out = self._out_buffer
        b0, b1, b2 = self.b0, self.b1, self.b2
        a1, a2 = self.a1, self.a2
        x1, x2, y1, y2 = self.x1, self.x2, self.y1, self.y2

        # Fast C-level list unpack avoids 480 numpy scalar wrapper allocations
        frame_list = frame.tolist() if isinstance(frame, np.ndarray) else frame
        for i in range(n):
            x0 = frame_list[i]
            y0 = b0 * x0 + b1 * x1 + b2 * x2 - a1 * y1 - a2 * y2
            # Anti-denormal flush: prevent CPU microcode stalling on exponential decay silence
            if -1e-15 < y0 < 1e-15:
                y0 = 0.0
            out[i] = y0
            x2 = x1
            x1 = x0
            y2 = y1
            y1 = y0

        self.x1, self.x2, self.y1, self.y2 = x1, x2, y1, y2
        return out


# Precomputed 480-sample Half-Cosine / Raised-Cosine (Hann) curve for zero-allocation C1 slew ramping
_S_CURVE_TABLE_480 = (0.5 * (1.0 - np.cos(np.pi * np.linspace(0.0, 1.0, 480)))).astype(np.float32)


class AdaptiveNoiseGate:
    """
    Adaptive Dual-Threshold (Hysteresis) Soft-Knee Noise Gate with Dynamic Onset Detection.
    Features:
    - Open Threshold: Standard threshold (0.70) rejects cooling pad fan noise during idle.
    - Dual-Key Onset Trigger: Fast-opens gate on early speech probability (0.30) if energy rises
      above background noise floor (+5.0 dB), preserving unvoiced consonants ('s', 't', 'p', 'k', 'h').
    - Cold-Start Gain: Soft-awakens at 0.35 on cold onset from dead silence to eliminate muffled syllables
      without creating harsh amplitude spikes or pops.
    - Close Threshold (Hysteresis): Lower threshold (0.45) keeps gate 100% open during soft word endings.
    - Extended Hangover (360ms): Holds open across natural speech pauses and breathing.
    - Smooth Exponential Decay (80ms): Gently fades to absolute zero silence without chopping.
    - Raised-Cosine S-Curve: Continuous C1 derivative gain ramping completely eliminates clicks and pops.
    """

    def __init__(
        self,
        threshold: float = 0.70,
        close_threshold: float = 0.45,
        hangover_ms: float = 360.0,
        decay_ms: float = 80.0,
        attack_ms: float = 15.0,
        frame_ms: float = 10.0,
        onset_threshold: float = 0.30,
        onset_snr_db: float = 6.0,
        cold_start_gain: float = 0.35,
        lookahead: bool = False,
        floor_gain: float = 0.0,
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
        self.cold_start_gain = cold_start_gain
        self.noise_floor_db = -60.0
        self.lookahead = lookahead
        self.floor_gain = floor_gain

        self.is_open = False
        self.current_gain = floor_gain
        self.frames_since_speech = self.hangover_frames + 10
        self._zero_buffer: np.ndarray | None = None
        self._delay_buffer: np.ndarray | None = None
        self._work_buffer: np.ndarray | None = None

    def process(
        self,
        frame: np.ndarray,
        speech_prob: float,
        input_rms_db: float | None = None,
        in_place: bool = False
    ) -> tuple[np.ndarray, float]:
        """
        Applies hysteresis soft-knee gating based on RNNoise speech probability and dynamic onset SNR.
        Sample-accurate Raised-Cosine S-curve ramping eliminates clicks, pops, and harsh transients.
        Optional 1-frame (10ms) pre-roll lookahead buffer enables 100% natural, unclipped voice onset.
        Returns the gated audio frame and current applied gain.
        """
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

        if not self.is_open:
            # Ambient noise floor tracking during silence
            if input_rms_db is not None and -100.0 < input_rms_db < -20.0:
                if input_rms_db < self.noise_floor_db:
                    self.noise_floor_db += (input_rms_db - self.noise_floor_db) * 0.15
                else:
                    self.noise_floor_db += (input_rms_db - self.noise_floor_db) * 0.02

            # Dual-Key Trigger evaluation:
            # 1. Standard high-confidence VAD trigger
            # 2. Fast onset trigger: energy rise >= onset_snr_db above noise floor with early speech cue
            is_onset = False
            if input_rms_db is not None:
                snr = input_rms_db - self.noise_floor_db
                if snr >= self.onset_snr_db and speech_prob >= self.onset_threshold:
                    is_onset = True

            if speech_prob >= self.threshold or is_onset:
                self.is_open = True
                self.frames_since_speech = 0
                target_gain = 1.0
                if self.floor_gain <= 0.0 and self.current_gain == 0.0 and self.cold_start_gain > 0.0:
                    self.current_gain = self.cold_start_gain
            else:
                target_gain = self.floor_gain
        else:
            # Mic is active: stay open during trailing word endings (speech_prob >= close_threshold)
            if speech_prob >= self.close_threshold:
                self.frames_since_speech = 0
                target_gain = 1.0
            else:
                self.frames_since_speech += 1
                if self.frames_since_speech <= self.hangover_frames:
                    # In hangover window: hold gate 100% open
                    target_gain = 1.0
                else:
                    # Speech ended: close gate and begin fade to silence
                    self.is_open = False
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

        # Transition path: Raised-Cosine (Hann) S-curve ramping eliminates clicks, pops, & harsh boundary steps
        if abs(end_gain - start_gain) > 0.005 and len(target_frame) > 0:
            if len(target_frame) == 480:
                s_curve = _S_CURVE_TABLE_480
            else:
                s_curve = (0.5 * (1.0 - np.cos(np.pi * np.linspace(0.0, 1.0, len(target_frame))))).astype(target_frame.dtype)
            ramp = (start_gain + (end_gain - start_gain) * s_curve).astype(target_frame.dtype, copy=False)
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
    peak = float(np.max(np.abs(frame)))
    rms = float(np.sqrt(np.dot(frame, frame) / len(frame)))
    
    # Convert to dBFS (reference 1.0 for normalized float32; 32767 only if PCM range > 100)
    ref = 32767.0 if peak > 100.0 else 1.0
    peak_db = 20.0 * math.log10(max(peak / ref, 1e-5))
    rms_db = 20.0 * math.log10(max(rms / ref, 1e-5))
    return peak_db, rms_db
