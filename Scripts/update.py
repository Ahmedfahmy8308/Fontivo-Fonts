#!/usr/bin/env python3
"""
Fontivo Fonts - Safe Font Lifecycle & Update CLI (v1.0.0)
Provides safe, controlled operations to:
  - Add a font to the canonical library:
      python Scripts/update.py add <font_path> --category <Arabic|Latin|...> [--family <FamilyName>]
  - Remove a font from the canonical library:
      python Scripts/update.py remove <font_name_or_path_or_id>
  - Modify a font binary:
      python Scripts/update.py modify <identifier> <new_file>
  - Update font metadata only:
      python Scripts/update.py metadata <identifier> <key> <value>
Integrates with the Incremental Build Engine to trigger minimal package rebuilds (~1s).
"""

import os
import sys
import json
import shutil
import argparse
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from common.hasher import get_file_sha256
from common.validator import validate_font_file
from common.state import BuildStateManager, atomic_write_json, normalize_path_string
import build as build_engine

ROOT = SCRIPTS_DIR.parent
LIB = ROOT / "Library"
BUILD = ROOT / "Build"
META = ROOT / "Metadata"

def assert_no_traversal(val: str, field_name: str = "input"):
    """Validates that an input does not contain path traversal sequences ('..')."""
    if not val:
        return
    s = str(val).replace("\\", "/")
    parts = s.split("/")
    if ".." in parts or any(p == ".." for p in parts) or "../" in s or "/.." in s or s == "..":
        raise ValueError(f"Security error: Traversal sequence ('..') detected in {field_name}: '{val}'")

def assert_inside_library(path_obj: Path, lib_root: Path = LIB) -> Path:
    """Ensures a path resolves strictly within the Library root."""
    resolved_target = path_obj.resolve()
    resolved_lib = lib_root.resolve()
    if not resolved_target.is_relative_to(resolved_lib):
        raise ValueError(f"Security error: Path '{resolved_target}' attempts to escape Library root '{resolved_lib}'")
    return resolved_target

def parse_font_family(font_path):
    """Parses font family name from font binary metadata tables using fontTools."""
    try:
        from fontTools.ttLib import TTFont
        font = TTFont(str(font_path))
        family_name = None
        # Try Typographic Family Name (nameID 16), then Font Family Name (nameID 1)
        for target_id in (16, 1):
            for record in font["name"].names:
                if record.nameID == target_id:
                    try:
                        name_str = record.toUnicode()
                        if name_str and name_str.strip():
                            family_name = name_str.strip()
                            break
                    except Exception:
                        pass
            if family_name:
                break
        if family_name:
            # Sanitize directory name
            sanitized = "".join(c for c in family_name if c not in '<>:"/\\|?*').strip()
            return sanitized if sanitized else font_path.stem
    except Exception:
        pass
    return font_path.stem

def handle_add(args):
    try:
        assert_no_traversal(args.font_path, "font_path")
        if args.category:
            assert_no_traversal(args.category, "category")
        if args.family:
            assert_no_traversal(args.family, "family")
    except ValueError as e:
        print(f"[-] {e}")
        return 1

    src_path = Path(args.font_path).resolve()
    if not src_path.exists() or not src_path.is_file():
        print(f"[-] Error: Source file not found: {src_path}")
        return 1

    # Validate font
    print(f"[*] Validating font binary: {src_path.name}...")
    res = validate_font_file(src_path)
    if not res["valid"]:
        print(f"[-] Cannot add invalid font: {res['status']} ({res['error']})")
        return 1

    sha = get_file_sha256(src_path)
    state_mgr = BuildStateManager(ROOT)
    existing = state_mgr.get_font_by_sha(sha)
    if existing:
        print(f"[!] Font with identical SHA-256 already exists in library: {existing['id']} ({existing['libraryRelativePath']})")
        return 0

    category = args.category or args.language or "Latin"
    family = args.family
    if not family:
        family = parse_font_family(src_path)
        print(f"[*] Automatically parsed font family: '{family}'")

    try:
        assert_no_traversal(category, "category")
        assert_no_traversal(family, "family")
        dest_dir = LIB / category / "Families" / family
        assert_inside_library(dest_dir)
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest_file = dest_dir / src_path.name
        assert_inside_library(dest_file)
    except ValueError as e:
        print(f"[-] {e}")
        return 1

    if dest_file.exists():
        print(f"[-] Destination file already exists: {dest_file.relative_to(ROOT).as_posix()}")
        return 1

    # Atomic copy via os.replace
    dest_tmp = dest_file.with_suffix(f"{dest_file.suffix}.tmp")
    shutil.copy2(src_path, dest_tmp)
    os.replace(dest_tmp, dest_file)
    print(f"[+] Placed font into canonical library: {dest_file.relative_to(ROOT).as_posix()}")

    # Trigger incremental build
    print("[*] Triggering incremental build engine...")
    rebuilt = build_engine.main()
    print(f"[+] Addition complete. Rebuilt {rebuilt} affected package(s) in ~1s.")
    return 0

