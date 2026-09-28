"""
Low-Latency Audio Streaming Engine for Getsu.
Synchronous WASAPI duplex streaming with in-flight DSP and RNNoise neural filter.
"""
import time
import threading
from typing import Optional, Callable
import numpy as np
import sounddevice as sd

from src.rnnoise import RNNoise, FRAME_SIZE, SAMPLE_RATE
from src.dsp import HighPassFilter, AdaptiveNoiseGate, calculate_levels


class AudioEngine:
    """
    Real-Time DSP Stream Engine.
    Routes audio from physical mic -> HighPassFilter -> RNNoise -> NoiseGate -> Target Output.
    """

    def __init__(
        self,
        input_device: int,
        output_device: int,
        denoise_enabled: bool = True,
        high_pass_enabled: bool = True,
        vad_threshold: float = 0.75,
        vad_close_threshold: float = 0.45,
        vad_hangover_ms: float = 180.0,
        mic_gain: float = 1.0,
        output_gain: float = 1.08,
        hpf_cutoff_hz: float = 80.0,
    ):
        self.input_device = input_device
        self.output_device = output_device
        self.denoise_enabled = denoise_enabled
        self.high_pass_enabled = high_pass_enabled
        self.vad_threshold = vad_threshold
        self.vad_close_threshold = vad_close_threshold
        self.vad_hangover_ms = vad_hangover_ms
        self.mic_gain = mic_gain
        self.output_gain = output_gain
        self.hpf_cutoff_hz = hpf_cutoff_hz
        self.is_muted = False

        # DSP Components
        self.rnnoise = RNNoise()
        self.hpf = HighPassFilter(cutoff_hz=self.hpf_cutoff_hz, sample_rate=float(SAMPLE_RATE))
        self.gate = AdaptiveNoiseGate(
            threshold=self.vad_threshold,
            close_threshold=self.vad_close_threshold,
            hangover_ms=self.vad_hangover_ms,
            decay_ms=40.0,
            attack_ms=15.0,
            frame_ms=10.0,
        )

        # Device Channel Configuration
        in_dev_info = sd.query_devices(self.input_device)
        out_dev_info = sd.query_devices(self.output_device)
        self.in_channels = min(2, max(1, in_dev_info['max_input_channels']))
        self.out_channels = min(2, max(1, out_dev_info['max_output_channels']))

        self._stream: Optional[sd.Stream] = None
        self._running = False
        self._lock = threading.Lock()

        # Live Metrics for UI (Thread-Safe Atomic Tuple: speech_prob, peak_db, rms_db, input_peak_db)
        self._metrics = (0.0, -100.0, -100.0, -100.0)
        self.total_frames = 0
        self.overflow_count = 0
        self.underflow_count = 0
        self._last_callback_time: float = 0.0

    @property
    def last_speech_prob(self) -> float:
        return self._metrics[0]

    @property
    def last_peak_db(self) -> float:
        return self._metrics[1]

    @property
    def last_rms_db(self) -> float:
        return self._metrics[2]

    @property
    def last_input_peak_db(self) -> float:
        return self._metrics[3]

    def _audio_callback(self, indata, outdata, frames, time_info, status):
        """10ms real-time audio processing callback."""
        self._last_callback_time = time.monotonic()
        if status:
            if status.input_overflow:
                self.overflow_count += 1
            if status.output_underflow:
                self.underflow_count += 1

        self.total_frames += 1

        # Check mute
        if self.is_muted:
            outdata.fill(0)
            self._metrics = (0.0, -100.0, -100.0, -100.0)
            return

        # 1. Stereo Downmixing with safe multi-channel array guard
        if indata.ndim > 1 and indata.shape[1] >= 2:
            frame_mono = (indata[:, 0] + indata[:, 1]) * 0.5
        elif indata.ndim > 1:
            frame_mono = indata[:, 0].copy()
        else:
            frame_mono = indata.copy()

        # 2. Apply mic gain & clamp normalized input before RNNoise
        if self.mic_gain != 1.0:
            frame_mono *= self.mic_gain
        np.clip(frame_mono, -1.0, 1.0, out=frame_mono)

        # 3. DSP Stage 1: High-Pass Filter (80Hz rumble cut for cooling pads)
        if self.high_pass_enabled:
            frame_mono = self.hpf.process(frame_mono)

        # Dual-Stage Metering: Pre-gate input level (audible voice level without fan rumble inflation)
        input_peak_db, input_rms_db = calculate_levels(frame_mono)

        speech_prob = 0.0
        # 4. DSP Stage 2 & 3: RNNoise Neural Suppression + Soft-Knee Adaptive Gate
        if self.denoise_enabled:
            # Scale to 16-bit float range expected by RNNoise
            frame_rn = frame_mono * 32767.0
            frame_rn, speech_prob = self.rnnoise.process_frame(frame_rn)
            frame_mono = frame_rn / 32767.0

            # Post-RNNoise make-up gain (+0.7 dB) to restore natural speech body
            if self.output_gain != 1.0:
                frame_mono *= self.output_gain

            # Soft-knee gate for cooling pad silence floor (in-place zeroing for zero heap allocation)
            frame_mono, _ = self.gate.process(frame_mono, speech_prob, in_place=True)
        else:
            # Bypass Mode: derive speech probability from RMS energy to silence background hiss
            speech_prob = min(1.0, max(0.0, (input_rms_db + 45.0) / 20.0))
            frame_mono, _ = self.gate.process(frame_mono, speech_prob, in_place=True)

        # Calculate post-gate output metrics and atomically swap metrics tuple
        peak_db, rms_db = calculate_levels(frame_mono)
        self._metrics = (speech_prob, peak_db, rms_db, input_peak_db)

        # Final clip to prevent DAC wrap distortion
        np.clip(frame_mono, -1.0, 1.0, out=frame_mono)

        # Route to output device (mono or stereo)
        if self.out_channels == 1:
            outdata[:] = frame_mono.reshape(-1, 1)
        else:
            outdata[:, 0] = frame_mono
            outdata[:, 1] = frame_mono

    def start(self):
        """Starts the real-time duplex stream."""
        with self._lock:
            if self._running:
                return

            # Dynamically query channel configuration in case devices changed
            in_dev_info = sd.query_devices(self.input_device)
            out_dev_info = sd.query_devices(self.output_device)
            self.in_channels = min(2, max(1, in_dev_info['max_input_channels']))
            self.out_channels = min(2, max(1, out_dev_info['max_output_channels']))

            print(f"[STREAM] Starting low-latency stream...")
            print(f"         Input  : [{self.input_device}] {in_dev_info['name']} ({self.in_channels} ch)")
            print(f"         Output : [{self.output_device}] {out_dev_info['name']} ({self.out_channels} ch)")
            print(f"         Latency: 10ms (480 samples @ 48kHz)")
            
            self._stream = sd.Stream(
                samplerate=SAMPLE_RATE,
                blocksize=FRAME_SIZE,
                device=(self.input_device, self.output_device),
                channels=(self.in_channels, self.out_channels),
                dtype='float32',
                latency='low',
                callback=self._audio_callback
            )
            self._stream.start()
            self._running = True
            self._last_callback_time = time.monotonic()
            print(f"[STREAM] Active! DSP + RNNoise running in background.")

    def stop(self):
        """Stops the real-time stream cleanly."""
        with self._lock:
            if not self._running:
                return
            self._running = False
            if self._stream:
                self._stream.stop()
                self._stream.close()
                self._stream = None
            print("[STREAM] Audio stream stopped.")

    def toggle_denoise(self) -> bool:
        """Toggles RNNoise on/off."""
        self.denoise_enabled = not self.denoise_enabled
        print(f"[ENGINE] Denoise state: {'ENABLED' if self.denoise_enabled else 'BYPASSED'}")
        return self.denoise_enabled

    def toggle_high_pass(self) -> bool:
        """Toggles 80Hz cooling pad filter."""
        self.high_pass_enabled = not self.high_pass_enabled
        print(f"[ENGINE] 80Hz Rumble Filter: {'ENABLED' if self.high_pass_enabled else 'DISABLED'}")
        return self.high_pass_enabled

    def toggle_mute(self) -> bool:
        """Toggles microphone mute."""
        self.is_muted = not self.is_muted
        print(f"[ENGINE] Microphone Mute: {'MUTED' if self.is_muted else 'ACTIVE'}")
        return self.is_muted

    def is_active(self) -> bool:
        return self._running

    def is_stream_alive(self) -> bool:
        """
        Returns True if the stream is running, healthy, and actively processing frames.
        Returns False if the stream has aborted, is inactive, or callback stopped firing (>2.0s).
        """
        if not self._running or self._stream is None:
            return False
        try:
            if not self._stream.active:
                return False
        except Exception:
            return False
        if (time.monotonic() - self._last_callback_time) > 2.0:
            return False
        return True

    def close(self):
        self.stop()
        self.rnnoise.close()


