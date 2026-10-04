"""
Low-Latency Audio Streaming Engine for Getsu.
Synchronous WASAPI duplex streaming with in-flight DSP and RNNoise neural filter.
"""
import time
import threading
from typing import Optional, Callable, Any
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
        vad_threshold: float = 0.70,
        vad_close_threshold: float = 0.45,
        vad_hangover_ms: float = 360.0,
        vad_decay_ms: float = 80.0,
        vad_onset_threshold: float = 0.30,
        vad_onset_snr_db: float = 6.0,
        vad_cold_start_gain: float = 0.35,
        vad_floor_gain: float = 0.02,
        mic_gain: float = 1.0,
        output_gain: float = 1.08,
        hpf_cutoff_hz: float = 80.0,
        router: Optional[Any] = None,
    ):
        self.input_device = input_device
        self.output_device = output_device
        self.denoise_enabled = denoise_enabled
        self.high_pass_enabled = high_pass_enabled
        self.vad_threshold = vad_threshold
        self.vad_close_threshold = vad_close_threshold
        self.vad_hangover_ms = vad_hangover_ms
        self.vad_decay_ms = vad_decay_ms
        self.vad_onset_threshold = vad_onset_threshold
        self.vad_onset_snr_db = vad_onset_snr_db
        self.vad_cold_start_gain = vad_cold_start_gain
        self.vad_floor_gain = vad_floor_gain
        self.mic_gain = mic_gain
        self.output_gain = output_gain
        self.hpf_cutoff_hz = hpf_cutoff_hz
        self.router = router
        self.router_swap_success: bool = True
        self.is_muted = False
        self._lock = threading.RLock()
        self._watchdog_lock = threading.Lock()

        # DSP Components
        self.denoise_available = True
        try:
            self.rnnoise = RNNoise()
        except Exception as e:
            print(f"[ENGINE] Warning: Failed to initialize RNNoise neural engine ({e}). Operating in DSP bypass mode.")
            self.rnnoise = None
            self.denoise_available = False
            self.denoise_enabled = False

        self.hpf = HighPassFilter(cutoff_hz=self.hpf_cutoff_hz, sample_rate=float(SAMPLE_RATE))
        self.gate = AdaptiveNoiseGate(
            threshold=self.vad_threshold,
            close_threshold=self.vad_close_threshold,
            hangover_ms=self.vad_hangover_ms,
            decay_ms=self.vad_decay_ms,
            attack_ms=15.0,
            frame_ms=10.0,
            onset_threshold=self.vad_onset_threshold,
            onset_snr_db=self.vad_onset_snr_db,
            cold_start_gain=self.vad_cold_start_gain,
            lookahead=True,
            floor_gain=self.vad_floor_gain,
        )

        # Device Channel Configuration
        in_dev_info = sd.query_devices(self.input_device)
        out_dev_info = sd.query_devices(self.output_device)
        self.in_channels = min(2, max(1, in_dev_info['max_input_channels']))
        self.out_channels = min(2, max(1, out_dev_info['max_output_channels']))

        self._stream: Optional[sd.Stream] = None
        self._running = False

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
        try:
            with self._watchdog_lock:
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
            rn = self.rnnoise
            if self.denoise_enabled and getattr(self, 'denoise_available', True) and rn is not None:
                # Scale to 16-bit float range expected by RNNoise
                frame_rn = frame_mono * 32767.0
                frame_rn, speech_prob = rn.process_frame(frame_rn)
                frame_mono = frame_rn / 32767.0

                # Post-RNNoise make-up gain (+0.7 dB) to restore natural speech body
                if self.output_gain != 1.0:
                    frame_mono *= self.output_gain

                # Soft-knee gate for cooling pad silence floor (in-place zeroing for zero heap allocation)
                frame_mono, _ = self.gate.process(frame_mono, speech_prob, input_rms_db=input_rms_db, in_place=True)
            else:
                # Bypass Mode: derive speech probability from RMS energy to silence background hiss
                speech_prob = min(1.0, max(0.0, (input_rms_db + 45.0) / 20.0))
                frame_mono, _ = self.gate.process(frame_mono, speech_prob, input_rms_db=input_rms_db, in_place=True)

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
        except Exception:
            # Fault barrier: output silence rather than letting PortAudio abort stream
            try:
                outdata.fill(0)
            except Exception:
                pass

    def start(self):
        """Starts the real-time duplex stream with transient error retry."""
        with self._lock:
            if self._running:
                return

            # Dynamically query channel configuration in case devices changed
            in_dev_info = sd.query_devices(self.input_device)
            out_dev_info = sd.query_devices(self.output_device)
            self.in_channels = min(2, max(1, in_dev_info['max_input_channels']))
            self.out_channels = min(2, max(1, out_dev_info['max_output_channels']))

            # Host API Alignment Guard:
            # PortAudio requires input and output in a duplex stream to share the exact same Host API.
            # If mismatched or if WDM-KS is detected, realign output to matching CABLE Input.
            try:
                apis = sd.query_hostapis()
                in_api_idx = in_dev_info.get('hostapi') if isinstance(in_dev_info, dict) else None
                out_api_idx = out_dev_info.get('hostapi') if isinstance(out_dev_info, dict) else None
                in_host = apis[in_api_idx]['name'] if (in_api_idx is not None and isinstance(apis, (list, tuple)) and in_api_idx < len(apis)) else ''
                out_host = apis[out_api_idx]['name'] if (out_api_idx is not None and isinstance(apis, (list, tuple)) and out_api_idx < len(apis)) else ''
                
                # 1. Input-side WDM-KS defense: realign to matching WASAPI device
                if 'WDM-KS' in in_host.upper():
                    from src.devices import get_input_devices
                    wasapi_mics = get_input_devices()
                    if wasapi_mics:
                        cur_name = in_dev_info.get('name', '').lower()
                        matched_mic = next((m for m in wasapi_mics if m['name'].lower() in cur_name or cur_name in m['name'].lower()), wasapi_mics[0])
                        if matched_mic['index'] != self.input_device:
                            print(f"[STREAM] Realigning WDM-KS input device from [{self.input_device}] to WASAPI [{matched_mic['index']}] {matched_mic['name']}")
                            self.input_device = matched_mic['index']
                            in_dev_info = sd.query_devices(self.input_device)
                            self.in_channels = min(2, max(1, in_dev_info['max_input_channels']))
                            in_api_idx = in_dev_info.get('hostapi') if isinstance(in_dev_info, dict) else None
                            in_host = apis[in_api_idx]['name'] if (in_api_idx is not None and isinstance(apis, (list, tuple)) and in_api_idx < len(apis)) else ''

                # 2. Output-side alignment: realign output to matching CABLE Input
                if (in_host and out_host and in_host != out_host) or 'WDM-KS' in out_host.upper():
                    from src.devices import find_matching_cable_input
                    matched = find_matching_cable_input(self.input_device)
                    if matched and matched['index'] != self.output_device:
                        print(f"[STREAM] Realigning output device from [{self.output_device}] to [{matched['index']}] {matched['name']} to match Host API ({in_host})")
                        self.output_device = matched['index']
                        out_dev_info = sd.query_devices(self.output_device)
                        self.out_channels = min(2, max(1, out_dev_info['max_output_channels']))
            except Exception as e:
                print(f"[STREAM] Host API alignment check skipped: {e}")

            print(f"[STREAM] Starting low-latency stream...")
            print(f"         Input  : [{self.input_device}] {in_dev_info['name']} ({self.in_channels} ch)")
            print(f"         Output : [{self.output_device}] {out_dev_info['name']} ({self.out_channels} ch)")
            print(f"         Latency: 10ms (480 samples @ 48kHz)")
            
            last_err = None
            for attempt in range(3):
                try:
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
                    break
                except Exception as e:
                    last_err = e
                    if self._stream:
                        try:
                            self._stream.close()
                        except Exception:
                            pass
                        self._stream = None

                    # If WDM-KS or Host mismatch failed, attempt emergency realign on retry
                    if attempt < 2 and any(k in str(e) for k in ["WdmSyncIoctl", "Illegal combination", "PaErrorCode -9999", "PaErrorCode -9993"]):
                        try:
                            from src.devices import get_input_devices, find_matching_cable_input
                            # Emergency realign input to safe WASAPI mic if WDM-KS was involved
                            if "WdmSyncIoctl" in str(e) or "WDM-KS" in str(e):
                                wasapi_mics = get_input_devices()
                                if wasapi_mics:
                                    cur_name = in_dev_info.get('name', '').lower()
                                    matched_mic = next((m for m in wasapi_mics if m['name'].lower() in cur_name or cur_name in m['name'].lower()), wasapi_mics[0])
                                    if matched_mic['index'] != self.input_device:
                                        print(f"[STREAM] Emergency realigning input device to [{matched_mic['index']}] {matched_mic['name']}")
                                        self.input_device = matched_mic['index']
                                        in_dev_info = sd.query_devices(self.input_device)
                                        self.in_channels = min(2, max(1, in_dev_info['max_input_channels']))

                            matched = find_matching_cable_input(self.input_device)
                            if matched and matched['index'] != self.output_device:
                                print(f"[STREAM] Emergency realigning output device to [{matched['index']}] {matched['name']}")
                                self.output_device = matched['index']
                                out_dev_info = sd.query_devices(self.output_device)
                                self.out_channels = min(2, max(1, out_dev_info['max_output_channels']))
                        except Exception:
                            pass

                    if attempt == 2:
                        raise last_err
                    print(f"[STREAM] Transient stream opening error (attempt {attempt + 1}/3): {e}. Retrying...")
                    time.sleep(0.10 * (2 ** attempt))

            self._running = True
            self._last_callback_time = time.monotonic()
            print(f"[STREAM] Active! DSP + RNNoise running in background.")
            if self.router and getattr(self.router, 'config', {}).get("auto_route", True):
                try:
                    self.router_swap_success = bool(self.router.swap_to_cable())
                    if not self.router_swap_success:
                        print("[ENGINE] Warning: auto_route swap_to_cable() reported failure.")
                except Exception as e:
                    self.router_swap_success = False
                    print(f"[ENGINE] Failed to swap default microphone: {e}")
            else:
                self.router_swap_success = True

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
            if self.overflow_count > 0 or self.underflow_count > 0:
                print(f"[STREAM] Stopped. Buffer diagnostics: {self.overflow_count} overflows, {self.underflow_count} underflows over {self.total_frames} frames.")
            else:
                print("[STREAM] Audio stream stopped.")

    def prepare_for_stop(self):
        """
        Unified cleanup method called by all 5 exit vectors:
        1. GUI Stop button
        2. System tray exit
        3. Window close (WM_NCDESTROY)
        4. Orderly Windows shutdown (atexit / WM_ENDSESSION)
        5. Watchdog emergency auto-restore
        """
        with self._lock:
            try:
                if self.router:
                    try:
                        self.router.restore_original()
                    except Exception as e:
                        print(f"[ENGINE] Failed to restore physical microphone: {e}")
            finally:
                self.stop()

    def set_vad_threshold(self, threshold: float):
        """Live thread-safe adjustment of VAD sensitivity without restarting stream."""
        self.vad_threshold = float(threshold)
        self.gate.threshold = float(threshold)
        print(f"[ENGINE] VAD threshold updated to: {self.vad_threshold:.2f}")

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
        with self._watchdog_lock:
            elapsed = time.monotonic() - self._last_callback_time
        if elapsed > 2.0:
            return False
        return True

    def close(self):
        self.stop()
        rn = self.rnnoise
        self.rnnoise = None
        if rn is not None:
            rn.close()


