"""
Getsu Desktop GUI Application.
Features:
- Single toggle button: "START" (Green) -> "STOP" (Red)
- Minimize and Close buttons both minimize to the Windows notification area (System Tray)
- Human-friendly, clean UI with zero broken symbols or technical jargon
- No vertical scrollbars
- Auto-selects Windows default microphone on startup
- Integrated VB-Cable detection with 1-click silent install
"""
import os
import sys
import re
import threading
import time
import signal
import ctypes
from ctypes import wintypes
from typing import List, Dict, Optional

import dearpygui.dearpygui as dpg
import sounddevice as sd
from PIL import Image, ImageDraw
import pystray
from pystray import MenuItem as item, Menu
import webbrowser

import numpy as np

from src.devices import (
    get_input_devices,
    check_vbcable_status,
    auto_select_output_device,
    get_physical_output_device,
    install_vbcable_driver,
    get_all_devices,
    ensure_physical_default_playback,
    find_matching_cable_input,
)
from src.config import load_config, save_config
from src.stream import AudioEngine, create_engine_from_config
from src.router import SmartMicRouter
from src.rnnoise import RNNoise, FRAME_SIZE, SAMPLE_RATE
from src.dsp import HighPassFilter, AdaptiveNoiseGate

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

        # DWMWA_CAPTION_COLOR: 35 (Win11 COLORREF 0x00BBGGRR -> RGB(16, 20, 26) is 0x001A1410)
        caption_color = ctypes.c_int(0x001A1410)
        dwmapi.DwmSetWindowAttribute(hwnd, 35, ctypes.byref(caption_color), ctypes.sizeof(caption_color))

        # DWMWA_TEXT_COLOR: 36 (Win11 COLORREF pure white 0x00FFFFFF)
        text_color = ctypes.c_int(0x00FFFFFF)
        dwmapi.DwmSetWindowAttribute(hwnd, 36, ctypes.byref(text_color), ctypes.sizeof(text_color))

        # DWMWA_BORDER_COLOR: 34 (Win11 COLORREF RGB(40, 52, 70) is 0x00463428)
        border_color = ctypes.c_int(0x00463428)
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


def ensure_app_icon_ico():
    """Ensure getsu.ico exists with multi-resolution sizes from getsu-icon.png."""
    if os.path.exists(ICON_PNG_PATH):
        try:
            if not os.path.exists(ICON_ICO_PATH) or os.path.getmtime(ICON_PNG_PATH) > os.path.getmtime(ICON_ICO_PATH):
                img = Image.open(ICON_PNG_PATH)
                img.save(
                    ICON_ICO_PATH,
                    format='ICO',
                    sizes=[(16, 16), (20, 20), (24, 24), (32, 32), (40, 40), (48, 48), (64, 64), (128, 128), (256, 256)]
                )
        except Exception as e:
            print(f"[WARN] Failed to convert icon to ICO: {e}")


