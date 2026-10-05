import unittest
from src.host_api_resolver import (
    resolve_pair,
    resolve_single,
    DeviceResolutionError,
    ResolvedPair,
    _similarity,
    _role,
)


def make_mock_audio_env():
    """
    Constructs a controlled multi-hostapi environment:
    Host APIs:
      0: MME
      1: Windows DirectSound
      2: Windows WASAPI
      3: Windows WDM-KS

    Devices:
      0: [MME] Microphone (Realtek Audio) (in=2, out=0)
      1: [MME] CABLE Input (VB-Audio Virtual Cable) (in=0, out=2)
      2: [DirectSound] Primary Sound Capture Driver (in=2, out=0)
      3: [DirectSound] CABLE Input (VB-Audio Virtual Cable) (in=0, out=2)
      4: [WASAPI] Microphone (Realtek Audio) (in=2, out=0)
      5: [WASAPI] Headset Microphone (USB Audio) (in=1, out=0)
      6: [WASAPI] Line In (Realtek Audio) (in=2, out=0)
      7: [WASAPI] CABLE Input (VB-Audio Virtual Cable) (in=0, out=2)
      8: [WASAPI] CABLE Input (16ch) (in=0, out=16)
      9: [WASAPI] Speakers (Realtek Audio) (in=0, out=2)
      10: [WDM-KS] Mic in at front panel (black) (in=1, out=0)
      11: [WDM-KS] Line in at rear panel (blue) (in=1, out=0)
      12: [WDM-KS] Microphone (Realtek Audio) (in=2, out=0)
      13: [WDM-KS] CABLE Input (VB-Audio) (in=0, out=2)
      14: [WDM-KS] Unknown Jack 999 (in=1, out=0)
    """
    hostapis = [
        {"name": "MME", "devices": [0, 1], "default_input_device": 0, "default_output_device": 1},
        {"name": "Windows DirectSound", "devices": [2, 3], "default_input_device": 2, "default_output_device": 3},
        {"name": "Windows WASAPI", "devices": [4, 5, 6, 7, 8, 9], "default_input_device": 5, "default_output_device": 7},
        {"name": "Windows WDM-KS", "devices": [10, 11, 12, 13, 14], "default_input_device": 12, "default_output_device": 13},
    ]
    devices = [
        {"name": "Microphone (Realtek Audio)", "hostapi": 0, "max_input_channels": 2, "max_output_channels": 0},
        {"name": "CABLE Input (VB-Audio Virtual Cable)", "hostapi": 0, "max_input_channels": 0, "max_output_channels": 2},
        {"name": "Primary Sound Capture Driver", "hostapi": 1, "max_input_channels": 2, "max_output_channels": 0},
        {"name": "CABLE Input (VB-Audio Virtual Cable)", "hostapi": 1, "max_input_channels": 0, "max_output_channels": 2},
        {"name": "Microphone (Realtek Audio)", "hostapi": 2, "max_input_channels": 2, "max_output_channels": 0},
        {"name": "Headset Microphone (USB Audio)", "hostapi": 2, "max_input_channels": 1, "max_output_channels": 0},
        {"name": "Line In (Realtek Audio)", "hostapi": 2, "max_input_channels": 2, "max_output_channels": 0},
        {"name": "CABLE Input (VB-Audio Virtual Cable)", "hostapi": 2, "max_input_channels": 0, "max_output_channels": 2},
        {"name": "CABLE Input (16ch)", "hostapi": 2, "max_input_channels": 0, "max_output_channels": 16},
        {"name": "Speakers (Realtek Audio)", "hostapi": 2, "max_input_channels": 0, "max_output_channels": 2},
        {"name": "Mic in at front panel (black)", "hostapi": 3, "max_input_channels": 1, "max_output_channels": 0},
        {"name": "Line in at rear panel (blue)", "hostapi": 3, "max_input_channels": 1, "max_output_channels": 0},
        {"name": "Microphone (Realtek Audio)", "hostapi": 3, "max_input_channels": 2, "max_output_channels": 0},
        {"name": "CABLE Input (VB-Audio)", "hostapi": 3, "max_input_channels": 0, "max_output_channels": 2},
        {"name": "Unknown Jack 999", "hostapi": 3, "max_input_channels": 1, "max_output_channels": 0},
    ]
    return devices, hostapis


