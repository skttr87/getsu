"""
Configuration Management for Getsu.
Saves and loads user audio preferences to config.json.
"""
import os
import json
from typing import Dict, Any

DEFAULT_CONFIG: Dict[str, Any] = {
    "app_name": "Getsu",
    "version": "1.1.0",
    "input_device_id": None,
    "input_device_name": None,
    "output_device_id": None,
    "output_device_name": None,
    "denoise_enabled": True,
    "high_pass_filter": True,       # 80Hz rumble filter (crucial for laptop cooling pad)
    "hpf_cutoff_hz": 80.0,          # High-pass filter cutoff frequency in Hz
    "vad_threshold": 0.75,          # Tuned for cooling pad noise rejection (0.0 to 1.0)
    "vad_close_threshold": 0.45,    # Hysteresis close threshold preserving unvoiced consonants
    "vad_hangover_ms": 180.0,       # Hold time to avoid cutting word endings
    "output_gain": 1.08,            # Post-RNNoise make-up gain (+0.7 dB)
    "mic_gain": 1.0,                # Input multiplier
    "mic_boost_db": 0,              # Microphone boost step (0, 5, 10, 15 dB)
    "mute": False,
}

import sys

if getattr(sys, 'frozen', False):
    CONFIG_FILE = os.path.join(os.path.dirname(sys.executable), "config.json")
else:
    CONFIG_FILE = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "config.json"))


def load_config() -> Dict[str, Any]:
    """Loads configuration from config.json or returns defaults."""
    if not os.path.exists(CONFIG_FILE):
        save_config(DEFAULT_CONFIG)
        return DEFAULT_CONFIG.copy()

    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        # Ensure all default keys exist
        merged = DEFAULT_CONFIG.copy()
        merged.update(data)
        return merged
    except Exception as e:
        print(f"[WARN] Failed to load config.json ({e}), using defaults.")
        return DEFAULT_CONFIG.copy()


def save_config(config_data: Dict[str, Any]) -> bool:
    """Safely saves configuration data to config.json."""
    try:
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(config_data, f, indent=4)
        return True
    except Exception as e:
        print(f"[ERROR] Failed to save config.json: {e}")
        return False