def create_tray_icon_image(is_active: bool = False) -> Image.Image:
    """Generate 64x64 icon for Windows notification area / system tray."""
    size = (64, 64)
    canvas = Image.new("RGBA", size, (0, 0, 0, 0))

    if os.path.exists(ICON_PNG_PATH):
        try:
            base_icon = Image.open(ICON_PNG_PATH).convert("RGBA")
            base_icon = base_icon.resize((50, 50), Image.Resampling.LANCZOS)
            canvas.paste(base_icon, (4, 4), mask=base_icon)
        except Exception:
            draw = ImageDraw.Draw(canvas)
            draw.ellipse((6, 6, 58, 58), fill=(20, 24, 32, 240), outline=(50, 65, 85), width=2)
    else:
        draw = ImageDraw.Draw(canvas)
        draw.ellipse((6, 6, 58, 58), fill=(20, 24, 32, 240), outline=(50, 65, 85), width=2)

    # Status indicator dot in bottom-right corner
    draw = ImageDraw.Draw(canvas)
    dot_color = (40, 215, 120, 255) if is_active else (225, 75, 75, 255)
    draw.ellipse((42, 42, 60, 60), fill=dot_color, outline=(16, 20, 26, 255), width=2)
    return canvas


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

        # Safeguard: Ensure Windows default playback device remains physical speakers/headphones
        def _check_playback_guard():
            ok = ensure_physical_default_playback()
            if ok:
                print("[INIT] Verified default Windows playback device is physical.")

        threading.Thread(target=_check_playback_guard, daemon=True).start()

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
                dev_name = sd.query_devices(self.selected_input_idx)['name'].lower()
        except Exception:
            pass
        is_laptop_mic = any(k in dev_name for k in ["realtek", "array", "built-in", "internal"])
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
                            self.set_status_pill("▲ Audio stream stalled • Restored physical mic", [240, 180, 50])
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
            self.selected_input_idx = self._get_windows_default_input()
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
                    print(f"[HARDWARE] Resuming stream on [{self.selected_input_idx}] {self.selected_input_name} -> [{self.selected_output_idx}] {self.selected_output_name}")
                    try:
                        self.engine = create_engine_from_config(
                            self.config,
                            self.selected_input_idx,
                            self.selected_output_idx,
                            router=self.router,
                        )
                        self.engine.start()
                        self.is_running = True
                        dpg.set_value("status_badge_text", "[ ACTIVE ]")
                        dpg.configure_item("status_badge_text", color=[45, 215, 115])
                        dpg.configure_item("btn_toggle", label="STOP", enabled=True)
                        dpg.bind_item_theme("btn_toggle", self.theme_stop_btn)
                        self.set_status_pill("● AI Filter Active • Clean Voice Routed", [45, 215, 115])
                        self._update_tray_icon()
                        self._set_voice_test_button_state(False)
                    except Exception as e:
                        print(f"[HARDWARE] Failed to restart stream after rescan: {e}")
                        self.is_running = False
                        self.set_status_pill(f"▲ {self._format_audio_error(e)}", [235, 75, 75])

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
            self.set_status_pill("▲ Stop noise cancellation first to switch microphone.", [240, 180, 50])
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
                print(f"[GUI] Switched microphone to: [{new_idx}] {app_data} (output: [{self.selected_output_idx}] {self.selected_output_name})")
                self.config["input_device_name"] = app_data
                self.config["input_device_id"] = new_idx
                save_config(self.config)
                self.set_status_pill(f"● Microphone selected: {clean_device_label(app_data)}", [65, 205, 130])
        finally:
            self._release_state_lock()

    def _get_idle_status(self) -> str:
        """Returns the single-line idle status string based on driver and auto_route configuration."""
        if not self.is_vbcable_installed:
            return "▲ Virtual Cable required • Please install driver"
        if self.config.get("auto_route", True):
            return "● Ready • Default Mic Routing ON"
        return "● Ready • Manual Routing Mode"

    def _get_idle_color(self) -> List[int]:
        """Returns the color for the idle status pill."""
        if not self.is_vbcable_installed:
            return [235, 180, 55]  # Warning Amber
        return [140, 155, 175]  # Muted Slate

    def set_status_pill(self, text: str, color: Optional[List[int]] = None):
        """Updates the compact single-line status pill with text and optional color."""
        if not self._ui_built or not dpg.does_item_exist("active_notice"):
            return
        dpg.set_value("active_notice", text)
        if color:
            dpg.configure_item("active_notice", color=color)

    def _format_audio_error(self, e: Exception) -> str:
        """Translates technical PortAudio / driver exceptions into clear, actionable advice."""
        err_str = str(e)
        if "WdmSyncIoctl" in err_str or "0x00000492" in err_str or "WDM-KS" in err_str:
            return "Could not start microphone (driver conflict). Please re-select your mic or restart Getsu."
        if "-9999" in err_str or "Unanticipated host error" in err_str:
            return "Could not start microphone. Ensure it is plugged in and not in exclusive use by another app."
        if "-9993" in err_str or "Illegal combination" in err_str:
            return "Audio device format mismatch. Re-selecting your microphone in the list usually fixes this."
        if any(k in err_str.lower() for k in ["busy", "access denied", "device unavailable", "in use"]):
            return "Microphone is in exclusive use by another app or disconnected."
        clean_err = err_str.replace("Error starting stream: ", "").strip()
        if len(clean_err) > 85:
            clean_err = clean_err[:82] + "..."
        return f"Could not start microphone: {clean_err}"

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
            self.set_status_pill("▲ Stop noise cancellation first to change routing preference.", [240, 180, 50])
            return

        try:
            self.config["auto_route"] = bool(app_data)
            save_config(self.config)
            print(f"[GUI] Set as Default Microphone while active: {app_data}")
            if not self.is_running:
                self.set_status_pill(self._get_idle_status(), self._get_idle_color())
        finally:
            self._release_state_lock()

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
            self.set_status_pill("▲ Microphone voice test in progress. Please wait.", [240, 180, 50])
            return

        if not self._state_lock.acquire(blocking=False):
            # Already transitioning (locked) - ignore spam clicks
            return

        if not self.is_running:
            # START FLOW
            if self.selected_input_idx is None or self.selected_input_idx < 0:
                self.set_status_pill("▲ No microphone detected. Please connect a mic.", [235, 75, 75])
                self._release_state_lock()
                return
            if self.selected_output_idx is None or self.selected_output_idx < 0:
                self.set_status_pill("▲ No audio output device detected.", [235, 75, 75])
                self._release_state_lock()
                return

            if not self.is_vbcable_installed:
                # Dynamically re-verify before showing modal in case driver was just installed
                self._refresh_devices(reinit_portaudio=True)
                if not self.is_vbcable_installed:
                    self.show_install_modal()
                    self.set_status_pill("▲ Virtual Cable required to route clean voice.", [235, 75, 75])
                    self._release_state_lock()
                    return

            # Lock UI immediately into Starting state (Crimson Red button)
            dpg.configure_item("btn_toggle", label="Starting...", enabled=False)
            dpg.bind_item_theme("btn_toggle", self.theme_stop_btn)
            dpg.set_value("status_badge_text", "[ STARTING... ]")
            dpg.configure_item("status_badge_text", color=[240, 180, 50])
            self._set_voice_test_button_state(False)
            self.set_status_pill("◌ Starting AI Filter • Routing to CABLE Output...", [240, 180, 50])

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
                    self.set_status_pill(f"▲ {self._format_audio_error(e)}", [235, 75, 75])
                finally:
                    # Enforce symmetric 3-second state lock
                    elapsed = time.monotonic() - start_time
                    remaining = max(0.0, 3.0 - elapsed)
                    if remaining > 0:
                        time.sleep(remaining)

                    if success and self.is_running:
                        dpg.set_value("status_badge_text", "[ ACTIVE ]")
                        dpg.configure_item("status_badge_text", color=[45, 215, 115])
                        dpg.configure_item("btn_toggle", label="STOP", enabled=True)
                        dpg.bind_item_theme("btn_toggle", self.theme_stop_btn)
                        if self.config.get("auto_route", True):
                            if getattr(self.engine, 'router_swap_success', True):
                                self.set_status_pill("● AI Filter Active • Clean Voice Routed", [45, 215, 115])
                            else:
                                self.set_status_pill("▲ AI Filter Active • Auto-Route Failed (Select CABLE in Apps)", [240, 180, 50])
                        else:
                            self.set_status_pill("● AI Filter Active • Manual Output Mode", [45, 215, 115])
                        self._set_voice_test_button_state(False)
                    else:
                        dpg.set_value("status_badge_text", "[ STOPPED ]")
                        dpg.configure_item("status_badge_text", color=[210, 75, 75])
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
            dpg.configure_item("status_badge_text", color=[240, 180, 50])
            self.set_status_pill("◌ Restoring physical microphone...", [240, 180, 50])

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
                    dpg.configure_item("status_badge_text", color=[210, 75, 75])
                    dpg.configure_item("btn_toggle", label="START", enabled=True)
                    dpg.bind_item_theme("btn_toggle", self.theme_start_btn)
                    self._set_voice_test_button_state(True)
                    self.set_status_pill(self._get_idle_status(), self._get_idle_color())
                    self._release_state_lock()

            threading.Thread(target=_stop_worker, daemon=True).start()

    def start_voice_test(self, sender=None, app_data=None):
        """Starts the 11-second in-memory voice preview test."""
        if not self.is_vbcable_installed:
            self.set_status_pill("▲ Virtual Cable required • Please install driver", [235, 180, 55])
            return
        if self.is_running:
            self.set_status_pill("▲ Stop noise cancellation first to run voice test.", [240, 180, 50])
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
                self.set_status_pill("▲ No microphone selected for voice test.", [235, 75, 75])
                return

            in_info = sd.query_devices(self.selected_input_idx)
            in_channels = min(2, max(1, in_info['max_input_channels']))

            # Instantiate DSP chain matching current settings
            boost_db = self.config.get("mic_boost_db", 0)
            boost_mult = 10.0 ** (boost_db / 20.0)
            dev_name = in_info['name'].lower()
            is_laptop = any(k in dev_name for k in ["realtek", "array", "built-in", "internal"])
            base_gain = 1.2 if is_laptop else 1.0
            test_gain = self.config.get("mic_gain", 1.0) * base_gain * boost_mult
            vad_th = float(self.config.get("vad_threshold", 0.70))

            rn = RNNoise()
            hpf = HighPassFilter(cutoff_hz=80.0, sample_rate=float(SAMPLE_RATE))
            gate = AdaptiveNoiseGate(
                threshold=vad_th,
                close_threshold=0.45,
                hangover_ms=self.config.get("vad_hangover_ms", 180.0),
                decay_ms=40.0,
                attack_ms=8.0,
                frame_ms=10.0,
            )

            # Phase 1: Record 11 seconds (speakers MUTED, RAM only)
            total_chunks = 1100
            chunk_size = FRAME_SIZE
            processed_chunks = []

            print(f"[VOICE TEST] Phase 1: Recording 11s from [{self.selected_input_idx}] {in_info['name']}...")
            with sd.InputStream(
                samplerate=SAMPLE_RATE,
                blocksize=chunk_size,
                device=self.selected_input_idx,
                channels=in_channels,
                dtype='float32'
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

                    # Apply gain & clamp
                    if test_gain != 1.0:
                        mono *= test_gain
                    np.clip(mono, -1.0, 1.0, out=mono)

                    # DSP
                    mono = hpf.process(mono)
                    frame_rn = mono * 32767.0
                    frame_rn, sp = rn.process_frame(frame_rn)
                    mono = (frame_rn / 32767.0) * 1.08
                    mono, _ = gate.process(mono, sp, in_place=True)
                    processed_chunks.append(mono)

                    # Update countdown UI once every 100 chunks (~1.0s)
                    if chunk_idx % 100 == 0:
                        sec_left = 11 - (chunk_idx // 100)
                        self.set_status_pill(f"◌ Voice Test: Recording to RAM... ({sec_left}s left)", [80, 195, 240])
                        dpg.configure_item("btn_voice_test", label=f"Recording... ({sec_left}s)")

            if not self._app_running or self._cancel_voice_test or not processed_chunks:
                return

            # Combine processed chunks strictly in RAM
            full_audio = np.concatenate(processed_chunks, axis=0)

            # Phase 2: Playback 11s through primary physical speakers/headphones
            out_dev = get_physical_output_device()
            out_idx = out_dev['index'] if out_dev else sd.default.device[1]
            out_info = sd.query_devices(out_idx)
            out_channels = min(2, max(1, out_info['max_output_channels']))

            print(f"[VOICE TEST] Phase 2: Playing back 11s to [{out_idx}] {out_info['name']}...")
            if out_channels == 2:
                playback_data = np.column_stack((full_audio, full_audio))
            else:
                playback_data = full_audio

            sd.play(playback_data, samplerate=SAMPLE_RATE, device=out_idx)

            # Countdown playback (11s)
            for sec in range(11, 0, -1):
                if not self._app_running or self._cancel_voice_test:
                    sd.stop()
                    break
                self.set_status_pill(f"◌ Voice Test: Playing back clean voice... ({sec}s left)", [80, 195, 240])
                dpg.configure_item("btn_voice_test", label=f"Playing back... ({sec}s)")
                time.sleep(1.0)
            sd.stop()

            # Phase 3: Immediate Purge
            del full_audio
            del playback_data
            del processed_chunks
            print("[VOICE TEST] Phase 3: RAM buffer purged. Zero disk clutter.")
            self.set_status_pill("● Voice test complete • Clean audio verified", [45, 215, 115])

        except sd.PortAudioError as pae:
            print(f"[VOICE TEST] Microphone access error: {pae}")
            self.set_status_pill("▲ Microphone busy or disconnected.", [235, 75, 75])
        except Exception as e:
            print(f"[VOICE TEST] Error during voice preview: {e}")
            self.set_status_pill(f"▲ {self._format_audio_error(e)}", [235, 75, 75])
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

    def hide_to_tray(self):
        """Hides window to Windows notification area."""
        if self._hwnd:
            user32.ShowWindow(self._hwnd, SW_HIDE)

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
            dpg.configure_item("banner_text", color=[65, 205, 130])
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
            dpg.configure_item("banner_text", color=[235, 180, 55])
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

        with dpg.font_registry():
            if path_regular and os.path.exists(path_regular):
                self.font_body = dpg.add_font(path_regular, self.s(17))
                bold_file = path_semibold if (path_semibold and os.path.exists(path_semibold)) else path_bold
                self.font_header = dpg.add_font(bold_file, self.s(18)) if (bold_file and os.path.exists(bold_file)) else self.font_body
                self.font_btn = dpg.add_font(path_bold if (path_bold and os.path.exists(path_bold)) else bold_file, self.s(20))
                # Notice text reduced by -1 as requested (from 19 to 18)
                self.font_notice = dpg.add_font(path_regular, self.s(18))
                self.font_small = dpg.add_font(path_regular, self.s(17))
                dpg.bind_font(self.font_body)
            else:
                self.font_body = None
                self.font_header = None
                self.font_btn = None
                self.font_notice = None
                self.font_small = None

        # Dark theme with DPI-scaled rounding and padding
        with dpg.theme() as global_theme:
            with dpg.theme_component(dpg.mvAll):
                dpg.add_theme_color(dpg.mvThemeCol_WindowBg, (16, 20, 26))
                dpg.add_theme_color(dpg.mvThemeCol_ChildBg, (22, 28, 36))
                dpg.add_theme_color(dpg.mvThemeCol_FrameBg, (30, 37, 48))
                dpg.add_theme_color(dpg.mvThemeCol_FrameBgHovered, (40, 50, 66))
                dpg.add_theme_style(dpg.mvStyleVar_FrameRounding, self.s(7))
                dpg.add_theme_style(dpg.mvStyleVar_WindowRounding, self.s(8))
                dpg.add_theme_style(dpg.mvStyleVar_ChildRounding, self.s(7))
                dpg.add_theme_style(dpg.mvStyleVar_ItemSpacing, self.s(10), self.s(8))
                dpg.add_theme_style(dpg.mvStyleVar_WindowPadding, self.s(14), self.s(12))

        # Start button theme (Emerald Green)
        with dpg.theme() as self.theme_start_btn:
            with dpg.theme_component(dpg.mvButton):
                dpg.add_theme_color(dpg.mvThemeCol_Button, (30, 130, 75))
                dpg.add_theme_color(dpg.mvThemeCol_ButtonHovered, (38, 160, 92))
                dpg.add_theme_color(dpg.mvThemeCol_ButtonActive, (24, 105, 60))

        # Stop button theme (Crimson Red)
        with dpg.theme() as self.theme_stop_btn:
            with dpg.theme_component(dpg.mvButton):
                dpg.add_theme_color(dpg.mvThemeCol_Button, (175, 45, 45))
                dpg.add_theme_color(dpg.mvThemeCol_ButtonHovered, (205, 55, 55))
                dpg.add_theme_color(dpg.mvThemeCol_ButtonActive, (140, 35, 35))

        # Banner install button theme (Muted Blue with Electric Blue rounded border)
        with dpg.theme() as theme_banner_btn:
            with dpg.theme_component(dpg.mvButton):
                dpg.add_theme_color(dpg.mvThemeCol_Button, (30, 55, 95))
                dpg.add_theme_color(dpg.mvThemeCol_ButtonHovered, (42, 78, 135))
                dpg.add_theme_color(dpg.mvThemeCol_ButtonActive, (24, 45, 78))
                dpg.add_theme_color(dpg.mvThemeCol_Border, (59, 130, 246, 220))
                dpg.add_theme_color(dpg.mvThemeCol_Text, (180, 215, 255))
                dpg.add_theme_style(dpg.mvStyleVar_FrameBorderSize, 1.0)
                dpg.add_theme_style(dpg.mvStyleVar_FrameRounding, self.s(6))

        # Status card child window theme (Symmetrical margins and calibrated padding)
        with dpg.theme() as theme_status_card:
            with dpg.theme_component(dpg.mvChildWindow):
                dpg.add_theme_style(dpg.mvStyleVar_WindowPadding, self.s(14), self.s(6))
                dpg.add_theme_style(dpg.mvStyleVar_ChildRounding, self.s(8))

        # Status text group theme (Zero item spacing so spacer sets exact vertical offset)
        with dpg.theme() as theme_status_text_group:
            with dpg.theme_component(dpg.mvGroup):
                dpg.add_theme_style(dpg.mvStyleVar_ItemSpacing, 0, 0)

        # Sponsor link button theme (Cyan text, transparent background)
        with dpg.theme() as theme_link_btn:
            with dpg.theme_component(dpg.mvButton):
                dpg.add_theme_color(dpg.mvThemeCol_Button, (0, 0, 0, 0))
                dpg.add_theme_color(dpg.mvThemeCol_ButtonHovered, (35, 60, 90, 130))
                dpg.add_theme_color(dpg.mvThemeCol_ButtonActive, (25, 45, 75, 180))
                dpg.add_theme_color(dpg.mvThemeCol_Text, (85, 195, 255))

        # Boost active button theme (Muted Blue with Electric Blue rounded border, like Install Driver)
        with dpg.theme() as self.theme_boost_active:
            with dpg.theme_component(dpg.mvButton):
                dpg.add_theme_color(dpg.mvThemeCol_Button, (30, 55, 95))
                dpg.add_theme_color(dpg.mvThemeCol_ButtonHovered, (42, 78, 135))
                dpg.add_theme_color(dpg.mvThemeCol_ButtonActive, (24, 45, 78))
                dpg.add_theme_color(dpg.mvThemeCol_Border, (59, 130, 246, 220))
                dpg.add_theme_color(dpg.mvThemeCol_Text, (180, 215, 255))
                dpg.add_theme_style(dpg.mvStyleVar_FrameBorderSize, 1.0)
                dpg.add_theme_style(dpg.mvStyleVar_FrameRounding, self.s(6))

        # Boost inactive button theme (Muted dark frame with subtle border)
        with dpg.theme() as self.theme_boost_inactive:
            with dpg.theme_component(dpg.mvButton):
                dpg.add_theme_color(dpg.mvThemeCol_Button, (22, 28, 38))
                dpg.add_theme_color(dpg.mvThemeCol_ButtonHovered, (32, 42, 58))
                dpg.add_theme_color(dpg.mvThemeCol_ButtonActive, (18, 24, 32))
                dpg.add_theme_color(dpg.mvThemeCol_Border, (40, 50, 68, 160))
                dpg.add_theme_color(dpg.mvThemeCol_Text, (130, 145, 165))
                dpg.add_theme_style(dpg.mvStyleVar_FrameBorderSize, 1.0)
                dpg.add_theme_style(dpg.mvStyleVar_FrameRounding, self.s(6))

        # Voice test button theme (Option A: Electric Ocean Blue - Available)
        with dpg.theme() as self.theme_voice_btn:
            with dpg.theme_component(dpg.mvButton):
                dpg.add_theme_color(dpg.mvThemeCol_Button, (35, 78, 130))
                dpg.add_theme_color(dpg.mvThemeCol_ButtonHovered, (48, 105, 175))
                dpg.add_theme_color(dpg.mvThemeCol_ButtonActive, (28, 62, 105))
                dpg.add_theme_color(dpg.mvThemeCol_Text, (240, 248, 255))
                dpg.add_theme_style(dpg.mvStyleVar_FrameRounding, self.s(6))

        # Voice test button theme (Sunken Charcoal - Unavailable while active, dead on hover)
        with dpg.theme() as self.theme_voice_btn_disabled:
            for state in (True, False):
                with dpg.theme_component(dpg.mvButton, enabled_state=state):
                    dpg.add_theme_color(dpg.mvThemeCol_Button, (20, 24, 30))
                    dpg.add_theme_color(dpg.mvThemeCol_ButtonHovered, (20, 24, 30))
                    dpg.add_theme_color(dpg.mvThemeCol_ButtonActive, (20, 24, 30))
                    dpg.add_theme_color(dpg.mvThemeCol_Text, (85, 95, 110))
                    dpg.add_theme_color(dpg.mvThemeCol_TextDisabled, (85, 95, 110))
                    dpg.add_theme_style(dpg.mvStyleVar_FrameRounding, self.s(6))

        # Unified Modal theme (Unified dark background, larger title padding, red close button)
        with dpg.theme() as theme_modal:
            with dpg.theme_component(dpg.mvAll):
                dpg.add_theme_color(dpg.mvThemeCol_WindowBg, (16, 20, 26))
                dpg.add_theme_color(dpg.mvThemeCol_TitleBg, (16, 20, 26))
                dpg.add_theme_color(dpg.mvThemeCol_TitleBgActive, (16, 20, 26))
                dpg.add_theme_color(dpg.mvThemeCol_TitleBgCollapsed, (16, 20, 26))
                dpg.add_theme_color(dpg.mvThemeCol_Border, (40, 52, 70))
                dpg.add_theme_style(dpg.mvStyleVar_FramePadding, self.s(10), self.s(10))
                dpg.add_theme_style(dpg.mvStyleVar_WindowPadding, self.s(18), self.s(16))
                dpg.add_theme_style(dpg.mvStyleVar_WindowRounding, self.s(8))
                dpg.add_theme_color(dpg.mvThemeCol_Button, (175, 45, 45))
                dpg.add_theme_color(dpg.mvThemeCol_ButtonHovered, (215, 55, 55))
                dpg.add_theme_color(dpg.mvThemeCol_ButtonActive, (140, 35, 35))
            with dpg.theme_component(dpg.mvButton):
                dpg.add_theme_color(dpg.mvThemeCol_Button, (175, 45, 45))
                dpg.add_theme_color(dpg.mvThemeCol_ButtonHovered, (215, 55, 55))
                dpg.add_theme_color(dpg.mvThemeCol_ButtonActive, (140, 35, 35))

        # Modal secondary/cancel button theme (Subtle dark frame)
        with dpg.theme() as theme_secondary_btn:
            with dpg.theme_component(dpg.mvButton):
                dpg.add_theme_color(dpg.mvThemeCol_Button, (28, 36, 48))
                dpg.add_theme_color(dpg.mvThemeCol_ButtonHovered, (38, 48, 64))
                dpg.add_theme_color(dpg.mvThemeCol_ButtonActive, (22, 28, 38))
                dpg.add_theme_color(dpg.mvThemeCol_Border, (48, 60, 80))
                dpg.add_theme_color(dpg.mvThemeCol_Text, (180, 195, 215))
                dpg.add_theme_style(dpg.mvStyleVar_FrameBorderSize, 1.0)
                dpg.add_theme_style(dpg.mvStyleVar_FrameRounding, self.s(6))

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
                        dpg.add_text("GETSU - AI NOISE CANCELLATION", color=[255, 255, 255], tag="header_title_text")
                    dpg.add_text("[ STOPPED ]", tag="status_badge_text", color=[210, 75, 75])

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
                                color=[65, 205, 130]
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
            dpg.add_text("Microphone Input:", color=[180, 195, 215])
            combo_items = list(self.device_map.keys())
            dpg.add_combo(
                items=combo_items,
                default_value=self._get_selected_input_label(),
                callback=self.on_input_changed,
                tag="input_combo",
                width=-1
            )

            dpg.add_spacer(height=self.s(4))

            # --- MICROPHONE BOOST (0, 5, 10, 15 dB) ---
            dpg.add_text("Microphone Boost:", color=[180, 195, 215])
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
            dpg.add_text("Voice Sensitivity Threshold:", color=[180, 195, 215])
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
                dpg.add_text("Created by Ihsan  |  Sponsored by", color=[120, 135, 155], tag="footer_creator_text")
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
            if self.font_btn:
                dpg.bind_item_font("btn_toggle", self.font_btn)
                dpg.bind_item_font("btn_voice_test", self.font_body if self.font_body else self.font_btn)
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
                    color=[215, 225, 240]
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
                dpg.add_text("Just click START. Most apps will switch to clean\naudio automatically.", color=[215, 225, 240])
                dpg.add_spacer(height=self.s(6))

                dpg.add_text("Notes: Only When Needed (Games & Voice Apps)", color=[85, 175, 255])
                dpg.add_text(
                    "If an app or game doesn't pick up the noise filter,\ngo to its audio settings and choose:",
                    color=[180, 195, 215]
                )
                dpg.add_spacer(height=self.s(2))
                dpg.add_text("Microphone  ->  CABLE Output", color=[65, 205, 130])
                dpg.add_spacer(height=self.s(2))
                dpg.add_text("(This guarantees clean voice, no restart needed.)", color=[140, 155, 175])

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
            auto_fit_h = int(rect_max[1] + self.s(16)) + diff
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
        dpg.start_dearpygui()

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
