"""
M3.1 Start with Windows: autostart registry logic, --minimized single-instance handling, was_running_on_exit.

winreg is replaced by an in-memory fake, so these tests never read or write the real Run key.
"""
import os
import sys
import json
import tempfile
import unittest
from unittest.mock import patch, MagicMock

import os as _os
import sys as _sys
_sys.path.insert(0, _os.path.dirname(__file__))
import _isolation  # noqa: E402,F401  (keeps config writes off the tracked config.json)

import src.autostart as autostart
import src.single_instance as si
import src.config as cfg
import src.gui as gui


class _FakeKey:
    def __init__(self, subkey):
        self.subkey = subkey

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeWinreg:
    """Minimal in-memory stand-in for the winreg functions autostart.py uses."""
    HKEY_CURRENT_USER = "HKCU"
    KEY_READ = 0x20019
    KEY_SET_VALUE = 0x0002
    REG_SZ = 1
    REG_BINARY = 3

    def __init__(self):
        self.keys = {}  # subkey -> {value name: (data, type)}

    def OpenKey(self, root, subkey, reserved=0, access=0):
        if subkey not in self.keys:
            if access & self.KEY_SET_VALUE:
                self.keys[subkey] = {}
            else:
                raise FileNotFoundError(subkey)
        return _FakeKey(subkey)

    def QueryValueEx(self, key, name):
        values = self.keys[key.subkey]
        if name not in values:
            raise FileNotFoundError(name)
        return values[name]

    def SetValueEx(self, key, name, reserved, typ, data):
        self.keys[key.subkey][name] = (data, typ)

    def DeleteValue(self, key, name):
        values = self.keys[key.subkey]
        if name not in values:
            raise FileNotFoundError(name)
        del values[name]

    def run_value(self):
        return self.keys.get(autostart.RUN_KEY, {}).get(autostart.VALUE_NAME)

    def approval_value(self):
        return self.keys.get(autostart.APPROVED_RUN_KEY, {}).get(autostart.VALUE_NAME)


FROZEN_EXE = "C:\\Program Files\\Getsu\\getsu.exe"


class _Base(unittest.TestCase):
    def setUp(self):
        self.reg = FakeWinreg()
        patches = [
            patch.object(autostart, "winreg", self.reg),
            patch("sys.frozen", True, create=True),
            patch("sys.executable", FROZEN_EXE),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)


class TestAutostartFrozen(_Base):
    def test_enable_writes_quoted_path_with_minimized_arg(self):
        self.assertTrue(autostart.enable())
        data, typ = self.reg.run_value()
        self.assertEqual(data, '"C:\\Program Files\\Getsu\\getsu.exe" --minimized')
        self.assertEqual(typ, FakeWinreg.REG_SZ)

    def test_enable_then_is_enabled(self):
        autostart.enable()
        self.assertTrue(autostart.is_enabled())

    def test_is_enabled_false_without_run_value(self):
        self.assertFalse(autostart.is_enabled())

    def test_disable_removes_run_value(self):
        autostart.enable()
        self.assertTrue(autostart.disable())
        self.assertIsNone(self.reg.run_value())
        self.assertFalse(autostart.is_enabled())

    def test_disable_when_not_enabled_is_harmless(self):
        self.assertTrue(autostart.disable())
        self.assertFalse(autostart.is_enabled())

    def test_startup_approved_enabled_byte_keeps_entry_on(self):
        autostart.enable()
        for first in (0x02, 0x06):
            self.reg.keys[autostart.APPROVED_RUN_KEY] = {
                autostart.VALUE_NAME: (bytes([first]) + b"\x00" * 11, FakeWinreg.REG_BINARY)}
            self.assertTrue(autostart.is_enabled(), hex(first))

    def test_startup_approved_disabled_byte_reports_unchecked(self):
        autostart.enable()
        self.reg.keys[autostart.APPROVED_RUN_KEY] = {
            autostart.VALUE_NAME: (b"\x03" + b"\x00" * 11, FakeWinreg.REG_BINARY)}
        self.assertFalse(autostart.is_enabled())

    def test_empty_approval_value_counts_as_enabled(self):
        autostart.enable()
        self.reg.keys[autostart.APPROVED_RUN_KEY] = {
            autostart.VALUE_NAME: (b"", FakeWinreg.REG_BINARY)}
        self.assertTrue(autostart.is_enabled())

    def test_enable_clears_task_manager_disabled_mark(self):
        autostart.enable()
        self.reg.keys[autostart.APPROVED_RUN_KEY] = {
            autostart.VALUE_NAME: (b"\x03" + b"\x00" * 11, FakeWinreg.REG_BINARY)}
        autostart.enable()
        self.assertIsNone(self.reg.approval_value())
        self.assertTrue(autostart.is_enabled())

    def test_disable_removes_approval_mark_too(self):
        autostart.enable()
        self.reg.keys[autostart.APPROVED_RUN_KEY] = {
            autostart.VALUE_NAME: (b"\x02" + b"\x00" * 11, FakeWinreg.REG_BINARY)}
        autostart.disable()
        self.assertIsNone(self.reg.run_value())
        self.assertIsNone(self.reg.approval_value())

    def test_path_with_spaces_is_quoted(self):
        self.assertEqual(autostart.run_command("C:\\My Apps\\Getsu\\getsu.exe"),
                         '"C:\\My Apps\\Getsu\\getsu.exe" --minimized')

    def test_is_supported_when_frozen(self):
        self.assertTrue(autostart.is_supported())


