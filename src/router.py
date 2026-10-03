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

try:
    from src.config import save_config
except ImportError:
    from config import save_config

from logging.handlers import RotatingFileHandler

# Configure file + console logger for routing audit
logger = logging.getLogger("GetsuRouter")
if not logger.handlers:
    logger.setLevel(logging.INFO)
    try:
        appdata_dir = os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")), "Getsu")
        os.makedirs(appdata_dir, exist_ok=True)
        log_file = os.path.join(appdata_dir, "getsu.log")
        fh = RotatingFileHandler(log_file, maxBytes=2 * 1024 * 1024, backupCount=2, encoding="utf-8")
        fh.setFormatter(logging.Formatter("[%(asctime)s] [%(levelname)s] [ROUTER] %(message)s"))
        logger.addHandler(fh)
    except Exception:
        pass
    sh = logging.StreamHandler()
    sh.setFormatter(logging.Formatter("[ROUTER] %(message)s"))
    logger.addHandler(sh)


# --- NATIVE COREAUDIO COM DEFINITIONS ---
# Use windll (not oledll) so non-zero HRESULTs (e.g. S_FALSE or RPC_E_CHANGED_MODE)
# do not raise unhandled Python OSErrors on threads where COM is already initialized.
ole32 = getattr(ctypes, 'windll', None)
if ole32:
    ole32 = ole32.ole32
    if hasattr(ole32, 'CoTaskMemFree'):
        ole32.CoTaskMemFree.argtypes = [ctypes.c_void_p]
        ole32.CoTaskMemFree.restype = None
    if hasattr(ole32, 'PropVariantClear'):
        ole32.PropVariantClear.argtypes = [ctypes.c_void_p]
        ole32.PropVariantClear.restype = ctypes.c_long


def _safe_co_initialize() -> None:
    """Safely initializes COM in MTA mode without raising if already initialized."""
    if ole32:
        try:
            # 0x0 = COINIT_MULTITHREADED
            ole32.CoInitializeEx(None, 0)
        except Exception:
            pass


class GUID(ctypes.Structure):
    _fields_ = [
        ('Data1', wintypes.DWORD),
        ('Data2', wintypes.WORD),
        ('Data3', wintypes.WORD),
        ('Data4', wintypes.BYTE * 8)
    ]


class PROPERTYKEY(ctypes.Structure):
    _fields_ = [
        ('fmtid', GUID),
        ('pid', wintypes.DWORD)
    ]


class PROPVARIANT(ctypes.Structure):
    _fields_ = [
        ('vt', wintypes.WORD),
        ('wReserved1', wintypes.WORD),
        ('wReserved2', wintypes.WORD),
        ('wReserved3', wintypes.WORD),
        ('pwszVal', wintypes.LPWSTR),
        ('_pad', ctypes.c_uint64),  # Pad to full 24-byte canonical Windows PROPVARIANT
    ]


PKEY_Device_FriendlyName = PROPERTYKEY(
    GUID(0xa45c254e, 0xdf1c, 0x4efd, (wintypes.BYTE * 8)(0x80, 0x20, 0x67, 0xd1, 0x46, 0xa8, 0x50, 0xe0)),
    14
)

# CoreAudio Device Enumerator
CLSID_MMDeviceEnumerator = GUID(0xBCDE0395, 0xE52F, 0x467C, (wintypes.BYTE * 8)(0x8E, 0x3D, 0xC4, 0x57, 0x92, 0x91, 0x69, 0x2E))
IID_IMMDeviceEnumerator = GUID(0xA95664D2, 0x9614, 0x4F35, (wintypes.BYTE * 8)(0xA7, 0x46, 0xDE, 0x8D, 0xB6, 0x36, 0x17, 0xE6))

# Windows 10 / 11 PolicyConfig Client
CLSID_PolicyConfigClient = GUID(0x870af99c, 0x171d, 0x4f9e, (wintypes.BYTE * 8)(0xaf, 0x0d, 0xe6, 0x3d, 0xf4, 0x0c, 0x2b, 0xc9))
IID_IPolicyConfig = GUID(0xf8679f50, 0x850a, 0x41cf, (wintypes.BYTE * 8)(0x9c, 0x72, 0x43, 0x0f, 0x29, 0x02, 0x90, 0xc8))


