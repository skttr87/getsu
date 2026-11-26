"""Cable visibility (owner decision 2026-11-26): the guarded setter, the marker rules, show/hide, the CLI flag, the
GUI and tray exit order, the first-run and install marks, the installer strings and the config migration.
COM, winreg and the config file are mocked; no device is hidden or shown."""
import ctypes
import os
import shutil
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import _isolation  # noqa: F401

from src import cable_visibility, win_audio

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
CAP_ID = "{0.0.1.00000000}.{aaaaaaaa-1111-4222-8333-000000000001}"
REN_ID = "{0.0.0.00000000}.{aaaaaaaa-1111-4222-8333-000000000002}"
MIC_ID = "{0.0.1.00000000}.{bbbbbbbb-1111-4222-8333-000000000001}"
VOICEMEETER_ID = "{0.0.1.00000000}.{cccccccc-1111-4222-8333-000000000001}"
REALTEK = {"id": MIC_ID, "name": "Microphone (Realtek(R) Audio)", "bus": "HDAUDIO", "form_factor": 4, "state": "active"}
VOICEMEETER = {"id": VOICEMEETER_ID, "name": "VoiceMeeter Output (VB-Audio VoiceMeeter VAIO)", "bus": "SWD",
               "form_factor": None, "state": "active"}


def _entry(endpoint_id, name, state, is_input=True):
    return {"id": endpoint_id, "name": name, "is_input": is_input, "state": state, "bus": "ROOT", "form_factor": None}


CABLE_OUT = _entry(CAP_ID, "CABLE Output (VB-Audio Virtual Cable)", "active", True)
CABLE_IN = _entry(REN_ID, "CABLE Input (VB-Audio Virtual Cable)", "active", False)


def _enumerate(entries_by_flow):
    """Stand-in for win_audio._enumerate_endpoints keyed by the dataflow flag."""
    def fake(is_input, state_mask):
        return [dict(e) for e in entries_by_flow.get(is_input, []) if e["state"] in _states(state_mask)]
    return fake


def _states(mask):
    names = set()
    if mask & win_audio.DEVICE_STATE_ACTIVE:
        names.add("active")
    if mask & win_audio.DEVICE_STATE_DISABLED:
        names.add("disabled")
    return names


class ListerTests(unittest.TestCase):
    def test_lister_asks_for_active_and_disabled_and_keeps_only_getsu_cable(self):
        flows = {True: [CABLE_OUT, dict(REALTEK, state="disabled")], False: [CABLE_IN, _entry(
            "{0.0.0.00000000}.{dddddddd-1111-4222-8333-000000000001}", "Speakers (Realtek)", "active", False)]}
        with patch.object(win_audio, "_enumerate_endpoints", side_effect=_enumerate(flows)) as enum:
            found = win_audio.native_list_getsu_cable_endpoints()
        masks = {c.args[1] for c in enum.call_args_list}
        self.assertEqual(masks, {win_audio.DEVICE_STATE_ACTIVE | win_audio.DEVICE_STATE_DISABLED})
        self.assertEqual({(e["id"], e["is_input"], e["state"]) for e in found},
                         {(CAP_ID, True, "active"), (REN_ID, False, "active")})

    def test_hidden_cable_endpoint_is_listed_as_disabled(self):
        flows = {True: [dict(CABLE_OUT, state="disabled")], False: []}
        with patch.object(win_audio, "_enumerate_endpoints", side_effect=_enumerate(flows)):
            found = win_audio.native_list_getsu_cable_endpoints()
        self.assertEqual([(e["id"], e["state"]) for e in found], [(CAP_ID, "disabled")])


