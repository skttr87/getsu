"""Release checks: the tracked config.json template matches DEFAULT_CONFIG, and the installer cleans up on upgrade
and uninstall."""
import json
import os
import sys
import unittest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.dirname(__file__))
import _isolation  # noqa: E402,F401  (keeps config writes off the tracked config.json)

from src.config import DEFAULT_CONFIG  # noqa: E402


class TrackedConfigTemplate(unittest.TestCase):
    """The installer copies config.json when the install folder is writable, so it must hold every current key."""

    def test_template_has_every_default_key_with_the_default_value(self):
        with open(os.path.join(ROOT, "config.json"), encoding="utf-8") as f:
            template = json.load(f)
        self.assertEqual(set(template), set(DEFAULT_CONFIG))
        for key, value in DEFAULT_CONFIG.items():
            if key == "version":
                continue  # publish.py syncs it at release
            self.assertEqual(template[key], value, key)


class InstallerCleanup(unittest.TestCase):
    def setUp(self):
        with open(os.path.join(ROOT, "setup.iss"), encoding="utf-8") as f:
            self.iss = f.read()

    def test_upgrade_removes_the_previous_runtime(self):
        self.assertIn('Type: filesandordirs; Name: "{app}\\_internal"', self.iss)

    def test_uninstall_removes_the_log_so_the_appdata_folder_goes(self):
        self.assertIn("{userappdata}\\Getsu\\getsu.log*", self.iss)

    def test_installer_sets_the_cable_format_after_installing_vb_cable(self):
        self.assertIn("'--set-cable-format'", self.iss)

    def test_upgrade_hides_the_cable_again(self):
        """PrepareToInstall force-closes Getsu, so the upgrade path hides the cable itself."""
        upgrade = self.iss[self.iss.index("If already installed, ensure physical microphone remains default"):]
        self.assertIn("'--cable-visibility hide'", upgrade[:upgrade.index("[Run]")])


class AudioRestoreHelper(unittest.TestCase):
    def setUp(self):
        with open(os.path.join(ROOT, "drivers", "vbcable", "AudioRestore.cs"), encoding="utf-8") as f:
            self.cs = f.read()

    def test_check_vbcable_counts_a_hidden_cable_as_installed(self):
        """Getsu hides its cable while closed; the installer and uninstaller must still see the driver."""
        check = self.cs[self.cs.index('"--check-vbcable"'):self.cs.index('"--backup"')]
        self.assertIn("1 | 2 /* DEVICE_STATE_ACTIVE | DEVICE_STATE_DISABLED */", check)

    def test_no_brand_names_in_device_choice(self):
        for brand in ("mpow", "hyperx", "razer", "logitech", "corsair", "steelseries", "yeti", "fifine", "elgato",
                      "rode", "sennheiser", "audio-technica"):
            self.assertNotIn(f'"{brand}"', self.cs.lower(), brand)

    def test_exe_is_newer_than_source(self):
        folder = os.path.join(ROOT, "drivers", "vbcable")
        self.assertGreaterEqual(os.path.getmtime(os.path.join(folder, "AudioRestore.exe")),
                                os.path.getmtime(os.path.join(folder, "AudioRestore.cs")))


if __name__ == "__main__":
    unittest.main()