def handle_remove(args):
    try:
        assert_no_traversal(args.identifier, "identifier")
    except ValueError as e:
        print(f"[-] {e}")
        return 1

    state_mgr = BuildStateManager(ROOT)
    identifier = args.identifier
    font_rec = state_mgr.find_font(identifier)

    if not font_rec:
        print(f"[-] Font not found in state: {identifier}")
        return 1

    # Resolve direct Library path and strictly validate
    raw_rel = font_rec["libraryRelativePath"].replace("\\", "/").strip()
    try:
        assert_no_traversal(raw_rel, "libraryRelativePath")
    except ValueError as e:
        print(f"[-] {e}")
        return 1

    if raw_rel.startswith("Library/Essential/"):
        rel_path = "Library/" + raw_rel[len("Library/Essential/"):]
    elif not raw_rel.startswith("Library/"):
        rel_path = f"Library/{raw_rel.lstrip('/')}"
    else:
        rel_path = raw_rel

    full_path = ROOT / rel_path
    try:
        assert_inside_library(full_path)
    except ValueError as e:
        print(f"[-] {e}")
        return 1

    if full_path.exists():
        full_path.unlink()
        print(f"[+] Removed font binary from disk: {rel_path}")
        # Clean up empty parent directory if empty and within Library
        parent = full_path.parent
        try:
            assert_inside_library(parent)
            if parent.exists() and not any(parent.iterdir()):
                parent.rmdir()
        except Exception:
            pass
    else:
        print(f"[!] File was already missing from disk: {rel_path}")

    # Trigger incremental build
    print("[*] Triggering incremental build engine...")
    rebuilt = build_engine.main()
    print(f"[+] Removal complete. Recorded tombstone and rebuilt {rebuilt} affected package(s) in ~1s.")
    return 0

def handle_modify(args):
    try:
        assert_no_traversal(args.identifier, "identifier")
        assert_no_traversal(args.new_file, "new_file")
    except ValueError as e:
        print(f"[-] {e}")
        return 1

    state_mgr = BuildStateManager(ROOT)
    identifier = args.identifier
    new_bin = Path(args.new_file).resolve()
    if not new_bin.exists() or not new_bin.is_file():
        print(f"[-] New binary file not found: {new_bin}")
        return 1

    # Validate new binary
    val = validate_font_file(new_bin)
    if not val["valid"]:
        print(f"[-] Cannot modify with invalid font: {val['status']} ({val['error']})")
        return 1

    font_rec = state_mgr.find_font(identifier)
    if not font_rec:
        print(f"[-] Font not found in state: {identifier}")
        return 1

    raw_rel = font_rec["libraryRelativePath"].replace("\\", "/").strip()
    try:
        assert_no_traversal(raw_rel, "libraryRelativePath")
    except ValueError as e:
        print(f"[-] {e}")
        return 1

    if raw_rel.startswith("Library/Essential/"):
        rel_path = "Library/" + raw_rel[len("Library/Essential/"):]
    elif not raw_rel.startswith("Library/"):
        rel_path = f"Library/{raw_rel.lstrip('/')}"
    else:
        rel_path = raw_rel

    full_path = ROOT / rel_path
    try:
        assert_inside_library(full_path)
    except ValueError as e:
        print(f"[-] {e}")
        return 1

    # Atomic modification via os.replace
    full_tmp = full_path.with_suffix(f"{full_path.suffix}.tmp")
    shutil.copy2(new_bin, full_tmp)
    os.replace(full_tmp, full_path)
    print(f"[+] Replaced binary for {font_rec['id']} at {rel_path}")

    # Trigger incremental build
    print("[*] Triggering incremental build engine...")
    rebuilt = build_engine.main()
    print(f"[+] Modification complete. Rebuilt {rebuilt} package(s) in ~1s.")
    return 0

