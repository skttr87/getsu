"""
Dark native popup menus for the tray icon (right-click menu).

pystray builds a native Win32 popup menu, which Windows draws light unless the process asks for dark app
modes. Windows 10 1903 and later export two undocumented uxtheme.dll functions by ordinal:
  135  SetPreferredAppMode(PreferredAppMode)  (ForceDark = 2)
  136  FlushMenuThemes()                      (applies the new mode to menus that already exist)
Ordinal 135 means something else on Windows 10 1809 (AllowDarkModeForApp), so the call is gated on the
build number. The calls are best effort: any failure leaves the menu light and never raises.
Sources for the ordinals and the enum values: the public dark-mode notes of the Notepad++ and darkmodelib
projects (community research, not Microsoft documentation).
"""
import ctypes
import sys
from typing import Optional

# SetPreferredAppMode exists from Windows 10 1903 (build 18362) on; below that, ordinal 135 is another function.
MIN_BUILD_PREFERRED_APP_MODE = 18362
ORDINAL_SET_PREFERRED_APP_MODE = 135
ORDINAL_FLUSH_MENU_THEMES = 136
APP_MODE_FORCE_DARK = 2


def windows_build() -> int:
    """Windows build number from RtlGetVersion (e.g. 26100), or 0 when it cannot be read."""
    if sys.platform != "win32":
        return 0
    try:
        class OSVERSIONINFOW(ctypes.Structure):
            _fields_ = [
                ("dwOSVersionInfoSize", ctypes.c_ulong),
                ("dwMajorVersion", ctypes.c_ulong),
                ("dwMinorVersion", ctypes.c_ulong),
                ("dwBuildNumber", ctypes.c_ulong),
                ("dwPlatformId", ctypes.c_ulong),
                ("szCSDVersion", ctypes.c_wchar * 128),
            ]

        info = OSVERSIONINFOW()
        info.dwOSVersionInfoSize = ctypes.sizeof(OSVERSIONINFOW)
        if ctypes.WinDLL("ntdll").RtlGetVersion(ctypes.byref(info)) != 0:
            return 0
        return int(info.dwBuildNumber)
    except Exception:
        return 0


PERSONALIZE_KEY = r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize"


def taskbar_uses_light_theme() -> bool:
    """
    True when the Windows taskbar (and so the notification area) is light: HKCU Personalize value
    SystemUsesLightTheme = 1. Missing value or any error counts as dark, the Windows default for the taskbar.
    Read-only. Windows broadcasts WM_SETTINGCHANGE "ImmersiveColorSet" when it changes.
    """
    if sys.platform != "win32":
        return False
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, PERSONALIZE_KEY) as key:
            value, _ = winreg.QueryValueEx(key, "SystemUsesLightTheme")
        return int(value) == 1
    except (OSError, ValueError, TypeError):
        return False


def supports_dark_menus(build: int) -> bool:
    """True when the running Windows build exports SetPreferredAppMode with the expected meaning."""
    return build >= MIN_BUILD_PREFERRED_APP_MODE


def _ordinal_function(dll, ordinal: int, prototype):
    """Looks up an exported function by ordinal (MAKEINTRESOURCE) and returns it as a ctypes callable, or None."""
    kernel32 = ctypes.WinDLL("kernel32")
    kernel32.GetProcAddress.restype = ctypes.c_void_p
    kernel32.GetProcAddress.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    address = kernel32.GetProcAddress(dll._handle, ctypes.c_void_p(ordinal))
    if not address:
        return None
    return prototype(address)


def enable_dark_menus(build: Optional[int] = None) -> bool:
    """
    Asks Windows for dark popup menus for this process. Returns True when the call was made.
    Call once at startup, before the tray icon is created. Never raises.
    """
    try:
        if build is None:
            build = windows_build()
        if not supports_dark_menus(build):
            return False
        uxtheme = ctypes.WinDLL("uxtheme")
        set_mode = _ordinal_function(uxtheme, ORDINAL_SET_PREFERRED_APP_MODE,
                                     ctypes.WINFUNCTYPE(ctypes.c_int, ctypes.c_int))
        if set_mode is None:
            return False
        set_mode(APP_MODE_FORCE_DARK)
        flush = _ordinal_function(uxtheme, ORDINAL_FLUSH_MENU_THEMES, ctypes.WINFUNCTYPE(None))
        if flush is not None:
            flush()
        return True
    except Exception:
        return False
