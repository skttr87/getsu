"""Guards that the test run never writes the tracked repo-root config.json.

The name sorts last, so unittest discover runs these checks after every other module.
"""
import os
import unittest

import sys as _sys
_sys.path.insert(0, os.path.dirname(__file__))
import _isolation  # noqa: E402,F401  (must be imported first: it isolates CONFIG_FILE)
import src.config as cfg
from src.config import DEFAULT_CONFIG, load_config, save_config


def _read_repo_config() -> bytes:
    with open(_isolation.REPO_CONFIG, "rb") as f:
        return f.read()


class TestConfigGuard(unittest.TestCase):

    def test_config_file_is_the_temp_copy_while_tests_run(self):
        self.assertEqual(os.path.normcase(cfg.CONFIG_FILE), os.path.normcase(_isolation.TEMP_CONFIG))
        self.assertNotEqual(os.path.normcase(cfg.CONFIG_FILE), os.path.normcase(_isolation.REPO_CONFIG))

    def test_representative_load_and_save_leave_tracked_file_alone(self):
        load_config()
        save_config(dict(DEFAULT_CONFIG))
        self.assertEqual(_read_repo_config(), _isolation.SNAPSHOT)

    def test_tracked_config_unchanged_after_the_suite(self):
        self.assertEqual(_read_repo_config(), _isolation.SNAPSHOT,
                         "a test changed the tracked repo-root config.json")


if __name__ == "__main__":
    unittest.main()