def handle_metadata(args):
    try:
        assert_no_traversal(args.identifier, "identifier")
    except ValueError as e:
        print(f"[-] {e}")
        return 1

    state_mgr = BuildStateManager(ROOT)
    identifier = args.identifier
    font_rec = state_mgr.find_font(identifier)
    if not font_rec:
        print(f"[-] Font not found in state: {identifier}")
        return 1

    key = args.key
    val = args.value
    font_rec[key] = val

    # Bump metadata revision only
    new_rev = state_mgr.bump_metadata_revision()
    state_mgr.save_state()

    # Update metadata files without rebuilding packages atomically via atomic_write_json
    fonts_file = META / "fonts.json"
    if fonts_file.exists():
        with open(fonts_file, "r", encoding="utf-8") as f:
            all_fonts = json.load(f)
        for f in all_fonts:
            if f["id"] == font_rec["id"]:
                f[key] = val
                break
        atomic_write_json(fonts_file, all_fonts, indent=2, ensure_ascii=False)

    # Update manifest metadata revision atomically via atomic_write_json
    manifest_file = ROOT / "Manifests" / "manifest.json"
    if manifest_file.exists():
        with open(manifest_file, "r", encoding="utf-8") as f:
            man = json.load(f)
        man["metadataRevision"] = new_rev
        atomic_write_json(manifest_file, man, indent=2, ensure_ascii=False)

    print(f"[+] Updated metadata field '{key}' = '{val}' on font {font_rec['id']}.")
    print(f"[+] Bumped metadata revision to {new_rev}. 0 packages rebuilt.")
    return 0

def main():
    parser = argparse.ArgumentParser(description="Fontivo Font Lifecycle Manager")
    subparsers = parser.add_subparsers(dest="command", required=True)

    # Add
    add_p = subparsers.add_parser("add", help="Add a font to Library/ and rebuild package in ~1s")
    add_p.add_argument("font_path", help="Path to font binary (.ttf, .otf, .ttc)")
    add_p.add_argument("--category", "--language", dest="category", default=None,
                       help="Category/Language folder (Arabic, Latin, Urdu, Persian, CJK, etc.)")
    add_p.add_argument("--family", default=None, help="Optional font family name (auto-parsed if omitted)")

    # Remove
    rem_p = subparsers.add_parser("remove", help="Remove font from Library, record tombstone, and rebuild package in ~1s")
    rem_p.add_argument("identifier", help="Font filename, relative path, or font ID")

    # Modify
    mod_p = subparsers.add_parser("modify", help="Replace font binary and rebuild affected package in ~1s")
    mod_p.add_argument("identifier", help="Font filename, relative path, or font ID")
    mod_p.add_argument("new_file", help="Path to new font binary")

    # Metadata
    meta_p = subparsers.add_parser("metadata", help="Update font metadata without rebuilding packages")
    meta_p.add_argument("identifier", help="Font filename, relative path, or font ID")
    meta_p.add_argument("key", help="Metadata attribute name")
    meta_p.add_argument("value", help="New attribute value")

    args = parser.parse_args()
    if args.command == "add":
        sys.exit(handle_add(args))
    elif args.command == "remove":
        sys.exit(handle_remove(args))
    elif args.command == "modify":
        sys.exit(handle_modify(args))
    elif args.command == "metadata":
        sys.exit(handle_metadata(args))

if __name__ == "__main__":
    main()