class GuardedVisibilitySetterTests(unittest.TestCase):
    def _run(self, visible, entries_by_flow, with_policy):
        calls = []

        @ctypes.WINFUNCTYPE(ctypes.c_int, ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_int)
        def set_visibility(p_policy, dev_id, vis):
            calls.append((dev_id, vis))
            return 0

        vtable = (ctypes.c_void_p * 20)()
        vtable[14] = ctypes.cast(set_visibility, ctypes.c_void_p).value
        with patch.object(win_audio, "_enumerate_endpoints", side_effect=_enumerate(entries_by_flow)), \
                patch.object(win_audio, "_with_policy_config",
                             side_effect=lambda action: action(ctypes.c_void_p(), vtable) if with_policy else None) as wp:
            ok = win_audio.set_getsu_cable_visibility(CAP_ID if entries_by_flow[True] else REN_ID, visible)
        return ok, calls, wp

    def test_refuses_a_non_cable_endpoint_without_calling_com(self):
        flows = {True: [REALTEK], False: []}
        with patch.object(win_audio, "_enumerate_endpoints", side_effect=_enumerate(flows)), \
                patch.object(win_audio, "_with_policy_config") as wp:
            self.assertFalse(win_audio.set_getsu_cable_visibility(MIC_ID, False))
            self.assertFalse(win_audio.set_getsu_cable_visibility(VOICEMEETER_ID, False))
        wp.assert_not_called()

    def test_refuses_an_id_that_is_not_listed(self):
        flows = {True: [CABLE_OUT], False: []}
        with patch.object(win_audio, "_enumerate_endpoints", side_effect=_enumerate(flows)), \
                patch.object(win_audio, "_with_policy_config") as wp:
            self.assertFalse(win_audio.set_getsu_cable_visibility(REN_ID, False))
        wp.assert_not_called()

    def test_refuses_a_malformed_id(self):
        with patch.object(win_audio, "_with_policy_config") as wp:
            self.assertFalse(win_audio.set_getsu_cable_visibility("not-an-id", True))
        wp.assert_not_called()

    def test_cable_endpoint_gets_slot_14_with_0_to_hide_and_1_to_show(self):
        flows = {True: [CABLE_OUT], False: []}
        ok, calls, _ = self._run(False, flows, with_policy=True)
        self.assertTrue(ok)
        self.assertEqual(calls, [(CAP_ID, 0)])
        ok, calls, _ = self._run(True, flows, with_policy=True)
        self.assertTrue(ok)
        self.assertEqual(calls, [(CAP_ID, 1)])

    def test_failed_com_call_is_reported_not_raised(self):
        flows = {True: [CABLE_OUT], False: []}
        with patch.object(win_audio, "_enumerate_endpoints", side_effect=_enumerate(flows)), \
                patch.object(win_audio, "_with_policy_config", side_effect=OSError("boom")):
            self.assertFalse(win_audio.set_getsu_cable_visibility(CAP_ID, False))

    def test_no_com_means_no_change(self):
        flows = {True: [CABLE_OUT], False: []}
        with patch.object(win_audio, "_enumerate_endpoints", side_effect=_enumerate(flows)), \
                patch.object(win_audio, "_with_policy_config", return_value=None):
            self.assertFalse(win_audio.set_getsu_cable_visibility(CAP_ID, True))


class MarkerTests(unittest.TestCase):
    def test_registry_marker_must_be_dword_one(self):
        import winreg
        key = MagicMock()
        key.__enter__.return_value = key
        key.__exit__.return_value = False
        with patch.object(winreg, "OpenKey", return_value=key), \
                patch.object(winreg, "QueryValueEx", return_value=(1, winreg.REG_DWORD)):
            self.assertTrue(cable_visibility._registry_marker())
        with patch.object(winreg, "OpenKey", return_value=key), \
                patch.object(winreg, "QueryValueEx", return_value=(0, winreg.REG_DWORD)):
            self.assertFalse(cable_visibility._registry_marker())
        with patch.object(winreg, "OpenKey", side_effect=OSError("missing")):
            self.assertFalse(cable_visibility._registry_marker())

    def test_config_flag_or_registry_marker(self):
        with patch.object(cable_visibility, "_registry_marker", return_value=False):
            self.assertFalse(cable_visibility.cable_installed_by_getsu({}))
            self.assertFalse(cable_visibility.cable_installed_by_getsu({"cable_installed_by_getsu": False}))
            self.assertTrue(cable_visibility.cable_installed_by_getsu({"cable_installed_by_getsu": True}))
        with patch.object(cable_visibility, "_registry_marker", return_value=True):
            self.assertTrue(cable_visibility.cable_installed_by_getsu({}))


