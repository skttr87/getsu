"""
Font sizes for DearPyGui, converted from CSS (design) pixels.

DearPyGui draws text with ImGui, which takes a font size as the full line height (ascender plus descender) in
pixels. CSS and the design mockup take the em size. For a font with (hhea ascender - hhea descender) / unitsPerEm
equal to k, a size of N DearPyGui pixels gives an em of N / k pixels, so the same N would draw letters about
1 - 1/k smaller than CSS N px. The conversion below multiplies by k so that a CSS size keeps its design look.
"""
import struct


def read_line_height_factor(path: str) -> float:
    """
    (hhea ascender - hhea descender) / unitsPerEm of a TrueType or OpenType file. This is the factor ImGui applies
    (stb_truetype scales a font so that the ascender-to-descender span equals the requested pixel size).
    Raises ValueError when the file is not a readable single-font sfnt.
    """
    with open(path, "rb") as f:
        return line_height_factor_from_bytes(f.read())


def line_height_factor_from_bytes(data: bytes) -> float:
    """Same as read_line_height_factor for file contents already in memory."""
    if len(data) < 12 or data[:4] == b"ttcf":
        raise ValueError("not a single-font sfnt file")
    num_tables = struct.unpack(">H", data[4:6])[0]
    tables = {}
    for i in range(num_tables):
        start = 12 + 16 * i
        if start + 16 > len(data):
            raise ValueError("truncated table directory")
        tag, _checksum, offset, length = struct.unpack(">4sIII", data[start:start + 16])
        tables[tag] = (offset, length)
    if b"head" not in tables or b"hhea" not in tables:
        raise ValueError("missing head or hhea table")
    head_off, head_len = tables[b"head"]
    hhea_off, hhea_len = tables[b"hhea"]
    if head_len < 20 or hhea_len < 8 or head_off + 20 > len(data) or hhea_off + 8 > len(data):
        raise ValueError("head or hhea table too short")
    units_per_em = struct.unpack(">H", data[head_off + 18:head_off + 20])[0]
    ascender, descender = struct.unpack(">hh", data[hhea_off + 4:hhea_off + 8])
    if units_per_em <= 0 or ascender - descender <= 0:
        raise ValueError("unusable font metrics")
    return (ascender - descender) / float(units_per_em)


def font_px(css_px: float, factor: float, dpi_scale: float) -> int:
    """DearPyGui font size in physical pixels for a CSS size: css * line-height factor * monitor DPI scale."""
    return max(1, int(round(css_px * factor * dpi_scale)))
