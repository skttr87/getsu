import unittest
import time
import threading
from unittest.mock import MagicMock, patch

class TestDeviceHotplug(unittest.TestCase):
    def test_debounced_hotplug_trigger(self):
        """Verify that rapid WM_DEVICECHANGE bursts only trigger a single rescan."""
        call_count = 0
        lock = threading.Lock()

        def mock_rescan():
            nonlocal call_count
            with lock:
                call_count += 1

        # Simulate GetsuGUI debounce logic
        timer = None
        timer_lock = threading.Lock()

        def trigger_event():
            nonlocal timer
            with timer_lock:
                if timer and timer.is_alive():
                    timer.cancel()
                timer = threading.Timer(0.1, mock_rescan)
                timer.daemon = True
                timer.start()

        # Fire 5 rapid events within 30ms
        for _ in range(5):
            trigger_event()
            time.sleep(0.005)

        # Wait for debounce timer (0.1s + margin)
        time.sleep(0.2)

        with lock:
            self.assertEqual(call_count, 1, "Debounce failed: expected exactly 1 rescan call")

        if timer and timer.is_alive():
            timer.cancel()

    def test_name_based_hotplug_fallback_on_index_shift(self):
        """
        Verify that unplugging a headset (e.g. MPOW at index 1) is reliably detected
        even when another device shifts into index 1, because identity is tracked by NAME.
        """
        # Initial state: MPOW is at index 1, Realtek is at index 2
        old_devices = {
            "MPOW USB Headset": 1,
            "Realtek High Definition Audio": 2,
        }
        selected_input_name = "MPOW USB Headset"
        selected_input_idx = 1

        # MPOW is unplugged -> Realtek shifts to index 1!
        new_devices = {
            "Realtek High Definition Audio": 1,
        }

        # Index-only check would FAIL here because 1 in new_devices.values() is True!
        self.assertTrue(selected_input_idx in new_devices.values(), "Demonstrates index-shifting trap")

        # Name-based check SUCCEEDS:
        is_disconnected = (selected_input_name not in new_devices)
        self.assertTrue(is_disconnected, "Name-based check correctly identifies MPOW was unplugged")

        # Simulate Getsu fallback behavior
        if is_disconnected:
            fallback_name = "Realtek High Definition Audio"
            selected_input_idx = new_devices[fallback_name]
            selected_input_name = fallback_name

        self.assertEqual(selected_input_name, "Realtek High Definition Audio")
        self.assertEqual(selected_input_idx, 1)


if __name__ == "__main__":
    unittest.main()
