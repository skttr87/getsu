"""Windows CoreAudio access for Getsu: default endpoint queries, endpoint classification
(bus type, form factor), cached readers, endpoint GUID checks and default capture switching.
All calls go through in-process COM vtables (ctypes); no external process is started here."""
import os
import sys
import logging
import ctypes
from ctypes import wintypes
from typing import Any, Dict, List, Optional, Tuple

try:
    from src.diagnostics import get_file_handler
except ImportError:
    from diagnostics import get_file_handler

# Routing audit logger: shares the getsu.log file handler with the rest of the app, plus console output.
logger = logging.getLogger("GetsuRouter")
if not logger.handlers:
    logger.setLevel(logging.INFO)
    _fh = get_file_handler()
    if _fh is not None:
        logger.addHandler(_fh)
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

# Hardware Bus Enumerator Property Key: DEVPKEY_Device_EnumeratorName
PKEY_Device_EnumeratorName = PROPERTYKEY(
    GUID(0xa45c254e, 0xdf1c, 0x4efd, (wintypes.BYTE * 8)(0x80, 0x20, 0x67, 0xd1, 0x46, 0xa8, 0x50, 0xe0)),
    24
)


# PKEY_AudioEndpoint_FormFactor: VT_UI4 holding an EndpointFormFactor value (see EndpointFormFactor below).
# Source: https://learn.microsoft.com/en-us/windows/win32/coreaudio/pkey-audioendpoint-formfactor
PKEY_AudioEndpoint_FormFactor = PROPERTYKEY(
    GUID(0x1DA5D803, 0xD492, 0x4EDD, (wintypes.BYTE * 8)(0x8C, 0x23, 0xE0, 0xC0, 0xFF, 0xEE, 0x7F, 0x0E)),
    0
)

VT_UI4 = 19
VT_LPWSTR = 31


class EndpointFormFactor:
    """EndpointFormFactor enumeration from mmdeviceapi.h (values in declaration order, starting at 0).
    Source: https://learn.microsoft.com/en-us/windows/win32/api/mmdeviceapi/ne-mmdeviceapi-endpointformfactor
    """
    REMOTE_NETWORK_DEVICE = 0
    SPEAKERS = 1
    LINE_LEVEL = 2
    HEADPHONES = 3
    MICROPHONE = 4
    HEADSET = 5
    HANDSET = 6
    UNKNOWN_DIGITAL_PASSTHROUGH = 7
    SPDIF = 8
    DIGITAL_AUDIO_DISPLAY_DEVICE = 9
    UNKNOWN_FORM_FACTOR = 10


class DeviceBusType:
    BLUETOOTH_HFP = "BTHHFENUM"   # Bluetooth Hands-Free Profile (Mic + Bidirectional)
    BLUETOOTH_A2DP = "BTHENUM"    # Bluetooth High-Quality Stereo Playback
    BLUETOOTH_LE = "BTHLEENUM"    # Windows 11 Bluetooth Low Energy (LC3)
    USB = "USB"                   # Dedicated USB Audio / 2.4GHz Dongles
    INTERNAL_HD = "HDAUDIO"       # High Definition Audio (Realtek / Motherboard)
    INTEL_AUDIO = "INTELAUDIO"   # Intel Smart Sound / High Definition
    VIRTUAL = "SWD"               # Software Virtual Drivers (VB-Audio, VoiceMeeter)
    ROOT = "ROOT"                 # Root-enumerated software devices (VB-Audio CABLE registers as ROOT\MEDIA\0000)
    UNKNOWN = "UNKNOWN"

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


