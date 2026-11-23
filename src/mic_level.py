"""
Recording levels when noise cancellation is turned on (owner decision 2026-11-24, rules.md section 3, levels).

1. Getsu's own VB-CABLE (CABLE Input and CABLE Output) at 100 %: an app's "automatically adjust microphone volume"
   may turn CABLE Output down, and then everyone hears the user quietly.
2. The microphone selected in Getsu: below 65 % it is raised to 95 %, ONCE per microphone (endpoint ID remembered in
   config["mic_level_raised"]). A level of 7 % (set earlier by some app) made speech so quiet that words were lost.
   If the user sets it below 65 % again later, that is deliberate: Getsu only shows a hint.
The only level setter is win_audio.set_endpoint_level_guarded.
"""
import logging
from typing import Callable, Dict, Optional, Tuple

from src import win_audio
from src.cable_format import cable_endpoints
from src.config import save_config
from src.devices import is_virtual_endpoint

RAISE_BELOW = 0.65
RAISE_TO = 0.95
CABLE_LEVEL = 1.0
logger = logging.getLogger("GetsuRouter")


def _pct(level: float) -> int:
    return int(round(level * 100))


def keep_cable_at_full_level() -> Dict[str, str]:
    """Sets every Getsu cable endpoint below 100 % to 100 %. {name: "ok" | "set" | "failed"}. Never raises."""
    results = {}
    for e in cable_endpoints():
        level = win_audio.native_get_endpoint_level(e["id"])
        if level is None:
            continue
        if level >= CABLE_LEVEL - 0.005:
            results[e["name"]] = "ok"
            continue
        logger.info(f"[LEVELS] '{e['name']}' is at {_pct(level)} %; setting 100 %.")
        results[e["name"]] = "set" if win_audio.set_endpoint_level_guarded(e["id"], CABLE_LEVEL) else "failed"
    return results


def check_microphone_level(config: Dict, mic_name: Optional[str],
                           save: Callable[[Dict], None] = save_config) -> Optional[Tuple[str, str]]:
    """
    Raises the selected microphone once from below 65 % to 95 %. mic_name is its full Windows name.
    Returns (status text, "info" | "warn") for the status line, or None when nothing needs saying.
    """
    if not mic_name:
        return None
    matches = [e for e in win_audio.native_list_active_endpoints(is_input=True) if e.get("name") == mic_name]
    if len(matches) != 1:
        return None
    mic = matches[0]
    if is_virtual_endpoint(mic["name"], mic.get("bus") or "UNKNOWN"):
        return None
    level = win_audio.native_get_endpoint_level(mic["id"])
    if level is None or level >= RAISE_BELOW:
        return None
    raised = [i for i in (config.get("mic_level_raised") or []) if isinstance(i, str)]
    if mic["id"] in raised:
        logger.info(f"[LEVELS] '{mic['name']}' is at {_pct(level)} %; left as set by the user.")
        return (f"Microphone level is {_pct(level)} %. Words may be cut if it is too low.", "warn")
    if not win_audio.set_endpoint_level_guarded(mic["id"], RAISE_TO):
        return (f"Microphone level is {_pct(level)} %. Raise it in Windows Sound settings.", "warn")
    raised.append(mic["id"])
    config["mic_level_raised"] = raised
    save(config)
    logger.info(f"[LEVELS] '{mic['name']}' raised from {_pct(level)} % to {_pct(RAISE_TO)} % (once).")
    return (f"Microphone level raised from {_pct(level)} % to {_pct(RAISE_TO)} %", "info")


def apply_levels_on_start(config: Dict, mic_name: Optional[str],
                          save: Callable[[Dict], None] = save_config) -> Optional[Tuple[str, str]]:
    """Both steps, run when the user turns noise cancellation on. Never raises."""
    try:
        keep_cable_at_full_level()
    except Exception as e:
        logger.warning(f"[LEVELS] cable level check failed: {e}")
    try:
        return check_microphone_level(config, mic_name, save)
    except Exception as e:
        logger.warning(f"[LEVELS] microphone level check failed: {e}")
        return None
