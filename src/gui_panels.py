"""Panel sheets (microphone, settings, help): shell, open and close rules, hit testing and clicks."""

import dearpygui.dearpygui as dpg
from src import ui_hit

from src.gui_theme import (
    ACTION_H,
    COL_ACCENT,
    COL_ACCENT_SOFT,
    COL_LINK_HOVER,
    COL_LINK_PRESSED,
    COL_TEXT,
    COL_TEXT_2,
    COL_WARNING,
    CONTENT_W,
    FEATURE_LEARN_ROOM,
    HELP_BODY_H,
    HELP_STEP_H,
    MIC_ROW_H,
    SETTINGS_ROW_H,
    SHEET_HEAD_H,
    WINDOW_W,
)


class PanelsMixin:
    """Methods mixed into GetsuGUI (see src/gui.py)."""

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
            dpg.configure_item("mic_list", height=max(self.s(MIC_ROW_H), sheet_h - self.s(SHEET_HEAD_H) - self.s(24)))
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
            content = rows * self.s(MIC_ROW_H) + max(0, rows - 1) + self.s(24)  # 8 px + 16 px below the list
            return min(vp_h, self.s(SHEET_HEAD_H) + content)
        if name == "settings":
            return min(vp_h, self.s(SHEET_HEAD_H) + self.s(4 * SETTINGS_ROW_H + 3) + self.s(24))
        return min(vp_h, self.s(SHEET_HEAD_H + HELP_BODY_H + 24))

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

    # --- panels -----------------------------------------------------------

    def _build_panel_shell(self, name: str):
        """Modal overlay: scrim (dim) above the window, and a sheet anchored to the bottom. Resized by _open_panel."""
        vp_w = self.s(WINDOW_W)
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

    def _sheet_head(self, title: str, done_callback, done_tag: str = "", title_font=None, panel: str = ""):
        width = self.s(CONTENT_W)
        title_font = title_font or self.font_title
        dpg.add_spacer(height=self.s(16))
        with dpg.group(horizontal=True):
            dpg.add_spacer(width=self.s(24))
            dpg.add_text(title, color=COL_TEXT)
            dpg.bind_item_font(dpg.last_item(), title_font)
            if done_callback is not None:
                done_w = self._text_w("Done", self.font_btn) + self.s(8)
                dpg.add_spacer(width=max(0, width - self._text_w(title, title_font) - done_w))
                self._text_link(done_tag or dpg.generate_uuid(), "Done", self.font_btn, done_w, self.s(26),
                                panel, done_callback)
        dpg.add_spacer(height=self.s(12))

    def _text_link(self, tag: str, label: str, font, width: int, height: int, panel: str, callback):
        """
        Text button with no fill or border in any state (Apple style). Hover lightens the text and press dims it
        (see _update_text_links). The child window is the click target, so the whole word is clickable.
        panel names the sheet the button is on; "" means the main window (clicks only while no panel is open).
        """
        with dpg.child_window(tag=tag, width=width, height=height, border=False, no_scrollbar=True,
                              no_scroll_with_mouse=True):
            self._bind_flat(tag)
            dpg.add_spacer(height=max(0, (height - self._text_h(label, font)) // 2))
            with dpg.group(horizontal=True):
                dpg.add_spacer(width=self.s(4))
                dpg.add_text(label, tag=f"{tag}_label", color=COL_ACCENT)
                if font:
                    dpg.bind_item_font(f"{tag}_label", font)
        # The child window sits inside the sheet, so its own position is relative to the sheet, not the viewport.
        # Measure the control by its text item instead, which reports a viewport rectangle (see _item_rect).
        self._hit_parts[tag] = [f"{tag}_label"]
        if panel:
            self._panel_targets.setdefault(panel, {})[tag] = callback
        else:
            self._click_targets[tag] = callback
        self._text_links[tag] = {"panel": panel, "label": f"{tag}_label", "state": None}

    def _update_text_links(self):
        """Per frame: text colour of the text buttons from the pointer (rest, hover or pressed). Only while their sheet is open (the main window: while no sheet is open)."""
        if not self._ui_built or not self._text_links:
            return
        point = dpg.get_mouse_pos(local=False)
        pressed = dpg.is_mouse_button_down(dpg.mvMouseButton_Left)
        for tag, meta in self._text_links.items():
            rect = self._item_rect(tag)
            on_screen = (self._open_panel_name is None) if not meta["panel"] else (self._open_panel_name == meta["panel"])
            inside = (on_screen and rect is not None and ui_hit.point_in_rect(point, rect))
            state = "rest" if not inside else ("pressed" if pressed else "hover")
            if state == meta["state"]:
                continue
            meta["state"] = state
            color = {"rest": COL_ACCENT, "hover": COL_LINK_HOVER, "pressed": COL_LINK_PRESSED}[state]
            if dpg.does_item_exist(meta["label"]):
                dpg.configure_item(meta["label"], color=color)

    def _panel_body(self, name: str):
        width = self.s(CONTENT_W)
        if name == "mics":
            self._sheet_head("Microphone", lambda: self._close_panels(), done_tag="sheet_done_mics", panel="mics")
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
            self._sheet_head("Settings", lambda: self._close_panels(), done_tag="sheet_done_settings",
                             title_font=self.font_set_title, panel="settings")
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
                with dpg.child_window(tag="help_body", width=width, height=self.s(HELP_BODY_H), border=False):
                    self._help_steps(width - self.s(34))
                    dpg.add_spacer(height=self.s(4))
                    # Clicks come from _on_mouse_click like every other panel control; the full-window panel takes
                    # the native click, so the button callback alone is not reliable.
                    dpg.add_button(tag="help_got_it", label="Got it", width=width, height=self.s(ACTION_H))
                    self._panel_targets.setdefault("help", {})["help_got_it"] = self._close_panels
                    dpg.bind_item_theme(dpg.last_item(), self.theme_start_btn)
                    dpg.bind_item_font(dpg.last_item(), self.font_btn)

    def _bind_list(self, tag: str):
        if self.theme_list and dpg.does_item_exist(tag):
            dpg.bind_item_theme(tag, self.theme_list)

    def _help_steps(self, text_w: int):
        steps = (
            ("Pick your microphone", "The one you speak into. Getsu remembers it.", None),
            ("Turn on noise cancellation", "Most apps switch to your clean voice automatically.", None),
            ("Only if an app still hears noise",
             "Set its input to ", ("Default", "CABLE Output")),
        )
        for number, (title, desc, chips) in enumerate(steps, start=1):
            with dpg.group(horizontal=True):
                # Number badge: the digit is centred in the circle by its measured size (Inter's figures sit in the
                # middle of the line box, so centring the line box centres the digit).
                badge = self.s(22)
                digit = str(number)
                digit_w, digit_h = self._text_w(digit, self.font_seg), self._glyph_h(digit, self.font_seg)
                dpg.add_drawlist(width=badge, height=badge, tag=f"step_num_{number}")
                dpg.draw_circle((badge / 2, badge / 2), badge / 2, color=COL_ACCENT_SOFT, fill=COL_ACCENT_SOFT,
                                parent=f"step_num_{number}")
                dpg.draw_text(((badge - digit_w) / 2, (badge - digit_h) / 2), digit, color=COL_ACCENT, size=digit_h,
                              parent=f"step_num_{number}", tag=f"step_num_{number}_digit")
                if self.font_seg:
                    dpg.bind_item_font(f"step_num_{number}_digit", self.font_seg)
                dpg.add_spacer(width=self.s(12))
                with dpg.child_window(width=text_w, height=self.s(HELP_STEP_H), border=False, no_scrollbar=True):
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
