"""
Device events while noise cancellation runs or is stopped (owner decision 2026-11-23, option a): a newly plugged USB
or wired headset is selected and switched to, the default capture is put back on CABLE Output after a re-scan, an
unrelated device event changes nothing, a headset that does not open falls back to the previous microphone, and a
removed microphone falls back by the existing ranking.

The Windows reads (endpoint listing, PortAudio, the bus and form factor readers) and the engine are replaced by
mocks. The GUI object is a real GetsuGUI whose window is not built, so no DearPyGui call is made.
"""
import threading
import unittest
from unittest.mock import MagicMock, patch

import _isolation  # noqa: F401

from src import devices as devices_mod
from src import gui as g
from src import gui_devices
from src import router as router_mod
from src.devices import DeviceKind, auto_select_input_device
from src.router import SmartMicRouter
from src.ui_queue import UiQueue

CABLE_GUID = "{cable-guid}"
REALTEK = "Microphone (Realtek(R) Audio)"
MPOW = "Microphone (2- MPOW Wireless Gaming Headset)"
USB_MIC = "Microphone (USB Studio Mic)"
BT = "Headset (HUAWEI FreeBuds SE 4 ANC)"
STICK = "USB Mass Storage"


def _kind(name, is_input=True):
    if "MPOW" in name:
        return DeviceKind.USB_HEADSET
    if "FreeBuds" in name:
        return DeviceKind.BLUETOOTH_HEADSET
    if "USB Studio" in name:
        return DeviceKind.USB_MIC
    if "Realtek" in name:
        return DeviceKind.BUILT_IN
    return DeviceKind.OTHER


class _Endpoints:
    """What Windows lists as active audio endpoints (capture and render)."""

    def __init__(self):
        self.capture = {}
        self.render = {}

    def listing(self, is_input=True):
        source = self.capture if is_input else self.render
        return [{"id": eid, "name": name} for eid, name in source.items()]


