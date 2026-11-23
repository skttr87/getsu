"""
Smart Microphone Routing and Windows CoreAudio Endpoint Management for Getsu.
Orchestrates automatic hot-swapping to CABLE Output on START,
and clean restoration of physical microphones on STOP, EXIT, or CRASH.

Uses direct in-process Windows CoreAudio COM interfaces (IPolicyConfig, IMMDeviceEnumerator)
for instant (<1ms), robust default recording endpoint management without external process dependencies.
Maintains bundled native AudioRestore.exe as a secondary fallback.
"""
import os
import sys
import logging
import subprocess
import threading
from typing import Optional, Tuple, Dict, Any
import ctypes
from ctypes import wintypes
import re

try:
    from src.config import save_config
except ImportError:
    from config import save_config

try:
    from src.diagnostics import get_file_handler
except ImportError:
    from diagnostics import get_file_handler

# CoreAudio helpers and the routing logger live in win_audio; re-exported so imports keep working.
from src.win_audio import (
    logger, ole32, _safe_co_initialize, GUID, PROPERTYKEY, PROPVARIANT, PKEY_Device_FriendlyName,
    PKEY_Device_EnumeratorName, PKEY_AudioEndpoint_FormFactor, VT_UI4, VT_LPWSTR,
    EndpointFormFactor, DeviceBusType, CLSID_MMDeviceEnumerator, IID_IMMDeviceEnumerator,
    CLSID_PolicyConfigClient, IID_IPolicyConfig, _release_com_ptr, native_get_current_default_mic,
    native_get_current_default_playback, _is_endpoint_id, native_get_device_bus_type,
    _bus_type_cache, _form_factor_cache, clear_device_bus_type_cache,
    native_get_device_bus_type_cached, _form_factor_from_variant, _endpoint_name_matches,
    native_get_device_form_factor, native_get_device_form_factor_cached,
    native_get_cable_capture_guid, native_get_physical_capture_guid, ENDPOINT_GUID_PATTERN,
    native_set_default_endpoint, get_audio_restore_exe
)


