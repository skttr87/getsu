import unittest
from unittest.mock import patch, MagicMock
from src.devices import is_virtual_input_device, VIRTUAL_INPUT_BLACKLIST

class TestDeviceFiltering(unittest.TestCase):
    def test_virtual_device_detection(self):
        # Known virtual device names that must be filtered
        virtual_names = [
            "CABLE Output (VB-Audio Virtual Cable)",
            "CABLE Output (VB-Audio Point)",
            "VoiceMeeter Output (VB-Audio VoiceMeeter VAIO)",
            "CABLE-A Output (VB-Audio Cable A)",
            "VB-Audio Virtual Cable",
        ]
        for name in virtual_names:
            self.assertTrue(is_virtual_input_device(name), f"Failed to detect virtual device: {name}")

        # Real physical microphones that must NOT be filtered
        physical_names = [
            "Microphone (Realtek(R) Audio)",
            "Microphone Array (Intel Smart Sound Technology)",
            "Headset (MPOW USB Audio)",
            "HyperX QuadCast (USB Audio)",
            "Blue Yeti Microphone",
        ]
        for name in physical_names:
            self.assertFalse(is_virtual_input_device(name), f"False positive on physical device: {name}")

    @patch("src.devices.sd.query_hostapis", return_value=[])
    @patch("src.devices.sd.default", device=(0, 0))
    @patch("src.devices.get_input_devices")
    def test_auto_select_headset_brands(self, mock_inputs, mock_default, mock_apis):
        from src.devices import auto_select_input_device

        # Should select Razer or Logitech headset over built-in Realtek
        mock_inputs.return_value = [
            {'index': 1, 'name': 'Microphone (Realtek(R) Audio)'},
            {'index': 2, 'name': 'Razer BlackShark V2 Pro Headset'},
        ]
        selected = auto_select_input_device()
        self.assertEqual(selected['name'], 'Razer BlackShark V2 Pro Headset')

        mock_inputs.return_value = [
            {'index': 1, 'name': 'Microphone (Realtek(R) Audio)'},
            {'index': 3, 'name': 'Logitech PRO X Wireless Gaming Headset'},
        ]
        selected = auto_select_input_device()
        self.assertEqual(selected['name'], 'Logitech PRO X Wireless Gaming Headset')

        # When headset is unplugged, falls back to Realtek or default
        mock_inputs.return_value = [
            {'index': 1, 'name': 'Microphone (Realtek(R) Audio)'},
        ]
        selected = auto_select_input_device()
        self.assertEqual(selected['name'], 'Microphone (Realtek(R) Audio)')


if __name__ == "__main__":
    unittest.main()
