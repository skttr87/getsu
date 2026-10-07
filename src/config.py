"""
Configuration Management for Getsu.
Saves and loads user audio preferences to config.json.
"""
import os
import json
import copy
from typing import Dict, Any

DEFAULT_CONFIG: Dict[str, Any] = {
    "app_name": "Getsu",
    "version": "1.2.8",

    "input_device_id": None,
    "input_device_name": None,
    "output_device_id": None,
    "output_device_name": None,
    "denoise_enabled": True,
    "high_pass_filter": True,       # 80Hz rumble filter (crucial for laptop cooling pad)
    "hpf_cutoff_hz": 80.0,          # High-pass filter cutoff frequency in Hz
    "vad_threshold": 0.70,          # Tuned for cooling pad noise rejection (0.0 to 1.0)
    "vad_close_threshold": 0.52,    # Hysteresis close threshold preserving unvoiced consonants
    "vad_hangover_ms": 320.0,       # Extended hold time across natural pauses and breaths
    "vad_decay_ms": 80.0,           # Smooth natural fade to pure silence
    "vad_onset_threshold": 0.35,    # Low-threshold trigger for initial unvoiced consonants
    "vad_onset_snr_db": 10.0,        # Required dB rise above noise floor for fast onset
    "vad_cold_start_gain": 0.0,     # Cold start initial gain (0.0 when floor_gain handles continuous continuity)
    "vad_floor_gain": 0.06,         # Noise gate floor / expander range ~ -24.4 dB, eliminates pop/harsh boundary steps
    "output_gain": 1.08,            # Post-RNNoise make-up gain (+0.7 dB)
    "mic_gain": 1.0,                # Input multiplier
    "mic_boost_db": 0,              # Microphone boost step (0, 5, 10, 15 dB)
    "mute": False,
    "is_swapped": False,            # Tracks if Windows default mic is currently CABLE Output
    "original_mic_id": None,        # Stores Windows CoreAudio endpoint GUID string of physical mic
    "auto_route": True,             # Settings toggle: Auto-Route to Apps (switch default mic when active)
}

import sys
import time
import tempfile

def get_config_path() -> str:
    """
    Determines writable path for config.json.
    - Source / dev mode: uses root/config.json
    - Frozen portable mode: if directory next to getsu.exe is writable, uses it.
    - Frozen installed mode: uses %APPDATA%\\Getsu\\config.json.
    """
    if getattr(sys, 'frozen', False):
        app_dir = os.path.dirname(sys.executable)
        local_cfg = os.path.join(app_dir, "config.json")
        # Check if local directory is writable (e.g. portable / user folder)
        try:
            test_file = os.path.join(app_dir, f".write_test_{os.getpid()}")
            with open(test_file, "w") as f:
                f.write("1")
            os.remove(test_file)
            return local_cfg
        except Exception:
            pass

        # Standard Windows Installed Application Mode: %APPDATA%\Getsu\config.json
        appdata_dir = os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")), "Getsu")
        try:
            os.makedirs(appdata_dir, exist_ok=True)
        except Exception:
            pass
        user_cfg = os.path.join(appdata_dir, "config.json")

        # Migrate local config if user config does not exist yet
        if not os.path.exists(user_cfg) and os.path.exists(local_cfg):
            try:
                import shutil
                shutil.copy2(local_cfg, user_cfg)
            except Exception:
                pass

        return user_cfg
    else:
        return os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "config.json"))


CONFIG_FILE = get_config_path()


def load_config() -> Dict[str, Any]:
    """Loads configuration from config.json or returns defaults."""
    if not os.path.exists(CONFIG_FILE):
        save_config(DEFAULT_CONFIG)
        return copy.deepcopy(DEFAULT_CONFIG)

    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        # Ensure all default keys exist
        merged = copy.deepcopy(DEFAULT_CONFIG)
        merged.update(data)

        # Automatic Migration: Upgrade legacy configs
        migrated = False
        if merged.get("vad_cold_start_gain") in (0.70, 0.35):
            merged["vad_cold_start_gain"] = 0.0
            migrated = True
        if merged.get("vad_onset_snr_db") in (4.0, 5.0, 6.0, 7.0, 8.0):
            merged["vad_onset_snr_db"] = 10.0
            migrated = True
        if merged.get("vad_onset_threshold") in (None, 0.30):
            merged["vad_onset_threshold"] = 0.35
            migrated = True
        if "vad_floor_gain" not in data or data.get("vad_floor_gain") in (None, 0.02, 0.03, 0.035, 0.04):
            merged["vad_floor_gain"] = 0.06
            migrated = True

        # v1.2.6 migration (handles stale close thresholds, hangovers, and version bump)
        try:
            from src.config_migration_v126 import migrate_v126
            if migrate_v126(data, merged):
                migrated = True
        except ImportError:
            try:
                from config_migration_v126 import migrate_v126
                if migrate_v126(data, merged):
                    migrated = True
            except Exception:
                pass

        if merged.get("version") != DEFAULT_CONFIG["version"]:
            merged["version"] = DEFAULT_CONFIG["version"]
            migrated = True

        if migrated:
            save_config(merged)

        return merged
    except Exception as e:
        print(f"[WARN] Failed to load config.json ({e}), using defaults.")
        return copy.deepcopy(DEFAULT_CONFIG)


import threading

_config_lock = threading.Lock()


def save_config(config_data: Dict[str, Any]) -> bool:
    """
    Atomically saves configuration data to config.json using temp-file swap
    with exponential backoff for transient Windows Defender / antivirus file inspection locks.
    Thread-safe against concurrent saves and in-flight dictionary mutations.
    """
    with _config_lock:
        data_copy = copy.deepcopy(config_data)
        try:
            config_dir = os.path.dirname(CONFIG_FILE)
            temp_fd, temp_path = tempfile.mkstemp(dir=config_dir, suffix=".tmp")
            try:
                with os.fdopen(temp_fd, "w", encoding="utf-8") as f:
                    json.dump(data_copy, f, indent=4)

                # 3-attempt exponential backoff retry for transient antivirus file locks (WinError 32)
                for attempt in range(3):
                    try:
                        os.replace(temp_path, CONFIG_FILE)
                        return True
                    except PermissionError:
                        if attempt == 2:
                            raise
                        time.sleep(0.05 * (attempt + 1))
            except Exception:
                try:
                    if os.path.exists(temp_path):
                        os.unlink(temp_path)
                except Exception:
                    pass
                raise
        except Exception as e:
            print(f"[ERROR] Failed to save config.json: {e}")
            return False
