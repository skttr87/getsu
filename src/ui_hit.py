"""
Hit testing for the custom click handling of the main window.

DearPyGui does not report a child window as hovered while the pointer is over one of its own child windows,
so clicks are resolved here from item rectangles instead. Pure functions: no DearPyGui calls, unit-tested.
Rectangles are (min_point, max_point) pairs in the same coordinate space as the point.
"""
from typing import Any, Iterable, List, Optional, Sequence, Tuple

Point = Tuple[float, float]
Rect = Tuple[Point, Point]


def point_in_rect(point: Sequence[float], rect: Rect) -> bool:
    """True when point lies inside rect. The min edge is inside, the max edge is outside."""
    (x0, y0), (x1, y1) = rect
    x, y = point[0], point[1]
    return x0 <= x < x1 and y0 <= y < y1


def pick_target(point: Sequence[float], targets: Iterable[Tuple[Any, Optional[Rect]]]) -> Optional[Any]:
    """
    Returns the key of the topmost target under point, or None.
    targets are (key, rect) pairs in draw order: the last matching target is on top. Targets without a rect
    (None, or an empty rectangle) never match.
    """
    hit = None
    for key, rect in targets:
        if rect is None:
            continue
        if point_in_rect(point, rect):
            hit = key
    return hit


def click_is_outside(point: Sequence[float], sheet_rect: Optional[Rect]) -> bool:
    """
    True when a click should close an open panel: the sheet is measured and the point is outside it.
    When the sheet has no rectangle yet, the panel stays open (nothing is guessed).
    """
    if sheet_rect is None:
        return False
    return not point_in_rect(point, sheet_rect)


def _clamp(v: float, lo: float, hi: float) -> float:
    return lo if v < lo else hi if v > hi else v


def slider_knob_x(value: float, x0: float, width: float, pad: float, lo: float, hi: float) -> float:
    """
    X of the knob centre for a value. The track runs from x0 + pad to x0 + width - pad, so the knob (and its halo)
    never leaves the control. The value is clamped to [lo, hi] for drawing.
    """
    t = (_clamp(value, lo, hi) - lo) / (hi - lo)
    return x0 + pad + t * (width - 2.0 * pad)


def slider_value_at(x: float, x0: float, width: float, pad: float, lo: float, hi: float, step: float = 0.01) -> float:
    """Value under x (inverse of slider_knob_x), clamped to [lo, hi] and rounded to step."""
    span = width - 2.0 * pad
    t = _clamp((x - x0 - pad) / span, 0.0, 1.0) if span > 0 else 0.0
    value = lo + t * (hi - lo)
    return round(round(value / step) * step, 2)


def segment_layout(width: float, height: float, count: int, inset: float) -> List[Tuple[Rect, Rect]]:
    """
    Geometry of a segmented control of count equal segments across a width x height track whose origin is the
    top-left corner. Returns one (segment, pill) pair per segment: the segment is its full-height slot, and the pill is
    the highlight drawn for a selected segment, inset by inset on all four sides. Pure geometry, no rounding.
    """
    if count <= 0:
        return []
    seg_w = width / float(count)
    layout = []
    for i in range(count):
        x0 = i * seg_w
        x1 = x0 + seg_w
        segment = ((x0, 0.0), (x1, float(height)))
        pill = ((x0 + inset, inset), (x1 - inset, height - inset))
        layout.append((segment, pill))
    return layout


def label_origin(segment: Rect, text_w: float, text_h: float) -> Tuple[float, float]:
    """Top-left origin that centres a text run of text_w x text_h in a segment rectangle."""
    (x0, y0), (x1, y1) = segment
    return ((x0 + x1) / 2.0 - text_w / 2.0, (y0 + y1) / 2.0 - text_h / 2.0)


def segment_index(point: Sequence[float], rect: Optional[Rect], count: int, inset: float = 0.0) -> Optional[int]:
    """
    Index of the equal segment under point in a control of count segments spanning rect (inset from both ends),
    or None when the point is outside the rectangle.
    """
    if rect is None or count <= 0 or not point_in_rect(point, rect):
        return None
    (x0, _), (x1, _) = rect
    left = x0 + inset
    seg_w = (x1 - inset - left) / count
    if seg_w <= 0:
        return None
    return int(_clamp((point[0] - left) // seg_w, 0, count - 1))
