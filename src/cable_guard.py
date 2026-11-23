"""
Cable guard: when Windows itself makes the virtual cable the default microphone or the default playback device
(for example after the user's headset is unplugged and Windows picks a replacement), switch it back.

decide() is a pure function over a GuardState and returns the Decisions to carry out. CableGuard runs the
checks off the audio thread, debounces device-change bursts and limits retries. gather_state() and
apply_decision() are the thin glue to the CoreAudio readers and setters.

Rules (rules.md section 3, option b):
- The microphone is only set back while noise cancellation is off, no voice test and no swap is running.
  While noise cancellation runs, the cable as default microphone is intended and is never touched.
- Playback is only ever moved away from the cable, to the last physical playback device or to the single
  built-in speakers / headphones. Playback is never set to the cable and never between two physical devices.
"""
import logging
import threading
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from src.config import save_config
from src.devices import DeviceKind, kind_for_endpoint, is_virtual_endpoint
from src.gui_common import clean_device_label
from src.win_audio import (
    native_get_current_default_mic,
    native_get_current_default_playback,
    native_list_active_endpoints,
    set_default_playback_away_from_cable,
)

ACTION_SET_MIC = "set_mic"
ACTION_SET_PLAYBACK = "set_playback"
ACTION_NOTE = "note"      # log only
ACTION_WARN = "warn"      # log, and a status line when the decision carries one

PLAYBACK_NO_TARGET_STATUS = "Sound is going into the virtual cable. Choose your speakers in Windows Sound settings."

_PLAYBACK_BUILTIN_KINDS = (DeviceKind.BUILT_IN, DeviceKind.WIRED_HEADSET)


@dataclass(frozen=True)
class Endpoint:
    id: str
    name: str
    is_virtual: bool
    kind: str = DeviceKind.OTHER


@dataclass(frozen=True)
class GuardState:
    default_mic_id: Optional[str]
    default_mic_is_cable: bool
    default_playback_id: Optional[str]
    default_playback_is_cable: bool
    nc_running: bool
    voice_test_running: bool
    swap_in_progress: bool
    selected_mic_id: Optional[str]          # endpoint ID of the microphone selected in Getsu, when known
    original_mic_id: Optional[str]          # config original_mic_id
    last_physical_playback_id: Optional[str]
    active_capture: Tuple[Endpoint, ...] = ()
    active_render: Tuple[Endpoint, ...] = ()


@dataclass(frozen=True)
class Decision:
    action: str
    target: Optional[Endpoint]
    log: str
    status: Optional[str] = None
    status_warn: bool = False


def _active(endpoints: Sequence[Endpoint], endpoint_id: Optional[str]) -> Optional[Endpoint]:
    """The active endpoint with this ID, or None. Virtual devices are returned too; callers decide what they accept."""
    if not endpoint_id:
        return None
    return next((e for e in endpoints if e.id == endpoint_id), None)


def _single_builtin(endpoints: Sequence[Endpoint], kinds: Sequence[str]) -> Optional[Endpoint]:
    """The only active physical endpoint of the given kinds, or None when there are none or several."""
    found = [e for e in endpoints if not e.is_virtual and e.kind in kinds]
    return found[0] if len(found) == 1 else None


def _mic_target(state: GuardState) -> Tuple[Optional[Endpoint], str]:
    """First active, non-virtual microphone of: original_mic_id, the microphone selected in Getsu, the single built-in mic."""
    candidates = (
        ("original_mic_id", _active(state.active_capture, state.original_mic_id)),
        ("selected in Getsu", _active(state.active_capture, state.selected_mic_id)),
        ("single built-in microphone", _single_builtin(state.active_capture, (DeviceKind.BUILT_IN,))),
    )
    for source, endpoint in candidates:
        if endpoint is not None and not endpoint.is_virtual:
            return endpoint, source
    return None, ""


def _playback_target(state: GuardState) -> Tuple[Optional[Endpoint], str]:
    """Last physical playback device when active, else the single built-in speakers / headphones."""
    last = _active(state.active_render, state.last_physical_playback_id)
    if last is not None and not last.is_virtual:
        return last, "last physical playback device"
    builtin = _single_builtin(state.active_render, _PLAYBACK_BUILTIN_KINDS)
    if builtin is not None:
        return builtin, "single built-in speakers or headphones"
    return None, ""


