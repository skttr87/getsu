"""
Audio Device Enumeration and VB-Audio Virtual Cable Management for Getsu.
"""
import os
import sys
import time
import subprocess
from typing import List, Dict, Optional, Tuple, Any, Union
import sounddevice as sd


def _start_enumeration_pass():
    """Each enumeration pass starts with an empty bus-type cache, so hotplugged devices are classified fresh."""
    try:
        from src.router import clear_device_bus_type_cache
        clear_device_bus_type_cache()
    except Exception:
        pass


def get_all_devices(force_rescan: bool = False) -> List[Dict]:
    """Retrieve all sound devices with HostAPI details."""
    _start_enumeration_pass()
    try:
        if force_rescan:
            try:
                if hasattr(sd, '_terminate') and hasattr(sd, '_initialize'):
                    sd._terminate()
                    sd._initialize()
            except Exception as e:
                print(f"[WARN] Failed to reinitialize PortAudio: {e}")
        devices = sd.query_devices()
        host_apis = sd.query_hostapis()
        if isinstance(devices, dict):
            devices = [devices]
        result = []
        for i, d in enumerate(devices):
            if not isinstance(d, dict):
                continue
            try:
                host_api_idx = d.get('hostapi')
                if isinstance(host_api_idx, int) and isinstance(host_apis, (list, tuple)) and 0 <= host_api_idx < len(host_apis):
                    host_name = host_apis[host_api_idx]['name']
                elif isinstance(host_api_idx, str):
                    host_name = host_api_idx
                elif isinstance(host_apis, dict) and host_api_idx in host_apis:
                    host_val = host_apis[host_api_idx]
                    host_name = host_val['name'] if isinstance(host_val, dict) else str(host_val)
                else:
                    host_name = 'WASAPI'
            except Exception:
                host_name = 'WASAPI'

            # Exclude raw Windows WDM-KS devices: WDM-KS uses legacy kernel IOCTLs
            # that fail with GLE 0x00000492 on modern Realtek/Intel HD audio drivers
            if 'WDM-KS' in host_name.upper():
                continue

            result.append({
                'index': i,
                'name': d.get('name', f'Device #{i}'),
                'hostapi': host_name,
                'inputs': d.get('max_input_channels', 0),
                'outputs': d.get('max_output_channels', 0),
                'samplerate': d.get('default_samplerate', 48000.0),
            })
        return result
    except Exception as e:
        print(f"[ERROR] Failed to query audio devices: {e}")
        return []


def check_vbcable_via_audiorestore() -> bool:
    """Invokes bundled native AudioRestore.exe to check if VB-Cable is active in Windows MMDevice API."""
    try:
        if getattr(sys, 'frozen', False):
            tool_exe = os.path.join(getattr(sys, '_MEIPASS', ''), "drivers", "vbcable", "AudioRestore.exe")
            if not os.path.exists(tool_exe):
                tool_exe = os.path.join(os.path.dirname(sys.executable), "drivers", "vbcable", "AudioRestore.exe")
        else:
            tool_exe = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "drivers", "vbcable", "AudioRestore.exe"))

        if os.path.exists(tool_exe):
            res = subprocess.run([tool_exe, "--check-vbcable"], creationflags=0x08000000, timeout=3)
            return res.returncode == 0
        return False
    except Exception:
        return False


VIRTUAL_INPUT_BLACKLIST = [
    "cable output",
    "cable input",
    "vb-audio",
    "vb-cable",
    "virtual audio",
    "virtual cable",
    "voicemeeter",
    "rtx voice",
    "nvidia broadcast",
    "krisp",
    "elgato wave link",
    "soundflower",
]


def is_virtual_input_device(name: str) -> bool:
    """Detects whether an input device is a virtual cable bridge rather than a physical microphone."""
    if not name:
        return False
    name_lower = name.lower()
    if any(keyword in name_lower for keyword in VIRTUAL_INPUT_BLACKLIST):
        return True
    try:
        from src.router import native_get_device_bus_type_cached, DeviceBusType
        bus = native_get_device_bus_type_cached(name, is_input=True)
        if bus == DeviceBusType.VIRTUAL:
            return True
    except Exception:
        pass
    return False


