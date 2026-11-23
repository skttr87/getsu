"""VB-CABLE at 48 kHz (owner decision 2026-11-24): format structure, which endpoints are touched, waiting after a fresh
install, refusals, the three callers (in-app install, installer, engine start) and the single-setter source scan."""
import ast
import ctypes
import os
import unittest
from unittest.mock import patch

import _isolation  # noqa: F401

from src import cable_format, win_audio

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
CAP_ID = "{0.0.1.00000000}.{aaaaaaaa-1111-4222-8333-000000000001}"
REN_ID = "{0.0.0.00000000}.{aaaaaaaa-1111-4222-8333-000000000002}"
MIC_ID = "{0.0.1.00000000}.{bbbbbbbb-1111-4222-8333-000000000001}"
CABLE_OUT = {"id": CAP_ID, "name": "CABLE Output (VB-Audio Virtual Cable)", "bus": "ROOT", "form_factor": None}
CABLE_IN = {"id": REN_ID, "name": "Speakers (VB-Audio Virtual Cable)", "bus": "ROOT", "form_factor": None}
REALTEK = {"id": MIC_ID, "name": "Microphone (Realtek(R) Audio)", "bus": "HDAUDIO", "form_factor": 4}
VOICEMEETER = {"id": "{0.0.1.00000000}.{cccccccc-1111-4222-8333-000000000001}",
               "name": "VoiceMeeter Output (VB-Audio VoiceMeeter VAIO)", "bus": "SWD", "form_factor": None}


def _lister(capture, render):
    return lambda is_input: list(capture if is_input else render)


class WaveFormatTests(unittest.TestCase):
    def test_sizes_match_the_windows_headers(self):
        self.assertEqual(ctypes.sizeof(win_audio.WAVEFORMATEX), 18)
        self.assertEqual(ctypes.sizeof(win_audio.WAVEFORMATEXTENSIBLE), 40)

    def test_endpoint_format_is_16_bit_pcm_stereo_48k(self):
        w = win_audio.make_wave_format(48000, 2, 16, is_float=False)
        f = w.Format
        self.assertEqual((f.wFormatTag, f.nChannels, f.nSamplesPerSec, f.wBitsPerSample), (0xFFFE, 2, 48000, 16))
        self.assertEqual((f.nBlockAlign, f.nAvgBytesPerSec, f.cbSize), (4, 192000, 22))
        self.assertEqual(w.dwChannelMask, 3)
        self.assertEqual(w.SubFormat.Data1, 1)  # KSDATAFORMAT_SUBTYPE_PCM

    def test_mix_format_is_32_bit_float(self):
        w = win_audio.make_wave_format(48000, 2, 32, is_float=True)
        self.assertEqual((w.Format.nBlockAlign, w.Format.nAvgBytesPerSec), (8, 384000))
        self.assertEqual(w.SubFormat.Data1, 3)  # KSDATAFORMAT_SUBTYPE_IEEE_FLOAT


class EnsureCable48kTests(unittest.TestCase):
    def _run(self, capture, render, formats, set_result=True, wait_s=0.0):
        with patch.object(win_audio, "native_list_active_endpoints", side_effect=_lister(capture, render)), \
                patch.object(win_audio, "native_get_endpoint_format", side_effect=lambda i: formats.get(i)), \
                patch.object(win_audio, "set_getsu_cable_format", return_value=set_result) as setter:
            results = cable_format.ensure_cable_48k(wait_s=wait_s, poll_s=0.0)
        return results, setter

    def test_cable_already_at_48k_is_left_alone(self):
        results, setter = self._run([CABLE_OUT, REALTEK], [CABLE_IN],
                                    {CAP_ID: (48000, 2, 16), REN_ID: (48000, 2, 24)})
        setter.assert_not_called()
        self.assertEqual(set(results.values()), {"ok"})

    def test_only_the_cable_side_at_44k_is_set(self):
        results, setter = self._run([CABLE_OUT, REALTEK], [CABLE_IN],
                                    {CAP_ID: (44100, 2, 24), REN_ID: (48000, 2, 24), MIC_ID: (44100, 2, 16)})
        setter.assert_called_once_with(CAP_ID, 48000)
        self.assertEqual(results[CABLE_OUT["name"]], "set")
        self.assertNotIn(REALTEK["name"], results)

    def test_other_devices_and_other_virtual_cables_are_never_touched(self):
        results, setter = self._run([REALTEK, VOICEMEETER], [], {MIC_ID: (44100, 2, 16)})
        setter.assert_not_called()
        self.assertEqual(results, {})

    def test_failure_is_reported_not_raised(self):
        results, _ = self._run([CABLE_OUT], [CABLE_IN], {CAP_ID: (44100, 2, 24), REN_ID: (48000, 2, 24)},
                               set_result=False)
        self.assertEqual(results[CABLE_OUT["name"]], "failed")

    def test_waits_for_the_endpoints_of_a_fresh_install(self):
        calls = {"n": 0}

        def lister(is_input):
            calls["n"] += 1
            if calls["n"] <= 2:  # first round: the cable is not registered yet
                return []
            return [CABLE_OUT] if is_input else [CABLE_IN]

        with patch.object(win_audio, "native_list_active_endpoints", side_effect=lister), \
                patch.object(win_audio, "native_get_endpoint_format",
                             side_effect=lambda i: {CAP_ID: (44100, 2, 24), REN_ID: (44100, 2, 24)}[i]), \
                patch.object(win_audio, "set_getsu_cable_format", return_value=True) as setter, \
                patch.object(cable_format.time, "sleep"):
            results = cable_format.ensure_cable_48k(wait_s=5.0, poll_s=0.0)
        self.assertEqual(setter.call_count, 2)
        self.assertEqual(set(results.values()), {"set"})

    def test_listing_error_gives_empty_result(self):
        with patch.object(win_audio, "native_list_active_endpoints", side_effect=OSError("COM")):
            self.assertEqual(cable_format.ensure_cable_48k(), {})


