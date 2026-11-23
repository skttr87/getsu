"""
Keeps Getsu's own VB-CABLE at 48 kHz (owner decision 2026-11-24, rules.md section 3).

Getsu processes audio at 48 kHz. A cable at 44.1 kHz still works (Windows converts), but adds a conversion and delay.
ensure_cable_48k() sets 2 channel, 16 bit, 48000 Hz on the endpoints of the VB-CABLE that Getsu installs (CABLE Input
and CABLE Output, matched by their driver name) when they are at another rate. It runs:
  - right after Getsu installs the cable (in-app install, and the installer through `getsu.exe --set-cable-format`);
  - once before noise cancellation starts, when the cable is not at 48 kHz (fixes existing installs).
Nothing else about any device is changed; the only setter is win_audio.set_getsu_cable_format.
"""
import logging
import time
from typing import Dict, List

from src import win_audio
from src.devices import is_getsu_cable_endpoint

TARGET_RATE = 48000
logger = logging.getLogger("GetsuRouter")


def cable_endpoints() -> List[Dict]:
    """The active endpoints of Getsu's VB-CABLE, both directions: [{"id", "name", "is_input"}]. Read-only."""
    found = []
    for is_input in (True, False):
        for e in win_audio.native_list_active_endpoints(is_input=is_input):
            if is_getsu_cable_endpoint(e.get("name", "")):
                found.append({"id": e["id"], "name": e["name"], "is_input": is_input})
    return found


def ensure_cable_48k(wait_s: float = 0.0, poll_s: float = 0.5) -> Dict[str, str]:
    """
    Sets every Getsu VB-CABLE endpoint that is not at 48 kHz to 2 ch, 16 bit, 48 kHz.
    wait_s: after a fresh install the endpoints appear a moment later; keep looking for them up to wait_s seconds.
    Returns {endpoint name: "ok" | "set" | "failed" | "unknown"}; empty when no cable endpoint exists. Never raises.
    """
    deadline = time.monotonic() + max(0.0, wait_s)
    endpoints: List[Dict] = []
    while True:
        try:
            endpoints = cable_endpoints()
        except Exception as e:
            logger.warning(f"[CABLE FORMAT] listing endpoints failed: {e}")
            endpoints = []
        # both directions present, or out of time
        if (any(e["is_input"] for e in endpoints) and any(not e["is_input"] for e in endpoints)) \
                or time.monotonic() >= deadline:
            break
        time.sleep(poll_s)

    results: Dict[str, str] = {}
    for e in endpoints:
        try:
            fmt = win_audio.native_get_endpoint_format(e["id"])
            if fmt is None:
                results[e["name"]] = "unknown"
                continue
            if fmt[0] == TARGET_RATE:
                results[e["name"]] = "ok"
                continue
            logger.info(f"[CABLE FORMAT] '{e['name']}' is at {fmt[0]} Hz; setting {TARGET_RATE} Hz.")
            results[e["name"]] = "set" if win_audio.set_getsu_cable_format(e["id"], TARGET_RATE) else "failed"
        except Exception as ex:
            logger.warning(f"[CABLE FORMAT] '{e['name']}': {ex}")
            results[e["name"]] = "failed"
    return results


def main_cli() -> int:
    """`getsu.exe --set-cable-format`: run by the installer right after it installs the cable. Exit code 0 always
    (a failed format change must not fail the installation); the result is printed and logged."""
    results = ensure_cable_48k(wait_s=10.0)
    if not results:
        print("[CABLE FORMAT] No VB-CABLE endpoint found.")
    for name, state in results.items():
        print(f"[CABLE FORMAT] {name}: {state}")
    return 0
