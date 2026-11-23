"""Issue #3 GUI hooks: the tray "Save diagnostics" action and the voice test's device refresh and start messages."""
import os
import unittest
from unittest.mock import ANY, MagicMock, patch

import _isolation  # noqa: F401  (keeps the log and PortAudio isolated)

from src import autostart, diagnostics, gui_tray
from src import gui_voice_test
from src.gui import GetsuGUI
from src.gui_theme import COL_ERROR


class SaveDiagnosticsTests(unittest.TestCase):
    def test_report_goes_to_the_desktop_and_is_selected_in_explorer(self):
        fake = MagicMock()
        desktop = os.path.join("C:\\", "Users", "me", "Desktop")
        report = os.path.join(desktop, "getsu-diagnostics-20261123-101010.txt")
        with patch.object(diagnostics, "desktop_dir", return_value=desktop), \
                patch.object(diagnostics, "save_report", return_value=report) as save, \
                patch.object(gui_tray.subprocess, "Popen") as popen:
            gui_tray.TrayMixin.save_diagnostics_report(fake)
        save.assert_called_once_with(desktop)
        popen.assert_called_once()
        command = popen.call_args[0][0]
        self.assertTrue(command.startswith("explorer.exe /select,"), command)
        self.assertIn(f'"{report}"', command)
        fake.set_status_pill.assert_called_once()

    def test_failed_report_shows_the_error_and_opens_nothing(self):
        fake = MagicMock()
        with patch.object(diagnostics, "desktop_dir", return_value="C:\\Desktop"), \
                patch.object(diagnostics, "save_report", side_effect=OSError("disk full")), \
                patch.object(gui_tray.subprocess, "Popen") as popen:
            gui_tray.TrayMixin.save_diagnostics_report(fake)
        popen.assert_not_called()
        fake.set_status_pill.assert_called_once_with("Could not save diagnostics: disk full", COL_ERROR)

    def test_desktop_dir_is_an_absolute_path(self):
        self.assertTrue(os.path.isabs(diagnostics.desktop_dir()))

    def test_tray_menu_has_save_diagnostics_between_autostart_and_exit(self):
        fake = MagicMock()
        fake.is_running = False
        with patch.object(gui_tray, "Menu") as menu, patch.object(gui_tray.pystray, "Icon"), \
                patch.object(gui_tray.win_theme, "enable_dark_menus"), \
                patch.object(gui_tray, "create_tray_icon_image", return_value=MagicMock()), \
                patch.object(gui_tray.threading, "Thread"), \
                patch.object(autostart, "is_supported", return_value=False):
            gui_tray.TrayMixin._start_tray_icon(fake)
        texts = [getattr(entry, "text", None) for entry in menu.call_args[0]]
        self.assertIn("Save diagnostics", texts)
        self.assertLess(texts.index("Save diagnostics"), texts.index("Exit"))


class VoiceTestStartMessageTests(unittest.TestCase):
    def _fake(self):
        fake = MagicMock()
        fake.is_running = False
        fake._is_testing_voice = False
        fake._state_lock = MagicMock()
        fake._state_lock.acquire.return_value = True
        return fake

    def test_start_while_noise_cancellation_runs_says_why(self):
        fake = self._fake()
        fake.is_running = True
        GetsuGUI.start_voice_test(fake)
        fake.set_status_pill.assert_called_once_with(
            "Voice test could not start: Stop noise cancellation first.", ANY)
        fake._set_button_enabled.assert_not_called()

    def test_start_while_a_test_runs_says_why(self):
        fake = self._fake()
        fake._is_testing_voice = True
        GetsuGUI.start_voice_test(fake)
        fake.set_status_pill.assert_called_once_with(
            "Voice test could not start: A voice test is already running.", ANY)

    def test_start_during_a_state_change_says_why(self):
        fake = self._fake()
        fake._state_lock.acquire.return_value = False
        GetsuGUI.start_voice_test(fake)
        fake.set_status_pill.assert_called_once_with(
            "Voice test could not start: Noise cancellation is starting or stopping.", ANY)


class VoiceTestWorkerTests(unittest.TestCase):
    def _worker_self(self, selected_idx=3):
        fake = MagicMock()
        fake.selected_input_idx = selected_idx
        fake.selected_input_name = "Mic"
        fake._app_running = True
        fake._cancel_voice_test = False
        fake.config = {"music_mode": True, "mic_boost_db": 0, "output_gain": 1.0, "vad_threshold": 0.7}
        fake._format_audio_error = lambda e: f"Input failed ({e})"
        return fake

    def test_no_microphone_is_reported_as_could_not_start(self):
        fake = self._worker_self(selected_idx=None)
        GetsuGUI._run_voice_test_worker(fake)
        fake.set_status_pill.assert_called_once_with("Voice test could not start: No microphone selected.", COL_ERROR)

    def test_microphone_is_looked_up_by_name_before_it_is_opened(self):
        fake = self._worker_self(selected_idx=3)
        info = {"name": "Mic", "max_input_channels": 1, "max_output_channels": 0, "hostapi": 0}
        with patch.object(gui_voice_test, "refresh_input_index", return_value=7) as refresh, \
                patch.object(gui_voice_test, "resolve_single", return_value=(7, [])), \
                patch.object(gui_voice_test, "is_laptop_microphone", return_value=False), \
                patch.object(gui_voice_test.sd, "query_devices", return_value=info), \
                patch.object(gui_voice_test.sd, "query_hostapis", return_value=[{"name": "MME"}]), \
                patch.object(gui_voice_test.sd, "InputStream", side_effect=RuntimeError("no device")) as stream:
            gui_voice_test.VoiceTestMixin._run_voice_test_worker(fake)
        refresh.assert_called_once_with(3, "Mic")
        self.assertEqual(stream.call_args.kwargs["device"], 7)

    def test_failure_to_open_the_microphone_says_could_not_start(self):
        fake = self._worker_self(selected_idx=3)
        info = {"name": "Mic", "max_input_channels": 1, "max_output_channels": 0, "hostapi": 0}
        with patch.object(gui_voice_test, "refresh_input_index", return_value=3), \
                patch.object(gui_voice_test, "resolve_single", return_value=(3, [])), \
                patch.object(gui_voice_test, "is_laptop_microphone", return_value=False), \
                patch.object(gui_voice_test.sd, "query_devices", return_value=info), \
                patch.object(gui_voice_test.sd, "query_hostapis", return_value=[{"name": "MME"}]), \
                patch.object(gui_voice_test.sd, "InputStream", side_effect=RuntimeError("no device")):
            gui_voice_test.VoiceTestMixin._run_voice_test_worker(fake)
        fake.set_status_pill.assert_called_with("Voice test could not start: Input failed (no device)", COL_ERROR)


if __name__ == "__main__":
    unittest.main()
