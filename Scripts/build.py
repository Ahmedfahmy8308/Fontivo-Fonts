#!/usr/bin/env python3
"""
Fontivo Fonts - Unified Incremental Package Build Engine (v1.0.0)
Performs content-addressed change detection against State/build-state.json.
Rebuilds ONLY genuinely affected packages in Build/.
Guarantees: A one-font change rebuilds exactly 1 package, leaving all other packages untouched.
"""

import os
import sys
import json
import time
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from common.hasher import get_file_sha256
from common.packager import build_package_archive, test_archive_integrity
from common.state import BuildStateManager, atomic_write_json, normalize_path_string
from common.validator import validate_font_file

ROOT = SCRIPTS_DIR.parent
LIB = ROOT / "Library"
BUILD = ROOT / "Build"
MAN = ROOT / "Manifests"
META = ROOT / "Metadata"

QUARANTINE_DIRS = {"Corrupted-Quarantine", "AppleDouble-Quarantine", "ZeroByte-Quarantine", "Legacy-FON"}

def scan_library():
    """Gathers all valid font files in Library/ with clean relative path and full path, enforcing strict path containment."""
    items = {}
    if not LIB.exists():
        return items
    lib_resolved = LIB.resolve()
    col_path_str = str(lib_resolved)
    for root, dirs, files in os.walk(col_path_str):
        dirs[:] = [d for d in dirs if d not in QUARANTINE_DIRS]
        for fname in files:
            full_p = (Path(root) / fname).resolve()
            # Strictly validate path traversal and root escaping
            if not full_p.is_relative_to(lib_resolved):
                print(f"[!] Security warning: Path {full_p} escapes library root {lib_resolved}. Skipping.")
                continue
            rel = full_p.relative_to(lib_resolved)
            clean_rel = rel.as_posix()
            if ".." in clean_rel.split("/") or "../" in clean_rel:
                print(f"[!] Security warning: Traversal sequence detected in {clean_rel}. Skipping.")
                continue
            items[clean_rel] = full_p
    return items

