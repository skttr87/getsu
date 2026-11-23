"""Cable guard: decision table, debounce and retry policy, and the CoreAudio glue (mocked)."""
import os
import sys as _sys
import threading
import time
import unittest
from unittest.mock import MagicMock, patch

_sys.path.insert(0, os.path.dirname(__file__))
import _isolation  # noqa: E402,F401

from src import cable_guard as cg
from src.cable_guard import (
    ACTION_NOTE, ACTION_SET_MIC, ACTION_SET_PLAYBACK, ACTION_WARN, PLAYBACK_NO_TARGET_STATUS,
    CableGuard, Decision, Endpoint, GuardState, apply_decision, decide, gather_state,
)
from src.devices import DeviceKind

CABLE_OUT = Endpoint("{0.0.1.00000000}.{aaaaaaaa-0000-4000-8000-000000000001}",
                     "CABLE Output (VB-Audio Virtual Cable)", True, DeviceKind.VIRTUAL)
CABLE_IN = Endpoint("{0.0.0.00000000}.{aaaaaaaa-0000-4000-8000-000000000002}",
                    "CABLE Input (VB-Audio Virtual Cable)", True, DeviceKind.VIRTUAL)
REALTEK_MIC = Endpoint("{0.0.1.00000000}.{bbbbbbbb-0000-4000-8000-000000000001}",
                       "Microphone (Realtek(R) Audio)", False, DeviceKind.BUILT_IN)
REALTEK_MIC_2 = Endpoint("{0.0.1.00000000}.{bbbbbbbb-0000-4000-8000-000000000002}",
                         "Microphone (Second Built-in)", False, DeviceKind.BUILT_IN)
HEADSET_MIC = Endpoint("{0.0.1.00000000}.{cccccccc-0000-4000-8000-000000000001}",
                       "Headset Microphone (Wireless Headset)", False, DeviceKind.USB_HEADSET)
SPEAKERS = Endpoint("{0.0.0.00000000}.{dddddddd-0000-4000-8000-000000000001}",
                    "Speakers (Realtek(R) Audio)", False, DeviceKind.BUILT_IN)
JACK_HEADPHONES = Endpoint("{0.0.0.00000000}.{dddddddd-0000-4000-8000-000000000002}",
                           "Headphones (Realtek(R) Audio)", False, DeviceKind.WIRED_HEADSET)
WIRELESS_HEADSET = Endpoint("{0.0.0.00000000}.{dddddddd-0000-4000-8000-000000000003}",
                            "Headset Playback (Wireless Headset)", False, DeviceKind.USB_HEADSET)


def make_state(**overrides) -> GuardState:
    base = dict(
        default_mic_id=CABLE_OUT.id,
        default_mic_is_cable=True,
        default_playback_id=SPEAKERS.id,
        default_playback_is_cable=False,
        nc_running=False,
        voice_test_running=False,
        swap_in_progress=False,
        selected_mic_id=None,
        original_mic_id=None,
        last_physical_playback_id=None,
        active_capture=(CABLE_OUT, REALTEK_MIC),
        active_render=(CABLE_IN, SPEAKERS),
    )
    base.update(overrides)
    return GuardState(**base)


def actions(decisions):
    return [d.action for d in decisions]