def create_engine_from_config(
    config: dict,
    input_device_id: int,
    output_device_id: int,
    is_laptop_mic: Optional[bool] = None,
    router: Optional[Any] = None,
) -> AudioEngine:
    """
    Constructs an AudioEngine instance configured with unified, optimized defaults.
    Automatically validates device indices against active hardware with graceful fallback,
    and detects built-in laptop microphones to apply wider hysteresis.
    """
    from src.devices import validate_device_index, auto_select_input_device, auto_select_output_device, is_laptop_microphone

    # Validate input device against active hardware
    valid_in = validate_device_index(input_device_id, is_input=True)
    if valid_in is None:
        fallback_in = auto_select_input_device()
        if fallback_in and isinstance(fallback_in, dict):
            print(f"[STREAM] Warning: Configured input device [{input_device_id}] invalid or disconnected. Falling back to [{fallback_in['index']}] {fallback_in['name']}")
            input_device_id = fallback_in['index']

    # Validate output device against active hardware
    valid_out = validate_device_index(output_device_id, is_input=False)
    if valid_out is None:
        fallback_out_res = auto_select_output_device()
        fallback_out = fallback_out_res[0] if isinstance(fallback_out_res, tuple) else fallback_out_res
        if fallback_out and isinstance(fallback_out, dict):
            print(f"[STREAM] Warning: Configured output device [{output_device_id}] invalid or disconnected. Falling back to [{fallback_out['index']}] {fallback_out['name']}")
            output_device_id = fallback_out['index']

    if is_laptop_mic is None:
        try:
            dev_name = sd.query_devices(input_device_id)['name']
            is_laptop_mic = is_laptop_microphone(dev_name)
        except Exception:
            is_laptop_mic = False

    if is_laptop_mic:
        default_th = 0.70
        default_hangover = 360.0
        base_gain = 1.2
    else:
        default_th = 0.70
        default_hangover = 360.0
        base_gain = 1.0

    cfg_th = config.get("vad_threshold")
    is_vad_customized = config.get("vad_customized", False)
    if is_vad_customized and cfg_th is not None:
        vad_threshold = cfg_th
    else:
        vad_threshold = default_th if (cfg_th is None or cfg_th in (0.75, 0.70)) else cfg_th

    cfg_hangover = config.get("vad_hangover_ms")
    vad_hangover_ms = default_hangover if (cfg_hangover is None or (not is_vad_customized and cfg_hangover in (180.0, 220.0, 360.0))) else cfg_hangover

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
        vad_decay_ms=config.get("vad_decay_ms", 80.0),
        vad_onset_threshold=config.get("vad_onset_threshold", 0.30),
        vad_onset_snr_db=config.get("vad_onset_snr_db", 6.0),
        vad_cold_start_gain=config.get("vad_cold_start_gain", 0.35),
        vad_floor_gain=config.get("vad_floor_gain", 0.02),
        mic_gain=mic_gain,
        output_gain=config.get("output_gain", 1.08),
        hpf_cutoff_hz=config.get("hpf_cutoff_hz", 80.0),
        router=router,
    )
