"""
Low-Latency Audio Streaming Engine for Getsu.
Synchronous WASAPI duplex streaming with in-flight DSP and RNNoise neural filter.
"""
import time
import threading
from typing import Optional, Any
import numpy as np
import sounddevice as sd

from src.rnnoise import RNNoise, FRAME_SIZE, SAMPLE_RATE
from src.dsp import (
    HighPassFilter,
    AdaptiveNoiseGate,
    calculate_levels,
    SpeechLeveler,
    TransientSuppressor,
    process_mono_frame,
    PRECLIP_KNEE_DEFAULT,
    InputDownmixer,
    DryBlend,
)
from src.host_api_resolver import resolve_pair, reresolve_index, DeviceResolutionError, ResolvedPair
from src.audio_errors import explain_start_error, ExplainedError
from src.diagnostics import info as log_info, log_start_failure

SAMPLE_RATE_INT = int(SAMPLE_RATE)
# PortAudio codes that mean "this device or rate did not open as requested": the open ladder steps down.
LADDER_CODES = (-9996, -9997, -9999)


def refresh_input_index(index: Optional[int], name: Optional[str]) -> Optional[int]:
    """
    Current index of the input endpoint called `name`, for callers that open a microphone outside the engine
    (the voice test). Returns `index` unchanged when the name is unknown or the device list cannot be read.
    """
    if index is None or not name:
        return index
    try:
        devices = sd.query_devices()
        apis = sd.query_hostapis()
        if not (isinstance(devices, (list, tuple)) and isinstance(apis, (list, tuple))):
            return index
        return reresolve_index(devices, apis, name, int(index))
    except Exception:
        return index



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
        vad_close_threshold: float = 0.52,
        vad_hangover_ms: float = 320.0,
        vad_decay_ms: float = 80.0,
        vad_onset_threshold: float = 0.35,
        vad_onset_snr_db: float = 10.0,
        vad_floor_gain: float = 0.06,
        mic_gain: float = 1.0,
        output_gain: float = 1.08,
        hpf_cutoff_hz: float = 80.0,
        router: Optional[Any] = None,
        auto_level: bool = False,
        mic_boost_db: float = 0.0,
        preclip_knee: float = PRECLIP_KNEE_DEFAULT,
        stereo_downmix: str = InputDownmixer.MODE_AUTO,
        vad_floor_tilt_hz: float = 0.0,
        rnnoise_dry_mix_db: Optional[float] = None,
        input_device_name: Optional[str] = None,
    ):
        self.input_device = input_device
        self.output_device = output_device
        # Name of the selected microphone. The index is re-resolved from it before each stream start.
        self.input_name: Optional[str] = input_device_name
        self.vad_floor_tilt_hz = float(vad_floor_tilt_hz)
        self._dry_blend: Optional[DryBlend] = (
            DryBlend(mix_db=float(rnnoise_dry_mix_db)) if rnnoise_dry_mix_db is not None else None
        )
        self.preclip_knee = float(preclip_knee)
        # Set at start: the VB-Cable sample rate when it is not 48 kHz, else None (see _check_cable_rate)
        self.cable_rate_mismatch: Optional[int] = None
        self._downmixer = InputDownmixer(frame_len=FRAME_SIZE, sample_rate=int(SAMPLE_RATE), mode=stereo_downmix)
        self.denoise_enabled = denoise_enabled
        self.high_pass_enabled = high_pass_enabled
        self.vad_threshold = vad_threshold
        self.vad_close_threshold = vad_close_threshold
        self.vad_hangover_ms = vad_hangover_ms
        self.vad_decay_ms = vad_decay_ms
        self.vad_onset_threshold = vad_onset_threshold
        self.vad_onset_snr_db = vad_onset_snr_db
        self.vad_floor_gain = vad_floor_gain
        self.mic_gain = mic_gain
        self.mic_boost_db = float(mic_boost_db)
        self.output_gain = output_gain
        self.hpf_cutoff_hz = hpf_cutoff_hz
        self.router = router
        self.router_swap_success: bool = True
        self.is_muted = False
        self._was_muted = False
        self.callback_error_count = 0
        self.last_callback_error: Optional[str] = None
        self.resolved_pair: Optional[ResolvedPair] = None
        self.last_start_error: Optional[ExplainedError] = None
        self._lock = threading.RLock()

        # DSP Components
        self.denoise_available = True
        try:
            self.rnnoise = RNNoise()
        except Exception as e:
            log_info(f"[ENGINE] Warning: Failed to initialize RNNoise neural engine ({e}). Operating in DSP bypass mode.")
            self.rnnoise = None
            self.denoise_available = False
            self.denoise_enabled = False

        self.hpf = HighPassFilter(cutoff_hz=self.hpf_cutoff_hz, sample_rate=float(SAMPLE_RATE))
        self.transient_suppressor = TransientSuppressor()
        self.gate = AdaptiveNoiseGate(
            threshold=self.vad_threshold,
            close_threshold=self.vad_close_threshold,
            hangover_ms=self.vad_hangover_ms,
            decay_ms=self.vad_decay_ms,
            attack_ms=28.0,
            frame_ms=10.0,
            onset_threshold=self.vad_onset_threshold,
            onset_snr_db=self.vad_onset_snr_db,
            lookahead=True,
            floor_gain=self.vad_floor_gain,
            cooldown_ms=150.0,
            floor_tilt_hz=self.vad_floor_tilt_hz,
            sample_rate=float(SAMPLE_RATE),
        )
        self.leveler: Optional[SpeechLeveler] = SpeechLeveler() if auto_level else None

        # Device Channel Configuration
        in_dev_info = sd.query_devices(self.input_device)
        out_dev_info = sd.query_devices(self.output_device)
        self.in_channels = min(2, max(1, in_dev_info['max_input_channels']))
        self.out_channels = min(2, max(1, out_dev_info['max_output_channels']))

        if not self.input_name:
            self.input_name = in_dev_info['name']

        self._stream: Optional[sd.Stream] = None
        self._running = False
        # Native-rate ladder step only: SpeexDSP resamplers and their buffers (created at start, see _setup_resampling)
        self._rs_up = None
        self._rs_down = None
        self._rs_in_channels = 1
        self.resample_length_mismatch = 0
        self.resampling_native_rate: Optional[int] = None

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

    def _check_cable_rate(self):
        """
        Reads the default sample rate of the VB-Cable endpoints when the engine writes to CABLE Input.
        Called once at stream start (never from the audio callback). Sets cable_rate_mismatch to the
        offending rate, or None when the rates are 48 kHz or the output is not a VB-Cable device.
        """
        self.cable_rate_mismatch = None
        try:
            from src.devices import check_vbcable_status
            installed, cable_in, cable_out = check_vbcable_status()
        except Exception:
            return
        if not installed or not isinstance(cable_in, dict) or not isinstance(cable_out, dict):
            return
        if cable_in.get("index") != self.output_device:
            return
        for dev in (cable_in, cable_out):
            try:
                rate = int(round(float(sd.query_devices(dev["index"])["default_samplerate"])))
            except Exception:
                continue
            if rate != int(SAMPLE_RATE):
                self.cable_rate_mismatch = rate
                log_info(f"[ENGINE] VB-Cable is set to {rate} Hz (Getsu uses {int(SAMPLE_RATE)} Hz). "
                      f"Set both CABLE devices to 48 kHz in Sound settings for best quality.")
                return

    def _audio_callback(self, indata, outdata, frames, time_info, status):
        """10ms real-time audio processing callback."""
        try:
            # Single float store: atomic under the GIL, read by is_stream_alive() without a lock
            self._last_callback_time = time.monotonic()
            if status:
                if status.input_overflow:
                    self.overflow_count += 1
                if status.output_underflow:
                    self.underflow_count += 1

            self.total_frames += 1

            # Check mute
            if self.is_muted:
                if not self._was_muted:
                    self.gate.reset()
                    self.hpf.reset_state()
                    self._was_muted = True
                outdata.fill(0)
                self._metrics = (0.0, -100.0, -100.0, -100.0)
                return
            self._was_muted = False

            # 1. Stereo Downmixing with safe multi-channel array guard
            if indata.ndim > 1 and indata.shape[1] >= 2:
                frame_mono = self._downmixer.process(indata)
            elif indata.ndim > 1:
                frame_mono = indata[:, 0].copy()
            else:
                frame_mono = indata.copy()

            # 2. Unified Core DSP Pipeline
            total_gain = self.output_gain * self.mic_gain
            rn = self.rnnoise if (self.denoise_enabled and getattr(self, 'denoise_available', True)) else None
            hpf = self.hpf if self.high_pass_enabled else None

            frame_mono, speech_prob, input_peak_db, input_rms_db = process_mono_frame(
                frame_mono=frame_mono,
                rnnoise=rn,
                hpf=hpf,
                gate=self.gate,
                total_gain=total_gain,
                denoise_enabled=bool(rn is not None),
                leveler=self.leveler,
                transient_suppressor=self.transient_suppressor,
                mic_boost_db=self.mic_boost_db,
                preclip_knee=self.preclip_knee,
                dry_blend=self._dry_blend,
            )

            # 3. Post-gate output metrics and clip protection
            peak_db, rms_db = calculate_levels(frame_mono)
            self._metrics = (speech_prob, peak_db, rms_db, input_peak_db)
            np.clip(frame_mono, -1.0, 1.0, out=frame_mono)

            # Route to output device (mono or stereo)
            if self.out_channels == 1:
                outdata[:] = frame_mono.reshape(-1, 1)
            else:
                outdata[:, 0] = frame_mono
                outdata[:, 1] = frame_mono
        except Exception as e:
            self.callback_error_count += 1
            self.last_callback_error = str(e)
            # Fault barrier: output silence rather than letting PortAudio abort stream
            try:
                outdata.fill(0)
            except Exception:
                pass

    def _audio_callback_resampled(self, indata, outdata, frames, time_info, status):
        """
        10 ms callback for the native-rate ladder step. The microphone block (native rate, for example 160
        samples at 16 kHz) is resampled up to 480 samples at 48 kHz, goes through the same process_mono_frame
        pipeline as _audio_callback, and the result is resampled back to the native block for the output.
        Both resamplers and all buffers are created in _setup_resampling; nothing is allocated here.
        """
        try:
            self._last_callback_time = time.monotonic()
            if status:
                if status.input_overflow:
                    self.overflow_count += 1
                if status.output_underflow:
                    self.underflow_count += 1
            self.total_frames += 1

            if self.is_muted:
                if not self._was_muted:
                    self.gate.reset()
                    self.hpf.reset_state()
                    self._was_muted = True
                outdata.fill(0)
                self._metrics = (0.0, -100.0, -100.0, -100.0)
                return
            self._was_muted = False

            # 1. Native rate -> 48 kHz (and stereo downmix at 48 kHz, where InputDownmixer expects its block)
            if self._rs_in_channels == 2:
                np.copyto(self._rs_in_planar, indata.T)
                up = self._rs_up.process(self._rs_in_planar)
                np.copyto(self._rs_stereo48, up.T)
                mono48 = self._downmixer.process(self._rs_stereo48)
            else:
                np.copyto(self._rs_in_mono, indata[:, 0])
                mono48 = self._rs_up.process(self._rs_in_mono)
            np.copyto(self._rs_frame48, mono48)

            # 2. Same DSP pipeline as the 48 kHz path
            total_gain = self.output_gain * self.mic_gain
            rn = self.rnnoise if (self.denoise_enabled and getattr(self, 'denoise_available', True)) else None
            hpf = self.hpf if self.high_pass_enabled else None
            frame48, speech_prob, input_peak_db, input_rms_db = process_mono_frame(
                frame_mono=self._rs_frame48,
                rnnoise=rn,
                hpf=hpf,
                gate=self.gate,
                total_gain=total_gain,
                denoise_enabled=bool(rn is not None),
                leveler=self.leveler,
                transient_suppressor=self.transient_suppressor,
                mic_boost_db=self.mic_boost_db,
                preclip_knee=self.preclip_knee,
                dry_blend=self._dry_blend,
            )
            peak_db, rms_db = calculate_levels(frame48)
            self._metrics = (speech_prob, peak_db, rms_db, input_peak_db)
            np.clip(frame48, -1.0, 1.0, out=frame48)

            # 3. 48 kHz -> native rate for the output endpoint
            np.copyto(self._rs_out48, frame48)
            down = self._rs_down.process(self._rs_out48)
            m = min(down.shape[0], frames)
            if m != frames:
                self.resample_length_mismatch += 1
            if self.out_channels == 1:
                outdata[:m, 0] = down[:m]
                if m < frames:
                    outdata[m:, 0] = 0.0
            else:
                outdata[:m, 0] = down[:m]
                outdata[:m, 1] = down[:m]
                if m < frames:
                    outdata[m:, :] = 0.0
        except Exception as e:
            self.callback_error_count += 1
            self.last_callback_error = str(e)
            try:
                outdata.fill(0)
            except Exception:
                pass

    def _setup_resampling(self, native_rate: int, block: int):
        """Creates the resamplers and buffers for the native-rate step. Called once per start, never in the callback."""
        from src.speexdsp import Resampler
        self._close_resampling()
        self._rs_in_channels = int(self.in_channels)
        self._rs_up = Resampler(native_rate, SAMPLE_RATE_INT, channels=self._rs_in_channels, max_block_in=block)
        self._rs_down = Resampler(SAMPLE_RATE_INT, native_rate, channels=1, max_block_in=FRAME_SIZE)
        self._rs_in_mono = np.zeros(block, dtype=np.float32)
        self._rs_in_planar = np.zeros((2, block), dtype=np.float32)
        self._rs_stereo48 = np.zeros((FRAME_SIZE, 2), dtype=np.float32)
        self._rs_frame48 = np.zeros(FRAME_SIZE, dtype=np.float32)
        self._rs_out48 = np.zeros(FRAME_SIZE, dtype=np.float32)
        self.resampling_native_rate = native_rate
        self.resample_length_mismatch = 0

    def _close_resampling(self):
        for attr in ("_rs_up", "_rs_down"):
            rs = getattr(self, attr, None)
            if rs is not None:
                rs.close()
            setattr(self, attr, None)
        self.resampling_native_rate = None

    def _open_and_start(self, samplerate: int, blocksize: int, callback, extra_settings, latency_setting, label: str):
        """One duplex open and start. Retries the transient busy code (-9985) with backoff; other errors are raised."""
        log_info(f"[STREAM] Opening duplex stream ({label}): input [{self.input_device}] -> output [{self.output_device}]")
        last_err = None
        for attempt in range(3):
            try:
                self._stream = sd.Stream(
                    samplerate=samplerate,
                    blocksize=blocksize,
                    device=(self.input_device, self.output_device),
                    channels=(self.in_channels, self.out_channels),
                    dtype='float32',
                    latency=latency_setting,
                    extra_settings=extra_settings,
                    callback=callback,
                )
                self._stream.start()
                return
            except Exception as e:
                last_err = e
                if self._stream:
                    try:
                        self._stream.close()
                    except Exception:
                        pass
                    self._stream = None
                if attempt == 2 or "PaErrorCode -9985" not in str(e):
                    raise last_err
                log_info(f"[STREAM] Device busy (attempt {attempt + 1}/3): {e}. Retrying...")
                time.sleep(0.10 * (2 ** attempt))

    def _native_rate(self) -> Optional[int]:
        try:
            return int(round(float(sd.query_devices(self.input_device)['default_samplerate'])))
        except Exception:
            return None

    def _refresh_channels(self):
        in_info = sd.query_devices(self.input_device)
        out_info = sd.query_devices(self.output_device)
        self.in_channels = min(2, max(1, in_info['max_input_channels']))
        self.out_channels = min(2, max(1, out_info['max_output_channels']))

    @staticmethod
    def _ladder_code(exc: BaseException) -> Optional[int]:
        return explain_start_error(exc).code

    def _open_with_ladder(self, extra_settings, latency_setting, is_wasapi: bool):
        """
        Opens the duplex stream with a step-down ladder for the codes in LADDER_CODES:
          1. 48 kHz on the resolved pair, 480-sample blocks (unchanged path).
          2. The microphone's native rate on the WASAPI pair (rate a multiple of 100 Hz), 10 ms blocks at the
             native rate, resampled inside the callback (_audio_callback_resampled).
          3. The same microphone and output on DirectSound at 48 kHz (resolve_pair restricted to DirectSound).
        Every attempt is logged. If no step opens, the first error is raised and the devices are restored.
        """
        orig_in, orig_out, orig_pair = self.input_device, self.output_device, self.resolved_pair
        try:
            self._open_and_start(SAMPLE_RATE_INT, FRAME_SIZE, self._audio_callback, extra_settings,
                                 latency_setting, "step 1: 48 kHz")
            return
        except Exception as e:
            first_err = e
            if self._ladder_code(e) not in LADDER_CODES:
                raise
            log_info(f"[STREAM] Step 1 (48 kHz) failed: {explain_start_error(e).code} {e}")

        # Step 2: native rate with resampling
        native = self._native_rate()
        if is_wasapi and native and native != SAMPLE_RATE_INT and native % 100 == 0:
            block = native // 100
            try:
                self._setup_resampling(native, block)
                self._open_and_start(native, block, self._audio_callback_resampled, extra_settings,
                                     latency_setting, f"step 2: native {native} Hz, {block}-sample blocks, resampled")
                log_info(f"[STREAM] Step 2 active: {native} Hz on the microphone, resampled to 48 kHz.")
                return
            except Exception as e:
                self._close_resampling()
                log_info(f"[STREAM] Step 2 ({native} Hz resampled) failed: {explain_start_error(e).code} {e}")
        else:
            log_info(f"[STREAM] Step 2 skipped (native rate {native} Hz, WASAPI pair: {is_wasapi})")

        # Step 3: DirectSound pair at 48 kHz
        try:
            raw_devs = sd.query_devices()
            raw_apis = sd.query_hostapis()
            ds_pair = resolve_pair(raw_devs, raw_apis, orig_in, orig_out, only_api="DIRECTSOUND")
        except Exception as e:
            ds_pair = None
            log_info(f"[STREAM] Step 3 skipped: no DirectSound pair ({e})")
        if ds_pair is not None:
            self.input_device, self.output_device = ds_pair.input, ds_pair.output
            self.resolved_pair = ds_pair
            self._refresh_channels()
            try:
                self._open_and_start(SAMPLE_RATE_INT, FRAME_SIZE, self._audio_callback, None,
                                     latency_setting, f"step 3: DirectSound 48 kHz ({ds_pair.host_name})")
                log_info("[STREAM] Step 3 active: DirectSound 48 kHz.")
                return
            except Exception as e:
                log_info(f"[STREAM] Step 3 (DirectSound) failed: {explain_start_error(e).code} {e}")

        self.input_device, self.output_device, self.resolved_pair = orig_in, orig_out, orig_pair
        self._refresh_channels()
        raise first_err

    def _refresh_input_index(self):
        """Re-resolves the microphone index from its name (indices can shift after PortAudio re-init)."""
        if not self.input_name:
            return
        try:
            devices = sd.query_devices()
            apis = sd.query_hostapis()
            if not (isinstance(devices, (list, tuple)) and isinstance(apis, (list, tuple))):
                return
            new_idx = reresolve_index(devices, apis, self.input_name, self.input_device)
        except Exception as e:
            log_info(f"[STREAM] Input index refresh skipped: {e!r}")
            return
        if new_idx != self.input_device:
            log_info(f"[STREAM] Input index refreshed from [{self.input_device}] to [{new_idx}] for '{self.input_name}'")
            self.input_device = new_idx

    def start(self):
        """Starts the stream. On failure records `last_start_error` (friendly text + raw details) and re-raises."""
        try:
            self._start_impl()
            self.last_start_error = None
        except Exception as e:
            self.last_start_error = explain_start_error(e)
            log_start_failure(e, self.last_start_error, self.input_device, self.output_device)
            raise

    def _start_impl(self):
        """Internal real-time duplex stream startup with transient error retry."""
        with self._lock:
            if self._running:
                return

            self._refresh_input_index()

            # Proactive Host API & Safe Device Resolution:
            # Resolves matching (input, output) pair up-front (WASAPI > DirectSound > MME) and eliminates WDM-KS.
            try:
                raw_devs = sd.query_devices()
                raw_apis = sd.query_hostapis()
                if isinstance(raw_devs, (list, tuple)) and isinstance(raw_apis, (list, tuple)):
                    pair = resolve_pair(raw_devs, raw_apis, self.input_device, self.output_device)
                    self.resolved_pair = pair
                    self.input_name = raw_devs[pair.input]['name']
                    if pair.input != self.input_device:
                        log_info(f"[STREAM] Realigned input device from [{self.input_device}] to [{pair.input}] ({pair.host_name})")
                        self.input_device = pair.input
                    if pair.output != self.output_device:
                        log_info(f"[STREAM] Realigned output device from [{self.output_device}] to [{pair.output}] ({pair.host_name})")
                        self.output_device = pair.output
                    for note in pair.notes:
                        log_info(f"[STREAM] {note}")
                    if pair.ambiguous:
                        log_info("[STREAM] Warning: more than one input could match the selected microphone; "
                              "please confirm it in the app.")
            except DeviceResolutionError:
                raise
            except Exception as e:
                log_info(f"[STREAM] Host API resolution failed unexpectedly: {e!r}")
                try:
                    in_host = sd.query_hostapis()[sd.query_devices(self.input_device)['hostapi']]['name']
                except Exception:
                    in_host = ""
                if "WDM-KS" in in_host.upper():
                    raise DeviceResolutionError(
                        "Could not move the microphone off the WDM-KS audio path. "
                        "Please re-select your microphone."
                    ) from e

            self._check_cable_rate()

            # Query channel configuration for resolved endpoints
            in_dev_info = sd.query_devices(self.input_device)
            out_dev_info = sd.query_devices(self.output_device)
            self.in_channels = min(2, max(1, in_dev_info['max_input_channels']))
            self.out_channels = min(2, max(1, out_dev_info['max_output_channels']))

            # Safe WASAPI auto_convert setting for robust 48kHz duplex streaming
            extra_settings = None
            is_wasapi = getattr(self.resolved_pair, 'is_wasapi', False) if self.resolved_pair else False
            if not self.resolved_pair:
                try:
                    in_api_idx = in_dev_info.get('hostapi') if isinstance(in_dev_info, dict) else None
                    apis = sd.query_hostapis()
                    in_host = apis[in_api_idx]['name'] if (in_api_idx is not None and isinstance(apis, (list, tuple)) and in_api_idx < len(apis)) else ''
                    is_wasapi = 'WASAPI' in in_host.upper()
                except Exception:
                    is_wasapi = False

            if is_wasapi:
                try:
                    extra_settings = sd.WasapiSettings(auto_convert=True)
                except Exception:
                    extra_settings = None

            # Recalibrate noise gate baseline profiler before opening audio stream
            self.gate.recalibrate()

            # Dynamic Bluetooth buffer adaptation: absorb 2.4GHz RF packet retries for Hands-Free Profile (HFP)
            is_bluetooth = False
            try:
                from src.router import native_get_device_bus_type, DeviceBusType
                in_dev_name = in_dev_info.get('name', '') if isinstance(in_dev_info, dict) else ''
                in_bus = native_get_device_bus_type(str(self.input_device) if self.input_device is not None else in_dev_name, is_input=True)
                is_bluetooth = (in_bus == DeviceBusType.BLUETOOTH_HFP)
            except Exception:
                is_bluetooth = False

            latency_setting = 0.035 if is_bluetooth else 'low'

            log_info(f"[STREAM] Starting low-latency stream...")
            log_info(f"         Input  : [{self.input_device}] {in_dev_info['name']} ({self.in_channels} ch)")
            log_info(f"         Output : [{self.output_device}] {out_dev_info['name']} ({self.out_channels} ch)")
            if is_bluetooth:
                log_info(f"         Latency: Bluetooth Jitter Buffer (35ms RF smoothing)")
            else:
                log_info(f"         Blocksize: 10ms (480 samples @ 48kHz) | Est. Latency: ~45ms round-trip")
            
            self._open_with_ladder(extra_settings, latency_setting, is_wasapi)


            self._running = True
            self._last_callback_time = time.monotonic()
            log_info(f"[STREAM] Active! DSP + RNNoise running in background.")
            if self.router and getattr(self.router, 'config', {}).get("auto_route", True):
                try:
                    self.router_swap_success = bool(self.router.swap_to_cable())
                    if not self.router_swap_success:
                        log_info("[ENGINE] Warning: auto_route swap_to_cable() reported failure.")
                except Exception as e:
                    self.router_swap_success = False
                    log_info(f"[ENGINE] Failed to swap default microphone: {e}")
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
                log_info(f"[STREAM] Stopped. Buffer diagnostics: {self.overflow_count} overflows, {self.underflow_count} underflows over {self.total_frames} frames.")
            else:
                log_info("[STREAM] Audio stream stopped.")

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
                        log_info(f"[ENGINE] Failed to restore physical microphone: {e}")
            finally:
                self.stop()

    def set_vad_threshold(self, threshold: float):
        """Live thread-safe adjustment of VAD sensitivity without restarting stream."""
        self.vad_threshold = float(threshold)
        self.gate.threshold = float(threshold)
        # Scale close threshold proportionally to maintain canonical 0.743 hysteresis ratio
        new_close = round(max(0.20, self.vad_threshold * (0.52 / 0.70)), 2)
        self.gate.close_threshold = new_close
        self.vad_close_threshold = new_close
        log_info(f"[ENGINE] VAD threshold updated to: {self.vad_threshold:.2f} (close: {new_close:.2f})")

    def toggle_denoise(self) -> bool:
        """Toggles RNNoise on/off."""
        self.denoise_enabled = not self.denoise_enabled
        log_info(f"[ENGINE] Denoise state: {'ENABLED' if self.denoise_enabled else 'BYPASSED'}")
        return self.denoise_enabled

    def toggle_high_pass(self) -> bool:
        """Toggles 80Hz cooling pad filter."""
        self.high_pass_enabled = not self.high_pass_enabled
        log_info(f"[ENGINE] 80Hz Rumble Filter: {'ENABLED' if self.high_pass_enabled else 'DISABLED'}")
        return self.high_pass_enabled

    def toggle_mute(self) -> bool:
        """Toggles microphone mute."""
        self.is_muted = not self.is_muted
        log_info(f"[ENGINE] Microphone Mute: {'MUTED' if self.is_muted else 'ACTIVE'}")
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
            log_info(f"[STREAM] Warning: Configured input device [{input_device_id}] invalid or disconnected. Falling back to [{fallback_in['index']}] {fallback_in['name']}")
            input_device_id = fallback_in['index']

    # Validate output device against active hardware
    valid_out = validate_device_index(output_device_id, is_input=False)
    if valid_out is None:
        fallback_out_res = auto_select_output_device()
        fallback_out = fallback_out_res[0] if isinstance(fallback_out_res, tuple) else fallback_out_res
        if fallback_out and isinstance(fallback_out, dict):
            log_info(f"[STREAM] Warning: Configured output device [{output_device_id}] invalid or disconnected. Falling back to [{fallback_out['index']}] {fallback_out['name']}")
            output_device_id = fallback_out['index']

    if is_laptop_mic is None:
        try:
            dev_name = sd.query_devices(input_device_id)['name']
            is_laptop_mic = is_laptop_microphone(dev_name)
        except Exception:
            is_laptop_mic = False

    base_gain = 1.2 if is_laptop_mic else 1.0

    cfg_th = config.get("vad_threshold")
    is_vad_customized = config.get("vad_customized", False)
    if is_vad_customized and cfg_th is not None:
        vad_threshold = cfg_th
    else:
        vad_threshold = 0.70 if (cfg_th is None or cfg_th in (0.75, 0.70)) else cfg_th

    music_mode = config.get("music_mode", False)
    denoise_enabled = config.get("denoise_enabled", True) if not music_mode else False
    vad_hangover_ms = 800.0 if music_mode else config.get("vad_hangover_ms", 320.0)
    vad_decay_ms = 250.0 if music_mode else config.get("vad_decay_ms", 80.0)
    vad_close_threshold = config.get("vad_close_threshold", 0.52)

    boost_db = config.get("mic_boost_db", 0)
    boost_mult = 10.0 ** (boost_db / 20.0)
    engine_mic_gain = base_gain * boost_mult

    # The saved name belongs to the saved index only; after a fallback the engine uses the device it was given.
    name_hint = config.get("input_device_name") if input_device_id == config.get("input_device_id") else None

    return AudioEngine(
        input_device=input_device_id,
        output_device=output_device_id,
        input_device_name=name_hint,
        denoise_enabled=denoise_enabled,
        high_pass_enabled=config.get("high_pass_filter", True),
        vad_threshold=vad_threshold,
        vad_close_threshold=vad_close_threshold,
        vad_hangover_ms=vad_hangover_ms,
        vad_decay_ms=vad_decay_ms,
        vad_onset_threshold=config.get("vad_onset_threshold", 0.35),
        vad_onset_snr_db=config.get("vad_onset_snr_db", 10.0),
        vad_floor_gain=config.get("vad_floor_gain", 0.06),
        mic_gain=engine_mic_gain,
        output_gain=config.get("output_gain", 1.08),
        hpf_cutoff_hz=config.get("hpf_cutoff_hz", 80.0),
        router=router,
        auto_level=config.get("auto_level", False),
        mic_boost_db=float(boost_db),
        preclip_knee=float(config.get("preclip_knee", PRECLIP_KNEE_DEFAULT)),
        stereo_downmix=config.get("stereo_downmix", InputDownmixer.MODE_AUTO),
        vad_floor_tilt_hz=float(config.get("vad_floor_tilt_hz", 0.0) or 0.0),
        rnnoise_dry_mix_db=config.get("rnnoise_dry_mix_db"),
    )
