"""Checks for the public README and the GitHub Pages site (docs/index.html).

Covers the text rules (no long dashes, no emoji), the self-hosted font and licence,
the outbound-request rule, the placeholders for release-time numbers, and that the
version rewrites in scripts/publish.py still hit each spot exactly once.
"""
import importlib.util
import os
import pathlib
import re
import shutil
import tempfile
import unittest
from unittest import mock

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
INDEX = os.path.join(REPO, "docs", "index.html")
README = os.path.join(REPO, "README.md")
SITEMAP = os.path.join(REPO, "docs", "sitemap.xml")
PUBLISH = os.path.join(REPO, "scripts", "publish.py")

# Same blocks the GUI text test uses, plus the Unicode dash characters the owner forbids.
FORBIDDEN_RANGES = (
    (0x2190, 0x21FF),    # arrows
    (0x25A0, 0x25FF),    # geometric shapes
    (0x2600, 0x27BF),    # miscellaneous symbols and dingbats
    (0x2B00, 0x2BFF),    # miscellaneous symbols and arrows
    (0x1F000, 0x1FAFF),  # emoji and pictographs
    (0xFE00, 0xFE0F),    # variation selectors (emoji presentation)
)
LONG_DASHES = ("—", "–")
DASH_ENTITIES = ("&mdash;", "&ndash;", "&#8212;", "&#8211;", "&#x2014;", "&#x2013;", "&#x2012;")

ALLOWED_HOSTS = {
    "skttr87.github.io",
    "github.com",
    "api.github.com",
    "raw.githubusercontent.com",
    "www.vb-cable.com",
    "www.instagram.com",
}


