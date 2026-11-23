"""The guarded playback default setter (win_audio.set_default_playback_away_from_cable) and the source scan that
keeps it the only code in src that sets a playback default."""
import ast
import os
import unittest
from unittest.mock import patch

import sys as _sys
_sys.path.insert(0, os.path.dirname(__file__))
import _isolation  # noqa: E402,F401

from src import win_audio
from src.win_audio import set_default_playback_away_from_cable

SRC_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src"))

CABLE = {"id": "{0.0.0.00000000}.{11111111-aaaa-4bbb-8ccc-000000000001}",
         "name": "CABLE Input (VB-Audio Virtual Cable)", "bus": "SWD", "form_factor": None}
OTHER_VIRTUAL = {"id": "{0.0.0.00000000}.{11111111-aaaa-4bbb-8ccc-000000000002}",
                 "name": "VoiceMeeter Input", "bus": "SWD", "form_factor": None}
SPEAKERS = {"id": "{0.0.0.00000000}.{22222222-aaaa-4bbb-8ccc-000000000001}",
            "name": "Speakers (Realtek(R) Audio)", "bus": "HDAUDIO", "form_factor": 1}
HEADSET = {"id": "{0.0.0.00000000}.{22222222-aaaa-4bbb-8ccc-000000000002}",
           "name": "Headset (MPOW Wireless Gaming Headset)", "bus": "USB", "form_factor": 5}


class TestSetDefaultPlaybackAwayFromCable(unittest.TestCase):
    def _run(self, target_id, render, current_id, set_result=True):
        current_name = next((e["name"] for e in render if e["id"] == current_id), "Unknown")
        with patch.object(win_audio, "native_list_active_endpoints", return_value=render), \
             patch.object(win_audio, "native_get_current_default_playback", return_value=(current_id, current_name)), \
             patch.object(win_audio, "native_set_default_endpoint", return_value=set_result) as set_mock:
            result = set_default_playback_away_from_cable(target_id)
        return result, set_mock

    def test_moves_default_playback_off_the_cable(self):
        result, set_mock = self._run(SPEAKERS["id"], [CABLE, SPEAKERS], CABLE["id"])
        self.assertTrue(result)
        set_mock.assert_called_once_with(SPEAKERS["id"])

    def test_refuses_a_capture_endpoint_id(self):
        capture_id = "{0.0.1.00000000}.{33333333-aaaa-4bbb-8ccc-000000000001}"
        result, set_mock = self._run(capture_id, [CABLE, SPEAKERS], CABLE["id"])
        self.assertFalse(result)
        set_mock.assert_not_called()

    def test_refuses_a_value_that_is_not_an_endpoint_id(self):
        for bad in ("Speakers (Realtek(R) Audio)", "", None, "{0.0.0.00000000}", "not-an-id"):
            result, set_mock = self._run(bad, [CABLE, SPEAKERS], CABLE["id"])
            self.assertFalse(result, bad)
            set_mock.assert_not_called()

    def test_refuses_a_virtual_target(self):
        result, set_mock = self._run(OTHER_VIRTUAL["id"], [CABLE, OTHER_VIRTUAL], CABLE["id"])
        self.assertFalse(result)
        set_mock.assert_not_called()

    def test_refuses_an_inactive_target(self):
        # SPEAKERS is not in the list of active render endpoints (unplugged or disabled).
        result, set_mock = self._run(SPEAKERS["id"], [CABLE, HEADSET], CABLE["id"])
        self.assertFalse(result)
        set_mock.assert_not_called()

    def test_refuses_when_current_default_playback_is_not_the_cable(self):
        result, set_mock = self._run(HEADSET["id"], [CABLE, SPEAKERS, HEADSET], SPEAKERS["id"])
        self.assertFalse(result)
        set_mock.assert_not_called()

    def test_refuses_when_there_is_no_default_playback(self):
        with patch.object(win_audio, "native_list_active_endpoints", return_value=[CABLE, SPEAKERS]), \
             patch.object(win_audio, "native_get_current_default_playback", return_value=(None, "Unknown")), \
             patch.object(win_audio, "native_set_default_endpoint", return_value=True) as set_mock:
            self.assertFalse(set_default_playback_away_from_cable(SPEAKERS["id"]))
            set_mock.assert_not_called()

    def test_returns_false_when_the_com_call_fails(self):
        result, set_mock = self._run(SPEAKERS["id"], [CABLE, SPEAKERS], CABLE["id"], set_result=False)
        self.assertFalse(result)
        set_mock.assert_called_once_with(SPEAKERS["id"])


def _enclosing_functions(tree):
    """Yields (function_name, node) for every node, with the name of the innermost enclosing def (or '<module>')."""
    stack = []

    def visit(node):
        is_def = isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        if is_def:
            stack.append(node.name)
        yield (stack[-1] if stack else "<module>"), node
        for child in ast.iter_child_nodes(node):
            yield from visit(child)
        if is_def:
            stack.pop()

    yield from visit(tree)


class TestOnlyThePlaybackSetterSetsPlaybackDefaults(unittest.TestCase):
    def _scan(self):
        for name in sorted(os.listdir(SRC_DIR)):
            if not name.endswith(".py"):
                continue
            path = os.path.join(SRC_DIR, name)
            with open(path, "r", encoding="utf-8") as f:
                tree = ast.parse(f.read(), filename=path)
            yield name, tree

    def test_native_set_default_endpoint_is_called_only_from_the_allowed_functions(self):
        # The capture path (router) sets capture endpoints only. The playback setter is the only render caller.
        allowed = {("router.py", "_set_capture_endpoint"),
                   ("win_audio.py", "set_default_playback_away_from_cable")}
        found = set()
        for file_name, tree in self._scan():
            for func, node in _enclosing_functions(tree):
                if isinstance(node, ast.Call):
                    callee = node.func
                    callee_name = callee.id if isinstance(callee, ast.Name) else getattr(callee, "attr", None)
                    if callee_name == "native_set_default_endpoint":
                        found.add((file_name, func))
        self.assertEqual(found, allowed)

    def test_policy_config_vtable_is_used_only_in_win_audio(self):
        for file_name, tree in self._scan():
            if file_name == "win_audio.py":
                continue
            for _, node in _enclosing_functions(tree):
                if isinstance(node, ast.Constant) and isinstance(node.value, str):
                    self.assertNotIn("SetDefaultEndpoint", node.value, file_name)
                if isinstance(node, ast.Subscript) and isinstance(node.slice, ast.Constant) and node.slice.value == 13:
                    self.fail(f"{file_name} indexes a vtable at slot 13 (IPolicyConfig::SetDefaultEndpoint)")

    def test_audio_restore_playback_flags_are_used_only_by_the_installer(self):
        # --restore and --ensure-physical (without the -capture suffix) change playback defaults in AudioRestore.exe.
        playback_flags = {"--restore", "--ensure-physical"}
        for file_name, tree in self._scan():
            for func, node in _enclosing_functions(tree):
                if isinstance(node, ast.Constant) and node.value in playback_flags:
                    self.assertEqual((file_name, func), ("devices.py", "install_vbcable_driver"),
                                     f"{node.value} used in {file_name}:{func}")


if __name__ == "__main__":
    unittest.main()