def decide(state: GuardState) -> List[Decision]:
    """Decisions for one guard run. Nothing is decided (and nothing is logged) when the default device is not the cable."""
    decisions: List[Decision] = []

    if state.default_mic_is_cable:
        if state.nc_running:
            decisions.append(Decision(ACTION_NOTE, None,
                                      "microphone: default is the cable, left alone (noise cancellation running)"))
        elif state.voice_test_running or state.swap_in_progress:
            decisions.append(Decision(ACTION_NOTE, None,
                                      "microphone: default is the cable, left alone (voice test or swap in progress)"))
        else:
            target, source = _mic_target(state)
            if target is not None:
                decisions.append(Decision(
                    ACTION_SET_MIC, target,
                    f"microphone: default is the cable, switching to '{target.name}' ({source})",
                    status=f"Microphone set back to {clean_device_label(target.name)}"))
            else:
                decisions.append(Decision(ACTION_NOTE, None,
                                          "microphone: default is the cable, no target (no active physical microphone)"))

    if state.default_playback_is_cable:
        target, source = _playback_target(state)
        if target is not None:
            decisions.append(Decision(
                ACTION_SET_PLAYBACK, target,
                f"playback: default is the cable, switching to '{target.name}' ({source})",
                status=f"Sound output set back to {clean_device_label(target.name)}"))
        else:
            decisions.append(Decision(
                ACTION_WARN, None,
                "playback: default is the cable, no target (no last physical device, no single built-in output)",
                status=PLAYBACK_NO_TARGET_STATUS, status_warn=True))

    return decisions


# ----------------------------------------------------------------------------- glue to CoreAudio

def _endpoints(entries: List[Dict], is_input: bool) -> List[Endpoint]:
    result = []
    for entry in entries:
        name = entry.get("name") or ""
        bus = entry.get("bus") or "UNKNOWN"
        result.append(Endpoint(
            id=entry["id"],
            name=name,
            is_virtual=is_virtual_endpoint(name, bus),
            kind=kind_for_endpoint(name, bus, entry.get("form_factor")),
        ))
    return result


def gather_state(config: Dict, selected_mic_name: Optional[str], nc_running: bool,
                 voice_test_running: bool, swap_in_progress: bool) -> GuardState:
    """
    Reads the Windows defaults and the active endpoints (read-only). Also records the default playback device in
    config["last_physical_playback_id"] when it is a physical device, which is the only change made here.
    selected_mic_name is the full Windows name of the microphone selected in Getsu (None when unknown).
    """
    mic_id, _ = native_get_current_default_mic()
    play_id, _ = native_get_current_default_playback()
    capture = _endpoints(native_list_active_endpoints(is_input=True), is_input=True)
    render = _endpoints(native_list_active_endpoints(is_input=False), is_input=False)

    mic_ep = _active(capture, mic_id)
    play_ep = _active(render, play_id)

    if play_ep is not None and not play_ep.is_virtual and config.get("last_physical_playback_id") != play_ep.id:
        config["last_physical_playback_id"] = play_ep.id
        save_config(config)
        logging.getLogger("GetsuRouter").info(f"[CABLE GUARD] remembered physical playback device '{play_ep.name}'")

    selected_id = None
    if selected_mic_name:
        matches = [e for e in capture if e.name == selected_mic_name]
        if len(matches) == 1:
            selected_id = matches[0].id

    return GuardState(
        default_mic_id=mic_id,
        default_mic_is_cable=bool(mic_ep is not None and mic_ep.is_virtual),
        default_playback_id=play_id,
        default_playback_is_cable=bool(play_ep is not None and play_ep.is_virtual),
        nc_running=bool(nc_running),
        voice_test_running=bool(voice_test_running),
        swap_in_progress=bool(swap_in_progress),
        selected_mic_id=selected_id,
        original_mic_id=config.get("original_mic_id"),
        last_physical_playback_id=config.get("last_physical_playback_id"),
        active_capture=tuple(capture),
        active_render=tuple(render),
    )