class SmartMicRouter:
    """
    Manages Windows default recording endpoint (eCapture) hot-swapping.
    Guarantees strict Input-Only isolation (eRender playback is never touched).
    """

    def __init__(self, config: Dict[str, Any]):
        self.config = config
        self.is_swapped = False
        self.original_mic_id: Optional[str] = config.get("original_mic_id")
        self._lock = threading.Lock()
        self.exe_path = get_audio_restore_exe()

    def _run_helper(self, *args: str) -> Tuple[int, str]:
        """Runs AudioRestore.exe with specified arguments silently."""
        if not os.path.exists(self.exe_path):
            return 1, f"AudioRestore.exe not found at {self.exe_path}"
        try:
            startupinfo = None
            if sys.platform == 'win32':
                startupinfo = subprocess.STARTUPINFO()
                startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
                startupinfo.wShowWindow = 0  # SW_HIDE

            proc = subprocess.run(
                [self.exe_path] + list(args),
                capture_output=True,
                text=True,
                startupinfo=startupinfo,
                timeout=5.0
            )
            if proc.returncode != 0 and proc.stderr:
                logger.warning(f"AudioRestore helper returned {proc.returncode}: {proc.stderr.strip()}")
            return proc.returncode, proc.stdout.strip()
        except Exception as e:
            return 1, str(e)

    def _set_capture_endpoint(self, guid: str) -> bool:
        """
        Sets Windows default capture endpoint using native in-process COM first,
        falling back to bundled AudioRestore.exe.
        """
        if not guid or not ENDPOINT_GUID_PATTERN.match(guid.strip()):
            logger.warning(f"Cannot set capture endpoint: invalid GUID format '{guid}'")
            return False

        try:
            if native_set_default_endpoint(guid):
                return True
        except Exception as e:
            logger.warning(f"Native COM set endpoint failed: {e}")

        # Fallback to AudioRestore.exe
        code, out = self._run_helper("--set-default-capture", guid)
        return (code == 0)

    def get_current_default_mic(self) -> Tuple[Optional[str], str]:
        """
        Retrieves the active Windows default recording device.
        Returns: (endpoint_guid, friendly_name)
        """
        guid, name = native_get_current_default_mic()
        if guid:
            return guid, name

        code, out = self._run_helper("--get-default-capture")
        if code == 0 and "|" in out:
            parts = out.split("|", 1)
            return parts[0].strip(), parts[1].strip()
        return None, "Unknown"

    def get_current_default_playback(self) -> Tuple[Optional[str], str]:
        """
        Retrieves the active Windows default playback device.
        Returns: (endpoint_guid, friendly_name)
        """
        guid, name = native_get_current_default_playback()
        if guid:
            return guid, name
        return None, "Unknown"

    def get_device_bus_type(self, device_id_or_name: Optional[str] = None, is_input: bool = True) -> str:
        """
        Retrieves the hardware bus enumerator for a device.
        Returns: DeviceBusType string (e.g. 'BTHHFENUM', 'BTHENUM', 'USB', 'HDAUDIO', 'SWD').
        """
        return native_get_device_bus_type(device_id_or_name, is_input=is_input)

    def get_cable_capture_guid(self) -> Optional[str]:
        """Scans active capture endpoints for CABLE Output and returns its GUID."""
        guid = native_get_cable_capture_guid()
        if guid:
            return guid

        code, out = self._run_helper("--get-cable-capture")
        if code == 0 and out.startswith("{"):
            return out.strip()
        return None

    def swap_to_cable(self) -> bool:
        """
        Atomically swaps Windows default recording device (eCapture) to CABLE Output.
        Pre-flight checks ensure virtual cable exists and original physical mic is safely backed up.
        """
        with self._lock:
            if not self.config.get("auto_route", True):
                logger.info("Auto-Route to Apps is disabled. Operating in manual/OBS routing mode.")
                return True

            if self.is_swapped:
                return True

            cable_guid = self.get_cable_capture_guid()
            if not cable_guid:
                logger.warning("Cannot swap: CABLE Output endpoint not found or VB-Cable not installed.")
                return False

            current_guid, current_name = self.get_current_default_mic()

            # HARD SAFETY GUARD: Never store CABLE Output as original mic
            if current_guid and "cable" not in current_name.lower():
                self.original_mic_id = current_guid
            elif not self.original_mic_id:
                # If current was already CABLE, keep whatever was in config or pick first physical
                phys_guid, _ = native_get_physical_capture_guid()
                self.original_mic_id = self.config.get("original_mic_id") or phys_guid

            # 1. Atomic config write BEFORE OS system changes
            self.config["is_swapped"] = True
            if self.original_mic_id:
                self.config["original_mic_id"] = self.original_mic_id
            save_config(self.config)

            # 2. Execute CoreAudio COM switch (native first, then helper)
            if not self._set_capture_endpoint(cable_guid):
                logger.error("Failed to set CABLE Output as default capture endpoint.")
                self.config["is_swapped"] = False
                save_config(self.config)
                return False

            # 3. Post-swap verification
            verify_guid, verify_name = self.get_current_default_mic()
            if "cable" in verify_name.lower() or (verify_guid and verify_guid.lower() == cable_guid.lower()):
                self.is_swapped = True
                logger.info(f"Successfully swapped default microphone to: {verify_name} ({verify_guid})")
                return True

            logger.warning(f"Post-swap verification inconclusive. Active: {verify_name} ({verify_guid})")
            self.is_swapped = False
            self.config["is_swapped"] = False
            save_config(self.config)
            return False

    def restore_original(self) -> bool:
        """
        Unified restore method called by all exit and stop vectors.
        Restores the physical microphone, with automatic fallback if unplugged.
        """
        with self._lock:
            if not self.is_swapped and not self.config.get("is_swapped", False):
                return True

            target_guid = self.original_mic_id or self.config.get("original_mic_id")
            if target_guid and not ENDPOINT_GUID_PATTERN.match(target_guid.strip()):
                logger.warning(f"Invalid target GUID format '{target_guid}', falling back to physical device scan.")
                target_guid = None
            if not target_guid:
                target_guid, _ = native_get_physical_capture_guid()

            success = False
            if target_guid:
                success = self._set_capture_endpoint(target_guid)
                if not success:
                    code, out = self._run_helper("--restore-capture", target_guid)
                    success = (code == 0)
            else:
                code, out = self._run_helper("--ensure-physical-capture")
                success = (code == 0)

            if success:
                self.is_swapped = False
                self.config["is_swapped"] = False
                save_config(self.config)

            logger.info(f"Microphone restore executed. Success={success}")
            return bool(success)

    def audit_crash_recovery(self) -> bool:
        """
        Audits config on startup to recover if Getsu crashed while swapped.
        Heals Windows audio state automatically.
        """
        if not self.config.get("is_swapped", False):
            return False

        current_guid, current_name = self.get_current_default_mic()
        if "cable" in current_name.lower():
            logger.info(f"Detected unclean shutdown with mic swapped to '{current_name}'. Restoring physical mic...")
            target_guid = self.config.get("original_mic_id")
            if target_guid and not ENDPOINT_GUID_PATTERN.match(target_guid.strip()):
                logger.warning(f"Invalid saved GUID format '{target_guid}' in config, falling back to physical device scan.")
                target_guid = None
            if not target_guid:
                target_guid, _ = native_get_physical_capture_guid()

            success = False
            if target_guid:
                success = self._set_capture_endpoint(target_guid)
                if not success:
                    code, _ = self._run_helper("--restore-capture", target_guid)
                    success = (code == 0)
            else:
                code, _ = self._run_helper("--ensure-physical-capture")
                success = (code == 0)

            if success:
                self.config["is_swapped"] = False
                save_config(self.config)
                logger.info("Physical microphone successfully restored on crash recovery!")
                return True
            else:
                logger.error("Failed to restore physical microphone on crash recovery.")
                return False

        # If already on a physical mic, just reset the flag
        self.config["is_swapped"] = False
        save_config(self.config)
        return False
