"""
Cable visibility (owner decision 2026-11-26, rules.md section 3).

Getsu's own VB-CABLE is hidden while Getsu is closed and shown while it runs. A hidden endpoint cannot be picked by
Windows as the default speakers or microphone, so a headset that disconnects while Getsu is closed does not leave the
cable as the default output.

Only the two endpoints of the VB-CABLE that Getsu installed are touched, and only when Getsu installed it:
  - the installer writes the registry value HKLM\\SOFTWARE\\Getsu "CableInstalledByGetsu" (DWORD 1);
  - an in-app install sets config["cable_installed_by_getsu"].
A cable the user installed themselves is never hidden or shown.

show_cable() and hide_cable() decide and never raise. The only function that changes a device is
win_audio.set_getsu_cable_visibility.
"""
import logging
import time
from typing import Dict, List, Optional

from src import win_audio

CONFIG_KEY = "cable_installed_by_getsu"
REG_KEY = r"SOFTWARE\Getsu"
REG_VALUE = "CableInstalledByGetsu"
SHOW_TIMEOUT_S = 3.0
POLL_S = 0.1
USAGE = "usage: --cable-visibility show | hide | show-always"

logger = logging.getLogger("GetsuRouter")


def _registry_marker() -> bool:
    """True when the installer's marker value exists as DWORD 1 (64-bit registry view). Any error means False."""
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, REG_KEY, 0, winreg.KEY_READ | winreg.KEY_WOW64_64KEY) as key:
            value, kind = winreg.QueryValueEx(key, REG_VALUE)
        return kind == winreg.REG_DWORD and value == 1
    except Exception:
        return False


def cable_installed_by_getsu(config: Optional[Dict]) -> bool:
    """True when Getsu installed the VB-CABLE: the config flag is set, or the installer's registry marker exists."""
    if isinstance(config, dict) and config.get(CONFIG_KEY) is True:
        return True
    return _registry_marker()


def mark_cable_installed_by_getsu(config: Dict) -> bool:
    """Records an in-app install of the cable by Getsu and saves the config. The cable is not hidden now (Getsu is
    running). Returns True when the config was saved. Never raises."""
    try:
        from src.config import save_config
        config[CONFIG_KEY] = True
        return bool(save_config(config))
    except Exception as e:
        logger.warning(f"[CABLE VISIBILITY] could not record the cable install: {e}")
        return False


def _cable_endpoints() -> List[Dict]:
    """Getsu's VB-CABLE endpoints, active and hidden: [{"id", "name", "is_input", "state"}]. Read-only."""
    try:
        return win_audio.native_list_getsu_cable_endpoints()
    except Exception as e:
        logger.warning(f"[CABLE VISIBILITY] listing endpoints failed: {e}")
        return []


def _wait_active(endpoint_id: str, timeout_s: float, poll_s: float) -> bool:
    """Polls until the endpoint reports active, or the timeout passes."""
    deadline = time.monotonic() + max(0.0, timeout_s)
    while True:
        if any(e["id"] == endpoint_id and e["state"] == "active" for e in _cable_endpoints()):
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(poll_s)


def _show_all(timeout_s: float, poll_s: float) -> Dict[str, str]:
    results: Dict[str, str] = {}
    for e in _cable_endpoints():
        name = e["name"]
        if e["state"] == "active":
            results[name] = "ok"
        elif e["state"] == "disabled":
            if not win_audio.set_getsu_cable_visibility(e["id"], True):
                results[name] = "failed"
            elif _wait_active(e["id"], timeout_s, poll_s):
                results[name] = "shown"
            else:
                logger.warning(f"[CABLE VISIBILITY] '{name}' did not become active within {timeout_s:.1f} s.")
                results[name] = "failed"
        else:
            results[name] = "failed"
        logger.info(f"[CABLE VISIBILITY] show '{name}': {results[name]}")
    return results


def _hide_all() -> Dict[str, str]:
    results: Dict[str, str] = {}
    for e in _cable_endpoints():
        name = e["name"]
        if e["state"] == "disabled":
            results[name] = "ok"
        elif e["state"] == "active":
            results[name] = "hidden" if win_audio.set_getsu_cable_visibility(e["id"], False) else "failed"
        else:
            results[name] = "failed"
        logger.info(f"[CABLE VISIBILITY] hide '{name}': {results[name]}")
    return results


def show_cable(config: Optional[Dict], timeout_s: float = SHOW_TIMEOUT_S, poll_s: float = POLL_S) -> Dict[str, str]:
    """Shows Getsu's hidden cable endpoints when Getsu installed the cable. Returns {} when it did not.
    Returns {endpoint name: "shown" | "ok" | "failed"}. Never raises."""
    try:
        if not cable_installed_by_getsu(config):
            return {}
        return _show_all(timeout_s, poll_s)
    except Exception as e:
        logger.warning(f"[CABLE VISIBILITY] show failed: {e}")
        return {}


def hide_cable(config: Optional[Dict]) -> Dict[str, str]:
    """Hides Getsu's cable endpoints when Getsu installed the cable. Returns {} when it did not.
    Returns {endpoint name: "hidden" | "ok" | "failed"}. Never raises."""
    try:
        if not cable_installed_by_getsu(config):
            return {}
        return _hide_all()
    except Exception as e:
        logger.warning(f"[CABLE VISIBILITY] hide failed: {e}")
        return {}


def show_always(timeout_s: float = SHOW_TIMEOUT_S, poll_s: float = POLL_S) -> Dict[str, str]:
    """Uninstaller path: shows the cable when Getsu installed it, by the installer's registry marker or by an in-app
    install (config flag), so no cable Getsu hid stays hidden after Getsu is removed."""
    try:
        if not _registry_marker():
            try:
                from src.config import load_config
                config = load_config()
            except Exception:
                config = None
            if not cable_installed_by_getsu(config):
                return {}
        return _show_all(timeout_s, poll_s)
    except Exception as e:
        logger.warning(f"[CABLE VISIBILITY] show-always failed: {e}")
        return {}


def main_cli(argv: List[str]) -> int:
    """`getsu.exe --cable-visibility show|hide|show-always`, run by the installer and the uninstaller. Exits with 0
    (a visibility problem must not fail the installation or uninstallation); the result is printed and logged."""
    try:
        mode = argv[argv.index("--cable-visibility") + 1]
    except (ValueError, IndexError):
        mode = ""

    try:
        if mode == "show-always":
            results = show_always()
        elif mode == "show":
            from src.config import load_config
            results = show_cable(load_config())
        elif mode == "hide":
            from src.config import load_config
            results = hide_cable(load_config())
        else:
            print(USAGE)
            return 0
    except Exception as e:
        print(f"[CABLE VISIBILITY] {mode} failed: {e}")
        return 0

    if not results:
        print("[CABLE VISIBILITY] Nothing to do: no VB-CABLE installed by Getsu was found.")
    for name, state in results.items():
        print(f"[CABLE VISIBILITY] {name}: {state}")
    return 0
