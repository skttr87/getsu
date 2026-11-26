"""First-run card for the virtual cable: states, install thread and the cable banner."""

import time

import dearpygui.dearpygui as dpg

from src.devices import install_vbcable_driver
from src.ui_queue import ui_only
from src.gui_theme import (
    ACTION_H,
    COL_ACCENT,
    COL_ERROR,
    COL_FILL,
    COL_TEXT,
    COL_TEXT_2,
    COL_WHITE,
    CONTENT_W,
    SETUP_BODY_INSTALLING,
    SETUP_BODY_OFFER,
)


class SetupMixin:
    """Methods mixed into GetsuGUI (see src/gui.py)."""

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
            if success:
                # Getsu installed the cable: its endpoints are hidden while Getsu is closed (cable_visibility.py).
                from src.cable_visibility import mark_cable_installed_by_getsu
                mark_cable_installed_by_getsu(self.config)

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

        self._start_thread(worker)

    @ui_only
    def _update_cable_banner(self):
        """Refreshes the voice test enable state and the idle notice (the status card was removed in phase B)."""
        self._set_voice_test_button_state(not self.is_running)
        if not self.is_running and self._busy is None:
            self.set_status_pill(self._get_idle_status(), self._get_idle_color())

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
            dpg.add_spacer(height=self.s(16))  # the card starts at the top of the power button
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
                # The gap before the progress bar and the gap before the buttons are shown by state (see
                # _apply_setup_card), so each state has one 12 px gap and the card keeps its bottom padding.
                dpg.add_spacer(height=self.s(12), tag="setup_gap_progress")
                with dpg.group(horizontal=True):
                    dpg.add_spacer(width=pad)
                    dpg.add_drawlist(width=inner_w, height=self.s(4), tag="setup_progress")
                dpg.add_spacer(height=self.s(12), tag="setup_gap_buttons")
                with dpg.group(horizontal=True, tag="setup_buttons"):
                    dpg.add_spacer(width=pad)
                    half_w = (inner_w - self.s(8)) // 2
                    dpg.add_button(label="Install cable", tag="setup_install", width=half_w, height=self.s(ACTION_H),
                                   callback=lambda: self.run_driver_install_thread())
                    dpg.bind_item_theme("setup_install", self.theme_start_btn)
                    dpg.bind_item_font("setup_install", self.font_btn)
                    dpg.add_spacer(width=self.s(8))
                    dpg.add_button(label="Not now", tag="setup_skip", width=inner_w - half_w - self.s(8),
                                   height=self.s(ACTION_H), callback=self.on_setup_skip_click)
                    dpg.bind_item_theme("setup_skip", self.theme_secondary_btn)
                    dpg.bind_item_font("setup_skip", self.font_btn)

    def _card_visible(self) -> bool:
        """The first-run card shows while the cable is missing (unless dismissed), and during an install or after one failed."""
        if self.is_vbcable_installed and self._install_state != "installing":
            return False
        if self._install_state in ("installing", "failed"):
            return True
        return not self.is_vbcable_installed and not self._setup_dismissed

    @ui_only
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

    @ui_only
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
        dpg.configure_item("setup_gap_progress", show=state == "installing")
        dpg.configure_item("setup_gap_buttons", show=state != "installing")
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