class ShowHideTests(unittest.TestCase):
    def test_nothing_happens_without_the_marker(self):
        with patch.object(cable_visibility, "_registry_marker", return_value=False), \
                patch.object(win_audio, "native_list_getsu_cable_endpoints") as listing, \
                patch.object(win_audio, "set_getsu_cable_visibility") as setter:
            self.assertEqual(cable_visibility.show_cable({}), {})
            self.assertEqual(cable_visibility.hide_cable({}), {})
        listing.assert_not_called()
        setter.assert_not_called()

    def test_config_flag_enables_show_and_hide(self):
        listing = [dict(CABLE_OUT, state="active")]
        with patch.object(cable_visibility, "_registry_marker", return_value=False), \
                patch.object(win_audio, "native_list_getsu_cable_endpoints", return_value=listing), \
                patch.object(win_audio, "set_getsu_cable_visibility", return_value=True) as setter:
            self.assertEqual(cable_visibility.hide_cable({"cable_installed_by_getsu": True}),
                             {CABLE_OUT["name"]: "hidden"})
        setter.assert_called_once_with(CAP_ID, False)

    def test_registry_marker_enables_hide_without_config_flag(self):
        listing = [dict(CABLE_OUT, state="active")]
        with patch.object(cable_visibility, "_registry_marker", return_value=True), \
                patch.object(win_audio, "native_list_getsu_cable_endpoints", return_value=listing), \
                patch.object(win_audio, "set_getsu_cable_visibility", return_value=True) as setter:
            cable_visibility.hide_cable({})
        setter.assert_called_once_with(CAP_ID, False)

    def test_show_waits_for_active_and_reports_shown(self):
        states = [[dict(CABLE_OUT, state="disabled")], [dict(CABLE_OUT, state="active")]]
        with patch.object(cable_visibility, "_registry_marker", return_value=True), \
                patch.object(win_audio, "native_list_getsu_cable_endpoints", side_effect=states), \
                patch.object(win_audio, "set_getsu_cable_visibility", return_value=True) as setter:
            results = cable_visibility.show_cable({}, timeout_s=1.0, poll_s=0.0)
        setter.assert_called_once_with(CAP_ID, True)
        self.assertEqual(results, {CABLE_OUT["name"]: "shown"})

    def test_show_reports_failed_on_timeout(self):
        with patch.object(cable_visibility, "_registry_marker", return_value=True), \
                patch.object(win_audio, "native_list_getsu_cable_endpoints",
                             return_value=[dict(CABLE_OUT, state="disabled")]), \
                patch.object(win_audio, "set_getsu_cable_visibility", return_value=True):
            results = cable_visibility.show_cable({}, timeout_s=0.05, poll_s=0.01)
        self.assertEqual(results, {CABLE_OUT["name"]: "failed"})

    def test_show_leaves_active_endpoints_alone(self):
        with patch.object(cable_visibility, "_registry_marker", return_value=True), \
                patch.object(win_audio, "native_list_getsu_cable_endpoints",
                             return_value=[dict(CABLE_OUT, state="active")]), \
                patch.object(win_audio, "set_getsu_cable_visibility") as setter:
            results = cable_visibility.show_cable({}, timeout_s=0.0, poll_s=0.0)
        setter.assert_not_called()
        self.assertEqual(results, {CABLE_OUT["name"]: "ok"})

    def test_hide_skips_already_hidden_endpoints(self):
        listing = [dict(CABLE_OUT, state="disabled"), dict(CABLE_IN, state="active")]
        with patch.object(cable_visibility, "_registry_marker", return_value=True), \
                patch.object(win_audio, "native_list_getsu_cable_endpoints", return_value=listing), \
                patch.object(win_audio, "set_getsu_cable_visibility", return_value=True) as setter:
            results = cable_visibility.hide_cable({})
        setter.assert_called_once_with(REN_ID, False)
        self.assertEqual(results, {CABLE_OUT["name"]: "ok", CABLE_IN["name"]: "hidden"})

    def test_show_always_needs_a_marker_or_the_in_app_flag(self):
        with patch.object(cable_visibility, "_registry_marker", return_value=False), \
                patch("src.config.load_config", return_value={"cable_installed_by_getsu": False}), \
                patch.object(win_audio, "set_getsu_cable_visibility") as setter:
            self.assertEqual(cable_visibility.show_always(), {})
        setter.assert_not_called()

    def test_show_always_shows_a_cable_installed_in_the_app(self):
        """An in-app install only sets the config flag; the uninstaller must still show the cable again."""
        hidden = dict(CABLE_IN, state="disabled")
        with patch.object(cable_visibility, "_registry_marker", return_value=False), \
                patch("src.config.load_config", return_value={"cable_installed_by_getsu": True}), \
                patch.object(win_audio, "native_list_getsu_cable_endpoints", return_value=[hidden]), \
                patch.object(cable_visibility, "_wait_active", return_value=True), \
                patch.object(win_audio, "set_getsu_cable_visibility", return_value=True) as setter:
            self.assertEqual(cable_visibility.show_always(), {CABLE_IN["name"]: "shown"})
        setter.assert_called_once_with(CABLE_IN["id"], True)

    def test_failures_never_raise(self):
        with patch.object(cable_visibility, "_registry_marker", return_value=True), \
                patch.object(win_audio, "native_list_getsu_cable_endpoints", side_effect=RuntimeError("com")):
            self.assertEqual(cable_visibility.show_cable({}), {})
            self.assertEqual(cable_visibility.hide_cable({}), {})
            self.assertEqual(cable_visibility.show_always(), {})

    def test_in_app_install_records_the_flag_and_saves(self):
        config = {}
        with patch("src.config.save_config", return_value=True) as save:
            self.assertTrue(cable_visibility.mark_cable_installed_by_getsu(config))
        self.assertIs(config["cable_installed_by_getsu"], True)
        save.assert_called_once_with(config)