def native_get_current_default_playback() -> Tuple[Optional[str], str]:
    """
    Direct in-process query for the active Windows default playback (render) endpoint.
    Checks eConsole (0) first, with fallback to eMultimedia (1) and eCommunications (2).
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
        # eRender = 0, eConsole = 0
        hr = GetDefaultAudioEndpoint(pEnum, 0, 0, ctypes.byref(pDev))
        if hr != 0 or not pDev:
            # Fallback to eMultimedia = 1
            hr = GetDefaultAudioEndpoint(pEnum, 0, 1, ctypes.byref(pDev))
            if hr != 0 or not pDev:
                hr = GetDefaultAudioEndpoint(pEnum, 0, 2, ctypes.byref(pDev))
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
                if GetValue(pStore, ctypes.byref(PKEY_Device_FriendlyName), ctypes.byref(pv)) == 0:
                    if pv.vt == 31 and pv.pwszVal:
                        name = str(pv.pwszVal)
            finally:
                if ole32 and hasattr(ole32, 'PropVariantClear'):
                    ole32.PropVariantClear(ctypes.byref(pv))

        return dev_id, name
    except Exception as e:
        logger.debug(f"native_get_current_default_playback exception: {e}")
        return None, "Unknown"
    finally:
        _release_com_ptr(pStore)
        _release_com_ptr(pDev)
        _release_com_ptr(pEnum)


def _is_endpoint_id(value: Optional[str]) -> bool:
    """True when value is a CoreAudio endpoint ID such as {0.0.1.00000000}.{GUID}, not a friendly name.
    Friendly names can contain dots and braces (for example "Speakers (2.1 Audio)"), so they are not checked by character."""
    return bool(value) and ENDPOINT_GUID_PATTERN.match(value.strip()) is not None


def _bus_from_enumerator(enumerator_name: str) -> str:
    """Maps a PKEY_Device_EnumeratorName value to a DeviceBusType string (the upper-cased name when unknown)."""
    enumerator = str(enumerator_name).strip().upper()
    if "BTHHFENUM" in enumerator:
        return DeviceBusType.BLUETOOTH_HFP
    elif "BTHENUM" in enumerator:
        return DeviceBusType.BLUETOOTH_A2DP
    elif "BTHLEENUM" in enumerator:
        return DeviceBusType.BLUETOOTH_LE
    elif "USB" in enumerator:
        return DeviceBusType.USB
    elif "HDAUDIO" in enumerator:
        return DeviceBusType.INTERNAL_HD
    elif "INTELAUDIO" in enumerator:
        return DeviceBusType.INTEL_AUDIO
    elif "SWD" in enumerator:
        return DeviceBusType.VIRTUAL
    elif enumerator.split("\\")[0] == "ROOT":
        return DeviceBusType.ROOT
    return enumerator


def native_get_device_bus_type(device_id_or_name: Optional[str] = None, is_input: bool = True) -> str:
    """
    Direct in-process query retrieving the hardware driver bus enumerator name (PKEY_Device_EnumeratorName).
    Returns DeviceBusType string (e.g. 'BTHHFENUM', 'BTHENUM', 'USB', 'HDAUDIO', 'SWD') or 'UNKNOWN'.
    """
    if sys.platform != 'win32' or not ole32:
        return DeviceBusType.UNKNOWN

    pEnum = None
    pDev = None
    pStore = None
    pCol = None
    try:
        _safe_co_initialize()
        pEnum = ctypes.c_void_p()
        if ole32.CoCreateInstance(ctypes.byref(CLSID_MMDeviceEnumerator), None, 1 | 2 | 4, ctypes.byref(IID_IMMDeviceEnumerator), ctypes.byref(pEnum)) != 0 or not pEnum:
            return DeviceBusType.UNKNOWN

        enum_vtable = ctypes.cast(ctypes.cast(pEnum, ctypes.POINTER(ctypes.c_void_p)).contents, ctypes.POINTER(ctypes.c_void_p))
        dataflow = 1 if is_input else 0

        # Branch 1: Specific ID (GUID or endpoint ID string)
        if _is_endpoint_id(device_id_or_name):
            GetDevice = ctypes.WINFUNCTYPE(ctypes.c_int, ctypes.c_void_p, wintypes.LPCWSTR, ctypes.POINTER(ctypes.c_void_p))(enum_vtable[5])
            pDev = ctypes.c_void_p()
            if GetDevice(pEnum, str(device_id_or_name), ctypes.byref(pDev)) != 0 or not pDev:
                return DeviceBusType.UNKNOWN
        # Branch 2: Default device if no name/ID specified
        elif not device_id_or_name:
            GetDefaultAudioEndpoint = ctypes.WINFUNCTYPE(ctypes.c_int, ctypes.c_void_p, ctypes.c_int, ctypes.c_int, ctypes.POINTER(ctypes.c_void_p))(enum_vtable[4])
            pDev = ctypes.c_void_p()
            if GetDefaultAudioEndpoint(pEnum, dataflow, 0, ctypes.byref(pDev)) != 0 or not pDev:
                return DeviceBusType.UNKNOWN
        # Branch 3: Friendly name match across active endpoints
        else:
            EnumAudioEndpoints = ctypes.WINFUNCTYPE(ctypes.c_int, ctypes.c_void_p, ctypes.c_int, wintypes.DWORD, ctypes.POINTER(ctypes.c_void_p))(enum_vtable[3])
            pCol = ctypes.c_void_p()
            if EnumAudioEndpoints(pEnum, dataflow, 1, ctypes.byref(pCol)) != 0 or not pCol:
                return DeviceBusType.UNKNOWN
            col_vtable = ctypes.cast(ctypes.cast(pCol, ctypes.POINTER(ctypes.c_void_p)).contents, ctypes.POINTER(ctypes.c_void_p))
            GetCount = ctypes.WINFUNCTYPE(ctypes.c_int, ctypes.c_void_p, ctypes.POINTER(wintypes.UINT))(col_vtable[3])
            Item = ctypes.WINFUNCTYPE(ctypes.c_int, ctypes.c_void_p, wintypes.UINT, ctypes.POINTER(ctypes.c_void_p))(col_vtable[4])
            count = wintypes.UINT()
            GetCount(pCol, ctypes.byref(count))

            target_name_lower = device_id_or_name.lower().strip()
            for i in range(count.value):
                item_dev = ctypes.c_void_p()
                if Item(pCol, i, ctypes.byref(item_dev)) == 0 and item_dev:
                    item_vtable = ctypes.cast(ctypes.cast(item_dev, ctypes.POINTER(ctypes.c_void_p)).contents, ctypes.POINTER(ctypes.c_void_p))
                    item_OpenStore = ctypes.WINFUNCTYPE(ctypes.c_int, ctypes.c_void_p, ctypes.c_int, ctypes.POINTER(ctypes.c_void_p))(item_vtable[4])
                    item_store = ctypes.c_void_p()
                    matched = False
                    if item_OpenStore(item_dev, 0, ctypes.byref(item_store)) == 0 and item_store:
                        st_vtable = ctypes.cast(ctypes.cast(item_store, ctypes.POINTER(ctypes.c_void_p)).contents, ctypes.POINTER(ctypes.c_void_p))
                        st_GetValue = ctypes.WINFUNCTYPE(ctypes.c_int, ctypes.c_void_p, ctypes.POINTER(PROPERTYKEY), ctypes.POINTER(PROPVARIANT))(st_vtable[5])
                        pv = PROPVARIANT()
                        if st_GetValue(item_store, ctypes.byref(PKEY_Device_FriendlyName), ctypes.byref(pv)) == 0:
                            if pv.vt == 31 and pv.pwszVal:
                                dev_name = str(pv.pwszVal).lower()
                                if target_name_lower in dev_name or dev_name in target_name_lower:
                                    matched = True
                        if ole32 and hasattr(ole32, 'PropVariantClear'):
                            ole32.PropVariantClear(ctypes.byref(pv))
                        _release_com_ptr(item_store)
                    if matched:
                        pDev = item_dev
                        break
                    _release_com_ptr(item_dev)

        if not pDev:
            return DeviceBusType.UNKNOWN

        dev_vtable = ctypes.cast(ctypes.cast(pDev, ctypes.POINTER(ctypes.c_void_p)).contents, ctypes.POINTER(ctypes.c_void_p))
        OpenPropertyStore = ctypes.WINFUNCTYPE(ctypes.c_int, ctypes.c_void_p, ctypes.c_int, ctypes.POINTER(ctypes.c_void_p))(dev_vtable[4])
        pStore = ctypes.c_void_p()
        if OpenPropertyStore(pDev, 0, ctypes.byref(pStore)) == 0 and pStore:
            store_vtable = ctypes.cast(ctypes.cast(pStore, ctypes.POINTER(ctypes.c_void_p)).contents, ctypes.POINTER(ctypes.c_void_p))
            GetValue = ctypes.WINFUNCTYPE(ctypes.c_int, ctypes.c_void_p, ctypes.POINTER(PROPERTYKEY), ctypes.POINTER(PROPVARIANT))(store_vtable[5])
            pv = PROPVARIANT()
            try:
                if GetValue(pStore, ctypes.byref(PKEY_Device_EnumeratorName), ctypes.byref(pv)) == 0:
                    if pv.vt == 31 and pv.pwszVal:
                        return _bus_from_enumerator(str(pv.pwszVal))
            finally:
                if ole32 and hasattr(ole32, 'PropVariantClear'):
                    ole32.PropVariantClear(ctypes.byref(pv))
        return DeviceBusType.UNKNOWN
    except Exception as e:
        logger.debug(f"native_get_device_bus_type exception: {e}")
        return DeviceBusType.UNKNOWN
    finally:
        _release_com_ptr(pCol)
        _release_com_ptr(pStore)
        _release_com_ptr(pDev)
        _release_com_ptr(pEnum)


_bus_type_cache: dict = {}


_form_factor_cache: dict = {}


def clear_device_bus_type_cache():
    """Forget cached bus types and form factors. Call at the start of every device rescan (hotplug, manual refresh)."""
    _bus_type_cache.clear()
    _form_factor_cache.clear()


def native_get_device_bus_type_cached(device_id_or_name: Optional[str] = None, is_input: bool = True) -> str:
    """
    native_get_device_bus_type() memoised per (device_id_or_name, is_input).
    Used by device enumeration, where the same endpoint is classified several times in one pass.
    The cache lives until clear_device_bus_type_cache(). It is never read from the audio callback.
    """
    key = (device_id_or_name, bool(is_input))
    cached = _bus_type_cache.get(key)
    if cached is not None:
        return cached
    value = native_get_device_bus_type(device_id_or_name, is_input=is_input)
    _bus_type_cache[key] = value
    return value


def _form_factor_from_variant(pv: PROPVARIANT) -> Optional[int]:
    """Decodes a VT_UI4 PROPVARIANT holding an EndpointFormFactor value. The ulVal member is at offset 8."""
    if pv.vt != VT_UI4:
        return None
    return int(ctypes.c_uint32.from_buffer(pv, 8).value)


def _endpoint_name_matches(item_dev, target_name_lower: str) -> bool:
    """True when the endpoint's PKEY_Device_FriendlyName matches target (same substring rule as the bus lookup)."""
    item_store = ctypes.c_void_p()
    matched = False
    try:
        item_vtable = ctypes.cast(ctypes.cast(item_dev, ctypes.POINTER(ctypes.c_void_p)).contents, ctypes.POINTER(ctypes.c_void_p))
        item_OpenStore = ctypes.WINFUNCTYPE(ctypes.c_int, ctypes.c_void_p, ctypes.c_int, ctypes.POINTER(ctypes.c_void_p))(item_vtable[4])
        if item_OpenStore(item_dev, 0, ctypes.byref(item_store)) == 0 and item_store:
            st_vtable = ctypes.cast(ctypes.cast(item_store, ctypes.POINTER(ctypes.c_void_p)).contents, ctypes.POINTER(ctypes.c_void_p))
            st_GetValue = ctypes.WINFUNCTYPE(ctypes.c_int, ctypes.c_void_p, ctypes.POINTER(PROPERTYKEY), ctypes.POINTER(PROPVARIANT))(st_vtable[5])
            pv = PROPVARIANT()
            try:
                if st_GetValue(item_store, ctypes.byref(PKEY_Device_FriendlyName), ctypes.byref(pv)) == 0:
                    if pv.vt == VT_LPWSTR and pv.pwszVal:
                        dev_name = str(pv.pwszVal).lower()
                        matched = target_name_lower in dev_name or dev_name in target_name_lower
            finally:
                if ole32 and hasattr(ole32, 'PropVariantClear'):
                    ole32.PropVariantClear(ctypes.byref(pv))
    except Exception:
        matched = False
    finally:
        _release_com_ptr(item_store)
    return matched