def get_input_devices(include_virtual: bool = False) -> List[Dict]:
    """Return all recording/microphone devices (WASAPI preferred), deduplicated across host APIs."""
    _start_enumeration_pass()
    try:
        from src.host_api_resolver import list_selectable_inputs
        selectable = list_selectable_inputs(sd.query_devices(), sd.query_hostapis(), include_virtual=include_virtual)
        if selectable:
            return [
                {
                    'index': item.index,
                    'name': item.name,
                    'hostapi': item.host_name,
                    'role': item.role,
                    'inputs': getattr(item, 'channels', 2),
                }
                for item in selectable
            ]
    except Exception:
        pass

    devices = get_all_devices()
    inputs = [d for d in devices if d['inputs'] > 0]
    if not include_virtual:
        physical_inputs = [d for d in inputs if not is_virtual_input_device(d['name'])]
        if physical_inputs:
            inputs = physical_inputs
    wasapi_inputs = [d for d in inputs if 'WASAPI' in d['hostapi']]
    return wasapi_inputs if wasapi_inputs else inputs



def get_output_devices() -> List[Dict]:
    """Return all playback/speaker devices (WASAPI preferred)."""
    _start_enumeration_pass()
    devices = get_all_devices()
    outputs = [d for d in devices if d['outputs'] > 0]
    wasapi_outputs = [d for d in outputs if 'WASAPI' in d['hostapi']]
    return wasapi_outputs if wasapi_outputs else outputs


def check_vbcable_status(force_rescan: bool = False) -> Tuple[bool, Optional[Dict], Optional[Dict]]:
    """
    Checks if VB-Audio Virtual Cable is installed and available.
    Returns: (is_installed, cable_input_playback, cable_output_recording)
    Note:
      - 'CABLE Input' is a PLAYBACK device where Getsu sends denoised audio.
      - 'CABLE Output' is a RECORDING device where Steam/Zoom listens.
    """
    devices = get_all_devices(force_rescan=force_rescan)
    cable_input = None
    cable_output = None

    for d in devices:
        name_lower = d['name'].lower()
        # Playback endpoint: CABLE Input
        if d['outputs'] > 0:
            if 'cable input' in name_lower or 'vb-cable' in name_lower or ('vb-audio' in name_lower and any(k in name_lower for k in ['cable', 'point', 'input'])):
                if not cable_input or 'WASAPI' in d['hostapi']:
                    cable_input = d
        # Recording endpoint: CABLE Output
        if d['inputs'] > 0:
            if 'cable output' in name_lower or 'vb-cable' in name_lower or ('vb-audio' in name_lower and any(k in name_lower for k in ['cable', 'point', 'output'])):
                if not cable_output or 'WASAPI' in d['hostapi']:
                    cable_output = d

    # Both endpoints found in PortAudio: fully confirmed
    if cable_input is not None and cable_output is not None:
        return True, cable_input, cable_output

    # Check Windows MMDevice directly if PortAudio missed either endpoint
    has_win_cable = False
    try:
        from src.router import native_get_cable_capture_guid
        if native_get_cable_capture_guid() is not None:
            has_win_cable = True
    except Exception:
        pass

    if not has_win_cable:
        has_win_cable = check_vbcable_via_audiorestore()

    # If Windows CoreAudio sees VB-Cable but PortAudio hasn't rescanned yet, re-query PortAudio
    if has_win_cable and not force_rescan:
        return check_vbcable_status(force_rescan=True)

    # If PortAudio sees cable_input (where Getsu sends audio) and Windows has the driver:
    if cable_input is not None and has_win_cable:
        fallback_output = cable_output or {'index': -1, 'name': 'CABLE Output (Windows Audio)'}
        return True, cable_input, fallback_output

    is_installed = (cable_input is not None) and (cable_output is not None)
    return is_installed, cable_input, cable_output


def find_matching_cable_input(input_device_id: Optional[int]) -> Optional[Dict]:
    """
    Finds the CABLE Input playback device matching the EXACT Host API of the given input device.
    PortAudio requires input and output in a duplex stream to share the same Host API (e.g. WASAPI <-> WASAPI).
    """
    if input_device_id is None:
        return None
    try:
        all_devs = get_all_devices()
        in_dev = next((d for d in all_devs if d['index'] == input_device_id), None)
        target_hostapi = in_dev['hostapi'] if in_dev else None

        # 1. First priority: CABLE Input with exact matching Host API
        if target_hostapi:
            for d in all_devs:
                if d['outputs'] > 0 and d['hostapi'] == target_hostapi:
                    name_lower = d['name'].lower()
                    if 'cable input' in name_lower or 'vb-cable' in name_lower or ('vb-audio' in name_lower and any(k in name_lower for k in ['cable', 'point', 'input'])):
                        return d

        # 2. Second priority: WASAPI CABLE Input
        for d in all_devs:
            if d['outputs'] > 0 and 'WASAPI' in d['hostapi']:
                name_lower = d['name'].lower()
                if 'cable input' in name_lower or 'vb-cable' in name_lower or ('vb-audio' in name_lower and any(k in name_lower for k in ['cable', 'point', 'input'])):
                    return d

        # 3. Third priority: Any non-WDM-KS CABLE Input
        for d in all_devs:
            if d['outputs'] > 0:
                name_lower = d['name'].lower()
                if 'cable input' in name_lower or 'vb-cable' in name_lower or ('vb-audio' in name_lower and any(k in name_lower for k in ['cable', 'point', 'input'])):
                    return d
    except Exception:
        pass
    return None


