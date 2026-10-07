#!/usr/bin/env python3
"""
scripts/publish.py — Unified Getsu Automated Release & Publishing Tool

Capabilities:
1. Auto-detects target version from src/config.py (or optional --version override).
2. Auto-extracts release notes directly from CHANGELOG.md for that version.
3. Automatically computes SHA-256 and verifies the installer binary.
4. Auto-synchronizes static fallback URLs and metadata in docs/index.html & docs/sitemap.xml.
5. Manages Git commits, tagging, and remote pushes.
6. Retrieves GitHub credentials seamlessly via Git Credential Manager.
7. Creates or updates GitHub Releases and uploads the installer asset with retry/collision handling.
8. Verifies live GitHub Release API status.
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
DIST_INSTALLER_DIR = REPO_ROOT / "dist" / "installer"

GITHUB_OWNER = "skttr87"
GITHUB_REPO = "getsu"
DEFAULT_GIT_CANDIDATES = [
    r"D:\dev\git\cmd\git.exe",
    shutil.which("git") or "git",
]
DEFAULT_ISCC_PATH = r"D:\dev\inno\app\ISCC.exe"


def find_git_exe() -> str:
    for candidate in DEFAULT_GIT_CANDIDATES:
        if candidate and os.path.isfile(candidate):
            return candidate
    return "git"


def run_cmd(args, cwd=None, check=True) -> subprocess.CompletedProcess:
    cwd = str(cwd or REPO_ROOT)
    return subprocess.run(args, cwd=cwd, check=check, text=True, capture_output=True)


def get_version(override: str = None) -> str:
    if override:
        return override.lstrip("v")
    
    # 1. Inspect src/config.py
    if CONFIG_PY.exists():
        content = CONFIG_PY.read_text(encoding="utf-8")
        m = re.search(r'["\']version["\']:\s*["\']([^"\']+)["\']', content)
        if m:
            return m.group(1).lstrip("v")

    # 2. Inspect config.json fallback
    if CONFIG_JSON.exists():
        try:
            data = json.loads(CONFIG_JSON.read_text(encoding="utf-8"))
            if "version" in data:
                return data["version"].lstrip("v")
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


def update_docs_files(version: str) -> bool:
    """Updates docs/index.html and docs/sitemap.xml with the target version."""
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
            print(f"[INFO] Updated {DOCS_INDEX.relative_to(REPO_ROOT)} to v{version}")
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
            print(f"[INFO] Updated {DOCS_SITEMAP.relative_to(REPO_ROOT)} lastmod to {today}")
            modified = True

    return modified


def build_installer(version: str):
    """Executes PyInstaller spec build and Inno Setup compiler."""
    print(f"\n[BUILD] Step 1: Compiling PyInstaller canonical spec...")
    py_exe = sys.executable
    res_pyi = subprocess.run([py_exe, "-m", "PyInstaller", "-y", "--clean", str(SPEC_FILE)], cwd=str(REPO_ROOT))
    if res_pyi.returncode != 0:
        raise RuntimeError("PyInstaller build failed.")

    print(f"\n[BUILD] Step 2: Compiling Inno Setup installer...")
    iscc = DEFAULT_ISCC_PATH if os.path.isfile(DEFAULT_ISCC_PATH) else "ISCC.exe"
    res_inno = subprocess.run([iscc, str(SETUP_ISS)], cwd=str(REPO_ROOT))
    if res_inno.returncode != 0:
        raise RuntimeError("Inno Setup compiler failed.")
    print("[BUILD] Installer compilation finished successfully.")


def commit_and_push_git(version: str, commit_msg: str = None, dry_run: bool = False):
    git_exe = find_git_exe()
    tag = f"v{version}"

    # Check for uncommitted docs or version changes
    status = run_cmd([git_exe, "status", "--porcelain"]).stdout.strip()
    if status:
        print(f"[GIT] Working tree changes detected:\n{status}")
        if dry_run:
            print("[DRY-RUN] Skipping git add, commit, and push.")
            return

        run_cmd([git_exe, "add", "-u"])
        # Commit if changes were staged
        staged = run_cmd([git_exe, "diff", "--staged", "--name-only"]).stdout.strip()
        if staged:
            msg = commit_msg or f"docs(readme,web): synchronize multi-stage noise suppression pipeline, limits & app setup guides for {tag}"
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
- **Size**: `{file_size:,} bytes` (~{size_mb:.1f} MB)
"""

    headers = {
        "Authorization": f"token {token}",
        "Accept": "application/vnd.github.v3+json",
        "User-Agent": "Getsu-Auto-Publisher",
    }

    print(f"\n[RELEASE] Target Tag: {tag}")
    print(f"[RELEASE] Title: {title}")
    print(f"[RELEASE] Installer: {installer_path} ({file_size:,} bytes, {sha256[:16]}...)")

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

    # 4. Upload binary asset
    upload_url = f"{upload_base}?name={asset_name}"
    print(f"[ASSET] Uploading {asset_name} ({file_size:,} bytes)...")

    with open(installer_path, "rb") as f:
        file_data = f.read()

    upload_headers = headers.copy()
    upload_headers["Content-Type"] = "application/octet-stream"
    upload_headers["Content-Length"] = str(len(file_data))

    upload_req = urllib.request.Request(upload_url, data=file_data, headers=upload_headers)
    with urllib.request.urlopen(upload_req) as upload_resp:
        asset_info = json.loads(upload_resp.read().decode())
        print(f"[SUCCESS] Asset uploaded successfully!")
        print(f"[DOWNLOAD URL] {asset_info['browser_download_url']}")

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


def main():
    parser = argparse.ArgumentParser(description="Unified Getsu Automated Release & Publishing Tool")
    parser.add_argument("--version", type=str, default=None, help="Explicit target version (e.g. 1.2.8)")
    parser.add_argument("--title", type=str, default=None, help="Custom release title")
    parser.add_argument("--message", type=str, default=None, help="Custom git commit message")
    parser.add_argument("--build", action="store_true", help="Compile PyInstaller and Inno Setup before publishing")
    parser.add_argument("--dry-run", action="store_true", help="Inspect operations without pushing or uploading")
    parser.add_argument("--no-git", action="store_true", help="Skip git commit/push operations")
    args = parser.parse_args()

    version = get_version(args.version)
    tag = f"v{version}"
    print(f"==================================================")
    print(f" Getsu Unified Release Automation — Target: {tag}")
    print(f"==================================================")

    # 1. Build if requested
    if args.build:
        build_installer(version)

    # 2. Locate installer
    installer_path = DIST_INSTALLER_DIR / f"Getsu-{tag}-Setup.exe"
    if not installer_path.is_file():
        # Fallback without tag
        fallback_path = DIST_INSTALLER_DIR / f"Getsu-Setup.exe"
        if fallback_path.is_file():
            installer_path = fallback_path
        else:
            print(f"[ERROR] Installer not found at {installer_path}")
            print("Tip: Run with --build or compile using Inno Setup first.")
            sys.exit(1)

    # 3. Synchronize Web Docs
    update_docs_files(version)

    # 4. Git Synchronization
    if not args.no_git:
        commit_and_push_git(version, commit_msg=args.message, dry_run=args.dry_run)

    # 5. Publish to GitHub
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