def _release_com_ptr(ptr) -> None:
    """Safely calls IUnknown::Release on a COM interface pointer."""
    if ptr and ole32:
        try:
            vtable = ctypes.cast(ctypes.cast(ptr, ctypes.POINTER(ctypes.c_void_p)).contents, ctypes.POINTER(ctypes.c_void_p))
            Release = ctypes.WINFUNCTYPE(ctypes.c_ulong, ctypes.c_void_p)(vtable[2])
            Release(ptr)
        except Exception:
            pass


def native_get_current_default_mic() -> Tuple[Optional[str], str]:
    """
    Direct in-process query for the active Windows default capture endpoint.
    Checks eConsole (0) first, with fallback to eCommunications (2).
    Returns (endpoint_guid, friendly_name).
    """
    if sys.platform != 'win32' or not ole32:
        return None, "Unknown"

    pEnum = None
    pDev = None
    pStore = None
    try:
        _safe_co_initialize()
        pEnum = ctypes.c_void_p()
        hr = ole32.CoCreateInstance(
            ctypes.byref(CLSID_MMDeviceEnumerator),
            None,
            1 | 2 | 4,  # CLSCTX_ALL
            ctypes.byref(IID_IMMDeviceEnumerator),
            ctypes.byref(pEnum)
        )
        if hr != 0 or not pEnum:
            return None, "Unknown"

        vtable = ctypes.cast(ctypes.cast(pEnum, ctypes.POINTER(ctypes.c_void_p)).contents, ctypes.POINTER(ctypes.c_void_p))
        GetDefaultAudioEndpoint = ctypes.WINFUNCTYPE(ctypes.c_int, ctypes.c_void_p, ctypes.c_int, ctypes.c_int, ctypes.POINTER(ctypes.c_void_p))(vtable[4])

        pDev = ctypes.c_void_p()
        # eCapture = 1, eConsole = 0
        hr = GetDefaultAudioEndpoint(pEnum, 1, 0, ctypes.byref(pDev))
        if hr != 0 or not pDev:
            # Fallback to eCommunications = 2
            hr = GetDefaultAudioEndpoint(pEnum, 1, 2, ctypes.byref(pDev))
            if hr != 0 or not pDev:
                return None, "Unknown"

        dev_vtable = ctypes.cast(ctypes.cast(pDev, ctypes.POINTER(ctypes.c_void_p)).contents, ctypes.POINTER(ctypes.c_void_p))
        OpenPropertyStore = ctypes.WINFUNCTYPE(ctypes.c_int, ctypes.c_void_p, ctypes.c_int, ctypes.POINTER(ctypes.c_void_p))(dev_vtable[4])
        GetId = ctypes.WINFUNCTYPE(ctypes.c_int, ctypes.c_void_p, ctypes.POINTER(wintypes.LPWSTR))(dev_vtable[5])

        dev_id = None
        pStr = wintypes.LPWSTR()
        try:
            if GetId(pDev, ctypes.byref(pStr)) == 0 and pStr.value:
                dev_id = str(pStr.value)
        finally:
            if pStr.value and ole32 and hasattr(ole32, 'CoTaskMemFree'):
                ole32.CoTaskMemFree(pStr)

        name = "Unknown"
        pStore = ctypes.c_void_p()
        if OpenPropertyStore(pDev, 0, ctypes.byref(pStore)) == 0 and pStore:
            store_vtable = ctypes.cast(ctypes.cast(pStore, ctypes.POINTER(ctypes.c_void_p)).contents, ctypes.POINTER(ctypes.c_void_p))
            GetValue = ctypes.WINFUNCTYPE(ctypes.c_int, ctypes.c_void_p, ctypes.POINTER(PROPERTYKEY), ctypes.POINTER(PROPVARIANT))(store_vtable[5])
            pv = PROPVARIANT()
            try:
                # 31 = VT_LPWSTR
                if GetValue(pStore, ctypes.byref(PKEY_Device_FriendlyName), ctypes.byref(pv)) == 0:
                    if pv.vt == 31 and pv.pwszVal:
                        name = str(pv.pwszVal)
            finally:
                if ole32 and hasattr(ole32, 'PropVariantClear'):
                    ole32.PropVariantClear(ctypes.byref(pv))

        return dev_id, name
    except Exception as e:
        logger.debug(f"native_get_current_default_mic exception: {e}")
        return None, "Unknown"
    finally:
        _release_com_ptr(pStore)
        _release_com_ptr(pDev)
        _release_com_ptr(pEnum)


