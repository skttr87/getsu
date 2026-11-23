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
# Windows numbers a repeated endpoint: "2- MPOW Wireless Gaming Headset". The number is not part of the name.
_ORDINAL_RE = re.compile(r"^\s*\d+\s*-\s*")


def split_name(full: str) -> Dict[str, str]:
    """Split "<endpoint word> (<device name>)" into the device name (title) and the endpoint word (prefix)."""
    match = _NAME_RE.match(full)
    if match:
        return {"title": match.group(2).strip(), "prefix": match.group(1).strip()}
    return {"title": full, "prefix": ""}


def strip_ordinal(title: str) -> str:
    """Title without a leading "<number>-" prefix and the spaces around it. A title that is only a number stays as it is."""
    stripped = _ORDINAL_RE.sub("", title, count=1).strip()
    return stripped or title.strip()


def display_titles(full_names) -> Dict[str, str]:
    """
    Display title for each detected microphone (full Windows endpoint name -> title shown in the UI).
    The "<number>-" prefix is dropped, unless two of the given microphones would then show the same title:
    those keep their numbers so they can be told apart. Only the shown title changes; identity and the
    hover text still use the full name.
    """
    names = list(full_names)
    base = {name: split_name(name)["title"] for name in names}
    plain = {name: strip_ordinal(base[name]) for name in names}
    counts: Dict[str, int] = {}
    for title in plain.values():
        counts[title] = counts.get(title, 0) + 1
    return {name: (base[name] if counts[plain[name]] > 1 else plain[name]) for name in names}


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
