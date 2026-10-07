import unittest

from src.audio_errors import explain_start_error
from src.host_api_resolver import DeviceResolutionError


class ExplainTests(unittest.TestCase):
    def kind(self, text, exc=RuntimeError):
        return explain_start_error(exc(text)).kind

    def test_reported_wdmks_error(self):
        raw = ("Error starting stream: Unanticipated host error [PaErrorCode -9999]: 'WdmSyncIoctl: DeviceIoControl "
               "GLE = 0x00000492 (prop_set = {1464EDA5-6A8F-11D1-9AA7-00A0C9223196}, prop_id = 0)' [Windows WDM-KS error 0]")
        e = explain_start_error(RuntimeError(raw))
        self.assertEqual(e.kind, "wdmks")
        self.assertEqual(e.code, -9999)
        self.assertIn("WdmSyncIoctl", e.technical)

    def test_codes(self):
        self.assertEqual(self.kind("PaErrorCode -9985"), "busy")
        self.assertEqual(self.kind("Invalid sample rate [PaErrorCode -9997]"), "sample_rate")
        self.assertEqual(self.kind("Illegal combination of I/O devices [PaErrorCode -9993]"), "host_mismatch")
        self.assertEqual(self.kind("Invalid device [PaErrorCode -9996]"), "invalid_device")
        self.assertEqual(self.kind("Invalid number of channels [PaErrorCode -9998]"), "channels")
        self.assertEqual(self.kind("PaErrorCode -9984"), "host_settings")
        self.assertEqual(self.kind("Unanticipated host error [PaErrorCode -9999]: 'MME error 5'"), "host_error")
        self.assertEqual(self.kind("something else"), "unknown")

    def test_resolution_error_uses_its_own_message(self):
        e = explain_start_error(DeviceResolutionError("No usable pair; install VB-Cable"))
        self.assertEqual(e.kind, "no_pair")
        self.assertIn("VB-Cable", e.advice)

    def test_technical_text_is_trimmed_not_lost(self):
        e = explain_start_error(RuntimeError("x" * 1000))
        self.assertEqual(len(e.technical), 400)


if __name__ == "__main__":
    unittest.main(verbosity=2)

