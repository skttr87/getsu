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
import threading
from typing import Tuple, Optional, Any
import numpy as np

# Names the tests and the live engine import through this module are re-exported here (see tests/test_dsp.py,
# tests/test_m12_hotpath.py, tests/test_m2_sound.py); the rest are imported from their own modules.
from src.dsp_filters import HighPassFilter, _design_tilt_fir  # noqa: F401  re-export
from src.dsp_gate import (_S_CURVE_TABLE_480, AdaptiveNoiseGate)  # noqa: F401  re-export
from src.dsp_dynamics import (  # noqa: F401  re-exports
    calculate_levels, PRECLIP_KNEE_DEFAULT, soft_preclip, soft_limit, TS_RADIUS,
    TS_SKIP_SPEECH_PROB, _TS_BELL, TransientSuppressor, SpeechLeveler
)
from src.dsp_input import (InputDownmixer, DryBlend)  # noqa: F401  re-export


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
