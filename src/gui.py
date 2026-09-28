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
import ctypes
from ctypes import wintypes
from typing import List, Dict, Optional

import dearpygui.dearpygui as dpg
import sounddevice as sd
from PIL import Image, ImageDraw
import pystray
from pystray import MenuItem as item, Menu
import webbrowser

from src.devices import (
    get_input_devices,
    check_vbcable_status,
    auto_select_output_device,
    install_vbcable_driver,
    get_all_devices,
)
from src.config import load_config, save_config
from src.stream import AudioEngine, create_engine_from_config

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


def clean_device_label(name: str) -> str:
    """Format raw audio device name into a clean, human-friendly label."""
    cleaned = name
    for tag in ["(Windows WASAPI)", "(Windows DirectSound)", "(MME)", "(Windows WDM-KS)"]:
        cleaned = cleaned.replace(tag, "")
    # Clean up prefixes like '2- ' or '1- '
    cleaned = re.sub(r'\(\s*\d+-\s*', '(', cleaned)
    return cleaned.strip()


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
        self.mic_boost_db = int(self.config.get("mic_boost_db", 0))
        self.engine: Optional[AudioEngine] = None
        self.is_running = False

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
        """Monitors stream health every 1.0s and triggers debounced hotplug recovery if audio stalls."""
        while self._app_running:
            time.sleep(1.0)
            if self.is_running and self.engine and not self._is_rescanning:
                if not self.engine.is_stream_alive():
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

        self.is_vbcable_installed, cable_in, _ = check_vbcable_status()
        if self.is_vbcable_installed and cable_in:
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
                if was_running:
                    print(f"[HARDWARE] Resuming stream on [{self.selected_input_idx}] {self.selected_input_name} -> [{self.selected_output_idx}] {self.selected_output_name}")
                    self.toggle_stream()

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
        """Handles user selecting a different microphone from dropdown."""
        if app_data in self.device_map:
            new_idx = self.device_map[app_data]
            self.selected_input_idx = new_idx
            self.selected_input_name = app_data
            self.config["input_device_name"] = app_data
            self.config["input_device_id"] = new_idx
            save_config(self.config)
            print(f"[GUI] Switched microphone to: [{new_idx}] {app_data}")
            if self.is_running and self.engine:
                self.engine.stop()
                self.engine = None
                self.is_running = False
                self.toggle_stream()

    def toggle_stream(self, sender=None, app_data=None):
        """Single toggle button handling Start and Stop."""
        if not self.is_running:
            # START
            if self.selected_input_idx is None or self.selected_input_idx < 0:
                dpg.set_value("active_notice", "No microphone detected. Please connect a microphone.")
                return
            if self.selected_output_idx is None or self.selected_output_idx < 0:
                dpg.set_value("active_notice", "No audio output device detected.")
                return
            try:
                self.engine = create_engine_from_config(
                    self.config,
                    self.selected_input_idx,
                    self.selected_output_idx,
                )
                self.engine.start()
                self.is_running = True

                self.config["input_device_id"] = self.selected_input_idx
                self.config["input_device_name"] = self.selected_input_name
                self.config["output_device_id"] = self.selected_output_idx
                self.config["output_device_name"] = self.selected_output_name
                save_config(self.config)

                # Update UI to active state
                dpg.set_value("status_badge_text", "[ ACTIVE ]")
                dpg.configure_item("status_badge_text", color=[45, 215, 115])
                dpg.configure_item("btn_toggle", label="STOP")
                dpg.bind_item_theme("btn_toggle", self.theme_stop_btn)
                dpg.set_value("active_notice", "AI Filter Active • Clean voice is streaming to your calls.\nYou can now minimize Getsu to the system tray.")
                self._update_tray_icon()
            except Exception as e:
                print(f"[ERROR] Failed to start stream: {e}")
                if self.engine:
                    try:
                        self.engine.close()
                    except Exception:
                        pass
                    self.engine = None
                self.is_running = False
                dpg.set_value("active_notice", f"Error: {e}")
        else:
            # STOP
            if self.engine:
                self.engine.stop()
                self.engine = None
            self.is_running = False

            # Update UI to stopped state
            dpg.set_value("status_badge_text", "[ STOPPED ]")
            dpg.configure_item("status_badge_text", color=[210, 75, 75])
            dpg.configure_item("btn_toggle", label="START")
            dpg.bind_item_theme("btn_toggle", self.theme_start_btn)
            dpg.set_value("active_notice", "Click START, then select CABLE Output (VB-Audio Virtual Cable) as mic in Steam, Zoom, or Discord.")
            self._update_tray_icon()

    def restore_window(self):
        """Restores window from notification area / system tray."""
        if self._hwnd:
            user32.ShowWindow(self._hwnd, SW_RESTORE)
            user32.SetForegroundWindow(self._hwnd)

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
            time.sleep(2.5)
            self._refresh_devices()

            dpg.hide_item("install_spinner")
            dpg.hide_item("modal_buttons_row")
            dpg.show_item("btn_install_done")

            if self.is_vbcable_installed:
                dpg.set_value(
                    "install_status_text",
                    "VB-Audio Virtual Cable Installed & Ready!\n\n"
                    "No restart required. Denoised audio is now ready\n"
                    "for Steam, Zoom, and Discord."
                )
            elif success:
                dpg.set_value(
                    "install_status_text",
                    "Driver Setup Completed!\n\n"
                    "If the device does not appear immediately,\n"
                    "a quick Windows restart will finalize it."
                )
            else:
                dpg.set_value(
                    "install_status_text",
                    "Installation was cancelled or failed.\n\n"
                    "You can continue using Headphone Monitor Mode."
                )

        threading.Thread(target=worker, daemon=True).start()

    def on_install_done_click(self, sender, app_data):
        dpg.hide_item("modal_vbcable")
        self._refresh_devices()
        self._update_cable_banner()

    def on_install_skip_click(self, sender, app_data):
        dpg.hide_item("modal_vbcable")
        self._update_cable_banner()

    def show_install_modal(self, sender=None, app_data=None):
        if self.is_vbcable_installed:
            return
        dpg.show_item("modal_buttons_row")
        dpg.configure_item("btn_install_confirm", enabled=True)
        dpg.configure_item("btn_install_cancel", enabled=True)
        dpg.hide_item("btn_install_done")
        dpg.hide_item("install_spinner")
        dpg.set_value(
            "install_status_text",
            "To send clean, noise-free audio into Steam Voice,\n"
            "Zoom, or Discord, VB-Audio Cable is needed.\n\n"
            "Do you want me to install this now?"
        )
        dpg.show_item("modal_vbcable")

    def _update_cable_banner(self):
        """Updates the status card."""
        if self.is_vbcable_installed:
            dpg.set_value("banner_text", "Virtual Mic: Ready for Steam, Zoom, & Discord")
            dpg.configure_item("banner_text", color=[65, 205, 130])
            dpg.hide_item("banner_install_btn")
            dpg.configure_item("banner_install_btn", enabled=False)
        else:
            dpg.set_value("banner_text", "Headphone Monitor Mode (VB-Cable not installed)")
            dpg.configure_item("banner_text", color=[235, 180, 55])
            dpg.show_item("banner_install_btn")
            dpg.configure_item("banner_install_btn", enabled=True)

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
            elif WM_GETSU_RESTORE and msg == WM_GETSU_RESTORE:
                self.restore_window()
                return 0
            elif msg == WM_DEVICECHANGE:
                self._on_device_change_event()
                return 1
            elif msg == 0x0082:  # WM_NCDESTROY
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
            self._app_running = False
            if self.engine:
                self.engine.stop()
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

        # Banner install button theme (Muted Blue)
        with dpg.theme() as theme_banner_btn:
            with dpg.theme_component(dpg.mvButton):
                dpg.add_theme_color(dpg.mvThemeCol_Button, (40, 70, 110))
                dpg.add_theme_color(dpg.mvThemeCol_ButtonHovered, (55, 95, 145))
                dpg.add_theme_color(dpg.mvThemeCol_ButtonActive, (30, 55, 85))

        # Sponsor link button theme (Cyan text, transparent background)
        with dpg.theme() as theme_link_btn:
            with dpg.theme_component(dpg.mvButton):
                dpg.add_theme_color(dpg.mvThemeCol_Button, (0, 0, 0, 0))
                dpg.add_theme_color(dpg.mvThemeCol_ButtonHovered, (35, 60, 90, 130))
                dpg.add_theme_color(dpg.mvThemeCol_ButtonActive, (25, 45, 75, 180))
                dpg.add_theme_color(dpg.mvThemeCol_Text, (85, 195, 255))

        # Boost active button theme (Emerald Green)
        with dpg.theme() as self.theme_boost_active:
            with dpg.theme_component(dpg.mvButton):
                dpg.add_theme_color(dpg.mvThemeCol_Button, (30, 125, 75))
                dpg.add_theme_color(dpg.mvThemeCol_ButtonHovered, (38, 155, 92))
                dpg.add_theme_color(dpg.mvThemeCol_ButtonActive, (24, 100, 60))
                dpg.add_theme_color(dpg.mvThemeCol_Text, (255, 255, 255))
                dpg.add_theme_style(dpg.mvStyleVar_FrameRounding, self.s(6))

        # Boost inactive button theme (Muted Dark Frame)
        with dpg.theme() as self.theme_boost_inactive:
            with dpg.theme_component(dpg.mvButton):
                dpg.add_theme_color(dpg.mvThemeCol_Button, (25, 32, 42))
                dpg.add_theme_color(dpg.mvThemeCol_ButtonHovered, (35, 45, 60))
                dpg.add_theme_color(dpg.mvThemeCol_ButtonActive, (20, 26, 35))
                dpg.add_theme_color(dpg.mvThemeCol_Text, (140, 155, 175))
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
            with dpg.child_window(width=-1, height=self.s(52), border=True, no_scrollbar=True):
                with dpg.group(horizontal=True):
                    dpg.add_text(
                        "Virtual Mic Status",
                        tag="banner_text",
                        color=[65, 205, 130]
                    )
                    dpg.add_spacer(width=self.s(20))
                    dpg.add_button(
                        label="Install Driver",
                        callback=self.show_install_modal,
                        tag="banner_install_btn",
                        width=self.s(110),
                        height=self.s(32),
                        show=False
                    )
            dpg.bind_item_theme("banner_install_btn", theme_banner_btn)

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

            dpg.add_spacer(height=self.s(6))

            # --- SINGLE ACTION BUTTON: START / STOP ---
            dpg.add_button(
                label="START",
                callback=self.toggle_stream,
                tag="btn_toggle",
                width=-1,
                height=self.s(54)
            )
            dpg.bind_item_theme("btn_toggle", self.theme_start_btn)

            dpg.add_spacer(height=self.s(6))

            # --- CLEAN INSTRUCTION ---
            dpg.add_text(
                "Click START, then select CABLE Output (VB-Audio Virtual Cable) as mic in Steam, Zoom, or Discord.",
                tag="active_notice",
                color=[150, 165, 185],
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
            if self.font_notice:
                dpg.bind_item_font("active_notice", self.font_notice)
            if self.font_small:
                dpg.bind_item_font("footer_creator_text", self.font_small)
                dpg.bind_item_font("sponsor_link_btn", self.font_small)

            # Initialize boost button states
            self._update_boost_buttons_ui()

            # --- MODAL: VB-CABLE SETUP ---
            with dpg.window(
                label="Virtual Audio Cable Setup",
                modal=True,
                show=False,
                tag="modal_vbcable",
                no_resize=True,
                no_move=True,
                width=self.s(450),
                height=self.s(240),
                pos=[self.s(25), self.s(60)]
            ):
                dpg.add_text("VB-Audio Virtual Cable Required", color=[235, 180, 55])
                dpg.add_separator()
                dpg.add_spacer(height=self.s(4))
                dpg.add_text(
                    "To send clean audio into Steam Voice, Zoom, or Discord,\n"
                    "VB-Audio Virtual Cable is needed.\n\n"
                    "Do you want me to install this now?",
                    tag="install_status_text"
                )
                dpg.add_spacer(height=self.s(6))
                dpg.add_loading_indicator(tag="install_spinner", show=False, radius=float(self.s(3)))
                dpg.add_spacer(height=self.s(6))

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

                dpg.add_button(
                    label="OK",
                    callback=self.on_install_done_click,
                    tag="btn_install_done",
                    show=False,
                    width=self.s(130),
                    height=self.s(34)
                )

        # Viewport configuration - auto-adapts height on first render
        dpg.create_viewport(
            title="Getsu - AI Noise Cancellation",
            small_icon=ICON_ICO_PATH if os.path.exists(ICON_ICO_PATH) else "",
            large_icon=ICON_ICO_PATH if os.path.exists(ICON_ICO_PATH) else "",
            width=self.s(520),
            height=self.s(450),
            resizable=False,
            decorated=True
        )
        dpg.setup_dearpygui()
        dpg.show_viewport()
        dpg.set_primary_window("main_window", True)

        # Render 3 layout frames to settle font glyphs, then dynamically auto-fit viewport height
        dpg.render_dearpygui_frame()
        dpg.render_dearpygui_frame()
        dpg.render_dearpygui_frame()
        try:
            rect_max = dpg.get_item_rect_max("footer_sponsor_row")
            diff = dpg.get_viewport_height() - dpg.get_viewport_client_height()
            auto_fit_h = int(rect_max[1] + self.s(16)) + diff
            dpg.set_viewport_height(auto_fit_h)
            dpg.render_dearpygui_frame()
        except Exception:
            pass

        self._update_cable_banner()

        if not self.is_vbcable_installed:
            dpg.show_item("modal_vbcable")

        # Start tray icon & window hook
        self._start_tray_icon()
        threading.Thread(target=self._setup_window_hook, daemon=True).start()

        # Run UI event loop
        dpg.start_dearpygui()
        dpg.destroy_context()

        # Cleanup
        self._app_running = False
        if self._device_change_timer and self._device_change_timer.is_alive():
            self._device_change_timer.cancel()
        if self._tray_icon:
            self._tray_icon.stop()
        if self.engine:
            self.engine.stop()


def launch_gui():
    app = GetsuGUI()
    app.build_ui()


if __name__ == "__main__":
    from src.single_instance import activate_existing_instance
    if activate_existing_instance():
        sys.exit(0)
    launch_gui()
