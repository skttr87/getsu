"""Microphone detection and selection, the microphone card and list, and hardware change handling."""

import threading
import time
from typing import List, Dict, Optional

import dearpygui.dearpygui as dpg
import sounddevice as sd
from src import ui_icons as icons

from src.config import save_config
from src.device_labels import display_titles, subtitle
from src.devices import (
    get_input_devices,
    auto_select_input_device,
    check_vbcable_status,
    auto_select_output_device,
    find_matching_cable_input,
    classify_device,
    DeviceKind,
)
from src.gui_common import clean_device_label
from src.gui_theme import (
    COL_ACCENT,
    COL_ERROR,
    COL_SEPARATOR,
    COL_TEXT,
    COL_TEXT_2,
    COL_TEXT_3,
    COL_WARNING,
    COL_WHITE,
    CONTENT_W,
    FEATURE_EARBUD_HINT,
    MIC_LIST_TEXT_W,
    MIC_ROW_H,
    MIC_TEXT_W,
)
from src.host_api_resolver import selection_hint
from src.router import clear_device_bus_type_cache
from src.stream import create_engine_from_config
from src.cable_guard import (
    DEVICE_CHANGE,
    DEVICE_CHANGE_DELAY_S,
    apply_decision,
    gather_state,
)
from src import win_audio

DEFAULT_POLL_EVERY_S = 5  # watchdog ticks (seconds) between reads of the Windows default mic and playback ids


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