class TestMicrophoneRule(unittest.TestCase):
    def test_default_mic_not_cable_means_no_decisions(self):
        state = make_state(default_mic_id=REALTEK_MIC.id, default_mic_is_cable=False)
        self.assertEqual(decide(state), [])

    def test_original_mic_is_the_first_choice(self):
        state = make_state(original_mic_id=HEADSET_MIC.id, selected_mic_id=REALTEK_MIC.id,
                           active_capture=(CABLE_OUT, REALTEK_MIC, HEADSET_MIC))
        decisions = decide(state)
        self.assertEqual(actions(decisions), [ACTION_SET_MIC])
        self.assertEqual(decisions[0].target, HEADSET_MIC)
        self.assertIn("original_mic_id", decisions[0].log)

    def test_selected_mic_is_second_when_original_is_not_active(self):
        state = make_state(original_mic_id=HEADSET_MIC.id, selected_mic_id=REALTEK_MIC.id,
                           active_capture=(CABLE_OUT, REALTEK_MIC))
        decisions = decide(state)
        self.assertEqual(decisions[0].target, REALTEK_MIC)
        self.assertIn("selected in Getsu", decisions[0].log)

    def test_single_built_in_mic_is_the_last_choice(self):
        state = make_state(original_mic_id=HEADSET_MIC.id, selected_mic_id=HEADSET_MIC.id,
                           active_capture=(CABLE_OUT, REALTEK_MIC))
        decisions = decide(state)
        self.assertEqual(decisions[0].target, REALTEK_MIC)
        self.assertIn("built-in", decisions[0].log)

    def test_several_built_in_mics_give_no_target(self):
        state = make_state(active_capture=(CABLE_OUT, REALTEK_MIC, REALTEK_MIC_2))
        decisions = decide(state)
        self.assertEqual(actions(decisions), [ACTION_NOTE])
        self.assertIsNone(decisions[0].target)
        self.assertIn("no target", decisions[0].log)

    def test_no_active_target_is_warning_only_and_no_set(self):
        state = make_state(original_mic_id=HEADSET_MIC.id, active_capture=(CABLE_OUT,))
        decisions = decide(state)
        self.assertNotIn(ACTION_SET_MIC, actions(decisions))
        self.assertTrue(any("no target" in d.log for d in decisions))

    def test_virtual_candidates_are_never_targets(self):
        # The cable itself as original and selected mic is virtual, so the built-in mic is picked instead.
        state = make_state(original_mic_id=CABLE_OUT.id, selected_mic_id=CABLE_OUT.id,
                           active_capture=(CABLE_OUT, REALTEK_MIC))
        decisions = decide(state)
        self.assertEqual(decisions[0].target, REALTEK_MIC)
        self.assertFalse(decisions[0].target.is_virtual)

    def test_running_noise_cancellation_leaves_the_cable_mic_alone(self):
        state = make_state(nc_running=True, original_mic_id=REALTEK_MIC.id, active_capture=(CABLE_OUT, REALTEK_MIC))
        decisions = decide(state)
        self.assertEqual(actions(decisions), [ACTION_NOTE])
        self.assertIn("left alone", decisions[0].log)

    def test_voice_test_leaves_the_cable_mic_alone(self):
        state = make_state(voice_test_running=True, original_mic_id=REALTEK_MIC.id, active_capture=(CABLE_OUT, REALTEK_MIC))
        self.assertNotIn(ACTION_SET_MIC, actions(decide(state)))

    def test_swap_in_progress_leaves_the_cable_mic_alone(self):
        state = make_state(swap_in_progress=True, original_mic_id=REALTEK_MIC.id, active_capture=(CABLE_OUT, REALTEK_MIC))
        self.assertNotIn(ACTION_SET_MIC, actions(decide(state)))