def native_get_device_form_factor(device_name: Optional[str], is_input: bool = True) -> Optional[int]:
    """
    Reads PKEY_AudioEndpoint_FormFactor (an EndpointFormFactor value) of the active capture (is_input) or
    render endpoint given by friendly name or by endpoint ID. Read-only: one property read per call.
    Returns None when the endpoint or the property is not available.
    """
    if sys.platform != 'win32' or not ole32 or not device_name:
        return None

    pEnum = None
    pCol = None
    pDev = None
    pStore = None
    try:
        _safe_co_initialize()
        pEnum = ctypes.c_void_p()
        if ole32.CoCreateInstance(ctypes.byref(CLSID_MMDeviceEnumerator), None, 1 | 2 | 4, ctypes.byref(IID_IMMDeviceEnumerator), ctypes.byref(pEnum)) != 0 or not pEnum:
            return None

        enum_vtable = ctypes.cast(ctypes.cast(pEnum, ctypes.POINTER(ctypes.c_void_p)).contents, ctypes.POINTER(ctypes.c_void_p))
        EnumAudioEndpoints = ctypes.WINFUNCTYPE(ctypes.c_int, ctypes.c_void_p, ctypes.c_int, wintypes.DWORD, ctypes.POINTER(ctypes.c_void_p))(enum_vtable[3])
        dataflow = 1 if is_input else 0
        pCol = ctypes.c_void_p()
        # DEVICE_STATE_ACTIVE = 1
        if EnumAudioEndpoints(pEnum, dataflow, 1, ctypes.byref(pCol)) != 0 or not pCol:
            return None

        col_vtable = ctypes.cast(ctypes.cast(pCol, ctypes.POINTER(ctypes.c_void_p)).contents, ctypes.POINTER(ctypes.c_void_p))
        GetCount = ctypes.WINFUNCTYPE(ctypes.c_int, ctypes.c_void_p, ctypes.POINTER(wintypes.UINT))(col_vtable[3])
        Item = ctypes.WINFUNCTYPE(ctypes.c_int, ctypes.c_void_p, wintypes.UINT, ctypes.POINTER(ctypes.c_void_p))(col_vtable[4])
        count = wintypes.UINT()
        GetCount(pCol, ctypes.byref(count))

        if _is_endpoint_id(device_name):
            # Endpoint ID: direct lookup, no name matching
            GetDevice = ctypes.WINFUNCTYPE(ctypes.c_int, ctypes.c_void_p, wintypes.LPCWSTR, ctypes.POINTER(ctypes.c_void_p))(enum_vtable[5])
            pDev = ctypes.c_void_p()
            if GetDevice(pEnum, device_name.strip(), ctypes.byref(pDev)) != 0 or not pDev:
                return None
        else:
            target_name_lower = device_name.lower().strip()
            for i in range(count.value):
                item_dev = ctypes.c_void_p()
                if Item(pCol, i, ctypes.byref(item_dev)) != 0 or not item_dev:
                    continue
                if _endpoint_name_matches(item_dev, target_name_lower):
                    pDev = item_dev
                    break
                _release_com_ptr(item_dev)

        if not pDev:
            return None

        dev_vtable = ctypes.cast(ctypes.cast(pDev, ctypes.POINTER(ctypes.c_void_p)).contents, ctypes.POINTER(ctypes.c_void_p))
        OpenPropertyStore = ctypes.WINFUNCTYPE(ctypes.c_int, ctypes.c_void_p, ctypes.c_int, ctypes.POINTER(ctypes.c_void_p))(dev_vtable[4])
        pStore = ctypes.c_void_p()
        if OpenPropertyStore(pDev, 0, ctypes.byref(pStore)) != 0 or not pStore:
            return None

        store_vtable = ctypes.cast(ctypes.cast(pStore, ctypes.POINTER(ctypes.c_void_p)).contents, ctypes.POINTER(ctypes.c_void_p))
        GetValue = ctypes.WINFUNCTYPE(ctypes.c_int, ctypes.c_void_p, ctypes.POINTER(PROPERTYKEY), ctypes.POINTER(PROPVARIANT))(store_vtable[5])
        pv = PROPVARIANT()
        try:
            if GetValue(pStore, ctypes.byref(PKEY_AudioEndpoint_FormFactor), ctypes.byref(pv)) != 0:
                return None
            return _form_factor_from_variant(pv)
        finally:
            if ole32 and hasattr(ole32, 'PropVariantClear'):
                ole32.PropVariantClear(ctypes.byref(pv))
    except Exception as e:
        logger.debug(f"native_get_device_form_factor exception: {e}")
        return None
    finally:
        _release_com_ptr(pStore)
        _release_com_ptr(pDev)
        _release_com_ptr(pCol)
        _release_com_ptr(pEnum)