HEADSET_KEYWORDS = [
    'mpow', 'headset', 'usb audio', 'wireless', 'corsair', 'hyperx',
    'steelseries', 'razer', 'logitech', 'yeti', 'fifine', 'elgato',
    'rode', 'jbl', 'sennheiser', 'epos', 'audio-technica', 'shure',
    'plantronics', 'jabra', 'anker', 'soundcore', 'focusrite', 'behringer'
]

OUTPUT_HEADPHONE_KEYWORDS = [
    'headset', 'headphones', 'mpow', 'speakers', 'razer', 'logitech',
    'corsair', 'hyperx', 'steelseries', 'jbl', 'sennheiser', 'epos',
    'audio-technica', 'realtek', 'soundcore', 'anker'
]


def is_laptop_microphone(device_name: str, device_id: Optional[Any] = None) -> bool:
    """
    Detects whether a microphone is an integrated motherboard/laptop chipset (Realtek/Intel HD).
    Uses hardware bus enumerator (HDAUDIO/INTELAUDIO) first, with fallback to localized name strings.
    """
    if not device_name:
        return False
    try:
        from src.router import native_get_device_bus_type, DeviceBusType
        bus = native_get_device_bus_type(str(device_id) if device_id is not None else device_name, is_input=True)
        if bus in (DeviceBusType.INTERNAL_HD, DeviceBusType.INTEL_AUDIO):
            return True
        if bus in (DeviceBusType.BLUETOOTH_HFP, DeviceBusType.BLUETOOTH_LE, DeviceBusType.USB):
            return False
    except Exception:
        pass
    name_lower = device_name.lower()
    return any(k in name_lower for k in ["realtek", "array", "built-in", "internal"])


def validate_device_index(device_id: Optional[int], is_input: bool = True) -> Optional[int]:
    """
    Validates that a device index currently exists and has the requested capability.
    Returns the validated index if valid, or None if invalid/disconnected.
    """
    if device_id is None:
        return None
    try:
        devices = get_all_devices()
        for d in devices:
            if d['index'] == device_id:
                if is_input and d['inputs'] > 0:
                    return device_id
                elif not is_input and d['outputs'] > 0:
                    return device_id
        return None
    except Exception:
        return None


def auto_select_input_device() -> Dict:
    """
    Select best input microphone:
    1. External / USB Headset / Studio Mic (MPOW, Razer, Logitech, Yeti, etc.)
    2. Windows default WASAPI recording endpoint
    3. Laptop built-in Realtek / Array mic
    4. General system fallback
    """
    inputs = get_input_devices()
    if not inputs:
        all_in = [d for d in get_all_devices() if d['inputs'] > 0]
        return all_in[0] if all_in else {'index': 0, 'name': 'Default Mic'}

    # 1. Look for USB / Wireless headset or external studio mic
    for d in inputs:
        name_lower = d['name'].lower()
        if any(k in name_lower for k in HEADSET_KEYWORDS):
            return d

    # 2. Windows default input from WASAPI host API
    try:
        for api in sd.query_hostapis():
            if 'WASAPI' in api['name'] and api['default_input_device'] >= 0:
                wasapi_def = api['default_input_device']
                for d in inputs:
                    if d['index'] == wasapi_def:
                        return d
                try:
                    def_name = sd.query_devices(wasapi_def)['name']
                    for d in inputs:
                        if d['name'] == def_name:
                            return d
                except Exception:
                    pass
    except Exception:
        pass

    # 3. PortAudio general default
    default_in_idx = sd.default.device[0]
    for d in inputs:
        if d['index'] == default_in_idx:
            return d

    # 4. Look for built-in laptop mic
    for d in inputs:
        name_lower = d['name'].lower()
        if any(k in name_lower for k in ['realtek', 'array', 'built-in', 'internal']):
            return d

    return inputs[0]