class TestPlaybackRule(unittest.TestCase):
    def test_default_playback_not_cable_means_no_playback_decision(self):
        state = make_state(default_mic_is_cable=False, default_playback_id=SPEAKERS.id,
                           default_playback_is_cable=False, last_physical_playback_id=SPEAKERS.id)
        self.assertEqual(decide(state), [])

    def test_last_physical_playback_is_used_when_active(self):
        state = make_state(default_mic_is_cable=False, default_playback_id=CABLE_IN.id, default_playback_is_cable=True,
                           last_physical_playback_id=JACK_HEADPHONES.id,
                           active_render=(CABLE_IN, SPEAKERS, JACK_HEADPHONES))
        decisions = decide(state)
        self.assertEqual(actions(decisions), [ACTION_SET_PLAYBACK])
        self.assertEqual(decisions[0].target, JACK_HEADPHONES)
        self.assertTrue(decisions[0].status.startswith("Sound output set back to "))

    def test_single_built_in_output_is_the_fallback(self):
        state = make_state(default_mic_is_cable=False, default_playback_id=CABLE_IN.id, default_playback_is_cable=True,
                           last_physical_playback_id=WIRELESS_HEADSET.id,
                           active_render=(CABLE_IN, SPEAKERS))
        decisions = decide(state)
        self.assertEqual(decisions[0].action, ACTION_SET_PLAYBACK)
        self.assertEqual(decisions[0].target, SPEAKERS)

    def test_two_built_in_outputs_give_warning_and_status_without_a_set(self):
        state = make_state(default_mic_is_cable=False, default_playback_id=CABLE_IN.id, default_playback_is_cable=True,
                           last_physical_playback_id=WIRELESS_HEADSET.id,
                           active_render=(CABLE_IN, SPEAKERS, JACK_HEADPHONES))
        decisions = decide(state)
        self.assertEqual(actions(decisions), [ACTION_WARN])
        self.assertEqual(decisions[0].status, PLAYBACK_NO_TARGET_STATUS)
        self.assertTrue(decisions[0].status_warn)
        self.assertIsNone(decisions[0].target)

    def test_inactive_last_device_falls_back_to_built_in(self):
        state = make_state(default_mic_is_cable=False, default_playback_id=CABLE_IN.id, default_playback_is_cable=True,
                           last_physical_playback_id=JACK_HEADPHONES.id, active_render=(CABLE_IN, SPEAKERS))
        self.assertEqual(decide(state)[0].target, SPEAKERS)

    def test_virtual_last_physical_playback_is_never_a_target(self):
        state = make_state(default_mic_is_cable=False, default_playback_id=CABLE_IN.id, default_playback_is_cable=True,
                           last_physical_playback_id=CABLE_IN.id, active_render=(CABLE_IN, SPEAKERS))
        decisions = decide(state)
        self.assertEqual(decisions[0].target, SPEAKERS)
        self.assertFalse(decisions[0].target.is_virtual)

    def test_playback_is_moved_while_noise_cancellation_runs(self):
        state = make_state(nc_running=True, default_mic_is_cable=False, default_playback_id=CABLE_IN.id,
                           default_playback_is_cable=True, last_physical_playback_id=SPEAKERS.id)
        self.assertEqual(actions(decide(state)), [ACTION_SET_PLAYBACK])

    def test_mic_and_playback_can_both_be_decided_in_one_run(self):
        state = make_state(original_mic_id=REALTEK_MIC.id, default_playback_id=CABLE_IN.id,
                           default_playback_is_cable=True, last_physical_playback_id=SPEAKERS.id)
        self.assertEqual(actions(decide(state)), [ACTION_SET_MIC, ACTION_SET_PLAYBACK])

    def test_every_playback_target_is_physical_and_active(self):
        for last in (None, CABLE_IN.id, JACK_HEADPHONES.id, WIRELESS_HEADSET.id):
            state = make_state(default_mic_is_cable=False, default_playback_id=CABLE_IN.id,
                               default_playback_is_cable=True, last_physical_playback_id=last,
                               active_render=(CABLE_IN, SPEAKERS, JACK_HEADPHONES))
            for d in decide(state):
                if d.target is not None:
                    self.assertFalse(d.target.is_virtual)
                    self.assertIn(d.target, state.active_render)


