import os
import subprocess
import tempfile
import unittest
from unittest.mock import MagicMock, patch

import src.diagnostics as diag


class TestSelectiveSuspendParsing(unittest.TestCase):
    SAMPLE = """
Power Scheme GUID: 381b4222-f694-41f0-9685-ff5bb260df2e  (Balanced)
  Subgroup GUID: 2a737441-1930-4402-8d77-b2bebba308a3  (USB settings)
    Power Setting GUID: 48e6b7a6-50f5-4782-a5d4-53bb8f07e226  (USB selective suspend setting)
      Possible Setting Index: 000
      Current AC Power Setting Index: 0x00000001
      Current DC Power Setting Index: 0x00000000
"""

    def test_parses_ac_and_dc(self):
        values = diag.parse_selective_suspend(self.SAMPLE)
        self.assertEqual(values, {"AC": 1, "DC": 0})

    def test_missing_lines_give_none(self):
        self.assertEqual(diag.parse_selective_suspend(""), {"AC": None, "DC": None})


class TestInnerName(unittest.TestCase):
    def test_takes_text_in_last_parentheses(self):
        self.assertEqual(diag._inner_name("Microphone (Jabra EVOLVE 20 MS)"), "Jabra EVOLVE 20 MS")

    def test_cyrillic_name(self):
        self.assertEqual(diag._inner_name("Микрофон гарнитуры (Jabra EVOLVE 20 MS)"), "Jabra EVOLVE 20 MS")

    def test_name_without_parentheses_is_used_whole(self):
        self.assertEqual(diag._inner_name("Headset"), "Headset")


class TestUsbPowerLines(unittest.TestCase):
    def _completed(self, stdout, code=0):
        return subprocess.CompletedProcess(args=[], returncode=code, stdout=stdout, stderr="")

    @patch("src.diagnostics.selective_suspend_lines", return_value=["AC line"])
    @patch("src.diagnostics._run")
    def test_no_matching_usb_device(self, mock_run, _ss):
        mock_run.return_value = self._completed("[]")
        lines = diag.usb_power_lines("Microphone (Jabra EVOLVE 20 MS)")
        self.assertEqual(lines[0], "AC line")
        self.assertTrue(any("no matching USB device" in line for line in lines))

    @patch("src.diagnostics.selective_suspend_lines", return_value=[])
    @patch("src.diagnostics._run")
    def test_chain_is_reported(self, mock_run, _ss):
        payload = ('[{"Device": "Jabra EVOLVE 20 MS", "Chain": ['
                   '{"Level": 0, "InstanceId": "USB\\\\VID_0B0E&PID_2472\\\\X", "FriendlyName": "Jabra", "MSPowerEnable": ["False"]},'
                   '{"Level": 1, "InstanceId": "USB\\\\ROOT_HUB30\\\\Y", "FriendlyName": "Hub", "MSPowerEnable": ["True"]}]}]')
        mock_run.return_value = self._completed(payload)
        lines = diag.usb_power_lines("Microphone (Jabra EVOLVE 20 MS)")
        self.assertTrue(any("USB device match: Jabra EVOLVE 20 MS" in line for line in lines))
        self.assertTrue(any("level 0" in line and "MSPower_DeviceEnable=['False']" in line for line in lines))
        self.assertTrue(any("level 1" in line for line in lines))

    @patch("src.diagnostics.selective_suspend_lines", return_value=[])
    @patch("src.diagnostics._run")
    def test_powershell_failure_is_reported_not_raised(self, mock_run, _ss):
        mock_run.side_effect = OSError("no powershell")
        lines = diag.usb_power_lines("Microphone (Jabra EVOLVE 20 MS)")
        self.assertTrue(any("query failed" in line for line in lines))

    def test_script_quotes_names_safely(self):
        script = diag._usb_power_script("Jack's Mic")
        self.assertIn("'Jack''s Mic'", script)
        self.assertNotIn("__NAME__", script)