def native_get_device_form_factor_cached(device_name: Optional[str] = None, is_input: bool = True) -> Optional[int]:
    """
    native_get_device_form_factor() memoised per (device_name, is_input), including None results.
    Cleared together with the bus-type cache by clear_device_bus_type_cache().
    """
    key = (device_name, bool(is_input))
    if key in _form_factor_cache:
        return _form_factor_cache[key]
    value = native_get_device_form_factor(device_name, is_input=is_input)
    _form_factor_cache[key] = value
    return value


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


def _describe_endpoint(pDev) -> Optional[Dict[str, Any]]:
    """Reads id, friendly name, bus type and form factor of one IMMDevice. Read-only."""
    pStore = ctypes.c_void_p()
    pStr = wintypes.LPWSTR()
    endpoint_id = None
    try:
        dev_vtable = ctypes.cast(ctypes.cast(pDev, ctypes.POINTER(ctypes.c_void_p)).contents, ctypes.POINTER(ctypes.c_void_p))
        OpenPropertyStore = ctypes.WINFUNCTYPE(ctypes.c_int, ctypes.c_void_p, ctypes.c_int, ctypes.POINTER(ctypes.c_void_p))(dev_vtable[4])
        GetId = ctypes.WINFUNCTYPE(ctypes.c_int, ctypes.c_void_p, ctypes.POINTER(wintypes.LPWSTR))(dev_vtable[5])
        if GetId(pDev, ctypes.byref(pStr)) == 0 and pStr.value:
            endpoint_id = str(pStr.value)
    finally:
        if pStr.value and ole32 and hasattr(ole32, 'CoTaskMemFree'):
            ole32.CoTaskMemFree(pStr)
    if not endpoint_id:
        return None

    name = ""
    bus = DeviceBusType.UNKNOWN
    form_factor = None
    try:
        if OpenPropertyStore(pDev, 0, ctypes.byref(pStore)) == 0 and pStore.value:
            store_vtable = ctypes.cast(ctypes.cast(pStore, ctypes.POINTER(ctypes.c_void_p)).contents, ctypes.POINTER(ctypes.c_void_p))
            GetValue = ctypes.WINFUNCTYPE(ctypes.c_int, ctypes.c_void_p, ctypes.POINTER(PROPERTYKEY), ctypes.POINTER(PROPVARIANT))(store_vtable[5])
            pv = PROPVARIANT()
            try:
                if GetValue(pStore, ctypes.byref(PKEY_Device_FriendlyName), ctypes.byref(pv)) == 0:
                    if pv.vt == VT_LPWSTR and pv.pwszVal:
                        name = str(pv.pwszVal)
            finally:
                if ole32 and hasattr(ole32, 'PropVariantClear'):
                    ole32.PropVariantClear(ctypes.byref(pv))
            pbus = PROPVARIANT()
            try:
                if GetValue(pStore, ctypes.byref(PKEY_Device_EnumeratorName), ctypes.byref(pbus)) == 0:
                    if pbus.vt == VT_LPWSTR and pbus.pwszVal:
                        bus = _bus_from_enumerator(str(pbus.pwszVal))
            finally:
                if ole32 and hasattr(ole32, 'PropVariantClear'):
                    ole32.PropVariantClear(ctypes.byref(pbus))
            pff = PROPVARIANT()
            try:
                if GetValue(pStore, ctypes.byref(PKEY_AudioEndpoint_FormFactor), ctypes.byref(pff)) == 0:
                    form_factor = _form_factor_from_variant(pff)
            finally:
                if ole32 and hasattr(ole32, 'PropVariantClear'):
                    ole32.PropVariantClear(ctypes.byref(pff))
    finally:
        if pStore.value:
            _release_com_ptr(pStore)
    return {"id": endpoint_id, "name": name, "bus": bus, "form_factor": form_factor}


