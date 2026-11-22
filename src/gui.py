"""
Getsu Desktop GUI Application.
Features:
- Single toggle button: "START" -> "STOP", both in the accent blue
- Minimize and Close buttons both minimize to the Windows notification area (System Tray)
- Human-friendly, clean UI with zero broken symbols or technical jargon
- No vertical scrollbars
- Auto-selects Windows default microphone on startup
- Integrated VB-Cable detection with 1-click silent install
"""
import os
import sys
import re
import math
import threading
import time
import signal
import ctypes
from ctypes import wintypes
from typing import List, Dict, Optional, Sequence

import dearpygui.dearpygui as dpg
import sounddevice as sd
from PIL import Image, ImageDraw
import pystray
from pystray import MenuItem as item, Menu
import webbrowser

import numpy as np

from src.devices import (
    get_input_devices,
    auto_select_input_device,
    check_vbcable_status,
    auto_select_output_device,
    get_physical_output_device,
    install_vbcable_driver,
    is_laptop_microphone,
    find_matching_cable_input,
)
from src.config import load_config, save_config
from src.stream import AudioEngine, create_engine_from_config
from src.router import SmartMicRouter
from src.rnnoise import RNNoise, FRAME_SIZE, SAMPLE_RATE
from src.dsp import (
    HighPassFilter,
    AdaptiveNoiseGate,
    SpeechLeveler,
    TransientSuppressor,
    process_mono_frame,
)
from src.host_api_resolver import resolve_single, selection_hint
from src.audio_errors import explain_start_error
from src.frame_pacing import frame_sleep_s


# Win32 Constants for System Tray Minimize/Close hook
user32 = ctypes.windll.user32
WNDPROC = ctypes.WINFUNCTYPE(ctypes.c_longlong, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)
GWLP_WNDPROC = -4
GWL_STYLE = -16
WM_CLOSE = 0x0010
WM_SYSCOMMAND = 0x0112
SC_MINIMIZE = 0xF020
SC_MAXIMIZE = 0xF030
SW_HIDE = 0
SW_RESTORE = 9
WS_MAXIMIZEBOX = 0x00010000
WS_THICKFRAME = 0x00040000
SWP_FRAMECHANGED = 0x0020
SWP_NOMOVE = 0x0002
SWP_NOSIZE = 0x0001
SWP_NOZORDER = 0x0004

