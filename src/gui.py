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
from src.router import SmartMicRouter, clear_device_bus_type_cache
from src.devices import classify_device, DeviceKind
from src.device_labels import split_name, subtitle
from src import ui_icons as icons
from src import ui_hit
from src import win_theme
from src import autostart
from src.rnnoise import RNNoise, FRAME_SIZE, SAMPLE_RATE
from src.dsp import (
    HighPassFilter,
    AdaptiveNoiseGate,
    SpeechLeveler,
    TransientSuppressor,
    process_mono_frame,
    InputDownmixer,
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

# Window geometry (logical pixels, scaled with self.s()): 420 px wide, 24 px side gutters.
WINDOW_W = 420
CONTENT_PAD = 24
CONTENT_W = WINDOW_W - 2 * CONTENT_PAD  # 372 px between the gutters
HERO_H = 212  # logical px: hero block and first-run card share this slot (the hero content ends at the meter)
SETTINGS_ROW_H = 64  # logical px: one settings row; a two-line detail fits
SET_LABEL_W = 210    # label column of a settings row (the detail wraps inside it)
SET_CTRL_W = 146     # control column of a settings row: slider, boost segments, switch
VAD_MIN, VAD_MAX = 0.40, 0.90  # Sensitivity range (vad_threshold)
BOOST_DBS = (0, 5, 10, 15)     # mic boost segments, left to right

# First-run card copy (mockup "First run" and "Installing cable" screens).
SETUP_BODY_OFFER = ("Getsu needs a free virtual audio cable to send your clean voice to Discord, Zoom and games. "
                    "It takes about a minute.")
SETUP_BODY_INSTALLING = "Windows may ask for permission. Click Yes. This window updates by itself."

# Features whose backend lands in a later task. Their controls are built but hidden.
FEATURE_MODES = False          # Game / Meeting / Session / Music segments (backend: M2.4)
FEATURE_EARBUD_HINT = False    # "Earbud mic drops audio to call quality." (earbud work: M5.x)
FEATURE_LEARN_ROOM = False     # "Learn my room" row in Settings (backend: M2.3)

# Icon shape for each device kind: bt = earbuds, headset, builtin = laptop, mic = microphone.
# The kind comes from devices.classify_device(); usb_mic uses the microphone, wired headsets the headset.
_GLYPH_FOR_KIND = {
    DeviceKind.BLUETOOTH_HEADSET: "bt",
    DeviceKind.USB_HEADSET: "headset",
    DeviceKind.WIRED_HEADSET: "headset",
    DeviceKind.BUILT_IN: "builtin",
    DeviceKind.USB_MIC: "mic",
    DeviceKind.LINE_IN: "mic",
    DeviceKind.OTHER: "mic",
}

MODE_NAMES = ("Game", "Meeting", "Session", "Music")
MODE_DESCRIPTIONS = {
    "Game": "Lowest delay, strong click removal",
    "Meeting": "Steady voice level for long calls",
    "Session": "Gentle, never cuts quiet speech",
    "Music": "No noise filter, natural sound",
}


class _Switch:
    """Drawn on/off switch, 42 x 26 px: accent track and white knob when on, fill track when off."""

    def __init__(self, gui, name: str, on: bool, callback):
        self.gui = gui
        self.name = name
        self.tag = f"switch_{name}"
        self.on = bool(on)
        self.callback = callback

    def build(self, parent: str):
        dpg.add_drawlist(width=self.gui.s(42), height=self.gui.s(26), tag=self.tag, parent=parent)
        self.draw()

    def draw(self):
        if not dpg.does_item_exist(self.tag):
            return
        s = self.gui.s
        dpg.delete_item(self.tag, children_only=True)
        track = COL_ACCENT if self.on else COL_FILL_2
        dpg.draw_rectangle((0, 0), (s(42), s(26)), color=track, fill=track, rounding=s(13), parent=self.tag)
        knob_r = s(11)
        cx = s(42) - s(2) - knob_r if self.on else s(2) + knob_r
        dpg.draw_circle((cx, s(13)), knob_r, color=COL_WHITE, fill=COL_WHITE, parent=self.tag)

    def set(self, on: bool):
        self.on = bool(on)
        self.draw()

    def toggle(self):
        self.set(not self.on)
        if self.callback:
            self.callback(self.on)


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
    def __init__(self, minimized: bool = False):
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
        # --minimized (Start with Windows): no window at launch; resume noise cancellation if it was on at exit.
        self._start_minimized = bool(minimized)
        self._resume_pending = autostart.should_resume_filter(
            self._start_minimized, bool(self.config.get("was_running_on_exit", False)))
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
        self.device_raw: Dict[str, str] = {}      # display label -> full Windows endpoint name
        self.selected_input_name: Optional[str] = self.config.get("input_device_name")
        self.selected_output_name: Optional[str] = self.config.get("output_device_name")
        self.selected_input_idx: Optional[int] = self.config.get("input_device_id")
        self.selected_output_idx: Optional[int] = self.config.get("output_device_id")
        self.is_vbcable_installed = False
        self._ui_built = False
        self._app_running = True
        self._is_rescanning = False

        # Phase B UI state. _busy is "starting" / "stopping" while the 3 s lock runs; _notice is (text, colour)
        # shown on the hero sub line in place of the default sentence.
        self._busy: Optional[str] = None
        self._notice = None
        self._switches: Dict[str, "_Switch"] = {}
        self._click_targets: Dict[str, object] = {}     # main window controls: tag -> callback
        self._panel_targets: Dict[str, Dict[str, object]] = {}  # per panel: rows and switches inside its sheet
        self._hit_parts: Dict[str, list] = {}  # control tag -> leaf drawlists that measure it (see _item_rect)
        self._open_panel_name: Optional[str] = None
        self._btn_state: Dict[str, tuple] = {}   # icon button tag -> (enabled, hovered), drives theme and icon colour
        self._btn_enabled: Dict[str, bool] = {}  # icon button tag -> enabled (clicks are ignored when False)
        self._btn_glyphs: Dict[str, str] = {}    # icon button tag -> glyph name (registry of icon buttons)
        self._btn_heights: Dict[str, int] = {}
        self._btn_hovered: Dict[str, bool] = {}
        self._setup_dismissed = False            # "Not now" hides the first-run card for this session
        self._install_state: Optional[str] = None  # None, "installing" or "failed" (the card's own states)
        self._install_error = ""
        self._card_sig = None
        self._setup_anim_t = 0.0
        self._sub_full_text = ""
        self._vt_active = False
        self._vt_progress = 0.0
        self._meter_phase = 0.0
        self._meter_level = 0.0
        self._meter_flat = True
        self._last_meter_t = 0.0
        self._work_h = 0
        self._mode = "Game"
        self.theme_flat = None
        self.theme_card = None
        self.theme_list = None
        self.theme_sheet = None
        self.theme_chip = None
        self.theme_scrim = None
        self._vad_value = 0.70                # Sensitivity knob value (set when the settings row is built)
        self._drag: Optional[str] = None      # "vad" while the Sensitivity knob follows the mouse
        self._slider_hovered = False
        self._seg_hover: Optional[int] = None  # index of the hovered boost segment

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
        self._persist_was_running(
            (self.is_running and self._busy != "stopping") or self._resume_pending)
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

    def _persist_was_running(self, running: bool):
        """Stores whether noise cancellation is on, for the next Start with Windows launch. Never raises."""
        try:
            self.config["was_running_on_exit"] = bool(running)
            save_config(self.config)
        except Exception as e:
            print(f"[WARN] Could not store the running state: {e}")

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
        self._draw_boost()
        if self.engine:
            new_gain = self._compute_current_gain()
            self.engine.mic_gain = new_gain
            self.engine.mic_boost_db = float(boost_db)
            print(f"[GUI] Live Microphone Gain updated to: {new_gain:.2f}x (+{boost_db} dB)")

    # --- Sensitivity knob and mic boost segments (drawn in drawlists; clicks come from _on_mouse_click) ---

    def _draw_slider(self):
        """Redraws the Sensitivity knob: 4 px track, accent fill up to the value, white knob with a soft shadow."""
        tag = "slider_vad"
        if not dpg.does_item_exist(tag):
            return
        s = self.s
        h = 30
        cy = h / 2.0
        track_h = 2.0
        x_knob = ui_hit.slider_knob_x(self._vad_value, 0, s(SET_CTRL_W), s(15), VAD_MIN, VAD_MAX)
        dpg.delete_item(tag, children_only=True)
        dpg.draw_rectangle((s(15), s(cy - track_h)), (s(SET_CTRL_W - 15), s(cy + track_h)),
                           color=COL_FILL_2, fill=COL_FILL_2, rounding=s(2), parent=tag)
        dpg.draw_rectangle((s(15), s(cy - track_h)), (max(s(15), x_knob), s(cy + track_h)),
                           color=COL_ACCENT, fill=COL_ACCENT, rounding=s(2), parent=tag)
        if self._slider_hovered:
            dpg.draw_circle((x_knob, s(cy)), s(15), color=COL_ACCENT_SOFT, fill=COL_ACCENT_SOFT, parent=tag)
        dpg.draw_circle((x_knob, s(cy + 1)), s(11), color=(0, 0, 0, 80), fill=(0, 0, 0, 80), parent=tag)
        dpg.draw_circle((x_knob, s(cy)), s(11), color=COL_WHITE, fill=COL_WHITE, parent=tag)

    def _draw_boost(self):
        """Redraws the mic boost track: selected segment is an accent pill with white text, hover tints the others."""
        if not self._ui_built or not dpg.does_item_exist("seg_boost_bg"):
            return
        s = self.s
        w, h, inset, gap = SET_CTRL_W, 30, 2, 2
        seg_w = (w - 2 * inset - gap * (len(BOOST_DBS) - 1)) / float(len(BOOST_DBS))
        dpg.delete_item("seg_boost_bg", children_only=True)
        dpg.draw_rectangle((0, 0), (s(w), s(h)), color=COL_FILL, fill=COL_FILL, rounding=s(9), parent="seg_boost_bg")
        for i, db in enumerate(BOOST_DBS):
            x0 = inset + i * (seg_w + gap)
            selected = db == self.mic_boost_db
            if selected:
                fill = COL_ACCENT
            elif self._seg_hover == i:
                fill = COL_FILL_2
            else:
                continue
            dpg.draw_rectangle((s(x0), s(inset)), (s(x0 + seg_w), s(h - inset)), color=fill, fill=fill,
                               rounding=s(7), parent="seg_boost_bg")
        for db in BOOST_DBS:
            if dpg.does_item_exist(f"seg_lbl_{db}"):
                dpg.configure_item(f"seg_lbl_{db}", color=COL_WHITE if db == self.mic_boost_db else COL_TEXT)

    def _vad_from_x(self, x: float, rect) -> float:
        return ui_hit.slider_value_at(x, rect[0][0], rect[1][0] - rect[0][0], self.s(15), VAD_MIN, VAD_MAX)

    def _set_vad(self, value: float):
        """Applies a Sensitivity value from the knob. Same path as before: on_vad_slider_changed debounces the save."""
        value = round(float(value), 2)
        if value == self._vad_value:
            return
        self._vad_value = value
        self._draw_slider()
        self.on_vad_slider_changed(None, value)

    def _slider_press(self):
        """Click on the Sensitivity track: jump to that value and start a drag."""
        rect = self._item_rect("slider_vad")
        if rect is None:
            return
        self._drag = "vad"
        self._set_vad(self._vad_from_x(dpg.get_mouse_pos(local=False)[0], rect))

    def _boost_press(self):
        """Click on the boost track: select the segment under the pointer."""
        rect = self._item_rect("seg_boost")
        idx = ui_hit.segment_index(dpg.get_mouse_pos(local=False), rect, len(BOOST_DBS), self.s(2))
        if idx is not None:
            self.set_mic_boost(BOOST_DBS[idx])

    def _tick_controls(self):
        """Per frame: follows the mouse while the Sensitivity knob is dragged, and updates hover highlights."""
        if not self._ui_built:
            return
        point = dpg.get_mouse_pos(local=False)
        settings_open = self._open_panel_name == "settings"
        if self._drag == "vad":
            rect = self._item_rect("slider_vad")
            if not settings_open or rect is None or not dpg.is_mouse_button_down(dpg.mvMouseButton_Left):
                self._drag = None
            else:
                self._set_vad(self._vad_from_x(point[0], rect))
        hovered = settings_open and ui_hit.point_in_rect(point, self._item_rect("slider_vad") or ((0, 0), (0, 0)))
        if hovered != self._slider_hovered:
            self._slider_hovered = hovered
            self._draw_slider()
        seg_hover = (ui_hit.segment_index(point, self._item_rect("seg_boost"), len(BOOST_DBS), self.s(2))
                     if settings_open else None)
        if seg_hover != self._seg_hover:
            self._seg_hover = seg_hover
            self._draw_boost()

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
        clear_device_bus_type_cache()
        if reinit_portaudio and not self.is_running:
            try:
                sd._terminate()
                sd._initialize()
            except Exception:
                pass

        self.input_devices = get_input_devices()
        self.device_map = {}
        self.device_raw = {}
        for d in self.input_devices:
            label = clean_device_label(d['name'])
            # Avoid duplicate keys if multiple devices have similar names
            if label in self.device_map:
                label = f"{label} #{d['index']}"
            self.device_map[label] = d['index']
            self.device_raw[label] = d['name']

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

        if self._ui_built:
            self._update_selection_hint(self._get_selected_input_label())
            self._refresh_mic_widgets()


    def _on_device_change_event(self):
        """Debounced listener for Windows WM_DEVICECHANGE hotplug signals."""
        clear_device_bus_type_cache()
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
        clear_device_bus_type_cache()
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

                # Update the microphone row and the microphone panel
                self._refresh_mic_widgets()

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
                            self._render_hero()
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
                            self._render_hero()
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
            # The selection stays on the active microphone and the user is told why.
            current_label = self._get_selected_input_label()
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


    def _get_idle_status(self) -> Optional[str]:
        """Notice shown while stopped. None means no notice: the hero shows its own sentence."""
        if not self.is_vbcable_installed:
            return "Virtual cable not installed"
        return None

    def _get_idle_color(self) -> Sequence[int]:
        """Colour of the idle notice."""
        if not self.is_vbcable_installed:
            return COL_WARNING
        return COL_TEXT_2

    def set_status_pill(self, text: Optional[str], color: Optional[Sequence[int]] = None):
        """
        Shows a notice on the hero sub line (replaces the old status pill). text None clears the notice.
        The notice colour defaults to secondary text.
        """
        self._notice = (text, tuple(color) if color else COL_TEXT_2) if text else None
        self._render_hero()

    def _format_audio_error(self, e: Exception) -> str:
        """Translates technical PortAudio / driver exceptions into clear, actionable advice."""
        explained = explain_start_error(e)
        print(f"[AUDIO] Stream error [{explained.kind}] code={explained.code}: {explained.technical}")
        return f"{explained.title}. {explained.advice}"


    def on_auto_route_toggled(self, sender, app_data):
        """Handles user toggling default microphone switch setting (Option B: locked while active)."""
        if self.is_running or self._is_testing_voice or not self._state_lock.acquire(blocking=False):
            # Option B: Lock auto-route preference while active/testing.
            # Revert checkbox value back to saved config setting and warn user.
            current_setting = self.config.get("auto_route", True)
            self._switch_set("auto_route", current_setting)
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
                # Dynamically re-verify before showing the setup card in case the driver was just installed
                self._refresh_devices(reinit_portaudio=True)
                if not self.is_vbcable_installed:
                    self.show_setup_card()
                    self.set_status_pill("Virtual Cable required to route clean voice.", COL_ERROR)
                    self._release_state_lock()
                    return

            # Lock the UI into the Starting state (power button ring, "Starting" line)
            self._busy = "starting"
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
                        self._refresh_mic_widgets()

                    self.config["input_device_id"] = self.selected_input_idx
                    self.config["input_device_name"] = self.selected_input_name
                    self.config["output_device_id"] = self.selected_output_idx
                    self.config["output_device_name"] = self.selected_output_name
                    self.config["was_running_on_exit"] = True
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

                    self._busy = None
                    if success and self.is_running:
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
                        self._render_hero()
                        self._set_voice_test_button_state(True)

                    self._release_state_lock()

            threading.Thread(target=_start_worker, daemon=True).start()

        else:
            # STOP FLOW
            # Lock UI immediately into Stopping state
            self._busy = "stopping"
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
                    self._persist_was_running(False)
                    self._update_tray_icon()
                except Exception as e:
                    print(f"[ERROR] Failed to stop stream cleanly: {e}")
                finally:
                    # Enforce symmetric 3-second state lock
                    elapsed = time.monotonic() - start_time
                    remaining = max(0.0, 3.0 - elapsed)
                    if remaining > 0:
                        time.sleep(remaining)

                    self._busy = None
                    self._set_voice_test_button_state(True)
                    self.set_status_pill(self._get_idle_status(), self._get_idle_color())
                    self._release_state_lock()

            threading.Thread(target=_stop_worker, daemon=True).start()

    def start_voice_test(self, sender=None, app_data=None):
        """Starts the 11-second in-memory voice preview test (microphone to speakers; the cable is not needed)."""
        if self.is_running:
            self.set_status_pill("Stop noise cancellation first to run voice test.", COL_WARNING)
            return
        if self._is_testing_voice:
            return
        if not self._state_lock.acquire(blocking=False):
            return

        self._is_testing_voice = True
        self._cancel_voice_test = False
        self._vt_active = True
        self._vt_progress = 0.0
        self._set_button_enabled("btn_voice_test", False)
        self._set_button_label("btn_voice_test", "Testing...")

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
            voice_downmix = InputDownmixer(frame_len=chunk_size, sample_rate=int(SAMPLE_RATE),
                                           mode=self.config.get("stereo_downmix", InputDownmixer.MODE_AUTO))
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
                        mono = voice_downmix.process(indata)
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
                        self._vt_progress = chunk_idx / float(total_chunks)
                        self._set_button_label("btn_voice_test", f"Recording... ({sec_left}s)")

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
                self._vt_progress = (11 - sec) / 11.0
                self._set_button_label("btn_voice_test", f"Playing back... ({sec}s)")
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
            self._vt_active = False
            self._vt_progress = 0.0
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
        """
        Silent VB-Cable installation in the background. The first-run card shows "Installing cable" with a progress
        bar until the install finishes; on failure it shows the error and Try again (see _poll_setup_card).
        The worker only changes state fields; the card is redrawn on the UI thread.
        """
        if self._install_state == "installing":
            return
        self._install_state = "installing"
        self._install_error = ""
        self._setup_dismissed = False

        def worker():
            success = install_vbcable_driver()

            # Wait for Windows AudioEndpointBuilder and Audiosrv to register the endpoints: poll every second, up to 10 s.
            for _ in range(10):
                time.sleep(1.0)
                self._refresh_devices(reinit_portaudio=True)
                if self.is_vbcable_installed:
                    break

            if self.is_vbcable_installed:
                self._install_error = ""
                self._install_state = None
            elif success:
                self._install_error = ("Setup finished, but the cable is not visible yet. "
                                       "A Windows restart completes it.")
                self._install_state = "failed"
            else:
                self._install_error = ("The install did not finish. Click Try again and accept the "
                                       "Windows permission prompt.")
                self._install_state = "failed"

        threading.Thread(target=worker, daemon=True).start()

    def show_setup_card_for_install(self):
        """Brings the first-run card back (used by the power button while the cable is missing)."""
        self.show_setup_card()
    def _update_cable_banner(self):
        """Refreshes the voice test enable state and the idle notice (the status card was removed in phase B)."""
        self._set_voice_test_button_state(not self.is_running)
        if not self.is_running and self._busy is None:
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

        # Force foreground activation on initial launch (not for a hidden --minimized launch)
        if not self._start_minimized:
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
            self._tick_ui()
            time.sleep(frame_sleep_s(self._is_window_visible(), time.perf_counter() - started))

    def _toggle_autostart(self, icon=None, menu_item=None):
        """Tray action for "Start with Windows": flips the Run entry. Never raises."""
        try:
            if autostart.is_enabled():
                autostart.disable()
            else:
                autostart.enable()
        except OSError as e:
            print(f"[AUTOSTART] Could not change the Windows startup entry: {e}")

    def _resume_filter_worker(self):
        """After a Start with Windows launch, starts noise cancellation once the devices are ready (up to 20 s)."""
        try:
            started = autostart.retry_until(self._try_resume, autostart.RESUME_WAIT_S, autostart.RESUME_POLL_S)
            if not started and self._app_running:
                print("[AUTOSTART] Noise cancellation was not resumed: the microphone or cable was not ready in time.")
        except Exception as e:
            print(f"[AUTOSTART] Resume failed: {e}")
        finally:
            self._resume_pending = False

    def _try_resume(self) -> bool:
        """One attempt of the resume: True when done (running, or Getsu is closing), False to retry."""
        if not self._app_running or self.is_running:
            return True
        if self._busy is not None or self._is_testing_voice:
            return False
        self._refresh_devices(reinit_portaudio=True)
        if not autostart.devices_ready(self.selected_input_idx, self.selected_output_idx, self.is_vbcable_installed):
            return False
        # Same start path as the button: 3 s state lock and the router swap.
        self.toggle_stream()
        while self._busy is not None and self._app_running:
            time.sleep(0.1)
        return self.is_running

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

        autostart_item = item(
            "Start with Windows", self._toggle_autostart,
            checked=lambda i: autostart.is_enabled(),
            enabled=autostart.is_supported())

        menu = Menu(
            item("Open Getsu", on_open, default=True),
            item("Toggle Start / Stop", on_toggle),
            autostart_item,
            pystray.Menu.SEPARATOR,
            item("Exit", on_exit),
        )

        # Dark right-click menu: must be requested before pystray creates the menu (best effort, see win_theme).
        win_theme.enable_dark_menus()
        self._tray_icon = pystray.Icon("getsu", create_tray_icon_image(self.is_running), "Getsu Noise Cancellation", menu)
        threading.Thread(target=self._tray_icon.run, daemon=True).start()

    # ------------------------------------------------------------------
    # Phase B layout: hero (power button, state, level meter), one card (microphone + mode),
    # actions, footer, and the three panels (microphone list, settings, help).
    # Controls with no native DearPyGui equivalent (power button, switches, icons, meter) are
    # drawlists. Their clicks go through _on_mouse_click, which tests hover on the drawlist.
    # ------------------------------------------------------------------

    def _text_w(self, text: str, font=None) -> int:
        """Width of a text run in physical pixels, measured with the given font (falls back to an estimate)."""
        try:
            return int(dpg.get_text_size(text, font=font or 0)[0])
        except Exception:
            return int(len(text) * self.s(7))

    def _fit_text(self, text: str, font, max_w: int) -> str:
        """Shortens text with '...' so it fits max_w pixels (names in the microphone rows)."""
        if self._text_w(text, font) <= max_w:
            return text
        cut = text
        while cut and self._text_w(cut + "...", font) > max_w:
            cut = cut[:-1]
        return cut.rstrip() + "..."

    def _centered_line(self, key: str, font, color, width: int, with_dot: bool = False):
        """A text line centred in width. With with_dot, a 7 px status dot sits before the text."""
        with dpg.group(horizontal=True):
            dpg.add_spacer(width=0, tag=f"{key}_pad")
            if with_dot:
                dpg.add_drawlist(width=self.s(8), height=self.s(18), tag=f"{key}_dot")
                dpg.add_spacer(width=self.s(6))
            dpg.add_text("", tag=f"{key}_text", color=color)
        if font:
            dpg.bind_item_font(f"{key}_text", font)

    def _tile_draw(self, drawlist: str, kind: str, bg, fg, size: int):
        """Rounded tile with a white input-device glyph (glyph: bt, headset, builtin, mic or nomic)."""
        dpg.delete_item(drawlist, children_only=True)
        y0 = self.s(14)  # the tile sits in the middle of the 60 px microphone row
        dpg.draw_rectangle((0, y0), (size, y0 + size), color=bg, fill=bg, rounding=self.s(8), parent=drawlist)
        self._draw_device_glyph(drawlist, kind, 0, y0, size, fg)

    def _draw_device_glyph(self, parent: str, kind: str, x0: float, y0: float, size: float, color, base=0):
        """Draws the icon for a device kind inside a square box (drawlist primitives only)."""
        thick = max(1.0, self.s(1.6))
        def poly(points, closed=False):
            dpg.draw_polyline(icons.to_px(points, x0, y0, size), color=color, thickness=thick, closed=closed, parent=parent)
        if kind == "bt":
            poly(icons.BUD_LEFT, closed=True)
            poly(icons.BUD_RIGHT, closed=True)
            poly(icons.BUD_STEM_LEFT)
            poly(icons.BUD_STEM_RIGHT)
        elif kind == "headset":
            poly(icons.HEADSET_BAND)
            poly(icons.HEADSET_LEFT, closed=True)
            poly(icons.HEADSET_RIGHT, closed=True)
        elif kind == "builtin":
            poly(icons.LAPTOP_SCREEN, closed=True)
            poly(icons.LAPTOP_BASE)
        else:
            poly(icons.MIC_BODY, closed=True)
            poly(icons.MIC_CRADLE)
            poly(icons.MIC_STEM)
            if kind == "nomic":
                poly([(4.0, 4.0), (20.0, 20.0)])

    def _draw_power(self):
        """Power button: filled circle, power glyph drawn with polylines, ring when stopped, arc while busy."""
        if not dpg.does_item_exist("power_drawlist"):
            return
        dl = "power_drawlist"
        dpg.delete_item(dl, children_only=True)
        size = self.s(104)
        c = size / 2.0
        if self.is_running:
            fill, glyph = COL_ACCENT, COL_WHITE
        else:
            fill, glyph = COL_CARD, (142, 142, 147)
        dpg.draw_circle((c, c), c, color=fill, fill=fill, parent=dl)
        if not self.is_running and self._busy is None:
            dpg.draw_circle((c, c), c - 0.5, color=(84, 84, 88, 255), thickness=1.0, parent=dl)
        if self._busy is not None:
            ring = icons.arc_points(c, c, c - self.s(3), -90.0, 180.0, 40)
            dpg.draw_polyline(ring, color=COL_ACCENT, thickness=float(self.s(3)), parent=dl)
        gs = self.s(38)
        x0 = c - gs / 2.0
        y0 = c - gs / 2.0
        thick = float(self.s(2))
        dpg.draw_polyline(icons.to_px(icons.POWER_STEM, x0, y0, gs), color=glyph, thickness=thick, parent=dl)
        dpg.draw_polyline(icons.to_px(icons.POWER_ARC, x0, y0, gs), color=glyph, thickness=thick, parent=dl)

    def _set_sub(self, text: str, color, dot_on: bool):
        """Secondary line under the state text: coloured dot plus text, centred."""
        width = self.s(372)
        pad_w = self.s(8) + self.s(6)
        # One line only: a long notice is cut with an ellipsis, and the full text is in the hover tooltip.
        fitted = self._fit_text(text, self.font_notice, width - pad_w - self.s(16))
        dpg.set_value("hero_sub_text", fitted)
        if color:
            dpg.configure_item("hero_sub_text", color=color)
        dpg.delete_item("hero_sub_dot", children_only=True)
        dot_color = COL_ACCENT if dot_on else COL_TEXT_3
        dpg.draw_circle((self.s(4), self.s(9)), float(self.s(3.5)), color=dot_color, fill=dot_color, parent="hero_sub_dot")
        tw = self._text_w(fitted, self.font_notice)
        dpg.configure_item("hero_sub_pad", width=max(0, (width - pad_w - tw) // 2))
        self._sub_full_text = text
        if dpg.does_item_exist("hero_sub_tip_text"):
            dpg.set_value("hero_sub_tip_text", text)
            dpg.configure_item("hero_sub_tip", show=fitted != text)

    def _render_hero(self):
        """Derives the hero (power button, state text, sub line, dot) from the engine state and the notice."""
        if not self._ui_built or not dpg.does_item_exist("hero_state_text"):
            return
        width = self.s(372)
        if self._busy == "starting":
            state, sub_default = "Starting", "Switching apps to your clean voice"
        elif self._busy == "stopping":
            state, sub_default = "Stopping", "Restoring your microphone"
        elif self.is_running:
            state, sub_default = "Noise cancellation on", "Apps hear your clean voice"
        else:
            state, sub_default = "Noise cancellation off", "Apps use your normal microphone"
        if self._notice:
            sub_text, sub_color = self._notice
        else:
            sub_text, sub_color = sub_default, COL_TEXT_2
        dpg.set_value("hero_state_text", state)
        dpg.configure_item("hero_state_pad", width=max(0, (width - self._text_w(state, self.font_state)) // 2))
        self._set_sub(sub_text, sub_color, dot_on=self.is_running)
        self._draw_power()

    def _on_power_click(self):
        """Clicking the power button does what START / STOP did: toggle_stream keeps the 3 s lock."""
        self.toggle_stream()

    def _draw_meter(self, level: float, live: bool):
        """Fourteen bars. Lit bars use the accent; when stopped every bar is flat and dim."""
        for i in range(14):
            if live:
                shape = 0.55 + 0.45 * abs(math.sin(i * 1.7 + self._meter_phase))
                h = self.s(3) + (self.s(18) - self.s(3)) * min(1.0, level * shape)
            else:
                h = self.s(3)
            x = i * self.s(7)
            fill = COL_ACCENT if (live and h > self.s(6)) else COL_FILL_2
            dpg.configure_item(f"meter_bar_{i}", pmin=(x, self.s(18) - h), pmax=(x + self.s(4), self.s(18)),
                               color=fill, fill=fill)

    def _tick_ui(self):
        """Called every render frame. Animates the level meter at most 20 times a second, and only while visible and running."""
        if not self._ui_built or not dpg.does_item_exist("meter_bar_0"):
            return
        self._update_button_hover()
        self._tick_controls()
        self._poll_setup_card()
        now = time.monotonic()
        if now - self._last_meter_t < 0.05:
            return
        self._last_meter_t = now
        self._meter_phase += 0.9
        live = bool(self.is_running and self.engine is not None and self._is_window_visible())
        if live:
            try:
                peak = float(self.engine.last_peak_db)
            except Exception:
                peak = -120.0
            level = min(1.0, max(0.0, (peak + 60.0) / 60.0))
            self._meter_level = max(level, self._meter_level * 0.8)
            self._draw_meter(self._meter_level, live=True)
            self._meter_flat = False
        elif not self._meter_flat:
            self._meter_level = 0.0
            self._draw_meter(0.0, live=False)
            self._meter_flat = True

    # --- microphone entries and widgets ----------------------------------

    def _mic_entries(self) -> List[Dict]:
        """One entry per detected microphone: label (for selection), title, subtitle, kind, full name."""
        raw_of = {label: self.device_raw.get(label, label) for label in self.device_map}
        kinds = {raw: classify_device(raw, is_input=True) for raw in raw_of.values()}
        entries = []
        for label, idx in self.device_map.items():
            raw = raw_of[label]
            kind = kinds[raw]
            entries.append({
                "label": label,
                "index": idx,
                "raw": raw,
                "title": split_name(raw)["title"],
                "subtitle": subtitle(raw, kind, kinds),
                "kind": kind,
                "glyph": _GLYPH_FOR_KIND.get(kind, "mic"),
            })
        return entries

    def _refresh_mic_widgets(self):
        """Updates the microphone row of the card, the hidden earbud hint, and the list if it is open."""
        if not self._ui_built or not dpg.does_item_exist("mic_row_title"):
            return
        entries = self._mic_entries()
        current = self._get_selected_input_label()
        chosen = next((e for e in entries if e["label"] == current), entries[0] if entries else None)
        width = self.s(268)
        if chosen is None:
            title, sub, glyph = "No microphone", "Getsu selects it as soon as you plug one in", "nomic"
            dpg.configure_item("mic_chev", show=False)
            dpg.configure_item("mic_row", show=True)
        else:
            title, sub, glyph = chosen["title"], chosen["subtitle"], chosen["glyph"]
            dpg.configure_item("mic_chev", show=True)
        dpg.set_value("mic_row_title", self._fit_text(title, self.font_header, width))
        dpg.set_value("mic_row_sub", self._fit_text(sub, self.font_notice, width))
        bg = COL_ACCENT if glyph != "nomic" else (99, 99, 102)
        self._tile_draw("mic_tile", glyph, bg, COL_WHITE, self.s(32))
        if FEATURE_EARBUD_HINT:
            bt_on = bool(chosen and chosen["kind"] == DeviceKind.BLUETOOTH_HEADSET
                         and any(e["kind"] == DeviceKind.BUILT_IN for e in entries))
            dpg.configure_item("hint_row", show=bt_on)
        if self._open_panel_name == "mics":
            self._rebuild_mic_list()

    def _select_mic_from_panel(self, label: str):
        """Selects a microphone the same way the old dropdown did (validation and locks live in on_input_changed)."""
        self.on_input_changed(None, label)
        self._close_panels()
        self._refresh_mic_widgets()

    def _rebuild_mic_list(self):
        """Rows of the microphone panel: kind icon, title, subtitle, and a check on the selected microphone."""
        if not dpg.does_item_exist("mic_list"):
            return
        dpg.delete_item("mic_list", children_only=True)
        self._panel_targets["mics"] = {}
        entries = self._mic_entries()
        current = self._get_selected_input_label()
        row_w = self.s(372)
        row_h = self.s(52)
        label_w = self.s(284)
        for i, entry in enumerate(entries):
            tag = f"mic_row_{i}"
            with dpg.child_window(tag=tag, parent="mic_list", width=row_w, height=row_h, border=False,
                                  no_scrollbar=True, no_scroll_with_mouse=True):
                self._bind_flat(tag)
                with dpg.group(horizontal=True):
                    dpg.add_spacer(width=self.s(16))
                    icon_dl = f"{tag}_icon"
                    dpg.add_drawlist(width=self.s(16), height=row_h, tag=icon_dl)
                    self._draw_device_glyph(icon_dl, entry["glyph"], self.s(0), self.s(18), self.s(16), COL_TEXT_2)
                    dpg.add_spacer(width=self.s(12))
                    with dpg.child_window(width=label_w, height=row_h, border=False, no_scrollbar=True):
                        self._bind_flat(f"{tag}_label")
                        dpg.add_spacer(height=self.s(10))
                        dpg.add_text(self._fit_text(entry["title"], self.font_list, label_w), color=COL_TEXT)
                        dpg.bind_item_font(dpg.last_item(), self.font_list)
                        dpg.add_text(self._fit_text(entry["subtitle"], self.font_notice, label_w), color=COL_TEXT_2)
                        dpg.bind_item_font(dpg.last_item(), self.font_notice)
                    dpg.add_spacer(width=self.s(12))
                    tick_dl = f"{tag}_tick"
                    dpg.add_drawlist(width=self.s(16), height=row_h, tag=tick_dl)
                    self._hit_parts[tag] = [icon_dl, tick_dl]
                    if entry["label"] == current:
                        dpg.draw_polyline(icons.to_px(icons.CHECK, 0, self.s(18), self.s(16)), color=COL_ACCENT,
                                          thickness=float(self.s(2)), parent=tick_dl)
            self._panel_targets["mics"][tag] = (lambda lab=entry["label"]: self._select_mic_from_panel(lab))
            if i < len(entries) - 1:
                div = f"{tag}_div"
                with dpg.group(horizontal=True, parent="mic_list"):
                    dpg.add_spacer(width=self.s(16))
                    dpg.add_drawlist(width=self.s(340), height=1, tag=div)
                    dpg.draw_line((0, 0), (self.s(340), 0), color=COL_SEPARATOR, thickness=1.0, parent=div)
        if not entries:
            dpg.add_text("No microphone detected.", color=COL_TEXT_2, parent="mic_list")

    def _bind_flat(self, tag: str):
        if dpg.does_item_exist(tag) and self.theme_flat:
            dpg.bind_item_theme(tag, self.theme_flat)

    # --- switches and overlays --------------------------------------------

    def _make_switch(self, name: str, parent: str, on: bool, callback):
        """Drawn toggle (42 x 26, accent when on). Registered for clicks."""
        sw = _Switch(self, name, on, callback)
        sw.build(parent)
        self._switches[name] = sw
        self._panel_targets.setdefault("settings", {})[sw.tag] = (lambda n=name: self._toggle_switch(n))

    def _toggle_switch(self, name: str):
        sw = self._switches.get(name)
        if sw is not None:
            sw.toggle()

    def _switch_set(self, name: str, on: bool):
        """Sets a switch without firing its callback (used when a change is refused)."""
        sw = self._switches.get(name)
        if sw is not None:
            sw.set(on)

    # --- icon buttons: tinted buttons built from a child window ----------------
    # The icon is a drawlist and the label a text item inside the button's own child window, so the icon is
    # clipped and hidden together with the button. Nothing is drawn on the viewport layer.

    def _text_h(self, text: str, font=None) -> int:
        """Height of a text run in physical pixels (falls back to an estimate)."""
        try:
            return int(dpg.get_text_size(text or " ", font=font or 0)[1])
        except Exception:
            return self.s(16)

    def _icon_button(self, tag: str, label: str, glyph: str, width: int, height: int, callback, tip: str = ""):
        """
        Tinted button: icon (16 px) and label (SemiBold 13 px) centred, 7 px apart, on the accent soft fill.
        glyph: play, sliders, help or cable. The label may be empty (icon only).
        Clicks go through _click_targets; hover and enabled state are applied by _update_button_hover and
        _refresh_button. Hover text is a dpg.tooltip on the button.
        """
        icon = self.s(16)
        gap = self.s(7)
        font = self.font_btn
        label_w = self._text_w(label, font) if label else 0
        content_w = icon + (gap + label_w if label else 0)
        pad = max(0, (width - content_w) // 2)
        with dpg.child_window(tag=tag, width=width, height=height, border=False, no_scrollbar=True,
                              no_scroll_with_mouse=True):
            with dpg.group(horizontal=True):
                dpg.add_spacer(width=pad)
                dpg.add_drawlist(width=icon, height=height, tag=f"{tag}_icon")
                if label:
                    dpg.add_spacer(width=gap)
                    with dpg.group():
                        dpg.add_spacer(height=max(0, (height - self._text_h(label, font)) // 2))
                        dpg.add_text(label, tag=f"{tag}_label", color=COL_ACCENT)
                        if font:
                            dpg.bind_item_font(f"{tag}_label", font)
        if tip:
            with dpg.tooltip(parent=tag, delay=0.5):
                dpg.add_text(tip, tag=f"{tag}_tip")
        self._btn_glyphs[tag] = glyph
        self._btn_heights[tag] = height
        self._btn_enabled.setdefault(tag, True)
        self._btn_hovered[tag] = False
        self._click_targets[tag] = (lambda t=tag, cb=callback: self._run_button(t, cb))
        self._btn_state[tag] = None
        self._refresh_button(tag)

    def _run_button(self, tag: str, callback):
        """Runs an icon button's callback unless the button is disabled."""
        if self._btn_enabled.get(tag, True):
            callback()

    def _set_button_enabled(self, tag: str, enabled: bool):
        """Enables or disables an icon button (drawn greyed when disabled, no click)."""
        self._btn_enabled[tag] = bool(enabled)
        self._refresh_button(tag)

    def _set_button_label(self, tag: str, text: str):
        if dpg.does_item_exist(f"{tag}_label"):
            dpg.set_value(f"{tag}_label", text)

    def _set_button_tip(self, tag: str, text: str):
        if dpg.does_item_exist(f"{tag}_tip"):
            dpg.set_value(f"{tag}_tip", text)

    def _refresh_button(self, tag: str):
        """Applies the fill, label and icon colour for the current enabled and hovered state. Redraws only on change."""
        if not dpg.does_item_exist(tag):
            return
        enabled = self._btn_enabled.get(tag, True)
        hovered = self._btn_hovered.get(tag, False) and enabled
        key = (enabled, hovered)
        if self._btn_state.get(tag) == key:
            return
        self._btn_state[tag] = key
        if not enabled:
            theme, color = self.theme_tint_disabled, COL_TEXT_3
        elif hovered:
            theme, color = self.theme_tint_hover, COL_ACCENT
        else:
            theme, color = self.theme_tint_normal, COL_ACCENT
        dpg.bind_item_theme(tag, theme)
        if dpg.does_item_exist(f"{tag}_label"):
            dpg.configure_item(f"{tag}_label", color=color)
        self._draw_button_icon(tag, color)

    def _update_button_hover(self):
        """Hover is tested on the button rectangles (see ui_hit). No button is hovered while a panel is open."""
        if not self._ui_built or not self._btn_glyphs:
            return
        point = dpg.get_mouse_pos(local=False)  # viewport space, the same space as item rectangles
        for tag in self._btn_glyphs:
            rect = self._item_rect(tag)
            hovered = (self._open_panel_name is None and rect is not None
                       and ui_hit.point_in_rect(point, rect))
            if hovered != self._btn_hovered.get(tag, False):
                self._btn_hovered[tag] = hovered
            self._refresh_button(tag)

    def _draw_button_icon(self, tag: str, color):
        """Draws a button glyph in its drawlist (drawlist primitives only), centred vertically."""
        dl = f"{tag}_icon"
        glyph = self._btn_glyphs.get(tag)
        if glyph is None or not dpg.does_item_exist(dl):
            return
        dpg.delete_item(dl, children_only=True)
        size = self.s(16)
        y0 = (self._btn_heights.get(tag, size) - size) / 2.0
        self._draw_glyph_shape(dl, glyph, 0, y0, size, color)

    def _draw_glyph_shape(self, parent: str, glyph: str, x0: float, y0: float, size: float, color):
        """Shapes of the button glyphs (play, sliders, help, cable) on a size px box at (x0, y0)."""
        thick = float(self.s(2))
        if glyph == "play":
            pts = icons.to_px(icons.PLAY, x0, y0, size)
            dpg.draw_polygon(pts, color=color, fill=color, parent=parent)
        elif glyph == "sliders":
            for (a, b) in icons.SLIDER_LINES:
                pa, pb = icons.to_px([a, b], x0, y0, size)
                dpg.draw_line(pa, pb, color=color, thickness=thick, parent=parent)
            for (c, r) in icons.SLIDER_KNOBS:
                dpg.draw_circle(icons.to_px([c], x0, y0, size)[0], float(r) * size / 24.0, color=color,
                                thickness=thick, parent=parent)
        elif glyph == "help":
            dpg.draw_polyline(icons.to_px(icons.HELP_RING, x0, y0, size), color=color, thickness=thick,
                              closed=True, parent=parent)
            dpg.draw_polyline(icons.to_px(icons.HELP_QUESTION, x0, y0, size), color=color, thickness=thick,
                              parent=parent)
            dpg.draw_circle(icons.to_px([icons.HELP_DOT], x0, y0, size)[0], float(self.s(1.2)), color=color,
                            fill=color, parent=parent)
        elif glyph == "cable":
            dpg.draw_polyline(icons.to_px(icons.CABLE_BODY, x0, y0, size), color=color, thickness=thick,
                              closed=True, parent=parent)
            dpg.draw_polyline(icons.to_px(icons.CABLE_PRONG_LEFT, x0, y0, size), color=color, thickness=thick,
                              parent=parent)
            dpg.draw_polyline(icons.to_px(icons.CABLE_PRONG_RIGHT, x0, y0, size), color=color, thickness=thick,
                              parent=parent)
            dpg.draw_polyline(icons.to_px(icons.CABLE_TAIL, x0, y0, size), color=color, thickness=thick,
                              parent=parent)

    # --- first-run card (replaces the hero while the virtual cable is missing) ---

    def _build_setup_card(self, width: int, height: int):
        """
        Card in the hero's slot (same height). Three states: One-time setup (offer), Installing cable (progress,
        no buttons) and a failed install (error text with Try again). Shown by _poll_setup_card.
        """
        pad = self.s(16)
        inner_w = width - 2 * pad
        with dpg.child_window(tag="setup_box", width=width, height=height, border=False, no_scrollbar=True,
                              show=False):
            self._bind_flat("setup_box")
            dpg.add_spacer(height=self.s(8))
            with dpg.child_window(tag="setup_card", width=width, height=height - self.s(16), border=False,
                                  no_scrollbar=True):
                self._bind_card("setup_card")
                dpg.add_spacer(height=pad)
                with dpg.group(horizontal=True):
                    dpg.add_spacer(width=pad)
                    dpg.add_drawlist(width=self.s(40), height=self.s(40), tag="setup_tile")
                    dpg.add_spacer(width=self.s(12))
                    with dpg.group():
                        dpg.add_spacer(height=self.s(9))
                        dpg.add_text("One-time setup", tag="setup_title", color=COL_TEXT)
                        dpg.bind_item_font("setup_title", self.font_title)
                dpg.add_spacer(height=self.s(12))
                with dpg.group(horizontal=True):
                    dpg.add_spacer(width=pad)
                    dpg.add_text(SETUP_BODY_OFFER, tag="setup_body", wrap=inner_w, color=COL_TEXT_2)
                    dpg.bind_item_font("setup_body", self.font_body)
                dpg.add_spacer(height=self.s(12))
                with dpg.group(horizontal=True):
                    dpg.add_spacer(width=pad)
                    dpg.add_drawlist(width=inner_w, height=self.s(4), tag="setup_progress")
                dpg.add_spacer(height=self.s(12))
                with dpg.group(horizontal=True, tag="setup_buttons"):
                    dpg.add_spacer(width=pad)
                    dpg.add_button(label="Install cable", tag="setup_install", width=self.s(170), height=self.s(40),
                                   callback=lambda: self.run_driver_install_thread())
                    dpg.bind_item_theme("setup_install", self.theme_start_btn)
                    dpg.bind_item_font("setup_install", self.font_btn)
                    dpg.add_spacer(width=self.s(8))
                    dpg.add_button(label="Not now", tag="setup_skip", width=inner_w - self.s(178), height=self.s(40),
                                   callback=self.on_setup_skip_click)
                    dpg.bind_item_theme("setup_skip", self.theme_secondary_btn)
                    dpg.bind_item_font("setup_skip", self.font_btn)

    def _card_visible(self) -> bool:
        """The first-run card shows while the cable is missing (unless dismissed), and during an install or after one failed."""
        if self.is_vbcable_installed and self._install_state != "installing":
            return False
        if self._install_state in ("installing", "failed"):
            return True
        return not self.is_vbcable_installed and not self._setup_dismissed

    def _poll_setup_card(self):
        """Every frame: applies the card state when it changed, and moves the install progress at most 20 times a second."""
        if not self._ui_built or not dpg.does_item_exist("setup_box"):
            return
        visible = self._card_visible()
        sig = (visible, self._install_state, self._install_error, self.is_vbcable_installed)
        if sig != self._card_sig:
            self._card_sig = sig
            self._apply_setup_card(visible)
        if visible and self._install_state == "installing":
            now = time.monotonic()
            if now - self._setup_anim_t >= 0.05:
                self._setup_anim_t = now
                self._draw_setup_progress(now)

    def _apply_setup_card(self, visible: bool):
        """Sets the card texts and buttons for the current state, and swaps it with the hero."""
        state = self._install_state
        if state == "installing":
            title, body, body_color = "Installing cable", SETUP_BODY_INSTALLING, COL_TEXT_2
        elif state == "failed":
            title, body, body_color = "One-time setup", self._install_error, COL_ERROR
        else:
            title, body, body_color = "One-time setup", SETUP_BODY_OFFER, COL_TEXT_2
        dpg.set_value("setup_title", title)
        dpg.set_value("setup_body", body)
        dpg.configure_item("setup_body", color=body_color)
        dpg.configure_item("setup_progress", show=state == "installing")
        dpg.configure_item("setup_buttons", show=state != "installing")
        dpg.configure_item("setup_install", label="Try again" if state == "failed" else "Install cable")
        dpg.configure_item("setup_box", show=visible)
        dpg.configure_item("hero_box", show=not visible)
        self._draw_setup_tile()
        self._update_cable_banner()

    def _draw_setup_tile(self):
        """Blue rounded tile with the cable glyph, 40 px (mockup .big-ic)."""
        dl = "setup_tile"
        if not dpg.does_item_exist(dl):
            return
        dpg.delete_item(dl, children_only=True)
        size = self.s(40)
        dpg.draw_rectangle((0, 0), (size, size), color=COL_ACCENT, fill=COL_ACCENT, rounding=self.s(10), parent=dl)
        glyph_px = self.s(20)
        self._draw_glyph_shape(dl, "cable", (size - glyph_px) / 2.0, (size - glyph_px) / 2.0, glyph_px, COL_WHITE)

    def _draw_setup_progress(self, now: float):
        """Indeterminate thin bar: a 35 % accent segment sliding across the track."""
        dl = "setup_progress"
        if not dpg.does_item_exist(dl):
            return
        dpg.delete_item(dl, children_only=True)
        width = self.s(CONTENT_W - 32)  # the card inner width: 16 px padding on each side
        height = self.s(4)
        dpg.draw_rectangle((0, 0), (width, height), color=COL_FILL, fill=COL_FILL, rounding=self.s(2), parent=dl)
        phase = (now * 0.8) % 1.0
        x0 = max(0.0, (phase * 1.35 - 0.35) * width)
        x1 = min(float(width), x0 + 0.35 * width)
        if x1 > x0:
            dpg.draw_rectangle((x0, 0), (x1, height), color=COL_ACCENT, fill=COL_ACCENT, rounding=self.s(2), parent=dl)

    def on_setup_skip_click(self, sender=None, app_data=None):
        """Not now: hides the card for this session and shows the hero with the notice 'Virtual cable not installed'."""
        self._setup_dismissed = True
        self._install_state = None
        self._poll_setup_card()

    def show_setup_card(self, sender=None, app_data=None):
        """Brings the first-run card back (power button or tray while the cable is missing)."""
        self._refresh_devices(reinit_portaudio=True)
        if self.is_vbcable_installed:
            self._update_cable_banner()
            return
        self._setup_dismissed = False
        self._poll_setup_card()

    def _open_panel(self, name: str):
        """Opens a panel (mics, settings or help) over the window. The window height does not change."""
        if not self._ui_built or not dpg.does_item_exist(f"panel_{name}"):
            return
        if name == "mics":
            self._rebuild_mic_list()
        vp_w = dpg.get_viewport_client_width()
        vp_h = dpg.get_viewport_client_height()
        sheet_h = self._panel_sheet_height(name, vp_h)
        if name == "mics":
            # The list takes the sheet height left after the head; it scrolls only when it is longer.
            dpg.configure_item("mic_list", height=max(self.s(52), sheet_h - self.s(56) - self.s(24)))
        dpg.configure_item(f"panel_{name}", pos=[0, 0], width=vp_w, height=vp_h)
        dpg.show_item(f"panel_{name}")
        dpg.focus_item(f"panel_{name}")  # raise above the main window
        dpg.configure_item(f"panel_{name}_gap", height=max(0, vp_h - sheet_h))
        dpg.configure_item(f"sheet_{name}", height=sheet_h, width=vp_w)
        self._open_panel_name = name

    def _panel_sheet_height(self, name: str, vp_h: int) -> int:
        """Sheet height: head plus content, never more than the viewport. The mic list scrolls when it is longer."""
        if name == "mics":
            rows = max(1, len(self.device_map))
            content = rows * self.s(52) + max(0, rows - 1) + self.s(16)
            return min(vp_h, self.s(56) + content)
        if name == "settings":
            return min(vp_h, self.s(56) + self.s(4 * SETTINGS_ROW_H + 3) + self.s(24))
        return min(vp_h, self.s(56 + 252 + 24))

    def _close_panels(self):
        for name in ("mics", "settings", "help"):
            if dpg.does_item_exist(f"panel_{name}"):
                dpg.hide_item(f"panel_{name}")
        self._open_panel_name = None

    def _leaf_rect(self, tag: str):
        """
        Rectangle (min, max) in viewport coordinates of one item, or None. Items other than windows report it with
        get_item_rect_min/max. Windows do not (DearPyGui 2.3.1 raises KeyError), so a window uses its position and
        size: that is exact for windows whose parent is the viewport or a top-level panel at (0, 0).
        """
        if not dpg.does_item_exist(tag):
            return None
        try:
            rmin = tuple(dpg.get_item_rect_min(tag))
            rmax = tuple(dpg.get_item_rect_max(tag))
        except KeyError:
            try:
                rmin = tuple(dpg.get_item_pos(tag))
                size = dpg.get_item_rect_size(tag)
                rmax = (rmin[0] + size[0], rmin[1] + size[1])
            except Exception:
                return None
        if rmax[0] <= rmin[0] or rmax[1] <= rmin[1]:
            return None
        return rmin, rmax

    def _item_rect(self, tag: str):
        """
        Rectangle of a clickable control. A control made of child windows (a microphone row) is measured by the
        union of its leaf drawlists, listed in _hit_parts, because nested windows have no rectangle of their own.
        """
        parts = self._hit_parts.get(tag)
        if not parts:
            return self._leaf_rect(tag)
        rects = [r for r in (self._leaf_rect(p) for p in parts) if r is not None]
        if not rects:
            return None
        lo = (min(r[0][0] for r in rects), min(r[0][1] for r in rects))
        hi = (max(r[1][0] for r in rects), max(r[1][1] for r in rects))
        return lo, hi

    def _on_mouse_click(self, sender=None, app_data=None):
        """
        Global left click, resolved geometrically (see ui_hit). With a panel open, a click outside its sheet closes
        it, a click on a row or switch inside the sheet runs that control, and the main window gets no clicks.
        With no panel open, the topmost main-window control under the pointer runs.
        """
        if not self._ui_built:
            return
        point = dpg.get_mouse_pos(local=False)  # viewport space, the same space as item rectangles
        if self._open_panel_name:
            name = self._open_panel_name
            if ui_hit.click_is_outside(point, self._item_rect(f"sheet_{name}")):
                self._close_panels()
                return
            panel_targets = self._panel_targets.get(name, {})
            tag = ui_hit.pick_target(point, [(t, self._item_rect(t)) for t in panel_targets])
            if tag is not None:
                panel_targets[tag]()
            return
        tag = ui_hit.pick_target(point, [(t, self._item_rect(t)) for t in self._click_targets])
        if tag is not None:
            self._click_targets[tag]()

    def show_how_to_use_modal(self, sender=None, app_data=None):
        """Opens the How to use panel over the window."""
        self._open_panel("help")

    def _set_mode(self, name: str):
        """Mode segment selection (UI only until the Modes backend lands in M2.4)."""
        self._mode = name
        for i, mode in enumerate(MODE_NAMES):
            dpg.bind_item_theme(f"mode_{i}", self.theme_boost_active if mode == name else self.theme_boost_inactive)
        desc = MODE_DESCRIPTIONS[name]
        dpg.set_value("mode_desc", desc)

    # --- main window -------------------------------------------------------

    def _build_hero(self, width: int):
        """Hero block (power, state, sub line, meter) in hero_box. The first-run card takes its place (see _build_setup_card)."""
        with dpg.child_window(tag="hero_box", width=width, height=self.s(HERO_H), border=False, no_scrollbar=True,
                              no_scroll_with_mouse=True):
            self._bind_flat("hero_box")
            dpg.add_spacer(height=self.s(16))
            with dpg.group(horizontal=True):
                dpg.add_spacer(width=(width - self.s(104)) // 2)
                dpg.add_drawlist(width=self.s(104), height=self.s(104), tag="power_drawlist")
            self._click_targets["power_drawlist"] = self._on_power_click
            dpg.add_spacer(height=self.s(16))
            self._centered_line("hero_state", self.font_state, COL_TEXT, width)
            dpg.add_spacer(height=self.s(2))
            self._centered_line("hero_sub", self.font_notice, COL_TEXT_2, width, with_dot=True)
            with dpg.tooltip("hero_sub_text", tag="hero_sub_tip", show=False):
                dpg.add_text("", tag="hero_sub_tip_text", wrap=self.s(340))
            dpg.add_spacer(height=self.s(12))
            with dpg.group(horizontal=True):
                dpg.add_spacer(width=(width - self.s(95)) // 2)
                dpg.add_drawlist(width=self.s(95), height=self.s(18), tag="meter")
            for i in range(14):
                x = i * self.s(7)
                dpg.draw_rectangle((x, self.s(15)), (x + self.s(4), self.s(18)), color=COL_FILL_2, fill=COL_FILL_2,
                                   rounding=self.s(2), tag=f"meter_bar_{i}", parent="meter")
        dpg.add_spacer(height=self.s(20))

    def _build_mic_card(self, width: int):
        modes_h = self.s(85) if FEATURE_MODES else 0
        hint_h = self.s(28) if FEATURE_EARBUD_HINT else 0
        card_h = self.s(60) + hint_h + modes_h
        with dpg.child_window(tag="mic_card", width=width, height=card_h, border=False, no_scrollbar=True):
            self._bind_card("mic_card")
            with dpg.child_window(tag="mic_row", width=width, height=self.s(60), border=False,
                                  no_scrollbar=True, no_scroll_with_mouse=True):
                self._bind_flat("mic_row")
                with dpg.group(horizontal=True):
                    dpg.add_spacer(width=self.s(16))
                    dpg.add_drawlist(width=self.s(32), height=self.s(60), tag="mic_tile")
                    dpg.add_spacer(width=self.s(12))
                    with dpg.child_window(width=self.s(268), height=self.s(60), border=False, no_scrollbar=True):
                        self._bind_flat("mic_label_box")
                        dpg.add_spacer(height=self.s(12))
                        dpg.add_text("", tag="mic_row_title", color=COL_TEXT)
                        dpg.bind_item_font("mic_row_title", self.font_header)
                        dpg.add_text("", tag="mic_row_sub", color=COL_TEXT_2)
                        dpg.bind_item_font("mic_row_sub", self.font_notice)
                    dpg.add_drawlist(width=self.s(16), height=self.s(60), tag="mic_chev")
                    dpg.draw_polyline(icons.to_px(icons.CHEVRON, 0, self.s(22), self.s(16)), color=COL_TEXT_3,
                                      thickness=float(self.s(2)), parent="mic_chev")
            self._click_targets["mic_row"] = self._open_mic_panel_from_row
            self._hit_parts["mic_row"] = ["mic_tile", "mic_chev"]
            with dpg.group(horizontal=True, tag="hint_row", show=FEATURE_EARBUD_HINT):
                dpg.add_spacer(width=self.s(16))
                dpg.add_text("Earbud mic drops audio to call quality.", color=COL_TEXT_2, tag="hint_text")
                dpg.bind_item_font("hint_text", self.font_notice)
                dpg.add_button(label="Use built-in mic", tag="btn_use_builtin", callback=self._use_builtin_mic)
                dpg.bind_item_theme("btn_use_builtin", self.theme_link_btn)
            with dpg.group(tag="mode_block", show=FEATURE_MODES):
                with dpg.group(horizontal=True):
                    dpg.add_spacer(width=self.s(16))
                    dpg.add_drawlist(width=width - self.s(32), height=1, tag="mode_divider")
                    dpg.draw_line((0, 0), (width - self.s(32), 0), color=COL_SEPARATOR, thickness=1.0, parent="mode_divider")
                dpg.add_spacer(height=self.s(12))
                with dpg.group(horizontal=True):
                    dpg.add_spacer(width=self.s(16))
                    dpg.add_text("Mode", color=COL_TEXT)
                    dpg.bind_item_font(dpg.last_item(), self.font_btn)
                    dpg.add_spacer(width=0, tag="mode_desc_pad")
                    dpg.add_text(MODE_DESCRIPTIONS[self._mode], color=COL_TEXT_2, tag="mode_desc")
                    dpg.bind_item_font("mode_desc", self.font_notice)
                dpg.add_spacer(height=self.s(8))
                seg_w = (width - self.s(32) - self.s(6)) // 4
                with dpg.group(horizontal=True):
                    dpg.add_spacer(width=self.s(16))
                    for i, mode in enumerate(MODE_NAMES):
                        dpg.add_button(label=mode, tag=f"mode_{i}", width=seg_w, height=self.s(30),
                                       callback=lambda s, a, m=mode: self._set_mode(m))
                        dpg.bind_item_font(f"mode_{i}", self.font_btn)
                        if i < 3:
                            dpg.add_spacer(width=self.s(2))
                dpg.add_spacer(height=self.s(16))
                self._set_mode(self._mode)

    def _bind_card(self, tag: str):
        if self.theme_card:
            dpg.bind_item_theme(tag, self.theme_card)

    def _open_mic_panel_from_row(self):
        if self.device_map:
            self._open_panel("mics")

    def _use_builtin_mic(self, sender=None, app_data=None):
        entries = self._mic_entries()
        builtin = next((e for e in entries if e["kind"] == DeviceKind.BUILT_IN), None)
        if builtin is not None:
            self.on_input_changed(None, builtin["label"])
            self._refresh_mic_widgets()

    def _build_actions(self, width: int):
        help_w = self.s(44)
        btn_w = (width - self.s(16) - help_w) // 2
        btn_h = self.s(44)
        with dpg.group(horizontal=True):
            self._icon_button("btn_voice_test", "Hear myself", "play", btn_w, btn_h, self.start_voice_test,
                              tip="Record 11 seconds, then hear how others hear you")
            dpg.add_spacer(width=self.s(8))
            self._icon_button("btn_settings", "Show settings", "sliders", btn_w, btn_h,
                              lambda: self._open_panel("settings"),
                              tip="Sensitivity, mic boost, default microphone")
            dpg.add_spacer(width=self.s(8))
            self._icon_button("btn_help", "", "help", help_w, btn_h,
                              lambda: self._open_panel("help"), tip="How to use Getsu")

    def _build_footer(self, width: int):
        """Creator on the left, sponsor link on the right. The gap between them is set from the rendered widths (_place_footer)."""
        with dpg.group(horizontal=True, tag="footer_row"):
            dpg.add_text("Created by Ihsan", color=COL_TEXT_3, tag="footer_creator_text")
            dpg.bind_item_font("footer_creator_text", self.font_footer)
            dpg.add_spacer(width=0, tag="footer_gap")
            with dpg.group(horizontal=True, tag="footer_sponsor"):
                dpg.add_text("Sponsored by", color=COL_TEXT_3, tag="footer_sponsor_text")
                dpg.bind_item_font("footer_sponsor_text", self.font_footer)
                dpg.add_spacer(width=self.s(4))
                dpg.add_button(label="Kopi Reman", callback=lambda: webbrowser.open("https://www.instagram.com/kopireman/"),
                               tag="sponsor_link_btn")
                dpg.bind_item_theme("sponsor_link_btn", self.theme_link_btn)
                dpg.bind_item_font("sponsor_link_btn", self.font_footer)

    def _place_footer(self):
        """Right-aligns the sponsor group: the gap is the content width minus the two groups as rendered."""
        try:
            left = dpg.get_item_rect_size("footer_creator_text")[0]
            right = dpg.get_item_rect_size("footer_sponsor")[0]
            dpg.configure_item("footer_gap", width=max(0, self.s(CONTENT_W) - left - right))
        except Exception as e:
            print(f"[WARN] Footer layout failed: {e}")
    # --- panels -----------------------------------------------------------

    def _build_panel_shell(self, name: str):
        """Modal overlay: scrim (dim) above the window, and a sheet anchored to the bottom. Resized by _open_panel."""
        vp_w = self.s(420)
        vp_h = self.s(640)
        # Not a DearPyGui modal: a modal popup that was closed earlier (the VB-Cable dialog) blocks later modals.
        # The window covers the whole client area, so it takes all input; _on_mouse_click closes it on a scrim click.
        with dpg.window(tag=f"panel_{name}", show=False, no_title_bar=True, no_move=True,
                        no_resize=True, no_scrollbar=True, no_collapse=True, no_close=True,
                        pos=[0, 0], width=vp_w, height=vp_h):
            if self.theme_scrim:
                dpg.bind_item_theme(f"panel_{name}", self.theme_scrim)
            dpg.add_spacer(tag=f"panel_{name}_gap", height=vp_h)
            with dpg.child_window(tag=f"sheet_{name}", width=vp_w, height=vp_h, border=False, no_scrollbar=False):
                if self.theme_sheet:
                    dpg.bind_item_theme(f"sheet_{name}", self.theme_sheet)
                self._panel_body(name)

    def _sheet_head(self, title: str, done_callback):
        width = self.s(372)
        dpg.add_spacer(height=self.s(16))
        with dpg.group(horizontal=True):
            dpg.add_spacer(width=self.s(24))
            dpg.add_text(title, color=COL_TEXT)
            dpg.bind_item_font(dpg.last_item(), self.font_title)
            dpg.add_spacer(width=max(0, width - self._text_w(title, self.font_title) - self.s(60)))
            if done_callback is not None:
                dpg.add_button(label="Done", callback=done_callback, width=self.s(44), height=self.s(26))
                dpg.bind_item_theme(dpg.last_item(), self.theme_link_btn)
                dpg.bind_item_font(dpg.last_item(), self.font_btn)
        dpg.add_spacer(height=self.s(12))

    def _panel_body(self, name: str):
        width = self.s(372)
        if name == "mics":
            self._sheet_head("Microphone", lambda: self._close_panels())
            with dpg.group(horizontal=True):
                dpg.add_spacer(width=self.s(24))
                with dpg.child_window(tag="mic_list", width=width, height=self.s(100), border=False):
                    self._bind_list("mic_list")
            dpg.add_spacer(height=self.s(8))
            with dpg.group(horizontal=True):
                dpg.add_spacer(width=self.s(24))
                dpg.add_text("", tag="input_selection_hint", wrap=width, color=COL_WARNING, show=False)
                dpg.bind_item_font("input_selection_hint", self.font_notice)
        elif name == "settings":
            self._sheet_head("Settings", lambda: self._close_panels())
            with dpg.group(horizontal=True):
                dpg.add_spacer(width=self.s(24))
                with dpg.child_window(tag="settings_card", width=width, height=self.s(4 * SETTINGS_ROW_H + 3),
                                      border=False):
                    self._bind_list("settings_card")
                    self._settings_row_slider(width)
                    self._divider(width)
                    self._settings_row_boost(width)
                    self._divider(width)
                    self._settings_row_switch("auto_route", "Use as default microphone",
                                              "Discord, Zoom and games pick it up automatically",
                                              bool(self.config.get("auto_route", True)),
                                              self._on_auto_route_switch)
                    self._divider(width)
                    self._settings_row_switch("music_mode", "Music & Singing mode", None,
                                              bool(self.config.get("music_mode", False)),
                                              self._on_music_mode_switch)
                    self._divider(width, show=FEATURE_LEARN_ROOM)
                    self._settings_row_learn(width, show=FEATURE_LEARN_ROOM)
        else:
            self._sheet_head("How to use Getsu", None)
            with dpg.group(horizontal=True):
                dpg.add_spacer(width=self.s(24))
                with dpg.child_window(tag="help_body", width=width, height=self.s(252), border=False):
                    self._help_steps(width - self.s(34))
                    dpg.add_spacer(height=self.s(4))
                    dpg.add_button(label="Got it", width=width, height=self.s(40), callback=lambda: self._close_panels())
                    dpg.bind_item_theme(dpg.last_item(), self.theme_start_btn)
                    dpg.bind_item_font(dpg.last_item(), self.font_btn)

    def _bind_list(self, tag: str):
        if self.theme_list and dpg.does_item_exist(tag):
            dpg.bind_item_theme(tag, self.theme_list)

    def _divider(self, width: int, show: bool = True):
        with dpg.group(horizontal=True, show=show):
            dpg.add_spacer(width=self.s(16))
            dpg.add_drawlist(width=width - self.s(16), height=1)
            dpg.draw_line((0, 0), (width - self.s(32), 0), color=COL_SEPARATOR, thickness=1.0, parent=dpg.last_item())

    # Settings rows: label column (SET_LABEL_W, detail text wraps inside it) and control column (SET_CTRL_W).

    def _settings_row_slider(self, width: int):
        self._vad_value = float(self.config.get("vad_threshold", 0.70))
        with dpg.group(horizontal=True):
            dpg.add_spacer(width=self.s(16))
            with dpg.child_window(width=self.s(SET_LABEL_W), height=self.s(SETTINGS_ROW_H), border=False,
                                  no_scrollbar=True):
                self._bind_flat(dpg.last_item())
                dpg.add_spacer(height=self.s(10))
                dpg.add_text("Sensitivity", color=COL_TEXT)
                dpg.add_text("Higher ignores more background voices", color=COL_TEXT_2,
                             wrap=self.s(SET_LABEL_W))
                dpg.bind_item_font(dpg.last_item(), self.font_notice)
            with dpg.group():
                dpg.add_spacer(height=self.s(17))
                dpg.add_drawlist(width=self.s(SET_CTRL_W), height=self.s(30), tag="slider_vad")
        self._panel_targets.setdefault("settings", {})["slider_vad"] = self._slider_press
        self._draw_slider()

    def _settings_row_boost(self, width: int):
        with dpg.group(horizontal=True):
            dpg.add_spacer(width=self.s(16))
            with dpg.child_window(width=self.s(SET_LABEL_W), height=self.s(SETTINGS_ROW_H), border=False,
                                  no_scrollbar=True):
                self._bind_flat(dpg.last_item())
                dpg.add_spacer(height=self.s(22))
                dpg.add_text("Mic boost", color=COL_TEXT)
            with dpg.group():
                dpg.add_spacer(height=self.s(17))
                with dpg.child_window(tag="seg_boost", width=self.s(SET_CTRL_W), height=self.s(30), border=False,
                                      no_scrollbar=True, no_scroll_with_mouse=True):
                    self._bind_flat("seg_boost")
                    dpg.add_drawlist(width=self.s(SET_CTRL_W), height=self.s(30), tag="seg_boost_bg")
                    # Labels are text items placed over the track (drawlist text cannot use the SemiBold font).
                    label_font = self.font_seg or self.font_btn
                    seg_w = (SET_CTRL_W - 4 - 2 * 3) / 4.0
                    for i, db in enumerate(BOOST_DBS):
                        label = "0" if db == 0 else f"+{db}"
                        text_w = self._text_w(label, label_font)
                        text_h = self._text_h(label, label_font)
                        cx = 2 + i * (seg_w + 2) + seg_w / 2.0
                        dpg.add_text(label, tag=f"seg_lbl_{db}", color=COL_TEXT,
                                     pos=[self.s(cx) - text_w // 2, (self.s(30) - text_h) // 2])
                        if label_font:
                            dpg.bind_item_font(f"seg_lbl_{db}", label_font)
        self._panel_targets.setdefault("settings", {})["seg_boost"] = self._boost_press
        self._hit_parts["seg_boost"] = ["seg_boost_bg"]
        self._draw_boost()

    def _settings_row_switch(self, name: str, label: str, detail, on: bool, callback):
        with dpg.group(horizontal=True):
            dpg.add_spacer(width=self.s(16))
            with dpg.child_window(width=self.s(SET_LABEL_W), height=self.s(SETTINGS_ROW_H), border=False,
                                  no_scrollbar=True):
                self._bind_flat(dpg.last_item())
                dpg.add_spacer(height=self.s(10 if detail else 22))
                dpg.add_text(label, color=COL_TEXT)
                if detail:
                    dpg.add_text(detail, color=COL_TEXT_2, wrap=self.s(SET_LABEL_W))
                    dpg.bind_item_font(dpg.last_item(), self.font_notice)
            with dpg.group(horizontal=True):
                dpg.add_spacer(width=self.s(SET_CTRL_W - 42))
                with dpg.group() as switch_group:
                    dpg.add_spacer(height=self.s(19))
            self._make_switch(name, switch_group, on, callback)

    def _settings_row_learn(self, width: int, show: bool = False):
        with dpg.group(horizontal=True, show=show):
            dpg.add_spacer(width=self.s(16))
            dpg.add_text("Learn my room", color=COL_TEXT)
            dpg.add_button(label="Learn", enabled=False, tag="btn_room_learn")
            dpg.bind_item_theme("btn_room_learn", self.theme_voice_btn)

    def _help_steps(self, text_w: int):
        steps = (
            ("Pick your microphone", "The one you speak into. Getsu remembers it.", None),
            ("Turn on noise cancellation", "Most apps switch to your clean voice automatically.", None),
            ("Only if an app still hears noise",
             "Set its input to ", ("Default", "CABLE Output")),
        )
        for number, (title, desc, chips) in enumerate(steps, start=1):
            with dpg.group(horizontal=True):
                dpg.add_drawlist(width=self.s(22), height=self.s(22), tag=f"step_num_{number}")
                dpg.draw_circle((self.s(11), self.s(11)), self.s(11), color=COL_ACCENT_SOFT, fill=COL_ACCENT_SOFT,
                                parent=f"step_num_{number}")
                dpg.draw_text((self.s(7), self.s(4)), str(number), color=COL_ACCENT, size=self.s(12),
                              parent=f"step_num_{number}")
                dpg.add_spacer(width=self.s(12))
                with dpg.child_window(width=text_w, height=self.s(52), border=False, no_scrollbar=True):
                    self._bind_flat(dpg.last_item())
                    dpg.add_text(title, color=COL_TEXT)
                    dpg.bind_item_font(dpg.last_item(), self.font_btn)
                    if chips is None:
                        dpg.add_text(desc, color=COL_TEXT_2, wrap=text_w)
                        dpg.bind_item_font(dpg.last_item(), self.font_notice)
                    else:
                        with dpg.group(horizontal=True):
                            dpg.add_text(desc, color=COL_TEXT_2)
                            dpg.bind_item_font(dpg.last_item(), self.font_notice)
                            for k, chip in enumerate(chips):
                                self._chip(chip)
                                if k == 0:
                                    dpg.add_text(" or ", color=COL_TEXT_2)
                                    dpg.bind_item_font(dpg.last_item(), self.font_notice)
                        dpg.add_text("Turn off the app's own noise filter.", color=COL_TEXT_2, wrap=text_w)
                        dpg.bind_item_font(dpg.last_item(), self.font_notice)
            dpg.add_spacer(height=self.s(12))

    def _chip(self, text: str):
        """Code-styled chip (the mockup's <code>): a small card-coloured child window with the text inside."""
        with dpg.child_window(width=self._text_w(text, self.font_notice) + self.s(12), height=self.s(18),
                              border=False, no_scrollbar=True):
            if self.theme_chip:
                dpg.bind_item_theme(dpg.last_item(), self.theme_chip)
            dpg.add_text(text, color=COL_TEXT)
            dpg.bind_item_font(dpg.last_item(), self.font_notice)

    # --- settings callbacks (switch toggles go through the same handlers as before) ----

    def _on_auto_route_switch(self, on: bool):
        self.on_auto_route_toggled(None, on)

    def _on_music_mode_switch(self, on: bool):
        self.on_music_mode_toggled(None, on)

    def _set_voice_test_button_state(self, enabled: bool):
        """Enables or disables Hear myself. Disabled while the cable is missing or during a voice test."""
        if not self._ui_built or not dpg.does_item_exist("btn_voice_test"):
            return
        # The voice test records the microphone and plays it back on the speakers; it never uses the cable.
        installing = self._install_state == "installing"
        if installing:
            reason = "Wait for the cable install to finish."
        elif not enabled:
            reason = "A voice test or a start is already running."
        else:
            reason = "Record 11 seconds, then hear how others hear you"
        enabled = enabled and not installing
        self._set_button_enabled("btn_voice_test", enabled)
        self._set_button_label("btn_voice_test", "Hear myself")
        self._set_button_tip("btn_voice_test", reason)

    def build_ui(self, headless: bool = False):
        """
        Builds the windows and runs the UI loop. headless=True builds and lays out the windows, then returns
        without the tray, the window hook, the signal handlers or the loop (used by the click-path tests).
        """
        dpg.create_context()
        self._ui_built = True
        self._last_meter_t = 0.0

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

        # Type scale: the mockup's 11-17 px read too small on Windows (owner feedback), so every step is ~15-20 %
        # larger: 12, 14, 15, 16, 18 and 20 px. SemiBold for titles and buttons.
        with dpg.font_registry():
            if path_regular and os.path.exists(path_regular):
                bold_file = path_semibold if (path_semibold and os.path.exists(path_semibold)) else path_bold
                if not (bold_file and os.path.exists(bold_file)):
                    bold_file = path_regular
                self.font_body = dpg.add_font(path_regular, self.s(15))
                self.font_small = dpg.add_font(path_regular, self.s(12))
                self.font_footer = dpg.add_font(path_regular, self.s(13))
                self.font_seg = dpg.add_font(bold_file, self.s(13))
                self.font_notice = dpg.add_font(path_regular, self.s(14))
                self.font_list = dpg.add_font(path_regular, self.s(16))
                self.font_header = dpg.add_font(bold_file, self.s(16))
                self.font_btn = dpg.add_font(bold_file, self.s(15))
                self.font_title = dpg.add_font(bold_file, self.s(18))
                self.font_state = dpg.add_font(bold_file, self.s(20))
                dpg.bind_font(self.font_body)
            else:
                self.font_body = self.font_small = self.font_notice = None
                self.font_list = self.font_header = self.font_btn = None
                self.font_title = self.font_state = None
                self.font_footer = self.font_seg = None

        # Dark theme from the approved mockup palette. Rounding: cards 14, buttons 10, segmented 9.
        # Spacing is zero by default: every gap in the layout is an explicit spacer.
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
                dpg.add_theme_color(dpg.mvThemeCol_SliderGrab, COL_WHITE)
                dpg.add_theme_color(dpg.mvThemeCol_SliderGrabActive, COL_WHITE)
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
                dpg.add_theme_style(dpg.mvStyleVar_ItemSpacing, 0, 0)
                dpg.add_theme_style(dpg.mvStyleVar_WindowPadding, self.s(CONTENT_PAD), 0)
                dpg.add_theme_style(dpg.mvStyleVar_WindowBorderSize, 0)
                dpg.add_theme_style(dpg.mvStyleVar_ChildBorderSize, 0)

        def _child_theme(rounding, bg=None):
            with dpg.theme() as theme:
                with dpg.theme_component(dpg.mvChildWindow):
                    if bg is not None:
                        dpg.add_theme_color(dpg.mvThemeCol_ChildBg, bg)
                    dpg.add_theme_style(dpg.mvStyleVar_ChildRounding, self.s(rounding))
                    dpg.add_theme_style(dpg.mvStyleVar_WindowPadding, 0, 0)
                    dpg.add_theme_style(dpg.mvStyleVar_ItemSpacing, 0, 0)
                    dpg.add_theme_style(dpg.mvStyleVar_ChildBorderSize, 0)
            return theme

        self.theme_flat = _child_theme(0, (0, 0, 0, 0))
        self.theme_card = _child_theme(14, COL_CARD)
        self.theme_list = _child_theme(12, COL_CARD)
        self.theme_sheet = _child_theme(14, COL_WINDOW)
        # Icon buttons (tinted): normal, hovered and disabled fills, 10 px corners.
        self.theme_tint_normal = _child_theme(10, COL_ACCENT_SOFT)
        self.theme_tint_hover = _child_theme(10, COL_ACCENT_SOFT_2)
        self.theme_tint_disabled = _child_theme(10, COL_FILL)
        with dpg.theme() as self.theme_chip:
            with dpg.theme_component(dpg.mvChildWindow):
                dpg.add_theme_color(dpg.mvThemeCol_ChildBg, COL_CARD)
                dpg.add_theme_style(dpg.mvStyleVar_ChildRounding, self.s(5))
                dpg.add_theme_style(dpg.mvStyleVar_WindowPadding, self.s(5), self.s(2))

        with dpg.theme() as self.theme_scrim:
            with dpg.theme_component(dpg.mvAll):
                dpg.add_theme_color(dpg.mvThemeCol_WindowBg, (0, 0, 0, 140))
                dpg.add_theme_style(dpg.mvStyleVar_WindowPadding, 0, 0)
                dpg.add_theme_style(dpg.mvStyleVar_WindowBorderSize, 0)
                dpg.add_theme_style(dpg.mvStyleVar_WindowRounding, 0)

        dpg.bind_theme(global_theme)

        # Primary accent button: "Got it", install confirm. The running state uses the same accent.
        with dpg.theme() as self.theme_start_btn:
            with dpg.theme_component(dpg.mvButton):
                dpg.add_theme_color(dpg.mvThemeCol_Button, COL_ACCENT)
                dpg.add_theme_color(dpg.mvThemeCol_ButtonHovered, COL_ACCENT_HOVER)
                dpg.add_theme_color(dpg.mvThemeCol_ButtonActive, COL_ACCENT_PRESSED)
                dpg.add_theme_color(dpg.mvThemeCol_Text, COL_WHITE)
                dpg.add_theme_style(dpg.mvStyleVar_FrameRounding, self.s(10))
        self.theme_stop_btn = self.theme_start_btn

        # Link button (sponsor, Done): accent text, no background.
        with dpg.theme() as self.theme_link_btn:
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

        # Tinted button (Hear myself, Show settings, help): accent text on a soft accent fill.
        with dpg.theme() as self.theme_voice_btn:
            with dpg.theme_component(dpg.mvButton):
                dpg.add_theme_color(dpg.mvThemeCol_Button, COL_ACCENT_SOFT)
                dpg.add_theme_color(dpg.mvThemeCol_ButtonHovered, COL_ACCENT_SOFT_2)
                dpg.add_theme_color(dpg.mvThemeCol_ButtonActive, COL_ACCENT_SOFT_2)
                dpg.add_theme_color(dpg.mvThemeCol_Text, COL_ACCENT)
                dpg.add_theme_style(dpg.mvStyleVar_FrameBorderSize, 0.0)
                dpg.add_theme_style(dpg.mvStyleVar_FrameRounding, self.s(10))

        # Secondary button (Not now, Cancel): neutral fill.
        with dpg.theme() as self.theme_secondary_btn:
            with dpg.theme_component(dpg.mvButton):
                dpg.add_theme_color(dpg.mvThemeCol_Button, COL_FILL)
                dpg.add_theme_color(dpg.mvThemeCol_ButtonHovered, COL_FILL_2)
                dpg.add_theme_color(dpg.mvThemeCol_ButtonActive, COL_FILL_3)
                dpg.add_theme_color(dpg.mvThemeCol_Text, COL_TEXT)
                dpg.add_theme_style(dpg.mvStyleVar_FrameBorderSize, 0.0)
                dpg.add_theme_style(dpg.mvStyleVar_FrameRounding, self.s(10))

        # Main window: no title bar (the OS title bar is drawn in DWM colours), no resize, no move.
        width = self.s(372)
        self._click_targets = {}
        with dpg.window(tag="main_window", no_title_bar=True, no_resize=True, no_move=True,
                        no_scrollbar=True, no_scroll_with_mouse=True, no_collapse=True):
            self._build_hero(width)
            self._build_setup_card(width, self.s(HERO_H))
            self._build_mic_card(width)
            dpg.add_spacer(height=self.s(24))
            self._build_actions(width)
            dpg.add_spacer(height=self.s(24))
            self._build_footer(width)
            dpg.add_spacer(height=self.s(16))


        # Panels: built once, shown over the window by _open_panel.
        for name in ("mics", "settings", "help"):
            self._build_panel_shell(name)

        if self.font_body:
            dpg.bind_item_font("hero_state_text", self.font_state)
            dpg.bind_item_font("hero_sub_text", self.font_notice)

        # Viewport. Width is 420 px; height is fitted to the content after the first frames.
        vp_w = self.s(420)
        vp_h = self.s(640)
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
                work = mi.rcWork
            else:
                work = wintypes.RECT()
                user32.SystemParametersInfoW(0x0030, 0, ctypes.byref(work), 0)
        except Exception:
            work = wintypes.RECT(0, 0, user32.GetSystemMetrics(0) or 1920, user32.GetSystemMetrics(1) or 1080)
        work_w = work.right - work.left
        self._work_h = work.bottom - work.top
        x_pos = work.left + max(0, (work_w - vp_w) // 2)
        y_pos = work.top + max(0, (self._work_h - vp_h) // 2)

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
        with dpg.handler_registry():
            dpg.add_mouse_click_handler(button=dpg.mvMouseButton_Left, callback=self._on_mouse_click)
        dpg.setup_dearpygui()
        dpg.show_viewport()
        if self._start_minimized:
            # Hide before the first frames are drawn; the window is restored later by the tray or a second launch.
            hidden_hwnd = user32.FindWindowW(None, "Getsu - AI Noise Cancellation")
            if hidden_hwnd:
                user32.ShowWindow(hidden_hwnd, SW_HIDE)
        dpg.set_primary_window("main_window", True)

        # Render frames to settle layout, then fit the viewport height to the content.
        for _ in range(3):
            dpg.render_dearpygui_frame()
        self._fit_window_height()
        dpg.render_dearpygui_frame()

        # Synchronous dead-center window placement on active monitor work area (taskbar-aware)
        try:
            hwnd = user32.FindWindowW(None, "Getsu - AI Noise Cancellation")
            if hwnd:
                apply_dwm_title_bar(hwnd)
                rect = wintypes.RECT()
                user32.GetWindowRect(hwnd, ctypes.byref(rect))
                win_w = rect.right - rect.left
                win_h = rect.bottom - rect.top
                cx = work.left + max(0, (work_w - win_w) // 2)
                cy = work.top + max(0, (self._work_h - win_h) // 2)
                user32.SetWindowPos(hwnd, 0, cx, cy, 0, 0, SWP_NOSIZE | SWP_NOZORDER)
        except Exception as e:
            print(f"[WARN] Failed to center window: {e}")

        self._refresh_mic_widgets()
        self._update_cable_banner()

        self._card_sig = None
        self._poll_setup_card()
        if headless:
            return

        # Start tray icon & window hook
        self._start_tray_icon()
        threading.Thread(target=self._setup_window_hook, daemon=True).start()
        if self._resume_pending:
            threading.Thread(target=self._resume_filter_worker, daemon=True).start()

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

    def _fit_window_height(self):
        """
        Fits the viewport to the content: client width 420 logical px, height = footer bottom plus 16 px,
        capped at 640 px and at the work area. The frame (title bar, borders) is added back to both sizes.
        """
        self._place_footer()
        try:
            frame_w = dpg.get_viewport_width() - dpg.get_viewport_client_width()
            dpg.set_viewport_width(self.s(WINDOW_W) + frame_w)
            bottom = dpg.get_item_rect_max("footer_creator_text")[1]
            frame_h = dpg.get_viewport_height() - dpg.get_viewport_client_height()
            desired = int(bottom + self.s(16)) + frame_h
            cap = min(self.s(640), self._work_h)
            dpg.set_viewport_height(min(desired, cap))
            # Only when the work area is shorter than the content does the window scroll.
            dpg.configure_item("main_window", no_scrollbar=desired <= cap)
        except Exception as e:
            print(f"[WARN] Window fit failed: {e}")



def launch_gui(minimized: bool = False):
    app = GetsuGUI(minimized=minimized)
    app.build_ui()


if __name__ == "__main__":
    from src.single_instance import activate_existing_instance
    if activate_existing_instance():
        sys.exit(0)
    launch_gui()
