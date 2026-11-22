"""Endpoint ID versus friendly name detection used by the CoreAudio readers in src/router.py."""
import unittest

from src.router import _is_endpoint_id


class TestEndpointIdDetection(unittest.TestCase):

    def test_friendly_names_with_dots_and_braces_are_not_endpoint_ids(self):
        for name in ("Speakers (2.1 Audio)", "Microphone (Realtek(R) Audio)", "Line 1.5 {x}", "Mic"):
            self.assertFalse(_is_endpoint_id(name), name)

    def test_real_endpoint_ids_are_endpoint_ids(self):
        # Shape of IMMDevice::GetId values: {dataflow.role.state.flags}.{GUID}
        for endpoint_id in ("{0.0.1.00000000}.{3b1e0a2c-1111-4a2b-9c3d-0123456789ab}",
                            "{0.0.0.00000000}.{5f6e7d8c-2222-4b3c-8d9e-aabbccddeeff}"):
            self.assertTrue(_is_endpoint_id(endpoint_id), endpoint_id)

    def test_empty_values_are_not_endpoint_ids(self):
        self.assertFalse(_is_endpoint_id(None))
        self.assertFalse(_is_endpoint_id(""))


if __name__ == "__main__":
    unittest.main()
