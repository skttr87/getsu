"""
Start with Windows: the per-user Run entry for Getsu and the Task Manager "Startup apps" switch.

enable() and disable() write only HKEY_CURRENT_USER, so no administrator rights are needed. They work only in
the frozen (installed) build, because the Run value must point at getsu.exe. is_enabled() reads the registry in
every build, so the tray checkmark shows the real state even when running from source.

Task Manager stores its Enabled/Disabled switch for each Run entry in
HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\Explorer\\StartupApproved\\Run as a REG_BINARY value with the
same name. The first byte is 0x02 when enabled and 0x03 when disabled; the other 11 bytes hold a FILETIME
for the last change. An odd first byte means disabled. A missing value means enabled.
Sources: windowsir.blogspot.com, "Windows Incident Response: StartupApproved\\Run, pt II" (2022-07), and
github.com/renanleonel/windows-taskbar-battery pull request 12 (odd first byte skipped at logon).
"""
import sys
import time
from typing import Callable

try:
    import winreg
except ImportError:  # not Windows
    winreg = None

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
APPROVED_RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Explorer\StartupApproved\Run"
VALUE_NAME = "Getsu"
MINIMIZED_ARG = "--minimized"


def is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def is_supported() -> bool:
    """True when the Run entry can be written: the frozen build on Windows."""
    return winreg is not None and is_frozen()


def run_command(exe_path: str = None) -> str:
    """Command line stored in the Run value. The path is quoted so spaces in the install folder are safe."""
    exe = exe_path if exe_path is not None else sys.executable
    return f'"{exe}" {MINIMIZED_ARG}'


def approval_means_disabled(data) -> bool:
    """True when a StartupApproved value says the entry is disabled in Task Manager (odd first byte)."""
    if not data:
        return False
    return (bytes(data)[0] & 1) == 1


def is_enabled() -> bool:
    """True when the Run entry exists and Task Manager has not disabled it."""
    if winreg is None:
        return False
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_READ) as key:
            winreg.QueryValueEx(key, VALUE_NAME)
    except OSError:
        return False
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, APPROVED_RUN_KEY, 0, winreg.KEY_READ) as key:
            data, _ = winreg.QueryValueEx(key, VALUE_NAME)
        if approval_means_disabled(data):
            return False
    except OSError:
        pass
    return True


def _delete_value(subkey: str):
    """Deletes the Getsu value under a HKCU subkey. A missing key or value is not an error."""
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, subkey, 0, winreg.KEY_SET_VALUE) as key:
            winreg.DeleteValue(key, VALUE_NAME)
    except FileNotFoundError:
        pass


def enable() -> bool:
    """
    Writes the Run entry and removes any Task Manager "Disabled" mark, so the entry is active at next sign-in.
    Returns False (and changes nothing) when autostart is not supported in this build.
    """
    if not is_supported():
        return False
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
        winreg.SetValueEx(key, VALUE_NAME, 0, winreg.REG_SZ, run_command())
    _delete_value(APPROVED_RUN_KEY)
    return True


def disable() -> bool:
    """Removes the Run entry and its Task Manager approval mark. Returns False when not supported."""
    if not is_supported():
        return False
    _delete_value(RUN_KEY)
    _delete_value(APPROVED_RUN_KEY)
    return True


def should_resume_filter(launched_minimized: bool, was_running_on_exit: bool) -> bool:
    """Automatic restart of noise cancellation happens only for a Start with Windows launch after a running session."""
    return bool(launched_minimized and was_running_on_exit)


def devices_ready(input_idx, output_idx, cable_installed: bool) -> bool:
    """True when an input, an output and the virtual cable are all known, so the start path can run."""
    return (cable_installed
            and input_idx is not None and input_idx >= 0
            and output_idx is not None and output_idx >= 0)


def retry_until(done: Callable[[], bool], timeout_s: float, interval_s: float,
                clock: Callable[[], float] = time.monotonic,
                sleep: Callable[[float], None] = time.sleep) -> bool:
    """Calls done() now and then every interval_s until it returns True or timeout_s has passed."""
    deadline = clock() + timeout_s
    while True:
        if done():
            return True
        if clock() >= deadline:
            return False
        sleep(interval_s)