class CliFlagTests(unittest.TestCase):
    def test_every_mode_exits_zero(self):
        with patch.object(cable_visibility, "show_always", side_effect=RuntimeError("x")):
            self.assertEqual(cable_visibility.main_cli(["getsu.exe", "--cable-visibility", "show-always"]), 0)
        with patch.object(cable_visibility, "hide_cable", return_value={}), \
                patch("src.config.load_config", return_value={}):
            self.assertEqual(cable_visibility.main_cli(["getsu.exe", "--cable-visibility", "hide"]), 0)
        self.assertEqual(cable_visibility.main_cli(["getsu.exe", "--cable-visibility", "bogus"]), 0)
        self.assertEqual(cable_visibility.main_cli(["getsu.exe", "--cable-visibility"]), 0)

    def test_show_mode_uses_the_config_file(self):
        with patch.object(cable_visibility, "show_cable", return_value={}) as show, \
                patch("src.config.load_config", return_value={"cable_installed_by_getsu": True}):
            cable_visibility.main_cli(["getsu.exe", "--cable-visibility", "show"])
        show.assert_called_once_with({"cable_installed_by_getsu": True})


class MainWiringTests(unittest.TestCase):
    """main.py routes the flag before the GUI, shows the cable after the single-instance check, and is source-checked
    like the existing installer tests (importing main.py would start the single-instance check)."""

    def setUp(self):
        with open(os.path.join(ROOT, "src", "main.py"), encoding="utf-8") as f:
            self.src = f.read()

    def test_flag_is_in_the_single_instance_skip_list(self):
        skip = self.src[self.src.index("if not any(arg in sys.argv"):self.src.index("activate_existing_instance(restore")]
        self.assertIn('"--cable-visibility"', skip)

    def test_flag_exits_before_the_gui_is_imported_or_started(self):
        route = self.src.index('if "--cable-visibility" in sys.argv')
        self.assertLess(route, self.src.index("from src.gui import"))
        self.assertLess(route, self.src.index("launch_gui(minimized"))
        self.assertIn("sys.exit(cable_visibility_main(sys.argv))", self.src)

    def test_cable_is_shown_after_the_single_instance_check_and_before_devices_are_read(self):
        single = self.src.index("activate_existing_instance(restore")
        main_def = self.src.index("def main():")
        show_call = self.src.index("_show_cable_at_start()", main_def)
        self.assertGreater(show_call, single)
        self.assertLess(show_call, self.src.index("recovery_router.audit_crash_recovery()"))
        self.assertLess(show_call, self.src.index("launch_gui(minimized"))

    def test_show_at_start_skips_device_check_and_driver_install(self):
        main_def = self.src.index("def main():")
        self.assertIn("if not (args.check_devices or args.install_driver):", self.src[main_def:main_def + 400])

    def test_cli_shutdown_never_hides(self):
        """--cli skips the single-instance check: hiding on its exit could cut off a running window's cable."""
        shut = self.src.index("def shutdown(")
        self.assertNotIn("hide_cable", self.src[shut:self.src.index("sys.exit(0)", shut)])

    def test_install_driver_marks_the_cable(self):
        install = self.src.index("success = install_vbcable_driver()")
        self.assertIn("mark_cable_installed_by_getsu(load_config())", self.src[install:install + 300])

    def test_show_at_start_reinitialises_portaudio_when_a_cable_was_shown(self):
        helper = self.src.index("def _show_cable_at_start():")
        body = self.src[helper:self.src.index("def main():")]
        self.assertIn("sd._terminate()", body)
        self.assertIn("sd._initialize()", body)