class TestCableGuardRunner(unittest.TestCase):
    def _guard(self, state=None, apply_result=True, gather_error=None):
        gather_calls = []

        def gather():
            gather_calls.append(1)
            if gather_error:
                raise gather_error
            return state or make_state(original_mic_id=REALTEK_MIC.id, active_capture=(CABLE_OUT, REALTEK_MIC))

        apply_calls = []

        def apply(decision):
            apply_calls.append(decision)
            return apply_result if not callable(apply_result) else apply_result(decision)

        notes = []
        log = MagicMock()
        guard = CableGuard(gather, apply, lambda text, warn: notes.append((text, warn)), logger=log)
        return guard, gather_calls, apply_calls, notes, log

    def test_a_burst_of_requests_gives_one_run(self):
        guard, gather_calls, _, _, _ = self._guard()
        for _ in range(6):
            guard.request("device_change", delay=0.1)
            time.sleep(0.01)
        time.sleep(0.5)
        self.assertEqual(len(gather_calls), 1)

    def test_zero_delay_request_runs_immediately_in_a_thread(self):
        guard, gather_calls, _, _, _ = self._guard()
        guard.request("startup", delay=0.0)
        time.sleep(0.3)
        self.assertEqual(len(gather_calls), 1)

    def test_cancel_stops_pending_runs(self):
        guard, gather_calls, _, _, _ = self._guard()
        guard.request("device_change", delay=0.1)
        guard.cancel()
        guard.request("device_change", delay=0.0)
        time.sleep(0.3)
        self.assertEqual(gather_calls, [])

    def test_success_applies_once_and_shows_the_status_line(self):
        guard, _, apply_calls, notes, log = self._guard(apply_result=True)
        guard.run_now("device_change")
        self.assertEqual(len(apply_calls), 1)
        self.assertEqual(apply_calls[0].action, ACTION_SET_MIC)
        self.assertEqual(notes, [("Microphone set back to Microphone (Realtek(R) Audio)", False)])
        # Every decision and every result is logged, one line each.
        self.assertGreaterEqual(log.info.call_count, 2)

    def test_failed_set_is_retried_only_on_the_next_device_change(self):
        guard, _, apply_calls, _, log = self._guard(apply_result=False)
        guard.run_now("startup")
        self.assertEqual(len(apply_calls), 1)
        guard.run_now("stop")
        self.assertEqual(len(apply_calls), 1)            # not retried on a stop run
        guard.run_now("device_change")
        self.assertEqual(len(apply_calls), 2)            # the one retry, on a device change
        guard.run_now("device_change")
        self.assertEqual(len(apply_calls), 2)            # after the second failure: no more attempts
        guard.run_now("device_change")
        self.assertEqual(len(apply_calls), 2)
        warnings = [c.args[0] for c in log.warning.call_args_list]
        self.assertEqual(len(warnings), 2)               # logged once per failure stage, not on every event
        self.assertIn("retry once on the next device change", warnings[0])
        self.assertIn("not retrying", warnings[1])

    def test_failure_budget_resets_when_the_condition_clears(self):
        states = [
            make_state(original_mic_id=REALTEK_MIC.id, active_capture=(CABLE_OUT, REALTEK_MIC)),
            make_state(default_mic_is_cable=False, default_playback_is_cable=False),
            make_state(original_mic_id=REALTEK_MIC.id, active_capture=(CABLE_OUT, REALTEK_MIC)),
        ]
        it = iter(states)
        apply_calls = []
        guard = CableGuard(lambda: next(it), lambda d: apply_calls.append(d) or False, lambda t, w: None, logger=MagicMock())
        guard.run_now("startup")       # fails, budget 1
        guard.run_now("startup")       # condition clears: budget reset
        guard.run_now("startup")       # fresh attempt on a new problem
        self.assertEqual(len(apply_calls), 2)

    def test_exceptions_are_logged_and_never_raised(self):
        guard, _, apply_calls, _, log = self._guard(gather_error=RuntimeError("com down"))
        guard.run_now("device_change")
        self.assertEqual(apply_calls, [])
        self.assertTrue(log.warning.called)

    def test_playback_status_is_shown_without_an_apply(self):
        state = make_state(default_mic_is_cable=False, default_playback_id=CABLE_IN.id, default_playback_is_cable=True,
                           last_physical_playback_id=None, active_render=(CABLE_IN, SPEAKERS, JACK_HEADPHONES))
        guard, _, apply_calls, notes, _ = self._guard(state=state)
        guard.run_now("device_change")
        self.assertEqual(apply_calls, [])
        self.assertEqual(notes, [(PLAYBACK_NO_TARGET_STATUS, True)])


