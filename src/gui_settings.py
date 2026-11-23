"""Settings panel: Sensitivity knob, mic boost, switches and the settings callbacks."""

import math
import threading

import dearpygui.dearpygui as dpg
import sounddevice as sd
from src import ui_hit

from src.config import save_config
from src.devices import is_laptop_microphone
from src.gui_theme import (
    BOOST_DBS,
    COL_ACCENT,
    COL_ACCENT_SOFT,
    COL_FILL,
    COL_FILL_2,
    COL_TEXT,
    COL_TEXT_2,
    COL_WARNING,
    COL_WHITE,
    SETTINGS_ROW_H,
    SET_CTRL_W,
    SET_LABEL_W,
    VAD_MAX,
    VAD_MIN,
)


class SettingsMixin:
    """Methods mixed into GetsuGUI (see src/gui.py)."""

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

    # Settings rows: label column (SET_LABEL_W, detail text wraps inside it) and control column (SET_CTRL_W).

    def _settings_row_slider(self, width: int):
        self._vad_value = float(self.config.get("vad_threshold", 0.70))
        row_h = self.s(SETTINGS_ROW_H)
        with dpg.group(horizontal=True):
            dpg.add_spacer(width=self.s(16))
            with dpg.child_window(width=self.s(SET_LABEL_W), height=row_h, border=False, no_scrollbar=True):
                self._bind_flat(dpg.last_item())
                # Label plus a two-line detail, centred in the row.
                dpg.add_spacer(height=self._block_top(row_h, self._text_h("Ag", self.font_set_label),
                                                      2 * self._text_h("Ag", self.font_set_detail)))
                dpg.add_text("Sensitivity", color=COL_TEXT)
                dpg.bind_item_font(dpg.last_item(), self.font_set_label)
                dpg.add_text("Higher ignores more background voices", color=COL_TEXT_2,
                             wrap=self.s(SET_LABEL_W))
                dpg.bind_item_font(dpg.last_item(), self.font_set_detail)
            with dpg.group():
                dpg.add_spacer(height=self._block_top(row_h, self.s(30)))
                dpg.add_drawlist(width=self.s(SET_CTRL_W), height=self.s(30), tag="slider_vad")
        self._panel_targets.setdefault("settings", {})["slider_vad"] = self._slider_press
        self._draw_slider()

    def _settings_row_boost(self, width: int):
        row_h = self.s(SETTINGS_ROW_H)
        with dpg.group(horizontal=True):
            dpg.add_spacer(width=self.s(16))
            with dpg.child_window(width=self.s(SET_LABEL_W), height=row_h, border=False, no_scrollbar=True):
                self._bind_flat(dpg.last_item())
                dpg.add_spacer(height=self._block_top(row_h, self._text_h("Ag", self.font_set_label)))
                dpg.add_text("Mic boost", color=COL_TEXT)
                dpg.bind_item_font(dpg.last_item(), self.font_set_label)
            with dpg.group():
                dpg.add_spacer(height=self._block_top(row_h, self.s(30)))
                with dpg.child_window(tag="seg_boost", width=self.s(SET_CTRL_W), height=self.s(30), border=False,
                                      no_scrollbar=True, no_scroll_with_mouse=True):
                    self._bind_flat("seg_boost")
                    dpg.add_drawlist(width=self.s(SET_CTRL_W), height=self.s(30), tag="seg_boost_bg")
                    # Labels are text items placed over the track (drawlist text cannot use the SemiBold font).
                    label_font = self.font_set_seg or self.font_btn
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
        row_h = self.s(SETTINGS_ROW_H)
        with dpg.group(horizontal=True):
            dpg.add_spacer(width=self.s(16))
            with dpg.child_window(width=self.s(SET_LABEL_W), height=row_h, border=False, no_scrollbar=True):
                self._bind_flat(dpg.last_item())
                lines = [self._text_h("Ag", self.font_set_label)]
                if detail:
                    lines.append(2 * self._text_h("Ag", self.font_set_detail))  # details wrap to two lines
                dpg.add_spacer(height=self._block_top(row_h, *lines))
                dpg.add_text(label, color=COL_TEXT)
                dpg.bind_item_font(dpg.last_item(), self.font_set_label)
                if detail:
                    dpg.add_text(detail, color=COL_TEXT_2, wrap=self.s(SET_LABEL_W))
                    dpg.bind_item_font(dpg.last_item(), self.font_set_detail)
            with dpg.group(horizontal=True):
                dpg.add_spacer(width=self.s(SET_CTRL_W - 42))
                with dpg.group() as switch_group:
                    dpg.add_spacer(height=self._block_top(row_h, self.s(26)))
            self._make_switch(name, switch_group, on, callback)

    def _settings_row_learn(self, width: int, show: bool = False):
        with dpg.group(horizontal=True, show=show):
            dpg.add_spacer(width=self.s(16))
            dpg.add_text("Learn my room", color=COL_TEXT)
            dpg.bind_item_font(dpg.last_item(), self.font_set_label)
            dpg.add_button(label="Learn", enabled=False, tag="btn_room_learn")
            dpg.bind_item_theme("btn_room_learn", self.theme_voice_btn)

    # --- settings callbacks (switch toggles go through the same handlers as before) ----

    def _on_auto_route_switch(self, on: bool):
        self.on_auto_route_toggled(None, on)

    def _on_music_mode_switch(self, on: bool):
        self.on_music_mode_toggled(None, on)
