import unittest
import sys
import ctypes

class TestSingleInstance(unittest.TestCase):
    def test_single_instance_detection(self):
        if sys.platform != "win32":
            self.skipTest("Windows-only test")

        import src.single_instance as si
        kernel32 = ctypes.windll.kernel32
        test_mutex_name = "Local\\Getsu_SingleInstance_Test_Mutex_XYZ"

        # 1. Initial call should acquire mutex and return False (not already running)
        is_already_running_1 = si.activate_existing_instance(mutex_name=test_mutex_name)
        self.assertFalse(is_already_running_1)
        self.assertIsNotNone(si._app_mutex)

        # 2. Emulate a 2nd launch in another process/context by creating another handle
        test_mutex2 = kernel32.CreateMutexW(None, False, test_mutex_name)
        err = kernel32.GetLastError()
        # 183 = ERROR_ALREADY_EXISTS
        self.assertEqual(err, 183)

        if test_mutex2:
            kernel32.CloseHandle(test_mutex2)

        # Clean up first mutex handle
        if si._app_mutex:
            kernel32.CloseHandle(si._app_mutex)
            si._app_mutex = None


if __name__ == "__main__":
    unittest.main()
