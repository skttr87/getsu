"""
Guards that fail the run when a test touched the owner's machine: the real getsu.log, the real PortAudio stack or
the real COM device enumerator. Named test_zz_* so it runs after the other modules.
"""
import os
import unittest

import _isolation

import sounddevice as sd

import src.diagnostics as diag
import src.win_audio as win_audio


class TestLogIsolation(unittest.TestCase):
    def test_appdata_points_at_the_temp_directory(self):
        self.assertEqual(os.path.normcase(os.environ["APPDATA"]), os.path.normcase(_isolation.TEMP_APPDATA))

    def test_shared_log_file_is_under_the_temp_directory(self):
        diag.get_file_handler()
        path = os.path.normcase(os.path.abspath(diag.log_path()))
        self.assertTrue(path.startswith(os.path.normcase(os.path.abspath(_isolation.TEMP_DIR))), path)
        handler = diag.get_file_handler()
        if handler is not None:
            self.assertTrue(os.path.normcase(handler.baseFilename).startswith(
                os.path.normcase(os.path.abspath(_isolation.TEMP_DIR))), handler.baseFilename)

    def test_real_log_file_was_not_written_during_the_run(self):
        before = _isolation.REAL_LOG_SIZE_BEFORE
        now = os.path.getsize(_isolation.REAL_LOG_PATH) if os.path.exists(_isolation.REAL_LOG_PATH) else None
        self.assertEqual(now, before, f"the tests wrote to {_isolation.REAL_LOG_PATH}")


class TestPortAudioAndComIsolation(unittest.TestCase):
    def test_real_query_devices_is_refused(self):
        with self.assertRaises(_isolation.RealCallRefused):
            sd.query_devices()

    def test_real_query_hostapis_is_refused(self):
        with self.assertRaises(_isolation.RealCallRefused):
            sd.query_hostapis()

    def test_real_stream_is_refused(self):
        with self.assertRaises(_isolation.RealCallRefused):
            sd.InputStream(samplerate=48000)

    def test_com_is_refused(self):
        with self.assertRaises(_isolation.RealCallRefused):
            win_audio.ole32.CoCreateInstance(None, None, 1, None, None)


if __name__ == "__main__":
    unittest.main()
