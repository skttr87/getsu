"""
getsu-cli --probe-mic: opens one microphone the ways the engine may open it and prints what worked.

Each attempt is an input-only stream read for one second; the peak level of what was captured is
reported. The optional duplex step opens the microphone together with the CABLE Input endpoint for one
second, the same way the engine does, but without any router call, so the Windows default microphone is
never changed. Results are printed as a table and can be pasted into a bug report.
"""
import math
from dataclasses import dataclass
from typing import Callable, List, Optional, Sequence

import numpy as np

from src.audio_errors import explain_start_error
from src.host_api_resolver import (
    DeviceResolutionError,
    MATCH_THRESHOLD,
    _find_input,
    _rank,
    _similarity,
    resolve_pair,
)

PROBE_SECONDS = 1.0
DUPLEX_RATE = 48000
DUPLEX_BLOCK = 480
SILENT_DB = -120.0


@dataclass
class ProbeAttempt:
    label: str
    host_api: str
    rate: Optional[int]
    channels: Optional[int]
    ok: bool
    result: str
    peak_db: Optional[float] = None


def peak_dbfs(samples: np.ndarray) -> float:
    """Peak level of a float32 block in dBFS (full scale 1.0). Silence is reported as -120 dBFS."""
    if samples is None or samples.size == 0:
        return SILENT_DB
    peak = float(np.max(np.abs(samples)))
    return 20.0 * math.log10(peak) if peak > 0.0 else SILENT_DB


def describe_error(exc: BaseException) -> str:
    explained = explain_start_error(exc)
    if explained.code is not None:
        return f"PaErrorCode {explained.code}: {explained.title}"
    text = str(exc).strip().splitlines()[0] if str(exc).strip() else type(exc).__name__
    return f"{type(exc).__name__}: {text[:100]}"


def _channels_for(dev: dict) -> int:
    return min(2, max(1, int(dev["max_input_channels"])))


def _capture_once(sd, device_idx: int, rate: int, channels: int, auto_convert: Optional[bool]) -> float:
    """Reads PROBE_SECONDS of input and returns the peak in dBFS. Raises on open or read failure."""
    extra = sd.WasapiSettings(auto_convert=True) if auto_convert else None
    frames = int(round(rate * PROBE_SECONDS))
    with sd.InputStream(device=device_idx, samplerate=rate, channels=channels,
                        dtype="float32", extra_settings=extra) as stream:
        data, _overflowed = stream.read(frames)
    return peak_dbfs(data)


def _duplex_once(sd, devices, hostapis, mic_idx: int, cable_idx: int) -> ProbeAttempt:
    pair = resolve_pair(devices, hostapis, mic_idx, cable_idx)   # raises DeviceResolutionError
    in_ch = _channels_for(devices[pair.input])
    out_ch = min(2, max(1, int(devices[pair.output]["max_output_channels"])))
    label = f"duplex mic + CABLE Input, {DUPLEX_RATE} Hz"
    host = pair.host_name
    extra = sd.WasapiSettings(auto_convert=True) if pair.is_wasapi else None
    silence = np.zeros((DUPLEX_BLOCK, out_ch), dtype=np.float32)
    blocks = int(round(PROBE_SECONDS * DUPLEX_RATE / DUPLEX_BLOCK))
    peak = 0.0
    with sd.Stream(device=(pair.input, pair.output), samplerate=DUPLEX_RATE, blocksize=DUPLEX_BLOCK,
                   channels=(in_ch, out_ch), dtype="float32", latency="low", extra_settings=extra) as stream:
        for _ in range(blocks):
            data, _overflowed = stream.read(DUPLEX_BLOCK)
            stream.write(silence)
            peak = max(peak, float(np.max(np.abs(data))) if data.size else 0.0)
    peak_db = 20.0 * math.log10(peak) if peak > 0.0 else SILENT_DB
    return ProbeAttempt(label, host, DUPLEX_RATE, in_ch, True, "ok", peak_db)


def find_input_index(devices: Sequence[dict], hostapis: Sequence[dict], target: Optional[str]) -> int:
    """
    Resolves a probe target: an index, a device name, or None (saved config, then the automatic choice).
    Name matches prefer WASAPI, then the closest name. Raises ValueError when nothing matches.
    """
    if target is None or str(target).strip() == "":
        from src.config import load_config
        from src.devices import auto_select_input_device
        cfg_idx = load_config().get("input_device_id")
        if isinstance(cfg_idx, int) and 0 <= cfg_idx < len(devices) and devices[cfg_idx]["max_input_channels"] > 0:
            return cfg_idx
        auto = auto_select_input_device()
        if auto and isinstance(auto, dict) and auto.get("index") is not None:
            return int(auto["index"])
        raise ValueError("no microphone found; pass an index or a name")

    text = str(target).strip()
    if text.isdigit():
        idx = int(text)
        if not (0 <= idx < len(devices)) or devices[idx]["max_input_channels"] <= 0:
            raise ValueError(f"device index {idx} is not an input device")
        return idx

    inputs = [i for i, d in enumerate(devices) if d["max_input_channels"] > 0]
    exact = [i for i in inputs if devices[i]["name"].casefold() == text.casefold()]
    if exact:
        return min(exact, key=lambda i: (_rank(hostapis[devices[i]["hostapi"]]["name"]), i))
    best, best_score = None, 0.0
    for i in inputs:
        score = _similarity(text, devices[i]["name"])
        if score > best_score:
            best, best_score = i, score
    if best is not None and best_score >= MATCH_THRESHOLD:
        return best
    raise ValueError(f"no input device matches '{text}'")


