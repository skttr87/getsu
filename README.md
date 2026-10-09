# Getsu — Real-Time AI Noise Cancellation

<p align="center">
  <img src="https://raw.githubusercontent.com/skttr87/getsu/main/assets/getsu-ui.png" alt="Getsu Interface" width="460" />
</p>

<p align="center">
  <strong>Real-time AI noise cancellation for Windows.</strong><br>
  Filters out fan roar, mechanical keyboards, room noise, and distant chatter directly on your CPU — free, offline, and lightweight.<br>
  <em>Works with Discord, Zoom, Steam, OBS, and all PC games.</em>
</p>

<p align="center">
  <a href="https://github.com/skttr87/getsu/releases/latest"><img src="https://img.shields.io/github/v/release/skttr87/getsu?style=for-the-badge&color=28a745&logo=windows" alt="Latest Release" /></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/License-MIT-blue?style=for-the-badge" alt="License" /></a>
  <img src="https://img.shields.io/badge/Platform-Windows_10%2F11-0078D6?style=for-the-badge&logo=windows" alt="Platform" />
  <img src="https://img.shields.io/badge/100%25-Free%20%26%20Offline-brightgreen?style=for-the-badge" alt="Privacy" />
</p>

---

> [!NOTE]
> **Getsu** comes from two Acehnese words: **get** (*"good"*) and **su** (*"sound"*).

---

## ⚡ Why Getsu?

Commercial noise cancellation tools often charge monthly subscriptions, drain your graphics card (GPU), or track your data:

