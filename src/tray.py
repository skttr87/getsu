"""
System Tray User Interface for Getsu using pystray and Pillow.
Provides real-time tray menu, status indicator icon, and quick controls.
"""
import os
import threading
from typing import Optional
from PIL import Image, ImageDraw
import pystray
from pystray import MenuItem as item, Menu

from src.devices import (
    get_input_devices,
    get_output_devices,
    check_vbcable_status,
    install_vbcable_driver,
)
from src.config import save_config


def create_tray_image(status: str = "active") -> Image.Image:
    """
    Dynamically generates a 64x64 tray icon:
    - 'active'  : Bright Green dot (Denoising active)
    - 'bypass'  : Amber / Orange dot (Bypassed)
    - 'muted'   : Red dot (Muted)
    """
    size = (64, 64)
    image = Image.new("RGBA", size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)

    # Outer circle border
    draw.ellipse((4, 4, 60, 60), fill=(20, 24, 30, 230), outline=(50, 60, 75), width=2)

    # Status indicator core
    if status == "muted":
        color = (235, 60, 60, 255)      # Red
    elif status == "bypass":
        color = (245, 175, 45, 255)     # Amber
    else:
        color = (40, 215, 120, 255)     # Emerald Green

    draw.ellipse((16, 16, 48, 48), fill=color)
    return image


class TrayApp:
    """Manages the Windows system tray lifecycle and menu interactions."""

    def __init__(self, engine, config: dict):
        self.engine = engine
        self.config = config
        self._icon: Optional[pystray.Icon] = None
        self._thread: Optional[threading.Thread] = None

    def _get_status_str(self) -> str:
        if self.engine.is_muted:
            return "muted"
        if not self.engine.denoise_enabled:
            return "bypass"
        return "active"

    def _update_icon_image(self):
        if self._icon:
            status = self._get_status_str()
            self._icon.icon = create_tray_image(status)
            title = f"Getsu: {status.upper()}"
            if self.engine.is_muted:
                title += " (Muted)"
            elif not self.engine.denoise_enabled:
                title += " (Bypass)"
            else:
                title += " (Active - Cooling Pad Filter ON)"
            self._icon.title = title

    def _toggle_denoise(self, icon, item):
        self.engine.toggle_denoise()
        self.config['denoise_enabled'] = self.engine.denoise_enabled
        save_config(self.config)
        self._update_icon_image()

    def _toggle_high_pass(self, icon, item):
        self.engine.toggle_high_pass()
        self.config['high_pass_filter'] = self.engine.high_pass_enabled
        save_config(self.config)
        self._update_icon_image()

    def _toggle_mute(self, icon, item):
        self.engine.toggle_mute()
        self.config['mute'] = self.engine.is_muted
        save_config(self.config)
        self._update_icon_image()

    def _select_input_device(self, dev_id: int):
        def _handler(icon, item):
            print(f"[UI] Switching microphone to: [{dev_id}]")
            prev_id = self.engine.input_device
            self.engine.prepare_for_stop()
            self.engine.input_device = dev_id
            try:
                self.engine.start()
                self.config['input_device_id'] = dev_id
                save_config(self.config)
            except Exception as e:
                print(f"[UI] Failed to switch microphone to [{dev_id}]: {e}. Rolling back to [{prev_id}].")
                self.engine.input_device = prev_id
                try:
                    self.engine.start()
                except Exception:
                    pass
            self._icon.update_menu()
        return _handler

    def _select_output_device(self, dev_id: int):
        def _handler(icon, item):
            print(f"[UI] Switching output destination to: [{dev_id}]")
            prev_id = self.engine.output_device
            self.engine.prepare_for_stop()
            self.engine.output_device = dev_id
            try:
                self.engine.start()
                self.config['output_device_id'] = dev_id
                save_config(self.config)
            except Exception as e:
                print(f"[UI] Failed to switch output destination to [{dev_id}]: {e}. Rolling back to [{prev_id}].")
                self.engine.output_device = prev_id
                try:
                    self.engine.start()
                except Exception:
                    pass
            self._icon.update_menu()
        return _handler

    def _on_install_vbcable(self, icon, item):
        print("[UI] Launching VB-Audio Virtual Cable installer...")
        success = install_vbcable_driver()
        if success:
            print("[UI] VB-Cable installed! Please reboot or reload devices.")
        self._icon.update_menu()

    def _on_exit(self, icon, item):
        print("[UI] Exiting Getsu...")
        try:
            self.engine.prepare_for_stop()
        finally:
            icon.stop()

    def _build_menu(self) -> Menu:
        is_vbcable, _, _ = check_vbcable_status()

        # Input devices submenu
        inputs = get_input_devices()
        input_items = []
        for d in inputs:
            idx = d['index']
            label = f"{d['name']} ({d['hostapi']})"
            is_checked = (idx == self.engine.input_device)
            input_items.append(
                item(label, self._select_input_device(idx), checked=lambda it, c=is_checked: c)
            )

        # Output devices submenu
        outputs = get_output_devices()
        output_items = []
        for d in outputs:
            idx = d['index']
            label = f"{d['name']} ({d['hostapi']})"
            is_checked = (idx == self.engine.output_device)
            output_items.append(
                item(label, self._select_output_device(idx), checked=lambda it, c=is_checked: c)
            )

        version = "1.2.8"
        try:
            from src.config import DEFAULT_CONFIG
            version = DEFAULT_CONFIG.get("version", "1.2.8")
        except Exception:
            pass
        menu_entries = [
            item(f"Getsu AI Noise Cancellation v{version}", None, enabled=False),

            item(
                lambda text: f"Status: {self._get_status_str().upper()}",
                None,
                enabled=False
            ),
            pystray.Menu.SEPARATOR,
            item(
                "AI Noise Filter (RNNoise)",
                self._toggle_denoise,
                checked=lambda it: self.engine.denoise_enabled
            ),
            item(
                "Cooling Pad 80Hz Rumble Filter",
                self._toggle_high_pass,
                checked=lambda it: self.engine.high_pass_enabled
            ),
            item(
                "Mute Microphone",
                self._toggle_mute,
                checked=lambda it: self.engine.is_muted
            ),
            pystray.Menu.SEPARATOR,
            item("Microphone Input", Menu(*input_items)),
            item("Audio Output (CABLE / Headset)", Menu(*output_items)),
            pystray.Menu.SEPARATOR,
        ]

        if not is_vbcable:
            menu_entries.append(
                item("Install VB-Audio Cable Driver (1-Click)", self._on_install_vbcable)
            )
            menu_entries.append(pystray.Menu.SEPARATOR)

        menu_entries.append(item("Exit Getsu", self._on_exit))
        return Menu(*menu_entries)

    def run(self):
        """Runs the tray icon event loop."""
        initial_img = create_tray_image(self._get_status_str())
        self._icon = pystray.Icon("getsu", initial_img, "Getsu Real-Time Noise Suppression", self._build_menu())
        self._icon.run()
