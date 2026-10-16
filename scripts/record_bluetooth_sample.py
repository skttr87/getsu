"""
Bluetooth Headset 30-Second Voice Recorder & Acoustic Sampler.
Captures clean, uncompressed WAV audio from connected Bluetooth headsets
with live VU level monitoring, acoustic telemetry, and optional playback check.
"""
import os
import sys
import time
import wave
import datetime
import argparse
from typing import Optional, Tuple, Dict, List

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

import numpy as np
import sounddevice as sd

# Default duration in seconds
RECORD_DURATION = 30.0
BLOCK_DURATION = 0.1  # 100ms per block for smooth live VU metering
OUTPUT_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "sound_samples")


def find_bluetooth_input_device() -> Tuple[Optional[int], Optional[Dict]]:
    """
    Scans system audio endpoints and auto-selects the primary Bluetooth headset microphone.
    Prefers Windows WASAPI, with DirectSound/MME fallback.
    """
    devices = sd.query_devices()
    hostapis = sd.query_hostapis()

    keywords = [
        "freebuds", "headset", "bluetooth", "wireless", "hands-free",
        "buds", "airpods", "wh-", "wf-", "sony", "jabra", "bose"
    ]

    candidates = []
    for idx, dev in enumerate(devices):
        if dev["max_input_channels"] <= 0:
            continue
        api_name = hostapis[dev["hostapi"]]["name"] if dev["hostapi"] < len(hostapis) else "Unknown"
        name_lower = dev["name"].lower()

        # Exclude raw WDM-KS kernel streaming devices
        if "WDM-KS" in api_name.upper():
            continue

        if any(k in name_lower for k in keywords):
            # Prioritize WASAPI -> DirectSound -> MME
            prio = 0 if "WASAPI" in api_name.upper() else (1 if "DIRECTSOUND" in api_name.upper() else 2)
            candidates.append((prio, idx, dev, api_name))

    if candidates:
        candidates.sort(key=lambda x: x[0])
        best = candidates[0]
        return best[1], {"device": best[2], "hostapi": best[3]}

    return None, None


def select_device_interactive() -> Tuple[int, Dict]:
    """Prompts user to select from available recording devices if auto-detection misses."""
    devices = sd.query_devices()
    hostapis = sd.query_hostapis()

    valid_inputs = []
    print("\nAvailable Input Devices:")
    for idx, dev in enumerate(devices):
        if dev["max_input_channels"] > 0:
            api_name = hostapis[dev["hostapi"]]["name"] if dev["hostapi"] < len(hostapis) else "Unknown"
            if "WDM-KS" not in api_name.upper():
                valid_inputs.append((idx, dev, api_name))
                print(f"  [{idx:2d}] {dev['name']} ({api_name}) - {dev['max_input_channels']}ch @ {int(dev['default_samplerate'])}Hz")

    while True:
        try:
            choice = input("\nEnter device number to record from: ").strip()
            idx = int(choice)
            for item in valid_inputs:
                if item[0] == idx:
                    return idx, {"device": item[1], "hostapi": item[2]}
            print("Invalid device number. Please choose from the list above.")
        except ValueError:
            print("Please enter a numeric device ID.")


def calculate_db(chunk: np.ndarray) -> float:
    """Calculates RMS level in dBFS."""
    rms = np.sqrt(np.mean(chunk**2))
    if rms <= 1e-7:
        return -96.0
    return float(20.0 * np.log10(rms))


def draw_vu_meter(db: float, width: int = 24) -> str:
    """Renders visual ASCII bar graph for real-time speech level."""
    # Scale: -60 dB to 0 dB
    clamped = max(-60.0, min(0.0, db))
    frac = (clamped + 60.0) / 60.0
    filled = int(round(frac * width))
    bar = "#" * filled + "-" * (width - filled)
    return bar


