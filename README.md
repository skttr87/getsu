# Getsu — Real-Time AI Noise Cancellation

<p align="center">
  <img src="https://raw.githubusercontent.com/skttr87/getsu/main/assets/getsu-ui.png" alt="Getsu Interface" width="450" />
</p>

<p align="center">
  <strong>Free, open-source real-time AI noise cancellation for Windows.</strong><br>
  A lightweight Krisp & NVIDIA Broadcast alternative for Steam, Discord, and Zoom.<br>
  <em>100% offline • CPU-only • Zero telemetry • Low latency</em>
</p>

<p align="center">
  <a href="https://github.com/skttr87/getsu/releases/latest"><img src="https://img.shields.io/github/v/release/skttr87/getsu?style=for-the-badge&color=28a745&logo=windows" alt="Latest Release" /></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/License-MIT-blue?style=for-the-badge" alt="License" /></a>
  <img src="https://img.shields.io/badge/Platform-Windows_10%2F11-0078D6?style=for-the-badge&logo=windows" alt="Platform" />
  <img src="https://img.shields.io/badge/Inference-1.1ms%20%2F%2010ms-brightgreen?style=for-the-badge" alt="Latency" />
</p>

---

> [!NOTE]
> **Getsu** is derived from two Acehnese words: **get**, which means *"good"*, and **su**, which means *"sound"*.

---

## ☕ Sponsored

<p align="center">
  <a href="https://www.instagram.com/kopireman/" target="_blank">
    <img src="https://raw.githubusercontent.com/skttr87/getsu/main/assets/kopi-reman.png" alt="Kopi Reman" width="160" />
  </a>
  <br>
  <strong><a href="https://www.instagram.com/kopireman/" target="_blank">Kopi Reman</a></strong>
</p>

---

## ⚡ Why Getsu?

Commercial noise cancellation tools often force subscriptions, burn dedicated GPU VRAM, or require always-on telemetry:

| Feature | **Getsu** | **Krisp** | **NVIDIA Broadcast** | **SteelSeries Sonar** |
|---|:---:|:---:|:---:|:---:|
| **Price** | **100% Free** | $96 / year (or 60 min/day) | Free (Requires RTX GPU) | Free |
| **Hardware** | **Any CPU (No GPU needed)** | Any CPU | Requires RTX 2060+ (~1.5GB VRAM) | Any CPU |
| **Privacy** | **100% Offline (Zero telemetry)** | Online accounts required | Telemetry bundled | Account & bloatware |
| **Size** | **~5.6 MB Executable** | >150 MB | >1 GB installer | >300 MB |
| **Cooling Pad Filter** | **Built-in 80Hz Biquad Cut** | General AI only | General AI only | Manual EQ |
| **USB Disconnect Recovery** | **Auto-Fallback to Default** | Call drops / freezes | May crash audio | Inconsistent |

---

## ✨ Features

- **RNNoise Neural Filter**: Real-time recurrent neural network (RNN) suppression processing 480-sample (10ms @ 48kHz) audio frames with imperceptible latency (<1.2ms inference).
- **80Hz Cooling Pad Rumble Filter**: Hardware-optimized 2nd-order Butterworth high-pass filter that cuts heavy laptop fan rumble and desk thumps before they reach the neural model.
- **Dynamic Device Hotplug & Auto-Fallback**: Seamlessly recovers if a USB or Bluetooth headset (e.g. MPOW, Razer, Logitech) is unplugged during a live call by automatically switching to the Windows default microphone.
- **Stream Health Watchdog**: Monitors real-time audio pipeline health every 1.0s and auto-recovers from abrupt driver or USB disconnections.
- **15ms Soft-Attack Adaptive Noise Gate**: Smooth exponential envelope eliminates clicks and pops on speech onset while maintaining a 180ms hangover to preserve word endings.
- **Universal Microphone Discovery**: Automatically prioritizes external headsets and studio mics (Razer, Logitech, Blue Yeti, Rode, Elgato, Fifine, HyperX, SteelSeries) or Windows default WASAPI communication endpoints.
- **1-Click Virtual Audio Cable Bridge**: Seamlessly routes clean voice into **Steam Voice Chat**, **Discord**, **Zoom**, **OBS**, and **Google Meet**.
- **Compact & Modern GUI**: Sleek dark-mode interface built with DearPyGui, featuring dynamic monitor DPI awareness and system tray minimize.