class TestAutostartFromSource(_Base):
    def setUp(self):
        super().setUp()
        # Dev mode: not frozen. Only the frozen build may write the Run entry.
        # Override the frozen patch from _Base; stop only this patch (patch.stopall would also drop _isolation).
        not_frozen = patch("sys.frozen", False, create=True)
        not_frozen.start()
        self.addCleanup(not_frozen.stop)

    def test_not_supported_from_source(self):
        self.assertFalse(autostart.is_supported())

    def test_enable_refused_from_source_and_writes_nothing(self):
        self.assertFalse(autostart.enable())
        self.assertIsNone(self.reg.run_value())

    def test_disable_refused_from_source_and_leaves_entry(self):
        self.reg.keys[autostart.RUN_KEY] = {autostart.VALUE_NAME: (FROZEN_EXE, FakeWinreg.REG_SZ)}
        self.assertFalse(autostart.disable())
        self.assertIsNotNone(self.reg.run_value())

    def test_is_enabled_still_reads_the_entry_from_source(self):
        # The greyed line shows the real state even in dev mode.
        self.reg.keys[autostart.RUN_KEY] = {autostart.VALUE_NAME: (FROZEN_EXE, FakeWinreg.REG_SZ)}
        self.assertTrue(autostart.is_enabled())


class TestAutostartNoWinreg(unittest.TestCase):
    def test_no_winreg_means_unsupported_and_unchecked(self):
        with patch.object(autostart, "winreg", None), patch("sys.frozen", True, create=True):
            self.assertFalse(autostart.is_supported())
            self.assertFalse(autostart.is_enabled())
            self.assertFalse(autostart.enable())


class TestResumeDecision(unittest.TestCase):
    def test_resume_only_for_minimized_launch_after_running_exit(self):
        self.assertTrue(autostart.should_resume_filter(True, True))
        self.assertFalse(autostart.should_resume_filter(True, False))
        self.assertFalse(autostart.should_resume_filter(False, True))
        self.assertFalse(autostart.should_resume_filter(False, False))

    def test_devices_ready_needs_input_output_and_cable(self):
        self.assertTrue(autostart.devices_ready(3, 5, True))
        self.assertFalse(autostart.devices_ready(3, 5, False))
        self.assertFalse(autostart.devices_ready(None, 5, True))
        self.assertFalse(autostart.devices_ready(3, -1, True))

    def test_retry_until_stops_on_success(self):
        answers = iter([False, False, True])
        calls = []
        now = [0.0]

        def clock():
            return now[0]

        def sleep(s):
            now[0] += s

        def done():
            calls.append(now[0])
            return next(answers)

        self.assertTrue(autostart.retry_until(done, 20.0, 1.0, clock=clock, sleep=sleep))
        self.assertEqual(len(calls), 3)

    def test_retry_until_gives_up_after_timeout(self):
        now = [0.0]

        def sleep(s):
            now[0] += s

        attempts = []

        def done():
            attempts.append(now[0])
            return False

        self.assertFalse(autostart.retry_until(done, 20.0, 1.0, clock=lambda: now[0], sleep=sleep))
        self.assertEqual(attempts[0], 0.0)
        self.assertLessEqual(attempts[-1], 20.0)
        self.assertGreaterEqual(attempts[-1], 19.0)


