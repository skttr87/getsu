"""
Icon geometry for the GUI, drawn with DearPyGui drawlist primitives (no SVG renderer, no image files).

Shapes are defined on the 24 x 24 grid of the approved mockup (opt/mockup/a-focus.html, SVG symbols)
and mapped to pixels with to_px(). Functions return plain point lists so the geometry can be tested
without DearPyGui.
"""
import math
from typing import List, Sequence, Tuple

Point = Tuple[float, float]

GRID = 24.0


def to_px(points: Sequence[Point], x0: float, y0: float, size: float) -> List[Point]:
    """Map grid points to pixels for a square box of the given size whose top-left corner is (x0, y0)."""
    scale = size / GRID
    return [(x0 + x * scale, y0 + y * scale) for x, y in points]


def arc_points(cx: float, cy: float, radius: float, start_deg: float, end_deg: float, steps: int = 32) -> List[Point]:
    """Points along a circle from start_deg to end_deg (degrees, y axis pointing down, sweep may be negative)."""
    pts = []
    for k in range(steps + 1):
        angle = math.radians(start_deg + (end_deg - start_deg) * k / steps)
        pts.append((cx + radius * math.cos(angle), cy + radius * math.sin(angle)))
    return pts


def circle_points(cx: float, cy: float, radius: float, steps: int = 24) -> List[Point]:
    return arc_points(cx, cy, radius, 0.0, 360.0, steps)[:-1]


def rounded_rect_points(x: float, y: float, w: float, h: float, r: float, steps: int = 6) -> List[Point]:
    """Outline of a rounded rectangle, clockwise from the top-left corner."""
    r = min(r, w / 2, h / 2)
    pts: List[Point] = []
    corners = (
        (x + w - r, y + r, -90.0, 0.0),
        (x + w - r, y + h - r, 0.0, 90.0),
        (x + r, y + h - r, 90.0, 180.0),
        (x + r, y + r, 180.0, 270.0),
    )
    for cx, cy, a0, a1 in corners:
        pts.extend(arc_points(cx, cy, r, a0, a1, steps))
    return pts


# Glyph shapes in grid units. Each entry is (kind, points or params).
# Power: stroke gap at the top, vertical stem from the top of the circle.
POWER_STEM = [(12.0, 3.0), (12.0, 11.0)]
POWER_ARC = arc_points(12.0, 12.0, 8.0, -136.5, -403.5, 40)

PLAY = [(8.0, 5.5), (8.0, 18.5), (18.0, 12.0)]
CHEVRON = [(9.0, 6.0), (15.0, 12.0), (9.0, 18.0)]
CHECK = [(5.0, 12.5), (9.5, 17.0), (19.0, 7.5)]

SLIDER_LINES = [((4.0, 7.0), (14.0, 7.0)), ((18.0, 7.0), (20.0, 7.0)),
                ((4.0, 17.0), (8.0, 17.0)), ((12.0, 17.0), (20.0, 17.0))]
SLIDER_KNOBS = [((16.0, 7.0), 2.0), ((10.0, 17.0), 2.0)]

HELP_RING = circle_points(12.0, 12.0, 9.0)
HELP_QUESTION = arc_points(12.0, 9.5, 2.5, 180.0, 400.0, 16) + [(12.0, 14.0)]
HELP_DOT = (12.0, 17.0)

# Input device icons (same shapes as the mockup symbols i-mic, i-laptop, i-headset, i-buds).
MIC_BODY = rounded_rect_points(9.0, 3.0, 6.0, 11.0, 3.0)
MIC_CRADLE = arc_points(12.0, 11.0, 6.5, 0.0, 180.0, 16)
MIC_STEM = [(12.0, 17.5), (12.0, 21.0)]
LAPTOP_SCREEN = rounded_rect_points(4.0, 5.0, 16.0, 11.0, 1.5)
LAPTOP_BASE = [(2.0, 19.0), (22.0, 19.0)]
HEADSET_BAND = arc_points(12.0, 14.0, 8.0, 180.0, 360.0, 20)
HEADSET_LEFT = rounded_rect_points(3.0, 14.0, 4.0, 6.0, 1.5)
HEADSET_RIGHT = rounded_rect_points(17.0, 14.0, 4.0, 6.0, 1.5)
BUD_LEFT = rounded_rect_points(4.0, 4.0, 6.0, 8.0, 3.0)
BUD_RIGHT = rounded_rect_points(14.0, 4.0, 6.0, 8.0, 3.0)
BUD_STEM_LEFT = [(8.0, 12.0), (8.0, 19.0)]
BUD_STEM_RIGHT = [(16.0, 12.0), (16.0, 19.0)]
