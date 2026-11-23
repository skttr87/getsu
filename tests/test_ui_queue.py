"""Unit tests for the UI task queue (src/ui_queue.py): thread routing, the per-frame cap, failures and closing."""
import threading
import unittest

import _isolation  # noqa: F401

from src.ui_queue import MAX_CALLS_PER_FRAME, UiQueue, ui_only


class _Host:
    """A minimal host with the two members ui_only relies on."""

    def __init__(self):
        self._ui_thread = threading.current_thread()
        self._ui_queue = UiQueue()
        self.calls = []

    def _on_ui_thread(self):
        return self._ui_thread is None or threading.current_thread() is self._ui_thread

    @ui_only
    def touch(self, value, flag=None):
        self.calls.append((threading.current_thread(), value, flag))
        return "result"


def _in_thread(fn):
    """Runs fn on a new thread and returns what it returned (or re-raises its error)."""
    box = {}

    def runner():
        try:
            box["value"] = fn()
        except BaseException as e:  # pragma: no cover - reported to the test
            box["error"] = e

    thread = threading.Thread(target=runner)
    thread.start()
    thread.join(5.0)
    if "error" in box:
        raise box["error"]
    return box.get("value")


class UiOnlyRoutingTests(unittest.TestCase):
    def test_on_the_ui_thread_the_call_runs_at_once(self):
        host = _Host()
        self.assertEqual(host.touch(1, flag="a"), "result")
        self.assertEqual(host.calls, [(threading.current_thread(), 1, "a")])
        self.assertEqual(host._ui_queue.pending(), 0)

    def test_from_a_worker_the_call_is_queued_and_returns_none(self):
        host = _Host()
        result = _in_thread(lambda: host.touch(2))
        self.assertIsNone(result)
        self.assertEqual(host.calls, [])
        self.assertEqual(host._ui_queue.pending(), 1)

    def test_queued_call_runs_on_the_ui_thread_when_drained(self):
        host = _Host()
        _in_thread(lambda: host.touch(3, flag="b"))
        self.assertEqual(host._ui_queue.drain(), 1)
        self.assertEqual(host.calls, [(threading.current_thread(), 3, "b")])

    def test_calls_keep_their_order(self):
        host = _Host()
        _in_thread(lambda: [host.touch(i) for i in range(5)])
        host._ui_queue.drain()
        self.assertEqual([c[1] for c in host.calls], [0, 1, 2, 3, 4])

    def test_before_the_loop_exists_every_thread_runs_the_call(self):
        host = _Host()
        host._ui_thread = None  # no render loop yet (build_ui not run)
        _in_thread(lambda: host.touch(4))
        self.assertEqual(host._ui_queue.pending(), 0)
        self.assertEqual(len(host.calls), 1)

    def test_a_host_without_a_queue_runs_the_call(self):
        class Bare:
            calls = []

            @ui_only
            def go(self):
                self.calls.append(1)

        bare = Bare()
        _in_thread(lambda: bare.go())
        self.assertEqual(bare.calls, [1])


class DrainTests(unittest.TestCase):
    def test_drain_runs_at_most_the_cap_per_frame(self):
        host = _Host()
        total = MAX_CALLS_PER_FRAME + 10
        _in_thread(lambda: [host.touch(i) for i in range(total)])
        self.assertEqual(host._ui_queue.drain(), MAX_CALLS_PER_FRAME)
        self.assertEqual(host._ui_queue.pending(), 10)
        self.assertEqual(host._ui_queue.drain(), 10)
        self.assertEqual(len(host.calls), total)

    def test_a_failing_call_does_not_stop_the_rest(self):
        queue = UiQueue()
        ran = []
        queue.post(lambda: 1 / 0)
        queue.post(ran.append, "after")
        self.assertEqual(queue.drain(), 2)
        self.assertEqual(ran, ["after"])

    def test_closed_queue_drops_pending_and_new_calls(self):
        queue = UiQueue()
        ran = []
        queue.post(ran.append, 1)
        queue.close()
        self.assertFalse(queue.post(ran.append, 2))
        self.assertEqual(queue.drain(), 0)
        self.assertEqual(ran, [])


if __name__ == "__main__":
    unittest.main()