def main():
    parser = argparse.ArgumentParser(description="Getsu Bluetooth Voice Recorder & Acoustic Sampler")
    parser.add_argument("--duration", type=float, default=15.0, help="Recording duration in seconds (default: 15.0)")
    parser.add_argument("--auto", action="store_true", help="Auto-start recording without waiting for interactive confirmation")
    parser.add_argument("--no-playback", action="store_true", help="Skip interactive playback prompt at the end")
    parser.add_argument("--play", action="store_true", help="Automatically play back recording after capture")
    args = parser.parse_args()

    record_duration = args.duration

    print("=" * 68)
    print(f"  Getsu Bluetooth Voice Recorder & Acoustic Sampler ({record_duration:.0f}s)")
    print("=" * 68)

    # 1. Device Resolution
    dev_idx, dev_meta = find_bluetooth_input_device()
    if dev_idx is not None and dev_meta is not None:
        dev_info = dev_meta["device"]
        api_name = dev_meta["hostapi"]
        print(f"\n[+] Auto-detected Bluetooth Headset:")
        print(f"    Device Name : {dev_info['name']}")
        print(f"    Host API    : {api_name} (Index {dev_idx})")
        print(f"    Native Rate : {int(dev_info['default_samplerate'])} Hz")
        print(f"    Channels    : {dev_info['max_input_channels']}")
        
        if not args.auto:
            confirm = input("\nUse this device? [Y/n]: ").strip().lower()
            if confirm == 'n':
                dev_idx, dev_meta = select_device_interactive()
    else:
        print("\n[!] No Bluetooth headset automatically identified.")
        dev_idx, dev_meta = select_device_interactive()

    dev_info = dev_meta["device"] if isinstance(dev_meta, dict) and "device" in dev_meta else sd.query_devices(dev_idx)
    api_name = dev_meta.get("hostapi", "") if isinstance(dev_meta, dict) else ""
    
    samplerate = int(dev_info["default_samplerate"])
    channels = min(1, dev_info["max_input_channels"])  # Standard mono speech capture

    extra_settings = None
    if "WASAPI" in api_name.upper():
        extra_settings = sd.WasapiSettings(auto_convert=True)

    os.makedirs(OUTPUT_DIR, exist_ok=True)
    timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    clean_name = "".join(c for c in dev_info['name'] if c.isalnum() or c in (' ', '_', '-')).strip().replace(' ', '_')
    output_filename = f"bluetooth_{clean_name}_{timestamp}.wav"
    output_path = os.path.join(OUTPUT_DIR, output_filename)

    print("\n" + "-" * 68)
    print(f"Target File  : {output_path}")
    print(f"Duration     : {record_duration:.1f} seconds")
    print(f"Sample Rate  : {samplerate} Hz (Mono 16-bit PCM)")
    print("-" * 68)

    # 2. Wait for user readiness
    if not args.auto:
        input("\n>>> Put on your Bluetooth headset, then press [ENTER] when ready to record... ")
    else:
        print("\n>>> Bluetooth headset detected. Preparing recording...")

    # 3. 3-Second Visual Countdown
    print("\nStarting in:")
    for count in range(3, 0, -1):
        print(f"   [{count}] ...", flush=True)
        time.sleep(1.0)
    print("   [* RECORDING - Speak naturally into your Bluetooth headset!]\n")

    # 4. Recording Loop with Live Metering
    block_samples = int(samplerate * BLOCK_DURATION)
    total_blocks = int(record_duration / BLOCK_DURATION)
    recorded_blocks: List[np.ndarray] = []

    try:
        with sd.InputStream(
            samplerate=samplerate,
            channels=channels,
            device=dev_idx,
            dtype="float32",
            extra_settings=extra_settings,
            blocksize=block_samples,
        ) as stream:
            start_time = time.time()
            for b_idx in range(total_blocks):
                data, overflowed = stream.read(block_samples)
                audio_mono = data[:, 0] if data.ndim > 1 else data.flatten()
                recorded_blocks.append(audio_mono.copy())

                elapsed = (b_idx + 1) * BLOCK_DURATION
                rms_db = calculate_db(audio_mono)
                vu_bar = draw_vu_meter(rms_db)
                
                status_tag = "Speaking" if rms_db > -35.0 else "Quiet"
                sys.stdout.write(
                    f"\r[{elapsed:4.1f}s / {record_duration:.0f}s] [{vu_bar}] {rms_db:5.1f} dBFS  ({status_tag:8s}) "
                )
                sys.stdout.flush()

        print("\n\n[OK] Recording complete!")
    except KeyboardInterrupt:
        print("\n\n[!] Recording stopped early by user.")
    except Exception as e:
        print(f"\n\n[ERROR] Recording failed: {e}")
        return

    if not recorded_blocks:
        print("[!] No audio data captured.")
        return

    # 5. Concatenate & Audio Analysis
    full_audio = np.concatenate(recorded_blocks, axis=0)
    actual_duration = len(full_audio) / samplerate
    peak_val = np.max(np.abs(full_audio))
    peak_db = 20.0 * np.log10(peak_val) if peak_val > 1e-7 else -96.0
    overall_rms = calculate_db(full_audio)

    print("\n" + "=" * 68)
    print("  Acoustic Telemetry & Signal Quality")
    print("=" * 68)
    print(f"  Duration Captured : {actual_duration:.2f} seconds ({len(full_audio):,} frames)")
    print(f"  Peak Amplitude    : {peak_db:.2f} dBFS (Peak factor: {peak_val:.3f})")
    print(f"  Overall RMS Energy: {overall_rms:.2f} dBFS")
    
    if peak_db >= -0.5:
        print("  [!] Clipping Notice : Audio reached digital ceiling (potential digital clipping).")
    elif overall_rms < -40.0:
        print("  [!] Low Volume      : Voice signal is quiet. Consider boosting microphone gain.")
    else:
        print("  [OK] Signal Quality   : Healthy speech energy profile.")

    # 6. Save to 16-bit PCM WAV File
    pcm_int16 = np.clip(full_audio * 32767.0, -32768, 32767).astype(np.int16)
    with wave.open(output_path, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)  # 16-bit
        wf.setframerate(samplerate)
        wf.writeframes(pcm_int16.tobytes())

    file_size_kb = os.path.getsize(output_path) / 1024.0
    print(f"\n[OK] Saved WAV Sample : {output_path} ({file_size_kb:.1f} KB)")

    # 7. Optional Immediate Playback Check
    try:
        should_play = False
        if args.play:
            should_play = True
        elif not args.no_playback:
            playback_check = input("\nPlay back this recording to verify audio quality? [Y/n]: ").strip().lower()
            should_play = (playback_check != 'n')

        if should_play:
            print("\nPlaying back sample through system default playback device...")
            # If mono, play as 2-channel duplicate for clear stereo listening
            playback_audio = np.column_stack((full_audio, full_audio))
            sd.play(playback_audio, samplerate=samplerate)
            sd.wait()
            print("[✓] Playback finished.")
    except Exception as e:
        print(f"[!] Playback notice: {e}")

    print("\nReady for analysis! You can clean this sample with:")
    print(f"  getsu-cli --clean \"{output_path}\"")
    print("=" * 68)


if __name__ == "__main__":
    main()

