"""
v1.2.6 config migration for Getsu (invoked from src/config.py:load_config).

Features:
- Runs ONCE, only when the stored version is not 1.2.6, so any value the user sets later
  (even 0.55 / 280) is never reset on the next launch.
- Skips users who customized VAD settings (vad_customized == True).
- Treats every previous shipped default as "stale default": close threshold 0.45/0.55,
  hangover 180/220/280/360.
"""

TARGET_VERSION = "1.2.6"
STALE_CLOSE_THRESHOLDS = (0.45, 0.55)
STALE_HANGOVERS_MS = (180.0, 220.0, 280.0, 360.0)
NEW_CLOSE_THRESHOLD = 0.52
NEW_HANGOVER_MS = 320.0


def migrate_v126(data: dict, merged: dict) -> bool:
    """
    data   : config exactly as read from disk (used for the stored version)
    merged : DEFAULT_CONFIG overlaid with data (mutated in place)
    Returns True when `merged` changed and should be saved.
    """
    if data.get("version") == TARGET_VERSION:
        return False

    if not merged.get("vad_customized", False):
        if merged.get("vad_close_threshold") in (None,) + STALE_CLOSE_THRESHOLDS:
            merged["vad_close_threshold"] = NEW_CLOSE_THRESHOLD
        if merged.get("vad_hangover_ms") in (None,) + STALE_HANGOVERS_MS:
            merged["vad_hangover_ms"] = NEW_HANGOVER_MS

    merged["version"] = TARGET_VERSION
    return True

