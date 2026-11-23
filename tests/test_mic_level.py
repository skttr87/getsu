"""Recording levels when noise cancellation turns on (owner decision 2026-11-24): microphone raised once from below 65 %
to 95 %, then the user's choice is respected; Getsu's own cable kept at 100 %; the single guarded setter."""
import ast
import os
import unittest
from unittest.mock import MagicMock, patch

import _isolation  # noqa: F401

from src import config as cfg_mod
from src import mic_level, win_audio

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
BUDS = {"id": "{0.0.1.00000000}.{11111111-0000-4000-8000-000000000001}", "name": "Headset (HUAWEI FreeBuds SE 4 ANC)",
        "bus": "BTHHFENUM", "form_factor": 5}
REALTEK = {"id": "{0.0.1.00000000}.{11111111-0000-4000-8000-000000000002}", "name": "Microphone (Realtek(R) Audio)",
           "bus": "HDAUDIO", "form_factor": 4}
CABLE_OUT = {"id": "{0.0.1.00000000}.{11111111-0000-4000-8000-000000000003}",
             "name": "CABLE Output (VB-Audio Virtual Cable)", "bus": "ROOT", "form_factor": None}
CABLE_IN = {"id": "{0.0.0.00000000}.{11111111-0000-4000-8000-000000000004}",
            "name": "CABLE Input (VB-Audio Virtual Cable)", "bus": "ROOT", "form_factor": None}
SPEAKERS = {"id": "{0.0.0.00000000}.{11111111-0000-4000-8000-000000000005}", "name": "Speakers (Realtek(R) Audio)",
            "bus": "HDAUDIO", "form_factor": 1}
VOICEMEETER = {"id": "{0.0.1.00000000}.{11111111-0000-4000-8000-000000000006}",
               "name": "VoiceMeeter Output (VB-Audio VoiceMeeter VAIO)", "bus": "SWD", "form_factor": None}
CAPTURE = [BUDS, REALTEK, CABLE_OUT, VOICEMEETER]
RENDER = [CABLE_IN, SPEAKERS]


class _Levels:
    """Fake Windows levels: reads from a dict, records every set."""

    def __init__(self, levels):
        self.levels = dict(levels)
        self.sets = []

    def get(self, endpoint_id):
        return self.levels.get(endpoint_id)

    def set(self, endpoint_id, level):
        self.sets.append((endpoint_id, round(level, 2)))
        self.levels[endpoint_id] = level
        return True


def _patched(levels):
    return (patch.object(win_audio, "native_list_active_endpoints",
                         side_effect=lambda is_input: list(CAPTURE if is_input else RENDER)),
            patch.object(win_audio, "native_get_endpoint_level", side_effect=levels.get),
            patch.object(win_audio, "set_endpoint_level_guarded", side_effect=levels.set))


class MicrophoneLevelTests(unittest.TestCase):
    def _check(self, levels, config, name=BUDS["name"]):
        save = MagicMock()
        p = _patched(levels)
        with p[0], p[1], p[2]:
            note = mic_level.check_microphone_level(config, name, save=save)
        return note, save

    def test_level_7_is_raised_once_to_95_and_remembered(self):
        levels, config = _Levels({BUDS["id"]: 0.07}), {"mic_level_raised": []}
        note, save = self._check(levels, config)
        self.assertEqual(levels.sets, [(BUDS["id"], 0.95)])
        self.assertEqual(config["mic_level_raised"], [BUDS["id"]])
        save.assert_called_once_with(config)
        self.assertEqual(note, ("Microphone level raised from 7 % to 95 %", "info"))

    def test_after_one_raise_a_low_level_is_respected_and_only_hinted(self):
        levels, config = _Levels({BUDS["id"]: 0.40}), {"mic_level_raised": [BUDS["id"]]}
        note, save = self._check(levels, config)
        self.assertEqual(levels.sets, [])
        save.assert_not_called()
        self.assertEqual(note[1], "warn")
        self.assertIn("40 %", note[0])

    def test_65_and_above_is_left_alone_without_a_message(self):
        for level in (0.65, 0.80, 1.0):
            levels = _Levels({BUDS["id"]: level})
            note, _ = self._check(levels, {"mic_level_raised": []})
            self.assertEqual(levels.sets, [], level)
            self.assertIsNone(note, level)

    def test_just_below_65_is_raised(self):
        levels = _Levels({BUDS["id"]: 0.64})
        self._check(levels, {"mic_level_raised": []})
        self.assertEqual(levels.sets, [(BUDS["id"], 0.95)])

    def test_each_microphone_is_remembered_separately(self):
        levels = _Levels({REALTEK["id"]: 0.30})
        config = {"mic_level_raised": [BUDS["id"]]}
        self._check(levels, config, name=REALTEK["name"])
        self.assertEqual(levels.sets, [(REALTEK["id"], 0.95)])
        self.assertEqual(config["mic_level_raised"], [BUDS["id"], REALTEK["id"]])

    def test_virtual_devices_and_unknown_names_are_never_touched(self):
        for name in (VOICEMEETER["name"], CABLE_OUT["name"], "Not connected mic", None):
            levels = _Levels({VOICEMEETER["id"]: 0.1, CABLE_OUT["id"]: 0.1})
            note, _ = self._check(levels, {"mic_level_raised": []}, name=name)
            self.assertEqual(levels.sets, [], name)
            self.assertIsNone(note, name)

    def test_failed_raise_gives_a_hint_and_is_not_remembered(self):
        levels, config = _Levels({BUDS["id"]: 0.07}), {"mic_level_raised": []}
        p = _patched(levels)
        with p[0], p[1], patch.object(win_audio, "set_endpoint_level_guarded", return_value=False):
            note = mic_level.check_microphone_level(config, BUDS["name"], save=MagicMock())
        self.assertEqual(config["mic_level_raised"], [])
        self.assertEqual(note[1], "warn")