def native_list_active_endpoints(is_input: bool) -> List[Dict[str, Any]]:
    """
    Lists the active capture (is_input) or render endpoints in one COM pass.
    Each entry is {"id", "name", "bus", "form_factor"}; bus is a DeviceBusType string and form_factor an
    EndpointFormFactor value or None. Read-only. Returns an empty list when COM is not available.
    """
    if sys.platform != 'win32' or not ole32:
        return []

    pEnum = None
    pCol = None
    try:
        _safe_co_initialize()
        pEnum = ctypes.c_void_p()
        if ole32.CoCreateInstance(ctypes.byref(CLSID_MMDeviceEnumerator), None, 1 | 2 | 4, ctypes.byref(IID_IMMDeviceEnumerator), ctypes.byref(pEnum)) != 0 or not pEnum:
            return []

        enum_vtable = ctypes.cast(ctypes.cast(pEnum, ctypes.POINTER(ctypes.c_void_p)).contents, ctypes.POINTER(ctypes.c_void_p))
        EnumAudioEndpoints = ctypes.WINFUNCTYPE(ctypes.c_int, ctypes.c_void_p, ctypes.c_int, wintypes.DWORD, ctypes.POINTER(ctypes.c_void_p))(enum_vtable[3])
        dataflow = 1 if is_input else 0  # eCapture = 1, eRender = 0
        pCol = ctypes.c_void_p()
        # DEVICE_STATE_ACTIVE = 1
        if EnumAudioEndpoints(pEnum, dataflow, 1, ctypes.byref(pCol)) != 0 or not pCol:
            return []

        col_vtable = ctypes.cast(ctypes.cast(pCol, ctypes.POINTER(ctypes.c_void_p)).contents, ctypes.POINTER(ctypes.c_void_p))
        GetCount = ctypes.WINFUNCTYPE(ctypes.c_int, ctypes.c_void_p, ctypes.POINTER(wintypes.UINT))(col_vtable[3])
        Item = ctypes.WINFUNCTYPE(ctypes.c_int, ctypes.c_void_p, wintypes.UINT, ctypes.POINTER(ctypes.c_void_p))(col_vtable[4])
        count = wintypes.UINT()
        GetCount(pCol, ctypes.byref(count))

        result: List[Dict[str, Any]] = []
        for i in range(count.value):
            pDev = ctypes.c_void_p()
            if Item(pCol, i, ctypes.byref(pDev)) != 0 or not pDev:
                continue
            try:
                entry = _describe_endpoint(pDev)
                if entry is not None:
                    result.append(entry)
            finally:
                _release_com_ptr(pDev)
        return result
    except Exception as e:
        logger.debug(f"native_list_active_endpoints exception: {e}")
        return []
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