def native_get_cable_capture_guid() -> Optional[str]:
    """
    Direct in-process scan of active eCapture endpoints to locate CABLE Output GUID.
    """
    if sys.platform != 'win32' or not ole32:
        return None

    pEnum = None
    pCol = None
    try:
        _safe_co_initialize()
        pEnum = ctypes.c_void_p()
        if ole32.CoCreateInstance(ctypes.byref(CLSID_MMDeviceEnumerator), None, 1 | 2 | 4, ctypes.byref(IID_IMMDeviceEnumerator), ctypes.byref(pEnum)) != 0:
            return None

        enum_vtable = ctypes.cast(ctypes.cast(pEnum, ctypes.POINTER(ctypes.c_void_p)).contents, ctypes.POINTER(ctypes.c_void_p))
        EnumAudioEndpoints = ctypes.WINFUNCTYPE(ctypes.c_int, ctypes.c_void_p, ctypes.c_int, wintypes.DWORD, ctypes.POINTER(ctypes.c_void_p))(enum_vtable[3])

        pCol = ctypes.c_void_p()
        # eCapture = 1, DEVICE_STATE_ACTIVE = 1
        if EnumAudioEndpoints(pEnum, 1, 1, ctypes.byref(pCol)) != 0 or not pCol:
            return None

        col_vtable = ctypes.cast(ctypes.cast(pCol, ctypes.POINTER(ctypes.c_void_p)).contents, ctypes.POINTER(ctypes.c_void_p))
        GetCount = ctypes.WINFUNCTYPE(ctypes.c_int, ctypes.c_void_p, ctypes.POINTER(wintypes.UINT))(col_vtable[3])
        Item = ctypes.WINFUNCTYPE(ctypes.c_int, ctypes.c_void_p, wintypes.UINT, ctypes.POINTER(ctypes.c_void_p))(col_vtable[4])

        count = wintypes.UINT()
        GetCount(pCol, ctypes.byref(count))

        for i in range(count.value):
            pDev = ctypes.c_void_p()
            if Item(pCol, i, ctypes.byref(pDev)) == 0 and pDev:
                pStore = None
                try:
                    dev_vtable = ctypes.cast(ctypes.cast(pDev, ctypes.POINTER(ctypes.c_void_p)).contents, ctypes.POINTER(ctypes.c_void_p))
                    OpenPropertyStore = ctypes.WINFUNCTYPE(ctypes.c_int, ctypes.c_void_p, ctypes.c_int, ctypes.POINTER(ctypes.c_void_p))(dev_vtable[4])
                    GetId = ctypes.WINFUNCTYPE(ctypes.c_int, ctypes.c_void_p, ctypes.POINTER(wintypes.LPWSTR))(dev_vtable[5])

                    name = ""
                    pStore = ctypes.c_void_p()
                    if OpenPropertyStore(pDev, 0, ctypes.byref(pStore)) == 0 and pStore:
                        store_vtable = ctypes.cast(ctypes.cast(pStore, ctypes.POINTER(ctypes.c_void_p)).contents, ctypes.POINTER(ctypes.c_void_p))
                        GetValue = ctypes.WINFUNCTYPE(ctypes.c_int, ctypes.c_void_p, ctypes.POINTER(PROPERTYKEY), ctypes.POINTER(PROPVARIANT))(store_vtable[5])
                        pv = PROPVARIANT()
                        try:
                            if GetValue(pStore, ctypes.byref(PKEY_Device_FriendlyName), ctypes.byref(pv)) == 0:
                                if pv.vt == 31 and pv.pwszVal:
                                    name = str(pv.pwszVal)
                        finally:
                            if ole32 and hasattr(ole32, 'PropVariantClear'):
                                ole32.PropVariantClear(ctypes.byref(pv))

                    name_lower = name.lower()
                    if "cable output" in name_lower or ("cable" in name_lower and "vb-audio" in name_lower) or "vb-cable" in name_lower:
                        pStr = wintypes.LPWSTR()
                        try:
                            if GetId(pDev, ctypes.byref(pStr)) == 0 and pStr.value:
                                return str(pStr.value)
                        finally:
                            if pStr.value and ole32 and hasattr(ole32, 'CoTaskMemFree'):
                                ole32.CoTaskMemFree(pStr)
                finally:
                    _release_com_ptr(pStore)
                    _release_com_ptr(pDev)
        return None
    except Exception as e:
        logger.debug(f"native_get_cable_capture_guid exception: {e}")
        return None
    finally:
        _release_com_ptr(pCol)
        _release_com_ptr(pEnum)


