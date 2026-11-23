"""Hero block of the main window: power button, state text, sub line, level meter and notices."""

import math
import time
from typing import Optional, Sequence

import dearpygui.dearpygui as dpg
from src import ui_icons as icons

from src.audio_errors import explain_start_error
from src.gui_theme import (
    COL_ACCENT,
    COL_CARD,
    COL_FILL_2,
    COL_TEXT,
    COL_TEXT_2,
    COL_TEXT_3,
    COL_WARNING,
    COL_WHITE,
    CONTENT_W,
    HERO_H,
    HERO_TOP,
    POWER_D,
)


class HeroMixin:
    """Methods mixed into GetsuGUI (see src/gui.py)."""

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

    def _draw_power(self):
        """Power button: filled circle, power glyph drawn with polylines, ring when stopped, arc while busy."""
        if not dpg.does_item_exist("power_drawlist"):
            return
        dl = "power_drawlist"
        dpg.delete_item(dl, children_only=True)
        size = self.s(POWER_D)
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
        width = self.s(CONTENT_W)
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
        width = self.s(CONTENT_W)
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
        self._update_text_links()
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

    # --- main window -------------------------------------------------------

    def _build_hero(self, width: int):
        """Hero block (power, state, sub line, meter) in hero_box. The first-run card takes its place (see _build_setup_card)."""
        with dpg.child_window(tag="hero_box", width=width, height=self.s(HERO_H), border=False, no_scrollbar=True,
                              no_scroll_with_mouse=True):
            self._bind_flat("hero_box")
            dpg.add_spacer(height=self.s(HERO_TOP))
            with dpg.group(horizontal=True):
                dpg.add_spacer(width=(width - self.s(POWER_D)) // 2)
                dpg.add_drawlist(width=self.s(POWER_D), height=self.s(POWER_D), tag="power_drawlist")
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