class TestSharedLogger(unittest.TestCase):
    def test_file_handler_is_a_single_shared_object(self):
        self.assertIs(diag.get_file_handler(), diag.get_file_handler())

    def test_router_logger_uses_the_shared_handler(self):
        import src.router as router
        shared = diag.get_file_handler()
        if shared is not None:
            self.assertIn(shared, router.logger.handlers)
            self.assertIn(shared, diag.get_logger().handlers)

    def test_emit_never_raises_when_console_is_gone(self):
        with patch("builtins.print", side_effect=OSError("no stdout")):
            diag.info("message without console")


class TestStartFailureLog(unittest.TestCase):
    @patch("src.diagnostics.is_usb_microphone", return_value=False)
    @patch("src.diagnostics._device_name", return_value="Microphone (Realtek(R) Audio)")
    @patch("src.diagnostics.device_dump", return_value="DUMP")
    @patch("src.diagnostics.describe_device", side_effect=lambda i, is_input=True: f"DESC {i}")
    def test_logs_code_and_devices(self, _desc, _dump, _name, _usb):
        exc = RuntimeError("Error opening WasapiStream: Invalid device [PaErrorCode -9996]")
        explained = MagicMock(title="The selected audio device is no longer available",
                              technical=str(exc), code=-9996)
        with self.assertLogs("Getsu", level="INFO") as cm:
            diag.log_start_failure(exc, explained, 4, 7)
        text = "\n".join(cm.output)
        self.assertIn("PaErrorCode: -9996", text)
        self.assertIn("Invalid device", text)
        self.assertIn("DESC 4", text)
        self.assertIn("DESC 7", text)
        self.assertIn("DUMP", text)

    @patch("src.diagnostics.usb_power_lines", return_value=["USB selective suspend AC: 1 (enabled)"])
    @patch("src.diagnostics.is_usb_microphone", return_value=True)
    @patch("src.diagnostics._device_name", return_value="Microphone (Jabra EVOLVE 20 MS)")
    @patch("src.diagnostics.device_dump", return_value="DUMP")
    @patch("src.diagnostics.describe_device", return_value="DESC")
    def test_usb_mic_adds_power_lines(self, _desc, _dump, _name, _usb, mock_power):
        exc = RuntimeError("PaErrorCode -9999")
        explained = MagicMock(title="t", technical="PaErrorCode -9999", code=-9999)
        with self.assertLogs("Getsu", level="INFO") as cm:
            diag.log_start_failure(exc, explained, 1, 2)
        mock_power.assert_called_once_with("Microphone (Jabra EVOLVE 20 MS)")
        self.assertIn("USB selective suspend AC: 1 (enabled)", "\n".join(cm.output))


class TestSaveReport(unittest.TestCase):
    @patch("src.diagnostics.usb_power_lines", return_value=["USB selective suspend AC: 0 (disabled)"])
    @patch("src.diagnostics.is_usb_microphone", return_value=True)
    @patch("src.diagnostics._device_name", return_value="Microphone (Jabra EVOLVE 20 MS)")
    @patch("src.diagnostics.device_dump", return_value="DUMP-TEXT")
    def test_writes_timestamped_report(self, _dump, _name, _usb, _power):
        fake_sd = MagicMock()
        fake_sd.default.device = (3, 5)
        with tempfile.TemporaryDirectory() as tmp, patch.dict("sys.modules", {"sounddevice": fake_sd}):
            path = diag.save_report(os.path.join(tmp, "out"))
            self.assertTrue(os.path.isfile(path))
            self.assertTrue(os.path.basename(path).startswith("getsu-diagnostics-"))
            self.assertTrue(path.endswith(".txt"))
            with open(path, "r", encoding="utf-8") as f:
                body = f.read()
        self.assertIn("== Device dump ==", body)
        self.assertIn("DUMP-TEXT", body)
        self.assertIn("== USB power ==", body)
        self.assertIn("USB selective suspend AC: 0 (disabled)", body)
        self.assertIn("== getsu.log ==", body)


if __name__ == "__main__":
    unittest.main()