def native_get_physical_capture_guid() -> Tuple[Optional[str], str]:
    """
    Direct in-process scan of active eCapture endpoints to locate the primary physical microphone.
    Excludes virtual audio bridges (CABLE, VB-Audio, VoiceMeeter, Virtual).
    Returns (endpoint_guid, friendly_name).
    """
    if sys.platform != 'win32' or not ole32:
        return None, "Unknown"

    pEnum = None
    pCol = None
    try:
        _safe_co_initialize()
        pEnum = ctypes.c_void_p()
        if ole32.CoCreateInstance(ctypes.byref(CLSID_MMDeviceEnumerator), None, 1 | 2 | 4, ctypes.byref(IID_IMMDeviceEnumerator), ctypes.byref(pEnum)) != 0:
            return None, "Unknown"

        enum_vtable = ctypes.cast(ctypes.cast(pEnum, ctypes.POINTER(ctypes.c_void_p)).contents, ctypes.POINTER(ctypes.c_void_p))
        EnumAudioEndpoints = ctypes.WINFUNCTYPE(ctypes.c_int, ctypes.c_void_p, ctypes.c_int, wintypes.DWORD, ctypes.POINTER(ctypes.c_void_p))(enum_vtable[3])

        pCol = ctypes.c_void_p()
        if EnumAudioEndpoints(pEnum, 1, 1, ctypes.byref(pCol)) != 0 or not pCol:
            return None, "Unknown"

        col_vtable = ctypes.cast(ctypes.cast(pCol, ctypes.POINTER(ctypes.c_void_p)).contents, ctypes.POINTER(ctypes.c_void_p))
        GetCount = ctypes.WINFUNCTYPE(ctypes.c_int, ctypes.c_void_p, ctypes.POINTER(wintypes.UINT))(col_vtable[3])
        Item = ctypes.WINFUNCTYPE(ctypes.c_int, ctypes.c_void_p, wintypes.UINT, ctypes.POINTER(ctypes.c_void_p))(col_vtable[4])

        count = wintypes.UINT()
        GetCount(pCol, ctypes.byref(count))

        for i in range(count.value):
            pDev = ctypes.c_void_p()
            if Item(pCol, i, ctypes.byref(pDev)) == 0 and pDev:
                pStore = None
                try:
                    dev_vtable = ctypes.cast(ctypes.cast(pDev, ctypes.POINTER(ctypes.c_void_p)).contents, ctypes.POINTER(ctypes.c_void_p))
                    OpenPropertyStore = ctypes.WINFUNCTYPE(ctypes.c_int, ctypes.c_void_p, ctypes.c_int, ctypes.POINTER(ctypes.c_void_p))(dev_vtable[4])
                    GetId = ctypes.WINFUNCTYPE(ctypes.c_int, ctypes.c_void_p, ctypes.POINTER(wintypes.LPWSTR))(dev_vtable[5])

                    name = ""
                    pStore = ctypes.c_void_p()
                    if OpenPropertyStore(pDev, 0, ctypes.byref(pStore)) == 0 and pStore:
                        store_vtable = ctypes.cast(ctypes.cast(pStore, ctypes.POINTER(ctypes.c_void_p)).contents, ctypes.POINTER(ctypes.c_void_p))
                        GetValue = ctypes.WINFUNCTYPE(ctypes.c_int, ctypes.c_void_p, ctypes.POINTER(PROPERTYKEY), ctypes.POINTER(PROPVARIANT))(store_vtable[5])
                        pv = PROPVARIANT()
                        try:
                            if GetValue(pStore, ctypes.byref(PKEY_Device_FriendlyName), ctypes.byref(pv)) == 0:
                                if pv.vt == 31 and pv.pwszVal:
                                    name = str(pv.pwszVal)
                        finally:
                            if ole32 and hasattr(ole32, 'PropVariantClear'):
                                ole32.PropVariantClear(ctypes.byref(pv))

                    name_lower = name.lower()
                    if "cable" not in name_lower and "vb-audio" not in name_lower and "voicemeeter" not in name_lower and "virtual" not in name_lower:
                        pStr = wintypes.LPWSTR()
                        try:
                            if GetId(pDev, ctypes.byref(pStr)) == 0 and pStr.value:
                                return str(pStr.value), name
                        finally:
                            if pStr.value and ole32 and hasattr(ole32, 'CoTaskMemFree'):
                                ole32.CoTaskMemFree(pStr)
                finally:
                    _release_com_ptr(pStore)
                    _release_com_ptr(pDev)
        return None, "Unknown"
    except Exception as e:
        logger.debug(f"native_get_physical_capture_guid exception: {e}")
        return None, "Unknown"
    finally:
        _release_com_ptr(pCol)
        _release_com_ptr(pEnum)


