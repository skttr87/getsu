"""
Shared diagnostics log and read-only device reports for Getsu.

- One rotating log file, %APPDATA%\\Getsu\\getsu.log, shared by every logger in the app
  (router, engine, voice test). Console output is kept for development runs.
- Start-failure reports: PortAudio error code, the chosen devices and a device dump.
- USB power reports for USB microphones: selective suspend from the active power plan and the
  Windows power-management flags of the device and its parent hub. Everything is read from
  Windows; nothing is sent to the device. These calls run only on start failure or on request,
  never in the audio callback.
"""
import base64
import datetime
import logging
import os
import re
import subprocess
import threading
from logging.handlers import RotatingFileHandler
from typing import Any, Iterable, List, Optional

LOG_FILE_NAME = "getsu.log"
LOGGER_NAME = "Getsu"
LOG_MAX_BYTES = 2 * 1024 * 1024
LOG_BACKUP_COUNT = 2
LOG_FORMAT = "[%(asctime)s] [%(levelname)s] [%(name)s] %(message)s"

# powercfg subgroup and setting GUIDs for USB selective suspend.
USB_SUBGROUP_GUID = "2a737441-1930-4402-8d77-b2bebba308a3"
USB_SELECTIVE_SUSPEND_GUID = "48e6b7a6-50f5-4782-a5d4-53bb8f07e226"

_CREATE_NO_WINDOW = 0x08000000
_handler_lock = threading.Lock()
_file_handler: Optional[logging.Handler] = None


def app_data_dir() -> str:
    return os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")), "Getsu")


# Known folder id of the Desktop (FOLDERID_Desktop).
_FOLDERID_DESKTOP = "B4BFCC3A-DB2C-424C-B029-7FE99A87C641"


def desktop_dir() -> str:
    """
    The user's Desktop folder as Windows reports it (follows a Desktop moved or redirected to OneDrive).
    Falls back to %USERPROFILE%\\Desktop when the shell cannot tell.
    """
    fallback = os.path.join(os.path.expanduser("~"), "Desktop")
    if os.name != "nt":
        return fallback
    try:
        import ctypes
        import uuid

        class _Guid(ctypes.Structure):
            _fields_ = [("Data1", ctypes.c_ulong), ("Data2", ctypes.c_ushort), ("Data3", ctypes.c_ushort),
                        ("Data4", ctypes.c_ubyte * 8)]

        guid = _Guid.from_buffer_copy(uuid.UUID(_FOLDERID_DESKTOP).bytes_le)
        raw = ctypes.c_wchar_p()
        hr = ctypes.windll.shell32.SHGetKnownFolderPath(ctypes.byref(guid), 0, None, ctypes.byref(raw))
        if hr == 0 and raw.value:
            return raw.value
    except Exception:
        pass
    return fallback


def log_path() -> str:
    return os.path.join(app_data_dir(), LOG_FILE_NAME)


def get_file_handler() -> Optional[logging.Handler]:
    """The single rotating file handler for getsu.log. Two handlers on one file break rotation on Windows."""
    global _file_handler
    with _handler_lock:
        if _file_handler is None:
            try:
                os.makedirs(app_data_dir(), exist_ok=True)
                fh = RotatingFileHandler(
                    log_path(), maxBytes=LOG_MAX_BYTES, backupCount=LOG_BACKUP_COUNT, encoding="utf-8"
                )
                fh.setFormatter(logging.Formatter(LOG_FORMAT))
                _file_handler = fh
            except Exception:
                return None
        return _file_handler


def get_logger(name: str = LOGGER_NAME) -> logging.Logger:
    """Logger that writes to getsu.log. Safe to call many times."""
    logger = logging.getLogger(name)
    logger.setLevel(logging.INFO)
    fh = get_file_handler()
    if fh is not None and fh not in logger.handlers:
        logger.addHandler(fh)
    return logger


def info(message: str) -> None:
    """Console line (development runs) plus a getsu.log line. Never raises."""
    _emit(message, logging.INFO)


def warning(message: str) -> None:
    _emit(message, logging.WARNING)


def error(message: str) -> None:
    _emit(message, logging.ERROR)


def _emit(message: str, level: int) -> None:
    try:
        print(message, flush=True)
    except Exception:
        # Windowed build: sys.stdout is None or closed.
        pass
    try:
        get_logger().log(level, message)
    except Exception:
        pass


# ----------------------------------------------------------------------------- device dump