class TestSingleInstanceMinimized(unittest.TestCase):
    def _fake_ctypes(self, hwnd=1234):
        kernel32 = MagicMock()
        user32 = MagicMock()
        kernel32.CreateMutexW.return_value = 77
        kernel32.GetLastError.return_value = 183  # ERROR_ALREADY_EXISTS
        kernel32.GetCurrentThreadId.return_value = 1
        user32.FindWindowW.return_value = hwnd
        user32.RegisterWindowMessageW.return_value = 5
        user32.GetForegroundWindow.return_value = 2
        user32.GetWindowThreadProcessId.return_value = 0
        fake = MagicMock()
        fake.windll.kernel32 = kernel32
        fake.windll.user32 = user32
        return fake, user32

    def test_minimized_second_launch_exits_without_restoring_window(self):
        fake, user32 = self._fake_ctypes()
        with patch.object(si, "_app_mutex", None), patch.object(si, "ctypes", fake), \
                patch.object(si.sys, "platform", "win32"):
            self.assertTrue(si.activate_existing_instance(restore=False))
        user32.ShowWindow.assert_not_called()
        user32.SetForegroundWindow.assert_not_called()
        user32.BringWindowToTop.assert_not_called()
        user32.PostMessageW.assert_not_called()

    def test_normal_second_launch_still_restores_window(self):
        fake, user32 = self._fake_ctypes()
        with patch.object(si, "_app_mutex", None), patch.object(si, "ctypes", fake), \
                patch.object(si.sys, "platform", "win32"):
            self.assertTrue(si.activate_existing_instance())
        user32.ShowWindow.assert_called_once_with(1234, 9)
        user32.PostMessageW.assert_called_once()

    def test_first_launch_is_not_affected_by_restore_flag(self):
        fake, user32 = self._fake_ctypes()
        fake.windll.kernel32.GetLastError.return_value = 0  # no existing mutex
        with patch.object(si, "_app_mutex", None), patch.object(si, "ctypes", fake), \
                patch.object(si.sys, "platform", "win32"):
            self.assertFalse(si.activate_existing_instance(restore=False))
        user32.ShowWindow.assert_not_called()


class TestWasRunningOnExitConfig(unittest.TestCase):
    def test_default_is_false(self):
        self.assertIs(cfg.DEFAULT_CONFIG["was_running_on_exit"], False)

    def test_legacy_config_gets_key_and_is_migrated(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            path = os.path.join(tmp_dir, "config.json")
            with patch("src.config.CONFIG_FILE", path):
                with open(path, "w", encoding="utf-8") as f:
                    json.dump({"version": "1.3.1", "vad_threshold": 0.70}, f)
                loaded = cfg.load_config()
                self.assertIs(loaded["was_running_on_exit"], False)
                with open(path, "r", encoding="utf-8") as f:
                    self.assertIs(json.load(f)["was_running_on_exit"], False)

    def test_stored_true_is_kept(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            path = os.path.join(tmp_dir, "config.json")
            with patch("src.config.CONFIG_FILE", path):
                with open(path, "w", encoding="utf-8") as f:
                    json.dump({"version": "1.3.1", "was_running_on_exit": True}, f)
                self.assertIs(cfg.load_config()["was_running_on_exit"], True)


class TestGuiAutostartHooks(unittest.TestCase):
    def _obj(self):
        obj = gui.GetsuGUI.__new__(gui.GetsuGUI)
        obj.config = {}
        obj._app_running = True
        obj.is_running = False
        obj._busy = None
        obj._is_testing_voice = False
        obj._resume_pending = True
        obj.selected_input_idx = 3
        obj.selected_output_idx = 5
        obj.is_vbcable_installed = True
        return obj

    def test_persist_was_running_writes_config(self):
        obj = self._obj()
        with patch.object(gui, "save_config") as save:
            obj._persist_was_running(True)
        self.assertIs(obj.config["was_running_on_exit"], True)
        save.assert_called_once_with(obj.config)

    def test_try_resume_does_not_start_when_devices_missing(self):
        obj = self._obj()
        obj.is_vbcable_installed = False
        obj._refresh_devices = MagicMock()
        obj.toggle_stream = MagicMock()
        self.assertFalse(obj._try_resume())
        obj.toggle_stream.assert_not_called()

    def test_try_resume_starts_through_toggle_stream_when_ready(self):
        obj = self._obj()
        obj._refresh_devices = MagicMock()

        def fake_toggle(sender=None, app_data=None):
            obj.is_running = True

        obj.toggle_stream = MagicMock(side_effect=fake_toggle)
        self.assertTrue(obj._try_resume())
        obj.toggle_stream.assert_called_once()

    def test_try_resume_waits_while_a_transition_is_busy(self):
        obj = self._obj()
        obj._busy = "starting"
        obj.toggle_stream = MagicMock()
        self.assertFalse(obj._try_resume())
        obj.toggle_stream.assert_not_called()

    def test_toggle_autostart_flips_the_entry(self):
        obj = self._obj()
        with patch.object(gui.autostart, "is_enabled", return_value=False), \
                patch.object(gui.autostart, "enable") as enable, \
                patch.object(gui.autostart, "disable") as disable:
            obj._toggle_autostart()
        enable.assert_called_once()
        disable.assert_not_called()


if __name__ == "__main__":
    unittest.main()
