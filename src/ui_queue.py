"""
Hand-off of DearPyGui work to the UI thread.

DearPyGui items may only be touched from the thread that runs the render loop. Worker threads (the watchdog,
start and stop, the voice test, the driver install, the hotplug timers, the cable guard) call the methods marked
with @ui_only: on the UI thread such a method runs at once; on any other thread its call is queued, and the render
loop runs the queue once per frame (at most MAX_CALLS_PER_FRAME calls, so one frame never stalls on a backlog).
"""
import collections
import functools
import threading
from typing import Any, Callable, Deque, Tuple

MAX_CALLS_PER_FRAME = 64


class UiQueue:
    """Thread-safe FIFO of calls for the UI thread. close() drops pending calls and refuses new ones."""

    def __init__(self):
        self._items: Deque[Tuple[Callable[..., Any], tuple, dict]] = collections.deque()
        self._lock = threading.Lock()
        self._closed = False

    def post(self, fn: Callable[..., Any], *args, **kwargs) -> bool:
        """Adds a call. False when the queue is closed (the call is dropped)."""
        with self._lock:
            if self._closed:
                return False
            self._items.append((fn, args, kwargs))
            return True

    def drain(self, limit: int = MAX_CALLS_PER_FRAME) -> int:
        """Runs up to limit queued calls on the calling thread. A failing call is reported and the rest still run."""
        done = 0
        while done < limit:
            with self._lock:
                if not self._items:
                    break
                fn, args, kwargs = self._items.popleft()
            try:
                fn(*args, **kwargs)
            except Exception as e:
                print(f"[UI] queued call {getattr(fn, '__name__', fn)} failed: {e}")
            done += 1
        return done

    def pending(self) -> int:
        with self._lock:
            return len(self._items)

    def close(self) -> None:
        with self._lock:
            self._closed = True
            self._items.clear()


def ui_only(method):
    """
    Marks a method that touches DearPyGui. Called on the UI thread it runs at once; called from another thread it
    is queued on self._ui_queue and returns None. The host object provides _on_ui_thread() and _ui_queue.
    """
    @functools.wraps(method)
    def wrapper(self, *args, **kwargs):
        queue = getattr(self, "_ui_queue", None)
        if queue is None or self._on_ui_thread():
            return method(self, *args, **kwargs)
        queue.post(method, self, *args, **kwargs)
        return None

    wrapper.__ui_only__ = True
    return wrapper
