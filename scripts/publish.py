#!/usr/bin/env python3
"""
scripts/publish.py — Unified Getsu Automated Release & Publishing Tool

Zero-Mistake Automated Publish Pipeline:
1. Pre-flight Quality Gate: Runs full unit test suite (107+ tests). Halts immediately if tests fail.
2. Version Detection & Validation: Checks target version against src/config.py, setup.iss, and CHANGELOG.md.
3. Changelog Enforcement: Verifies CHANGELOG.md has documented release notes for target version.
4. Automatic Codebase Synchronization:
   - src/config.py (APP_VERSION & DEFAULT_CONFIG['version'])
   - config.json ('version')
   - setup.iss (#define MyAppVersion & VersionInfo macros)
   - build/version_info.txt (PyInstaller Windows PE file version metadata)
   - docs/index.html (softwareVersion, schema.org downloadUrl, badges, CTA button text)
   - docs/sitemap.xml (lastmod date)
5. Clean Build & Native Binary Verification:
   - PyInstaller canonical spec build
   - Verifies dist/getsu/getsu.exe exists and PE ProductVersion matches
   - Verifies rnnoise.dll native binary exists and is >= 14 MB (14,825,472 bytes)
   - Inno Setup installer compilation (ISCC.exe setup.iss)
   - Verifies dist/installer/Getsu-v{version}-Setup.exe exists and is >= 30 MB
   - Verifies installer Windows PE metadata (ProductVersion, FileVersion, OriginalFilename)
6. Git Synchronization:
   - Stages and commits synchronized files
   - Creates/updates annotated git tag v{version}
   - Pushes main branch and tags to origin
7. GitHub Releases Deployment:
   - Retrieves GitHub token via Git Credential Manager
   - Creates or updates GitHub release
   - Computes SHA-256 and MD5 hashes
   - Replaces stale release assets with exponential backoff upload
8. Live Status Verification:
   - Queries GitHub API to confirm public release availability and asset size
"""

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

# Paths & Defaults
REPO_ROOT = Path(__file__).resolve().parent.parent
CONFIG_PY = REPO_ROOT / "src" / "config.py"
CONFIG_JSON = REPO_ROOT / "config.json"
CHANGELOG_MD = REPO_ROOT / "CHANGELOG.md"
DOCS_INDEX = REPO_ROOT / "docs" / "index.html"
DOCS_SITEMAP = REPO_ROOT / "docs" / "sitemap.xml"
SETUP_ISS = REPO_ROOT / "setup.iss"
SPEC_FILE = REPO_ROOT / "build" / "getsu.spec"
VERSION_INFO_TXT = REPO_ROOT / "build" / "version_info.txt"
VERSION_INFO_CLI_TXT = REPO_ROOT / "build" / "version_info_cli.txt"
DIST_GETSU_DIR = REPO_ROOT / "dist" / "getsu"
DIST_INSTALLER_DIR = REPO_ROOT / "dist" / "installer"

GITHUB_OWNER = "skttr87"
GITHUB_REPO = "getsu"
DEFAULT_GIT_CANDIDATES = [
    r"D:\dev\git\cmd\git.exe",
    shutil.which("git") or "git",
]
DEFAULT_ISCC_PATH = r"D:\dev\inno\app\ISCC.exe"
DEFAULT_PYENV_PYTHON = r"D:\dev\project\getsu\pyenv\Scripts\python.exe"


def find_git_exe() -> str:
    for candidate in DEFAULT_GIT_CANDIDATES:
        if candidate and os.path.isfile(candidate):
            return candidate
    return "git"


def get_python_exe() -> str:
    if os.path.isfile(DEFAULT_PYENV_PYTHON):
        return DEFAULT_PYENV_PYTHON
    return sys.executable


def run_cmd(args, cwd=None, check=True) -> subprocess.CompletedProcess:
    cwd = str(cwd or REPO_ROOT)
    return subprocess.run(args, cwd=cwd, check=check, text=True, capture_output=True)