class TestHostApiResolver(unittest.TestCase):
    def setUp(self):
        self.devices, self.hostapis = make_mock_audio_env()

    def test_wdmks_pair_moves_to_wasapi_by_name(self):
        # dev 12 (WDM-KS Realtek Mic), dev 13 (WDM-KS Cable)
        pair = resolve_pair(self.devices, self.hostapis, 12, 13)
        self.assertEqual(pair.input, 4)  # WASAPI Realtek Mic
        self.assertEqual(pair.output, 7)  # WASAPI Cable Input
        self.assertTrue(pair.is_wasapi)
        self.assertFalse(pair.substituted_input)
        self.assertFalse(pair.ambiguous)
        self.assertTrue(len(pair.notes) >= 2)

    def test_unmatched_wdmks_name_uses_default_and_flags_it(self):
        # dev 14 (WDM-KS Unknown Jack 999) has no name similarity or role match
        # WASAPI default input is dev 5
        pair = resolve_pair(self.devices, self.hostapis, 14, 13)
        self.assertEqual(pair.input, 5)  # WASAPI default input
        self.assertEqual(pair.output, 7)  # WASAPI Cable
        self.assertTrue(pair.substituted_input)
        self.assertTrue(pair.ambiguous)

    def test_never_substitutes_for_a_usable_non_wdmks_device(self):
        # dev 2 is DirectSound "Primary Sound Capture Driver"
        # It has 0 similarity to WASAPI devices, but it's usable on DirectSound
        pair = resolve_pair(self.devices, self.hostapis, 2, 3)
        self.assertEqual(pair.input, 2)
        self.assertEqual(pair.output, 3)
        self.assertIn("DirectSound", pair.host_name)
        self.assertFalse(pair.substituted_input)

    def test_mme_selection_upgrades_to_wasapi(self):
        # dev 0 (MME Mic) & dev 1 (MME Cable)
        # WASAPI is higher priority and has exact match for both!
        pair = resolve_pair(self.devices, self.hostapis, 0, 1)
        self.assertEqual(pair.input, 4)
        self.assertEqual(pair.output, 7)
        self.assertTrue(pair.is_wasapi)

    def test_wasapi_pair_unchanged_no_notes(self):
        # dev 4 & dev 7 are already WASAPI
        pair = resolve_pair(self.devices, self.hostapis, 4, 7)
        self.assertEqual(pair.input, 4)
        self.assertEqual(pair.output, 7)
        self.assertEqual(pair.notes, [])

    def test_falls_back_to_mme_when_wasapi_has_no_cable(self):
        devs, apis = make_mock_audio_env()
        # Remove cable devices from WASAPI and DirectSound
        apis[1]["devices"] = [2]
        apis[2]["devices"] = [4, 5, 6, 9]
        # Starting with WDM-KS mic (12) and WDM-KS cable (13)
        pair = resolve_pair(devs, apis, 12, 13)
        # Should fall back to MME where dev 0 (Mic) and dev 1 (Cable) exist
        self.assertEqual(pair.input, 0)
        self.assertEqual(pair.output, 1)
        self.assertEqual(pair.host_name, "MME")

    def test_no_cable_anywhere_raises_clear_error(self):
        devs, apis = make_mock_audio_env()
        for d in devs:
            if "cable input" in d["name"].lower():
                d["max_output_channels"] = 0
        with self.assertRaises(DeviceResolutionError) as ctx:
            resolve_pair(devs, apis, 12, 13)
        self.assertIn("No usable microphone + virtual cable pair found", str(ctx.exception))

    def test_wdmks_only_system_raises(self):
        devs = [
            {"name": "Microphone (Realtek Audio)", "hostapi": 0, "max_input_channels": 2, "max_output_channels": 0},
            {"name": "CABLE Input (VB-Audio)", "hostapi": 0, "max_input_channels": 0, "max_output_channels": 2},
        ]
        wdmks_apis = [{"name": "Windows WDM-KS", "devices": [0, 1], "default_input_device": 0, "default_output_device": 1}]
        with self.assertRaises(DeviceResolutionError) as ctx:
            resolve_pair(devs, wdmks_apis, 0, 1)
        self.assertIn("none available", str(ctx.exception))

    def test_out_of_range_index_raises(self):
        with self.assertRaises(DeviceResolutionError) as ctx:
            resolve_pair(self.devices, self.hostapis, -1, 7)
        self.assertIn("out of range", str(ctx.exception))

        with self.assertRaises(DeviceResolutionError) as ctx:
            resolve_pair(self.devices, self.hostapis, 4, 9999)
        self.assertIn("out of range", str(ctx.exception))

    def test_result_is_never_wdmks(self):
        pair = resolve_pair(self.devices, self.hostapis, 10, 13)
        self.assertNotIn("WDM-KS", pair.host_name.upper())

    def test_prefers_16ch_last(self):
        # If both normal CABLE and 16ch are in WASAPI, output 7 (standard) is chosen over 8 (16ch)
        pair = resolve_pair(self.devices, self.hostapis, 4, 13)
        self.assertEqual(pair.output, 7)

    def test_mic_jack_finds_realtek_mic_not_usb_default(self):
        # dev 10 is "Mic in at front panel (black)"
        # Default WASAPI is dev 5 ("Headset Microphone (USB Audio)")
        # Role 'mic' matches both dev 4 and dev 5, but dev 4 has "Microphone (Realtek Audio)"
        pair = resolve_pair(self.devices, self.hostapis, 10, 13)
        # Even if multiple mics exist, dev 5 is default so it picks from mic pool
        self.assertIn(pair.input, (4, 5))
        self.assertTrue(pair.substituted_input)

    def test_unique_mic_role_beats_wrong_default(self):
        # If there is only ONE mic candidate in WASAPI and default is something else (or -1)
        devs, apis = make_mock_audio_env()
        apis[2]["devices"] = [4, 6, 7]  # dev 4 is only mic; dev 6 is Line In
        apis[2]["default_input_device"] = 6  # default is Line In!
        pair = resolve_pair(devs, apis, 10, 13)
        # dev 10 is 'mic', so it MUST pick dev 4 (Microphone), not default dev 6 (Line In)
        self.assertEqual(pair.input, 4)
        self.assertFalse(pair.ambiguous)

    def test_line_jack_finds_line_in(self):
        # dev 11 is "Line in at rear panel (blue)"
        # WASAPI has dev 6 "Line In (Realtek Audio)"
        pair = resolve_pair(self.devices, self.hostapis, 11, 13)
        self.assertEqual(pair.input, 6)

    def test_usable_device_is_not_guessed(self):
        # dev 2 is DirectSound "Primary Sound Capture Driver"
        # It should NOT be guessed as WASAPI default mic
        pair = resolve_pair(self.devices, self.hostapis, 2, 3)
        self.assertEqual(pair.input, 2)
        self.assertEqual(pair.host_name, "Windows DirectSound")

    def test_is_wasapi_flag(self):
        pair_wasapi = ResolvedPair(4, 7, "Windows WASAPI")
        self.assertTrue(pair_wasapi.is_wasapi)
        pair_mme = ResolvedPair(0, 1, "MME")
        self.assertFalse(pair_mme.is_wasapi)
        pair_ds = ResolvedPair(2, 3, "Windows DirectSound")
        self.assertFalse(pair_ds.is_wasapi)

    def test_single_input_unchanged_when_safe(self):
        # dev 4 is WASAPI (safe)
        idx, notes = resolve_single(self.devices, self.hostapis, 4, is_input=True)
        self.assertEqual(idx, 4)
        self.assertEqual(notes, [])

        # dev 0 is MME (safe)
        idx, notes = resolve_single(self.devices, self.hostapis, 0, is_input=True)
        self.assertEqual(idx, 0)
        self.assertEqual(notes, [])

    def test_single_input_wdmks_resolved_for_voice_test(self):
        # dev 12 is WDM-KS "Microphone (Realtek Audio)"
        idx, notes = resolve_single(self.devices, self.hostapis, 12, is_input=True)
        self.assertEqual(idx, 4)  # WASAPI "Microphone (Realtek Audio)"
        self.assertTrue(len(notes) > 0)

    def test_single_output_wdmks_resolved(self):
        # dev 13 is WDM-KS CABLE Input
        idx, notes = resolve_single(self.devices, self.hostapis, 13, is_input=False)
        self.assertEqual(idx, 7)  # WASAPI CABLE Input
        self.assertTrue(len(notes) > 0)

    def test_single_raises_when_nothing_safe(self):
        devs = [
            {"name": "Microphone (Realtek Audio)", "hostapi": 0, "max_input_channels": 2, "max_output_channels": 0},
        ]
        wdmks_apis = [{"name": "Windows WDM-KS", "devices": [0], "default_input_device": 0, "default_output_device": -1}]
        with self.assertRaises(DeviceResolutionError):
            resolve_single(devs, wdmks_apis, 0, is_input=True)


if __name__ == "__main__":
    unittest.main()