class GuiTeardownOrderTests(unittest.TestCase):
    def test_hide_runs_after_the_microphone_is_restored(self):
        from src.gui import GetsuGUI

        order = []
        engine = SimpleNamespace(prepare_for_stop=lambda: order.append("restore"))
        fake = SimpleNamespace(
            is_running=False, _busy="", _resume_pending=False, _app_running=True, _cancel_voice_test=False,
            _cable_guard=SimpleNamespace(cancel=lambda: None), _device_change_timer=None, _vad_save_timer=None,
            engine=engine, router=None, config={"cable_installed_by_getsu": True},
            _persist_was_running=lambda running: None,
        )
        with patch("src.cable_visibility.hide_cable", side_effect=lambda cfg: order.append("hide") or {}):
            GetsuGUI._teardown_engine_and_timers(fake)
        self.assertEqual(order, ["restore", "hide"])

    def test_tray_exit_hides_after_the_engine_stops(self):
        from src.tray import TrayApp

        order = []
        engine = SimpleNamespace(prepare_for_stop=lambda: order.append("restore"))
        tray = SimpleNamespace(engine=engine, config={})
        icon = SimpleNamespace(stop=lambda: order.append("icon"))
        with patch("src.cable_visibility.hide_cable", side_effect=lambda cfg: order.append("hide") or {}):
            TrayApp._on_exit(tray, icon, None)
        self.assertEqual(order, ["restore", "hide", "icon"])


class InstallerTests(unittest.TestCase):
    def setUp(self):
        with open(os.path.join(ROOT, "setup.iss"), encoding="utf-8") as f:
            self.iss = f.read()

    def test_installer_writes_the_marker_and_hides_the_cable_after_the_install(self):
        fmt = self.iss.index("'--set-cable-format'")
        marker = self.iss.index("RegWriteDWordValue(HKLM64, 'SOFTWARE\\Getsu', 'CableInstalledByGetsu', 1)")
        hide = self.iss.index("'--cable-visibility hide'")
        self.assertLess(fmt, marker)
        self.assertLess(marker, hide)

    def test_uninstaller_shows_the_cable_after_taskkill_and_removes_the_marker(self):
        block = self.iss.index("procedure CurUninstallStepChanged")
        taskkill = self.iss.index("/F /T /IM getsu.exe", block)
        show = self.iss.index("'--cable-visibility show-always'", block)
        delete_value = self.iss.index("RegDeleteValue(HKLM64, 'SOFTWARE\\Getsu', 'CableInstalledByGetsu')", block)
        delete_key = self.iss.index("RegDeleteKeyIfEmpty(HKLM64, 'SOFTWARE\\Getsu')", block)
        remove_cable = self.iss.index("'-u -h'", block)
        self.assertLess(taskkill, show)
        self.assertLess(show, delete_value)
        self.assertLess(delete_value, delete_key)
        self.assertLess(delete_key, remove_cable)


class ConfigMigrationTests(unittest.TestCase):
    def test_default_is_false_and_old_configs_gain_the_key(self):
        import json
        import src.config as cfg

        self.assertIs(cfg.DEFAULT_CONFIG["cable_installed_by_getsu"], False)
        tmp = tempfile.mkdtemp()
        try:
            path = os.path.join(tmp, "config.json")
            with open(path, "w", encoding="utf-8") as f:
                json.dump({"version": cfg.APP_VERSION, "mic_level_raised": []}, f)
            with patch.object(cfg, "CONFIG_FILE", path):
                loaded = cfg.load_config()
            self.assertIs(loaded["cable_installed_by_getsu"], False)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class SingleSetterScanTests(unittest.TestCase):
    """Only win_audio.set_getsu_cable_visibility may call the visibility slot, and only cable_visibility.py calls it."""

    def test_visibility_slot_is_used_only_by_the_guarded_setter(self):
        import ast
        with open(os.path.join(ROOT, "src", "win_audio.py"), encoding="utf-8") as f:
            tree = ast.parse(f.read())
        users = set()
        for fn in (n for n in tree.body if isinstance(n, ast.FunctionDef)):
            for node in ast.walk(fn):
                if isinstance(node, ast.Name) and node.id == "_VT_SET_ENDPOINT_VISIBILITY":
                    users.add(fn.name)
        self.assertEqual(users, {"set_getsu_cable_visibility"})

    def test_only_cable_visibility_calls_the_setter(self):
        callers = set()
        src = os.path.join(ROOT, "src")
        for name in os.listdir(src):
            if not name.endswith(".py") or name == "win_audio.py":
                continue
            with open(os.path.join(src, name), encoding="utf-8") as f:
                if "set_getsu_cable_visibility" in f.read():
                    callers.add(name)
        self.assertEqual(callers, {"cable_visibility.py"})


if __name__ == "__main__":
    unittest.main()
