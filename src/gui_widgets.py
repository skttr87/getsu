"""Drawn controls of the main window and panels: switches, text measuring, icon buttons and device glyphs."""

import dearpygui.dearpygui as dpg
from src import ui_icons as icons, ui_hit

from src.gui_theme import (
    COL_ACCENT,
    COL_CARD,
    COL_FILL_2,
    COL_SEPARATOR,
    COL_TEXT,
    COL_TEXT_3,
    COL_WHITE,
    MIC_ROW_H,
    TEXT_LEAD,
)


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


class WidgetsMixin:
    """Methods mixed into GetsuGUI (see src/gui.py)."""

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
        y0 = (self.s(MIC_ROW_H) - size) // 2  # the tile sits in the middle of the microphone row
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

    def _text_h(self, text: str, font=None, wrap: int = -1) -> int:
        """
        Height a text item takes in the layout, in physical pixels: its lines (wrapped at wrap px if given) plus the
        leading above and below (TEXT_LEAD, applied by DearPyGui as FramePadding.y). Use _glyph_h for drawlist text.
        """
        return self._glyph_h(text, font, wrap) + 2 * self.s(TEXT_LEAD)

    def _glyph_h(self, text: str, font=None, wrap: int = -1) -> int:
        """Height of the text lines themselves in physical pixels, without leading (falls back to an estimate)."""
        try:
            return int(dpg.get_text_size(text or " ", wrap_width=wrap, font=font or 0)[1])
        except Exception:
            return self.s(16)

    def _block_top(self, box_h: int, *line_heights: int) -> int:
        """Top offset in physical pixels that centres a stack of text lines in a box of box_h physical pixels."""
        return max(0, (box_h - sum(line_heights)) // 2)

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
                dpg.add_spacer(width=pad, tag=f"{tag}_pad")
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
        self._btn_content[tag] = (width, icon, gap, font)
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
        """Changes an icon button's label and centres the icon and label again (the progress labels are longer)."""
        if dpg.does_item_exist(f"{tag}_label"):
            dpg.set_value(f"{tag}_label", text)
        meta = self._btn_content.get(tag)
        if meta is not None and dpg.does_item_exist(f"{tag}_pad"):
            width, icon, gap, font = meta
            content_w = icon + gap + self._text_w(text, font)
            dpg.configure_item(f"{tag}_pad", width=max(0, (width - content_w) // 2))

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

    def _divider(self, width: int, show: bool = True):
        with dpg.group(horizontal=True, show=show):
            dpg.add_spacer(width=self.s(16))
            dpg.add_drawlist(width=width - self.s(16), height=1)
            dpg.draw_line((0, 0), (width - self.s(32), 0), color=COL_SEPARATOR, thickness=1.0, parent=dpg.last_item())

    def _chip(self, text: str):
        """
        Code-styled chip (the mockup's <code>): card-coloured rounded box with the text centred, drawn in one drawlist.
        The box is one text line high, so the chip text shares the baseline of the text around it (Inter's capitals
        sit in the middle of its line box). A borderless child window ignores its padding, so it is not used here.
        """
        font = self.font_notice
        pad = self.s(6)
        text_w, text_h = self._text_w(text, font), self._glyph_h(text, font)
        lead = self.s(TEXT_LEAD)  # the drawlist replaces a text item, so it takes the same leading as the text around it
        width = text_w + 2 * pad
        with dpg.drawlist(width=width, height=text_h + 2 * lead):
            dpg.draw_rectangle((0, lead), (width, lead + text_h), color=COL_CARD, fill=COL_CARD, rounding=self.s(5))
            label = dpg.draw_text((pad, lead), text, color=COL_TEXT, size=text_h)
            if font:
                dpg.bind_item_font(label, font)