class DevicesMixin:
    """Methods mixed into GetsuGUI (see src/gui.py)."""

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
        ticks = 0
        while self._app_running:
            time.sleep(1.0)
            ticks += 1
            if ticks % DEFAULT_POLL_EVERY_S == 0:
                self._poll_default_change()
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

    def _cable_guard_gather(self):
        """Current Windows defaults and Getsu state for the cable guard (read-only, see src/cable_guard.py)."""
        raw_name = self.device_raw.get(self.selected_input_name) if self.selected_input_name else None
        return gather_state(
            self.config,
            raw_name,
            nc_running=self.is_running,
            voice_test_running=self._is_testing_voice,
            swap_in_progress=(self._busy is not None or self._is_rescanning or self._resume_pending),
        )

    def _poll_default_change(self):
        """
        Reads the Windows default mic and playback ids (read-only) and runs the cable guard when either changed.
        A default changed in Windows Sound settings, or picked by Windows itself, sends no WM_DEVICECHANGE, so the
        device-change trigger alone misses it. The first poll only records the ids.
        """
        try:
            ids = (win_audio.native_get_current_default_mic()[0], win_audio.native_get_current_default_playback()[0])
        except Exception:
            return
        previous = getattr(self, "_last_default_ids", None)
        self._last_default_ids = ids
        if previous is not None and ids != previous:
            self._cable_guard.request("default change", delay=0.5)

    def _cable_guard_apply(self, decision) -> bool:
        return apply_decision(self.router, decision)

    def _cable_guard_notify(self, text: str, warn: bool):
        if not self._ui_built or not self._app_running:
            return
        self.set_status_pill(text, COL_WARNING if warn else COL_TEXT_2)

    def _on_device_change_event(self):
        """Debounced listener for Windows WM_DEVICECHANGE hotplug signals."""
        clear_device_bus_type_cache()
        # Windows picks its own replacement default asynchronously; the guard runs a little later.
        self._cable_guard.request(DEVICE_CHANGE, delay=DEVICE_CHANGE_DELAY_S)
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

    # --- microphone entries and widgets ----------------------------------

    def _mic_entries(self) -> List[Dict]:
        """One entry per detected microphone: label (for selection), title, subtitle, kind, full name."""
        raw_of = {label: self.device_raw.get(label, label) for label in self.device_map}
        kinds = {raw: classify_device(raw, is_input=True) for raw in raw_of.values()}
        titles = display_titles(raw_of.values())
        entries = []
        for label, idx in self.device_map.items():
            raw = raw_of[label]
            kind = kinds[raw]
            entries.append({
                "label": label,
                "index": idx,
                "raw": raw,
                "title": titles[raw],
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
        width = self.s(MIC_TEXT_W)
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
        # Drop only the old rows: the panel's other controls (the Done text button) stay registered.
        targets = self._panel_targets.setdefault("mics", {})
        for old in [t for t in targets if t.startswith("mic_row_")]:
            del targets[old]
            self._hit_parts.pop(old, None)
        entries = self._mic_entries()
        current = self._get_selected_input_label()
        row_w = self.s(CONTENT_W)
        row_h = self.s(MIC_ROW_H)
        label_w = self.s(MIC_LIST_TEXT_W)
        icon_y = (row_h - self.s(16)) // 2  # the 16 px icons and tick sit in the middle of the row
        for i, entry in enumerate(entries):
            tag = f"mic_row_{i}"
            with dpg.child_window(tag=tag, parent="mic_list", width=row_w, height=row_h, border=False,
                                  no_scrollbar=True, no_scroll_with_mouse=True):
                self._bind_flat(tag)
                with dpg.group(horizontal=True):
                    dpg.add_spacer(width=self.s(16))
                    icon_dl = f"{tag}_icon"
                    dpg.add_drawlist(width=self.s(16), height=row_h, tag=icon_dl)
                    self._draw_device_glyph(icon_dl, entry["glyph"], self.s(0), icon_y, self.s(16), COL_TEXT_2)
                    dpg.add_spacer(width=self.s(12))
                    with dpg.child_window(width=label_w, height=row_h, border=False, no_scrollbar=True):
                        self._bind_flat(f"{tag}_label")
                        dpg.add_spacer(height=self._block_top(row_h, self._text_h("Ag", self.font_list),
                                                              self._text_h("Ag", self.font_notice)))
                        dpg.add_text(self._fit_text(entry["title"], self.font_list, label_w), color=COL_TEXT)
                        dpg.bind_item_font(dpg.last_item(), self.font_list)
                        dpg.add_text(self._fit_text(entry["subtitle"], self.font_notice, label_w), color=COL_TEXT_2)
                        dpg.bind_item_font(dpg.last_item(), self.font_notice)
                    dpg.add_spacer(width=self.s(12))
                    tick_dl = f"{tag}_tick"
                    dpg.add_drawlist(width=self.s(16), height=row_h, tag=tick_dl)
                    self._hit_parts[tag] = [icon_dl, tick_dl]
                    if entry["label"] == current:
                        dpg.draw_polyline(icons.to_px(icons.CHECK, 0, icon_y, self.s(16)), color=COL_ACCENT,
                                          thickness=float(self.s(2)), parent=tick_dl)
            self._panel_targets["mics"][tag] = (lambda lab=entry["label"]: self._select_mic_from_panel(lab))
            if i < len(entries) - 1:
                div = f"{tag}_div"
                div_w = self.s(CONTENT_W - 32)
                with dpg.group(horizontal=True, parent="mic_list"):
                    dpg.add_spacer(width=self.s(16))
                    dpg.add_drawlist(width=div_w, height=1, tag=div)
                    dpg.draw_line((0, 0), (div_w, 0), color=COL_SEPARATOR, thickness=1.0, parent=div)
        if not entries:
            dpg.add_text("No microphone detected.", color=COL_TEXT_2, parent="mic_list")

    def _build_mic_card(self, width: int):
        hint_h = self.s(28) if FEATURE_EARBUD_HINT else 0
        row_h = self.s(MIC_ROW_H)
        card_h = row_h + hint_h
        with dpg.child_window(tag="mic_card", width=width, height=card_h, border=False, no_scrollbar=True):
            self._bind_card("mic_card")
            with dpg.child_window(tag="mic_row", width=width, height=row_h, border=False,
                                  no_scrollbar=True, no_scroll_with_mouse=True):
                self._bind_flat("mic_row")
                with dpg.group(horizontal=True):
                    dpg.add_spacer(width=self.s(16))
                    dpg.add_drawlist(width=self.s(32), height=row_h, tag="mic_tile")
                    dpg.add_spacer(width=self.s(12))
                    with dpg.child_window(width=self.s(MIC_TEXT_W), height=row_h, border=False, no_scrollbar=True):
                        self._bind_flat("mic_label_box")
                        dpg.add_spacer(height=self._block_top(row_h, self._text_h("Ag", self.font_header),
                                                              self._text_h("Ag", self.font_notice)))
                        dpg.add_text("", tag="mic_row_title", color=COL_TEXT)
                        dpg.bind_item_font("mic_row_title", self.font_header)
                        dpg.add_text("", tag="mic_row_sub", color=COL_TEXT_2)
                        dpg.bind_item_font("mic_row_sub", self.font_notice)
                    dpg.add_drawlist(width=self.s(16), height=row_h, tag="mic_chev")
                    dpg.draw_polyline(icons.to_px(icons.CHEVRON, 0, (row_h - self.s(16)) // 2, self.s(16)),
                                      color=COL_TEXT_3, thickness=float(self.s(2)), parent="mic_chev")
            self._click_targets["mic_row"] = self._open_mic_panel_from_row
            self._hit_parts["mic_row"] = ["mic_tile", "mic_chev"]
            with dpg.group(horizontal=True, tag="hint_row", show=FEATURE_EARBUD_HINT):
                dpg.add_spacer(width=self.s(16))
                dpg.add_text("Earbud mic drops audio to call quality.", color=COL_TEXT_2, tag="hint_text")
                dpg.bind_item_font("hint_text", self.font_notice)
                self._text_link("btn_use_builtin", "Use built-in mic", self.font_body,
                                self._text_w("Use built-in mic", self.font_body) + self.s(8),
                                self._text_h("Use built-in mic", self.font_body), "", self._use_builtin_mic)

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