def auto_select_output_device() -> Tuple[Dict, str]:
    """
    Select best output device:
    - If VB-Cable is installed: Select 'CABLE Input' (Bridge Mode).
    - If VB-Cable NOT installed: Select active physical headphones/speakers (Monitor/Pass-through Mode).
    Returns (device_dict, mode_str).
    """
    is_vbcable, cable_in, _ = check_vbcable_status()
    if is_vbcable and cable_in:
        return cable_in, "VB-Cable Bridge (Ready for Steam/Zoom)"

    # Fallback: Headphone/Speaker monitor mode
    phys = get_physical_output_device()
    if phys:
        return phys, "Headphone Monitor Pass-Through (Local Test)"

    outputs = get_output_devices()
    return outputs[0] if outputs else {'index': 0, 'name': 'Default Output'}, "Monitor Mode"


def get_physical_output_device(force_rescan: bool = False) -> Optional[Dict]:
    """
    Returns the user's active physical playback device (headphones/speakers),
    strictly excluding virtual cables (CABLE Input, VoiceMeeter, etc.).
    Priority 1: Native Windows active default playback endpoint from CoreAudio.
    Priority 2: Hardware bus priority (Bluetooth Stereo BTHENUM or USB over HDAUDIO).
    Priority 3: Keyword preference for headphones/headsets over onboard speakers.
    Priority 4: Fallback to first physical output.
    """
    if force_rescan:
        try:
            sd._terminate()
            sd._initialize()
        except Exception:
            pass

    outputs = get_output_devices()
    physical_outputs = [d for d in outputs if not is_virtual_input_device(d['name'])]
    if not physical_outputs:
        return None

    # Priority 1: Match active Windows default playback endpoint from CoreAudio
    try:
        from src.router import native_get_current_default_playback
        guid, win_name = native_get_current_default_playback()
        if win_name and win_name != "Unknown" and not is_virtual_input_device(win_name):
            win_name_lower = win_name.lower().strip()
            for d in physical_outputs:
                d_name = d['name'].lower().strip()
                if win_name_lower in d_name or d_name in win_name_lower:
                    return d
    except Exception:
        pass

    # Priority 2: Hardware bus priority (BTHENUM or USB headset over HDAUDIO)
    try:
        from src.router import native_get_device_bus_type_cached, DeviceBusType
        for d in physical_outputs:
            bus = native_get_device_bus_type_cached(d['name'], is_input=False)
            if bus in (DeviceBusType.BLUETOOTH_A2DP, DeviceBusType.BLUETOOTH_LE, DeviceBusType.USB):
                return d
    except Exception:
        pass

    # Priority 3: Fallback keyword preference for headphones over onboard speakers
    headphone_keywords = [
        "headset", "headphones", "bluetooth", "wireless", "buds", "freebuds",
        "airpods", "wh-", "wf-", "mpow", "usb", "hyperx", "razer", "logitech",
        "corsair", "steelseries", "jbl", "sennheiser", "soundcore"
    ]
    for d in physical_outputs:
        name_lower = d['name'].lower()
        if any(k in name_lower for k in headphone_keywords):
            return d

    # Priority 4: First physical output (e.g. onboard Realtek speakers)
    return physical_outputs[0]



