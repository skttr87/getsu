"""
Keeps the test run away from the tracked repo-root config.json.

Importing this module points src.config.CONFIG_FILE at a temporary file for the rest of the process,
so load_config() and save_config() called by any test write to the temp directory only. The bytes of
the tracked file are recorded here so tests/test_zz_config_guard.py can assert they did not change.
"""
import atexit
import os
import shutil
import tempfile
from unittest.mock import patch

import src.config as _cfg

REPO_CONFIG = os.path.abspath(os.path.join(os.path.dirname(_cfg.__file__), "..", "config.json"))

with open(REPO_CONFIG, "rb") as _f:
    SNAPSHOT = _f.read()

TEMP_DIR = tempfile.mkdtemp(prefix="getsu-tests-")
TEMP_CONFIG = os.path.join(TEMP_DIR, "config.json")

_patcher = patch.object(_cfg, "CONFIG_FILE", TEMP_CONFIG)
_patcher.start()


def _cleanup():
    _patcher.stop()
    shutil.rmtree(TEMP_DIR, ignore_errors=True)


atexit.register(_cleanup)
