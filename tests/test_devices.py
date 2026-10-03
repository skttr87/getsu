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
    def test_laptop_mic_detection(self):
        from src.devices import is_laptop_microphone
        self.assertTrue(is_laptop_microphone("Realtek(R) Audio Microphone Array"))
        self.assertTrue(is_laptop_microphone("Internal Microphone (Conexant)"))
        self.assertTrue(is_laptop_microphone("Built-in Audio"))
        self.assertFalse(is_laptop_microphone("HyperX QuadCast"))
        self.assertFalse(is_laptop_microphone("Shure SM7B"))

    @patch("src.devices.get_all_devices")
    def test_validate_device_index(self, mock_get_all):
        from src.devices import validate_device_index
        mock_get_all.return_value = [
            {'index': 1, 'name': 'Mic', 'inputs': 1, 'outputs': 0},
            {'index': 2, 'name': 'Speaker', 'inputs': 0, 'outputs': 2},
        ]
        self.assertEqual(validate_device_index(1, is_input=True), 1)
        self.assertIsNone(validate_device_index(1, is_input=False))
    @patch("src.devices.get_all_devices")
    def test_check_vbcable_status_variants(self, mock_get_all):
        from src.devices import check_vbcable_status
        # Standard names
        mock_get_all.return_value = [
            {'index': 1, 'name': 'CABLE Input (VB-Audio Virtual Cable)', 'inputs': 0, 'outputs': 2, 'hostapi': 'Windows WASAPI'},
            {'index': 2, 'name': 'CABLE Output (VB-Audio Virtual Cable)', 'inputs': 2, 'outputs': 0, 'hostapi': 'Windows WASAPI'},
        ]
        installed, cin, cout = check_vbcable_status()
        self.assertTrue(installed)
        self.assertEqual(cin['index'], 1)
        self.assertEqual(cout['index'], 2)

        # Variant names (e.g. MME truncated or alternative naming)
        mock_get_all.return_value = [
            {'index': 3, 'name': 'VB-Audio Point', 'inputs': 0, 'outputs': 2, 'hostapi': 'MME'},
            {'index': 4, 'name': 'VB-Audio Cable', 'inputs': 2, 'outputs': 0, 'hostapi': 'MME'},
        ]
        installed, cin, cout = check_vbcable_status()
        self.assertTrue(installed)

    @patch("src.devices.sd._initialize")
    @patch("src.devices.sd._terminate")
    @patch("src.devices.sd.query_devices")
    @patch("src.devices.sd.query_hostapis")
    def test_check_vbcable_force_rescan(self, mock_hostapis, mock_query, mock_term, mock_init):
        from src.devices import check_vbcable_status
        mock_hostapis.return_value = [{'name': 'Windows WASAPI'}]
        mock_query.return_value = [
            {'name': 'CABLE Input', 'hostapi': 0, 'max_input_channels': 0, 'max_output_channels': 2, 'default_samplerate': 48000.0},
            {'name': 'CABLE Output', 'hostapi': 0, 'max_input_channels': 2, 'max_output_channels': 0, 'default_samplerate': 48000.0},
        ]
        installed, cin, cout = check_vbcable_status(force_rescan=True)
        self.assertTrue(installed)
        mock_term.assert_called_once()
        mock_init.assert_called_once()


if __name__ == "__main__":
    unittest.main()
