# Changelog

All notable changes to **Getsu (AI Real-Time Noise Cancellation)** are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

---

## [1.2.4] - 2026-10-04

### Fixed & Enhanced
- **Zero Audio Output Interference**: Eliminated all playback device tampering across the entire app lifecycle. Removed background playback guards (`_check_playback_guard` in `src/gui.py`) and installer overrides (`--ensure-physical` in `setup.iss`). Getsu now strictly operates on audio input (`eCapture`), leaving whatever playback endpoint is chosen by Windows or the user (USB/Wireless headsets, Bluetooth, DACs, HDMI, speakers) 100% untouched during installation, launch, stream start, stream stop, and exit.
- **Direct WASAPI Headset Playback for Voice Test**: Fixed `get_physical_output_device()` in `src/devices.py` to query the active Windows WASAPI default playback device directly. "Hear Myself (11s Voice Test)" now reliably plays clean audio into your active USB/Wireless headset without falling back to motherboard Realtek speakers.
- **1-Frame (10ms) Pre-roll Lookahead Buffer**: Added an ultra-low-latency 10ms pre-roll lookahead buffer to `AdaptiveNoiseGate`. When speech begins after long silence, the gate ramps open smoothly across the pre-roll frame *before* the syllable peaks, passing your natural voice with 100% full fidelity and zero muffling, zero harshness, and zero perceptible delay.
- **S-Curve (Raised-Cosine / Hann) Gain Ramping**: Upgraded gate transition interpolation in `AdaptiveNoiseGate` to a precomputed Raised-Cosine S-curve lookup table. Guarantees zero derivative ($C^1$ smooth) at buffer boundaries, 100% eliminating transient clicks and pop sounds while running 3x faster than linear interpolation.
- **Synchronized Voice Test Pipeline**: Synced all DSP parameters (`hangover_ms=360`, `decay_ms=80`, `attack_ms=15`, `cold_start_gain=0.35`, `lookahead=True`) in the Voice Test worker with the live stream pipeline.
- **Enhanced Background Voice Rejection (`onset_snr_db = 6.0 dB`)**: Raised onset threshold to require a 2.0x amplitude jump over ambient room noise, blocking distant background chatter while keeping close-proximity voice responsive.
- **Seamless Config Migration**: Added automatic legacy config upgrade in `src/config.py` that gracefully migrates older `config.json` files to the smooth v1.2.4 gate parameters.

---

## [1.2.3] - 2026-10-04

### Fixed & Enhanced
- **Installer & GUI Headset Auto-Selection**: Added capture endpoint preservation (`--backup-capture` and `--restore-capture`) in `setup.iss` and `AudioRestore.cs` so driver installation never resets Windows default recording endpoint to motherboard Realtek. Connected `src/gui.py` directly to `auto_select_input_device()` so USB/Wireless gaming headsets (MPOW, HyperX, Razer, Logitech, Corsair, SteelSeries, etc.) are accurately auto-selected on first launch.
- **Sample-Accurate Slew Ramping**: Replaced block gain stepping with per-sample linear interpolation (`np.linspace`) across the 480-sample frame during gain transitions in `AdaptiveNoiseGate`. Completely eliminates micro-square-wave transient clicks and harshness on speech onset.
- **Enhanced Consonant Sensitivity (`onset_snr_db = 4.0 dB`)**: Optimized the energy rise threshold over ambient room noise from 7.0 dB to 4.0 dB (2x more sensitive to speech power), enabling gentle whispering and soft unvoiced consonants (*"h"*, *"s"*, *"p"*, *"t"*) to wake up the gate instantly without being muffled.

---

## [1.2.2] - 2026-10-04

### Added
- **Dual-Key Onset Trigger (`AdaptiveNoiseGate`)**: Dynamically tracks ambient background noise floor (-60 dB to -20 dB) during silence. If speech probability exceeds `0.30` and incoming energy rises `+7.0 dB` above ambient room noise, the gate opens instantly on the very first 10ms frame, eliminating swallowed or muffled unvoiced consonants (*"h"*, *"s"*, *"p"*, *"t"*, *"k"*) when breaking long dead silence.
- **Instant Cold-Start Gain (`cold_start_gain = 0.70`)**: Snaps gate gain directly to 0.70 (-3 dB) from silence, ensuring full initial consonant clarity while maintaining smooth zero-click transitions.
- **Extended Natural Speech Hangover (`360.0ms`)**: Broadened gate hold time to 360ms to preserve soft conversational breathing, natural pauses, and subtle word endings without premature gating.
- **Studio-Calibrated Exponential Decay (`80.0ms`)**: Tuned natural fade-out curve (decaying to absolute zero in ~350ms) to prevent abrupt sound cutting while blocking mechanical keyboard clatter.

