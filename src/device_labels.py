"""
Display labels for microphones: the title, the device kind, and the subtitle.

Windows endpoint names look like "<endpoint word> (<device name>)", for example
"Microphone (Realtek(R) Audio)" or "Headset (HUAWEI FreeBuds SE 4 ANC)". The part inside the
outer parentheses is shown as the title. The kind comes from the bus enumerator that Windows
reports for the endpoint (no device names or vendor IDs are used here).
"""
import re
from typing import Dict, Optional

KIND_BLUETOOTH = "bt"
KIND_USB = "usb"
KIND_BUILTIN = "builtin"
KIND_OTHER = "other"

KIND_LABELS = {
    KIND_BLUETOOTH: "Bluetooth headset",
    KIND_USB: "USB headset",
    KIND_BUILTIN: "Built-in microphone",
    KIND_OTHER: "Microphone",
}

_NAME_RE = re.compile(r"^([^()]+?)\s*\((.+)\)\s*$")


def split_name(full: str) -> Dict[str, str]:
    """Split "<endpoint word> (<device name>)" into the device name (title) and the endpoint word (prefix)."""
    match = _NAME_RE.match(full)
    if match:
        return {"title": match.group(2).strip(), "prefix": match.group(1).strip()}
    return {"title": full, "prefix": ""}


def kind_from_bus_type(bus_type: Optional[str]) -> str:
    """Map a Windows bus enumerator name (router.native_get_device_bus_type) to a device kind."""
    if bus_type == "BTHHFENUM":
        return KIND_BLUETOOTH
    if bus_type == "USB":
        return KIND_USB
    if bus_type in ("HDAUDIO", "INTELAUDIO"):
        return KIND_BUILTIN
    return KIND_OTHER


def base_subtitle(name: str, kind: str) -> str:
    """Type label. For an unknown type the endpoint word ("Line In") is more useful than a generic label."""
    if kind == KIND_OTHER:
        return split_name(name)["prefix"] or KIND_LABELS[KIND_OTHER]
    return KIND_LABELS[kind]


def subtitle(name: str, kind: str, all_entries: Dict[str, str]) -> str:
    """
    Subtitle for one microphone. The endpoint word is added only when another entry would show the
    same title with the same type label, so duplicates can be told apart.

    all_entries maps every detected full name to its kind.
    """
    title = split_name(name)["title"]
    base = base_subtitle(name, kind)
    prefix = split_name(name)["prefix"]
    clash = any(
        other != name
        and split_name(other)["title"] == title
        and base_subtitle(other, other_kind) == base
        for other, other_kind in all_entries.items()
    )
    return f"{base}, {prefix}" if (clash and prefix) else base
