"""
Keeps the test run away from the owner's machine state.

Importing this module, which every test module does near the top:
- points src.config.CONFIG_FILE at a temporary file, so load_config() and save_config() never write the tracked
  repo-root config.json. The bytes of that file are kept in SNAPSHOT for tests/test_zz_config_guard.py.
- points APPDATA at a temporary directory and closes the shared getsu.log handler if one was already opened, so no
  test writes the real %APPDATA%\\Getsu\\getsu.log. tests/test_isolation_guard.py checks both.
- replaces the sounddevice entry points that reach PortAudio, and the COM object in src.win_audio, with stand-ins
  that raise RealCallRefused. A test that needs them patches them itself (for example patch("src.devices.sd"));
  its patch replaces the stand-in for the length of the test.
"""
import atexit
import logging
import os
import shutil
import tempfile
from unittest.mock import patch

import sounddevice as sd

import src.config as _cfg
import src.diagnostics as _diag
import src.win_audio as _win_audio

REPO_CONFIG = os.path.abspath(os.path.join(os.path.dirname(_cfg.__file__), "..", "config.json"))

with open(REPO_CONFIG, "rb") as _f:
    SNAPSHOT = _f.read()

TEMP_DIR = tempfile.mkdtemp(prefix="getsu-tests-")
TEMP_CONFIG = os.path.join(TEMP_DIR, "config.json")
TEMP_APPDATA = os.path.join(TEMP_DIR, "appdata")
os.makedirs(TEMP_APPDATA, exist_ok=True)

REFUSED_SD_ENTRY_POINTS = ("query_devices", "query_hostapis", "_initialize", "_terminate", "play", "stop",
                           "InputStream", "OutputStream", "RawStream", "Stream")


class RealCallRefused(RuntimeError):
    """Raised when a test reaches a real PortAudio or COM entry point without patching it."""


def _refuse(name):
    def _stand_in(*args, **kwargs):
        raise RealCallRefused(f"tests must not call the real {name}; patch it in the test")
    _stand_in.__name__ = name
    return _stand_in


class _RefusingCom:
    """Stands in for src.win_audio.ole32 (ctypes.windll): every attribute is a call that raises."""

    def __getattr__(self, name):
        return _refuse(f"COM {name}")


_patchers = [patch.object(_cfg, "CONFIG_FILE", TEMP_CONFIG), patch.object(_win_audio, "ole32", _RefusingCom())]
for _name in REFUSED_SD_ENTRY_POINTS:
    if hasattr(sd, _name):
        _patchers.append(patch.object(sd, _name, _refuse(f"sounddevice.{_name}")))
for _p in _patchers:
    _p.start()

_saved_appdata = os.environ.get("APPDATA")
REAL_LOG_PATH = os.path.join(_saved_appdata or os.path.expanduser("~"), "Getsu", "getsu.log")
REAL_LOG_SIZE_BEFORE = os.path.getsize(REAL_LOG_PATH) if os.path.exists(REAL_LOG_PATH) else None
os.environ["APPDATA"] = TEMP_APPDATA


def _move_file_handler(to_new_file: bool):
    """
    Replaces the shared getsu.log handler with one that writes under TEMP_APPDATA, and re-attaches every logger
    that held the old one (src.router and others keep the handler object they were given at import time).
    to_new_file False only closes the temp handler, at exit.
    """
    with _diag._handler_lock:
        old = _diag._file_handler
        _diag._file_handler = None
    if old is None:
        return
    new = _diag.get_file_handler() if to_new_file else None
    for logger in [logging.getLogger()] + [lg for lg in logging.Logger.manager.loggerDict.values()
                                           if isinstance(lg, logging.Logger)]:
        if old in logger.handlers:
            logger.removeHandler(old)
            if new is not None:
                logger.addHandler(new)
    try:
        old.close()
    except Exception:
        pass


_move_file_handler(to_new_file=True)


def _cleanup():
    _move_file_handler(to_new_file=False)
    for _p in reversed(_patchers):
        _p.stop()
    if _saved_appdata is None:
        os.environ.pop("APPDATA", None)
    else:
        os.environ["APPDATA"] = _saved_appdata
    shutil.rmtree(TEMP_DIR, ignore_errors=True)


atexit.register(_cleanup)
