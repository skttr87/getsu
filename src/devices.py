"""
Audio Device Enumeration and VB-Audio Virtual Cable Management for Getsu.
"""
import os
import sys
import subprocess
from typing import List, Dict, Optional, Tuple
import sounddevice as sd


def get_all_devices() -> List[Dict]:
    """Retrieve all sound devices with HostAPI details."""
    try:
        devices = sd.query_devices()
        host_apis = sd.query_hostapis()
        result = []
        for i, d in enumerate(devices):
            host_name = host_apis[d['hostapi']]['name']
            result.append({
                'index': i,
                'name': d['name'],
                'hostapi': host_name,
                'inputs': d['max_input_channels'],
                'outputs': d['max_output_channels'],
                'samplerate': d['default_samplerate'],
            })
        return result
    except Exception as e:
        print(f"[ERROR] Failed to query audio devices: {e}")
        return []


VIRTUAL_INPUT_BLACKLIST = [
    "cable output",
    "cable input",
    "vb-audio",
    "virtual audio",
    "virtual cable",
    "voicemeeter",
]


def is_virtual_input_device(name: str) -> bool:
    """Detects whether an input device is a virtual cable bridge rather than a physical microphone."""
    name_lower = name.lower()
    return any(keyword in name_lower for keyword in VIRTUAL_INPUT_BLACKLIST)


def get_input_devices(include_virtual: bool = False) -> List[Dict]:
    """Return all recording/microphone devices (WASAPI preferred), excluding virtual cables by default."""
    devices = get_all_devices()
    # Filter for devices with input channels > 0
    inputs = [d for d in devices if d['inputs'] > 0]

    # Hide virtual cables (e.g. CABLE Output) from input dropdown so users are not confused
    if not include_virtual:
        physical_inputs = [d for d in inputs if not is_virtual_input_device(d['name'])]
        if physical_inputs:
            inputs = physical_inputs

    # Prioritize WASAPI devices
    wasapi_inputs = [d for d in inputs if 'WASAPI' in d['hostapi']]
    return wasapi_inputs if wasapi_inputs else inputs


def get_output_devices() -> List[Dict]:
    """Return all playback/speaker devices (WASAPI preferred)."""
    devices = get_all_devices()
    outputs = [d for d in devices if d['outputs'] > 0]
    wasapi_outputs = [d for d in outputs if 'WASAPI' in d['hostapi']]
    return wasapi_outputs if wasapi_outputs else outputs


def check_vbcable_status() -> Tuple[bool, Optional[Dict], Optional[Dict]]:
    """
    Checks if VB-Audio Virtual Cable is installed and available.
    Returns: (is_installed, cable_input_playback, cable_output_recording)
    Note:
      - 'CABLE Input' is a PLAYBACK device where Getsu sends denoised audio.
      - 'CABLE Output' is a RECORDING device where Steam/Zoom listens.
    """
    devices = get_all_devices()
    cable_input = None
    cable_output = None

    for d in devices:
        name = d['name']
        # Playback endpoint: CABLE Input
        if 'cable input' in name.lower() and d['outputs'] > 0:
            if not cable_input or 'WASAPI' in d['hostapi']:
                cable_input = d
        # Recording endpoint: CABLE Output
        if 'cable output' in name.lower() and d['inputs'] > 0:
            if not cable_output or 'WASAPI' in d['hostapi']:
                cable_output = d

    is_installed = (cable_input is not None) and (cable_output is not None)
    return is_installed, cable_input, cable_output


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
    - If VB-Cable NOT installed: Select default headphones/speakers (Monitor/Pass-through Mode).
    Returns (device_dict, mode_str).
    """
    is_vbcable, cable_in, _ = check_vbcable_status()
    if is_vbcable and cable_in:
        return cable_in, "VB-Cable Bridge (Ready for Steam/Zoom)"

    # Fallback: Headphone/Speaker monitor mode
    outputs = get_output_devices()
    default_out_idx = sd.default.device[1]
    
    # Check default output
    for d in outputs:
        if d['index'] == default_out_idx:
            return d, "Headphone Monitor Pass-Through (Local Test)"
            
    # Check for headphones / speakers
    for d in outputs:
        name_lower = d['name'].lower()
        if any(k in name_lower for k in OUTPUT_HEADPHONE_KEYWORDS):
            return d, "Headphone Monitor Pass-Through (Local Test)"

    return outputs[0] if outputs else {'index': 0, 'name': 'Default Output'}, "Monitor Mode"


def install_vbcable_driver() -> bool:
    """
    Triggers silent installation of bundled VB-Audio Virtual Cable with UAC elevation.
    """
    if getattr(sys, 'frozen', False):
        setup_exe = os.path.join(getattr(sys, '_MEIPASS', ''), "drivers", "vbcable", "VBCABLE_Setup_x64.exe")
        if not os.path.exists(setup_exe):
            setup_exe = os.path.join(os.path.dirname(sys.executable), "drivers", "vbcable", "VBCABLE_Setup_x64.exe")
    else:
        setup_exe = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "drivers", "vbcable", "VBCABLE_Setup_x64.exe"))
    if not os.path.exists(setup_exe):
        print(f"[ERROR] Driver setup not found at: {setup_exe}")
        return False

    print(f"[INSTALL] Requesting UAC elevation to install VB-Audio Virtual Cable...")
    # Run via PowerShell with RunAs verb (escape single quotes for usernames with apostrophes)
    safe_setup_exe = setup_exe.replace("'", "''")
    cmd = [
        "powershell",
        "-Command",
        f"Start-Process -FilePath '{safe_setup_exe}' -ArgumentList '-i -h' -Verb RunAs -Wait"
    ]
    try:
        res = subprocess.run(cmd, capture_output=True, text=True)
        return res.returncode == 0
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
    print(f" [INPUT]  Selected Microphone   : [{selected_in['index']}] {selected_in['name']}")
    print(f"          Host API              : {selected_in.get('hostapi', 'Unknown')}")

    # Selected Output
    selected_out, mode = auto_select_output_device()
    print(f" [OUTPUT] Selected Destination  : [{selected_out['index']}] {selected_out['name']}")
    print(f"          Operating Mode        : {mode}")
    print("=" * 70)
    return is_vbcable, selected_in, selected_out


if __name__ == "__main__":
    print_device_report()