def _read(path):
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def _load_publish():
    spec = importlib.util.spec_from_file_location("publish_under_test", PUBLISH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class TextRulesTests(unittest.TestCase):

    def _check_file(self, path):
        text = _read(path)
        for ch in LONG_DASHES:
            self.assertNotIn(ch, text, f"long dash U+{ord(ch):04X} in {os.path.basename(path)}")
        for entity in DASH_ENTITIES:
            self.assertNotIn(entity, text, f"dash entity {entity} in {os.path.basename(path)}")
        found = []
        for line_no, line in enumerate(text.splitlines(), 1):
            for ch in line:
                code = ord(ch)
                if any(lo <= code <= hi for lo, hi in FORBIDDEN_RANGES):
                    found.append((line_no, hex(code)))
        self.assertEqual(found, [], f"pictographic symbols in {os.path.basename(path)}: {found}")

    def test_index_has_no_long_dashes_or_emoji(self):
        self._check_file(INDEX)

    def test_readme_has_no_long_dashes_or_emoji(self):
        self._check_file(README)

    def test_no_old_measurements_or_colours_left(self):
        banned = ["<1.2", "1.5 %", "1.5%", "~35", "35 MB", "pyrnnoise", "#238636", "#2EA043", "#58A6FF", "Google Fonts"]
        for path in (INDEX, README):
            text = _read(path)
            for item in banned:
                self.assertNotIn(item, text, f"{item!r} still in {os.path.basename(path)}")

    def test_release_measurements_are_filled_in(self):
        """v1.4.0: delay and installer size were measured (2026-11-26), so no placeholder may remain."""
        for path in (INDEX, README):
            text = _read(path)
            name = os.path.basename(path)
            self.assertNotIn("measured at release", text, name)
            self.assertIn("110 ms", text, name)
            self.assertIn("29 MB", text, name)


class ThirdPartyRequestTests(unittest.TestCase):

    def test_index_loads_nothing_from_third_party_hosts(self):
        text = _read(INDEX)
        # Attributes that cause a request on page load: src, href of link, url() in CSS.
        loads = re.findall(r'<(?:img|script|link|source|iframe)\b[^>]*?\b(?:src|href)="([^"]+)"', text)
        loads += re.findall(r"url\(\"?([^\")]+)\"?\)", text)
        for target in loads:
            if target.startswith(("http://", "https://")):
                host = re.match(r"https?://([^/]+)", target).group(1)
                self.assertIn(host, ALLOWED_HOSTS, f"page loads {target}")
            else:
                self.assertFalse(target.startswith("//"), f"protocol-relative load {target}")

    def test_index_fetches_only_github_api(self):
        text = _read(INDEX)
        urls = re.findall(r"fetch\('(https://[^']+)'", text)
        self.assertTrue(urls, "expected the release lookup to use fetch")
        for url in urls:
            self.assertTrue(url.startswith("https://api.github.com/repos/skttr87/getsu/"), url)

    def test_fonts_are_self_hosted_with_licence(self):
        text = _read(INDEX)
        self.assertIn('url("assets/fonts/Inter-Regular.ttf")', text)
        self.assertIn('url("assets/fonts/Inter-SemiBold.ttf")', text)
        for name in ("Inter-Regular.ttf", "Inter-SemiBold.ttf", "OFL.txt"):
            self.assertTrue(os.path.isfile(os.path.join(REPO, "docs", "assets", "fonts", name)), name)
        licence = _read(os.path.join(REPO, "docs", "assets", "fonts", "OFL.txt"))
        self.assertIn("SIL OPEN FONT LICENSE Version 1.1", licence)

    def test_referenced_local_assets_exist(self):
        text = _read(INDEX)
        for ref in re.findall(r'(?:src|href)="(assets/[^"]+)"', text):
            self.assertTrue(os.path.isfile(os.path.join(REPO, "docs", ref)), ref)


class PalettePlacementTests(unittest.TestCase):

    def test_palette_matches_app_theme(self):
        text = _read(INDEX).upper()
        for colour in ("#1C1C1E", "#2C2C2E", "#3A3A3C", "#F5F5F7", "#A1A1A6", "#6E6E73", "#0A84FF", "#2F96FF", "#FF453A"):
            self.assertIn(colour, text)


class PublishRegexTests(unittest.TestCase):
    """Runs scripts/publish.py version rewrites on temporary copies and checks each spot changes once."""

    VERSION = "9.9.9"

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="getsu-docs-test-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        os.makedirs(os.path.join(self.tmp, "docs"))
        shutil.copy(INDEX, os.path.join(self.tmp, "docs", "index.html"))
        shutil.copy(SITEMAP, os.path.join(self.tmp, "docs", "sitemap.xml"))
        shutil.copy(README, os.path.join(self.tmp, "README.md"))
        self.publish = _load_publish()
        t = self.tmp
        patches = {
            "REPO_ROOT": t,
            "CHANGELOG_MD": os.path.join(t, "CHANGELOG.md"),  # README.md is looked up next to it
            "DOCS_INDEX": os.path.join(t, "docs", "index.html"),
            "DOCS_SITEMAP": os.path.join(t, "docs", "sitemap.xml"),
            # Files that publish.py also edits but that are not part of this test: absent in tmp, so skipped.
            "CONFIG_PY": os.path.join(t, "absent_config.py"),
            "CONFIG_JSON": os.path.join(t, "absent_config.json"),
            "SETUP_ISS": os.path.join(t, "absent_setup.iss"),
            "VERSION_INFO_TXT": os.path.join(t, "absent_version_info.txt"),
            "VERSION_INFO_CLI_TXT": os.path.join(t, "absent_version_info_cli.txt"),
        }
        for name, value in patches.items():
            p = mock.patch.object(self.publish, name, pathlib.Path(value))
            p.start()
            self.addCleanup(p.stop)

    def test_regexes_rewrite_each_index_spot_exactly_once(self):
        self.publish.sync_codebase_versions(self.VERSION)
        text = _read(os.path.join(self.tmp, "docs", "index.html"))
        v = self.VERSION
        spots = {
            "softwareVersion": f'"softwareVersion": "{v}"',
            "schema downloadUrl": f'"downloadUrl": "https://github.com/skttr87/getsu/releases/download/v{v}/Getsu-v{v}-Setup.exe"',
            "release badge": f'<div class="tagline-badge" id="release-badge">v{v} • Windows',
            "CTA href": f'href="https://github.com/skttr87/getsu/releases/download/v{v}/Getsu-v{v}-Setup.exe" class="btn-download"',
            "CTA text": f'<span id="download-text">Download Getsu v{v} (Setup.exe)</span>',
        }
        for name, needle in spots.items():
            self.assertEqual(text.count(needle), 1, f"{name} should be rewritten exactly once")
        self.assertEqual(text.count(v), 7, "unexpected extra version strings in index.html")

    def test_regexes_rewrite_readme_badges_exactly_once(self):
        self.publish.sync_codebase_versions(self.VERSION)
        text = _read(os.path.join(self.tmp, "README.md"))
        self.assertEqual(text.count(f"https://img.shields.io/badge/Release-v{self.VERSION}-"), 1)
        # The Download Latest Installer badge was removed (2026-11-27, it sat misaligned in Quick start); the release
        # badge is the only versioned badge.
        self.assertNotIn("Download%20Latest%20Installer", text)

    def test_sitemap_lastmod_is_rewritten(self):
        self.publish.sync_codebase_versions(self.VERSION)
        text = _read(os.path.join(self.tmp, "docs", "sitemap.xml"))
        self.assertRegex(text, r"<lastmod>\d{4}-\d{2}-\d{2}</lastmod>")
        self.assertEqual(len(re.findall(r"<lastmod>", text)), 1)


if __name__ == "__main__":
    unittest.main()