| Feature | **Getsu** | **Krisp** | **NVIDIA Broadcast** |
|---|:---:|:---:|:---:|
| **Price** | **100% Free Forever** | $96 / year (or 60 min limit) | Free (Requires RTX GPU) |
| **Requirements** | **Any PC or Laptop (CPU only)** | Any PC | Expensive RTX Graphics Card |
| **Privacy** | **100% Offline (Zero accounts/tracking)** | Online accounts required | Telemetry bundled |
| **Download Size** | **~35 MB** | >150 MB | >1,000 MB (1 GB+) |
| **Gaming Friendly** | **Near-zero latency (won't drop game FPS)** | High memory usage | Heavy GPU VRAM usage |

---

## 🛡️ What Getsu Blocks

Getsu combines an **80Hz Butterworth rumble filter**, the **Mozilla RNNoise neural network**, and an **Adaptive S-Curve Noise Gate** with lookahead to silence domestic and gaming noise directly on your CPU:

| Noise Type | Real-World Examples | How Getsu Eliminates It |
|---|---|---|
| **Stationary Background Noise** | Laptop cooling pads, GPU/CPU fan roar, air conditioning | **RNNoise Neural Spectral Subtraction** (100% suppressed on CPU) |
| **Low-End Physical Rumble** | Desk typing vibrations, floor footsteps, 50/60Hz AC hum | **80Hz High-Pass Filter** (eradicates sub-audible energy before AI) |
| **Sharp Transient Clicks** | Mechanical keyboards (Blue/Brown/Red switches), mouse clicks | **S-Curve Noise Gate + 10ms Pre-Roll** (smooth zero-jerk ramping) |
| **Far-Field Domestic Chatter** | Family talking in next room, background living room TV | **10 dB Onset SNR Bar** (requires near-field proximity energy to open) |
| **Sentence-Ending Chatter** | Voices trailing off while background noise continues | **150ms Re-Arm Cooldown Holdoff** (blocks decay-abort clicks) |

---

## 🚀 Quick Start (3 Steps)

### 1. Download & Install
[![Download Latest Release](https://img.shields.io/github/v/release/skttr87/getsu?label=Download%20Latest%20Installer&color=28a745&style=flat-square&logo=windows)](https://github.com/skttr87/getsu/releases/latest)  
Download and run the latest **[Getsu Setup Installer (Releases)](https://github.com/skttr87/getsu/releases/latest)**.  
*(The installer automatically sets up the required virtual audio driver in the background).*

### 2. Choose Your Microphone
Open Getsu from your Desktop or Start Menu and select your microphone from the dropdown list.

### 3. Click START
Click the large green **START** button. That's it!  
Getsu will automatically route your clean, noise-free microphone to Discord, Steam, Zoom, and your games.

> [!TIP]
> **Want to test how you sound?**  
> Click **"Hear Myself (11s Voice Test)"** while stopped. Speak into your mic, and Getsu will play back 11 seconds of your clean, noise-filtered voice!

### 💡 What if an app is still hearing noise? (e.g. Steam or Discord)
Getsu automatically routes clean audio to any app set to your **"Default"** microphone. If an app still hears background noise, it is usually because that app was previously locked to your raw physical mic:

* **Steam**: Go to **Settings** ➔ **Voice** ➔ Set **Voice Input Device** to **`Default`** (or **`CABLE Output (VB-Audio Virtual Cable)`**). *(Tip: Turn off Steam's built-in "Noise Cancellation" to avoid double-processing).*
* **Discord**: Go to **User Settings** (⚙️) ➔ **Voice & Video** ➔ Set **Input Device** to **`Default`** (or **`CABLE Output (VB-Audio Virtual Cable)`**). *(Tip: Turn off Discord's Krisp filter).*
* **PC Games (CS2, Valorant, Apex, CoD)**: Ensure in-game audio settings use **`Default System Device`** or **`CABLE Output`**.

---

## ✨ Key Features

- 🧠 **AI Voice Isolation**: Silences noisy mechanical keyboards, desk fans, air conditioners, and background chatter.
- 🎯 **Smart Auto-Routing**: Automatically connects to your voice apps when you click START, and safely restores your normal mic when you click STOP or close the app.
- 💨 **Ultra-Low Latency**: Lightning-fast processing (<1.2ms) so your voice never lags behind your gameplay.
- 🎧 **"Hear Myself" Preview**: Test your voice in 11 seconds without creating screeching feedback loops or annoying your friends in a live call.
- 🎚️ **Sensitivity Slider**: Easily adjust how sensitive Getsu is to quiet voices vs. background noise.
- 🔌 **Plug & Play Recovery**: Unplugged your USB headset by accident? Getsu automatically switches to your backup mic without dropping your call.
- 🖥️ **Looks Crisp Everywhere**: Automatically scales and sharpens on laptops, standard monitors, and 4K screens.

---

## 💻 Headless Audio Batch Cleaner (`getsu-cli`)

Getsu includes a dedicated console companion tool, **`getsu-cli.exe`**, designed for synchronous command-line processing, audio batch cleaning, podcast post-production, and automated CI pipelines.

> [!TIP]
> **Independent Execution**: `getsu-cli` runs synchronously in any terminal (PowerShell, Command Prompt, Windows Terminal) and can process files in the background even while the Getsu desktop application (`getsu.exe`) is actively running.  
> *(If you selected "Add Getsu CLI to system PATH" during installation, you can run `getsu-cli` from any directory).*

### Common CLI Commands

#### 1. Clean a Single Audio File
```powershell
# Automatically outputs to recording_cleaned.wav
getsu-cli --clean recording.wav

# Or specify a custom output path
getsu-cli --clean recording.wav D:\audio\cleaned.wav
```

#### 2. Clean an Entire Folder / Directory
Processes all audio files (`.wav`, `.mp3`, `.flac`, `.ogg`) inside the directory:
```powershell
getsu-cli --clean D:\recordings\podcast\
```

#### 3. Linked-Stereo Gating (`--stereo`)
For multi-channel podcasts and stereo recordings. Tracks a synchronized master energy envelope across Left and Right channels, eliminating stereo panning image wobble:
```powershell
getsu-cli --clean interview_stereo.wav --stereo
```

#### 4. Music & Performance Mode (`--music-mode`)
Bypasses the neural model and extends gate sustain to 800ms with a gentle 250ms decay, preserving natural room decay and instrument/singing vibrato:
```powershell
getsu-cli --clean vocal_take.wav --music-mode
```

#### 5. Makeup Gain & Voiced Speech Leveling
```powershell
# Add +4.0 dB of clean makeup gain
getsu-cli --clean quiet_mic.wav --gain 4.0

# Automatically normalize quiet speaker levels toward -24 dBFS
getsu-cli --clean quiet_mic.wav --auto-level
```

### CLI Options & Flags Reference

| Option / Flag | Description |
| :--- | :--- |
| `--clean <path>` | Path to input audio file or folder to clean (*required*). |
| `[output_path]` | Destination WAV path or directory (*optional, defaults to `[name]_cleaned.wav`*). |
| `--stereo` | Coupled linked-stereo gating for multi-channel audio files. |
| `--music-mode` | Bypasses RNNoise and extends hangover to 800ms for music/singing. |
| `--auto-level` | Dynamically normalizes quiet speech levels toward $-24\text{ dBFS}$. |
| `--gain <dB>` | Post-processing makeup gain boost in decibels (e.g. `--gain 3.0`). |
| `--no-rnnoise` | Bypass neural noise reduction (uses 80Hz rumble filter and noise gate only). |
| `--no-gate` | Bypass noise gate (uses neural noise reduction only). |
| `--report-json <file>` | Exports frame-by-frame telemetry JSON log for automated acoustic profiling. |
| `--no-progress` | Suppresses the dynamic console progress bar (ideal for headless CI/CD scripts). |
| `--no-stats` | Suppresses the terminal summary statistics table. |
| `--check-devices` | Prints system audio endpoints and diagnostics report. |

---

## ⚠️ Limits & Transparency Notes

Every software has engineering trade-offs. Here is what to expect upfront:

- **Windows Only**: Designed for Windows 10 and Windows 11 (64-bit). There are currently no macOS or Linux builds.
- **Virtual Audio Driver Required**: Relies on VB-Audio Virtual Cable to bridge the clean audio output to your games and chat applications (the official installer installs this for you automatically).
- **Resource Footprint**: Adds <1.2ms processing latency and consumes ~1.5% CPU on a standard quad-core CPU. **0% GPU usage** (no CUDA/RTX requirements).
- **Unsigned SmartScreen Warning**: Getsu is independent open-source software and does not carry an expensive commercial code-signing certificate ($300+/year). Windows SmartScreen may show an *"Unknown Publisher"* warning. Click **More info** ➔ **Run anyway**. All source code is completely public and SHA-256 installer hashes are posted on every [GitHub Release](https://github.com/skttr87/getsu/releases).

---

## ❓ Frequently Asked Questions

<details>
<summary><strong>How is this different from Discord's Krisp?</strong></summary>

> Krisp only cleans your microphone *inside* Discord voice channels. Getsu cleans your microphone at the operating system level, protecting **Discord, Steam, Zoom, Google Meet, OBS Studio, and games (CS2, Valorant, Dota 2, Apex)** simultaneously with zero time limits and zero subscriptions.
</details>

<details>
<summary><strong>Do I need an NVIDIA RTX graphics card?</strong></summary>

> No. Getsu's DSP pipeline runs 100% on standard x86 CPU instructions. It works smoothly on Intel, AMD, and budget integrated-GPU laptops without hogging graphics VRAM or compute cores.
</details>

<details>
<summary><strong>Will Getsu affect my in-game FPS?</strong></summary>

> Virtually not at all. Because Getsu does not touch your GPU and only uses ~1.5% CPU time, your graphics card remains 100% dedicated to rendering game frames.
</details>

<details>
<summary><strong>Does it really block mechanical keyboard switches and clicks?</strong></summary>

> Yes! While standalone RNNoise models struggle with sharp mechanical clatter, Getsu pairs RNNoise with an adaptive S-curve noise gate and a 10ms lookahead pre-roll buffer. Keystrokes, mouse clicks, and desk bumps are silenced without chopping off the first syllable of your words.
</details>

<details>
<summary><strong>Is Getsu private and offline?</strong></summary>

> 100% yes. All audio processing runs locally on your machine. Getsu never saves your voice to disk, transmits zero audio over the network, has no analytics tracking, and requires no account. You can audit the entire source code in this repository.
</details>

---

## ☕ Sponsored

<p align="center">
  <a href="https://www.instagram.com/kopireman/" target="_blank">
    <img src="https://raw.githubusercontent.com/skttr87/getsu/main/assets/kopi-reman.png" alt="Kopi Reman" width="150" />
  </a>
  <br>
  <strong><a href="https://www.instagram.com/kopireman/" target="_blank">Kopi Reman</a></strong>
</p>

---

<details>
<summary><strong>🛠️ For Developers & Advanced Users (Click to expand)</strong></summary>

### Audio Processing Pipeline
```
[ Microphone ] ──► [ 80Hz Fan Rumble Filter ] ──► [ RNNoise AI Engine ] ──► [ Noise Gate ] ──► [ CABLE Output ]
```

### Building from Source
```powershell
# 1. Clone repository
git clone https://github.com/skttr87/getsu.git
cd getsu

# 2. Set up virtual environment
python -m venv venv
.\venv\Scripts\activate
pip install -r requirements.txt

# 3. Launch or test
python -m src.main
python -m unittest discover tests
```

### Compiling Standalone Executable
```powershell
python -m PyInstaller --clean --noconfirm build/getsu.spec
```
</details>

---

## 📜 Credits & License

- **[RNNoise](https://github.com/xiph/rnnoise)**: Recurrent neural network architecture for audio noise reduction by Jean-Marc Valin (Xiph.Org / Mozilla).
- **[pyrnnoise](https://github.com/pengzhendong/pyrnnoise)**: Precompiled Windows x64 native binary distribution by Zhendong Peng.
- **[VB-Audio](https://vb-audio.com/Cable/)**: High-fidelity virtual audio cable driver.
- **[DearPyGui](https://github.com/hoffstadt/DearPyGui)**: Fast GPU-accelerated immediate-mode GUI engine.

This project is open-source under the **[MIT License](LICENSE)**.