def device_dump() -> str:
    """Full sounddevice device and host API listing, as text. Never raises."""
    try:
        import sounddevice as sd
    except Exception as e:
        return f"sounddevice unavailable: {e!r}"
    lines: List[str] = []
    try:
        lines.append("sd.query_devices():")
        lines.append(str(sd.query_devices()))
    except Exception as e:
        lines.append(f"sd.query_devices() failed: {e!r}")
    try:
        lines.append("sd.query_hostapis():")
        for i, api in enumerate(sd.query_hostapis()):
            lines.append(f"  [{i}] {api.get('name')} devices={list(api.get('devices', []))} "
                         f"default_in={api.get('default_input_device')} default_out={api.get('default_output_device')}")
    except Exception as e:
        lines.append(f"sd.query_hostapis() failed: {e!r}")
    try:
        lines.append(f"sd.default.device = {tuple(sd.default.device)}")
    except Exception:
        pass
    return "\n".join(lines)


def describe_device(index: Optional[int], is_input: bool = True) -> str:
    """One line for a device index: index, name, host API, default rate and max channels."""
    if index is None:
        return "none"
    try:
        import sounddevice as sd
        dev = sd.query_devices(index)
        apis = sd.query_hostapis()
        api_name = apis[dev["hostapi"]]["name"]
        ch = dev["max_input_channels"] if is_input else dev["max_output_channels"]
        return (f"[{index}] '{dev['name']}' host_api='{api_name}' "
                f"default_samplerate={dev['default_samplerate']} max_{'in' if is_input else 'out'}_channels={ch}")
    except Exception as e:
        return f"[{index}] (query failed: {e!r})"


# ----------------------------------------------------------------------------- USB power (read-only)

_CURRENT_LINE_RE = re.compile(r"Current\s+(AC|DC)\s+Power\s+Setting\s+Index:\s*0x([0-9a-fA-F]+)")


def parse_selective_suspend(powercfg_text: str) -> dict:
    """Extracts AC/DC values of USB selective suspend from `powercfg /query` output. 0 = disabled, 1 = enabled."""
    result = {"AC": None, "DC": None}
    for m in _CURRENT_LINE_RE.finditer(powercfg_text or ""):
        result[m.group(1)] = int(m.group(2), 16)
    return result


def _run(args: List[str], timeout: float = 20.0) -> subprocess.CompletedProcess:
    return subprocess.run(
        args, capture_output=True, text=True, timeout=timeout,
        creationflags=_CREATE_NO_WINDOW, encoding="utf-8", errors="replace",
    )


def selective_suspend_lines() -> List[str]:
    try:
        proc = _run(["powercfg", "/query", "SCHEME_CURRENT", USB_SUBGROUP_GUID, USB_SELECTIVE_SUSPEND_GUID])
    except Exception as e:
        return [f"USB selective suspend: powercfg failed: {e!r}"]
    if proc.returncode != 0:
        return [f"USB selective suspend: powercfg exit {proc.returncode}: {(proc.stderr or proc.stdout).strip()[:200]}"]
    values = parse_selective_suspend(proc.stdout)
    out = []
    for plug in ("AC", "DC"):
        v = values.get(plug)
        if v is None:
            out.append(f"USB selective suspend {plug}: not reported")
        else:
            state = "enabled: USB audio can be suspended" if v else "disabled"
            out.append(f"USB selective suspend {plug}: {v} ({state})")
    return out


_PS_TEMPLATE = r"""
$ErrorActionPreference = 'SilentlyContinue'
$inner = '__NAME__'
$found = @()
$devs = Get-PnpDevice -PresentOnly | Where-Object {
    $_.InstanceId -like 'USB\*' -and $_.FriendlyName -and
    ($_.FriendlyName.IndexOf($inner, [StringComparison]::OrdinalIgnoreCase) -ge 0)
}
$power = Get-CimInstance -Namespace root/wmi -ClassName MSPower_DeviceEnable
foreach ($d in $devs) {
    $chain = @()
    $id = $d.InstanceId
    for ($level = 0; $level -lt 3 -and $id; $level++) {
        $p = Get-PnpDeviceProperty -InstanceId $id -KeyName 'DEVPKEY_Device_Parent'
        $name = (Get-PnpDevice -InstanceId $id).FriendlyName
        $pw = @($power | Where-Object { $_.InstanceName -and $_.InstanceName.StartsWith($id, [StringComparison]::OrdinalIgnoreCase) } | ForEach-Object { [string]$_.Enable })
        $chain += [pscustomobject]@{ Level = $level; InstanceId = $id; FriendlyName = $name; MSPowerEnable = $pw }
        $id = if ($p) { [string]$p.Data } else { $null }
    }
    $found += [pscustomobject]@{ Device = $d.FriendlyName; Chain = $chain }
}
if ($found.Count -eq 0) { '[]' } else { $found | ConvertTo-Json -Depth 5 -Compress }
"""


def _usb_power_script(device_name: str) -> str:
    # Name is embedded in a single-quoted PowerShell string; double the quotes.
    return _PS_TEMPLATE.replace("__NAME__", device_name.replace("'", "''"))


