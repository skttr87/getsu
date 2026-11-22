"""
Hit testing for the custom click handling of the main window.

DearPyGui does not report a child window as hovered while the pointer is over one of its own child windows,
so clicks are resolved here from item rectangles instead. Pure functions: no DearPyGui calls, unit-tested.
Rectangles are (min_point, max_point) pairs in the same coordinate space as the point.
"""
from typing import Iterable, Optional, Sequence, Tuple, Any

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