---

## 📥 How to use

1. **Download Installer**: Download **[`Getsu-v1.1.0-Setup.exe`](https://github.com/skttr87/getsu/releases/latest)**.
2. **Run Setup**:
   - Launch the installer and choose your destination folder (e.g., `C:\Program Files\Getsu` or `D:\Getsu`).
   - The installer automatically configures the **VB-Audio Virtual Cable** driver in the background and preserves your physical speakers/headphones as the default Windows sound device.
3. **Launch Getsu**:
   - Open **Getsu** from your Desktop or Start Menu.
   - Choose your input **Microphone** and click **START**.
4. **Route Your Voice**:
   - In Discord, Steam, or Zoom, set your input device to:  
     👉 **`CABLE Output (VB-Audio Virtual Cable)`**
5. **Disable In-App Noise Filters**: Turn off Discord Krisp or Zoom background noise suppression to prevent double-filtering.

> [!IMPORTANT]
> **Windows Audio Playback**: Keep your computer's main sound output set to your real **Speakers or Headphones** (the volume icon in your Windows taskbar). **`CABLE Output`** should only be selected as your **Microphone** inside voice apps (Discord, Steam, Zoom).

---

## 🎧 Audio Processing Pipeline

```
[ Physical Microphone / Headset ]
               │
               ▼
[ 1. Input Gain & Overdrive Limiter ]  (Clamps [-1.0, 1.0] after boost)
               │
               ▼
[ 2. 80Hz Rumble Filter (HPF) ]        (Cuts laptop fan / desk vibration)
               │
               ▼
[ 3. RNNoise Neural Suppression ]      (Removes background noise in 1.1ms)
               │
               ▼
[ 4. Make-Up Gain (+0.7 dB) ]          (Restores natural speech warmth)
               │
               ▼
[ 5. Adaptive Soft-Knee Gate ]         (Clamps room hiss between sentences)
               │
               ▼
[ CABLE Input (VB-Audio Virtual Cable) ] ──► [ Discord / Steam / Zoom ]
```
---

## 🛠️ Building & Running from Source

### Prerequisites
- Windows 10 or 11 (64-bit)
- Python 3.10+

### Installation

```powershell
# 1. Clone the repository
git clone https://github.com/skttr87/getsu.git
cd getsu

# 2. Create and activate a virtual environment
python -m venv venv
.\venv\Scripts\activate

# 3. Install dependencies
pip install -r requirements.txt

# 4. Launch Getsu
python -m src.main
```

### Running Tests

```powershell
python -m unittest discover -s tests -v
```

### Compiling Executable

```powershell
python -m PyInstaller --clean --noconfirm build/getsu.spec
```
The compiled portable application will be placed in `dist/getsu/`.

---

## ⚙️ Configuration (`config.json`)

Getsu automatically saves your settings in `config.json` next to the executable:

```json
{
  "app_name": "Getsu",
  "version": "1.1.0",
  "denoise_enabled": true,
  "high_pass_filter": true,
  "hpf_cutoff_hz": 80.0,
  "vad_threshold": 0.75,
  "vad_close_threshold": 0.45,
  "vad_hangover_ms": 180.0,
  "output_gain": 1.08,
  "mic_boost_db": 0,
  "mute": false
}
```

---

## 📜 Credits & Acknowledgments

- **[RNNoise](https://github.com/xiph/rnnoise)**: Recurrent neural network for audio noise reduction by Jean-Marc Valin (Xiph.Org / Mozilla).
- **[VB-Audio](https://vb-audio.com/Cable/)**: High-fidelity virtual audio cable driver.
- **[DearPyGui](https://github.com/hoffstadt/DearPyGui)**: Fast, GPU-accelerated immediate-mode Python GUI framework.
- **[Icons8](https://icons8.com/)**: Application iconography and UI assets.

---

## 📄 License

This project is licensed under the [MIT License](LICENSE).