def install_vbcable_driver() -> bool:
    """
    Triggers silent installation of bundled VB-Audio Virtual Cable with UAC elevation,
    automatically preserving the user's physical speakers/headphones as default playback.
    """
    if getattr(sys, 'frozen', False):
        base_driver_dir = os.path.join(getattr(sys, '_MEIPASS', ''), "drivers", "vbcable")
        if not os.path.exists(base_driver_dir):
            base_driver_dir = os.path.join(os.path.dirname(sys.executable), "drivers", "vbcable")
    else:
        base_driver_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "drivers", "vbcable"))

    setup_exe = os.path.join(base_driver_dir, "VBCABLE_Setup_x64.exe")
    restore_exe = os.path.join(base_driver_dir, "AudioRestore.exe")

    if not os.path.exists(setup_exe):
        print(f"[ERROR] Driver setup not found at: {setup_exe}")
        return False

    backup_file = os.path.join(os.environ.get("TEMP", "C:\\Windows\\Temp"), "getsu_audio_backup.txt")

    # 1. Pre-backup physical playback endpoint
    if os.path.exists(restore_exe):
        try:
            subprocess.run([restore_exe, "--backup", backup_file], creationflags=0x08000000, timeout=5)
        except Exception:
            pass

    print(f"[INSTALL] Requesting UAC elevation to install VB-Audio Virtual Cable...")
    try:
        if sys.platform == 'win32':
            import ctypes
            from ctypes import wintypes

            class SHELLEXECUTEINFOW(ctypes.Structure):
                _fields_ = [
                    ('cbSize', wintypes.DWORD),
                    ('fMask', wintypes.ULONG),
                    ('hwnd', wintypes.HWND),
                    ('lpVerb', wintypes.LPCWSTR),
                    ('lpFile', wintypes.LPCWSTR),
                    ('lpParameters', wintypes.LPCWSTR),
                    ('lpDirectory', wintypes.LPCWSTR),
                    ('nShow', ctypes.c_int),
                    ('hInstApp', wintypes.HINSTANCE),
                    ('lpIDList', wintypes.LPVOID),
                    ('lpClass', wintypes.LPCWSTR),
                    ('hkeyClass', wintypes.HKEY),
                    ('dwHotKey', wintypes.DWORD),
                    ('hIconOrMonitor', wintypes.HANDLE),
                    ('hProcess', wintypes.HANDLE)
                ]

            SEE_MASK_NOCLOSEPROCESS = 0x00000040
            sei = SHELLEXECUTEINFOW()
            sei.cbSize = ctypes.sizeof(SHELLEXECUTEINFOW)
            sei.fMask = SEE_MASK_NOCLOSEPROCESS
            sei.hwnd = None
            sei.lpVerb = "runas"
            sei.lpFile = setup_exe
            sei.lpParameters = "-i -h"
            sei.lpDirectory = os.path.dirname(setup_exe)
            sei.nShow = 1  # SW_SHOWNORMAL

            success = ctypes.windll.shell32.ShellExecuteExW(ctypes.byref(sei))
            if not success:
                print("[ERROR] ShellExecuteExW failed or UAC was declined.")
                return False

            if sei.hProcess:
                try:
                    # Wait up to 120 seconds for driver installer to finish
                    ctypes.windll.kernel32.WaitForSingleObject(sei.hProcess, 120000)
                finally:
                    ctypes.windll.kernel32.CloseHandle(sei.hProcess)
        else:
            return False

        # Allow Windows AudioEndpointBuilder 1.5 seconds to register audio nodes
        time.sleep(1.5)

        # 2. Post-restore physical playback and capture endpoints
        if os.path.exists(restore_exe):
            try:
                subprocess.run([restore_exe, "--restore", backup_file], creationflags=0x08000000, timeout=5)
                subprocess.run([restore_exe, "--ensure-physical"], creationflags=0x08000000, timeout=5)
                subprocess.run([restore_exe, "--ensure-physical-capture"], creationflags=0x08000000, timeout=5)
            except Exception:
                pass
        return True
    except Exception as e:
        print(f"[ERROR] Failed to launch installer: {e}")
        return False


def print_device_report():
    """Prints a clear startup diagnostic report of audio devices."""
    print("=" * 70)
    print("                 GETSU AUDIO DEVICE DIAGNOSTICS")
    print("=" * 70)

    # VB-Cable status
    is_vbcable, cable_in, cable_out = check_vbcable_status()
    if is_vbcable:
        print(" [STATUS] VB-Audio Virtual Cable : INSTALLED & READY")
        print(f"          - Playback Endpoint   : [{cable_in['index']}] {cable_in['name']}")
        print(f"          - Recording Endpoint  : [{cable_out['index']}] {cable_out['name']}")
    else:
        print(" [STATUS] VB-Audio Virtual Cable : NOT DETECTED")
        print("          Notice: To pipe denoised audio directly into Steam or Zoom,")
        print("          VB-Cable is required. A bundled installer is included in:")
        print("          drivers/vbcable/VBCABLE_Setup_x64.exe")

    print("-" * 70)
    # Selected Input
    selected_in = auto_select_input_device()
    if selected_in:
        print(f" [INPUT]  Selected Microphone   : [{selected_in['index']}] {selected_in['name']}")
        print(f"          Host API              : {selected_in.get('hostapi', 'Unknown')}")
    else:
        print(" [INPUT]  No microphone detected.")

    # Selected Output
    selected_out, mode = auto_select_output_device()
    if selected_out:
        print(f" [OUTPUT] Selected Destination  : [{selected_out['index']}] {selected_out['name']}")
        print(f"          Operating Mode        : {mode}")
    else:
        print(" [OUTPUT] No audio output device detected.")
    print("=" * 70)
    return is_vbcable, selected_in, selected_out


if __name__ == "__main__":
    print_device_report()
