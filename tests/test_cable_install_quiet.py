"""
The in-app VB-CABLE install runs the setup hidden (SW_HIDE) and closes the message boxes that the setup process
itself leaves open. Nothing here runs the real installer: ShellExecuteExW, EnumWindows and the window messages
are mocks.
"""
import ctypes
import unittest
from unittest.mock import MagicMock, call, patch

import _isolation  # noqa: F401

from src import devices

SW_HIDE = 0
SETUP_PID = 4321
UAC_PID = 777
DIALOG = "#32770"


class _FakeUser32:
    """Only the calls close_setup_message_boxes makes."""

    def __init__(self):
        self.sent = []  # (hwnd, message, wparam)
        self.alive = set()

    def SendMessageTimeoutW(self, hwnd, msg, wparam, lparam, flags, timeout, result):
        self.sent.append((hwnd, msg, wparam))
        if msg == devices._WM_CLOSE or (msg == devices._WM_COMMAND and wparam == devices._IDOK):
            self.alive.discard(hwnd)  # a box that takes IDOK goes away
        return 1

    def IsWindow(self, hwnd):
        return hwnd in self.alive


class CloseSetupMessageBoxesTests(unittest.TestCase):
    def setUp(self):
        # hwnd -> (owner pid, class, title, visible)
        self.windows = {
            10: (SETUP_PID, DIALOG, "VB-Audio Virtual Cable", True),         # setup's message box, the one to close
            11: (UAC_PID, DIALOG, "User Account Control", True),             # another process: never touched
            12: (SETUP_PID, "Button", "", True),                             # setup's own non-dialog window
            13: (SETUP_PID, DIALOG, "hidden", False),                        # not visible: left alone
            14: (SETUP_PID, DIALOG, "second", True),
        }
        self.user32 = _FakeUser32()
        self.user32.alive = {10, 11, 12, 13, 14}
        self.log = []

    def _run(self, pid=SETUP_PID):
        with patch.object(devices, "_enum_top_level_windows", return_value=list(self.windows)), \
                patch.object(devices, "_window_info", side_effect=lambda user32, hwnd: self.windows[hwnd]):
            return devices.close_setup_message_boxes(pid, user32=self.user32, log=self.log.append)

    def test_only_the_setups_own_visible_dialogs_are_closed(self):
        closed = self._run()
        self.assertEqual(closed, 2)
        touched = {hwnd for hwnd, _msg, _w in self.user32.sent}
        self.assertEqual(touched, {10, 14})

    def test_idok_is_sent_to_the_box_and_wm_close_only_if_it_is_still_there(self):
        self._run()
        sent_to_10 = [(msg, w) for hwnd, msg, w in self.user32.sent if hwnd == 10]
        self.assertEqual(sent_to_10, [(devices._WM_COMMAND, devices._IDOK)])  # it went away on IDOK

        self.user32.sent.clear()
        self.user32.alive = {10, 11, 12, 13, 14}

        def stubborn(hwnd, msg, wparam, lparam, flags, timeout, result):
            self.user32.sent.append((hwnd, msg, wparam))
            return 1  # the box ignores IDOK and stays
        self.user32.SendMessageTimeoutW = stubborn
        self._run()
        sent_to_10 = [(msg, w) for hwnd, msg, w in self.user32.sent if hwnd == 10]
        self.assertEqual(sent_to_10, [(devices._WM_COMMAND, devices._IDOK), (devices._WM_CLOSE, 0)])

    def test_title_is_logged(self):
        self._run()
        self.assertTrue(any("VB-Audio Virtual Cable" in line for line in self.log), self.log)

    def test_no_pid_does_nothing(self):
        self.assertEqual(self._run(pid=0), 0)
        self.assertEqual(self.user32.sent, [])

    def test_other_processes_windows_never_receive_a_message(self):
        self._run()
        self.assertNotIn(11, {hwnd for hwnd, _m, _w in self.user32.sent})


class EnumerationHelperTests(unittest.TestCase):
    def test_enum_collects_every_top_level_handle(self):
        user32 = MagicMock()

        def enum(callback, lparam):
            for handle in (101, 102, 103):
                callback(handle, 0)
            return 1

        user32.EnumWindows.side_effect = enum
        self.assertEqual(devices._enum_top_level_windows(user32), [101, 102, 103])


class InstallLaunchTests(unittest.TestCase):
    """install_vbcable_driver: the setup is launched hidden and its process is waited on with the message box closer."""

    def _install(self, process_handle=0x5555):
        shell = MagicMock()
        kernel = MagicMock()
        kernel.GetProcessId.return_value = SETUP_PID
        kernel.WaitForSingleObject.return_value = 0  # the setup has already exited

        def execute(byref_info):
            info = byref_info._obj
            info.hProcess = process_handle
            return 1

        shell.ShellExecuteExW.side_effect = execute
        fake_windll = MagicMock()
        fake_windll.shell32 = shell
        fake_windll.kernel32 = kernel
        with patch.object(ctypes, "windll", fake_windll, create=True), \
                patch.object(devices.os.path, "exists", return_value=True), \
                patch.object(devices.subprocess, "run"), \
                patch.object(devices.time, "sleep"), \
                patch.object(devices, "close_setup_message_boxes", return_value=0) as closer, \
                patch.object(devices.sys, "platform", "win32"):
            result = devices.install_vbcable_driver()
        return result, shell, kernel, closer

    def test_setup_is_launched_with_sw_hide_and_the_runas_verb(self):
        result, shell, _kernel, _closer = self._install()
        self.assertTrue(result)
        info = shell.ShellExecuteExW.call_args[0][0]._obj
        self.assertEqual(info.nShow, SW_HIDE)
        self.assertEqual(info.lpVerb, "runas")
        self.assertEqual(info.lpParameters, "-i -h")

    def test_message_boxes_are_closed_for_the_setup_process_only(self):
        _result, _shell, kernel, closer = self._install()
        kernel.GetProcessId.assert_called_once()
        closer.assert_called_with(SETUP_PID)

    def test_failed_launch_returns_false_and_closes_nothing(self):
        shell = MagicMock()
        shell.ShellExecuteExW.return_value = 0  # UAC declined
        fake_windll = MagicMock()
        fake_windll.shell32 = shell
        with patch.object(ctypes, "windll", fake_windll, create=True), \
                patch.object(devices.os.path, "exists", return_value=True), \
                patch.object(devices.subprocess, "run"), \
                patch.object(devices.sys, "platform", "win32"), \
                patch.object(devices, "close_setup_message_boxes") as closer:
            self.assertFalse(devices.install_vbcable_driver())
        closer.assert_not_called()


class WaitLoopTests(unittest.TestCase):
    def test_boxes_are_closed_while_the_setup_still_runs(self):
        kernel = MagicMock()
        kernel.GetProcessId.return_value = SETUP_PID
        # Two polls with the setup still running, then it exits.
        kernel.WaitForSingleObject.side_effect = [0x102, 0x102, 0]
        fake_windll = MagicMock()
        fake_windll.kernel32 = kernel
        with patch.object(ctypes, "windll", fake_windll, create=True), \
                patch.object(devices, "close_setup_message_boxes") as closer:
            devices._wait_for_setup(0x5555, timeout_s=60.0)
        # Two passes while running, and one after the process exited.
        self.assertEqual(closer.call_args_list, [call(SETUP_PID)] * 3)


if __name__ == "__main__":
    unittest.main()