PLAYBACK_ENDPOINT_PREFIX = "{0.0.0."


def set_default_playback_away_from_cable(target_id: str) -> bool:
    """
    The only function in src that sets a playback (render) default. It moves the default playback
    (all three roles) from the virtual cable to target_id, a physical device, and nothing else.
    Refuses, logs a warning and returns False unless all of these hold:
    - target_id is a render endpoint ID (prefix {0.0.0.);
    - the target is an active render endpoint and is not virtual;
    - the current default playback (console role) is the VB-CABLE Getsu installs (CABLE Input).
    """
    if sys.platform != 'win32' or not ole32:
        return False
    if not target_id or not ENDPOINT_GUID_PATTERN.match(target_id.strip()) or not target_id.strip().startswith(PLAYBACK_ENDPOINT_PREFIX):
        logger.warning(f"Playback default not changed: '{target_id}' is not a render endpoint ID.")
        return False
    target_id = target_id.strip()

    from src.devices import is_getsu_cable_endpoint, is_virtual_endpoint

    render = native_list_active_endpoints(is_input=False)
    target = next((e for e in render if e["id"] == target_id), None)
    if target is None:
        logger.warning(f"Playback default not changed: target {target_id} is not an active render endpoint.")
        return False
    if is_virtual_endpoint(target["name"], target["bus"]):
        logger.warning(f"Playback default not changed: target '{target['name']}' is a virtual device.")
        return False

    current_id, _ = native_get_current_default_playback()
    current = next((e for e in render if current_id and e["id"] == current_id), None)
    if current is None or not is_getsu_cable_endpoint(current["name"]):
        logger.warning("Playback default not changed: the current default playback is not Getsu's VB-CABLE.")
        return False

    ok = native_set_default_endpoint(target_id)
    if ok:
        logger.info(f"Default playback moved from '{current['name']}' to '{target['name']}' ({target_id}).")
    else:
        logger.warning(f"Default playback change to '{target['name']}' failed.")
    return ok


