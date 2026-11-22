"""
Frame pacing for the GUI render loop.

A visible window is rendered at most 20 times per second. A hidden or minimized window is rendered
once every 100 ms: each rendered frame also pumps the window message queue (tray restore, device
change notifications, Windows shutdown), so the gap between frames must stay short.
"""

VISIBLE_FRAME_INTERVAL_S = 0.05
HIDDEN_FRAME_INTERVAL_S = 0.10


def frame_interval_s(window_visible: bool) -> float:
    """Target time between the starts of two rendered frames."""
    return VISIBLE_FRAME_INTERVAL_S if window_visible else HIDDEN_FRAME_INTERVAL_S


def frame_sleep_s(window_visible: bool, frame_elapsed_s: float) -> float:
    """Seconds to wait after a frame that took frame_elapsed_s, so the next frame starts on the interval."""
    return max(0.0, frame_interval_s(window_visible) - max(0.0, frame_elapsed_s))
