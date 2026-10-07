"""
Turn PortAudio / device-resolution failures into (a) plain-language advice for the GUI and
(b) the raw technical text for logs, so a friendly message never hides the real cause.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

_CODE_RE = re.compile(r"PaErrorCode\s+(-?\d+)")


@dataclass(frozen=True)
class ExplainedError:
    kind: str                 # wdmks | busy | sample_rate | host_mismatch | invalid_device | channels |
                              # host_settings | no_pair | host_error | unknown
    title: str
    advice: str
    technical: str            # raw error text (trimmed to 400 chars) for logs
    code: Optional[int] = None


def explain_start_error(exc: BaseException) -> ExplainedError:
    text = str(exc).strip()
    technical = text[:400]
    low = text.lower()
    m = _CODE_RE.search(text)
    code = int(m.group(1)) if m else None

    def make(kind: str, title: str, advice: str) -> ExplainedError:
        return ExplainedError(kind, title, advice, technical, code)

    if type(exc).__name__ == "DeviceResolutionError" or "DeviceResolutionError" in type(exc).__name__:
        return make("no_pair", "No usable microphone and virtual cable pair", text)

    if "wdmsyncioctl" in low or "wdm-ks" in low or "0x00000492" in low:
        return make(
            "wdmks",
            "This microphone entry uses an audio path your driver doesn't support",
            "Pick the same microphone again from the list (Getsu now uses the standard Windows audio path). "
            "If it keeps happening, check that the microphone is plugged into the pink jack.",
        )
    if code == -9985:
        return make(
            "busy",
            "The microphone is busy",
            "Close other apps that may be using it (Discord, OBS, browser tabs), and in Windows Sound settings "
            "turn off 'Allow applications to take exclusive control' for the microphone and the virtual cable.",
        )
    if code == -9997:
        return make(
            "sample_rate",
            "The device doesn't accept 48 kHz",
            "In Windows Sound settings > device Properties > Advanced, set the default format to 48000 Hz.",
        )
    if code == -9993:
        return make(
            "host_mismatch",
            "Audio driver mismatch",
            "Re-selecting your microphone in the dropdown fixes this.",
        )
    if code == -9996:
        return make(
            "invalid_device",
            "The selected audio device is no longer available",
            "Check that the microphone is plugged in, then re-select it in the dropdown.",
        )
    if code == -9998:
        return make(
            "channels",
            "The device channel layout is unsupported",
            "Getsu supports 1-channel (mono) and 2-channel (stereo) microphones. Check Windows Sound properties.",
        )
    if code == -9984:
        return make(
            "host_settings",
            "Audio driver settings rejected",
            "Try toggling the microphone in the dropdown or restarting Getsu.",
        )
    if code == -9999:
        return make(
            "host_error",
            "Windows audio driver error",
            "Ensure your microphone is plugged in securely. If you selected 'Line In', select 'Microphone' instead.",
        )
    return make(
        "unknown",
        "Could not start microphone",
        "Re-select your microphone in the dropdown, or check Windows Sound settings.",
    )