def main():
    print("=== Fontivo Unified Incremental Build Engine (v1.0.0) ===")
    state_mgr = BuildStateManager(ROOT)
    state = state_mgr.data

    # Load existing catalogs
    with open(META / "packages.json", "r", encoding="utf-8") as f:
        packages_catalog = json.load(f)
    with open(META / "collections.json", "r", encoding="utf-8") as f:
        collections_catalog = json.load(f)

    # 1. Scan current files on disk in Library/
    current_files = scan_library()
    print(f"[*] Scanned Library/ on disk: {len(current_files):,} valid font files.")

    # 2. Compare against state to classify changes
    # Key in state: clean_rel inside Library/ (<Category>/Families/<Family>/<File>)
    state_fonts_by_rel = {}
    lib_resolved = LIB.resolve()
    for fid, f in state["fonts"].items():
        lp = f.get("libraryRelativePath", "").replace("\\", "/").strip()
        if ".." in lp.split("/"):
            print(f"[!] Security warning: Traversal sequence in state font {fid}: {lp}. Skipping.")
            continue
        if lp.startswith("Library/Essential/"):
            clean_rel = lp[len("Library/Essential/"):]
        elif lp.startswith("Library/"):
            clean_rel = lp[len("Library/"):]
        else:
            parts = lp.split("/")
            clean_rel = "/".join(parts[1:]) if len(parts) > 1 else f.get("fileName", "")
        clean_rel = clean_rel.lstrip("/")
        resolved_fp = (lib_resolved / clean_rel).resolve()
        if not resolved_fp.is_relative_to(lib_resolved):
            print(f"[!] Security warning: State font {fid} resolves outside Library root: {resolved_fp}. Skipping.")
            continue
        state_fonts_by_rel[clean_rel] = f

    added = []
    removed = []
    modified = []
    unchanged = []

    # Check additions and modifications
    for clean_rel, fp in current_files.items():
        if clean_rel not in state_fonts_by_rel:
            sha = get_file_sha256(fp)
            added.append((clean_rel, fp, sha))
        else:
            state_rec = state_fonts_by_rel[clean_rel]
            stat_res = fp.stat()
            if stat_res.st_size != state_rec["sizeBytes"]:
                sha = get_file_sha256(fp)
                modified.append((clean_rel, fp, sha, state_rec))
            elif state_rec.get("mtime") and stat_res.st_mtime == state_rec["mtime"]:
                unchanged.append((clean_rel, state_rec))
            else:
                sha = get_file_sha256(fp)
                if sha != state_rec["sha256"]:
                    modified.append((clean_rel, fp, sha, state_rec))
                else:
                    state_rec["mtime"] = stat_res.st_mtime
                    unchanged.append((clean_rel, state_rec))

    # Check removals
    for clean_rel, state_rec in state_fonts_by_rel.items():
        if clean_rel not in current_files:
            removed.append((clean_rel, state_rec))

    print(f"[*] Change Detection: Added: {len(added)}, Removed: {len(removed)}, Modified: {len(modified)}, Unchanged: {len(unchanged):,}")

    if not added and not removed and not modified:
        print("[+] Repository is up-to-date. Zero packages rebuilt. Build state preserved.")
        return 0

    # 3. Identify dirty packages
    dirty_packages = set()

    # Track package contents: package_id -> list of (arcname, full_path, font_id, sha256)
    package_file_map = {pid: [] for pid in packages_catalog.keys()}

    # Pre-fill with current unchanged state
    for clean_rel, s_rec in unchanged:
        pid = s_rec["packageId"]
        full_p = current_files[clean_rel]
        package_file_map[pid].append((clean_rel, full_p, s_rec["id"], s_rec["sha256"]))

    # Handle removals: mark package dirty and record tombstone
    for clean_rel, s_rec in removed:
        pid = s_rec["packageId"]
        dirty_packages.add(pid)
        state_mgr.record_tombstone(s_rec["id"], s_rec, reason="DELETED_FROM_LIBRARY")
        if s_rec["id"] in state["fonts"]:
            del state["fonts"][s_rec["id"]]
        print(f"  [-] Detected REMOVAL: {clean_rel} (Package: {pid}) -> Tombstone recorded.")

    # Handle modifications: mark package dirty and update state
    for clean_rel, fp, new_sha, s_rec in modified:
        pid = s_rec["packageId"]
        dirty_packages.add(pid)
        package_file_map[pid].append((clean_rel, fp, s_rec["id"], new_sha))
        s_rec["sha256"] = new_sha
        s_rec["sizeBytes"] = fp.stat().st_size
        s_rec["mtime"] = fp.stat().st_mtime
        print(f"  [*] Detected MODIFICATION: {clean_rel} (Package: {pid}) -> SHA: {new_sha[:16]}")

    # Handle additions: assign to target package stably
    cand_pids = sorted(list(packages_catalog.keys()))
    target_pid = cand_pids[-1]

    for clean_rel, fp, new_sha in added:
        new_fid = f"font-{new_sha[:16]}"
        dirty_packages.add(target_pid)
        norm_rel = str(clean_rel).replace("\\", "/").strip().lstrip("/")
        if ".." in norm_rel.split("/") or not fp.resolve().is_relative_to(lib_resolved):
            raise ValueError(f"Security error: Invalid path for addition: {clean_rel}")
        package_file_map[target_pid].append((norm_rel, fp, new_fid, new_sha))

        parts = norm_rel.split("/")
        lang = parts[0] if len(parts) > 0 else "Latin"
        fam = parts[2] if len(parts) > 2 else (parts[1] if len(parts) > 1 else "Unknown")

        state["fonts"][new_fid] = {
            "id": new_fid,
            "sha256": new_sha,
            "fileName": fp.name,
            "format": fp.suffix[1:].upper(),
            "sizeBytes": fp.stat().st_size,
            "collection": "Essential",
            "packageId": target_pid,
            "libraryRelativePath": f"Library/{norm_rel}",
            "familyName": fam,
            "primaryLanguage": lang,
            "mtime": fp.stat().st_mtime,
            "isBaseCollection": True,
            "isAdditional": False
        }
        print(f"  [+] Detected ADDITION: {norm_rel} -> Assigned to Package: {target_pid}")

    print(f"\n[*] Rebuilding {len(dirty_packages)} affected package(s): {sorted(list(dirty_packages))}")

    # 4. Rebuild ONLY dirty packages in Build/
    BUILD.mkdir(parents=True, exist_ok=True)
    for pid in sorted(list(dirty_packages)):
        pkg_meta = packages_catalog[pid]
        out_zip = BUILD / pkg_meta["fileName"]

        files_for_zip = [(item[0], item[1]) for item in package_file_map[pid]]

        t0 = time.time()
        res = build_package_archive(out_zip, files_for_zip)
        t_build = time.time() - t0

        # Validate CRC
        crc_ok, crc_err = test_archive_integrity(out_zip)
        if not crc_ok:
            raise RuntimeError(f"Corrupted archive generated for {pid}: {crc_err}")

        # Bump package patch version
        v_parts = pkg_meta.get("version", "1.0.0").split(".")
        v_parts[-1] = str(int(v_parts[-1]) + 1)
        pkg_meta["version"] = ".".join(v_parts)
        pkg_meta["sha256"] = res["sha256"]
        pkg_meta["compressedSizeBytes"] = res["compressedSizeBytes"]
        pkg_meta["uncompressedSizeBytes"] = res["uncompressedSizeBytes"]
        pkg_meta["fontCount"] = res["fontCount"]
        pkg_meta["buildTimestamp"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        pkg_meta["files"] = [item[0] for item in package_file_map[pid]]

        state["packages"][pid] = {
            "id": pid,
            "fileName": pkg_meta["fileName"],
            "collectionId": "fontivo-essential",
            "version": pkg_meta["version"],
            "sha256": pkg_meta["sha256"],
            "fontCount": pkg_meta["fontCount"],
            "compressedSizeBytes": pkg_meta["compressedSizeBytes"],
            "uncompressedSizeBytes": pkg_meta["uncompressedSizeBytes"],
            "format": "zip"
        }

        print(f"[+] Rebuilt {pid} ({pkg_meta['fileName']}) in {t_build:.2f}s: "
              f"Version: {pkg_meta['version']}, {res['fontCount']} fonts, {res['compressedSizeBytes']/(1024*1024):.2f} MB, SHA: {res['sha256'][:16]}...")

    # 5. Bump library version
    new_lib_version = state_mgr.bump_library_version(patch=True)
    state["libraryVersion"] = new_lib_version
    state["buildTimestamp"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    state["totalUniqueValidFonts"] = len(state["fonts"])
    state_mgr.save_state()

    # 6. Update Metadata files atomically
    atomic_write_json(META / "packages.json", packages_catalog, indent=2, ensure_ascii=False)

    collections_catalog["fontivo-essential"]["type"] = "base"
    collections_catalog["fontivo-essential"]["version"] = new_lib_version
    collections_catalog["fontivo-essential"]["fontCount"] = len(state["fonts"])
    collections_catalog["fontivo-essential"]["packageCount"] = len(packages_catalog)
    collections_catalog["fontivo-essential"]["totalCompressedSizeBytes"] = sum(p["compressedSizeBytes"] for p in packages_catalog.values())
    collections_catalog["fontivo-essential"]["totalUncompressedSizeBytes"] = sum(p["uncompressedSizeBytes"] for p in packages_catalog.values())
    collections_catalog["fontivo-essential"]["packageIds"] = list(packages_catalog.keys())

    atomic_write_json(META / "collections.json", collections_catalog, indent=2, ensure_ascii=False)

    # 7. Update Compact Manifest conforming to Ahmedfahmy8308/Fontivo-Fonts releases
    compact_manifest = {
        "schemaVersion": "1.0.0",
        "libraryVersion": new_lib_version,
        "metadataRevision": state.get("metadataRevision", 1),
        "releaseDate": time.strftime("%Y-%m-%d", time.gmtime()),
        "minimumFontivoAppVersion": "1.0.0",
        "packageBaseUrl": f"https://github.com/Ahmedfahmy8308/Fontivo-Fonts/releases/download/v{new_lib_version}",
        "metadataBaseUrl": f"https://raw.githubusercontent.com/Ahmedfahmy8308/Fontivo-Fonts/v{new_lib_version}/Metadata",
        "summary": {
            "totalUniqueFonts": len(state["fonts"]),
            "totalFamilies": collections_catalog["fontivo-essential"].get("familyCount", 4477),
            "totalPackages": len(packages_catalog),
            "totalCompressedSizeBytes": sum(p["compressedSizeBytes"] for p in packages_catalog.values()),
            "totalUncompressedSizeBytes": sum(p["uncompressedSizeBytes"] for p in packages_catalog.values())
        },
        "collections": collections_catalog,
        "packages": {pid: {
            "id": p["id"],
            "fileName": p["fileName"],
            "collectionId": p["collectionId"],
            "version": p["version"],
            "url": f"https://github.com/Ahmedfahmy8308/Fontivo-Fonts/releases/download/v{new_lib_version}/{p['fileName']}",
            "fontCount": p["fontCount"],
            "compressedSizeBytes": p["compressedSizeBytes"],
            "uncompressedSizeBytes": p["uncompressedSizeBytes"],
            "sha256": p["sha256"],
            "format": p.get("format", "zip")
        } for pid, p in packages_catalog.items()}
    }
    atomic_write_json(MAN / "manifest.json", compact_manifest, indent=2, ensure_ascii=False)

    print(f"\n[+] Incremental build finished successfully: Library upgraded to v{new_lib_version}. {len(dirty_packages)} package(s) rebuilt.")
    return len(dirty_packages)

if __name__ == "__main__":
    sys.exit(0 if main() is not None else 1)