def create_engine_from_config(
    config: dict,
    input_device_id: int,
    output_device_id: int,
    is_laptop_mic: Optional[bool] = None,
) -> AudioEngine:
    """
    Constructs an AudioEngine instance configured with unified, optimized defaults.
    Automatically detects built-in laptop microphones to apply wider hysteresis.
    """
    if is_laptop_mic is None:
        try:
            dev_name = sd.query_devices(input_device_id)['name'].lower()
            is_laptop_mic = any(k in dev_name for k in ["realtek", "array", "built-in", "internal"])
        except Exception:
            is_laptop_mic = False

    if is_laptop_mic:
        default_th = 0.70
        default_hangover = 220.0
        base_gain = 1.2
    else:
        default_th = 0.75
        default_hangover = 180.0
        base_gain = 1.0

    cfg_th = config.get("vad_threshold")
    vad_threshold = default_th if (cfg_th is None or cfg_th == 0.75) else cfg_th

    cfg_hangover = config.get("vad_hangover_ms")
    vad_hangover_ms = default_hangover if (cfg_hangover is None or cfg_hangover == 180.0) else cfg_hangover

    boost_db = config.get("mic_boost_db", 0)
    boost_mult = 10.0 ** (boost_db / 20.0)
    mic_gain = config.get("mic_gain", 1.0) * base_gain * boost_mult

    return AudioEngine(
        input_device=input_device_id,
        output_device=output_device_id,
        denoise_enabled=config.get("denoise_enabled", True),
        high_pass_enabled=config.get("high_pass_filter", True),
        vad_threshold=vad_threshold,
        vad_close_threshold=config.get("vad_close_threshold", 0.45),
        vad_hangover_ms=vad_hangover_ms,
        mic_gain=mic_gain,
        output_gain=config.get("output_gain", 1.08),
        hpf_cutoff_hz=config.get("hpf_cutoff_hz", 80.0),
    )