def usb_power_lines(device_name: str) -> List[str]:
    """Selective suspend plus MSPower_DeviceEnable for the USB device and its parents (read-only)."""
    lines = selective_suspend_lines()
    inner = _inner_name(device_name)
    if not inner:
        lines.append("USB device: name has no usable text, skipped")
        return lines
    encoded = base64.b64encode(_usb_power_script(inner).encode("utf-16-le")).decode("ascii")
    try:
        proc = _run(["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                     "-EncodedCommand", encoded], timeout=30.0)
    except Exception as e:
        lines.append(f"USB device power query failed: {e!r}")
        return lines
    text = (proc.stdout or "").strip()
    if proc.returncode != 0 or not text:
        lines.append(f"USB device power query returned no data (exit {proc.returncode})")
        return lines
    import json
    try:
        data = json.loads(text)
    except Exception:
        lines.append(f"USB device power query output not parsed: {text[:200]}")
        return lines
    if isinstance(data, dict):
        data = [data]
    if not data:
        lines.append(f"USB device for '{device_name}': no matching USB device (not a USB-attached endpoint, or name differs)")
        return lines
    for dev in data:
        lines.append(f"USB device match: {dev.get('Device')}")
        chain = dev.get("Chain") or []
        if isinstance(chain, dict):
            chain = [chain]
        for node in chain:
            lines.append(f"  level {node.get('Level')}: {node.get('FriendlyName')} | {node.get('InstanceId')} | "
                         f"MSPower_DeviceEnable={node.get('MSPowerEnable')}")
    return lines


def _inner_name(device_name: str) -> str:
    """'Microphone (Jabra EVOLVE 20 MS)' -> 'Jabra EVOLVE 20 MS'. Falls back to the whole name."""
    m = re.search(r"\(([^()]+)\)\s*$", device_name or "")
    text = (m.group(1) if m else (device_name or "")).strip()
    return text


def is_usb_microphone(device_name: str) -> bool:
    """True when Windows classifies the endpoint as a USB microphone or headset (bus data, no name matching)."""
    try:
        from src.devices import classify_device
        return classify_device(device_name, True) in ("usb_mic", "usb_headset")
    except Exception:
        return False


# ----------------------------------------------------------------------------- reports

def log_start_failure(exc: BaseException, explained: Any, input_idx: Optional[int], output_idx: Optional[int]) -> None:
    """Logs everything needed to diagnose a failed stream start. Never raises."""
    try:
        code = getattr(explained, "code", None)
        error(f"[STREAM] Start failed: {getattr(explained, 'title', '')} | technical: "
              f"{getattr(explained, 'technical', repr(exc))}")
        error(f"[STREAM]   PaErrorCode: {code if code is not None else 'none'} | exception: {type(exc).__name__}")
        error(f"[STREAM]   input : {describe_device(input_idx, True)}")
        error(f"[STREAM]   output: {describe_device(output_idx, False)}")
        in_name = _device_name(input_idx, True)
        if in_name and is_usb_microphone(in_name):
            for line in usb_power_lines(in_name):
                info(f"[STREAM]   {line}")
        info(f"[STREAM] Device dump at failure:\n{device_dump()}")
    except Exception:
        pass


def _device_name(index: Optional[int], is_input: bool) -> Optional[str]:
    if index is None:
        return None
    try:
        import sounddevice as sd
        return sd.query_devices(index)["name"]
    except Exception:
        return None


def _read_log_text() -> str:
    """Rotated backups first, then the current file, so the text reads in time order."""
    base = log_path()
    parts = []
    for path in [f"{base}.{n}" for n in range(LOG_BACKUP_COUNT, 0, -1)] + [base]:
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                parts.append(f"--- {os.path.basename(path)} ---\n{f.read()}")
        except OSError:
            continue
    return "\n".join(parts) if parts else "(no log file yet)"


def save_report(dest_dir: str) -> str:
    """
    Writes getsu.log, a fresh device dump and the USB power settings for the default microphone to a
    timestamped .txt in dest_dir. Returns the file path. Read-only with respect to the system.
    """
    os.makedirs(dest_dir, exist_ok=True)
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    path = os.path.join(dest_dir, f"getsu-diagnostics-{stamp}.txt")

    sections: List[str] = [f"Getsu diagnostics report, created {datetime.datetime.now().isoformat(timespec='seconds')}"]
    sections.append("== Device dump ==\n" + device_dump())

    try:
        import sounddevice as sd
        in_idx = sd.default.device[0]
        in_name = _device_name(in_idx, True)
    except Exception:
        in_name = None
    power: Iterable[str]
    if in_name and is_usb_microphone(in_name):
        power = usb_power_lines(in_name)
    elif in_name:
        power = [f"Default microphone '{in_name}' is not a USB device; USB power settings not applicable."]
    else:
        power = ["No default microphone; USB power settings not read."]
    sections.append("== USB power ==\n" + "\n".join(power))
    sections.append("== getsu.log ==\n" + _read_log_text())

    with open(path, "w", encoding="utf-8") as f:
        f.write("\n\n".join(sections) + "\n")
    return path
