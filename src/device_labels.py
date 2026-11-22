"""
Display labels for microphones: the title, the device kind, and the subtitle.

Windows endpoint names look like "<endpoint word> (<device name>)", for example
"Microphone (Realtek(R) Audio)" or "Headset (HUAWEI FreeBuds SE 4 ANC)". The part inside the
outer parentheses is shown as the title. The kind is the one devices.classify_device() returns
(bus enumerator and endpoint form factor reported by Windows; no device names or vendor IDs).
"""
import re
from typing import Dict

from src.devices import DeviceKind, kind_label

_NAME_RE = re.compile(r"^([^()]+?)\s*\((.+)\)\s*$")


def split_name(full: str) -> Dict[str, str]:
    """Split "<endpoint word> (<device name>)" into the device name (title) and the endpoint word (prefix)."""
    match = _NAME_RE.match(full)
    if match:
        return {"title": match.group(2).strip(), "prefix": match.group(1).strip()}
    return {"title": full, "prefix": ""}


def base_subtitle(name: str, kind: str) -> str:
    """Type label for a microphone. For an unknown type the endpoint word ("Line In") is more useful than a generic label."""
    if kind == DeviceKind.OTHER:
        return split_name(name)["prefix"] or kind_label(DeviceKind.OTHER, is_input=True)
    return kind_label(kind, is_input=True)


def subtitle(name: str, kind: str, all_entries: Dict[str, str]) -> str:
    """
    Subtitle for one microphone. The endpoint word is added only when another entry would show the
    same title with the same type label, so duplicates can be told apart.

    all_entries maps every detected full name to its kind (DeviceKind value).
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
