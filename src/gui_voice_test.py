"""The 11-second voice test (record, play back, purge) and the state of its button."""

import threading
import time

import dearpygui.dearpygui as dpg
import sounddevice as sd
import numpy as np

from src.devices import get_physical_output_device, is_laptop_microphone
from src.dsp import (
    HighPassFilter,
    AdaptiveNoiseGate,
    SpeechLeveler,
    TransientSuppressor,
    process_mono_frame,
    InputDownmixer,
)
from src.gui_theme import COL_ACCENT, COL_ERROR, COL_WARNING
from src.host_api_resolver import resolve_single
from src.rnnoise import RNNoise, FRAME_SIZE, SAMPLE_RATE
from src.stream import refresh_input_index


class VoiceTestMixin:
    """Methods mixed into GetsuGUI (see src/gui.py)."""

    def start_voice_test(self, sender=None, app_data=None):
        """Starts the 11-second in-memory voice preview test (microphone to speakers; the cable is not needed)."""
        if self.is_running:
            self.set_status_pill("Voice test could not start: Stop noise cancellation first.", COL_WARNING)
            return
        if self._is_testing_voice:
            self.set_status_pill("Voice test could not start: A voice test is already running.", COL_WARNING)
            return
        if not self._state_lock.acquire(blocking=False):
            self.set_status_pill("Voice test could not start: Noise cancellation is starting or stopping.", COL_WARNING)
            return

        self._is_testing_voice = True
        self._cancel_voice_test = False
        self._vt_active = True
        self._vt_progress = 0.0
        self._set_button_enabled("btn_voice_test", False)
        self._set_button_label("btn_voice_test", "Testing...")

        threading.Thread(target=self._run_voice_test_worker, daemon=True).start()

    def _run_voice_test_worker(self):
        """
        Executes the 3-phase Voice Test:
        Phase 1: Record 11s from physical microphone (speakers MUTED, zero acoustic feedback).
                 Processed in-flight through HighPassFilter, RNNoise, AdaptiveNoiseGate, and mic_gain.
                 Stored strictly in RAM (numpy float32 buffer).
        Phase 2: Play back 11s processed audio through primary physical playback device.
        Phase 3: Immediate RAM purge (buffer deleted, zero disk files).
        """
        rn = None
        recording = False  # True once the microphone is open: later errors are not "could not start"
        try:
            if self.selected_input_idx is None or self.selected_input_idx < 0:
                self.set_status_pill("Voice test could not start: No microphone selected.", COL_ERROR)
                return

            # The index of the microphone can change when Windows renumbers the devices (issue #3): look up its
            # current index by name before the microphone is opened.
            input_idx = refresh_input_index(self.selected_input_idx, self.selected_input_name)

            # Resolve safe non-WDM-KS input endpoint for voice preview
            try:
                raw_devs = sd.query_devices()
                raw_apis = sd.query_hostapis()
                safe_input_idx, notes = resolve_single(raw_devs, raw_apis, input_idx, is_input=True)
                for note in notes:
                    print(f"[VOICE TEST] {note}")
            except Exception as e:
                print(f"[VOICE TEST] Input resolution fallback: {e}")
                safe_input_idx = input_idx

            in_info = sd.query_devices(safe_input_idx)
            in_channels = min(2, max(1, in_info['max_input_channels']))

            # Instantiate DSP chain matching current settings
            boost_db = self.config.get("mic_boost_db", 0)
            boost_mult = 10.0 ** (boost_db / 20.0)
            dev_name = in_info.get('name', '')
            is_laptop = is_laptop_microphone(dev_name, safe_input_idx)
            base_gain = 1.2 if is_laptop else 1.0
            test_gain = base_gain * boost_mult
            out_gain = float(self.config.get("output_gain", 1.08))
            total_gain = out_gain * test_gain
            vad_th = float(self.config.get("vad_threshold", 0.70))

            music_mode = self.config.get("music_mode", False)
            rn = None if music_mode else RNNoise()
            hpf = HighPassFilter(cutoff_hz=80.0, sample_rate=float(SAMPLE_RATE))
            ts = TransientSuppressor()
            hangover_ms = 800.0 if music_mode else float(self.config.get("vad_hangover_ms", 320.0))
            decay_ms = 250.0 if music_mode else float(self.config.get("vad_decay_ms", 80.0))
            gate = AdaptiveNoiseGate(
                threshold=vad_th,
                close_threshold=float(self.config.get("vad_close_threshold", 0.52)),
                hangover_ms=hangover_ms,
                decay_ms=decay_ms,
                attack_ms=28.0,
                frame_ms=10.0,
                onset_threshold=float(self.config.get("vad_onset_threshold", 0.35)),
                onset_snr_db=float(self.config.get("vad_onset_snr_db", 10.0)),
                lookahead=True,
                floor_gain=float(self.config.get("vad_floor_gain", 0.06)),
                cooldown_ms=150.0,
            )
            gate.recalibrate()
            leveler = SpeechLeveler() if self.config.get("auto_level", False) else None

            # Phase 1: Record 11 seconds (speakers MUTED, RAM only)
            total_chunks = 1100
            chunk_size = FRAME_SIZE
            processed_chunks = []

            # Query host API for WASAPI auto_convert sample rate resiliency (e.g. 16kHz Bluetooth mics)
            in_extra_settings = None
            try:
                raw_apis = sd.query_hostapis()
                in_api_idx = in_info.get('hostapi')
                if in_api_idx is not None and isinstance(raw_apis, (list, tuple)) and in_api_idx < len(raw_apis):
                    if 'WASAPI' in raw_apis[in_api_idx]['name'].upper():
                        in_extra_settings = sd.WasapiSettings(auto_convert=True)
            except Exception:
                in_extra_settings = None

            print(f"[VOICE TEST] Phase 1: Recording 11s from [{safe_input_idx}] {in_info['name']}...")
            voice_downmix = InputDownmixer(frame_len=chunk_size, sample_rate=int(SAMPLE_RATE),
                                           mode=self.config.get("stereo_downmix", InputDownmixer.MODE_AUTO))
            with sd.InputStream(
                samplerate=SAMPLE_RATE,
                blocksize=chunk_size,
                device=safe_input_idx,
                channels=in_channels,
                dtype='float32',
                extra_settings=in_extra_settings,
            ) as stream:
                recording = True
                for chunk_idx in range(total_chunks):
                    if not self._app_running or self._cancel_voice_test:
                        break
                    indata, _ = stream.read(chunk_size)
                    # Mono mix
                    if indata.ndim > 1 and indata.shape[1] >= 2:
                        mono = voice_downmix.process(indata)
                    elif indata.ndim > 1:
                        mono = indata[:, 0].copy()
                    else:
                        mono = indata.copy()

                    # Unified Single-Frame DSP Pipeline
                    mono, _, _, _ = process_mono_frame(
                        frame_mono=mono,
                        rnnoise=rn,
                        hpf=hpf,
                        gate=gate,
                        total_gain=total_gain,
                        denoise_enabled=bool(rn is not None),
                        leveler=leveler,
                        transient_suppressor=ts,
                        mic_boost_db=float(boost_db),
                    )
                    # process_mono_frame may return a buffer it reuses for the next frame: keep a copy.
                    processed_chunks.append(mono.copy())

                    # Update countdown UI once every 100 chunks (~1.0s)
                    if chunk_idx % 100 == 0:
                        sec_left = 11 - (chunk_idx // 100)
                        self.set_status_pill(f"Voice Test: Recording to RAM... ({sec_left}s left)", COL_ACCENT)
                        self._vt_progress = chunk_idx / float(total_chunks)
                        self._set_button_label("btn_voice_test", f"Recording... ({sec_left}s)")

            if not self._app_running or self._cancel_voice_test or not processed_chunks:
                return

            # Combine processed chunks strictly in RAM
            full_audio = np.concatenate(processed_chunks, axis=0)

            # Phase 2: Playback 11s through primary physical speakers/headphones
            out_dev = get_physical_output_device(force_rescan=True)
            out_idx = out_dev['index'] if (out_dev and isinstance(out_dev, dict)) else (out_dev if isinstance(out_dev, int) else sd.default.device[1])
            try:
                raw_devs = sd.query_devices()
                raw_apis = sd.query_hostapis()
                safe_out_idx, _ = resolve_single(raw_devs, raw_apis, out_idx, is_input=False)
            except Exception:
                safe_out_idx = out_idx

            out_info = sd.query_devices(safe_out_idx)
            out_channels = min(2, max(1, out_info['max_output_channels']))

            out_extra_settings = None
            try:
                raw_apis = sd.query_hostapis()
                out_api_idx = out_info.get('hostapi')
                if out_api_idx is not None and isinstance(raw_apis, (list, tuple)) and out_api_idx < len(raw_apis):
                    if 'WASAPI' in raw_apis[out_api_idx]['name'].upper():
                        out_extra_settings = sd.WasapiSettings(auto_convert=True)
            except Exception:
                out_extra_settings = None

            print(f"[VOICE TEST] Phase 2: Playing back 11s to [{safe_out_idx}] {out_info['name']}...")
            if out_channels == 2:
                playback_data = np.column_stack((full_audio, full_audio))
            else:
                playback_data = full_audio

            sd.play(playback_data, samplerate=SAMPLE_RATE, device=safe_out_idx, extra_settings=out_extra_settings)

            # Countdown playback (11s)
            for sec in range(11, 0, -1):
                if not self._app_running or self._cancel_voice_test:
                    sd.stop()
                    break
                self.set_status_pill(f"Voice Test: Playing back clean voice... ({sec}s left)", COL_ACCENT)
                self._vt_progress = (11 - sec) / 11.0
                self._set_button_label("btn_voice_test", f"Playing back... ({sec}s)")
                time.sleep(1.0)
            sd.stop()

            # Phase 3: Immediate Purge
            del full_audio
            del playback_data
            del processed_chunks
            print("[VOICE TEST] Phase 3: RAM buffer purged. Zero disk clutter.")
            self.set_status_pill("Voice test complete. Clean audio verified", COL_ACCENT)

        except Exception as e:
            print(f"[VOICE TEST] Error during voice preview: {e}")
            reason = self._format_audio_error(e)
            self.set_status_pill(reason if recording else f"Voice test could not start: {reason}", COL_ERROR)
        finally:
            if rn is not None:
                try:
                    rn.close()
                except Exception:
                    pass
            self._is_testing_voice = False
            self._set_voice_test_button_state(True)
            self._vt_active = False
            self._vt_progress = 0.0
            self._release_state_lock()

    def _set_voice_test_button_state(self, enabled: bool):
        """Enables or disables Hear myself. Disabled while the cable is missing or during a voice test."""
        if not self._ui_built or not dpg.does_item_exist("btn_voice_test"):
            return
        # The voice test records the microphone and plays it back on the speakers; it never uses the cable.
        installing = self._install_state == "installing"
        if installing:
            reason = "Wait for the cable install to finish."
        elif not enabled:
            reason = "A voice test or a start is already running."
        else:
            reason = "Record 11 seconds, then hear how others hear you"
        enabled = enabled and not installing
        self._set_button_enabled("btn_voice_test", enabled)
        self._set_button_label("btn_voice_test", "Hear myself")
        self._set_button_tip("btn_voice_test", reason)