# --- VB-CABLE format (owner decision 2026-11-24): the only code that sets a device format -------------------------
# IPolicyConfig vtable (Windows 7 and later): 4 GetDeviceFormat, 6 SetDeviceFormat, 13 SetDefaultEndpoint (used above).
_VT_GET_DEVICE_FORMAT = 4
_VT_SET_DEVICE_FORMAT = 6
WAVE_FORMAT_EXTENSIBLE = 0xFFFE
_KS_GUID_TAIL = (0x80, 0x00, 0x00, 0xAA, 0x00, 0x38, 0x9B, 0x71)
KSDATAFORMAT_SUBTYPE_PCM = GUID(0x00000001, 0x0000, 0x0010, (wintypes.BYTE * 8)(*_KS_GUID_TAIL))
KSDATAFORMAT_SUBTYPE_IEEE_FLOAT = GUID(0x00000003, 0x0000, 0x0010, (wintypes.BYTE * 8)(*_KS_GUID_TAIL))
SPEAKER_FRONT_LEFT_RIGHT = 0x3


class WAVEFORMATEX(ctypes.Structure):
    _pack_ = 1
    _fields_ = [
        ("wFormatTag", wintypes.WORD), ("nChannels", wintypes.WORD), ("nSamplesPerSec", wintypes.DWORD),
        ("nAvgBytesPerSec", wintypes.DWORD), ("nBlockAlign", wintypes.WORD), ("wBitsPerSample", wintypes.WORD),
        ("cbSize", wintypes.WORD),
    ]


class WAVEFORMATEXTENSIBLE(ctypes.Structure):
    _pack_ = 1
    _fields_ = [("Format", WAVEFORMATEX), ("wValidBitsPerSample", wintypes.WORD), ("dwChannelMask", wintypes.DWORD),
                ("SubFormat", GUID)]


def make_wave_format(rate: int, channels: int, bits: int, is_float: bool) -> WAVEFORMATEXTENSIBLE:
    """WAVEFORMATEXTENSIBLE for a PCM (endpoint) or float (shared-mode mix) format."""
    w = WAVEFORMATEXTENSIBLE()
    f = w.Format
    f.wFormatTag = WAVE_FORMAT_EXTENSIBLE
    f.nChannels = channels
    f.nSamplesPerSec = rate
    f.wBitsPerSample = bits
    f.nBlockAlign = channels * bits // 8
    f.nAvgBytesPerSec = rate * f.nBlockAlign
    f.cbSize = ctypes.sizeof(WAVEFORMATEXTENSIBLE) - ctypes.sizeof(WAVEFORMATEX)  # 22
    w.wValidBitsPerSample = bits
    w.dwChannelMask = SPEAKER_FRONT_LEFT_RIGHT if channels == 2 else 0
    w.SubFormat = KSDATAFORMAT_SUBTYPE_IEEE_FLOAT if is_float else KSDATAFORMAT_SUBTYPE_PCM
    return w