class CableLevelTests(unittest.TestCase):
    def test_cable_below_100_is_set_to_100_and_other_devices_are_not(self):
        levels = _Levels({CABLE_OUT["id"]: 0.35, CABLE_IN["id"]: 1.0, SPEAKERS["id"]: 0.2, REALTEK["id"]: 0.1})
        p = _patched(levels)
        with p[0], p[1], p[2]:
            results = mic_level.keep_cable_at_full_level()
        self.assertEqual(levels.sets, [(CABLE_OUT["id"], 1.0)])
        self.assertEqual(results[CABLE_OUT["name"]], "set")
        self.assertEqual(results[CABLE_IN["name"]], "ok")

    def test_apply_levels_on_start_never_raises(self):
        with patch.object(win_audio, "native_list_active_endpoints", side_effect=OSError("COM")):
            self.assertIsNone(mic_level.apply_levels_on_start({"mic_level_raised": []}, BUDS["name"], save=MagicMock()))


class GuardedSetterTests(unittest.TestCase):
    def _set(self, endpoint):
        with patch.object(win_audio, "native_list_active_endpoints",
                          side_effect=lambda is_input: list(CAPTURE if is_input else RENDER)), \
                patch.object(win_audio, "_with_endpoint_volume", return_value=0) as com, \
                patch.object(win_audio, "native_get_endpoint_level", return_value=0.95):
            ok = win_audio.set_endpoint_level_guarded(endpoint["id"] if endpoint else "x", 0.95)
        return ok, com

    def test_physical_microphone_and_getsu_cable_are_allowed(self):
        for ep in (BUDS, REALTEK, CABLE_OUT, CABLE_IN):
            ok, com = self._set(ep)
            self.assertTrue(ok, ep["name"])
            com.assert_called_once()

    def test_speakers_and_other_virtual_devices_are_refused(self):
        for ep in (SPEAKERS, VOICEMEETER, None):
            ok, com = self._set(ep)
            self.assertFalse(ok)
            com.assert_not_called()


class ConfigTests(unittest.TestCase):
    def test_default_and_sanitize(self):
        self.assertEqual(cfg_mod.DEFAULT_CONFIG["mic_level_raised"], [])
        for bad, good in ((None, []), ("x", []), ([1, "id"], ["id"])):
            c = dict(cfg_mod.DEFAULT_CONFIG, mic_level_raised=bad)
            self.assertTrue(cfg_mod.sanitize_config(c))
            self.assertEqual(c["mic_level_raised"], good)


class WiringTests(unittest.TestCase):
    def test_levels_run_when_noise_cancellation_starts_before_the_engine(self):
        with open(os.path.join(ROOT, "src", "gui.py"), encoding="utf-8") as f:
            src = f.read()
        worker = src[src.index("def _start_worker"):]
        self.assertLess(worker.index("apply_levels_on_start("), worker.index("self.engine.start()"))

    def test_only_mic_level_calls_the_level_setter_and_only_one_function_uses_the_slot(self):
        callers = set()
        for name in os.listdir(os.path.join(ROOT, "src")):
            if name.endswith(".py") and name != "win_audio.py":
                with open(os.path.join(ROOT, "src", name), encoding="utf-8") as f:
                    if "set_endpoint_level_guarded" in f.read():
                        callers.add(name)
        self.assertEqual(callers, {"mic_level.py"})
        with open(os.path.join(ROOT, "src", "win_audio.py"), encoding="utf-8") as f:
            tree = ast.parse(f.read())
        users = {fn.name for fn in tree.body if isinstance(fn, ast.FunctionDef)
                 for n in ast.walk(fn) if isinstance(n, ast.Name) and n.id == "_VT_EV_SET_LEVEL_SCALAR"}
        self.assertEqual(users, {"set_endpoint_level_guarded"})


if __name__ == "__main__":
    unittest.main()
