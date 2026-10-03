"""
Getsu - Single Instance Manager (Micro-Checker).
Ensures only one instance of Getsu runs at any time.
If an instance is already running (visible or hidden in tray),
brings it to the foreground instantly (~3ms) and exits the new process
without loading heavy libraries (no audio, no UI, no neural model).
"""
import sys
import time
import ctypes
import atexit

MUTEX_NAME = "Local\\Getsu_SingleInstance_Mutex_8F9A"
WINDOW_TITLE = "Getsu - AI Noise Cancellation"
REGISTERED_MSG_NAME = "GETSU_RESTORE_WINDOW_MSG"

_app_mutex = None  # Global reference to prevent GC from closing handle


def activate_existing_instance(mutex_name: str = MUTEX_NAME) -> bool:
    """
    Checks if another instance of Getsu is already running.
    - If yes: Finds the existing window (even if minimized or hidden in tray),
      restores it, brings it to foreground, and returns True.
    - If no: Holds the mutex handle and returns False.
    """
    global _app_mutex

    if sys.platform != "win32":
        return False

    kernel32 = ctypes.windll.kernel32
    user32 = ctypes.windll.user32

    # Attempt to create or open named mutex
    _app_mutex = kernel32.CreateMutexW(None, False, mutex_name)
    last_error = kernel32.GetLastError()

    # ERROR_ALREADY_EXISTS = 183
    if last_error == 183:
        # Existing instance detected! Find the window by title.
        hwnd = user32.FindWindowW(None, WINDOW_TITLE)

        # In case the first instance was just launched a fraction of a second ago
        retries = 15
        while not hwnd and retries > 0:
            time.sleep(0.05)
            hwnd = user32.FindWindowW(None, WINDOW_TITLE)
            retries -= 1

        if hwnd:
            # 1. Post custom registered message so primary instance wndproc triggers restore
            try:
                msg_id = user32.RegisterWindowMessageW(REGISTERED_MSG_NAME)
                if msg_id:
                    user32.PostMessageW(hwnd, msg_id, 0, 0)
            except Exception:
                pass

            # 2. Win32 restore call: SW_RESTORE = 9 (restores minimized or hidden window)
            user32.ShowWindow(hwnd, 9)

            # 3. Bypass Windows foreground-lock (Anti-Focus-Stealing) via AttachThreadInput
            try:
                fore_hwnd = user32.GetForegroundWindow()
                fore_thread = user32.GetWindowThreadProcessId(fore_hwnd, None)
                cur_thread = kernel32.GetCurrentThreadId()

                if fore_thread and cur_thread and fore_thread != cur_thread:
                    user32.AttachThreadInput(cur_thread, fore_thread, True)
                    user32.BringWindowToTop(hwnd)
                    user32.SetForegroundWindow(hwnd)
                    user32.AttachThreadInput(cur_thread, fore_thread, False)
                else:
                    user32.BringWindowToTop(hwnd)
                    user32.SetForegroundWindow(hwnd)
            except Exception:
                user32.SetForegroundWindow(hwnd)
            return True

        # Mutex existed but no window was found (stale mutex from killed/crashed instance).
        # Close stale handle and create a fresh mutex to claim exclusive ownership cleanly.
        try:
            kernel32.CloseHandle(_app_mutex)
        except Exception:
            pass
        _app_mutex = kernel32.CreateMutexW(None, False, mutex_name)
        return False

    return False


def release_instance():
    """Explicitly closes and releases the named mutex handle on normal exit."""
    global _app_mutex
    if _app_mutex and sys.platform == "win32":
        try:
            ctypes.windll.kernel32.CloseHandle(_app_mutex)
        except Exception:
            pass
        _app_mutex = None


atexit.register(release_instance)