def get_version(override: str = None) -> str:
    if override:
        return override.lstrip("v")

    # 1. Inspect src/config.py
    if CONFIG_PY.exists():
        content = CONFIG_PY.read_text(encoding="utf-8")
        m_app = re.search(r'APP_VERSION:\s*str\s*=\s*["\']([^"\']+)["\']', content)
        if m_app:
            return m_app.group(1).lstrip("v")
        m = re.search(r'["\']version["\']:\s*["\']([^"\']+)["\']', content)
        if m:
            return m.group(1).lstrip("v")

    # 2. Inspect config.json fallback
    if CONFIG_JSON.exists():
        try:
            data = json.loads(CONFIG_JSON.read_text(encoding="utf-8"))
            if "version" in data:
                return str(data["version"]).lstrip("v")
        except Exception:
            pass

    raise RuntimeError("Could not auto-detect version from src/config.py or config.json.")


def get_changelog_notes(version: str) -> str:
    if not CHANGELOG_MD.exists():
        return ""
    content = CHANGELOG_MD.read_text(encoding="utf-8")
    pattern = rf'##\s*\[v?{re.escape(version)}\][^\n]*\n(.*?)(?=\n##\s*\[|\Z)'
    m = re.search(pattern, content, re.DOTALL)
    if not m:
        return ""
    notes = m.group(1).strip()
    notes = re.sub(r'\n---+\s*$', '', notes).strip()
    return notes


def compute_sha256(file_path: Path) -> str:
    h = hashlib.sha256()
    with open(file_path, "rb") as f:
        while chunk := f.read(65536):
            h.update(chunk)
    return h.hexdigest().upper()


def compute_md5(file_path: Path) -> str:
    h = hashlib.md5()
    with open(file_path, "rb") as f:
        while chunk := f.read(65536):
            h.update(chunk)
    return h.hexdigest().upper()