class DeviceEventTests(unittest.TestCase):

    def setUp(self):
        self.endpoints = _Endpoints()
        self.endpoints.render["speakers"] = "Speakers (Realtek(R) Audio)"  # a PC always has a playback endpoint
        self.inputs = []  # PortAudio input list, as get_input_devices() returns it
        self.engines = []
        self.start_failures = set()  # raw names whose engine fails to open

        self.router = MagicMock(spec=SmartMicRouter)
        self.router.is_swapped = True
        self.router.reassert_cable_default.return_value = None
        self.router.restore_original.return_value = True

        self.app = object.__new__(g.GetsuGUI)
        app = self.app
        app.config = {"auto_route": True}
        app.router = self.router
        app.engine = None
        app.is_running = False
        app._busy = None
        app._is_rescanning = False
        app._device_change_lock = threading.Lock()
        app._state_lock = threading.Lock()
        app._app_running = True
        app._ui_built = False
        app._ui_queue = UiQueue()
        app._ui_thread = None
        app._tray_icon = None
        app._pending_headset = None
        app._endpoint_state = None
        app._open_panel_name = None
        app._format_audio_error = lambda e: f"Input failed ({e})"
        app.device_map = {}
        app.device_raw = {}
        app.input_devices = []
        app.selected_input_idx = None
        app.selected_input_name = None
        app.selected_output_idx = 99
        app.selected_output_name = "CABLE Input"
        app.is_vbcable_installed = True
        app.set_status_pill = MagicMock()
        app._is_testing_voice = False
        app._update_selection_hint = MagicMock()

        patches = [
            patch.object(gui_devices, "get_input_devices", side_effect=lambda *a, **k: list(self.inputs)),
            patch("src.devices.get_input_devices", side_effect=lambda *a, **k: list(self.inputs)),
            patch.object(gui_devices, "classify_device", side_effect=_kind),
            patch.object(devices_mod, "classify_device", side_effect=_kind),
            patch.object(devices_mod, "_windows_default_input", return_value=None),
            patch.object(devices_mod, "get_all_devices", return_value=[]),
            patch.object(gui_devices.DevicesMixin, "_get_windows_default_input", return_value=None),
            patch.object(gui_devices, "check_vbcable_status", return_value=(True, None, None)),
            patch.object(gui_devices, "find_matching_cable_input", return_value=None),
            patch.object(gui_devices, "auto_select_output_device", return_value=({"index": 99, "name": "CABLE Input"}, "")),
            patch.object(gui_devices, "save_config"),
            patch.object(gui_devices, "create_engine_from_config", side_effect=self._make_engine),
            patch.object(gui_devices.sd, "_terminate"),
            patch.object(gui_devices.sd, "_initialize"),
            patch.object(gui_devices, "clear_device_bus_type_cache"),
            patch("src.win_audio.native_list_active_endpoints", side_effect=self.endpoints.listing),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def _make_engine(self, config, input_idx, output_idx, router=None):
        name = next((d["name"] for d in self.inputs if d["index"] == input_idx), None)
        engine = MagicMock(name=f"engine[{input_idx}]")

        def start():
            if name in self.start_failures:
                raise RuntimeError(f"{name} did not open")

        engine.start.side_effect = start
        self.engines.append((input_idx, engine))
        return engine

    # --- scenario helpers ---------------------------------------------------

    def _plug_in(self, raw_name, index, endpoint_id=None, capture=True):
        """A capture endpoint (microphone) is listed by PortAudio as well; a render endpoint is not an input."""
        if capture:
            self.inputs.append({"index": index, "name": raw_name})
            self.endpoints.capture[endpoint_id or f"id-{index}"] = raw_name
        else:
            self.endpoints.render[endpoint_id or f"id-{index}"] = raw_name

    def _unplug(self, raw_name):
        self.inputs = [d for d in self.inputs if d["name"] != raw_name]
        for eid, name in list(self.endpoints.capture.items()):
            if name == raw_name:
                del self.endpoints.capture[eid]

    def _start_with(self, raw_names):
        """Starts Getsu as if it ran with these microphones: selects the first, records the endpoints."""
        for i, name in enumerate(raw_names, start=1):
            self._plug_in(name, i)
        app = self.app
        app._refresh_devices(reinit_portaudio=False)
        app._endpoint_state = app._endpoint_snapshot()
        self.engines.clear()

    def _run(self):
        """Starts the stream on the selected microphone, as toggle_stream does."""
        app = self.app
        app._select_label(app.selected_input_name)
        app.engine = self._make_engine(app.config, app.selected_input_idx, app.selected_output_idx)
        app.engine.start()
        app.is_running = True
        self.engines.clear()

    def _statuses(self):
        return [c.args[0] for c in self.app.set_status_pill.call_args_list]

    def _label_of(self, raw):
        return gui_devices.clean_device_label(raw)

    def _event(self, force=False):
        self.app._handle_hardware_rescan(force=force)


class UnrelatedEventTests(DeviceEventTests):
    def test_mouse_or_usb_stick_while_running_does_not_stop_or_restart(self):
        self._start_with([REALTEK])
        self._run()
        engine = self.app.engine
        self._plug_in(STICK, 5, endpoint_id="stick", capture=False)  # a USB stick: no audio endpoint at all
        self._event()
        engine.stop.assert_not_called()
        self.assertEqual(self.engines, [])
        self.assertIs(self.app.engine, engine)
        self.assertTrue(self.app.is_running)

    def test_same_endpoints_again_changes_nothing(self):
        self._start_with([REALTEK])
        self._run()
        engine = self.app.engine
        self._event()
        engine.stop.assert_not_called()
        self.assertEqual(self.engines, [])

    def test_playback_change_while_running_does_not_restart(self):
        self._start_with([REALTEK])
        self._run()
        engine = self.app.engine
        self.endpoints.render["speakers"] = "Speakers (HDMI)"
        self._event()
        engine.stop.assert_not_called()
        self.assertEqual(self.engines, [])


class HeadsetPlugTests(DeviceEventTests):
    def test_headset_plugged_while_running_switches_and_reasserts_cable(self):
        self._start_with([REALTEK])
        self._run()
        self._plug_in(MPOW, 2)
        self._event()
        self.assertEqual([idx for idx, _ in self.engines], [2])  # exactly one new engine
        self.assertEqual(self.app.selected_input_name, self._label_of(MPOW))
        self.assertTrue(self.app.is_running)
        self.router.reassert_cable_default.assert_called_once_with()
        self.assertIn("Switched to MPOW Wireless Gaming Headset", self._statuses())

    def test_headset_plugged_while_stopped_is_selected(self):
        self._start_with([REALTEK])
        self._plug_in(MPOW, 2)
        self._event()
        self.assertEqual(self.engines, [])
        self.assertFalse(self.app.is_running)
        self.assertEqual(self.app.selected_input_name, self._label_of(MPOW))
        self.assertIn("MPOW Wireless Gaming Headset selected", self._statuses())

    def test_bluetooth_plugged_is_listed_but_not_selected(self):
        self._start_with([REALTEK])
        self._plug_in(BT, 3)
        self._event()
        self.assertEqual(self.app.selected_input_name, self._label_of(REALTEK))
        self.assertFalse(any(s and ("selected" in s or "Switched" in s) for s in self._statuses()))

    def test_bluetooth_plugged_while_running_keeps_the_same_microphone(self):
        self._start_with([REALTEK])
        self._run()
        self._plug_in(BT, 3)
        self._event()
        self.assertEqual([idx for idx, _ in self.engines], [1])  # restarted on the microphone it had
        self.assertEqual(self.app.selected_input_name, self._label_of(REALTEK))
        self.assertNotIn("Switched to HUAWEI FreeBuds SE 4 ANC", self._statuses())

    def test_usb_microphone_is_not_auto_selected(self):
        self._start_with([REALTEK])
        self._plug_in(USB_MIC, 4)
        self._event()
        self.assertEqual(self.app.selected_input_name, self._label_of(REALTEK))

    def test_two_headsets_at_once_use_the_existing_ranking(self):
        self._start_with([REALTEK])
        second = "Microphone (MPOW Second Headset)"
        self._plug_in(second, 5)
        self._plug_in(MPOW, 2)
        self._event()
        expected = auto_select_input_device(candidates=[
            {"index": 5, "name": second}, {"index": 2, "name": MPOW}])
        self.assertEqual(self.app.selected_input_idx, expected["index"])


class OpenFailureTests(DeviceEventTests):
    def test_headset_that_does_not_open_falls_back_and_keeps_running(self):
        self._start_with([REALTEK])
        self._run()
        self.start_failures.add(MPOW)
        self._plug_in(MPOW, 2)
        self._event()
        self.assertTrue(self.app.is_running)
        self.assertEqual(self.app.selected_input_name, self._label_of(REALTEK))
        self.assertIn("Could not use MPOW Wireless Gaming Headset yet. Still using Realtek(R) Audio.",
                      self._statuses())
        self.router.restore_original.assert_not_called()

    def test_failed_headset_is_tried_again_on_the_next_device_event(self):
        self._start_with([REALTEK])
        self._run()
        self.start_failures.add(MPOW)
        self._plug_in(MPOW, 2)
        self._event()
        self.assertEqual(self.app._pending_headset, MPOW)
        self.engines.clear()
        self.start_failures.clear()
        self._event()  # any audio device event: here the same endpoints, so the pending retry is what runs
        self.assertEqual([idx for idx, _ in self.engines][:1], [2])
        self.assertEqual(self.app.selected_input_name, self._label_of(MPOW))
        self.assertIsNone(self.app._pending_headset)
        self.assertTrue(self.app.is_running)

    def test_both_microphones_failing_stops_with_an_error_and_gives_the_mic_back(self):
        self._start_with([REALTEK])
        self._run()
        self.start_failures.update({MPOW, REALTEK})
        self._plug_in(MPOW, 2)
        self._event()
        self.assertFalse(self.app.is_running)
        self.router.restore_original.assert_called_once_with()
        self.assertTrue(any("Input failed" in s for s in self._statuses()))


class SelectedMicrophoneUnpluggedTests(DeviceEventTests):
    def test_selected_mic_removed_while_running_falls_back_by_ranking_not_list_order(self):
        # The laptop mic is first in the list, but a USB microphone outranks the built-in one.
        self._start_with([REALTEK, USB_MIC, MPOW])
        self.app._select_label(self._label_of(MPOW))
        self._run()
        self._unplug(MPOW)
        self._event()
        self.assertEqual(self.app.selected_input_name, self._label_of(USB_MIC))
        self.assertTrue(self.app.is_running)
        self.assertIn("Switched to USB Studio Mic", self._statuses())

    def test_selected_mic_removed_while_stopped_uses_the_ranking(self):
        self._start_with([REALTEK, USB_MIC, MPOW])
        self.app._select_label(self._label_of(MPOW))
        self._unplug(MPOW)
        self._event()
        self.assertEqual(self.app.selected_input_name, self._label_of(USB_MIC))
        self.assertFalse(self.app.is_running)

    def test_no_microphone_left_while_running_stops_with_a_message(self):
        self._start_with([MPOW])
        self._run()
        self._unplug(MPOW)
        self._event()
        self.assertFalse(self.app.is_running)
        self.assertIn("No microphone detected. Please connect a mic.", self._statuses())


class EventBurstTests(DeviceEventTests):
    def test_burst_of_identical_events_restarts_once(self):
        self._start_with([REALTEK])
        self._run()
        self._plug_in(MPOW, 2)
        for _ in range(5):
            self._event()
        self.assertEqual([idx for idx, _ in self.engines], [2])

    def test_watchdog_rescan_restarts_a_dead_stream_without_an_endpoint_change(self):
        self._start_with([REALTEK])
        self._run()
        self._event(force=True)
        self.assertEqual([idx for idx, _ in self.engines], [1])
        self.assertTrue(self.app.is_running)


class RouterDefaultTests(unittest.TestCase):
    """The router puts CABLE Output back as the default after a re-scan (see SmartMicRouter.reassert_cable_default)."""

    def _router(self, current):
        router = SmartMicRouter({"auto_route": True})
        router.is_swapped = True
        router.get_cable_capture_guid = lambda: CABLE_GUID
        router.get_current_default_mic = lambda: current
        router._set_capture_endpoint = MagicMock(return_value=True)
        return router

    def test_default_moved_to_a_headset_is_taken_as_original_and_cable_restored(self):
        router = self._router(("{headset-guid}", "Headset (MPOW)"))
        with patch.object(router_mod, "save_config") as save:
            new_original = router.reassert_cable_default()
        self.assertEqual(new_original, "{headset-guid}")
        self.assertEqual(router.original_mic_id, "{headset-guid}")
        router._set_capture_endpoint.assert_called_once_with(CABLE_GUID)
        save.assert_called()

    def test_default_already_cable_changes_nothing(self):
        router = self._router(("{cable-guid}", "CABLE Output (VB-Audio Virtual Cable)"))
        self.assertIsNone(router.reassert_cable_default())
        router._set_capture_endpoint.assert_not_called()

    def test_not_swapped_changes_nothing(self):
        router = self._router(("{headset-guid}", "Headset (MPOW)"))
        router.is_swapped = False
        self.assertIsNone(router.reassert_cable_default())
        router._set_capture_endpoint.assert_not_called()


class WatchdogWiringTests(unittest.TestCase):
    def test_watchdog_schedules_a_forced_rescan(self):
        app = object.__new__(g.GetsuGUI)
        app._app_running = True
        app._is_rescanning = False
        app.is_running = True
        app.engine = MagicMock()
        app.engine.is_stream_alive.return_value = False
        app.router = None
        app._device_change_lock = threading.Lock()
        app._device_change_timer = None
        app._poll_default_change = MagicMock()
        ticks = {"n": 0}

        def fake_sleep(_):
            ticks["n"] += 1
            if ticks["n"] > 1:
                app._app_running = False

        with patch.object(gui_devices.time, "sleep", side_effect=fake_sleep), \
                patch.object(gui_devices.threading, "Timer") as timer:
            app._watchdog_loop()
        kwargs = timer.call_args.kwargs
        self.assertEqual(kwargs.get("kwargs"), {"force": True})


if __name__ == "__main__":
    unittest.main()
