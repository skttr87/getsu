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
import math
import threading
import time
import signal
import ctypes
from ctypes import wintypes
from typing import List, Dict, Optional
import webbrowser

import dearpygui.dearpygui as dpg
import sounddevice as sd
import pystray
from src import autostart, ui_type

from src.config import load_config, save_config
from src.frame_pacing import frame_sleep_s
from src.gui_common import APP_DIR, ICON_ICO_PATH, ICON_PNG_PATH, clean_device_label
from src.gui_devices import DevicesMixin, _GLYPH_FOR_KIND
from src.gui_hero import HeroMixin
from src.gui_panels import PanelsMixin
from src.gui_settings import SettingsMixin
from src.gui_setup import SetupMixin
from src.gui_theme import (
    ACTION_H,
    BOOST_DBS,
    COL_ACCENT,
    COL_ACCENT_HOVER,
    COL_ACCENT_PRESSED,
    COL_ACCENT_SOFT,
    COL_ACCENT_SOFT_2,
    COL_CARD,
    COL_CARD_HOVER,
    COL_ERROR,
    COL_FILL,
    COL_FILL_2,
    COL_FILL_3,
    COL_SEPARATOR,
    COL_TEXT,
    COL_TEXT_2,
    COL_TEXT_3,
    COL_WARNING,
    COL_WHITE,
    COL_WINDOW,
    CONTENT_PAD,
    CONTENT_W,
    FEATURE_EARBUD_HINT,
    FEATURE_LEARN_ROOM,
    HELP_BODY_H,
    HELP_STEP_H,
    HERO_H,
    HERO_TOP,
    MIC_LIST_TEXT_W,
    MIC_ROW_H,
    MIC_TEXT_W,
    POWER_D,
    SETTINGS_ROW_H,
    SETUP_BODY_INSTALLING,
    SETUP_BODY_OFFER,
    SET_CTRL_W,
    SET_LABEL_W,
    SHEET_HEAD_H,
    TITLE_BORDER_COLOR,
    TITLE_CAPTION_COLOR,
    TITLE_TEXT_COLOR,
    TEXT_LEAD,
    TYPE_BODY,
    TYPE_BUTTON,
    TYPE_FOOTER,
    TYPE_ROW,
    TYPE_SET_DETAIL,
    TYPE_SET_LABEL,
    TYPE_SET_SEG,
    TYPE_SET_TITLE,
    TYPE_SMALL,
    TYPE_STATE,
    TYPE_SUB,
    TYPE_TITLE,
    VAD_MAX,
    VAD_MIN,
    WINDOW_W,
)
from src.gui_tray import TRAY_BARS, TrayMixin, create_tray_icon_image, ensure_app_icon_ico
from src.gui_voice_test import VoiceTestMixin
from src.gui_widgets import WidgetsMixin, _Switch
from src.gui_win32 import (
    GWLP_WNDPROC,
    GWL_STYLE,
    SC_MAXIMIZE,
    SC_MINIMIZE,
    SWP_FRAMECHANGED,
    SWP_NOMOVE,
    SWP_NOSIZE,
    SWP_NOZORDER,
    SW_HIDE,
    SW_RESTORE,
    WM_CLOSE,
    WM_SYSCOMMAND,
    WNDPROC,
    WS_MAXIMIZEBOX,
    WS_THICKFRAME,
    Win32Mixin,
    apply_dwm_title_bar,
    force_foreground_window,
    init_dpi_awareness,
    user32,
)
from src.router import SmartMicRouter
from src.stream import AudioEngine, create_engine_from_config
from src.cable_guard import CableGuard