---

## [1.2.1] - 2026-10-04

### Fixed
- **Realtek & Intel Driver Kernel Streaming Error (`GLE 0x492`)**: Filtered out obsolete and buggy `Windows WDM-KS` (Kernel Streaming) devices from audio device enumeration. Modern Realtek High Definition and DCH drivers reject low-level WDM-KS pin property IOCTLs, causing `PaErrorCode -9999 (WdmSyncIoctl DeviceIoControl GLE = 0x00000492)`.
- **Dual-Sided Host API Alignment & Self-Healing**: Enforced strict Host API matching between input and output devices (`find_matching_cable_input()`), ensuring that WASAPI microphones are paired exclusively with the WASAPI endpoint of VB-Cable (`CABLE Input`). Added input-side realignment and emergency retry self-healing in `AudioEngine.start()` to prevent `Illegal combination of I/O devices [PaErrorCode -9993]`.
- **Speech Onset Clarity After Long Silence**: Tuned default voice sensitivity threshold to `0.70` (from `0.75`) and tightened noise gate attack time to `8.0ms` (from `15.0ms`), eliminating muffled or swallowed consonant attacks (*"s"*, *"t"*, *"k"*, *"p"*) when speaking again after prolonged quiet.
- **Installer Multi-Instance Prevention**: Added kernel mutex guards (`SetupMutex` and `AppMutex`) in Inno Setup to prevent duplicate installer windows and file-lock collisions when users double-click the setup executable.
- **Clean Initial Device Auto-Detection**: Reset distributed `config.json` template device fields to `null`, ensuring new installations immediately run the 4-tier hardware auto-detection on first launch (prioritizing active USB/wireless headsets over motherboard Realtek jacks).
- **Human-Readable Error Translation**: Replaced raw C library PortAudio stack dumps in the GUI status pill with plain-English guidance for driver conflicts, exclusive-mode locks, and device disconnects.

---

## [1.2.0] - 2026-10-01

### Added
- **Smart Microphone Routing (Auto-Swap & Restore)**: Automatically swaps the Windows default recording device (`eCapture`) to `CABLE Output` upon clicking START, piping noise-free voice directly into Discord, Zoom, and Steam without manual settings changes.
- **Direct In-Process CoreAudio COM Routing (`src/router.py`)**: Sub-millisecond (<1ms) Windows audio endpoint hot-swapping implemented directly in-process via `ctypes` (`IPolicyConfig`, `IMMDeviceEnumerator`), eliminating child process latency, antivirus delays, and UAC elevation barriers.
- **5-Vector Audio Restoration Architecture**: Guarantees physical microphones are seamlessly restored across all stop and exit vectors: GUI Stop button, System Tray exit, Window close, OS shutdown (`WM_ENDSESSION`), and Watchdog emergency auto-restore.
- **"Hear Myself" 11-Second Voice Preview Test**: In-memory test recording and playing back 11 seconds of filtered voice with physical speakers muted during recording (zero acoustic feedback loop) and zero disk clutter.
- **Real-Time Voice Sensitivity Threshold Slider**: Thread-safe live float slider (`0.40 - 0.90`) for fine-tuning voice detection vs. background noise in real-time.
- **Crash-Proof Atomic Configuration Management (`src/config.py`)**: Replaced truncating file writes with temporary file replacement and exponential backoff retry for transient Windows Defender file inspection locks (`WinError 32`), with `%APPDATA%\Getsu` storage for installed mode.
- **Inno Setup Upgrade & Uninstaller Hardening (`setup.iss`)**: Proactively terminates active instances and restores physical audio before installation/removal, eliminating Restart Manager prompts and locked file deletion errors.
- **Expanded Automated Test Suite**: Added 17 new unit and integration tests (32 total), validating CoreAudio COM swapping, atomic configuration, and crash recovery.

### Fixed
- **C# COM Interop Stack Misalignment (`AudioRestore.cs`)**: Added missing `[PreserveSig]` attributes to `IPolicyConfigWin10`, fixing stack corruption and silent failures during CoreAudio endpoint switching.
- **COM Apartment Mode Compatibility**: Switched from `ctypes.oledll` to `ctypes.windll.ole32` with safe multi-threaded apartment (`COINIT_MULTITHREADED`) initialization, preventing `RPC_E_CHANGED_MODE` exceptions on background worker threads.
- **Teardown Lifecycle Sequence**: Ensured background threads and audio streams are fully stopped and restored before destroying the DearPyGui context.
- **Stale Named Mutex False-Positive**: Verified active window handle (`HWND`) exists before assuming an instance is running, resolving startup failure after task termination.

