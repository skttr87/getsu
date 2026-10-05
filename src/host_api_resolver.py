"""
Host API resolver for Getsu streams.

Why: PortAudio's WDM-KS backend fails on many Realtek endpoints (GLE 0x492 /
ERROR_SET_NOT_FOUND). A duplex stream also needs input and output on the SAME host API.
Instead of opening a device and patching things up after it fails, this module picks a
matching (input, output) pair up front, trying host APIs in order WASAPI -> DirectSound -> MME.
WDM-KS is never auto-selected.

Entry points:
- resolve_pair(...)   : duplex engine (input + VB-Cable output on one host API)
- resolve_single(...) : one device, e.g. the "Hear Myself" voice test using separate streams

Pure functions: pass in sd.query_devices() / sd.query_hostapis() so they are easy to test.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import List, Optional, Sequence, Tuple

PREFERRED_HOST_APIS = ("WASAPI", "DIRECTSOUND", "MME")
AVOIDED_HOST_APIS = ("WDM-KS",)
MATCH_THRESHOLD = 0.6
_STOP_TOKENS = frozenset({"r", "hd", "input", "audio", "device", "high", "definition"})

# Jack "roles" let a WDM-KS name like "Mic in at front panel (black)" find
# "Microphone (Realtek(R) Audio)" even though the names share no words.
_ROLES = {"mic": {"mic", "microphone"}, "line": {"line"}, "mix": {"mix"}}


class DeviceResolutionError(RuntimeError):
    """No usable device / pair exists on a safe host API."""


@dataclass
class ResolvedPair:
    input: int
    output: int
    host_name: str
    substituted_input: bool = False   # input was chosen by role/default, not by name
    ambiguous: bool = False           # several equally plausible inputs; GUI should let the user confirm
    notes: List[str] = field(default_factory=list)

    @property
    def is_wasapi(self) -> bool:
        return "WASAPI" in self.host_name.upper()


def _is_avoided(host_name: str) -> bool:
    upper = host_name.upper()
    return any(a in upper for a in AVOIDED_HOST_APIS)


def _rank(host_name: str) -> int:
    upper = host_name.upper()
    for i, key in enumerate(PREFERRED_HOST_APIS):
        if key in upper:
            return i
    return len(PREFERRED_HOST_APIS)


def _tokens(name: str) -> set:
    return {t for t in re.findall(r"[a-z0-9]+", name.lower()) if t not in _STOP_TOKENS}


def _similarity(a: str, b: str) -> float:
    ta, tb = _tokens(a), _tokens(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


def _role(name: str) -> Optional[str]:
    """'mic' / 'line' / 'mix' when the name clearly indicates exactly one jack type, else None."""
    words = set(re.findall(r"[a-z0-9]+", name.lower()))
    found = [r for r, keys in _ROLES.items() if words & keys]
    return found[0] if len(found) == 1 else None


def _api_order(hostapis) -> List[int]:
    return sorted(
        (i for i, h in enumerate(hostapis) if not _is_avoided(h["name"])),
        key=lambda i: _rank(hostapis[i]["name"]),
    )


def _find_input(devices, hostapis, api_idx: int, src_idx: int, original_unusable: bool):
    """Return (index, how, ambiguous); how in {'same','name','role','default'}; index None if no match."""
    src = devices[src_idx]
    if src["hostapi"] == api_idx:
        return src_idx, "same", False

    candidates = [i for i in hostapis[api_idx]["devices"] if devices[i]["max_input_channels"] > 0]
    best, best_score = None, 0.0
    for i in candidates:
        score = _similarity(src["name"], devices[i]["name"])
        if score > best_score:
            best, best_score = i, score
    if best is not None and best_score >= MATCH_THRESHOLD:
        return best, "name", False

    # Guessing is only allowed when the original device is unusable (WDM-KS).
    # Never silently swap a working device for "the first microphone in the list".
    if not original_unusable:
        return None, "", False

    pool = candidates
    role = _role(src["name"])
    if role:
        same = [i for i in candidates if _role(devices[i]["name"]) == role]
        if len(same) == 1:
            return same[0], "role", False
        if same:
            pool = same

    default = hostapis[api_idx].get("default_input_device", -1)
    if default is not None and default in pool:
        return default, "default", len(pool) > 1
    if role and pool is not candidates:      # several same-role inputs, default is something else
        return pool[0], "role", True
    return None, "", False


def _find_output(devices, hostapis, api_idx: int, out_idx: int) -> Optional[int]:
    """Keep the chosen output if it is already on this host API, else use VB-Cable's 'CABLE Input'."""
    cur = devices[out_idx]
    if cur["hostapi"] == api_idx and cur["max_output_channels"] > 0:
        return out_idx
    cables = [
        i for i in hostapis[api_idx]["devices"]
        if devices[i]["max_output_channels"] > 0 and "cable input" in devices[i]["name"].lower()
    ]
    cables.sort(key=lambda i: ("16ch" in devices[i]["name"].lower(), i))
    return cables[0] if cables else None