def _policy_config_supported() -> bool:
    try:
        v = sys.getwindowsversion()
        return v.major >= 10 and v.build >= 18362
    except Exception:
        return False


def _with_policy_config(action):
    """Runs action(pPolicy, vtable) on a fresh IPolicyConfig and releases it. Returns None when COM is unavailable."""
    if sys.platform != 'win32' or not ole32 or not _policy_config_supported():
        return None
    pPolicy = None
    try:
        _safe_co_initialize()
        pPolicy = ctypes.c_void_p()
        hr = ole32.CoCreateInstance(ctypes.byref(CLSID_PolicyConfigClient), None, 1 | 2 | 4,
                                    ctypes.byref(IID_IPolicyConfig), ctypes.byref(pPolicy))
        if hr != 0 or not pPolicy:
            return None
        vtable = ctypes.cast(ctypes.cast(pPolicy, ctypes.POINTER(ctypes.c_void_p)).contents, ctypes.POINTER(ctypes.c_void_p))
        return action(pPolicy, vtable)
    finally:
        _release_com_ptr(pPolicy)


def native_get_endpoint_format(endpoint_id: str) -> Optional[Tuple[int, int, int]]:
    """(sample rate, channels, bits) of an endpoint's shared-mode format as Windows stores it, or None. Read-only."""
    if not endpoint_id or not ENDPOINT_GUID_PATTERN.match(endpoint_id.strip()):
        return None

    def action(pPolicy, vtable):
        get_fmt = ctypes.WINFUNCTYPE(ctypes.c_int, ctypes.c_void_p, wintypes.LPCWSTR, ctypes.c_int,
                                     ctypes.POINTER(ctypes.c_void_p))(vtable[_VT_GET_DEVICE_FORMAT])
        p_fmt = ctypes.c_void_p()
        if get_fmt(pPolicy, endpoint_id.strip(), 0, ctypes.byref(p_fmt)) != 0 or not p_fmt:
            return None
        try:
            f = ctypes.cast(p_fmt, ctypes.POINTER(WAVEFORMATEX)).contents
            return int(f.nSamplesPerSec), int(f.nChannels), int(f.wBitsPerSample)
        finally:
            ole32.CoTaskMemFree(p_fmt)

    try:
        return _with_policy_config(action)
    except (Exception, OSError) as e:
        logger.warning(f"Reading the format of {endpoint_id} failed: {e}")
        return None


def set_getsu_cable_format(endpoint_id: str, rate: int = 48000) -> bool:
    """
    The only function in src that sets a device format. It sets 2 channel, 16 bit, <rate> Hz on one endpoint of the
    VB-CABLE that Getsu installs (CABLE Input or CABLE Output) and refuses any other device. Returns True when Windows
    reports the new rate afterwards. Fails (for example while another app uses the cable) without side effects.
    """
    from src.devices import is_getsu_cable_endpoint

    if not endpoint_id or not ENDPOINT_GUID_PATTERN.match(endpoint_id.strip()):
        logger.warning(f"Cable format not changed: '{endpoint_id}' is not an endpoint ID.")
        return False
    endpoint_id = endpoint_id.strip()
    active = native_list_active_endpoints(is_input=True) + native_list_active_endpoints(is_input=False)
    target = next((e for e in active if e["id"] == endpoint_id), None)
    if target is None or not is_getsu_cable_endpoint(target["name"]):
        logger.warning(f"Cable format not changed: {endpoint_id} is not an active VB-CABLE endpoint of Getsu.")
        return False

    endpoint_fmt = make_wave_format(rate, 2, 16, is_float=False)
    mix_fmt = make_wave_format(rate, 2, 32, is_float=True)

    def action(pPolicy, vtable):
        set_fmt = ctypes.WINFUNCTYPE(ctypes.c_int, ctypes.c_void_p, wintypes.LPCWSTR, ctypes.c_void_p,
                                     ctypes.c_void_p)(vtable[_VT_SET_DEVICE_FORMAT])
        return set_fmt(pPolicy, endpoint_id, ctypes.addressof(endpoint_fmt), ctypes.addressof(mix_fmt))

    try:
        hr = _with_policy_config(action)
    except (Exception, OSError) as e:
        logger.warning(f"Setting the format of '{target['name']}' failed: {e}")
        return False
    after = native_get_endpoint_format(endpoint_id)
    ok = hr == 0 and after is not None and after[0] == rate
    if ok:
        logger.info(f"[CABLE FORMAT] '{target['name']}' set to 2 ch, 16 bit, {rate} Hz.")
    else:
        hr_text = "none" if hr is None else hex(hr & 0xffffffff)
        logger.warning(f"[CABLE FORMAT] '{target['name']}' not changed (hr {hr_text}, now {after}).")
    return ok


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