def apply_decision(router, decision: Decision) -> bool:
    """Carries out a set decision. Microphone changes go through the router capture path, playback only through
    the guarded setter. Returns True on success."""
    if decision.target is None:
        return False
    if decision.action == ACTION_SET_MIC:
        return bool(router.set_default_capture(decision.target.id))
    if decision.action == ACTION_SET_PLAYBACK:
        return bool(set_default_playback_away_from_cable(decision.target.id))
    return False


# ----------------------------------------------------------------------------- runner

DEVICE_CHANGE_DELAY_S = 2.0
DEVICE_CHANGE = "device_change"


class CableGuard:
    """
    Runs decide() off the audio thread.
    - request(reason, delay): schedules one run; a later request before it fires replaces it (debounce).
    - A failed set is retried at most once, on the next device change. After the second failure the set is not
      tried again until the condition clears (the default is no longer the cable) or Getsu restarts.
    - Every decision and every result is logged, one line each.
    """

    def __init__(self, gather: Callable[[], GuardState], apply: Callable[[Decision], bool],
                 notify: Callable[[str, bool], None], logger: Optional[logging.Logger] = None):
        self._gather = gather
        self._apply = apply
        self._notify = notify
        self._log = logger or logging.getLogger("GetsuRouter")
        self._run_lock = threading.Lock()
        self._timer_lock = threading.Lock()
        self._timer: Optional[threading.Timer] = None
        self._pending_reason: Optional[str] = None
        self._closed = False
        self._failures: Dict[str, int] = {ACTION_SET_MIC: 0, ACTION_SET_PLAYBACK: 0}

    def request(self, reason: str, delay: float = DEVICE_CHANGE_DELAY_S) -> None:
        with self._timer_lock:
            if self._closed:
                return
            if self._timer is not None:
                self._timer.cancel()
                if self._pending_reason == DEVICE_CHANGE:
                    reason = DEVICE_CHANGE  # a pending device change keeps its retry allowance
            timer = threading.Timer(max(0.0, float(delay)), self._fire)
            timer.daemon = True
            self._timer = timer
            self._pending_reason = reason
            timer.start()

    def cancel(self) -> None:
        with self._timer_lock:
            self._closed = True
            if self._timer is not None:
                self._timer.cancel()
                self._timer = None

    def _fire(self) -> None:
        with self._timer_lock:
            reason = self._pending_reason or DEVICE_CHANGE
            self._pending_reason = None
            self._timer = None
            if self._closed:
                return
        self.run_now(reason)

    def run_now(self, reason: str) -> None:
        """One guard run in the calling thread. Exceptions are logged, never raised."""
        with self._run_lock:
            try:
                self._run_once(reason)
            except Exception as e:
                self._log.warning(f"[CABLE GUARD] {reason}: run failed: {e}")

    def _retry_allowed(self, action: str, reason: str) -> bool:
        failures = self._failures[action]
        return failures == 0 or (failures == 1 and reason == DEVICE_CHANGE)

    def _run_once(self, reason: str) -> None:
        decisions = decide(self._gather())
        for action in (ACTION_SET_MIC, ACTION_SET_PLAYBACK):
            if not any(d.action == action for d in decisions):
                self._failures[action] = 0  # condition cleared: a later problem gets a fresh attempt

        for decision in decisions:
            self._log.info(f"[CABLE GUARD] {reason}: {decision.log}")
            if decision.action not in (ACTION_SET_MIC, ACTION_SET_PLAYBACK):
                if decision.status:
                    self._notify(decision.status, decision.status_warn)
                continue
            if not self._retry_allowed(decision.action, reason):
                continue
            if self._apply(decision):
                self._failures[decision.action] = 0
                self._log.info(f"[CABLE GUARD] {reason}: {decision.action} done: {decision.target.name}")
                if decision.status:
                    self._notify(decision.status, decision.status_warn)
            else:
                self._failures[decision.action] += 1
                if self._failures[decision.action] == 1:
                    self._log.warning(f"[CABLE GUARD] {reason}: {decision.action} failed for "
                                      f"'{decision.target.name}'; retry once on the next device change")
                else:
                    self._log.warning(f"[CABLE GUARD] {reason}: {decision.action} failed again for "
                                      f"'{decision.target.name}'; not retrying until Getsu restarts")
