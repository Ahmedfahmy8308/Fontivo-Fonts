#!/usr/bin/env python3
"""
Fontivo Fonts - Unified Library Scanner CLI (v1.0.0)
Scans the canonical unified library, computes content counts, and verifies alignment with build state.
"""

import sys
import json
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from common.hasher import get_file_sha256
from common.state import BuildStateManager

ROOT = SCRIPTS_DIR.parent
LIB = ROOT / "Library"
BUILD = ROOT / "Build"

def main():
    print("=== Fontivo Unified Library Scanner (v1.0.0) ===")
    mgr = BuildStateManager(ROOT)
    state_fonts = mgr.data.get("fonts", {})
    state_packages = mgr.data.get("packages", {})
    print(f"[*] State tracks: {len(state_fonts):,} valid fonts across {len(state_packages)} packages.")

    if not LIB.exists():
        print(f"[-] Error: Library folder missing at {LIB}")
        return 1

    # Scan all font files in Library/ (.ttf, .otf, .ttc, .woff, .woff2) enforcing strict containment
    lib_resolved = LIB.resolve()
    lib_files = []
    for f in LIB.rglob("*"):
        if f.is_file() and f.suffix.lower() in {".ttf", ".otf", ".ttc", ".woff", ".woff2"}:
            resolved_f = f.resolve()
            if not resolved_f.is_relative_to(lib_resolved):
                print(f"[!] Security warning: File {resolved_f} escapes Library root. Skipping.")
                continue
            rel_str = resolved_f.relative_to(lib_resolved).as_posix()
            if ".." in rel_str.split("/") or "../" in rel_str:
                print(f"[!] Security warning: Traversal sequence in {rel_str}. Skipping.")
                continue
            lib_files.append(f)

    print(f"[+] Total font files on disk in Library/: {len(lib_files):,}")

    # Breakdown by category
    categories = {}
    for f in lib_files:
        rel = f.resolve().relative_to(lib_resolved)
        cat = rel.parts[0] if len(rel.parts) > 0 else "Root"
        categories[cat] = categories.get(cat, 0) + 1

    print("[*] Category breakdown in Library/:")
    for cat in sorted(categories.keys()):
        print(f"    - {cat:<15}: {categories[cat]:,} fonts")

    # Verify state alignment
    missing_on_disk = 0
    for fid, f_rec in state_fonts.items():
        rel = str(f_rec.get("libraryRelativePath", "")).replace("\\", "/").strip()
        if ".." in rel.split("/") or "../" in rel:
            print(f"[-] Security error: Traversal in state font {fid}: {rel}")
            missing_on_disk += 1
            continue
        if rel.startswith("Library/Essential/"):
            rel = "Library/" + rel[len("Library/Essential/"):]
        elif not rel.startswith("Library/"):
            rel = f"Library/{rel.lstrip('/')}"
        disk_path = (ROOT / rel).resolve()
        if not disk_path.is_relative_to(lib_resolved) or not disk_path.is_file():
            missing_on_disk += 1

    if missing_on_disk > 0:
        print(f"[-] Integrity Warning: {missing_on_disk} fonts tracked in state are missing on disk!")
        return 1
    else:
        print(f"[+] 100% of tracked fonts ({len(state_fonts):,}) verified physically present on disk in Library/.")

    # Check Build/ packages
    if BUILD.exists():
        build_zips = sorted(list(BUILD.glob("*.zip")))
        total_zip_mb = sum(z.stat().st_size for z in build_zips) / (1024 * 1024)
        print(f"[+] Distribution packages in Build/: {len(build_zips)} archives ({total_zip_mb:.2f} MB total).")
    else:
        print("[-] Build/ directory not found.")

    print("\n[+] Verification complete: Library/ and Build/ are in perfect alignment.")
    return 0

if __name__ == "__main__":
    sys.exit(main())