class TestGatherState(unittest.TestCase):
    def _patches(self, mic, play, capture, render):
        return (
            patch.object(cg, "native_get_current_default_mic", return_value=mic),
            patch.object(cg, "native_get_current_default_playback", return_value=play),
            patch.object(cg, "native_list_active_endpoints",
                         side_effect=lambda is_input: capture if is_input else render),
            patch.object(cg, "save_config"),
        )

    @staticmethod
    def _entry(ep):
        bus = {DeviceKind.VIRTUAL: "SWD", DeviceKind.BUILT_IN: "HDAUDIO", DeviceKind.USB_HEADSET: "USB",
               DeviceKind.WIRED_HEADSET: "HDAUDIO"}.get(ep.kind, "UNKNOWN")
        return {"id": ep.id, "name": ep.name, "bus": bus, "form_factor": None}

    def test_physical_default_playback_is_remembered_and_saved(self):
        config = {"original_mic_id": None, "last_physical_playback_id": None}
        p = self._patches((CABLE_OUT.id, CABLE_OUT.name), (SPEAKERS.id, SPEAKERS.name),
                          [self._entry(CABLE_OUT), self._entry(REALTEK_MIC)],
                          [self._entry(CABLE_IN), self._entry(SPEAKERS)])
        with p[0], p[1], p[2], p[3] as save:
            state = gather_state(config, None, False, False, False)
        self.assertEqual(config["last_physical_playback_id"], SPEAKERS.id)
        save.assert_called_once_with(config)
        self.assertTrue(state.default_mic_is_cable)
        self.assertFalse(state.default_playback_is_cable)

    def test_cable_default_playback_is_not_remembered(self):
        config = {"original_mic_id": None, "last_physical_playback_id": SPEAKERS.id}
        p = self._patches((REALTEK_MIC.id, REALTEK_MIC.name), (CABLE_IN.id, CABLE_IN.name),
                          [self._entry(REALTEK_MIC)],
                          [self._entry(CABLE_IN), self._entry(SPEAKERS)])
        with p[0], p[1], p[2], p[3] as save:
            state = gather_state(config, None, False, False, False)
        self.assertEqual(config["last_physical_playback_id"], SPEAKERS.id)
        save.assert_not_called()
        self.assertTrue(state.default_playback_is_cable)
        self.assertEqual(state.last_physical_playback_id, SPEAKERS.id)

    def test_selected_mic_is_resolved_by_unique_name(self):
        config = {"original_mic_id": REALTEK_MIC.id, "last_physical_playback_id": None}
        p = self._patches((CABLE_OUT.id, CABLE_OUT.name), (SPEAKERS.id, SPEAKERS.name),
                          [self._entry(CABLE_OUT), self._entry(REALTEK_MIC), self._entry(HEADSET_MIC)],
                          [self._entry(SPEAKERS)])
        with p[0], p[1], p[2], p[3]:
            state = gather_state(config, HEADSET_MIC.name, True, False, False)
        self.assertEqual(state.selected_mic_id, HEADSET_MIC.id)
        self.assertEqual(state.original_mic_id, REALTEK_MIC.id)
        self.assertTrue(state.nc_running)

    def test_ambiguous_selected_name_is_unknown(self):
        config = {}
        p = self._patches((CABLE_OUT.id, CABLE_OUT.name), (SPEAKERS.id, SPEAKERS.name),
                          [self._entry(REALTEK_MIC), self._entry(REALTEK_MIC_2)], [self._entry(SPEAKERS)])
        twin = Endpoint(REALTEK_MIC_2.id, REALTEK_MIC.name, False, DeviceKind.BUILT_IN)
        with p[0], p[1], p[2], p[3]:
            with patch.object(cg, "native_list_active_endpoints",
                              side_effect=lambda is_input: [self._entry(REALTEK_MIC), self._entry(twin)] if is_input
                              else [self._entry(SPEAKERS)]):
                state = gather_state(config, REALTEK_MIC.name, False, False, False)
        self.assertIsNone(state.selected_mic_id)


class TestApplyDecision(unittest.TestCase):
    def test_microphone_goes_through_the_router_capture_path(self):
        router = MagicMock()
        router.set_default_capture.return_value = True
        d = Decision(ACTION_SET_MIC, REALTEK_MIC, "x")
        self.assertTrue(apply_decision(router, d))
        router.set_default_capture.assert_called_once_with(REALTEK_MIC.id)

    def test_playback_goes_only_through_the_guarded_setter(self):
        router = MagicMock()
        with patch.object(cg, "set_default_playback_away_from_cable", return_value=True) as setter:
            d = Decision(ACTION_SET_PLAYBACK, SPEAKERS, "x")
            self.assertTrue(apply_decision(router, d))
        setter.assert_called_once_with(SPEAKERS.id)
        router.set_default_capture.assert_not_called()

    def test_notes_and_warnings_apply_nothing(self):
        router = MagicMock()
        self.assertFalse(apply_decision(router, Decision(ACTION_NOTE, None, "x")))
        self.assertFalse(apply_decision(router, Decision(ACTION_WARN, None, "x")))
        router.set_default_capture.assert_not_called()