import re

ENDPOINT_GUID_PATTERN = re.compile(r"^(\{[0-9a-fA-F\.\-]+\}\.)?\{[0-9a-zA-Z\-]+\}$")


def native_set_default_endpoint(dev_id: str) -> bool:
    """
    Direct in-process call to IPolicyConfig::SetDefaultEndpoint.
    Sets all three roles: eConsole (0), eMultimedia (1), eCommunications (2).
    Returns True if at least one role succeeded with S_OK.
    """
    if sys.platform != 'win32' or not ole32 or not dev_id:
        return False

    dev_id_clean = dev_id.strip()
    if not ENDPOINT_GUID_PATTERN.match(dev_id_clean):
        logger.warning(f"Rejected invalid CoreAudio endpoint GUID format: {dev_id}")
        return False

    # Guard: Only use vtable[13] on Windows 10 1903+ (build 18362+) and Windows 11
    # On legacy Windows builds, vtable layout differs; fallback cleanly to AudioRestore.exe
    try:
        win_ver = sys.getwindowsversion()
        if win_ver.major < 10 or win_ver.build < 18362:
            logger.info(f"Windows build {win_ver.build} < 18362; falling back to AudioRestore helper.")
            return False
    except Exception:
        pass

    pPolicy = None
    try:
        _safe_co_initialize()
        pPolicy = ctypes.c_void_p()
        hr = ole32.CoCreateInstance(
            ctypes.byref(CLSID_PolicyConfigClient),
            None,
            1 | 2 | 4,
            ctypes.byref(IID_IPolicyConfig),
            ctypes.byref(pPolicy)
        )
        if hr != 0 or not pPolicy:
            return False

        vtable = ctypes.cast(ctypes.cast(pPolicy, ctypes.POINTER(ctypes.c_void_p)).contents, ctypes.POINTER(ctypes.c_void_p))
        if not vtable or not vtable[13]:
            return False
        SetDefaultEndpoint = ctypes.WINFUNCTYPE(ctypes.c_int, ctypes.c_void_p, wintypes.LPCWSTR, ctypes.c_uint)(vtable[13])

        # ERole: eConsole=0, eMultimedia=1, eCommunications=2
        hr0 = SetDefaultEndpoint(pPolicy, dev_id, 0)
        hr1 = SetDefaultEndpoint(pPolicy, dev_id, 1)
        hr2 = SetDefaultEndpoint(pPolicy, dev_id, 2)

        success = (hr0 == 0 or hr1 == 0 or hr2 == 0)
        if success:
            logger.info(f"Native COM SetDefaultEndpoint succeeded for '{dev_id}' (roles 0,1,2: {hex(hr0 & 0xffffffff)}, {hex(hr1 & 0xffffffff)}, {hex(hr2 & 0xffffffff)})")
        else:
            logger.warning(f"Native COM SetDefaultEndpoint failed for '{dev_id}' (roles 0,1,2: {hex(hr0 & 0xffffffff)}, {hex(hr1 & 0xffffffff)}, {hex(hr2 & 0xffffffff)})")
        return success
    except (Exception, OSError, ctypes.ArgumentError) as e:
        logger.warning(f"Native COM SetDefaultEndpoint error: {e}")
        return False
    finally:
        _release_com_ptr(pPolicy)


def get_audio_restore_exe() -> str:
    """Locates the bundled native AudioRestore.exe helper."""
    if getattr(sys, 'frozen', False):
        base_dir = getattr(sys, '_MEIPASS', os.path.dirname(sys.executable))
        exe_path = os.path.join(base_dir, "drivers", "vbcable", "AudioRestore.exe")
        if os.path.exists(exe_path):
            return exe_path
        alt_path = os.path.join(os.path.dirname(sys.executable), "drivers", "vbcable", "AudioRestore.exe")
        if os.path.exists(alt_path):
            return alt_path

    dev_path = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "drivers", "vbcable", "AudioRestore.exe"))
    return dev_path


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