class GetsuGUI(
    Win32Mixin, TrayMixin, WidgetsMixin, DevicesMixin,
    HeroMixin, SetupMixin, VoiceTestMixin, PanelsMixin,
    SettingsMixin,
):

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
        self._btn_content: Dict[str, tuple] = {}  # icon button tag -> (width, icon, gap, font) for centring labels
        self._btn_heights: Dict[str, int] = {}
        self._btn_hovered: Dict[str, bool] = {}
        self._text_links: Dict[str, dict] = {}   # text button tag -> {panel, label, state}, see _text_link
        self._setup_dismissed = False            # "Not now" hides the first-run card for this session
        self._install_state: Optional[str] = None  # None, "installing" or "failed" (the card's own states)
        self._install_error = ""
        self._card_sig = None
        self._setup_anim_t = 0.0
        self._sub_full_text = ""
        self._vt_active = False
        self._vt_progress = 0.0
        self._meter_phase = 0.0
        self._power_hover = 0.0       # power button hover amount 0..1, eased (see HeroMixin._update_power_hover)
        self._power_pressed = False
        self._power_anim_t = None
        self._meter_level = 0.0
        self._meter_flat = True
        self._last_meter_t = 0.0
        self._work_h = 0
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
        self._boost_labels_pending = False     # boost labels are centred once the settings panel is rendered

        self._hwnd: Optional[int] = None
        self._old_wndproc = None
        self._wndproc_cb = None
        self._tray_icon: Optional[pystray.Icon] = None
        self._device_change_timer: Optional[threading.Timer] = None
        self._device_change_lock = threading.Lock()
        self._cable_guard = CableGuard(self._cable_guard_gather, self._cable_guard_apply, self._cable_guard_notify)

        self._refresh_devices()

        # Windows may have picked the cable as default at startup (after a headset was unplugged while Getsu was closed).
        self._cable_guard.request("startup", delay=1.0)

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
        self._cable_guard.cancel()
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
                    # The microphone is restored now; check that Windows did not leave the cable as default.
                    self._cable_guard.request("stop", delay=0.0)
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

    def _build_actions(self, width: int):
        help_w = self.s(44)
        btn_w = (width - self.s(16) - help_w) // 2
        btn_h = self.s(ACTION_H)
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
                # Text link like Done: no fill in any state, the text lightens on hover (see _text_link).
                self._text_link("sponsor_link_btn", "Kopi Reman", self.font_footer,
                                self._text_w("Kopi Reman", self.font_footer) + self.s(8),
                                self._text_h("Kopi Reman", self.font_footer), "",
                                lambda: webbrowser.open("https://www.instagram.com/kopireman/"))

    def _place_footer(self):
        """Right-aligns the sponsor group: the gap is the content width minus the two groups as rendered."""
        try:
            left = dpg.get_item_rect_size("footer_creator_text")[0]
            right = dpg.get_item_rect_size("footer_sponsor")[0]
            dpg.configure_item("footer_gap", width=max(0, self.s(CONTENT_W) - left - right))
        except Exception as e:
            print(f"[WARN] Footer layout failed: {e}")

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

        # Type scale: CSS sizes (TYPE_*) converted to DearPyGui sizes. The factor comes from the font file in use,
        # so a fallback font gets its own factor. Unreadable metrics keep the sizes unconverted.
        line_factor = 1.0
        try:
            line_factor = ui_type.read_line_height_factor(path_regular)
        except (OSError, ValueError) as e:
            print(f"[WARN] Font metrics unreadable, sizes are not converted: {e}")
        print(f"[GUI] Font line-height factor: {line_factor:.4f} (DPI scale {self.dpi_scale:.2f})")

        def fpx(css_px):
            return ui_type.font_px(css_px, line_factor, self.dpi_scale)

        with dpg.font_registry():
            if path_regular and os.path.exists(path_regular):
                bold_file = path_semibold if (path_semibold and os.path.exists(path_semibold)) else path_bold
                if not (bold_file and os.path.exists(bold_file)):
                    bold_file = path_regular
                self.font_body = dpg.add_font(path_regular, fpx(TYPE_BODY))
                self.font_small = dpg.add_font(path_regular, fpx(TYPE_SMALL))
                self.font_footer = dpg.add_font(path_regular, fpx(TYPE_FOOTER))
                self.font_seg = dpg.add_font(bold_file, fpx(TYPE_SUB))
                self.font_notice = dpg.add_font(path_regular, fpx(TYPE_SUB))
                self.font_list = dpg.add_font(bold_file, fpx(TYPE_ROW))
                self.font_header = dpg.add_font(bold_file, fpx(TYPE_ROW))
                self.font_btn = dpg.add_font(bold_file, fpx(TYPE_BUTTON))
                self.font_title = dpg.add_font(bold_file, fpx(TYPE_TITLE))
                self.font_state = dpg.add_font(bold_file, fpx(TYPE_STATE))
                self.font_set_title = dpg.add_font(bold_file, fpx(TYPE_SET_TITLE))
                self.font_set_label = dpg.add_font(path_regular, fpx(TYPE_SET_LABEL))
                self.font_set_detail = dpg.add_font(path_regular, fpx(TYPE_SET_DETAIL))
                self.font_set_seg = dpg.add_font(bold_file, fpx(TYPE_SET_SEG))
                dpg.bind_font(self.font_body)
            else:
                self.font_body = self.font_small = self.font_notice = None
                self.font_list = self.font_header = self.font_btn = None
                self.font_title = self.font_state = None
                self.font_footer = self.font_seg = None
                self.font_set_title = self.font_set_label = self.font_set_detail = self.font_set_seg = None

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
                # DearPyGui places a text item FramePadding.y below the layout cursor and reserves the same below it
                # (ImGui default 3 px). Used here as the line leading (TEXT_LEAD); _text_h includes it, so layouts
                # that centre text by _text_h stay centred and stacked lines get the design's spacing.
                dpg.add_theme_style(dpg.mvStyleVar_FramePadding, self.s(4), self.s(TEXT_LEAD))
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

        # Viewport. Width is WINDOW_W; height is fitted to the content after the first frames.
        vp_w = self.s(WINDOW_W)
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
        # DearPyGui builds the font atlas on the first frame. Text is measured by the layout below, so one
        # frame is drawn here first.
        dpg.render_dearpygui_frame()

        # Main window: no title bar (the OS title bar is drawn in DWM colours), no resize, no move.
        width = self.s(CONTENT_W)
        self._click_targets = {}
        with dpg.window(tag="main_window", no_title_bar=True, no_resize=True, no_move=True,
                        no_scrollbar=True, no_scroll_with_mouse=True, no_collapse=True):
            self._build_hero(width)
            self._build_setup_card(width, self.s(HERO_H))
            dpg.add_spacer(height=self.s(20))  # 20 px from the hero or first-run slot to the microphone card
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
        Fits the viewport to the content: client width WINDOW_W logical px, height = footer bottom plus 16 px,
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