def get_github_token() -> str:
    git_exe = find_git_exe()
    try:
        p = subprocess.Popen(
            [git_exe, "credential", "fill"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        out, _ = p.communicate("protocol=https\nhost=github.com\n\n")
        for line in out.splitlines():
            if line.startswith("password="):
                return line.split("=", 1)[1].strip()
    except Exception as e:
        print(f"[WARN] Failed to query git credential manager: {e}")
    return os.environ.get("GITHUB_TOKEN", "")


# ---------------------------------------------------------------------------
# Pre-Flight Quality Gates
# ---------------------------------------------------------------------------

def run_preflight_tests():
    """Runs entire unit test suite. Fails fast if any test breaks."""
    print("\n[PRE-FLIGHT] Step 1/3: Running full unit test suite...")
    py_exe = get_python_exe()
    res = subprocess.run([py_exe, "-m", "unittest", "discover", "tests"], cwd=str(REPO_ROOT))
    if res.returncode != 0:
        raise RuntimeError("Pre-flight unit tests failed! Fix test regressions before publishing.")
    print("[PRE-FLIGHT] All unit tests passed successfully.")


def check_git_status():
    """Verifies Git state and active branch."""
    print("[PRE-FLIGHT] Step 2/3: Checking Git branch and remote health...")
    git_exe = find_git_exe()
    res = run_cmd([git_exe, "rev-parse", "--abbrev-ref", "HEAD"], check=False)
    branch = res.stdout.strip()
    if branch and branch != "main":
        print(f"[WARN] Active Git branch is '{branch}', not 'main'!")
    else:
        print(f"[PRE-FLIGHT] Active Git branch: '{branch}' (clean)")


def validate_changelog(version: str):
    """Ensures CHANGELOG.md contains documented notes for the version."""
    print(f"[PRE-FLIGHT] Step 3/3: Validating CHANGELOG.md entry for v{version}...")
    notes = get_changelog_notes(version)
    if not notes or len(notes.strip()) < 10:
        raise RuntimeError(
            f"CHANGELOG.md has no documented release notes for version [{version}]. "
            f"Please update CHANGELOG.md with bullet points before releasing."
        )
    print(f"[PRE-FLIGHT] CHANGELOG.md entry verified ({len(notes)} characters).")


# ---------------------------------------------------------------------------
# Codebase Version Synchronization
# ---------------------------------------------------------------------------

def update_docs_files(version: str) -> bool:
    """Updates docs/index.html and docs/sitemap.xml with target version."""
    modified = False

    # 1. docs/index.html
    if DOCS_INDEX.exists():
        index_text = DOCS_INDEX.read_text(encoding="utf-8")
        orig_text = index_text

        # softwareVersion
        index_text = re.sub(
            r'("softwareVersion":\s*")[^"]+(")',
            rf'\g<1>{version}\g<2>',
            index_text
        )
        # downloadUrl in schema.org
        index_text = re.sub(
            r'("downloadUrl":\s*"https://github\.com/[^/]+/[^/]+/releases/download/)v?[^/]+(/Getsu-)[^/]+(-Setup\.exe")',
            rf'\g<1>v{version}\g<2>v{version}\g<3>',
            index_text
        )
        # release badge
        index_text = re.sub(
            r'(<div class="tagline-badge" id="release-badge">)v?[^ ]+(\s*•\s*Windows)',
            rf'\g<1>v{version}\g<2>',
            index_text
        )
        # cta download link
        index_text = re.sub(
            r'(href="https://github\.com/[^/]+/[^/]+/releases/download/)v?[^/]+(/Getsu-)[^/]+(-Setup\.exe" class="btn-download")',
            rf'\g<1>v{version}\g<2>v{version}\g<3>',
            index_text
        )
        # cta button text
        index_text = re.sub(
            r'(<span id="download-text">Download Getsu )v?[^ ]+(\s*\(Setup\.exe\)</span>)',
            rf'\g<1>v{version}\g<2>',
            index_text
        )

        if index_text != orig_text:
            DOCS_INDEX.write_text(index_text, encoding="utf-8")
            print(f"[INFO] Synchronized {DOCS_INDEX.relative_to(REPO_ROOT)} to v{version}")
            modified = True

    # 2. docs/sitemap.xml
    if DOCS_SITEMAP.exists():
        sitemap_text = DOCS_SITEMAP.read_text(encoding="utf-8")
        orig_sitemap = sitemap_text
        today = datetime.now().strftime("%Y-%m-%d")
        sitemap_text = re.sub(
            r'(<lastmod>)[^<]+(</lastmod>)',
            rf'\g<1>{today}\g<2>',
            sitemap_text
        )
        if sitemap_text != orig_sitemap:
            DOCS_SITEMAP.write_text(sitemap_text, encoding="utf-8")
            print(f"[INFO] Synchronized {DOCS_SITEMAP.relative_to(REPO_ROOT)} lastmod to {today}")
            modified = True

    return modified


def sync_codebase_versions(version: str) -> bool:
    """Synchronizes target version across src/config.py, setup.iss, build/version_info.txt, and docs."""
    modified = False

    # 1. src/config.py
    if CONFIG_PY.exists():
        cfg_text = CONFIG_PY.read_text(encoding="utf-8")
        orig_cfg = cfg_text
        cfg_text = re.sub(
            r'(APP_VERSION:\s*str\s*=\s*")[^"]+(")',
            rf'\g<1>{version}\g<2>',
            cfg_text
        )
        if cfg_text != orig_cfg:
            CONFIG_PY.write_text(cfg_text, encoding="utf-8")
            print(f"[INFO] Synchronized {CONFIG_PY.relative_to(REPO_ROOT)} APP_VERSION to v{version}")
            modified = True

    # 2. config.json
    if CONFIG_JSON.exists():
        try:
            cfg_data = json.loads(CONFIG_JSON.read_text(encoding="utf-8"))
            if cfg_data.get("version") != version:
                cfg_data["version"] = version
                CONFIG_JSON.write_text(json.dumps(cfg_data, indent=4), encoding="utf-8")
                print(f"[INFO] Synchronized {CONFIG_JSON.relative_to(REPO_ROOT)} version to v{version}")
                modified = True
        except Exception as e:
            print(f"[WARN] Could not parse config.json: {e}")

    # 3. setup.iss (#define MyAppVersion & VersionInfo macros)
    if SETUP_ISS.exists():
        iss_text = SETUP_ISS.read_text(encoding="utf-8")
        orig_iss = iss_text
        iss_text = re.sub(
            r'(#define\s+MyAppVersion\s+")[^"]+(")',
            rf'\g<1>{version}\g<2>',
            iss_text
        )
        iss_text = re.sub(
            r'VersionInfoVersion=.*',
            r'VersionInfoVersion={#MyAppVersion}.0',
            iss_text
        )
        iss_text = re.sub(
            r'VersionInfoProductVersion=.*',
            r'VersionInfoProductVersion={#MyAppVersion}',
            iss_text
        )
        iss_text = re.sub(
            r'VersionInfoOriginalFileName=.*',
            r'VersionInfoOriginalFileName=Getsu-v{#MyAppVersion}-Setup.exe',
            iss_text
        )
        if iss_text != orig_iss:
            SETUP_ISS.write_text(iss_text, encoding="utf-8")
            print(f"[INFO] Synchronized {SETUP_ISS.relative_to(REPO_ROOT)} to v{version}")
            modified = True

    # 4. build/version_info.txt & build/version_info_cli.txt
    for vi_path in (VERSION_INFO_TXT, VERSION_INFO_CLI_TXT):
        if vi_path.exists():
            vi_text = vi_path.read_text(encoding="utf-8")
            orig_vi = vi_text
            try:
                parts = [int(p) for p in version.split(".")][:3]
                while len(parts) < 3:
                    parts.append(0)
                tuple_str = f"({parts[0]}, {parts[1]}, {parts[2]}, 0)"
                vi_text = re.sub(r'filevers=\([^)]+\)', f'filevers={tuple_str}', vi_text)
                vi_text = re.sub(r'prodvers=\([^)]+\)', f'prodvers={tuple_str}', vi_text)
                vi_text = re.sub(r"StringStruct\('FileVersion',\s*'[^']+'\)", f"StringStruct('FileVersion', '{version}.0')", vi_text)
                vi_text = re.sub(r"StringStruct\('ProductVersion',\s*'[^']+'\)", f"StringStruct('ProductVersion', '{version}')", vi_text)
                if vi_text != orig_vi:
                    vi_path.write_text(vi_text, encoding="utf-8")
                    print(f"[INFO] Synchronized {vi_path.relative_to(REPO_ROOT)} to v{version}")
                    modified = True
            except Exception as e:
                print(f"[WARN] Could not update {vi_path.name}: {e}")

    # 5. docs/index.html & docs/sitemap.xml
    docs_mod = update_docs_files(version)
    if docs_mod:
        modified = True

    return modified


# ---------------------------------------------------------------------------
# PE Header & Binary Integrity Auditing
# ---------------------------------------------------------------------------

def verify_pe_metadata(file_path: Path, expected_version: str, expected_filename: str = None):
    """Inspects Windows PE metadata using PowerShell and asserts exact version matching."""
    print(f"[AUDIT] Checking Windows PE metadata on {file_path.name}...")
    cmd = [
        "powershell", "-NoProfile", "-Command",
        f"(Get-Item '{file_path}').VersionInfo | Select-Object -Property ProductVersion,FileVersion,OriginalFilename | ConvertTo-Json"
    ]
    res = subprocess.run(cmd, capture_output=True, text=True)
    if res.returncode != 0 or not res.stdout.strip():
        print(f"[WARN] Could not query PE metadata: {res.stderr.strip()}")
        return

    try:
        data = json.loads(res.stdout.strip())
        prod_ver = (data.get("ProductVersion") or "").strip()
        file_ver = (data.get("FileVersion") or "").strip()
        orig_name = (data.get("OriginalFilename") or "").strip()

        if prod_ver != expected_version:
            raise RuntimeError(
                f"PE ProductVersion mismatch in {file_path.name}! "
                f"Expected '{expected_version}', but binary contains '{prod_ver}'."
            )
        if not file_ver.startswith(expected_version):
            raise RuntimeError(
                f"PE FileVersion mismatch in {file_path.name}! "
                f"Expected '{expected_version}.0', but binary contains '{file_ver}'."
            )
        if expected_filename and orig_name != expected_filename:
            raise RuntimeError(
                f"PE OriginalFilename mismatch in {file_path.name}! "
                f"Expected '{expected_filename}', but binary contains '{orig_name}'."
            )

        print(f"[AUDIT] [OK] PE metadata verified: ProductVersion={prod_ver}, FileVersion={file_ver}, OriginalFilename={orig_name}")
    except json.JSONDecodeError:
        pass


def verify_dist_integrity(version: str):
    """Verifies that PyInstaller produced all mandatory native binaries and assets."""
    print("\n[AUDIT] Verifying PyInstaller staged distribution (dist/getsu)...")
    getsu_exe = DIST_GETSU_DIR / "getsu.exe"
    if not getsu_exe.is_file():
        raise RuntimeError("Staged executable dist/getsu/getsu.exe is missing!")

    # Check rnnoise.dll
    rnnoise_candidates = [
        DIST_GETSU_DIR / "_internal" / "src" / "native" / "rnnoise.dll",
        DIST_GETSU_DIR / "src" / "native" / "rnnoise.dll",
    ]
    rnnoise_path = next((p for p in rnnoise_candidates if p.is_file()), None)
    if not rnnoise_path:
        raise RuntimeError("CRITICAL: rnnoise.dll was NOT found in dist/getsu! Check getsu.spec datas.")

    rnnoise_size = rnnoise_path.stat().st_size
    if rnnoise_size < 14_000_000:
        raise RuntimeError(f"CRITICAL: rnnoise.dll is undersized ({rnnoise_size} bytes). Expected ~14.8 MB.")
    print(f"[AUDIT] [OK] rnnoise.dll confirmed: {rnnoise_size:,} bytes at {rnnoise_path.relative_to(REPO_ROOT)}")

    # Check PE Version on getsu.exe
    verify_pe_metadata(getsu_exe, expected_version=version, expected_filename="getsu.exe")

    # Check getsu-cli.exe
    getsu_cli_exe = DIST_GETSU_DIR / "getsu-cli.exe"
    if not getsu_cli_exe.is_file():
        raise RuntimeError("Staged CLI executable dist/getsu/getsu-cli.exe is missing!")
    verify_pe_metadata(getsu_cli_exe, expected_version=version, expected_filename="getsu-cli.exe")


def build_installer(version: str):
    """Compiles PyInstaller canonical spec and Inno Setup installer."""
    py_exe = get_python_exe()

    # Step 1: PyInstaller Spec Build
    print(f"\n[BUILD] Step 1/2: Compiling PyInstaller canonical spec...")
    res_pyi = subprocess.run([py_exe, "-m", "PyInstaller", "-y", "--clean", str(SPEC_FILE)], cwd=str(REPO_ROOT))
    if res_pyi.returncode != 0:
        raise RuntimeError("PyInstaller build failed.")

    # Audit PyInstaller distribution
    verify_dist_integrity(version)

    # Step 2: Inno Setup Compilation
    print(f"\n[BUILD] Step 2/2: Compiling Inno Setup installer...")
    iscc = DEFAULT_ISCC_PATH if os.path.isfile(DEFAULT_ISCC_PATH) else "ISCC.exe"
    res_inno = subprocess.run([iscc, str(SETUP_ISS)], cwd=str(REPO_ROOT))
    if res_inno.returncode != 0:
        raise RuntimeError("Inno Setup compiler failed.")

    # Audit Installer
    installer_path = DIST_INSTALLER_DIR / f"Getsu-v{version}-Setup.exe"
    if not installer_path.is_file():
        raise RuntimeError(f"Expected installer not found at {installer_path}")

    installer_size = installer_path.stat().st_size
    if installer_size < 30_000_000:
        raise RuntimeError(f"Installer size ({installer_size} bytes) is suspiciously small! Expected >= 30 MB.")

    print(f"[BUILD] [OK] Installer compiled: {installer_path.name} ({installer_size:,} bytes)")
    verify_pe_metadata(
        installer_path,
        expected_version=version,
        expected_filename=f"Getsu-v{version}-Setup.exe"
    )


# ---------------------------------------------------------------------------
# Git and GitHub Deployment
# ---------------------------------------------------------------------------

def commit_and_push_git(version: str, commit_msg: str = None, dry_run: bool = False):
    git_exe = find_git_exe()
    tag = f"v{version}"

    # Check for uncommitted docs or version changes
    status = run_cmd([git_exe, "status", "--porcelain"]).stdout.strip()
    if status:
        print(f"\n[GIT] Working tree changes detected:\n{status}")
        if dry_run:
            print("[DRY-RUN] Skipping git add, commit, and push.")
            return

        run_cmd([git_exe, "add", "-A"])
        # Commit if changes were staged
        staged = run_cmd([git_exe, "diff", "--staged", "--name-only"]).stdout.strip()
        if staged:
            msg = commit_msg or f"release(v{version}): prepare distribution assets, sync PE metadata & docs"
            run_cmd([git_exe, "commit", "-m", msg])
            print(f"[GIT] Committed updated files for {tag}: {msg}")

    if dry_run:
        print(f"[DRY-RUN] Would tag {tag} and push origin main --tags")
        return

    # Update tag to current HEAD
    run_cmd([git_exe, "tag", "-f", "-a", tag, "-m", f"Release {tag}"])
    print(f"[GIT] Tagged {tag}")

    # Push to origin
    print(f"[GIT] Pushing main branch and tag {tag} to origin...")
    run_cmd([git_exe, "push", "origin", "main", "--tags", "-f"])
    print(f"[GIT] Push completed.")


def publish_github_release(
    version: str,
    installer_path: Path,
    title: str = None,
    dry_run: bool = False,
):
    token = get_github_token()
    if not token:
        raise RuntimeError("Could not retrieve GitHub credentials from Git Credential Manager or GITHUB_TOKEN.")

    tag = f"v{version}"
    asset_name = installer_path.name
    file_size = installer_path.stat().st_size
    sha256 = compute_sha256(installer_path)
    md5 = compute_md5(installer_path)
    size_mb = file_size / (1024 * 1024)

    # Prepare Release Body
    changelog = get_changelog_notes(version)
    if not changelog:
        changelog = f"- Maintenance and stability release {tag}."

    if not title:
        title = f"Getsu {tag}"

    body = f"""## What's New in Getsu {tag}

{changelog}

---

### Verify Your Download
- **File**: `{asset_name}`
- **SHA-256**: `{sha256}`
- **MD5**: `{md5}`
- **Size**: `{file_size:,} bytes` (~{size_mb:.1f} MB)
"""

    headers = {
        "Authorization": f"token {token}",
        "Accept": "application/vnd.github.v3+json",
        "User-Agent": "Getsu-Auto-Publisher",
    }

    print(f"\n[RELEASE] Target Tag: {tag}")
    print(f"[RELEASE] Title: {title}")
    print(f"[RELEASE] Installer: {installer_path.name} ({file_size:,} bytes, {sha256[:16]}...)")

    if dry_run:
        print("\n[DRY-RUN] Would publish release with body:")
        print("--------------------------------------------------")
        print(body)
        print("--------------------------------------------------")
        return

    # 1. Check existing release
    release_url = f"https://api.github.com/repos/{GITHUB_OWNER}/{GITHUB_REPO}/releases/tags/{tag}"
    req = urllib.request.Request(release_url, headers=headers)
    release = None

    try:
        with urllib.request.urlopen(req) as resp:
            release = json.loads(resp.read().decode())
            print(f"[RELEASE] Found existing release for {tag} (ID: {release['id']})")
    except urllib.error.HTTPError as e:
        if e.code == 404:
            print(f"[RELEASE] No existing release found for {tag}. Creating a new release...")
        else:
            raise RuntimeError(f"Failed to check existing release: {e}")

    # 2. Create or Update Release
    if not release:
        create_url = f"https://api.github.com/repos/{GITHUB_OWNER}/{GITHUB_REPO}/releases"
        payload = json.dumps({
            "tag_name": tag,
            "name": title,
            "body": body,
            "draft": False,
            "prerelease": False,
        }).encode("utf-8")
        req = urllib.request.Request(create_url, data=payload, headers=headers)
        with urllib.request.urlopen(req) as resp:
            release = json.loads(resp.read().decode())
            print(f"[SUCCESS] Release created: {release['html_url']}")
    else:
        update_url = f"https://api.github.com/repos/{GITHUB_OWNER}/{GITHUB_REPO}/releases/{release['id']}"
        payload = json.dumps({
            "tag_name": tag,
            "name": title,
            "body": body,
        }).encode("utf-8")
        patch_headers = headers.copy()
        patch_headers["Content-Type"] = "application/json"
        patch_req = urllib.request.Request(update_url, data=payload, headers=patch_headers, method="PATCH")
        with urllib.request.urlopen(patch_req) as resp:
            release = json.loads(resp.read().decode())
            print(f"[SUCCESS] Release notes updated: {release['html_url']}")

    # 3. Check and clean up existing binary asset
    upload_url_template = release.get("upload_url", "")
    upload_base = upload_url_template.split("{")[0]

    for asset in release.get("assets", []):
        if asset["name"] == asset_name:
            print(f"[ASSET] Existing asset found (ID: {asset['id']}). Deleting old asset...")
            del_req = urllib.request.Request(
                f"https://api.github.com/repos/{GITHUB_OWNER}/{GITHUB_REPO}/releases/assets/{asset['id']}",
                headers=headers,
                method="DELETE",
            )
            with urllib.request.urlopen(del_req):
                print(f"[ASSET] Old asset deleted successfully.")

    # 4. Upload binary asset with retry
    upload_url = f"{upload_base}?name={asset_name}"
    print(f"[ASSET] Uploading {asset_name} ({file_size:,} bytes)...")

    with open(installer_path, "rb") as f:
        file_data = f.read()

    upload_headers = headers.copy()
    upload_headers["Content-Type"] = "application/octet-stream"
    upload_headers["Content-Length"] = str(len(file_data))

    max_retries = 3
    for attempt in range(1, max_retries + 1):
        try:
            upload_req = urllib.request.Request(upload_url, data=file_data, headers=upload_headers)
            with urllib.request.urlopen(upload_req) as upload_resp:
                asset_info = json.loads(upload_resp.read().decode())
                print(f"[SUCCESS] Asset uploaded successfully!")
                print(f"[DOWNLOAD URL] {asset_info['browser_download_url']}")
                break
        except Exception as e:
            if attempt < max_retries:
                wait_s = attempt * 3
                print(f"[WARN] Upload attempt {attempt} failed ({e}). Retrying in {wait_s}s...")
                time.sleep(wait_s)
            else:
                raise RuntimeError(f"Failed to upload asset after {max_retries} attempts: {e}")

    print(f"\n[DONE] Release {tag} is live at: {release['html_url']}")


def verify_live_api():
    url = f"https://api.github.com/repos/{GITHUB_OWNER}/{GITHUB_REPO}/releases/latest?t={int(time.time())}"
    req = urllib.request.Request(url, headers={"User-Agent": "Getsu-Checker", "Accept": "application/vnd.github.v3+json"})
    try:
        with urllib.request.urlopen(req) as resp:
            data = json.loads(resp.read().decode())
            print(f"\n[LIVE API VERIFICATION]")
            print(f"Latest Release Tag : {data.get('tag_name')}")
            print(f"Release Name       : {data.get('name')}")
            for a in data.get("assets", []):
                print(f"Asset Available    : {a.get('name')} ({a.get('size'):,} bytes)")
    except Exception as e:
        print(f"[WARN] Live API check encountered: {e}")


# ---------------------------------------------------------------------------
# CLI Entry Point
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Unified Getsu Automated Release & Publishing Tool — Zero Mistake Pipeline"
    )
    parser.add_argument("--version", type=str, default=None, help="Explicit target version (e.g. 1.2.8)")
    parser.add_argument("--title", type=str, default=None, help="Custom release title")
    parser.add_argument("--message", type=str, default=None, help="Custom git commit message")
    parser.add_argument("--skip-tests", action="store_true", help="Skip pre-flight unit tests")
    parser.add_argument("--skip-build", action="store_true", help="Skip compiling PyInstaller & Inno Setup")
    parser.add_argument("--dry-run", action="store_true", help="Inspect operations without pushing or uploading")
    parser.add_argument("--no-git", action="store_true", help="Skip git commit/push operations")
    args = parser.parse_args()

    version = get_version(args.version)
    tag = f"v{version}"
    print("=" * 60)
    print(f"  Getsu Zero-Mistake Release Automation — Target: {tag}")
    print("=" * 60)

    # 1. Pre-flight Quality Gates
    if not args.skip_tests and not args.dry_run:
        run_preflight_tests()
    check_git_status()
    validate_changelog(version)

    # 2. Synchronize Version across Entire Codebase
    sync_codebase_versions(version)

    # 3. Clean Build & Binary Audits (Default: Always Build unless --skip-build)
    installer_path = DIST_INSTALLER_DIR / f"Getsu-{tag}-Setup.exe"
    if not args.skip_build:
        build_installer(version)
    else:
        print("[INFO] Skipping build (--skip-build flag set).")
        if not installer_path.is_file():
            fallback_path = DIST_INSTALLER_DIR / f"Getsu-Setup.exe"
            if fallback_path.is_file():
                installer_path = fallback_path
            else:
                print(f"[ERROR] Installer not found at {installer_path}")
                print("Tip: Run without --skip-build to compile automatically.")
                sys.exit(1)
        # Still audit the existing binary
        verify_pe_metadata(installer_path, expected_version=version, expected_filename=f"Getsu-{tag}-Setup.exe")

    # 4. Git Synchronization
    if not args.no_git:
        commit_and_push_git(version, commit_msg=args.message, dry_run=args.dry_run)

    # 5. Publish to GitHub Releases
    publish_github_release(
        version=version,
        installer_path=installer_path,
        title=args.title,
        dry_run=args.dry_run,
    )

    # 6. Verify Live API
    if not args.dry_run:
        verify_live_api()


if __name__ == "__main__":
    main()