def _find_cable_input(devices: Sequence[dict]) -> Optional[int]:
    """CABLE Input endpoints (output side), WASAPI first, then the other host APIs."""
    cands = [i for i, d in enumerate(devices)
             if d["max_output_channels"] > 0 and "cable input" in d["name"].lower()]
    return min(cands, key=lambda i: (("16ch" in devices[i]["name"].lower()), i)) if cands else None


def run_probe(target: Optional[str] = None, out: Callable[[str], None] = print, duplex: bool = True) -> int:
    """Runs the probe, prints the report through `out`, and returns a process exit code (0 when any attempt opened)."""
    import sounddevice as sd

    devices = sd.query_devices()
    hostapis = sd.query_hostapis()
    try:
        mic_idx = find_input_index(devices, hostapis, target)
    except ValueError as e:
        out(f"[PROBE] {e}")
        return 2

    mic = devices[mic_idx]
    mic_api = hostapis[mic["hostapi"]]["name"]
    try:
        pa_version = sd.get_portaudio_version()[1]
    except Exception:
        pa_version = "unknown"
    out(f"Getsu microphone probe (sounddevice {sd.__version__}, {pa_version})")
    out(f"Microphone: [{mic_idx}] {mic['name']} | {mic_api} | default {mic['default_samplerate']:.0f} Hz | "
        f"{int(mic['max_input_channels'])} ch")
    out("")

    # (label, host API key, rate or None for the endpoint default, auto_convert)
    plan = [
        ("WASAPI shared, auto_convert, 48000 Hz", "WASAPI", DUPLEX_RATE, True),
        ("WASAPI, endpoint default rate", "WASAPI", None, False),
        ("DirectSound, 48000 Hz", "DIRECTSOUND", DUPLEX_RATE, False),
        ("MME, 48000 Hz", "MME", DUPLEX_RATE, False),
    ]
    attempts: List[ProbeAttempt] = []
    total = len(plan) + (1 if duplex else 0)
    for n, (label, api_key, rate, auto_convert) in enumerate(plan, 1):
        out(f"[{n}/{total}] {label}: opening for {PROBE_SECONDS:.0f} s ...")
        attempts.append(_probe_one(sd, devices, hostapis, mic_idx, label, api_key, rate, auto_convert))
        out(f"      -> {attempts[-1].result}")

    if duplex:
        cable_idx = _find_cable_input(devices)
        if cable_idx is None:
            out(f"[{total}/{total}] duplex mic + CABLE Input: skipped (CABLE Input not found)")
        else:
            n = total
            out(f"[{n}/{total}] duplex mic + CABLE Input, {DUPLEX_RATE} Hz: opening for {PROBE_SECONDS:.0f} s ...")
            try:
                attempt = _duplex_once(sd, devices, hostapis, mic_idx, cable_idx)
            except DeviceResolutionError as e:
                attempt = ProbeAttempt(f"duplex mic + CABLE Input, {DUPLEX_RATE} Hz", "-", DUPLEX_RATE, None,
                                       False, f"no usable pair: {e}")
            except Exception as e:
                attempt = ProbeAttempt(f"duplex mic + CABLE Input, {DUPLEX_RATE} Hz", "-", DUPLEX_RATE, None,
                                       False, describe_error(e))
            attempts.append(attempt)
            out(f"      -> {attempt.result}")

    out("")
    _print_table(attempts, out)
    opened = sum(1 for a in attempts if a.ok)
    out("")
    out(f"{opened} of {len(attempts)} attempts opened the microphone.")
    return 0 if opened else 1


def _probe_one(sd, devices, hostapis, mic_idx: int, label: str, api_key: str,
               rate: Optional[int], auto_convert: bool) -> ProbeAttempt:
    api_idx = next((i for i, h in enumerate(hostapis) if api_key in h["name"].upper()), None)
    if api_idx is None:
        return ProbeAttempt(label, api_key, rate, None, False, f"{api_key} host API not present")
    found, _how, _amb = _find_input(devices, hostapis, api_idx, mic_idx, False)
    host_name = hostapis[api_idx]["name"]
    if found is None:
        return ProbeAttempt(label, host_name, rate, None, False, "no matching input endpoint on this host API")
    dev = devices[found]
    use_rate = int(round(dev["default_samplerate"])) if rate is None else int(rate)
    channels = _channels_for(dev)
    try:
        peak = _capture_once(sd, found, use_rate, channels, auto_convert)
    except Exception as e:
        return ProbeAttempt(label, host_name, use_rate, channels, False, describe_error(e))
    return ProbeAttempt(label, host_name, use_rate, channels, True, "ok", peak)


def _print_table(attempts: List[ProbeAttempt], out: Callable[[str], None]) -> None:
    """Fixed columns; the result text is last so long error messages are never cut."""
    head = f"{'#':>2}  {'attempt':<36} {'host API':<22} {'rate':>6} {'ch':>2} {'peak dBFS':>9}  result"
    out(head)
    out("-" * len(head))
    for i, a in enumerate(attempts, 1):
        rate = f"{a.rate}" if a.rate else "-"
        ch = f"{a.channels}" if a.channels else "-"
        peak = f"{a.peak_db:.1f}" if a.peak_db is not None else "-"
        out(f"{i:>2}  {a.label:<36} {a.host_api[:22]:<22} {rate:>6} {ch:>2} {peak:>9}  {a.result}")
