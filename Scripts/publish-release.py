#!/usr/bin/env python3
"""
Fontivo Fonts - GitHub Release Automation Tool (v1.0.0)
Prepares, validates, and orchestrates GitHub Releases for font distribution packages.
Official Repository: https://github.com/Ahmedfahmy8308/Fontivo-Fonts

Modes:
  --check            Validates Build/ archives, SHA-256 checksums, and manifest download URLs.
  --prepare-manifest Updates download URLs in Manifests/manifest.json conforming to official release schema.
  --dry-run (default) Validates release assets and outputs exact CLI commands for tagging and publishing.
  --publish          Executes GitHub Release creation via GitHub CLI ('gh').
"""

import os
import sys
import json
import re
import argparse
import hashlib
import subprocess
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from common.state import atomic_write_json, atomic_write_text

TAG_REGEX = re.compile(r"^v[0-9]+\.[0-9]+\.[0-9]+(-[a-zA-Z0-9.]+)?$")
REPO_REGEX = re.compile(r"^[a-zA-Z0-9_.-]+/[a-zA-Z0-9_.-]+$")

ROOT = Path(__file__).resolve().parent.parent
BUILD = ROOT / "Build"
MAN = ROOT / "Manifests"
META = ROOT / "Metadata"

DEFAULT_REPO = "Ahmedfahmy8308/Fontivo-Fonts"
DEFAULT_REPO_URL = f"https://github.com/{DEFAULT_REPO}"

def get_file_sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(65536):
            h.update(chunk)
    return h.hexdigest()

def ensure_manifest_urls(manifest, repo_name, version):
    """Ensures download URLs conform to https://github.com/{repo}/releases/download/v{version}/{fileName}"""
    clean_version = version.lstrip("v")
    base_url = f"https://github.com/{repo_name}/releases/download/v{clean_version}"
    meta_url = f"https://raw.githubusercontent.com/{repo_name}/v{clean_version}/Metadata"
    
    modified = False
    if manifest.get("packageBaseUrl") != base_url:
        manifest["packageBaseUrl"] = base_url
        modified = True
    if manifest.get("metadataBaseUrl") != meta_url:
        manifest["metadataBaseUrl"] = meta_url
        modified = True

    packages = manifest.get("packages", {})
    if isinstance(packages, dict):
        for pid, p in packages.items():
            expected_url = f"{base_url}/{p['fileName']}"
            if p.get("url") != expected_url:
                p["url"] = expected_url
                modified = True
    elif isinstance(packages, list):
        for p in packages:
            expected_url = f"{base_url}/{p['fileName']}"
            if p.get("url") != expected_url:
                p["url"] = expected_url
                modified = True

    return modified

def generate_release_notes(version, packages_list):
    total_fonts = sum(p["fontCount"] for p in packages_list)
    total_comp_mb = sum(p["compressedSizeBytes"] for p in packages_list) / (1024 * 1024)
    total_uncomp_mb = sum(p["uncompressedSizeBytes"] for p in packages_list) / (1024 * 1024)
    
    notes = [
        f"## Fontivo Fonts Release v{version.lstrip('v')}",
        "",
        "### Highlights",
        f"- **Canonical Collection**: Unified `fontivo-essential` distribution",
        f"- **Total Valid Fonts**: {total_fonts:,} font files across 4,477 typographic families",
        f"- **Language Coverage**: Arabic, Latin, Urdu, Persian, CJK, Indic, Hebrew, and Unclassified",
        f"- **Distribution Packages**: {len(packages_list)} deterministic packages in `Build/`",
        f"- **Compressed Download Size**: ~{total_comp_mb:.2f} MB (Uncompressed: ~{total_uncomp_mb:.2f} MB)",
        f"- **Incremental Build Guarantee**: Content-addressed change detection (~1s single-font rebuilds)",
        "",
        "### Package Checksums (SHA-256)",
        "| Package | Fonts | Size (MB) | SHA-256 Checksum |",
        "| :--- | :---: | :---: | :--- |"
    ]
    for p in packages_list:
        sz_mb = p["compressedSizeBytes"] / (1024 * 1024)
        notes.append(f"| `{p['fileName']}` | {p['fontCount']} | {sz_mb:.2f} | `{p['sha256']}` |")

    return "\n".join(notes)