class GuardedSetterTests(unittest.TestCase):
    def _set(self, endpoint_id, capture, render):
        with patch.object(win_audio, "native_list_active_endpoints", side_effect=_lister(capture, render)), \
                patch.object(win_audio, "_with_policy_config", return_value=0) as com, \
                patch.object(win_audio, "native_get_endpoint_format", return_value=(48000, 2, 16)):
            ok = win_audio.set_getsu_cable_format(endpoint_id, 48000)
        return ok, com

    def test_sets_a_getsu_cable_endpoint(self):
        ok, com = self._set(CAP_ID, [CABLE_OUT], [CABLE_IN])
        self.assertTrue(ok)
        com.assert_called_once()

    def test_refuses_a_physical_device(self):
        ok, com = self._set(MIC_ID, [REALTEK, CABLE_OUT], [])
        self.assertFalse(ok)
        com.assert_not_called()

    def test_refuses_another_virtual_cable(self):
        ok, com = self._set(VOICEMEETER["id"], [VOICEMEETER], [])
        self.assertFalse(ok)
        com.assert_not_called()

    def test_refuses_an_inactive_or_invalid_id(self):
        for bad in (CAP_ID, "not-an-id", "", None):
            ok, com = self._set(bad, [], [])
            self.assertFalse(ok, bad)
            com.assert_not_called()


class CallerTests(unittest.TestCase):
    def test_installer_sets_the_format_after_installing_the_cable(self):
        with open(os.path.join(ROOT, "setup.iss"), encoding="utf-8") as f:
            iss = f.read()
        install = iss.index("'-i -h'")
        fmt = iss.index("'--set-cable-format'")
        self.assertLess(install, fmt)
        self.assertIn("ExpandConstant('{app}\\{#MyAppExeName}')", iss[fmt - 120:fmt])

    def test_cli_flag_exits_before_the_single_instance_check_and_the_gui(self):
        with open(os.path.join(ROOT, "src", "main.py"), encoding="utf-8") as f:
            main_src = f.read()
        skip_list = main_src[main_src.index("if not any(arg in sys.argv"):main_src.index("activate_existing_instance(restore")]
        self.assertIn('"--set-cable-format"', skip_list)
        self.assertLess(main_src.index('if "--set-cable-format" in sys.argv'), main_src.index("from src.gui import"))

    def test_in_app_install_sets_the_format(self):
        with open(os.path.join(ROOT, "src", "devices.py"), encoding="utf-8") as f:
            tree = ast.parse(f.read())
        fn = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "install_vbcable_driver")
        names = {n.func.id for n in ast.walk(fn) if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
        self.assertIn("ensure_cable_48k", names)

    def test_engine_start_skips_the_warning_when_the_cable_was_fixed(self):
        from src.stream import AudioEngine
        engine = AudioEngine.__new__(AudioEngine)
        engine.output_device = 3
        cable_in = {"index": 3, "name": "CABLE Input"}
        cable_out = {"index": 4, "name": "CABLE Output"}
        with patch("src.devices.check_vbcable_status", return_value=(True, cable_in, cable_out)), \
                patch.object(cable_format, "ensure_cable_48k", return_value={"CABLE Output": "set"}) as ensure, \
                patch("src.stream.sd.query_devices") as query:
            AudioEngine._check_cable_rate(engine)
        ensure.assert_called_once()
        query.assert_not_called()
        self.assertIsNone(engine.cable_rate_mismatch)


class SingleSetterScanTests(unittest.TestCase):
    """Only win_audio.set_getsu_cable_format may set a device format, and only cable_format.py may call it."""

    def test_set_device_format_slot_is_used_only_by_the_guarded_setter(self):
        with open(os.path.join(ROOT, "src", "win_audio.py"), encoding="utf-8") as f:
            tree = ast.parse(f.read())
        users = set()
        for fn in (n for n in tree.body if isinstance(n, ast.FunctionDef)):  # module-level functions (nested helpers count for their parent)
            for node in ast.walk(fn):
                if isinstance(node, ast.Name) and node.id == "_VT_SET_DEVICE_FORMAT":
                    users.add(fn.name)
        self.assertEqual(users, {"set_getsu_cable_format"})

    def test_only_cable_format_calls_the_setter(self):
        callers = set()
        src = os.path.join(ROOT, "src")
        for name in os.listdir(src):
            if not name.endswith(".py") or name == "win_audio.py":
                continue
            with open(os.path.join(src, name), encoding="utf-8") as f:
                if "set_getsu_cable_format" in f.read():
                    callers.add(name)
        self.assertEqual(callers, {"cable_format.py"})


if __name__ == "__main__":
    unittest.main()
