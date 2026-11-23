"""
Runtime guard for DearPyGui and the UI thread: every worker code path that changes the window goes through the UI
queue, so DearPyGui is only ever called from the thread that runs the render loop.

The DearPyGui module is replaced in the GUI modules by a spy that records the thread of each call. The worker
paths below run on a separate thread; the spy must record no call from that thread, and the queued work must
show up after the queue is drained on the UI thread.
"""
import threading
import unittest
from unittest.mock import MagicMock, patch

import _isolation  # noqa: F401

import dearpygui.dearpygui as dpg

from src import gui as g
from src import gui_theme as theme

GUI_MODULES = ("src.gui", "src.gui_hero", "src.gui_widgets", "src.gui_devices", "src.gui_setup",
               "src.gui_voice_test", "src.gui_panels", "src.gui_tray", "src.gui_settings")
# The class attribute is patched in setUpClass (no device scan at start-up); the real method is kept here.
REAL_REFRESH_DEVICES = g.GetsuGUI._refresh_devices


class _DpgSpy:
    """Forwards to DearPyGui and reports each call with the thread it came from."""

    def __init__(self, record):
        self._record = record

    def __getattr__(self, name):
        value = getattr(dpg, name)
        if not callable(value):
            return value

        def spy(*args, **kwargs):
            self._record(name)
            return value(*args, **kwargs)

        spy.__name__ = name
        return spy


def _run_on_worker(fn):
    errors = []

    def runner():
        try:
            fn()
        except BaseException as e:  # reported to the test below
            errors.append(e)

    thread = threading.Thread(target=runner, name="getsu-test-worker")
    thread.start()
    thread.join(10.0)
    if errors:
        raise errors[0]
    return thread


class WorkerPathsNeverTouchDearPyGuiTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.main_ident = threading.get_ident()
        cls.calls = []
        cls.lock = threading.Lock()
        spy = _DpgSpy(cls._record)
        cls.patchers = [
            patch.object(g.GetsuGUI, "_refresh_devices"),
            patch.object(g.GetsuGUI, "_watchdog_loop", lambda self: None),
            patch.object(g, "SmartMicRouter"),
            patch.object(g.CableGuard, "request"),
        ]
        cls.patchers += [patch(f"{name}.dpg", spy) for name in GUI_MODULES]
        for p in cls.patchers:
            p.start()
        try:
            app = g.GetsuGUI()
            app.device_map = {"USB Mic": 1, "Laptop Mic": 2}
            app.device_raw = {"USB Mic": "USB Mic", "Laptop Mic": "Laptop Mic"}
            app.build_ui(headless=True)
        except Exception:
            for p in reversed(cls.patchers):
                p.stop()
            raise
        cls.app = app
        for _ in range(3):
            dpg.render_dearpygui_frame()

    @classmethod
    def tearDownClass(cls):
        dpg.destroy_context()
        for p in reversed(cls.patchers):
            p.stop()

    @classmethod
    def _record(cls, name):
        ident = threading.get_ident()
        if ident != cls.main_ident:
            with cls.lock:
                cls.calls.append((ident, name))

    def setUp(self):
        with self.lock:
            self.calls.clear()

    def _assert_no_worker_calls(self):
        with self.lock:
            leaked = list(self.calls)
        self.assertEqual(leaked, [], "DearPyGui was called from a worker thread")

    def test_status_line_and_hero_from_a_worker_are_queued(self):
        app = self.app
        _run_on_worker(lambda: app.set_status_pill("Starting from a worker", theme.COL_ACCENT))
        _run_on_worker(app._render_hero)
        self._assert_no_worker_calls()
        self.assertGreaterEqual(app._ui_queue.pending(), 2)
        app._drain_ui_queue()
        self.assertEqual(app._ui_queue.pending(), 0)
        self.assertEqual(app._notice[0], "Starting from a worker")

    def test_microphone_card_from_a_worker_is_queued(self):
        app = self.app
        _run_on_worker(app._refresh_mic_widgets)
        _run_on_worker(lambda: app._update_selection_hint("USB Mic"))
        self._assert_no_worker_calls()
        app._drain_ui_queue()

    def test_buttons_from_a_worker_are_queued(self):
        app = self.app
        _run_on_worker(lambda: app._set_voice_test_button_state(False))
        _run_on_worker(lambda: app._set_button_label("btn_voice_test", "Recording... (5s)"))
        _run_on_worker(lambda: app._set_button_enabled("btn_voice_test", True))
        _run_on_worker(lambda: app._set_button_tip("btn_voice_test", "tip"))
        self._assert_no_worker_calls()
        app._drain_ui_queue()

    def test_cable_banner_and_setup_card_from_a_worker_are_queued(self):
        app = self.app
        _run_on_worker(app._update_cable_banner)
        _run_on_worker(app._poll_setup_card)
        self._assert_no_worker_calls()
        app._drain_ui_queue()

    def test_cable_guard_notice_from_a_worker_is_queued(self):
        app = self.app
        _run_on_worker(lambda: app._cable_guard_notify("Cable notice", True))
        self._assert_no_worker_calls()
        app._drain_ui_queue()
        self.assertEqual(app._notice[0], "Cable notice")

    def test_device_refresh_from_a_worker_is_queued(self):
        app = self.app
        inputs = [{"index": 1, "name": "USB Mic"}, {"index": 2, "name": "Laptop Mic"}]
        with patch("src.gui_devices.get_input_devices", return_value=inputs), \
                patch("src.gui_devices.check_vbcable_status", return_value=(True, None, None)), \
                patch("src.gui_devices.find_matching_cable_input", return_value=None), \
                patch("src.gui_devices.auto_select_output_device", return_value=({"index": 3, "name": "Out"}, None)), \
                patch("src.gui_devices.auto_select_input_device", return_value=inputs[0]), \
                patch("src.gui_devices.save_config"):
            _run_on_worker(lambda: REAL_REFRESH_DEVICES(app, reinit_portaudio=False))
        self._assert_no_worker_calls()
        app._drain_ui_queue()
        self.assertIn("USB Mic", app.device_map)

    def test_tray_exit_asks_the_ui_thread_to_end_the_loop(self):
        app = self.app
        with patch.object(g, "dpg") as fake_dpg:
            _run_on_worker(app._stop_render_loop)
            fake_dpg.stop_dearpygui.assert_not_called()
            self.assertEqual(app._ui_queue.pending(), 1)
            app._drain_ui_queue()
            fake_dpg.stop_dearpygui.assert_called_once_with()



if __name__ == "__main__":
    unittest.main()