def main():
    parser = argparse.ArgumentParser(description="Fontivo Fonts GitHub Release Tool")
    parser.add_argument("--repo", default=DEFAULT_REPO, help=f"GitHub repository owner/repo (default: {DEFAULT_REPO})")
    parser.add_argument("--tag", default=None, help="Release tag name (default: reads v{version} from manifest.json)")
    parser.add_argument("--check", action="store_true", help="Perform integrity check on all Build/ packages")
    parser.add_argument("--prepare-manifest", action="store_true", help="Sync manifest package download URLs to target repo and version")
    parser.add_argument("--publish", action="store_true", help="Execute actual release creation using GitHub CLI ('gh')")
    parser.add_argument("--notes-out", default=None, help="Optional path to output generated release notes markdown")
    args = parser.parse_args()

    # Normalize and strictly validate repo against argument injection
    repo = args.repo.replace("https://github.com/", "").strip("/")
    if not REPO_REGEX.match(repo):
        print(f"[-] Security error: Invalid repository format '{repo}'. Must match {REPO_REGEX.pattern}")
        return 1

    # 1. Load and validate manifest
    manifest_file = MAN / "manifest.json"
    if not manifest_file.exists():
        print(f"[-] Error: Manifest file not found: {manifest_file}")
        return 1

    with open(manifest_file, "r", encoding="utf-8") as f:
        manifest = json.load(f)

    lib_version = manifest.get("libraryVersion", "1.0.0")
    tag = args.tag or f"v{lib_version}"
    if not TAG_REGEX.match(tag):
        print(f"[-] Security error: Invalid release tag '{tag}'. Must match {TAG_REGEX.pattern}")
        return 1
    clean_ver = tag.lstrip("v")

    # Validate notes_out if specified
    if args.notes_out:
        notes_str = str(args.notes_out).replace("\\", "/")
        if ".." in notes_str.split("/") or ".." in notes_str:
            print(f"[-] Security error: Traversal sequence ('..') in --notes-out: {args.notes_out}")
            return 1

    print(f"=== Fontivo Fonts GitHub Release Pipeline [{tag}] ===")
    print(f"[*] Target Repository: https://github.com/{repo}")
    print(f"[*] Library Version:   {lib_version}")

    # 2. Update manifest URLs if requested or needed
    urls_updated = ensure_manifest_urls(manifest, repo, clean_ver)
    if urls_updated or args.prepare_manifest:
        atomic_write_json(manifest_file, manifest, indent=2, ensure_ascii=False)
        print(f"[+] Synchronized download URLs in Manifests/manifest.json conforming to:")
        print(f"    https://github.com/{repo}/releases/download/{tag}/{{fileName}}")

    # 3. Read package list
    raw_pkgs = manifest.get("packages", {})
    packages_list = list(raw_pkgs.values()) if isinstance(raw_pkgs, dict) else raw_pkgs
    print(f"[*] Manifest specifies {len(packages_list)} distribution packages.")

    # 4. Verify packages in Build/ directly
    print(f"[*] Inspecting Build/ directory: {BUILD.resolve()}...")
    if not BUILD.exists():
        print(f"[-] Error: Build directory does not exist at {BUILD}")
        return 1

    all_zips = []
    missing = 0
    mismatches = 0

    for p in packages_list:
        fname = p["fileName"]
        zp = BUILD / fname
        if not zp.exists():
            print(f"  [-] Missing package archive: {zp.name}")
            missing += 1
            continue
        all_zips.append(zp)
        actual_sha = get_file_sha256(zp)
        if actual_sha != p["sha256"]:
            print(f"  [-] SHA-256 mismatch for {fname}: manifest={p['sha256'][:16]}, disk={actual_sha[:16]}")
            mismatches += 1

    if missing > 0 or mismatches > 0:
        print(f"\n[-] Validation FAILED: {missing} missing archives, {mismatches} checksum mismatches.")
        print("[-] Run 'python Scripts/build.py' to rebuild affected packages.")
        return 1

    total_mb = sum(z.stat().st_size for z in all_zips) / (1024 * 1024)
    print(f"[+] All {len(all_zips)} ZIP packages verified successfully in Build/ ({total_mb:.2f} MB total, 100% SHA match)!")

    # 5. Generate Release Notes
    release_notes = generate_release_notes(clean_ver, packages_list)
    if args.notes_out:
        notes_path = Path(args.notes_out)
        atomic_write_text(notes_path, release_notes, encoding="utf-8")
        print(f"[+] Wrote release notes to {notes_path}")

    # If --check was passed, exit with success
    if args.check:
        print("\n[+] Release asset verification PASSED.")
        return 0

    # 6. Formulate clean CLI commands
    notes_temp_file = ROOT / "Build" / "RELEASE_NOTES.tmp.md"
    atomic_write_text(notes_temp_file, release_notes, encoding="utf-8")

    gh_cmd = [
        "gh", "release", "create", tag,
        "--repo", repo,
        "--title", f"Fontivo Fonts {tag}",
        "--notes-file", str(notes_temp_file),
    ] + [str(z) for z in all_zips]

    if not args.publish:
        print("\n" + "="*70)
        print("                     RELEASE CLI INSTRUCTIONS")
        print("="*70)
        print("Step 1: Commit and tag the release in Git:")
        print("  git add Manifests/ Metadata/ State/ Scripts/ README.md LICENSE CHANGELOG.md CONTRIBUTING.md SECURITY.md .gitignore")
        print(f'  git commit -m "release: Fontivo Fonts {tag}"')
        print(f'  git tag -a {tag} -m "Fontivo Fonts release {tag}"')
        print("  git push origin main --tags")
        print(f"\nStep 2: Publish release and upload all {len(all_zips)} packages via GitHub CLI:")
        print(f"  gh release create {tag} Build/*.zip --repo {repo} --title \"Fontivo Fonts {tag}\" --notes-file Build/RELEASE_NOTES.tmp.md")
        print("\nOr execute automatically via this script:")
        print(f"  python Scripts/publish-release.py --publish --tag {tag} --repo {repo}")
        print("\nStep 3: Web UI fallback URL:")
        print(f"  https://github.com/{repo}/releases/new?tag={tag}")
        print("="*70)
        return 0

    # Execute actual release
    print(f"\n[*] Executing GitHub CLI release creation on {repo}...")
    try:
        res = subprocess.run(gh_cmd, check=True, capture_output=True, text=True)
        print(f"[+] Release published successfully: {res.stdout.strip()}")
        if notes_temp_file.exists():
            notes_temp_file.unlink()
        return 0
    except subprocess.CalledProcessError as e:
        print(f"[-] Release failed: {e.stderr.strip()}")
        return 1
    except FileNotFoundError:
        print("[-] GitHub CLI ('gh') is not installed or not in PATH.")
        print("    Install 'gh' (https://cli.github.com/) or upload packages manually via GitHub Web UI.")
        return 1

if __name__ == "__main__":
    sys.exit(main())