class TestGuiWiring(unittest.TestCase):
    def test_device_change_event_requests_a_delayed_guard_run(self):
        from src.gui_devices import DevicesMixin
        fake = MagicMock()
        DevicesMixin._on_device_change_event(fake)
        fake._cable_guard.request.assert_called_once_with("device_change", delay=2.0)

    def test_gather_passes_the_full_name_of_the_selected_microphone(self):
        from src.gui_devices import DevicesMixin
        fake = MagicMock()
        fake.selected_input_name = "Microphone (Realtek(R) Audio) #2"
        fake.device_raw = {"Microphone (Realtek(R) Audio) #2": "Microphone (Realtek(R) Audio)"}
        fake.is_running = False
        fake._is_testing_voice = False
        fake._busy = None
        fake._is_rescanning = False
        fake._resume_pending = True
        with patch("src.gui_devices.gather_state", return_value="state") as gather:
            self.assertEqual(DevicesMixin._cable_guard_gather(fake), "state")
        args, kwargs = gather.call_args
        self.assertEqual(args[1], "Microphone (Realtek(R) Audio)")
        self.assertFalse(kwargs["nc_running"])
        self.assertTrue(kwargs["swap_in_progress"])  # a pending resume counts as a swap in progress

    def test_notify_is_silent_before_the_window_exists(self):
        from src.gui_devices import DevicesMixin
        fake = MagicMock()
        fake._ui_built = False
        fake._app_running = True
        DevicesMixin._cable_guard_notify(fake, "Microphone set back to X", False)
        fake.set_status_pill.assert_not_called()

    def test_notify_shows_the_status_line(self):
        from src.gui_devices import DevicesMixin
        fake = MagicMock()
        fake._ui_built = True
        fake._app_running = True
        DevicesMixin._cable_guard_notify(fake, PLAYBACK_NO_TARGET_STATUS, True)
        fake.set_status_pill.assert_called_once()
        self.assertEqual(fake.set_status_pill.call_args.args[0], PLAYBACK_NO_TARGET_STATUS)

    def test_default_poll_runs_the_guard_only_when_a_default_changed(self):
        """A default changed in Windows Sound settings sends no device event; the watchdog poll catches it."""
        from types import SimpleNamespace
        from src.gui_devices import DevicesMixin
        fake = SimpleNamespace(_cable_guard=MagicMock())
        mic = ["{0.0.1.00000000}.{a}"]
        play = ["{0.0.0.00000000}.{speakers}"]
        with patch("src.win_audio.native_get_current_default_mic", side_effect=lambda: (mic[0], "m")), \
                patch("src.win_audio.native_get_current_default_playback", side_effect=lambda: (play[0], "p")):
            DevicesMixin._poll_default_change(fake)  # first poll only records
            DevicesMixin._poll_default_change(fake)  # nothing changed
            fake._cable_guard.request.assert_not_called()
            play[0] = "{0.0.0.00000000}.{cable}"
            DevicesMixin._poll_default_change(fake)
            fake._cable_guard.request.assert_called_once_with("default change", delay=0.5)
            DevicesMixin._poll_default_change(fake)  # same again: no second run
            self.assertEqual(fake._cable_guard.request.call_count, 1)

    def test_default_poll_ignores_read_errors(self):
        from types import SimpleNamespace
        from src.gui_devices import DevicesMixin
        fake = SimpleNamespace(_cable_guard=MagicMock())
        with patch("src.win_audio.native_get_current_default_mic", side_effect=OSError("COM")):
            DevicesMixin._poll_default_change(fake)
        fake._cable_guard.request.assert_not_called()

    def test_teardown_cancels_the_guard(self):
        from src.gui import GetsuGUI
        fake = MagicMock()
        fake._device_change_timer = None
        fake._vad_save_timer = None
        GetsuGUI._teardown_engine_and_timers(fake)
        fake._cable_guard.cancel.assert_called_once()
        self.assertFalse(fake._app_running)


if __name__ == "__main__":
    unittest.main()