---

## [1.1.0] - 2026-09-29

### Added
- **Dynamic Device Hotplug & Auto-Fallback**: Automatically detects USB/Bluetooth headset disconnects (e.g., MPOW) and seamlessly switches to the Windows default microphone without halting audio playback.
- **Stream Health Watchdog**: Monitors duplex audio pipeline health every 1.0s and auto-recovers if a USB connection is abruptly severed.
- **Persistent Device Identity by Name**: Replaced brittle integer index checks with clean device labels (`selected_input_name`, `selected_output_name`), resolving the PortAudio index-shifting trap.
- **Expanded Microphone Brand Discovery**: Added automatic high-priority recognition for Razer, Logitech, Blue Yeti, Elgato, Rode, Fifine, HyperX, SteelSeries, Sennheiser, Audio-Technica, and prioritized Windows WASAPI default endpoints.
- **Output Destination Auto-Validation**: Auto-validates output monitor device if a USB headset is unplugged, cleanly falling back to laptop speakers or VB-Cable.
- **Configurable DSP Parameters**: Added `vad_close_threshold` (0.45), `output_gain` (1.08), and `hpf_cutoff_hz` (80.0) to `DEFAULT_CONFIG`.
- **Pre-RNNoise Overdrive Protection**: Clamps normalized input to `[-1.0, 1.0]` after mic boost to prevent RNNoise neural network saturation.
- **Stereo Microphone Downmixing**: Added safe multi-channel array summing for stereo gaming headsets with 1D/2D dimensional guards.
- **Bypass Mode Energy Gating**: Background hiss is gated even when AI denoise is disabled.
- **Clean Subclassing Teardown (`WM_NCDESTROY`)**: Unhooks the window subclass procedure cleanly on Win32 message `0x0082`.
- **HWND Polling Loop**: Added a 40-iteration polling loop (2.0s) to reliably find and subclass the DearPyGui window handle across monitor DPI scales.
- **Named Mutex Graceful Release**: Mutex handle properly closed on app exit via `atexit.register(release_instance)`.
- **PowerShell Apostrophe Sanitization**: Escaped single quotes (`.replace("'", "''")`) in VB-Cable driver setup to support Windows usernames with apostrophes (e.g., `O'Connor`).

### Changed
- **Zero-Allocation 80Hz Rumble Filter**: Direct Form I Butterworth filter rewritten with pre-allocated buffer and fast local variable caching — dropping latency from 181 µs to **117 µs** (35% speedup, 0 heap allocations).
- **15 ms Soft-Attack Noise Gate**: Exponential envelope ramp (`attack_rate = 1.0 - math.exp(-10/15)`) eliminates audio clicks and popping when speech starts.
- **Post-RNNoise Make-up Gain Compensation**: Added `output_gain = 1.08` (+0.7 dB) to restore natural vocal warmth after neural noise suppression.
- **Dual-Stage Metering**: Independent pre-gate and post-gate level calculations.
- **Atomic Metrics Swap**: Live UI meters now update via atomic 4-tuple swap, eliminating thread-safety race conditions between audio threads and GUI rendering.
- **BLAS-Optimized RMS Calculation**: Replaced `np.sqrt(np.mean(frame**2))` with `np.dot(frame, frame)` via SIMD BLAS, eliminating 100 temporary heap allocations per second.
- **Unified Engine Builder**: Centralized `create_engine_from_config()` with automatic laptop microphone detection (`realtek`, `array`, `built-in`) across GUI, Tray, and CLI modes.

### Fixed
- **Level Meter Inversion Bug**: Fixed dBFS reference bug in `calculate_levels()` where peaks > 1.0 caused meter dropouts to false -90 dBFS silence.
- **PortAudio Stream Reinitialization**: Eliminated `sd.PortAudioError` crashes when unplugging or re-plugging USB headsets during active streaming.
- **Cross-Thread Window Destruction**: Removed `user32.DestroyWindow` from tray thread to eliminate double-free crashes with DearPyGui.
- **RNNoise C-Contiguity**: Enforced `np.ascontiguousarray` before passing raw memory pointers to ctypes native C functions.

---

## [1.0.0] - 2026-09-20

### Added
- Real-time AI noise cancellation powered by RNNoise neural network (10ms frames @ 48kHz).
- 80Hz rumble cut filter for laptop cooling pad fan vibration.
- Adaptive soft-knee noise gate with hangover.
- VB-Audio Virtual Cable bridge for routing denoised audio into Steam, Zoom, and Discord.
- Single-instance Named Mutex with instant window restore.
- Sleek DearPyGui dark mode dashboard with DPI monitor awareness.
- Windows System Tray integration with dynamic status icon.