user32.SetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_void_p]
user32.SetWindowLongPtrW.restype = ctypes.c_void_p
user32.CallWindowProcW.argtypes = [ctypes.c_void_p, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
user32.CallWindowProcW.restype = ctypes.c_longlong
user32.IsWindowVisible.argtypes = [wintypes.HWND]
user32.IsIconic.argtypes = [wintypes.HWND]


# Palette from the approved mockup (opt/mockup/a-focus.html, CSS variables). Dark theme only.
# One accent colour (Apple system blue) marks every on, selected or primary state.
# Red is reserved for errors and orange for warnings. No green, no glow.
COL_WINDOW = (28, 28, 30)           # --win
COL_CARD = (44, 44, 46)             # --card
COL_CARD_HOVER = (58, 58, 60)       # --card-hi
COL_SEPARATOR = (84, 84, 88, 140)   # --sep (55 % opacity)
COL_TEXT = (245, 245, 247)          # --text
COL_TEXT_2 = (161, 161, 166)        # --text2
COL_TEXT_3 = (110, 110, 115)        # --text3
COL_FILL = (118, 118, 128, 61)      # --fill (24 %)
COL_FILL_2 = (118, 118, 128, 92)    # --fill2 (36 %)
COL_FILL_3 = (118, 118, 128, 112)   # --fill3 (44 %)
COL_ACCENT = (10, 132, 255)         # --accent
COL_ACCENT_HOVER = (47, 150, 255)   # --accent-hi
COL_ACCENT_PRESSED = (8, 104, 210)
COL_ACCENT_SOFT = (10, 132, 255, 46)     # --accent-soft (18 %)
COL_ACCENT_SOFT_2 = (10, 132, 255, 71)   # --accent-soft2 (28 %)
COL_WARNING = (255, 159, 10)        # --orange
COL_ERROR = (255, 69, 58)           # --red
COL_WHITE = (255, 255, 255)

# Windows 11 DWM title bar. COLORREF is 0x00BBGGRR: caption #1C1C1E, text #A1A1A6, border #2C2C2E.
TITLE_CAPTION_COLOR = 0x001E1C1C
TITLE_TEXT_COLOR = 0x00A6A1A1
TITLE_BORDER_COLOR = 0x002E2C2C


def init_dpi_awareness() -> tuple[int, float]:
    """
    Enables Windows Per-Monitor V2 DPI awareness and queries active monitor DPI.
    Prevents Windows DWM from bilinearly upscaling the window and blurring fonts.
    Returns (dpi, dpi_scale).
    """
    try:
        # DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2 (-4)
        ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
    except Exception:
        try:
            # Per-Monitor awareness (Windows 8.1+)
            ctypes.windll.shcore.SetProcessDpiAwareness(2)
        except Exception:
            try:
                ctypes.windll.user32.SetProcessDPIAware()
            except Exception:
                pass

    try:
        hdc = ctypes.windll.user32.GetDC(0)
        dpi = ctypes.windll.gdi32.GetDeviceCaps(hdc, 88)  # LOGPIXELSX
        ctypes.windll.user32.ReleaseDC(0, hdc)
        scale = max(1.0, dpi / 96.0)
        return dpi, scale
    except Exception:
        return 96, 1.0


def force_foreground_window(hwnd: int):
    """
    Bypasses Windows Foreground Lock Timeout (Anti-Focus-Stealing) via AttachThreadInput.
    Ensures the window is restored, brought to top, and granted foreground input focus.
    """
    if not hwnd or sys.platform != "win32":
        return
    try:
        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32
        fore_hwnd = user32.GetForegroundWindow()
        fore_thread = user32.GetWindowThreadProcessId(fore_hwnd, None)
        cur_thread = kernel32.GetCurrentThreadId()

        user32.ShowWindow(hwnd, SW_RESTORE)
        if fore_thread and cur_thread and fore_thread != cur_thread:
            user32.AttachThreadInput(cur_thread, fore_thread, True)
            user32.BringWindowToTop(hwnd)
            user32.SetForegroundWindow(hwnd)
            user32.AttachThreadInput(cur_thread, fore_thread, False)
        else:
            user32.BringWindowToTop(hwnd)
            user32.SetForegroundWindow(hwnd)
    except Exception:
        try:
            user32.SetForegroundWindow(hwnd)
        except Exception:
            pass


def apply_dwm_title_bar(hwnd: int):
    """
    Applies Windows 10/11 Immersive Dark Mode and custom title bar colors
    to match the application's unified dark aesthetic.
    """
    if not hwnd or sys.platform != "win32":
        return
    try:
        dwmapi = ctypes.windll.dwmapi
        true_val = ctypes.c_int(1)
        # DWMWA_USE_IMMERSIVE_DARK_MODE: 20 (Win11 / Win10 20H1+), 19 (Win10 1809-1909)
        if dwmapi.DwmSetWindowAttribute(hwnd, 20, ctypes.byref(true_val), ctypes.sizeof(true_val)) != 0:
            dwmapi.DwmSetWindowAttribute(hwnd, 19, ctypes.byref(true_val), ctypes.sizeof(true_val))

        # DWMWA_CAPTION_COLOR: 35
        caption_color = ctypes.c_int(TITLE_CAPTION_COLOR)
        dwmapi.DwmSetWindowAttribute(hwnd, 35, ctypes.byref(caption_color), ctypes.sizeof(caption_color))

        # DWMWA_TEXT_COLOR: 36
        text_color = ctypes.c_int(TITLE_TEXT_COLOR)
        dwmapi.DwmSetWindowAttribute(hwnd, 36, ctypes.byref(text_color), ctypes.sizeof(text_color))

        # DWMWA_BORDER_COLOR: 34
        border_color = ctypes.c_int(TITLE_BORDER_COLOR)
        dwmapi.DwmSetWindowAttribute(hwnd, 34, ctypes.byref(border_color), ctypes.sizeof(border_color))
    except Exception as e:
        print(f"[WARN] Failed to apply DWM title bar styling: {e}")


def clean_device_label(name: str) -> str:
    """Format raw audio device name into a clean, human-friendly label."""
    cleaned = name
    for tag in ["(Windows WASAPI)", "(Windows DirectSound)", "(MME)", "(Windows WDM-KS)"]:
        cleaned = cleaned.replace(tag, "")
    # Clean up prefixes like '2- ' or '1- '
    cleaned = re.sub(r'\(\s*\d+-\s*', '(', cleaned)
    cleaned = cleaned.strip()
    if len(cleaned) > 52:
        cleaned = cleaned[:49] + "..."
    return cleaned


if getattr(sys, 'frozen', False):
    APP_DIR = getattr(sys, '_MEIPASS', os.path.dirname(sys.executable))
else:
    APP_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
ICON_PNG_PATH = os.path.join(APP_DIR, "getsu-icon.png")
ICON_ICO_PATH = os.path.join(APP_DIR, "getsu.ico")


# Tray glyph on the 16 px grid of opt/mockup/logo/getsu-tray.svg: (x, y, width, height) of each bar.
TRAY_BARS = ((1, 6, 2, 4), (4, 4, 2, 8), (7, 2, 2, 12), (10, 3, 2, 10), (13, 5, 2, 6))


def ensure_app_icon_ico():
    """
    Checks that the app icon files are present. They are generated by tools/make_icons.py and committed,
    so nothing is rebuilt at run time.
    """
    for path in (ICON_ICO_PATH, ICON_PNG_PATH):
        if not os.path.exists(path):
            print(f"[WARN] App icon file missing: {path} (regenerate with tools/make_icons.py)")


def create_tray_icon_image(is_active: bool = False) -> Image.Image:
    """
    Builds the 64 px notification area icon: the white five-bar glyph plus a status dot.
    The dot is Apple blue when noise cancellation is on and grey when it is stopped.
    Drawn at 16 px per grid unit and reduced to 64 px, so the edges stay smooth.
    """
    unit = 16
    canvas = Image.new("RGBA", (16 * unit, 16 * unit), (0, 0, 0, 0))
    draw = ImageDraw.Draw(canvas)
    for x, y, w, h in TRAY_BARS:
        draw.rounded_rectangle(
            (x * unit, y * unit, (x + w) * unit - 1, (y + h) * unit - 1),
            radius=unit,
            fill=(255, 255, 255, 255),
        )
    # Status dot in the bottom-right corner, separated from the bars by a dark ring.
    dot_color = (10, 132, 255, 255) if is_active else (142, 142, 147, 255)
    draw.ellipse((9 * unit, 9 * unit, 16 * unit - 1, 16 * unit - 1), fill=(32, 32, 32, 255))
    draw.ellipse(
        (int(10.5 * unit), int(10.5 * unit), int(14.5 * unit) - 1, int(14.5 * unit) - 1),
        fill=dot_color,
    )
    return canvas.resize((64, 64), Image.Resampling.LANCZOS)


class GetsuGUI:
    def __init__(self):
        # Explicit AppUserModelID ensures Windows Taskbar pins the custom icon instead of python.exe
        try:
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("getsu.ai.noisecancellation.1")
        except Exception:
            pass

        ensure_app_icon_ico()

        # 1. Detect and apply monitor DPI scaling
        self.dpi, self.dpi_scale = init_dpi_awareness()
        print(f"[GUI] Active Monitor DPI: {self.dpi} (DPI Scale: {self.dpi_scale:.2f}x)")

        self.config = load_config()
        self.router = SmartMicRouter(self.config)
        self.mic_boost_db = int(self.config.get("mic_boost_db", 0))
        self.engine: Optional[AudioEngine] = None
        self.is_running = False
        self._is_testing_voice = False
        self._state_lock = threading.Lock()
        self._vad_timer_lock = threading.Lock()
        self._vad_save_timer: Optional[threading.Timer] = None
        self._cancel_voice_test = False

        self.input_devices: List[Dict] = []
        self.device_map: Dict[str, int] = {}
        self.selected_input_name: Optional[str] = self.config.get("input_device_name")
        self.selected_output_name: Optional[str] = self.config.get("output_device_name")
        self.selected_input_idx: Optional[int] = self.config.get("input_device_id")
        self.selected_output_idx: Optional[int] = self.config.get("output_device_id")
        self.is_vbcable_installed = False
        self.has_icon_texture = False
        self._ui_built = False
        self._app_running = True
        self._is_rescanning = False

        self._hwnd: Optional[int] = None
        self._old_wndproc = None
        self._wndproc_cb = None
        self._tray_icon: Optional[pystray.Icon] = None
        self._device_change_timer: Optional[threading.Timer] = None
        self._device_change_lock = threading.Lock()

        self._refresh_devices()

        # Start stream health watchdog
        threading.Thread(target=self._watchdog_loop, daemon=True).start()

    def s(self, val: float) -> int:
        """Scale pixel value according to active monitor DPI."""
        return max(1, int(round(val * self.dpi_scale)))

    def _release_state_lock(self):
        """Idempotently releases _state_lock without raising if already unlocked."""
        try:
            self._state_lock.release()
        except RuntimeError:
            pass

    def _teardown_engine_and_timers(self):
        """Unified teardown: halts background workers, cancels timers, and restores audio routing."""
        self._app_running = False
        self._cancel_voice_test = True
        for timer in (self._device_change_timer, self._vad_save_timer):
            if timer and timer.is_alive():
                try:
                    timer.cancel()
                except Exception:
                    pass
        if self.engine:
            try:
                self.engine.prepare_for_stop()
            except Exception:
                pass
            self.engine = None
        elif self.router and self.router.is_swapped:
            try:
                self.router.restore_original()
            except Exception:
                pass

    def _compute_current_gain(self) -> float:
        dev_name = ""
        try:
            if self.selected_input_idx is not None and self.selected_input_idx >= 0:
                dev_name = sd.query_devices(self.selected_input_idx)['name']
        except Exception:
            pass
        is_laptop_mic = is_laptop_microphone(dev_name, self.selected_input_idx)
        base_gain = 1.2 if is_laptop_mic else 1.0
        boost_mult = 10.0 ** (self.mic_boost_db / 20.0)
        return base_gain * boost_mult

    def set_mic_boost(self, boost_db: int):
        self.mic_boost_db = boost_db
        self.config["mic_boost_db"] = boost_db
        save_config(self.config)
        self._update_boost_buttons_ui()
        if self.engine:
            new_gain = self._compute_current_gain()
            self.engine.mic_gain = new_gain
            self.engine.mic_boost_db = float(boost_db)
            print(f"[GUI] Live Microphone Gain updated to: {new_gain:.2f}x (+{boost_db} dB)")

    def _update_boost_buttons_ui(self):
        if not self._ui_built:
            return
        try:
            for b in [0, 5, 10, 15]:
                tag = f"btn_boost_{b}"
                if dpg.does_item_exist(tag):
                    if b == self.mic_boost_db:
                        dpg.bind_item_theme(tag, self.theme_boost_active)
                    else:
                        dpg.bind_item_theme(tag, self.theme_boost_inactive)
        except Exception:
            pass

    def _get_windows_default_input(self) -> int:
        """Find the Windows default recording device index directly from WASAPI."""
        # 1. Query the WASAPI Host API's default_input_device (matches Windows Settings exactly)
        for api in sd.query_hostapis():
            if 'WASAPI' in api['name'] and api['default_input_device'] >= 0:
                wasapi_def = api['default_input_device']
                for d in self.input_devices:
                    if d['index'] == wasapi_def:
                        return d['index']
                try:
                    def_name = sd.query_devices(wasapi_def)['name']
                    for d in self.input_devices:
                        if d['name'] == def_name:
                            return d['index']
                except Exception:
                    pass

        # 2. General PortAudio default
        default_in = sd.default.device[0]
        for d in self.input_devices:
            if d['index'] == default_in:
                return d['index']

        return self.input_devices[0]['index'] if self.input_devices else default_in

    def _watchdog_loop(self):
        """Monitors stream health every 1.0s and triggers emergency mic restoration and debounced hotplug recovery if audio stalls."""
        while self._app_running:
            time.sleep(1.0)
            if self.is_running and self.engine and not self._is_rescanning:
                if not self.engine.is_stream_alive():
                    # Emergency Restore Vector (Watchdog): If stream dead while swapped, restore physical mic immediately
                    if self.router and self.router.is_swapped:
                        print("[WATCHDOG] Audio stream stalled while microphone swapped. Emergency restoring default capture...")
                        try:
                            self.router.restore_original()
                            self.set_status_pill("Audio stream stalled. Restored physical mic", COL_WARNING)
                        except Exception as e:
                            print(f"[WATCHDOG] Emergency restore error: {e}")
                    with self._device_change_lock:
                        if not self._device_change_timer or not self._device_change_timer.is_alive():
                            print("[WATCHDOG] Audio stream stalled or disconnected. Triggering rescan...")
                            self._device_change_timer = threading.Timer(0.3, self._handle_hardware_rescan)
                            self._device_change_timer.daemon = True
                            self._device_change_timer.start()

    def _refresh_devices(self, reinit_portaudio: bool = True):
        """Scans connected devices and updates status."""
        if reinit_portaudio and not self.is_running:
            try:
                sd._terminate()
                sd._initialize()
            except Exception:
                pass

        self.input_devices = get_input_devices()
        self.device_map = {}
        for d in self.input_devices:
            label = clean_device_label(d['name'])
            # Avoid duplicate keys if multiple devices have similar names
            if label in self.device_map:
                label = f"{label} #{d['index']}"
            self.device_map[label] = d['index']

        # Match persistent device identity by NAME first, then fallback
        if self.selected_input_name and self.selected_input_name in self.device_map:
            self.selected_input_idx = self.device_map[self.selected_input_name]
        elif self.selected_input_idx is not None and self.selected_input_idx in self.device_map.values():
            self.selected_input_name = self._get_selected_input_label()
        else:
            best_input = auto_select_input_device()
            self.selected_input_idx = best_input['index'] if (best_input and 'index' in best_input and best_input['index'] in self.device_map.values()) else self._get_windows_default_input()
            self.selected_input_name = self._get_selected_input_label()
            self.config["input_device_name"] = self.selected_input_name
            self.config["input_device_id"] = self.selected_input_idx
            save_config(self.config)

        self.is_vbcable_installed, cable_in, _ = check_vbcable_status(force_rescan=reinit_portaudio)
        matching_cable = find_matching_cable_input(self.selected_input_idx)
        if self.is_vbcable_installed and matching_cable:
            self.selected_output_idx = matching_cable['index']
            self.selected_output_name = matching_cable['name']
        elif self.is_vbcable_installed and cable_in:
            self.selected_output_idx = cable_in['index']
            self.selected_output_name = cable_in['name']
        else:
            target_out, _ = auto_select_output_device()
            self.selected_output_idx = target_out['index']
            self.selected_output_name = target_out.get('name', 'Default Output')

        if self._ui_built and dpg.does_item_exist("input_combo"):
            combo_items = list(self.device_map.keys())
            current_label = self._get_selected_input_label()
            dpg.configure_item("input_combo", items=combo_items, default_value=current_label)
            self._update_selection_hint(current_label)


    def _on_device_change_event(self):
        """Debounced listener for Windows WM_DEVICECHANGE hotplug signals."""
        with self._device_change_lock:
            if self._device_change_timer and self._device_change_timer.is_alive():
                self._device_change_timer.cancel()
            # 600ms debounce allows Windows PnP audio stack to fully register endpoints
            self._device_change_timer = threading.Timer(0.6, self._handle_hardware_rescan)
            self._device_change_timer.daemon = True
            self._device_change_timer.start()

    def _handle_hardware_rescan(self):
        """Processes hardware changes, updates dropdown UI, and auto-switches to newly plugged mic if stopped."""
        print("[HARDWARE] Audio device change detected. Rescanning...")
        with self._device_change_lock:
            self._is_rescanning = True
            try:
                was_running = self.is_running
                if was_running and self.engine:
                    print("[HARDWARE] Pausing active stream for clean PortAudio reinitialization...")
                    self.engine.stop()
                    self.engine = None
                    self.is_running = False

                # Safe PortAudio reinitialization: no active streams exist right now
                try:
                    sd._terminate()
                    sd._initialize()
                except Exception as e:
                    print(f"[HARDWARE] PortAudio reinit error: {e}")

                old_devices = dict(self.device_map)
                self._refresh_devices(reinit_portaudio=False)
                new_devices = dict(self.device_map)

                added = set(new_devices.keys()) - set(old_devices.keys())
                removed = set(old_devices.keys()) - set(new_devices.keys())

                if added:
                    print(f"[HARDWARE] New microphone detected: {', '.join(added)}")
                    # If stream was stopped before the rescan, auto-select newly plugged microphone
                    if not was_running:
                        for label in added:
                            self.selected_input_idx = new_devices[label]
                            self.selected_input_name = label
                            self.config["input_device_name"] = label
                            self.config["input_device_id"] = self.selected_input_idx
                            save_config(self.config)
                            print(f"[HARDWARE] Auto-selected new microphone: {label}")
                            break
                    else:
                        # Edge Case 1.6: User plugs new USB headset while active mid-call.
                        # Update original_mic_id to the new headset, keeping CABLE Output default so call isn't interrupted.
                        if self.router and self.router.is_swapped:
                            guid, name = self.router.get_current_default_mic()
                            if guid and "cable" not in name.lower():
                                self.router.original_mic_id = guid
                                self.config["original_mic_id"] = guid
                                save_config(self.config)
                                print(f"[HARDWARE] Updated original_mic_id to new headset: {name}")

                if removed:
                    print(f"[HARDWARE] Microphone removed: {', '.join(removed)}")
                    # Name-based check: if active microphone was removed or is not in new devices
                    if self.selected_input_name in removed or self.selected_input_name not in new_devices:
                        self.selected_input_idx = self._get_windows_default_input()
                        self.selected_input_name = self._get_selected_input_label()
                        self.config["input_device_name"] = self.selected_input_name
                        self.config["input_device_id"] = self.selected_input_idx
                        save_config(self.config)
                        print(f"[HARDWARE] Active microphone disconnected. Fallback to: {self.selected_input_name} (index {self.selected_input_idx})")

                # If active device is in new devices, keep its index updated in case of index shifts
                if self.selected_input_name in new_devices:
                    self.selected_input_idx = new_devices[self.selected_input_name]

                # Validate output endpoint
                self.is_vbcable_installed, cable_in, _ = check_vbcable_status()
                if self.is_vbcable_installed and cable_in:
                    self.selected_output_idx = cable_in['index']
                    self.selected_output_name = cable_in['name']
                else:
                    target_out, _ = auto_select_output_device()
                    self.selected_output_idx = target_out['index']
                    self.selected_output_name = target_out.get('name', 'Default Output')

                # Update UI dropdown
                if self._ui_built and dpg.does_item_exist("input_combo"):
                    combo_items = list(self.device_map.keys())
                    current_label = self._get_selected_input_label()
                    dpg.configure_item("input_combo", items=combo_items, default_value=current_label)

                self._update_cable_banner()

                # Seamlessly restart stream if it was running before
                if was_running and self._app_running:
                    acquired = self._state_lock.acquire(timeout=4.0)
                    if acquired:
                        try:
                            # Re-verify that user didn't stop the stream while rescan was running
                            if not self._app_running:
                                return
                            print(f"[HARDWARE] Resuming stream on [{self.selected_input_idx}] {self.selected_input_name} -> [{self.selected_output_idx}] {self.selected_output_name}")
                            self.engine = create_engine_from_config(
                                self.config,
                                self.selected_input_idx,
                                self.selected_output_idx,
                                router=self.router,
                            )
                            self.engine.start()
                            self.is_running = True
                            if self._ui_built and dpg.does_item_exist("status_badge_text"):
                                dpg.set_value("status_badge_text", "[ ACTIVE ]")
                                dpg.configure_item("status_badge_text", color=COL_ACCENT)
                                dpg.configure_item("btn_toggle", label="STOP", enabled=True)
                                dpg.bind_item_theme("btn_toggle", self.theme_stop_btn)
                            self.set_status_pill("AI Filter Active. Clean Voice Routed", COL_ACCENT)
                            self._update_tray_icon()
                            self._set_voice_test_button_state(False)
                        except Exception as e:
                            print(f"[HARDWARE] Failed to restart stream after rescan: {e}")
                            self.is_running = False
                            if self.engine:
                                try:
                                    self.engine.prepare_for_stop()
                                except Exception:
                                    pass
                                self.engine = None
                            if self._ui_built and dpg.does_item_exist("status_badge_text"):
                                dpg.set_value("status_badge_text", "[ STOPPED ]")
                                dpg.configure_item("status_badge_text", color=COL_TEXT_2)
                                dpg.configure_item("btn_toggle", label="START", enabled=True)
                                dpg.bind_item_theme("btn_toggle", self.theme_start_btn)
                            self._set_voice_test_button_state(True)
                            self.set_status_pill(f"{self._format_audio_error(e)}", COL_ERROR)
                        finally:
                            self._release_state_lock()
                    else:
                        print("[HARDWARE] Could not acquire state lock to resume stream; aborting auto-restart.")

            except Exception as e:
                print(f"[HARDWARE] Error during hardware rescan: {e}")
            finally:
                self._is_rescanning = False

    def _get_selected_input_label(self) -> str:
        for label, idx in self.device_map.items():
            if idx == self.selected_input_idx:
                return label
        return list(self.device_map.keys())[0] if self.device_map else "No Microphone Detected"

    def on_input_changed(self, sender, app_data):
        """Handles user selecting a different microphone from dropdown (Option B: locked while active)."""
        if self.is_running or self._is_testing_voice or not self._state_lock.acquire(blocking=False):
            # Option B: Lock microphone switching while active/testing.
            # Revert dropdown value back to currently active microphone and warn user.
            current_label = self._get_selected_input_label()
            if dpg.does_item_exist("input_combo"):
                dpg.set_value("input_combo", current_label)
            self._update_selection_hint(current_label)
            self.set_status_pill("Stop noise cancellation first to switch microphone.", COL_WARNING)
            return

        try:
            if app_data in self.device_map:
                new_idx = self.device_map[app_data]
                self.selected_input_idx = new_idx
                self.selected_input_name = app_data
                matching_cable = find_matching_cable_input(new_idx)
                if self.is_vbcable_installed and matching_cable:
                    self.selected_output_idx = matching_cable['index']
                    self.selected_output_name = matching_cable['name']
                elif self.is_vbcable_installed:
                    target_out, _ = auto_select_output_device()
                    if target_out:
                        self.selected_output_idx = target_out['index']
                        self.selected_output_name = target_out.get('name', 'Default Output')
                print(f"[GUI] Switched microphone to: [{new_idx}] {app_data} (output: [{self.selected_output_idx}] {self.selected_output_name})")
                self.config["input_device_name"] = app_data
                self.config["input_device_id"] = new_idx
                self.config["output_device_name"] = self.selected_output_name
                self.config["output_device_id"] = self.selected_output_idx
                save_config(self.config)
                self._update_selection_hint(app_data)
                self.set_status_pill(f"Microphone selected: {clean_device_label(app_data)}", COL_ACCENT)
        finally:
            self._release_state_lock()

    def _update_selection_hint(self, selected_label: Optional[str] = None):
        """Updates the dynamic advisory hint beneath the microphone dropdown."""
        if not self._ui_built or not dpg.does_item_exist("input_selection_hint"):
            return
        label = selected_label or self._get_selected_input_label()
        available = list(self.device_map.keys())
        hint = selection_hint(label, available)
        if hint:
            dpg.set_value("input_selection_hint", hint)
            dpg.show_item("input_selection_hint")
        else:
            dpg.hide_item("input_selection_hint")


    def _get_idle_status(self) -> str:
        """Returns the single-line idle status string based on driver and auto_route configuration."""
        if not self.is_vbcable_installed:
            return "Virtual Cable required. Please install driver"
        if self.config.get("auto_route", True):
            return "Ready. Default Mic Routing ON"
        return "Ready. Manual Routing Mode"

    def _get_idle_color(self) -> Sequence[int]:
        """Returns the color for the idle status pill."""
        if not self.is_vbcable_installed:
            return COL_WARNING
        return COL_TEXT_2

    def set_status_pill(self, text: str, color: Optional[Sequence[int]] = None):
        """Updates the compact single-line status pill with text and optional color."""
        if not self._ui_built or not dpg.does_item_exist("active_notice"):
            return
        dpg.set_value("active_notice", text)
        if color:
            dpg.configure_item("active_notice", color=color)

    def _format_audio_error(self, e: Exception) -> str:
        """Translates technical PortAudio / driver exceptions into clear, actionable advice."""
        explained = explain_start_error(e)
        print(f"[AUDIO] Stream error [{explained.kind}] code={explained.code}: {explained.technical}")
        return f"{explained.title}. {explained.advice}"


    def _set_voice_test_button_state(self, enabled: bool):
        """Sets the voice test button enabled/disabled state, contextual label, and theme."""
        if not self._ui_built or not dpg.does_item_exist("btn_voice_test"):
            return
        if enabled and not self.is_vbcable_installed:
            enabled = False
        if enabled:
            dpg.configure_item("btn_voice_test", enabled=True, label="Hear Myself (11s Voice Test)")
            dpg.bind_item_theme("btn_voice_test", self.theme_voice_btn)
        else:
            dpg.configure_item("btn_voice_test", enabled=False, label="Voice Test Unavailable")
            dpg.bind_item_theme("btn_voice_test", self.theme_voice_btn_disabled)

    def on_auto_route_toggled(self, sender, app_data):
        """Handles user toggling default microphone switch setting (Option B: locked while active)."""
        if self.is_running or self._is_testing_voice or not self._state_lock.acquire(blocking=False):
            # Option B: Lock auto-route preference while active/testing.
            # Revert checkbox value back to saved config setting and warn user.
            current_setting = self.config.get("auto_route", True)
            if dpg.does_item_exist("chk_auto_route"):
                dpg.set_value("chk_auto_route", current_setting)
            self.set_status_pill("Stop noise cancellation first to change routing preference.", COL_WARNING)
            return

        try:
            self.config["auto_route"] = bool(app_data)
            save_config(self.config)
            print(f"[GUI] Set as Default Microphone while active: {app_data}")
            if not self.is_running:
                self.set_status_pill(self._get_idle_status(), self._get_idle_color())
        finally:
            self._release_state_lock()

    def on_music_mode_toggled(self, sender, app_data):
        """Live toggle for Music & Performance Mode (bypasses RNNoise, extends hangover to 800ms)."""
        enabled = bool(app_data)
        self.config["music_mode"] = enabled
        save_config(self.config)
        print(f"[GUI] Music & Performance Mode: {'ENABLED' if enabled else 'DISABLED'}")
        if self.engine:
            if enabled:
                self.engine.denoise_enabled = False
                self.engine.gate.hangover_frames = int(800.0 / 10.0)
                self.engine.gate.decay_rate = math.exp(-10.0 / 250.0)
            else:
                self.engine.denoise_enabled = self.config.get("denoise_enabled", True)
                hangover = float(self.config.get("vad_hangover_ms", 320.0))
                decay = float(self.config.get("vad_decay_ms", 80.0))
                self.engine.gate.hangover_frames = int(hangover / 10.0)
                self.engine.gate.decay_rate = math.exp(-10.0 / decay)

    def on_vad_slider_changed(self, sender, app_data):
        """Live thread-safe adjustment of VAD sensitivity without restarting stream."""
        new_val = round(float(app_data), 2)
        self.config["vad_threshold"] = new_val
        self.config["vad_customized"] = True
        if self.engine:
            self.engine.set_vad_threshold(new_val)
        with self._vad_timer_lock:
            if self._vad_save_timer and self._vad_save_timer.is_alive():
                self._vad_save_timer.cancel()
            self._vad_save_timer = threading.Timer(0.5, lambda: save_config(self.config))
            self._vad_save_timer.daemon = True
            self._vad_save_timer.start()

    def toggle_stream(self, sender=None, app_data=None):
        """
        Single toggle button handling Start and Stop with symmetric 3-second state lock.
        Spam-proof with thread locks and smooth status feedback.
        """
        if not self._app_running:
            return

        if self._is_testing_voice:
            self.set_status_pill("Microphone voice test in progress. Please wait.", COL_WARNING)
            return

        if not self._state_lock.acquire(blocking=False):
            # Already transitioning (locked) - ignore spam clicks
            return

        if not self.is_running:
            # START FLOW
            if self.selected_input_idx is None or self.selected_input_idx < 0:
                self.set_status_pill("No microphone detected. Please connect a mic.", COL_ERROR)
                self._release_state_lock()
                return
            if self.selected_output_idx is None or self.selected_output_idx < 0:
                self.set_status_pill("No audio output device detected.", COL_ERROR)
                self._release_state_lock()
                return

            if not self.is_vbcable_installed:
                # Dynamically re-verify before showing modal in case driver was just installed
                self._refresh_devices(reinit_portaudio=True)
                if not self.is_vbcable_installed:
                    self.show_install_modal()
                    self.set_status_pill("Virtual Cable required to route clean voice.", COL_ERROR)
                    self._release_state_lock()
                    return

            # Lock UI immediately into Starting state (accent button, label shows the state)
            dpg.configure_item("btn_toggle", label="Starting...", enabled=False)
            dpg.bind_item_theme("btn_toggle", self.theme_stop_btn)
            dpg.set_value("status_badge_text", "[ STARTING... ]")
            dpg.configure_item("status_badge_text", color=COL_TEXT_2)
            self._set_voice_test_button_state(False)
            self.set_status_pill("Starting AI Filter. Routing to CABLE Output...", COL_TEXT_2)

            def _start_worker():
                start_time = time.monotonic()
                success = False
                try:
                    self.engine = create_engine_from_config(
                        self.config,
                        self.selected_input_idx,
                        self.selected_output_idx,
                        router=self.router,
                    )
                    self.engine.start()
                    self.is_running = True
                    success = True

                    pair = getattr(self.engine, 'resolved_pair', None)
                    if pair:
                        if pair.input != self.selected_input_idx:
                            print(f"[GUI] Synchronizing selected input to resolved safe device [{pair.input}]")
                            self.selected_input_idx = pair.input
                            try:
                                self.selected_input_name = sd.query_devices(pair.input)['name']
                            except Exception:
                                pass
                        if pair.output != self.selected_output_idx:
                            self.selected_output_idx = pair.output
                            try:
                                self.selected_output_name = sd.query_devices(pair.output)['name']
                            except Exception:
                                pass
                        if dpg.does_item_exist("input_combo"):
                            dpg.configure_item("input_combo", default_value=self._get_selected_input_label())

                    self.config["input_device_id"] = self.selected_input_idx
                    self.config["input_device_name"] = self.selected_input_name
                    self.config["output_device_id"] = self.selected_output_idx
                    self.config["output_device_name"] = self.selected_output_name
                    save_config(self.config)
                    self._update_tray_icon()
                except Exception as e:
                    print(f"[ERROR] Failed to start stream: {e}")
                    if self.engine:
                        try:
                            self.engine.prepare_for_stop()
                        except Exception:
                            pass
                        self.engine = None
                    self.is_running = False
                    self.set_status_pill(f"{self._format_audio_error(e)}", COL_ERROR)
                finally:
                    # Enforce symmetric 3-second state lock
                    elapsed = time.monotonic() - start_time
                    remaining = max(0.0, 3.0 - elapsed)
                    if remaining > 0:
                        time.sleep(remaining)

                    if success and self.is_running:
                        dpg.set_value("status_badge_text", "[ ACTIVE ]")
                        dpg.configure_item("status_badge_text", color=COL_ACCENT)
                        dpg.configure_item("btn_toggle", label="STOP", enabled=True)
                        dpg.bind_item_theme("btn_toggle", self.theme_stop_btn)
                        pair = getattr(self.engine, 'resolved_pair', None)
                        if pair and pair.ambiguous:
                            self.set_status_pill("Active. Default mic selected (Confirm in dropdown)", COL_WARNING)
                        elif self.config.get("auto_route", True):
                            if getattr(self.engine, 'router_swap_success', True):
                                self.set_status_pill("AI Filter Active. Clean Voice Routed", COL_ACCENT)
                            else:
                                self.set_status_pill("AI Filter Active. Auto-Route Failed (Select CABLE in Apps)", COL_WARNING)
                        else:
                            self.set_status_pill("AI Filter Active. Manual Output Mode", COL_ACCENT)
                        self._set_voice_test_button_state(False)
                    else:
                        dpg.set_value("status_badge_text", "[ STOPPED ]")
                        dpg.configure_item("status_badge_text", color=COL_TEXT_2)
                        dpg.configure_item("btn_toggle", label="START", enabled=True)
                        dpg.bind_item_theme("btn_toggle", self.theme_start_btn)
                        self._set_voice_test_button_state(True)

                    self._release_state_lock()

            threading.Thread(target=_start_worker, daemon=True).start()

        else:
            # STOP FLOW
            # Lock UI immediately into Stopping state
            dpg.configure_item("btn_toggle", label="Stopping...", enabled=False)
            dpg.set_value("status_badge_text", "[ STOPPING... ]")
            dpg.configure_item("status_badge_text", color=COL_TEXT_2)
            self.set_status_pill("Restoring physical microphone...", COL_TEXT_2)

            def _stop_worker():
                start_time = time.monotonic()
                try:
                    if self.engine:
                        self.engine.prepare_for_stop()
                        self.engine = None
                    elif self.router and self.router.is_swapped:
                        self.router.restore_original()
                    self.is_running = False
                    self._update_tray_icon()
                except Exception as e:
                    print(f"[ERROR] Failed to stop stream cleanly: {e}")
                finally:
                    # Enforce symmetric 3-second state lock
                    elapsed = time.monotonic() - start_time
                    remaining = max(0.0, 3.0 - elapsed)
                    if remaining > 0:
                        time.sleep(remaining)

                    dpg.set_value("status_badge_text", "[ STOPPED ]")
                    dpg.configure_item("status_badge_text", color=COL_TEXT_2)
                    dpg.configure_item("btn_toggle", label="START", enabled=True)
                    dpg.bind_item_theme("btn_toggle", self.theme_start_btn)
                    self._set_voice_test_button_state(True)
                    self.set_status_pill(self._get_idle_status(), self._get_idle_color())
                    self._release_state_lock()

            threading.Thread(target=_stop_worker, daemon=True).start()

    def start_voice_test(self, sender=None, app_data=None):
        """Starts the 11-second in-memory voice preview test."""
        if not self.is_vbcable_installed:
            self.set_status_pill("Virtual Cable required. Please install driver", COL_WARNING)
            return
        if self.is_running:
            self.set_status_pill("Stop noise cancellation first to run voice test.", COL_WARNING)
            return
        if self._is_testing_voice:
            return
        if not self._state_lock.acquire(blocking=False):
            return

        self._is_testing_voice = True
        self._cancel_voice_test = False
        dpg.configure_item("btn_voice_test", enabled=False, label="Testing...")
        dpg.configure_item("btn_toggle", enabled=False)

        threading.Thread(target=self._run_voice_test_worker, daemon=True).start()

    def _run_voice_test_worker(self):
        """
        Executes the 3-phase Voice Test:
        Phase 1: Record 11s from physical microphone (speakers MUTED, zero acoustic feedback).
                 Processed in-flight through HighPassFilter, RNNoise, AdaptiveNoiseGate, and mic_gain.
                 Stored strictly in RAM (numpy float32 buffer).
        Phase 2: Play back 11s processed audio through primary physical playback device.
        Phase 3: Immediate RAM purge (buffer deleted, zero disk files).
        """
        rn = None
        try:
            if self.selected_input_idx is None or self.selected_input_idx < 0:
                self.set_status_pill("No microphone selected for voice test.", COL_ERROR)
                return

            # Resolve safe non-WDM-KS input endpoint for voice preview
            try:
                raw_devs = sd.query_devices()
                raw_apis = sd.query_hostapis()
                safe_input_idx, notes = resolve_single(raw_devs, raw_apis, self.selected_input_idx, is_input=True)
                for note in notes:
                    print(f"[VOICE TEST] {note}")
            except Exception as e:
                print(f"[VOICE TEST] Input resolution fallback: {e}")
                safe_input_idx = self.selected_input_idx

            in_info = sd.query_devices(safe_input_idx)
            in_channels = min(2, max(1, in_info['max_input_channels']))

            # Instantiate DSP chain matching current settings
            boost_db = self.config.get("mic_boost_db", 0)
            boost_mult = 10.0 ** (boost_db / 20.0)
            dev_name = in_info.get('name', '')
            is_laptop = is_laptop_microphone(dev_name, safe_input_idx)
            base_gain = 1.2 if is_laptop else 1.0
            test_gain = base_gain * boost_mult
            out_gain = float(self.config.get("output_gain", 1.08))
            total_gain = out_gain * test_gain
            vad_th = float(self.config.get("vad_threshold", 0.70))

            music_mode = self.config.get("music_mode", False)
            rn = None if music_mode else RNNoise()
            hpf = HighPassFilter(cutoff_hz=80.0, sample_rate=float(SAMPLE_RATE))
            ts = TransientSuppressor()
            hangover_ms = 800.0 if music_mode else float(self.config.get("vad_hangover_ms", 320.0))
            decay_ms = 250.0 if music_mode else float(self.config.get("vad_decay_ms", 80.0))
            gate = AdaptiveNoiseGate(
                threshold=vad_th,
                close_threshold=float(self.config.get("vad_close_threshold", 0.52)),
                hangover_ms=hangover_ms,
                decay_ms=decay_ms,
                attack_ms=28.0,
                frame_ms=10.0,
                onset_threshold=float(self.config.get("vad_onset_threshold", 0.35)),
                onset_snr_db=float(self.config.get("vad_onset_snr_db", 10.0)),
                lookahead=True,
                floor_gain=float(self.config.get("vad_floor_gain", 0.06)),
                cooldown_ms=150.0,
            )
            gate.recalibrate()
            leveler = SpeechLeveler() if self.config.get("auto_level", False) else None

            # Phase 1: Record 11 seconds (speakers MUTED, RAM only)
            total_chunks = 1100
            chunk_size = FRAME_SIZE
            processed_chunks = []

            # Query host API for WASAPI auto_convert sample rate resiliency (e.g. 16kHz Bluetooth mics)
            in_extra_settings = None
            try:
                raw_apis = sd.query_hostapis()
                in_api_idx = in_info.get('hostapi')
                if in_api_idx is not None and isinstance(raw_apis, (list, tuple)) and in_api_idx < len(raw_apis):
                    if 'WASAPI' in raw_apis[in_api_idx]['name'].upper():
                        in_extra_settings = sd.WasapiSettings(auto_convert=True)
            except Exception:
                in_extra_settings = None

            print(f"[VOICE TEST] Phase 1: Recording 11s from [{safe_input_idx}] {in_info['name']}...")
            with sd.InputStream(
                samplerate=SAMPLE_RATE,
                blocksize=chunk_size,
                device=safe_input_idx,
                channels=in_channels,
                dtype='float32',
                extra_settings=in_extra_settings,
            ) as stream:
                for chunk_idx in range(total_chunks):
                    if not self._app_running or self._cancel_voice_test:
                        break
                    indata, _ = stream.read(chunk_size)
                    # Mono mix
                    if indata.ndim > 1 and indata.shape[1] >= 2:
                        mono = (indata[:, 0] + indata[:, 1]) * 0.5
                    elif indata.ndim > 1:
                        mono = indata[:, 0].copy()
                    else:
                        mono = indata.copy()

                    # Unified Single-Frame DSP Pipeline
                    mono, _, _, _ = process_mono_frame(
                        frame_mono=mono,
                        rnnoise=rn,
                        hpf=hpf,
                        gate=gate,
                        total_gain=total_gain,
                        denoise_enabled=bool(rn is not None),
                        leveler=leveler,
                        transient_suppressor=ts,
                        mic_boost_db=float(boost_db),
                    )
                    processed_chunks.append(mono)

                    # Update countdown UI once every 100 chunks (~1.0s)
                    if chunk_idx % 100 == 0:
                        sec_left = 11 - (chunk_idx // 100)
                        self.set_status_pill(f"Voice Test: Recording to RAM... ({sec_left}s left)", COL_ACCENT)
                        dpg.configure_item("btn_voice_test", label=f"Recording... ({sec_left}s)")

            if not self._app_running or self._cancel_voice_test or not processed_chunks:
                return

            # Combine processed chunks strictly in RAM
            full_audio = np.concatenate(processed_chunks, axis=0)

            # Phase 2: Playback 11s through primary physical speakers/headphones
            out_dev = get_physical_output_device(force_rescan=True)
            out_idx = out_dev['index'] if (out_dev and isinstance(out_dev, dict)) else (out_dev if isinstance(out_dev, int) else sd.default.device[1])
            try:
                raw_devs = sd.query_devices()
                raw_apis = sd.query_hostapis()
                safe_out_idx, _ = resolve_single(raw_devs, raw_apis, out_idx, is_input=False)
            except Exception:
                safe_out_idx = out_idx

            out_info = sd.query_devices(safe_out_idx)
            out_channels = min(2, max(1, out_info['max_output_channels']))

            out_extra_settings = None
            try:
                raw_apis = sd.query_hostapis()
                out_api_idx = out_info.get('hostapi')
                if out_api_idx is not None and isinstance(raw_apis, (list, tuple)) and out_api_idx < len(raw_apis):
                    if 'WASAPI' in raw_apis[out_api_idx]['name'].upper():
                        out_extra_settings = sd.WasapiSettings(auto_convert=True)
            except Exception:
                out_extra_settings = None

            print(f"[VOICE TEST] Phase 2: Playing back 11s to [{safe_out_idx}] {out_info['name']}...")
            if out_channels == 2:
                playback_data = np.column_stack((full_audio, full_audio))
            else:
                playback_data = full_audio

            sd.play(playback_data, samplerate=SAMPLE_RATE, device=safe_out_idx, extra_settings=out_extra_settings)

            # Countdown playback (11s)
            for sec in range(11, 0, -1):
                if not self._app_running or self._cancel_voice_test:
                    sd.stop()
                    break
                self.set_status_pill(f"Voice Test: Playing back clean voice... ({sec}s left)", COL_ACCENT)
                dpg.configure_item("btn_voice_test", label=f"Playing back... ({sec}s)")
                time.sleep(1.0)
            sd.stop()

            # Phase 3: Immediate Purge
            del full_audio
            del playback_data
            del processed_chunks
            print("[VOICE TEST] Phase 3: RAM buffer purged. Zero disk clutter.")
            self.set_status_pill("Voice test complete. Clean audio verified", COL_ACCENT)

        except Exception as e:
            print(f"[VOICE TEST] Error during voice preview: {e}")
            self.set_status_pill(f"{self._format_audio_error(e)}", COL_ERROR)
        finally:
            if rn is not None:
                try:
                    rn.close()
                except Exception:
                    pass
            self._is_testing_voice = False
            self._set_voice_test_button_state(True)
            if dpg.does_item_exist("btn_toggle"):
                dpg.configure_item("btn_toggle", enabled=True)
            self._release_state_lock()

    def restore_window(self):
        """Restores window from notification area / system tray and forces foreground focus."""
        if self._hwnd:
            force_foreground_window(self._hwnd)

    def _update_tray_icon(self):
        if self._tray_icon:
            self._tray_icon.icon = create_tray_icon_image(self.is_running)
            self._tray_icon.title = f"Getsu: {'Active' if self.is_running else 'Stopped'}"

    def run_driver_install_thread(self):
        """Runs silent VB-Cable installation in background."""
        dpg.show_item("install_spinner")
        dpg.set_value(
            "install_status_text",
            "Installing VB-Audio Virtual Cable...\n\n"
            "A Windows permission prompt (UAC) may appear.\n"
            "Please click 'Yes' to allow the installation."
        )
        dpg.configure_item("btn_install_confirm", enabled=False)
        dpg.configure_item("btn_install_cancel", enabled=False)

        def worker():
            success = install_vbcable_driver()

            # Robust polling loop: Wait for Windows AudioEndpointBuilder and Audiosrv to register endpoints
            # Check every 1.0s up to 10 seconds
            for _ in range(10):
                time.sleep(1.0)
                self._refresh_devices(reinit_portaudio=True)
                if self.is_vbcable_installed:
                    break

            dpg.hide_item("install_spinner")
            dpg.hide_item("modal_buttons_row")
            dpg.show_item("btn_install_done")

            if self.is_vbcable_installed:
                dpg.set_value(
                    "install_status_text",
                    "Virtual Audio Driver Installed & Ready!\n\n"
                    "No restart required. Clean, denoised audio is now\n"
                    "ready for all your games and voice apps."
                )
                self._update_cable_banner()
            elif success:
                dpg.set_value(
                    "install_status_text",
                    "Driver Setup Completed!\n\n"
                    "If the device does not appear immediately,\n"
                    "a quick Windows restart will finalize it."
                )
                self._update_cable_banner()
            else:
                dpg.set_value(
                    "install_status_text",
                    "Installation was cancelled or failed.\n\n"
                    "You can continue using Headphone Monitor Mode."
                )

        threading.Thread(target=worker, daemon=True).start()

    def on_install_done_click(self, sender, app_data):
        dpg.hide_item("modal_vbcable")
        self._refresh_devices(reinit_portaudio=True)
        self._update_cable_banner()

    def on_install_skip_click(self, sender, app_data):
        dpg.hide_item("modal_vbcable")
        self._refresh_devices(reinit_portaudio=True)
        self._update_cable_banner()

    def show_install_modal(self, sender=None, app_data=None):
        self._refresh_devices(reinit_portaudio=True)
        if self.is_vbcable_installed:
            self._update_cable_banner()
            return
        dpg.show_item("modal_buttons_row")
        dpg.configure_item("btn_install_confirm", enabled=True)
        dpg.configure_item("btn_install_cancel", enabled=True)
        dpg.hide_item("btn_install_done")
        dpg.hide_item("install_spinner")
        dpg.set_value(
            "install_status_text",
            "To send clean, noise-free audio into games, voice chats,\n"
            "and calls, the Virtual Audio driver is required.\n\n"
            "Do you want me to install this now?"
        )
        if dpg.does_item_exist("modal_vbcable"):
            try:
                vp_w = dpg.get_viewport_client_width()
                vp_h = dpg.get_viewport_client_height()
                w = self.s(450)
                h = self.s(240)
                dpg.set_item_pos("modal_vbcable", [max(0, (vp_w - w) // 2), max(0, (vp_h - h) // 2)])
            except Exception:
                pass
            dpg.show_item("modal_vbcable")

    def show_how_to_use_modal(self, sender=None, app_data=None):
        """Displays the How to Use Getsu modal dialog, dynamically centered on the active viewport."""
        if dpg.does_item_exist("modal_how_to_use"):
            try:
                vp_w = dpg.get_viewport_client_width()
                vp_h = dpg.get_viewport_client_height()
                w = self.s(450)
                h = self.s(380)
                dpg.set_item_pos("modal_how_to_use", [max(0, (vp_w - w) // 2), max(0, (vp_h - h) // 2)])
            except Exception:
                pass
            dpg.show_item("modal_how_to_use")

    def _update_cable_banner(self):
        """Updates the status card and bottom status pill."""
        if self.is_vbcable_installed:
            dpg.set_value("banner_text", "Virtual Mic: Ready for All Voice Apps & Games")
            dpg.configure_item("banner_text", color=COL_ACCENT)
            dpg.hide_item("banner_install_btn")
            dpg.configure_item("banner_install_btn", enabled=False)
            if dpg.does_item_exist("banner_how_to_use_btn"):
                dpg.show_item("banner_how_to_use_btn")
                dpg.configure_item("banner_how_to_use_btn", enabled=True)
            self._set_voice_test_button_state(not self.is_running)
            if not self.is_running:
                self.set_status_pill(self._get_idle_status(), self._get_idle_color())
        else:
            dpg.set_value("banner_text", "Driver not installed")
            dpg.configure_item("banner_text", color=COL_WARNING)
            dpg.show_item("banner_install_btn")
            dpg.configure_item("banner_install_btn", enabled=True)
            if dpg.does_item_exist("banner_how_to_use_btn"):
                dpg.hide_item("banner_how_to_use_btn")
                dpg.configure_item("banner_how_to_use_btn", enabled=False)
            self._set_voice_test_button_state(False)
            if not self.is_running:
                self.set_status_pill(self._get_idle_status(), self._get_idle_color())

    def _setup_window_hook(self):
        """Subclass the DearPyGui window so Minimize and Close minimize to tray."""
        title = "Getsu - AI Noise Cancellation"
        self._hwnd = None
        for _ in range(40):
            self._hwnd = user32.FindWindowW(None, title)
            if self._hwnd:
                break
            time.sleep(0.05)

        if not self._hwnd:
            print("[WARN] Getsu window handle not found for subclassing.")
            return

        # Force foreground activation on initial launch
        force_foreground_window(self._hwnd)
        apply_dwm_title_bar(self._hwnd)

        # Disable Windows Maximize button and resizing frame for compact widget
        try:
            style = user32.GetWindowLongW(self._hwnd, GWL_STYLE)
            style &= ~WS_MAXIMIZEBOX
            style &= ~WS_THICKFRAME
            user32.SetWindowLongW(self._hwnd, GWL_STYLE, style)
            user32.SetWindowPos(self._hwnd, 0, 0, 0, 0, 0, SWP_FRAMECHANGED | SWP_NOMOVE | SWP_NOSIZE | SWP_NOZORDER)
        except Exception as e:
            print(f"[WARN] Failed to disable maximize box: {e}")

        WM_DEVICECHANGE = 0x0219
        WM_GETSU_RESTORE = user32.RegisterWindowMessageW("GETSU_RESTORE_WINDOW_MSG")

        def wndproc(hwnd, msg, wparam, lparam):
            if msg == WM_SYSCOMMAND:
                cmd = wparam & 0xFFF0
                if cmd == SC_MINIMIZE:
                    # Minimize -> Hide to system tray
                    user32.ShowWindow(hwnd, SW_HIDE)
                    return 0
                elif cmd == SC_MAXIMIZE:
                    # Maximize -> Block completely (fixed compact widget)
                    return 0
            elif msg == WM_CLOSE:
                # Close button [X] -> Hide to system tray
                user32.ShowWindow(hwnd, SW_HIDE)
                return 0
            elif msg == 0x0011:  # WM_QUERYENDSESSION (Windows Shutdown / Restart Manager)
                return 1
            elif msg == 0x0016:  # WM_ENDSESSION (Windows Shutdown / Restart Manager)
                if wparam:
                    self._teardown_engine_and_timers()
                    dpg.stop_dearpygui()
                    return 0
            elif WM_GETSU_RESTORE and msg == WM_GETSU_RESTORE:
                self.restore_window()
                return 0
            elif msg == WM_DEVICECHANGE:
                self._on_device_change_event()
                return 1
            elif msg == 0x0082:  # WM_NCDESTROY
                self._teardown_engine_and_timers()
                # Cleanly unhook subclass procedure before window destruction
                user32.SetWindowLongPtrW(hwnd, GWLP_WNDPROC, self._old_wndproc)
                return user32.CallWindowProcW(self._old_wndproc, hwnd, msg, wparam, lparam)
            return user32.CallWindowProcW(self._old_wndproc, hwnd, msg, wparam, lparam)

        self._wndproc_cb = WNDPROC(wndproc)
        self._old_wndproc = user32.SetWindowLongPtrW(self._hwnd, GWLP_WNDPROC, self._wndproc_cb)

        # Set window titlebar and taskbar icon explicitly via Win32
        WM_SETICON = 0x0080
        IMAGE_ICON = 1
        LR_LOADFROMFILE = 0x00000010
        if os.path.exists(ICON_ICO_PATH):
            try:
                # Query Windows native icon metrics for small (titlebar/Task Manager) and large (Alt+Tab) icons
                cx_sm = user32.GetSystemMetrics(49) or 16  # SM_CXSMICON
                cy_sm = user32.GetSystemMetrics(50) or 16  # SM_CYSMICON
                cx_lg = user32.GetSystemMetrics(11) or 32  # SM_CXICON
                cy_lg = user32.GetSystemMetrics(12) or 32  # SM_CYICON

                hicon_sm = user32.LoadImageW(None, ICON_ICO_PATH, IMAGE_ICON, cx_sm, cy_sm, LR_LOADFROMFILE)
                hicon_lg = user32.LoadImageW(None, ICON_ICO_PATH, IMAGE_ICON, cx_lg, cy_lg, LR_LOADFROMFILE)
                if hicon_sm:
                    user32.SendMessageW(self._hwnd, WM_SETICON, 0, hicon_sm)
                if hicon_lg:
                    user32.SendMessageW(self._hwnd, WM_SETICON, 1, hicon_lg)
            except Exception:
                pass

    def _is_window_visible(self) -> bool:
        """True when the window is on screen (not hidden to the tray, not minimized). Unknown handle counts as visible."""
        if not self._hwnd:
            return True
        return bool(user32.IsWindowVisible(self._hwnd)) and not bool(user32.IsIconic(self._hwnd))

    def _run_ui_loop(self):
        """
        Render loop that replaces dpg.start_dearpygui(), paced by window visibility (see src/frame_pacing.py).
        Each frame pumps the window message queue, so the longest gap between frames is 100 ms.
        dpg.stop_dearpygui() ends the loop within one frame interval.
        """
        while dpg.is_dearpygui_running():
            started = time.perf_counter()
            dpg.render_dearpygui_frame()
            time.sleep(frame_sleep_s(self._is_window_visible(), time.perf_counter() - started))

    def _start_tray_icon(self):
        """Starts system tray icon in background thread."""
        def on_open(icon, item):
            self.restore_window()

        def on_toggle(icon, item):
            self.toggle_stream()

        def on_exit(icon, item):
            print("[APP] Exiting Getsu...")
            self._teardown_engine_and_timers()
            icon.stop()
            dpg.stop_dearpygui()

        menu = Menu(
            item("Open Getsu", on_open, default=True),
            item("Toggle Start / Stop", on_toggle),
            pystray.Menu.SEPARATOR,
            item("Exit", on_exit),
        )

        self._tray_icon = pystray.Icon("getsu", create_tray_icon_image(self.is_running), "Getsu Noise Cancellation", menu)
        threading.Thread(target=self._tray_icon.run, daemon=True).start()

    def build_ui(self):
        dpg.create_context()
        self._ui_built = True

        # --- APP ICON TEXTURE REGISTRY ---
        self.has_icon_texture = False
        if os.path.exists(ICON_PNG_PATH):
            try:
                iw, ih, ic, idata = dpg.load_image(ICON_PNG_PATH)
                with dpg.texture_registry(show=False):
                    dpg.add_static_texture(width=iw, height=ih, default_value=idata, tag="app_icon_texture")
                self.has_icon_texture = True
            except Exception as e:
                print(f"[WARN] Failed to load icon texture: {e}")

        # --- FONT CONFIGURATION: BUNDLED INTER (WITH SYSTEM FALLBACK) ---
        bundled_font_dir = os.path.join(APP_DIR, "assets", "fonts")
        system_font_dir = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "Fonts")

        inter_reg = os.path.join(bundled_font_dir, "Inter-Regular.ttf")
        inter_semi = os.path.join(bundled_font_dir, "Inter-SemiBold.ttf")
        inter_bold = os.path.join(bundled_font_dir, "Inter-Bold.ttf")

        if os.path.exists(inter_reg):
            path_regular = inter_reg
            path_semibold = inter_semi if os.path.exists(inter_semi) else inter_reg
            path_bold = inter_bold if os.path.exists(inter_bold) else path_semibold
            font_family = "Bundled Inter"
        elif os.path.exists(os.path.join(system_font_dir, "segoeui.ttf")):
            path_regular = os.path.join(system_font_dir, "segoeui.ttf")
            path_semibold = os.path.join(system_font_dir, "seguisb.ttf")
            path_bold = os.path.join(system_font_dir, "segoeuib.ttf")
            font_family = "System Segoe UI"
        else:
            path_regular = os.path.join(system_font_dir, "arial.ttf")
            path_semibold = os.path.join(system_font_dir, "arialbd.ttf")
            path_bold = os.path.join(system_font_dir, "arialbd.ttf")
            font_family = "Universal Arial"

        print(f"[GUI] Active Font Engine: {font_family}")

        # Type scale from the mockup: 11, 12, 13, 14 and 16 px. SemiBold for titles and buttons.
        with dpg.font_registry():
            if path_regular and os.path.exists(path_regular):
                bold_file = path_semibold if (path_semibold and os.path.exists(path_semibold)) else path_bold
                if not (bold_file and os.path.exists(bold_file)):
                    bold_file = path_regular
                self.font_body = dpg.add_font(path_regular, self.s(13))
                self.font_small = dpg.add_font(path_regular, self.s(11))
                self.font_notice = dpg.add_font(path_regular, self.s(12))
                self.font_header = dpg.add_font(bold_file, self.s(14))
                self.font_btn = dpg.add_font(bold_file, self.s(13))
                self.font_start = dpg.add_font(bold_file, self.s(16))
                dpg.bind_font(self.font_body)
            else:
                self.font_body = None
                self.font_header = None
                self.font_btn = None
                self.font_start = None
                self.font_notice = None
                self.font_small = None

        # Dark theme from the approved mockup palette. Rounding: cards 14, buttons 10, segmented 9.
        with dpg.theme() as global_theme:
            with dpg.theme_component(dpg.mvAll):
                dpg.add_theme_color(dpg.mvThemeCol_WindowBg, COL_WINDOW)
                dpg.add_theme_color(dpg.mvThemeCol_ChildBg, COL_CARD)
                dpg.add_theme_color(dpg.mvThemeCol_PopupBg, COL_CARD)
                dpg.add_theme_color(dpg.mvThemeCol_Text, COL_TEXT)
                dpg.add_theme_color(dpg.mvThemeCol_TextDisabled, COL_TEXT_3)
                dpg.add_theme_color(dpg.mvThemeCol_Border, COL_SEPARATOR)
                dpg.add_theme_color(dpg.mvThemeCol_Separator, COL_SEPARATOR)
                dpg.add_theme_color(dpg.mvThemeCol_FrameBg, COL_FILL)
                dpg.add_theme_color(dpg.mvThemeCol_FrameBgHovered, COL_FILL_2)
                dpg.add_theme_color(dpg.mvThemeCol_FrameBgActive, COL_FILL_3)
                dpg.add_theme_color(dpg.mvThemeCol_Button, COL_FILL)
                dpg.add_theme_color(dpg.mvThemeCol_ButtonHovered, COL_FILL_2)
                dpg.add_theme_color(dpg.mvThemeCol_ButtonActive, COL_FILL_3)
                dpg.add_theme_color(dpg.mvThemeCol_Header, COL_ACCENT_SOFT)
                dpg.add_theme_color(dpg.mvThemeCol_HeaderHovered, COL_CARD_HOVER)
                dpg.add_theme_color(dpg.mvThemeCol_HeaderActive, COL_ACCENT_SOFT_2)
                dpg.add_theme_color(dpg.mvThemeCol_CheckMark, COL_ACCENT)
                dpg.add_theme_color(dpg.mvThemeCol_SliderGrab, COL_ACCENT)
                dpg.add_theme_color(dpg.mvThemeCol_SliderGrabActive, COL_ACCENT_HOVER)
                dpg.add_theme_color(dpg.mvThemeCol_ScrollbarBg, (0, 0, 0, 0))
                dpg.add_theme_color(dpg.mvThemeCol_ScrollbarGrab, COL_FILL_3)
                dpg.add_theme_color(dpg.mvThemeCol_ScrollbarGrabHovered, COL_FILL_3)
                dpg.add_theme_color(dpg.mvThemeCol_ScrollbarGrabActive, COL_FILL_3)
                dpg.add_theme_color(dpg.mvThemeCol_TextSelectedBg, COL_ACCENT_SOFT_2)
                dpg.add_theme_style(dpg.mvStyleVar_FrameRounding, self.s(10))
                dpg.add_theme_style(dpg.mvStyleVar_GrabRounding, self.s(2))
                dpg.add_theme_style(dpg.mvStyleVar_ChildRounding, self.s(14))
                dpg.add_theme_style(dpg.mvStyleVar_PopupRounding, self.s(10))
                dpg.add_theme_style(dpg.mvStyleVar_WindowRounding, 0)
                dpg.add_theme_style(dpg.mvStyleVar_ItemSpacing, self.s(10), self.s(8))
                dpg.add_theme_style(dpg.mvStyleVar_WindowPadding, self.s(14), self.s(12))

        # Status card: flat card without border.
        with dpg.theme() as theme_status_card:
            with dpg.theme_component(dpg.mvChildWindow):
                dpg.add_theme_style(dpg.mvStyleVar_WindowPadding, self.s(14), self.s(6))
                dpg.add_theme_style(dpg.mvStyleVar_ChildRounding, self.s(14))
                dpg.add_theme_style(dpg.mvStyleVar_ChildBorderSize, 0)

        # Status text group theme (Zero item spacing so spacer sets exact vertical offset)
        with dpg.theme() as theme_status_text_group:
            with dpg.theme_component(dpg.mvGroup):
                dpg.add_theme_style(dpg.mvStyleVar_ItemSpacing, 0, 0)

        # Primary accent button: START. The running state (STOP) uses the same accent, it is an "on" state.
        with dpg.theme() as self.theme_start_btn:
            with dpg.theme_component(dpg.mvButton):
                dpg.add_theme_color(dpg.mvThemeCol_Button, COL_ACCENT)
                dpg.add_theme_color(dpg.mvThemeCol_ButtonHovered, COL_ACCENT_HOVER)
                dpg.add_theme_color(dpg.mvThemeCol_ButtonActive, COL_ACCENT_PRESSED)
                dpg.add_theme_color(dpg.mvThemeCol_Text, COL_WHITE)
                dpg.add_theme_style(dpg.mvStyleVar_FrameRounding, self.s(10))
        self.theme_stop_btn = self.theme_start_btn

        # Tinted accent button: install driver, how to use.
        with dpg.theme() as theme_banner_btn:
            with dpg.theme_component(dpg.mvButton):
                dpg.add_theme_color(dpg.mvThemeCol_Button, COL_ACCENT_SOFT)
                dpg.add_theme_color(dpg.mvThemeCol_ButtonHovered, COL_ACCENT_SOFT_2)
                dpg.add_theme_color(dpg.mvThemeCol_ButtonActive, COL_ACCENT_SOFT_2)
                dpg.add_theme_color(dpg.mvThemeCol_Text, COL_ACCENT)
                dpg.add_theme_style(dpg.mvStyleVar_FrameBorderSize, 0.0)
                dpg.add_theme_style(dpg.mvStyleVar_FrameRounding, self.s(10))

        # Link button (sponsor): accent text, no background.
        with dpg.theme() as theme_link_btn:
            with dpg.theme_component(dpg.mvButton):
                dpg.add_theme_color(dpg.mvThemeCol_Button, (0, 0, 0, 0))
                dpg.add_theme_color(dpg.mvThemeCol_ButtonHovered, COL_ACCENT_SOFT)
                dpg.add_theme_color(dpg.mvThemeCol_ButtonActive, COL_ACCENT_SOFT_2)
                dpg.add_theme_color(dpg.mvThemeCol_Text, COL_ACCENT)

        # Selected segment: filled accent with white text.
        with dpg.theme() as self.theme_boost_active:
            with dpg.theme_component(dpg.mvButton):
                dpg.add_theme_color(dpg.mvThemeCol_Button, COL_ACCENT)
                dpg.add_theme_color(dpg.mvThemeCol_ButtonHovered, COL_ACCENT_HOVER)
                dpg.add_theme_color(dpg.mvThemeCol_ButtonActive, COL_ACCENT_PRESSED)
                dpg.add_theme_color(dpg.mvThemeCol_Text, COL_WHITE)
                dpg.add_theme_style(dpg.mvStyleVar_FrameBorderSize, 0.0)
                dpg.add_theme_style(dpg.mvStyleVar_FrameRounding, self.s(9))

        # Unselected segment: translucent fill, primary text.
        with dpg.theme() as self.theme_boost_inactive:
            with dpg.theme_component(dpg.mvButton):
                dpg.add_theme_color(dpg.mvThemeCol_Button, COL_FILL)
                dpg.add_theme_color(dpg.mvThemeCol_ButtonHovered, COL_FILL_2)
                dpg.add_theme_color(dpg.mvThemeCol_ButtonActive, COL_FILL_3)
                dpg.add_theme_color(dpg.mvThemeCol_Text, COL_TEXT)
                dpg.add_theme_style(dpg.mvStyleVar_FrameBorderSize, 0.0)
                dpg.add_theme_style(dpg.mvStyleVar_FrameRounding, self.s(9))

        # Tinted button (voice test, dialog confirm): accent text on a soft accent fill.
        with dpg.theme() as self.theme_voice_btn:
            with dpg.theme_component(dpg.mvButton):
                dpg.add_theme_color(dpg.mvThemeCol_Button, COL_ACCENT_SOFT)
                dpg.add_theme_color(dpg.mvThemeCol_ButtonHovered, COL_ACCENT_SOFT_2)
                dpg.add_theme_color(dpg.mvThemeCol_ButtonActive, COL_ACCENT_SOFT_2)
                dpg.add_theme_color(dpg.mvThemeCol_Text, COL_ACCENT)
                dpg.add_theme_style(dpg.mvStyleVar_FrameRounding, self.s(10))

        # Disabled tinted button: grey text on the neutral fill, no hover change.
        with dpg.theme() as self.theme_voice_btn_disabled:
            for state in (True, False):
                with dpg.theme_component(dpg.mvButton, enabled_state=state):
                    dpg.add_theme_color(dpg.mvThemeCol_Button, COL_FILL)
                    dpg.add_theme_color(dpg.mvThemeCol_ButtonHovered, COL_FILL)
                    dpg.add_theme_color(dpg.mvThemeCol_ButtonActive, COL_FILL)
                    dpg.add_theme_color(dpg.mvThemeCol_Text, COL_TEXT_3)
                    dpg.add_theme_color(dpg.mvThemeCol_TextDisabled, COL_TEXT_3)
                    dpg.add_theme_style(dpg.mvStyleVar_FrameRounding, self.s(10))

        # Unified modal theme: window background, hairline border, 12 px corners.
        with dpg.theme() as theme_modal:
            with dpg.theme_component(dpg.mvAll):
                dpg.add_theme_color(dpg.mvThemeCol_WindowBg, COL_WINDOW)
                dpg.add_theme_color(dpg.mvThemeCol_TitleBg, COL_WINDOW)
                dpg.add_theme_color(dpg.mvThemeCol_TitleBgActive, COL_WINDOW)
                dpg.add_theme_color(dpg.mvThemeCol_TitleBgCollapsed, COL_WINDOW)
                dpg.add_theme_color(dpg.mvThemeCol_Border, COL_SEPARATOR)
                dpg.add_theme_style(dpg.mvStyleVar_FramePadding, self.s(10), self.s(10))
                dpg.add_theme_style(dpg.mvStyleVar_WindowPadding, self.s(18), self.s(16))
                dpg.add_theme_style(dpg.mvStyleVar_WindowRounding, self.s(12))

        # Secondary button (Not now, Cancel): neutral fill.
        with dpg.theme() as theme_secondary_btn:
            with dpg.theme_component(dpg.mvButton):
                dpg.add_theme_color(dpg.mvThemeCol_Button, COL_FILL)
                dpg.add_theme_color(dpg.mvThemeCol_ButtonHovered, COL_FILL_2)
                dpg.add_theme_color(dpg.mvThemeCol_ButtonActive, COL_FILL_3)
                dpg.add_theme_color(dpg.mvThemeCol_Text, COL_TEXT)
                dpg.add_theme_style(dpg.mvStyleVar_FrameBorderSize, 0.0)
                dpg.add_theme_style(dpg.mvStyleVar_FrameRounding, self.s(10))

        dpg.bind_theme(global_theme)

        # Main window: zero scrollbar, no resize, no move
        with dpg.window(
            tag="main_window",
            no_title_bar=True,
            no_resize=True,
            no_move=True,
            no_scrollbar=True,
            no_scroll_with_mouse=True,
        ):
            # --- HEADER ---
            with dpg.table(header_row=False, policy=dpg.mvTable_SizingStretchProp):
                dpg.add_table_column(init_width_or_weight=0.74)
                dpg.add_table_column(init_width_or_weight=0.26)
                with dpg.table_row():
                    with dpg.group(horizontal=True):
                        if self.has_icon_texture:
                            dpg.add_image("app_icon_texture", width=self.s(22), height=self.s(22))
                            dpg.add_spacer(width=self.s(3))
                        dpg.add_text("GETSU - AI NOISE CANCELLATION", color=COL_TEXT, tag="header_title_text")
                    dpg.add_text("[ STOPPED ]", tag="status_badge_text", color=COL_TEXT_2)

            dpg.add_spacer(height=self.s(3))

            # --- VB-CABLE STATUS CARD ---
            with dpg.child_window(tag="status_card", width=-1, height=self.s(44), border=True, no_scrollbar=True):
                with dpg.table(header_row=False, policy=dpg.mvTable_SizingStretchProp):
                    dpg.add_table_column(init_width_or_weight=1.0)
                    dpg.add_table_column(width_fixed=True, init_width_or_weight=self.s(120))
                    with dpg.table_row():
                        with dpg.group(tag="status_text_group"):
                            dpg.add_spacer(height=self.s(3))
                            dpg.add_text(
                                "Virtual Mic Status",
                                tag="banner_text",
                                color=COL_ACCENT
                            )
                        with dpg.group(tag="status_btn_group"):
                            dpg.add_button(
                                label="Install Driver",
                                callback=self.show_install_modal,
                                tag="banner_install_btn",
                                width=-1,
                                height=self.s(28),
                                show=False
                            )
                            dpg.add_button(
                                label="How to Use",
                                callback=self.show_how_to_use_modal,
                                tag="banner_how_to_use_btn",
                                width=-1,
                                height=self.s(28),
                                show=False
                            )
            dpg.bind_item_theme("status_card", theme_status_card)
            dpg.bind_item_theme("status_text_group", theme_status_text_group)
            dpg.bind_item_theme("status_btn_group", theme_status_text_group)
            dpg.bind_item_theme("banner_install_btn", theme_banner_btn)
            dpg.bind_item_theme("banner_how_to_use_btn", theme_banner_btn)

            dpg.add_spacer(height=self.s(3))

            # --- INPUT DEVICE SELECTION ---
            dpg.add_text("Microphone Input:", color=COL_TEXT_2)
            combo_items = list(self.device_map.keys())
            dpg.add_combo(
                items=combo_items,
                default_value=self._get_selected_input_label(),
                callback=self.on_input_changed,
                tag="input_combo",
                width=-1
            )
            dpg.add_text("", tag="input_selection_hint", wrap=self.s(360), color=COL_WARNING, show=False)

            dpg.add_spacer(height=self.s(4))


            # --- MICROPHONE BOOST (0, 5, 10, 15 dB) ---
            dpg.add_text("Microphone Boost:", color=COL_TEXT_2)
            with dpg.table(header_row=False, policy=dpg.mvTable_SizingStretchSame):
                dpg.add_table_column(init_width_or_weight=1.0)
                dpg.add_table_column(init_width_or_weight=1.0)
                dpg.add_table_column(init_width_or_weight=1.0)
                dpg.add_table_column(init_width_or_weight=1.0)
                with dpg.table_row():
                    dpg.add_button(
                        label="0 dB",
                        width=-1,
                        height=self.s(30),
                        tag="btn_boost_0",
                        callback=lambda: self.set_mic_boost(0)
                    )
                    dpg.add_button(
                        label="+5 dB",
                        width=-1,
                        height=self.s(30),
                        tag="btn_boost_5",
                        callback=lambda: self.set_mic_boost(5)
                    )
                    dpg.add_button(
                        label="+10 dB",
                        width=-1,
                        height=self.s(30),
                        tag="btn_boost_10",
                        callback=lambda: self.set_mic_boost(10)
                    )
                    dpg.add_button(
                        label="+15 dB",
                        width=-1,
                        height=self.s(30),
                        tag="btn_boost_15",
                        callback=lambda: self.set_mic_boost(15)
                    )

            dpg.add_spacer(height=self.s(5))

            # --- VOICE SENSITIVITY THRESHOLD SLIDER ---
            dpg.add_text("Voice Sensitivity Threshold:", color=COL_TEXT_2)
            dpg.add_slider_float(
                default_value=float(self.config.get("vad_threshold", 0.70)),
                min_value=0.40,
                max_value=0.90,
                format="%.2f",
                callback=self.on_vad_slider_changed,
                tag="slider_vad",
                width=-1,
                height=self.s(26)
            )

            dpg.add_spacer(height=self.s(4))

            # --- DEFAULT MICROPHONE SWITCH CHECKBOX ---
            dpg.add_checkbox(
                label="Set as Default Microphone while active",
                default_value=self.config.get("auto_route", True),
                callback=self.on_auto_route_toggled,
                tag="chk_auto_route"
            )

            dpg.add_spacer(height=self.s(4))

            # --- MUSIC & PERFORMANCE MODE CHECKBOX ---
            dpg.add_checkbox(
                label="Music & Singing Mode (Bypasses AI filter, 800ms sustain)",
                default_value=self.config.get("music_mode", False),
                callback=self.on_music_mode_toggled,
                tag="chk_music_mode"
            )

            dpg.add_spacer(height=self.s(6))

            # --- SINGLE ACTION BUTTON: START / STOP ---
            dpg.add_button(
                label="START",
                callback=self.toggle_stream,
                tag="btn_toggle",
                width=-1,
                height=self.s(50)
            )
            dpg.bind_item_theme("btn_toggle", self.theme_start_btn)

            dpg.add_spacer(height=self.s(4))

            # --- HEAR MYSELF (11s VOICE PREVIEW TEST) ---
            dpg.add_button(
                label="Hear Myself (11s Voice Test)",
                callback=self.start_voice_test,
                tag="btn_voice_test",
                width=-1,
                height=self.s(32)
            )
            dpg.bind_item_theme("btn_voice_test", self.theme_voice_btn)

            dpg.add_spacer(height=self.s(6))

            # --- COMPACT STATUS PILL (OPTION B) ---
            dpg.add_text(
                self._get_idle_status(),
                tag="active_notice",
                color=self._get_idle_color(),
                wrap=self.s(485)
            )

            dpg.add_spacer(height=self.s(6))
            dpg.add_separator()
            dpg.add_spacer(height=self.s(4))

            # --- FOOTER: CREATOR & SPONSOR ---
            with dpg.group(horizontal=True, tag="footer_sponsor_row"):
                dpg.add_text("Created by Ihsan  |  Sponsored by", color=COL_TEXT_3, tag="footer_creator_text")
                dpg.add_button(
                    label="Kopi Reman",
                    callback=lambda: webbrowser.open("https://www.instagram.com/kopireman/"),
                    small=True,
                    tag="sponsor_link_btn"
                )
            dpg.bind_item_theme("sponsor_link_btn", theme_link_btn)

            # Bind specialized fonts to elements
            if self.font_header:
                dpg.bind_item_font("header_title_text", self.font_header)
                dpg.bind_item_font("status_badge_text", self.font_header)
            if self.font_start:
                dpg.bind_item_font("btn_toggle", self.font_start)
            if self.font_notice:
                dpg.bind_item_font("active_notice", self.font_notice)
            if self.font_small:
                dpg.bind_item_font("footer_creator_text", self.font_small)
                dpg.bind_item_font("sponsor_link_btn", self.font_small)

            # Initialize boost button states
            self._update_boost_buttons_ui()

            # --- MODAL: VB-CABLE SETUP ---
            modal_w = self.s(450)
            modal_h = self.s(240)
            modal_x = max(0, (self.s(520) - modal_w) // 2)
            modal_y = max(0, (self.s(652) - modal_h) // 2)
            with dpg.window(
                label="Virtual Audio Cable Setup",
                modal=True,
                show=False,
                tag="modal_vbcable",
                no_resize=True,
                no_move=True,
                no_scrollbar=True,
                width=modal_w,
                height=modal_h,
                pos=[modal_x, modal_y]
            ):
                dpg.add_text(
                    "To send clean, noise-free audio into games, voice chats,\n"
                    "and calls, the Virtual Audio driver is required.\n\n"
                    "Do you want me to install this now?",
                    tag="install_status_text",
                    color=COL_TEXT
                )
                dpg.add_spacer(height=self.s(8))
                dpg.add_loading_indicator(tag="install_spinner", show=False, radius=float(self.s(3)))
                dpg.add_spacer(height=self.s(8))

                with dpg.group(horizontal=True, tag="modal_buttons_row"):
                    dpg.add_button(
                        label="Install (1-Click)",
                        callback=lambda s, a: self.run_driver_install_thread(),
                        tag="btn_install_confirm",
                        width=self.s(180),
                        height=self.s(34)
                    )
                    dpg.add_button(
                        label="Not Now",
                        callback=self.on_install_skip_click,
                        tag="btn_install_cancel",
                        width=self.s(110),
                        height=self.s(34)
                    )

                with dpg.group(horizontal=True):
                    dpg.add_spacer(width=(modal_w - self.s(130)) // 2 - self.s(18))
                    dpg.add_button(
                        label="OK",
                        callback=self.on_install_done_click,
                        tag="btn_install_done",
                        show=False,
                        width=self.s(130),
                        height=self.s(34)
                    )
            dpg.bind_item_theme("modal_vbcable", theme_modal)
            dpg.bind_item_theme("btn_install_confirm", self.theme_voice_btn)
            dpg.bind_item_theme("btn_install_cancel", theme_secondary_btn)
            dpg.bind_item_theme("btn_install_done", self.theme_voice_btn)

            # --- MODAL: HOW TO USE GETSU ---
            guide_w = self.s(450)
            guide_h = self.s(380)
            guide_x = max(0, (self.s(520) - guide_w) // 2)
            guide_y = max(0, (self.s(652) - guide_h) // 2)
            with dpg.window(
                label="How to Use Getsu",
                modal=True,
                show=False,
                tag="modal_how_to_use",
                no_resize=True,
                no_move=True,
                no_scrollbar=True,
                width=guide_w,
                height=guide_h,
                pos=[guide_x, guide_y]
            ):
                dpg.add_text("Just click START. Most apps will switch to clean\naudio automatically.", color=COL_TEXT)
                dpg.add_spacer(height=self.s(6))

                dpg.add_text("Notes: Only When Needed (Games & Voice Apps)", color=COL_ACCENT)
                dpg.add_text(
                    "If an app or game doesn't pick up the noise filter,\ngo to its audio settings and choose:",
                    color=COL_TEXT_2
                )
                dpg.add_spacer(height=self.s(2))
                dpg.add_text("Microphone  ->  CABLE Output", color=COL_ACCENT)
                dpg.add_spacer(height=self.s(2))
                dpg.add_text("(This guarantees clean voice, no restart needed.)", color=COL_TEXT_2)

                dpg.add_spacer(height=self.s(12))
                with dpg.group(horizontal=True):
                    dpg.add_spacer(width=(guide_w - self.s(130)) // 2 - self.s(18))
                    dpg.add_button(
                        label="Got It",
                        callback=lambda: dpg.hide_item("modal_how_to_use"),
                        tag="btn_how_to_use_ok",
                        width=self.s(130),
                        height=self.s(34)
                    )
            dpg.bind_item_theme("modal_how_to_use", theme_modal)
            dpg.bind_item_theme("btn_how_to_use_ok", self.theme_voice_btn)

            # Buttons use the SemiBold 13 px face; this runs after every button has been created.
            if self.font_btn:
                for tag in ("btn_voice_test", "banner_install_btn", "banner_how_to_use_btn",
                            "btn_boost_0", "btn_boost_5", "btn_boost_10", "btn_boost_15",
                            "btn_install_confirm", "btn_install_cancel", "btn_install_done",
                            "btn_how_to_use_ok"):
                    if dpg.does_item_exist(tag):
                        dpg.bind_item_font(tag, self.font_btn)

        # Viewport configuration - center on active monitor work area (taskbar-aware) from frame 0
        vp_w = self.s(520)
        vp_h = self.s(652)
        try:
            class MONITORINFO(ctypes.Structure):
                _fields_ = [
                    ("cbSize", wintypes.DWORD),
                    ("rcMonitor", wintypes.RECT),
                    ("rcWork", wintypes.RECT),
                    ("dwFlags", wintypes.DWORD),
                ]
            cursor_pos = wintypes.POINT()
            user32.GetCursorPos(ctypes.byref(cursor_pos))
            MONITOR_DEFAULTTONEAREST = 2
            hmon = user32.MonitorFromPoint(cursor_pos, MONITOR_DEFAULTTONEAREST)
            mi = MONITORINFO()
            mi.cbSize = ctypes.sizeof(MONITORINFO)
            if hmon and user32.GetMonitorInfoW(hmon, ctypes.byref(mi)):
                work_w = mi.rcWork.right - mi.rcWork.left
                work_h = mi.rcWork.bottom - mi.rcWork.top
                x_pos = mi.rcWork.left + max(0, (work_w - vp_w) // 2)
                y_pos = mi.rcWork.top + max(0, (work_h - vp_h) // 2)
            else:
                work_rect = wintypes.RECT()
                user32.SystemParametersInfoW(0x0030, 0, ctypes.byref(work_rect), 0)
                work_w = work_rect.right - work_rect.left
                work_h = work_rect.bottom - work_rect.top
                x_pos = work_rect.left + max(0, (work_w - vp_w) // 2)
                y_pos = work_rect.top + max(0, (work_h - vp_h) // 2)
        except Exception:
            try:
                screen_w = user32.GetSystemMetrics(0) or 1920
                screen_h = user32.GetSystemMetrics(1) or 1080
            except Exception:
                screen_w = 1920
                screen_h = 1080
            x_pos = max(0, (screen_w - vp_w) // 2)
            y_pos = max(0, (screen_h - vp_h) // 2)

        dpg.create_viewport(
            title="Getsu - AI Noise Cancellation",
            small_icon=ICON_ICO_PATH if os.path.exists(ICON_ICO_PATH) else "",
            large_icon=ICON_ICO_PATH if os.path.exists(ICON_ICO_PATH) else "",
            width=vp_w,
            height=vp_h,
            x_pos=x_pos,
            y_pos=y_pos,
            resizable=False,
            decorated=True
        )
        dpg.setup_dearpygui()
        dpg.show_viewport()
        dpg.set_primary_window("main_window", True)

        # Render 3 layout frames to settle font glyphs, then fine-tune viewport height
        dpg.render_dearpygui_frame()
        dpg.render_dearpygui_frame()
        dpg.render_dearpygui_frame()
        try:
            rect_max = dpg.get_item_rect_max("footer_sponsor_row")
            diff = dpg.get_viewport_height() - dpg.get_viewport_client_height()
            auto_fit_h = int(rect_max[1] + self.s(26)) + diff
            if abs(auto_fit_h - dpg.get_viewport_height()) > 2:
                dpg.set_viewport_height(auto_fit_h)
            dpg.render_dearpygui_frame()
        except Exception:
            pass

        # Synchronous dead-center window placement on active monitor work area (taskbar-aware)
        try:
            hwnd = user32.FindWindowW(None, "Getsu - AI Noise Cancellation")
            if hwnd:
                apply_dwm_title_bar(hwnd)
                rect = wintypes.RECT()
                user32.GetWindowRect(hwnd, ctypes.byref(rect))
                win_w = rect.right - rect.left
                win_h = rect.bottom - rect.top
                MONITOR_DEFAULTTONEAREST = 2
                hmon = user32.MonitorFromWindow(hwnd, MONITOR_DEFAULTTONEAREST)
                mi = MONITORINFO()
                mi.cbSize = ctypes.sizeof(MONITORINFO)
                if hmon and user32.GetMonitorInfoW(hmon, ctypes.byref(mi)):
                    work_w = mi.rcWork.right - mi.rcWork.left
                    work_h = mi.rcWork.bottom - mi.rcWork.top
                    cx = mi.rcWork.left + max(0, (work_w - win_w) // 2)
                    cy = mi.rcWork.top + max(0, (work_h - win_h) // 2)
                    user32.SetWindowPos(hwnd, 0, cx, cy, 0, 0, SWP_NOSIZE | SWP_NOZORDER)
        except Exception as e:
            print(f"[WARN] Failed to center window: {e}")

        self._update_cable_banner()
        self._update_selection_hint()

        if not self.is_vbcable_installed:

            dpg.show_item("modal_vbcable")

        # Start tray icon & window hook
        self._start_tray_icon()
        threading.Thread(target=self._setup_window_hook, daemon=True).start()

        # Handle terminal signals cleanly (Ctrl+C / SIGINT / SIGTERM)
        def _sig_handler(sig, frame):
            print("\n[GUI] Terminal exit signal received. Closing cleanly...")
            dpg.stop_dearpygui()

        try:
            signal.signal(signal.SIGINT, _sig_handler)
            signal.signal(signal.SIGTERM, _sig_handler)
        except Exception:
            pass

        # Run UI event loop
        self._run_ui_loop()

        # Teardown background workers and restore audio before destroying DPG context
        self._teardown_engine_and_timers()
        if self._tray_icon:
            try:
                self._tray_icon.stop()
            except Exception:
                pass

        dpg.destroy_context()


def launch_gui():
    app = GetsuGUI()
    app.build_ui()


if __name__ == "__main__":
    from src.single_instance import activate_existing_instance
    if activate_existing_instance():
        sys.exit(0)
    launch_gui()