def _check_index(devices, *indices):
    for idx in indices:
        if not (0 <= idx < len(devices)):
            raise DeviceResolutionError(
                f"Device index {idx} is out of range. Device numbering changes when audio drivers "
                "or jacks change; reselect the device."
            )


def resolve_pair(
    devices: Sequence[dict],
    hostapis: Sequence[dict],
    input_idx: int,
    output_idx: int,
) -> ResolvedPair:
    _check_index(devices, input_idx, output_idx)

    orig_in = devices[input_idx]
    orig_host = hostapis[orig_in["hostapi"]]["name"]
    original_unusable = _is_avoided(orig_host)
    api_order = _api_order(hostapis)

    for api_idx in api_order:
        inp, how, ambiguous = _find_input(devices, hostapis, api_idx, input_idx, original_unusable)
        if inp is None:
            continue
        out = _find_output(devices, hostapis, api_idx, output_idx)
        if out is None:
            continue

        host_name = hostapis[api_idx]["name"]
        notes: List[str] = []
        if inp != input_idx:
            notes.append(f"Input moved from '{orig_in['name']}' ({orig_host}) to "
                         f"'{devices[inp]['name']}' ({host_name}).")
        if how == "role":
            notes.append("No exact name match; chose the input with the same jack type.")
        elif how == "default":
            notes.append(f"No matching endpoint on {host_name}; using its default input.")
        if ambiguous:
            notes.append("More than one input could match; please confirm your microphone.")
        if out != output_idx:
            notes.append(f"Output moved to '{devices[out]['name']}' ({host_name}) to match the input.")
        return ResolvedPair(inp, out, host_name, how in ("role", "default"), ambiguous, notes)

    tried = ", ".join(hostapis[i]["name"] for i in api_order) or "none available"
    raise DeviceResolutionError(
        f"No usable microphone + virtual cable pair found (tried: {tried}; WDM-KS is skipped). "
        "Check that VB-Cable ('CABLE Input') is installed and the microphone is plugged in and "
        "enabled in Windows Sound settings."
    )


def resolve_single(
    devices: Sequence[dict],
    hostapis: Sequence[dict],
    idx: int,
    is_input: bool = True,
) -> Tuple[int, List[str]]:
    """
    Make one device safe to open on its own (e.g. voice-test streams). Devices already on a
    non-WDM-KS host API are returned unchanged. Returns (index, notes).
    """
    _check_index(devices, idx)
    dev = devices[idx]
    orig_host = hostapis[dev["hostapi"]]["name"]
    if not _is_avoided(orig_host):
        return idx, []

    key = "max_input_channels" if is_input else "max_output_channels"
    default_key = "default_input_device" if is_input else "default_output_device"
    for api_idx in _api_order(hostapis):
        if is_input:
            found, how, _ = _find_input(devices, hostapis, api_idx, idx, True)
        else:
            cands = [i for i in hostapis[api_idx]["devices"] if devices[i][key] > 0]
            scored = sorted(((_similarity(dev["name"], devices[i]["name"]), i) for i in cands), reverse=True)
            if scored and scored[0][0] >= MATCH_THRESHOLD:
                found, how = scored[0][1], "name"
            else:
                d = hostapis[api_idx].get(default_key, -1)
                found, how = (d, "default") if d is not None and d >= 0 and devices[d][key] > 0 else (None, "")
        if found is not None:
            host_name = hostapis[api_idx]["name"]
            notes = [f"'{dev['name']}' ({orig_host}) replaced by '{devices[found]['name']}' ({host_name})."]
            if how in ("role", "default"):
                notes.append("No exact name match; please confirm this is the right device.")
            return found, notes
    raise DeviceResolutionError(
        f"No usable replacement for '{dev['name']}' found outside WDM-KS. "
        "Check that the device is plugged in and enabled in Windows Sound settings."
    )

