# Changelog

All notable changes to **Getsu (AI Real-Time Noise Cancellation)** are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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
