import unittest

from src.host_api_resolver import (
    _role,
    _similarity,
    _tokens,
    device_role,
    list_selectable_inputs,
    resolve_pair,
)

JABRA_RU = "Микрофон гарнитуры (Jabra EVOLVE 20 MS)"


def _env(wasapi_names, mme_names=(), cable=True):
    """WASAPI endpoints (plus optional MME copies with names truncated to 31 characters)."""
    devices, mme_ids, wasapi_ids = [], [], []
    for name in mme_names:
        devices.append({"name": name[:31], "hostapi": 0, "max_input_channels": 1, "max_output_channels": 0})
        mme_ids.append(len(devices) - 1)
    for name in wasapi_names:
        devices.append({"name": name, "hostapi": 1, "max_input_channels": 1, "max_output_channels": 0})
        wasapi_ids.append(len(devices) - 1)
    out_ids = []
    if cable:
        devices.append({"name": "CABLE Input (VB-Audio Virtual Cable)", "hostapi": 1,
                        "max_input_channels": 0, "max_output_channels": 2})
        wasapi_ids.append(len(devices) - 1)
        out_ids.append(len(devices) - 1)
    hostapis = [
        {"name": "MME", "devices": mme_ids, "default_input_device": mme_ids[0] if mme_ids else -1,
         "default_output_device": -1},
        {"name": "Windows WASAPI", "devices": wasapi_ids, "default_input_device": wasapi_ids[0],
         "default_output_device": out_ids[0] if out_ids else -1},
    ]
    return devices, hostapis


class TestTokens(unittest.TestCase):
    def test_cyrillic_words_are_kept(self):
        tokens = _tokens(JABRA_RU)
        self.assertIn("микрофон", tokens)
        self.assertIn("гарнитуры", tokens)
        self.assertIn("jabra", tokens)

    def test_case_is_folded(self):
        self.assertEqual(_tokens("USB AUDIO"), _tokens("usb audio"))
        self.assertIn("микрофон", _tokens("МИКРОФОН"))

    def test_accented_latin_words_kept(self):
        self.assertIn("micrófono", _tokens("Micrófono (USB)"))


class TestRoles(unittest.TestCase):
    def test_russian_jabra_name_is_mic(self):
        self.assertEqual(_role(JABRA_RU), "mic")
        self.assertEqual(device_role(JABRA_RU), "mic")

    def test_localised_mic_keywords(self):
        for name in ("Mikrofon (USB Audio)", "Microfone (USB)", "Micrófono (USB)", "Microphone (USB)",
                     "Mikrofon (Realtek)", "Микрофон (USB Audio)"):
            self.assertEqual(_role(name), "mic", name)

    def test_line_still_line(self):
        self.assertEqual(_role("Линия (Realtek)"), None)
        self.assertEqual(_role("Line In (Realtek Audio)"), "line")

    def test_no_role_for_plain_name(self):
        self.assertIsNone(_role("Гарнитура (USB Audio)"))


class TestSimilarity(unittest.TestCase):
    def test_cyrillic_names_do_not_collapse_to_empty(self):
        self.assertGreater(_similarity(JABRA_RU, JABRA_RU), 0.99)
        self.assertGreater(_similarity("Микрофон (USB Audio)", "Микрофон (USB Audio) "), 0.99)

    def test_fully_cyrillic_names_of_two_devices_stay_apart(self):
        a = "Микрофон (USB Audio)"
        b = "Микрофон (2- USB Audio)"
        self.assertLess(_similarity(a, b), 0.75)


class TestDedupeAndResolution(unittest.TestCase):
    def test_two_cyrillic_devices_are_listed_twice(self):
        a = "Микрофон (USB Audio)"
        b = "Микрофон (2- USB Audio)"
        devices, hostapis = _env([a, b], cable=False)
        inputs = list_selectable_inputs(devices, hostapis)
        self.assertEqual(sorted(i.name for i in inputs), sorted([a, b]))
        self.assertTrue(all(i.role == "mic" for i in inputs))

    def test_truncated_mme_copy_resolves_to_wasapi_endpoint(self):
        # MME names are cut at 31 characters; the Cyrillic name must still match its WASAPI twin.
        devices, hostapis = _env([JABRA_RU], mme_names=[JABRA_RU])
        mme_idx = 0
        pair = resolve_pair(devices, hostapis, mme_idx, len(devices) - 1)
        self.assertEqual(devices[pair.input]["name"], JABRA_RU)
        self.assertTrue(pair.is_wasapi)


if __name__ == "__main__":
    unittest.main()
