"""
Device classification from Windows data (bus enumerator and endpoint form factor) for M4.5.

The CoreAudio readers are replaced by fake property stores keyed by (friendly name, is_input).
Names include non-English ones, because classification must not depend on the Windows language.
"""
import ctypes
import inspect
import unittest
from unittest.mock import MagicMock, patch

from src import devices
from src.devices import (
    DeviceKind,
    auto_select_input_device,
    classify_device,
    get_physical_output_device,
    is_laptop_microphone,
    is_virtual_input_device,
    kind_label,
)
from src.router import (
    DeviceBusType,
    EndpointFormFactor,
    PROPVARIANT,
    _form_factor_from_variant,
    clear_device_bus_type_cache,
)


class FakeEndpointStore(unittest.TestCase):
    """Replaces the raw bus and form factor readers with dictionaries keyed by (name, is_input)."""

    def setUp(self):
        self.buses = {}
        self.form_factors = {}
        clear_device_bus_type_cache()
        self._patches = [
            patch("src.router.native_get_device_bus_type",
                  side_effect=lambda name, is_input=True: self.buses.get((name, is_input), DeviceBusType.UNKNOWN)),
            patch("src.router.native_get_device_form_factor",
                  side_effect=lambda name, is_input=True: self.form_factors.get((name, is_input))),
            patch("src.devices._start_enumeration_pass"),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in self._patches:
            p.stop()
        clear_device_bus_type_cache()

    def endpoint(self, name, bus, form_factor=None, is_input=True):
        self.buses[(name, is_input)] = bus
        if form_factor is not None:
            self.form_factors[(name, is_input)] = form_factor


class TestClassifyByBusType(FakeEndpointStore):

    def test_bluetooth_hands_free_mic_is_bluetooth_headset(self):
        self.endpoint("Headset (HUAWEI FreeBuds SE 4 ANC)", DeviceBusType.BLUETOOTH_HFP, EndpointFormFactor.HEADSET)
        self.assertEqual(classify_device("Headset (HUAWEI FreeBuds SE 4 ANC)", True), DeviceKind.BLUETOOTH_HEADSET)

    def test_bluetooth_stereo_and_le_outputs_are_bluetooth_headset(self):
        self.endpoint("Headphones (Earbuds)", DeviceBusType.BLUETOOTH_A2DP, EndpointFormFactor.HEADPHONES, is_input=False)
        self.endpoint("Earbuds LE", DeviceBusType.BLUETOOTH_LE, None, is_input=False)
        self.assertEqual(classify_device("Headphones (Earbuds)", False), DeviceKind.BLUETOOTH_HEADSET)
        self.assertEqual(classify_device("Earbuds LE", False), DeviceKind.BLUETOOTH_HEADSET)

    def test_usb_headset_cyrillic_name(self):
        name = "Микрофон гарнитуры (Jabra EVOLVE 20 MS)"
        self.endpoint(name, DeviceBusType.USB, EndpointFormFactor.HEADSET)
        self.assertEqual(classify_device(name, True), DeviceKind.USB_HEADSET)

    def test_usb_headset_class_form_factors_are_usb_headset(self):
        for ff in (EndpointFormFactor.HEADSET, EndpointFormFactor.HEADPHONES, EndpointFormFactor.HANDSET):
            name = f"usb device ff{ff}"
            self.endpoint(name, DeviceBusType.USB, ff)
            self.assertEqual(classify_device(name, True), DeviceKind.USB_HEADSET, f"form factor {ff}")

    def test_usb_microphone_form_factor_is_usb_mic(self):
        name = "Микрофон (USB Audio)"
        self.endpoint(name, DeviceBusType.USB, EndpointFormFactor.MICROPHONE)
        self.assertEqual(classify_device(name, True), DeviceKind.USB_MIC)

    def test_usb_speakers_are_other(self):
        self.endpoint("USB Speakers", DeviceBusType.USB, EndpointFormFactor.SPEAKERS, is_input=False)
        self.assertEqual(classify_device("USB Speakers", False), DeviceKind.OTHER)

    def test_usb_without_form_factor_is_other(self):
        self.endpoint("Mystery USB", DeviceBusType.USB, None)
        self.assertEqual(classify_device("Mystery USB", True), DeviceKind.OTHER)

    def test_hd_audio_microphone_is_built_in(self):
        self.endpoint("Microphone (Realtek(R) Audio)", DeviceBusType.INTERNAL_HD, EndpointFormFactor.MICROPHONE)
        self.assertEqual(classify_device("Microphone (Realtek(R) Audio)", True), DeviceKind.BUILT_IN)

    def test_hd_audio_without_form_factor_is_built_in(self):
        self.endpoint("Микрофон (Встроенный)", DeviceBusType.INTERNAL_HD, None)
        self.assertEqual(classify_device("Микрофон (Встроенный)", True), DeviceKind.BUILT_IN)

    def test_intel_audio_microphone_is_built_in(self):
        self.endpoint("Microphone Array (Intel)", DeviceBusType.INTEL_AUDIO, EndpointFormFactor.MICROPHONE)
        self.assertEqual(classify_device("Microphone Array (Intel)", True), DeviceKind.BUILT_IN)

    def test_hd_audio_headset_jack_is_wired_headset(self):
        self.endpoint("Headset Microphone (Realtek)", DeviceBusType.INTERNAL_HD, EndpointFormFactor.HEADSET)
        self.assertEqual(classify_device("Headset Microphone (Realtek)", True), DeviceKind.WIRED_HEADSET)

    def test_hd_audio_headphone_jack_output_is_wired_headset(self):
        self.endpoint("Headphones (Realtek)", DeviceBusType.INTERNAL_HD, EndpointFormFactor.HEADPHONES, is_input=False)
        self.assertEqual(classify_device("Headphones (Realtek)", False), DeviceKind.WIRED_HEADSET)

    def test_intel_audio_headset_jack_is_wired_headset(self):
        self.endpoint("Headset (Intel)", DeviceBusType.INTEL_AUDIO, EndpointFormFactor.HEADSET)
        self.assertEqual(classify_device("Headset (Intel)", True), DeviceKind.WIRED_HEADSET)

    def test_hd_audio_line_level_is_line_in(self):
        self.endpoint("Line In (Realtek)", DeviceBusType.INTERNAL_HD, EndpointFormFactor.LINE_LEVEL)
        self.assertEqual(classify_device("Line In (Realtek)", True), DeviceKind.LINE_IN)

    def test_usb_line_level_is_other_not_line_in(self):
        self.endpoint("USB Line", DeviceBusType.USB, EndpointFormFactor.LINE_LEVEL)
        self.assertEqual(classify_device("USB Line", True), DeviceKind.OTHER)

    def test_swd_is_virtual(self):
        self.endpoint("CABLE Output (VB-Audio Virtual Cable)", DeviceBusType.VIRTUAL)
        self.assertEqual(classify_device("CABLE Output (VB-Audio Virtual Cable)", True), DeviceKind.VIRTUAL)

    def test_unknown_enumerator_is_other(self):
        self.endpoint("Mystery Mic", "FOOBUS")
        self.assertEqual(classify_device("Mystery Mic", True), DeviceKind.OTHER)

    def test_root_enumerator_is_virtual(self):
        self.endpoint("CABLE Output (VB-Audio Virtual Cable)", DeviceBusType.ROOT)
        self.assertEqual(classify_device("CABLE Output (VB-Audio Virtual Cable)", True), DeviceKind.VIRTUAL)
        self.endpoint("Other Root Device", DeviceBusType.ROOT)
        self.assertEqual(classify_device("Other Root Device", True), DeviceKind.VIRTUAL)

    def test_unknown_bus_without_virtual_name_is_other(self):
        self.assertEqual(classify_device("Some Microphone", True), DeviceKind.OTHER)

    def test_empty_name_is_other(self):
        self.assertEqual(classify_device("", True), DeviceKind.OTHER)

    def test_name_with_dot_is_classified_by_name(self):
        self.endpoint("Speakers (2.1 Audio)", DeviceBusType.INTERNAL_HD, EndpointFormFactor.SPEAKERS, is_input=False)
        self.assertEqual(classify_device("Speakers (2.1 Audio)", False), DeviceKind.BUILT_IN)

    def test_direction_is_passed_to_readers(self):
        self.endpoint("Speakers (Realtek(R) Audio)", DeviceBusType.INTERNAL_HD, EndpointFormFactor.SPEAKERS, is_input=False)
        self.assertEqual(classify_device("Speakers (Realtek(R) Audio)", False), DeviceKind.BUILT_IN)
        self.assertEqual(classify_device("Speakers (Realtek(R) Audio)", True), DeviceKind.OTHER)


class TestVirtualDetection(FakeEndpointStore):

    def test_cable_name_is_virtual_even_when_bus_is_known(self):
        self.endpoint("CABLE Output (VB-Audio)", DeviceBusType.INTERNAL_HD)
        self.assertTrue(is_virtual_input_device("CABLE Output (VB-Audio)"))
        self.assertEqual(classify_device("CABLE Output (VB-Audio)", True), DeviceKind.VIRTUAL)

    def test_root_bus_is_virtual(self):
        self.endpoint("CABLE Output (VB-Audio Virtual Cable)", DeviceBusType.ROOT)
        self.assertTrue(is_virtual_input_device("CABLE Output (VB-Audio Virtual Cable)"))

    def test_product_fallback_names_ignored_when_bus_known(self):
        self.endpoint("Krisp Microphone", DeviceBusType.USB, EndpointFormFactor.MICROPHONE)
        self.assertFalse(is_virtual_input_device("Krisp Microphone"))

    def test_unknown_bus_uses_generic_cable_names(self):
        for name in ("CABLE Output (VB-Audio Virtual Cable)", "VB-Audio Virtual Cable", "Virtual Cable 2"):
            self.assertTrue(is_virtual_input_device(name), name)

    def test_unknown_bus_uses_product_fallback(self):
        for name in ("Krisp Microphone", "NVIDIA Broadcast Microphone", "Elgato Wave Link Mic"):
            self.assertTrue(is_virtual_input_device(name), name)

    def test_physical_devices_not_virtual(self):
        self.endpoint("Headset (HUAWEI FreeBuds SE 4 ANC)", DeviceBusType.BLUETOOTH_HFP)
        self.assertFalse(is_virtual_input_device("Headset (HUAWEI FreeBuds SE 4 ANC)"))
        self.assertFalse(is_virtual_input_device("Микрофон гарнитуры (Jabra EVOLVE 20 MS)"))


class TestFormFactorDecoding(unittest.TestCase):

    def test_microsoft_enum_values(self):
        # Source: https://learn.microsoft.com/en-us/windows/win32/api/mmdeviceapi/ne-mmdeviceapi-endpointformfactor
        # Declared in this order with the first member = 0 (RemoteNetworkDevice).
        values = [
            "REMOTE_NETWORK_DEVICE", "SPEAKERS", "LINE_LEVEL", "HEADPHONES", "MICROPHONE", "HEADSET",
            "HANDSET", "UNKNOWN_DIGITAL_PASSTHROUGH", "SPDIF", "DIGITAL_AUDIO_DISPLAY_DEVICE", "UNKNOWN_FORM_FACTOR",
        ]
        for expected, member in enumerate(values):
            self.assertEqual(getattr(EndpointFormFactor, member), expected, member)

    def test_ui4_variant_decodes_to_form_factor(self):
        pv = PROPVARIANT()
        pv.vt = 19  # VT_UI4
        ctypes.c_uint32.from_buffer(pv, 8).value = EndpointFormFactor.HEADSET
        self.assertEqual(_form_factor_from_variant(pv), EndpointFormFactor.HEADSET)

    def test_non_ui4_variant_is_none(self):
        pv = PROPVARIANT()
        pv.vt = 31  # VT_LPWSTR
        self.assertIsNone(_form_factor_from_variant(pv))


class TestKindLabel(unittest.TestCase):

    def test_input_labels(self):
        self.assertEqual(kind_label("bluetooth_headset"), "Bluetooth headset")
        self.assertEqual(kind_label("usb_headset"), "USB headset")
        self.assertEqual(kind_label("usb_mic"), "USB microphone")
        self.assertEqual(kind_label("wired_headset"), "Wired headset")
        self.assertEqual(kind_label("built_in"), "Built-in microphone")
        self.assertEqual(kind_label("line_in"), "Line in")
        self.assertEqual(kind_label("virtual"), "Virtual device")
        self.assertEqual(kind_label("other"), "Microphone")
        self.assertEqual(kind_label("nonsense"), "Microphone")

    def test_output_labels(self):
        self.assertEqual(kind_label("bluetooth_headset", is_input=False), "Bluetooth headphones")
        self.assertEqual(kind_label("usb_headset", is_input=False), "USB headset")
        self.assertEqual(kind_label("usb_mic", is_input=False), "USB device")
        self.assertEqual(kind_label("wired_headset", is_input=False), "Wired headphones")
        self.assertEqual(kind_label("built_in", is_input=False), "Built-in speakers")
        self.assertEqual(kind_label("line_in", is_input=False), "Line out")
        self.assertEqual(kind_label("virtual", is_input=False), "Virtual device")
        self.assertEqual(kind_label("other", is_input=False), "Speakers")
        self.assertEqual(kind_label("nonsense", is_input=False), "Speakers")


class TestAutoSelectInput(FakeEndpointStore):

    def setUp(self):
        super().setUp()
        self.sd = MagicMock()
        self.sd.query_hostapis.return_value = [{'name': 'Windows WASAPI', 'default_input_device': -1}]
        self.sd.default.device = (-1, -1)
        self._sd_patch = patch("src.devices.sd", self.sd)
        self._sd_patch.start()

    def tearDown(self):
        self._sd_patch.stop()
        super().tearDown()

    def set_default(self, index):
        """Windows default recording endpoint (WASAPI) and PortAudio default both point at index."""
        self.sd.query_hostapis.return_value = [{'name': 'Windows WASAPI', 'default_input_device': index}]
        self.sd.default.device = (index, -1)

    def run_select(self, inputs):
        with patch("src.devices.get_input_devices", return_value=inputs):
            return auto_select_input_device()

    def test_usb_headset_beats_windows_default_and_built_in(self):
        self.endpoint("Microphone (Realtek(R) Audio)", DeviceBusType.INTERNAL_HD, EndpointFormFactor.MICROPHONE)
        self.endpoint("Микрофон гарнитуры (Jabra EVOLVE 20 MS)", DeviceBusType.USB, EndpointFormFactor.HEADSET)
        self.set_default(0)
        inputs = [
            {'index': 0, 'name': "Microphone (Realtek(R) Audio)"},
            {'index': 2, 'name': "Микрофон гарнитуры (Jabra EVOLVE 20 MS)"},
        ]
        self.assertEqual(self.run_select(inputs)['index'], 2)

    def test_bluetooth_default_plus_usb_headset_picks_usb_headset(self):
        self.endpoint("Headset (HUAWEI FreeBuds SE 4 ANC)", DeviceBusType.BLUETOOTH_HFP, EndpointFormFactor.HEADSET)
        self.endpoint("USB Headset", DeviceBusType.USB, EndpointFormFactor.HEADSET)
        self.set_default(1)
        inputs = [
            {'index': 1, 'name': "Headset (HUAWEI FreeBuds SE 4 ANC)"},
            {'index': 3, 'name': "USB Headset"},
        ]
        self.assertEqual(self.run_select(inputs)['index'], 3)

    def test_wired_headset_beats_usb_microphone_and_default(self):
        self.endpoint("Studio Mic", DeviceBusType.USB, EndpointFormFactor.MICROPHONE)
        self.endpoint("Headset Microphone (Realtek)", DeviceBusType.INTERNAL_HD, EndpointFormFactor.HEADSET)
        self.set_default(0)
        inputs = [
            {'index': 0, 'name': "Studio Mic"},
            {'index': 1, 'name': "Headset Microphone (Realtek)"},
        ]
        self.assertEqual(self.run_select(inputs)['index'], 1)

    def test_usb_microphone_beats_windows_default(self):
        self.endpoint("Microphone (Realtek(R) Audio)", DeviceBusType.INTERNAL_HD, EndpointFormFactor.MICROPHONE)
        self.endpoint("Studio Mic", DeviceBusType.USB, EndpointFormFactor.MICROPHONE)
        self.set_default(0)
        inputs = [
            {'index': 0, 'name': "Microphone (Realtek(R) Audio)"},
            {'index': 1, 'name': "Studio Mic"},
        ]
        self.assertEqual(self.run_select(inputs)['index'], 1)

    def test_only_bluetooth_is_default_and_built_in_present_picks_bluetooth(self):
        self.endpoint("Headset (HUAWEI FreeBuds SE 4 ANC)", DeviceBusType.BLUETOOTH_HFP, EndpointFormFactor.HEADSET)
        self.endpoint("Microphone (Realtek(R) Audio)", DeviceBusType.INTERNAL_HD, EndpointFormFactor.MICROPHONE)
        self.set_default(0)
        inputs = [
            {'index': 0, 'name': "Headset (HUAWEI FreeBuds SE 4 ANC)"},
            {'index': 1, 'name': "Microphone (Realtek(R) Audio)"},
        ]
        self.assertEqual(self.run_select(inputs)['index'], 0)

    def test_bluetooth_without_default_beats_built_in(self):
        self.endpoint("Headset (HUAWEI FreeBuds SE 4 ANC)", DeviceBusType.BLUETOOTH_HFP, EndpointFormFactor.HEADSET)
        self.endpoint("Microphone (Realtek(R) Audio)", DeviceBusType.INTERNAL_HD, EndpointFormFactor.MICROPHONE)
        inputs = [
            {'index': 1, 'name': "Microphone (Realtek(R) Audio)"},
            {'index': 0, 'name': "Headset (HUAWEI FreeBuds SE 4 ANC)"},
        ]
        self.assertEqual(self.run_select(inputs)['index'], 0)

    def test_built_in_default_is_used_before_bluetooth(self):
        # Order rule 3: a Windows default that is not Bluetooth wins over Bluetooth and built-in.
        self.endpoint("Headset (HUAWEI FreeBuds SE 4 ANC)", DeviceBusType.BLUETOOTH_HFP, EndpointFormFactor.HEADSET)
        self.endpoint("Microphone (Realtek(R) Audio)", DeviceBusType.INTERNAL_HD, EndpointFormFactor.MICROPHONE)
        self.set_default(1)
        inputs = [
            {'index': 0, 'name': "Headset (HUAWEI FreeBuds SE 4 ANC)"},
            {'index': 1, 'name': "Microphone (Realtek(R) Audio)"},
        ]
        self.assertEqual(self.run_select(inputs)['index'], 1)

    def test_windows_default_other_beats_built_in(self):
        self.endpoint("Microphone (Realtek(R) Audio)", DeviceBusType.INTERNAL_HD, EndpointFormFactor.MICROPHONE)
        self.endpoint("Other Mic", DeviceBusType.UNKNOWN)
        self.set_default(1)
        inputs = [
            {'index': 0, 'name': "Microphone (Realtek(R) Audio)"},
            {'index': 1, 'name': "Other Mic"},
        ]
        self.assertEqual(self.run_select(inputs)['index'], 1)

    def test_portaudio_default_is_used_when_wasapi_default_missing(self):
        self.endpoint("Microphone (Realtek(R) Audio)", DeviceBusType.INTERNAL_HD, EndpointFormFactor.MICROPHONE)
        self.endpoint("Other Mic", DeviceBusType.UNKNOWN)
        self.sd.default.device = (1, -1)
        inputs = [
            {'index': 0, 'name': "Microphone (Realtek(R) Audio)"},
            {'index': 1, 'name': "Other Mic"},
        ]
        self.assertEqual(self.run_select(inputs)['index'], 1)

    def test_virtual_default_is_never_selected(self):
        self.endpoint("CABLE Output (VB-Audio Virtual Cable)", DeviceBusType.VIRTUAL)
        self.endpoint("Microphone (Realtek(R) Audio)", DeviceBusType.INTERNAL_HD, EndpointFormFactor.MICROPHONE)
        self.set_default(1)
        inputs = [
            {'index': 0, 'name': "Microphone (Realtek(R) Audio)"},
            {'index': 1, 'name': "CABLE Output (VB-Audio Virtual Cable)"},
        ]
        self.assertEqual(self.run_select(inputs)['index'], 0)

    def test_built_in_chosen_when_no_default_and_no_headset(self):
        self.endpoint("Microphone (Realtek(R) Audio)", DeviceBusType.INTERNAL_HD, EndpointFormFactor.MICROPHONE)
        self.endpoint("Line Thing", DeviceBusType.USB, EndpointFormFactor.LINE_LEVEL)
        inputs = [
            {'index': 4, 'name': "Line Thing"},
            {'index': 0, 'name': "Microphone (Realtek(R) Audio)"},
        ]
        self.assertEqual(self.run_select(inputs)['index'], 0)

    def test_first_input_when_nothing_matches(self):
        inputs = [{'index': 5, 'name': "Unknown A"}, {'index': 6, 'name': "Unknown B"}]
        self.assertEqual(self.run_select(inputs)['index'], 5)


class TestPhysicalOutputRanking(FakeEndpointStore):

    def test_headphones_form_factor_beats_speakers(self):
        outputs = [
            {'index': 1, 'name': 'Speakers (Realtek(R) Audio)', 'hostapi': 'Windows WASAPI', 'inputs': 0, 'outputs': 2},
            {'index': 2, 'name': 'Наушники (Гарнитура)', 'hostapi': 'Windows WASAPI', 'inputs': 0, 'outputs': 2},
        ]
        self.endpoint('Speakers (Realtek(R) Audio)', DeviceBusType.INTERNAL_HD, EndpointFormFactor.SPEAKERS, is_input=False)
        self.endpoint('Наушники (Гарнитура)', DeviceBusType.UNKNOWN, EndpointFormFactor.HEADPHONES, is_input=False)
        with patch("src.devices.get_all_devices", return_value=outputs), \
                patch("src.router.native_get_current_default_playback", return_value=(None, "Unknown")):
            selected = get_physical_output_device()
        self.assertEqual(selected['name'], 'Наушники (Гарнитура)')

    def test_without_form_factor_first_output_is_used(self):
        outputs = [
            {'index': 1, 'name': 'Speakers A', 'hostapi': 'Windows WASAPI', 'inputs': 0, 'outputs': 2},
            {'index': 2, 'name': 'Speakers B', 'hostapi': 'Windows WASAPI', 'inputs': 0, 'outputs': 2},
        ]
        with patch("src.devices.get_all_devices", return_value=outputs), \
                patch("src.router.native_get_current_default_playback", return_value=(None, "Unknown")):
            selected = get_physical_output_device()
        self.assertEqual(selected['name'], 'Speakers A')


class TestLaptopMicrophone(FakeEndpointStore):

    def test_built_in_kind_is_laptop_microphone(self):
        self.endpoint("Microphone Array (Intel Smart Sound)", DeviceBusType.INTEL_AUDIO, EndpointFormFactor.MICROPHONE)
        self.assertTrue(is_laptop_microphone("Microphone Array (Intel Smart Sound)"))

    def test_other_kinds_are_not_laptop_microphone(self):
        self.endpoint("USB Mic", DeviceBusType.USB, EndpointFormFactor.MICROPHONE)
        self.endpoint("Headset (FreeBuds)", DeviceBusType.BLUETOOTH_HFP, EndpointFormFactor.HEADSET)
        self.endpoint("Headset Microphone (Realtek)", DeviceBusType.INTERNAL_HD, EndpointFormFactor.HEADSET)
        self.assertFalse(is_laptop_microphone("USB Mic"))
        self.assertFalse(is_laptop_microphone("Headset (FreeBuds)"))
        self.assertFalse(is_laptop_microphone("Headset Microphone (Realtek)"))
        self.assertFalse(is_laptop_microphone("Unknown Mic"))
        self.assertFalse(is_laptop_microphone(""))


class TestNoBrandKeywords(unittest.TestCase):

    FORBIDDEN_BRAND_TOKENS = [
        "mpow", "jabra", "razer", "logitech", "corsair", "hyperx", "steelseries", "realtek",
        "freebuds", "airpods", "soundcore", "anker", "yeti", "fifine", "focusrite", "behringer",
        "sennheiser", "plantronics", "shure", "audio-technica", "jbl", "epos", "rode",
        "wh-", "wf-", "buds", "soundflower",
    ]

    def test_old_keyword_lists_are_gone(self):
        self.assertFalse(hasattr(devices, "HEADSET_KEYWORDS"))
        self.assertFalse(hasattr(devices, "OUTPUT_HEADPHONE_KEYWORDS"))

    def test_no_brand_tokens_in_devices_source(self):
        source = inspect.getsource(devices)
        start = source.index("VIRTUAL_PRODUCT_FALLBACK = [")
        end = source.index("]", start)
        outside = source[:start] + source[end:]
        lowered = outside.lower()
        for token in self.FORBIDDEN_BRAND_TOKENS:
            self.assertNotIn(token, lowered, f"brand token left in devices.py: {token}")

    def test_virtual_product_fallback_is_limited_to_documented_entries(self):
        self.assertEqual(
            devices.VIRTUAL_PRODUCT_FALLBACK,
            ["voicemeeter", "rtx voice", "nvidia broadcast", "krisp", "elgato wave link"],
        )


if __name__ == "__main__":
    unittest.main()
