#!/usr/bin/env python3
"""
Fontivo Fonts - Unified Incremental Package Build Engine (v1.1.0)
Performs content-addressed change detection against State/build-state.json.
Rebuilds ONLY genuinely affected packages in Build/.
Updates Metadata/fonts.json, Metadata/families.json, Metadata/packages.json,
Metadata/collections.json, and Manifests/manifest.json atomically.
Guarantees: A one-font change rebuilds exactly 1 package, leaving all other packages untouched.
"""

import os
import sys
import json
import time
import hashlib
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from common.hasher import get_file_sha256
from common.mojibake import repair_mojibake
from common.packager import build_package_archive, test_archive_integrity
from common.state import (
    BuildStateManager,
    atomic_write_json,
    normalize_path_string,
    normalize_library_rel_path,
)
from common.validator import validate_font_file, extract_font_metadata, SUPPORTED_EXTENSIONS
from sync_docs import sync_documentation

ROOT = SCRIPTS_DIR.parent
LIB = ROOT / "Library"
BUILD = ROOT / "Build"
MAN = ROOT / "Manifests"
META = ROOT / "Metadata"

QUARANTINE_DIRS = {"Corrupted-Quarantine", "AppleDouble-Quarantine", "ZeroByte-Quarantine", "Legacy-FON"}
SYMBOLS_PACK_ID = "pack-essential-015"
SYMBOLS_KEYWORDS = ("symbol", "dingbat", "ornament", "wingding", "webding", "icon")


def scan_library(lib_dir=None, known_state_by_rel=None, validate=True):
    """
    Gathers all valid font files in Library/ with clean relative path and full path,
    enforcing strict path containment and binary font validation via validate_font_file.

    Args:
        lib_dir: Optional Path to Library directory (defaults to module LIB).
        known_state_by_rel: Optional dict mapping clean_rel -> (fid, state_rec) used as
            an incremental validation cache when size and mtime are unchanged.
            When None (default), validate_font_file is invoked on every discovered file.
        validate: Whether to run font validation (True by default).
    """
    target_lib = Path(lib_dir) if lib_dir is not None else LIB
    items = {}
    if not target_lib.exists():
        return items

    lib_resolved = target_lib.resolve()
    col_path_str = str(lib_resolved)
    skipped_invalid = 0

    for root, dirs, files in os.walk(col_path_str):
        dirs[:] = sorted(d for d in dirs if d not in QUARANTINE_DIRS)
        for fname in sorted(files):
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

            if validate:
                # Fast pre-checks (always enforced, even for cached entries)
                if fname.startswith("._"):
                    skipped_invalid += 1
                    continue
                if full_p.suffix.lower() not in SUPPORTED_EXTENSIONS:
                    skipped_invalid += 1
                    continue
                try:
                    st = full_p.stat()
                except OSError:
                    skipped_invalid += 1
                    continue
                if st.st_size == 0:
                    skipped_invalid += 1
                    continue

                # Skip re-parsing unchanged already-validated state files for fast incremental builds
                use_cached = False
                if known_state_by_rel is not None and clean_rel in known_state_by_rel:
                    _, cached_rec = known_state_by_rel[clean_rel]
                    if cached_rec.get("sizeBytes") == st.st_size and (
                        cached_rec.get("mtime") is None or st.st_mtime == cached_rec.get("mtime")
                    ):
                        use_cached = True

                if not use_cached:
                    val = validate_font_file(full_p)
                    if not val["valid"]:
                        skipped_invalid += 1
                        print(f"[!] Skipping invalid font ({val['status']}): {clean_rel} — {val['error']}")
                        continue

            items[clean_rel] = full_p

    if skipped_invalid:
        print(f"[*] Skipped {skipped_invalid} invalid/non-font file(s) during Library scan.")
    return items


def generate_unique_font_id(sha256_hex: str, clean_rel: str, existing_ids: set) -> str:
    """
    Generates a deterministic font ID that never collides when identical font
    binaries (same SHA-256) exist in two different folders.
    """
    base_id = f"font-{sha256_hex[:16]}"
    if base_id not in existing_ids:
        return base_id

    norm_rel = normalize_library_rel_path(clean_rel, with_library_prefix=False)
    combined = hashlib.sha256(f"{sha256_hex}:{norm_rel}".encode("utf-8")).hexdigest()[:16]
    cand = f"font-{combined}"
    counter = 2
    while cand in existing_ids:
        cand = f"font-{hashlib.sha256(f'{sha256_hex}:{norm_rel}:{counter}'.encode('utf-8')).hexdigest()[:16]}"
        counter += 1
    return cand


def _is_symbol_font_path(clean_rel: str) -> bool:
    low = clean_rel.lower()
    return any(kw in low for kw in SYMBOLS_KEYWORDS)


def select_target_package(clean_rel: str, packages_catalog: dict, package_file_map: dict, package_sizes: dict = None) -> str:
    """
    Deterministically assigns a newly added font to the most appropriate package
    instead of dumping every new font into the final Symbols pack (pack-essential-015).

    Selection priority:
      1. If another font from the exact same family (<Lang>/Families/<Family>/) is already
         in a package, assign to that same package to keep family variants together.
      2. If the font path indicates Symbols/Dingbats/Ornaments and SYMBOLS_PACK_ID exists,
         assign to SYMBOLS_PACK_ID.
      3. Find all packages currently hosting fonts of the same top-level language folder
         (excluding SYMBOLS_PACK_ID whenever non-symbols packages exist for that language).
      4. If no package currently hosts that language, consider all packages except
         SYMBOLS_PACK_ID (unless it is the only package).
      5. Pick the least-loaded candidate package (smallest byte size, then fewest fonts,
         then lexicographically first package ID).
    """
    cand_pids = sorted(packages_catalog.keys())
    if not cand_pids:
        raise ValueError("No packages defined in packages_catalog")
    if len(cand_pids) == 1:
        return cand_pids[0]

    norm_rel = normalize_library_rel_path(clean_rel, with_library_prefix=False)
    parts = norm_rel.split("/")
    lang = parts[0] if parts else "Latin"
    family_prefix = "/".join(parts[:3]) + "/" if len(parts) >= 3 else None

    # Build quick lookup from package_file_map (or packages_catalog['files'])
    pack_paths = {}
    for pid in cand_pids:
        entries = package_file_map.get(pid)
        if entries:
            pack_paths[pid] = [item[0] for item in entries]
        else:
            pack_paths[pid] = list(packages_catalog[pid].get("files", []))

    # 1. Keep family styles together in the same package
    if family_prefix:
        for pid in cand_pids:
            if any(p.startswith(family_prefix) for p in pack_paths[pid]):
                return pid

    # 2. Symbols/Dingbats fonts belong in the Symbols pack
    if _is_symbol_font_path(norm_rel) and SYMBOLS_PACK_ID in packages_catalog:
        return SYMBOLS_PACK_ID

    # 3. Packages already hosting the same top-level language
    lang_prefix = f"{lang}/"
    lang_matches = [
        pid for pid in cand_pids
        if any(p.startswith(lang_prefix) for p in pack_paths[pid])
    ]

    # Avoid dumping non-symbol fonts into SYMBOLS_PACK_ID when other packs exist
    non_sym_lang = [pid for pid in lang_matches if pid != SYMBOLS_PACK_ID]
    if non_sym_lang:
        pool = non_sym_lang
    elif lang_matches:
        # e.g., only SYMBOLS_PACK_ID had that language; still prefer non-symbol packs if empty/small?
        # Keep non-symbols if this is not a symbol font and other packs exist
        non_sym_all = [pid for pid in cand_pids if pid != SYMBOLS_PACK_ID]
        pool = non_sym_all if non_sym_all else lang_matches
    else:
        non_sym_all = [pid for pid in cand_pids if pid != SYMBOLS_PACK_ID]
        pool = non_sym_all if non_sym_all else cand_pids

    # 5. Choose the least-loaded package in the pool
    def _load_key(pid):
        if package_sizes is not None and pid in package_sizes:
            size_bytes = package_sizes[pid]
        else:
            size_bytes = packages_catalog[pid].get("uncompressedSizeBytes") or packages_catalog[pid].get("compressedSizeBytes") or 0
        font_count = len(package_file_map.get(pid, []))
        return (size_bytes, font_count, pid)

    return min(pool, key=_load_key)


def _rebuild_families_catalog(fonts_list: list, existing_families: list) -> list:
    """
    Rebuilds Metadata/families.json deterministically from Metadata/fonts.json
    while preserving existing familyIds for untouched families.
    """
    existing_ids = {}
    for fam in existing_families or []:
        fname = fam.get("familyName")
        if fname and fam.get("familyId"):
            existing_ids[fname] = fam["familyId"]

    grouped = {}
    for f in fonts_list:
        fam_name = f.get("familyName") or Path(f.get("fileName", "Unknown")).stem
        grouped.setdefault(fam_name, []).append(f)

    families_out = []
    for fam_name in sorted(grouped.keys()):
        members = grouped[fam_name]
        fam_id = existing_ids.get(fam_name)
        if not fam_id:
            fam_id = f"fam-{hashlib.sha256(fam_name.encode('utf-8')).hexdigest()[:16]}"

        collections_set = set()
        scripts_set = set()
        designer = None
        manufacturer = None
        is_variable = False
        primary_lang = members[0].get("primaryLanguage", "Latin")

        font_entries = []
        for m in members:
            for c in m.get("collections") or ["fontivo-essential"]:
                collections_set.add(c)
            for s in m.get("supportedScripts") or []:
                scripts_set.add(s)
            if not designer and m.get("designer"):
                designer = m["designer"]
            if not manufacturer and m.get("manufacturer"):
                manufacturer = m["manufacturer"]
            if m.get("isVariable"):
                is_variable = True

            font_entries.append({
                "id": m["id"],
                "subfamilyName": m.get("subfamilyName") or "Regular",
                "fullName": m.get("fullName") or fam_name,
                "postScriptName": m.get("postScriptName") or fam_name.replace(" ", ""),
                "weight": m.get("weight", 400),
                "style": m.get("style", "normal"),
                "format": m.get("format", "TTF"),
                "sizeBytes": m.get("sizeBytes", 0),
                "sha256": m.get("sha256", ""),
                "libraryPath": m.get("libraryRelativePath", ""),
                "packageId": m.get("packageId", ""),
            })

        families_out.append({
            "familyId": fam_id,
            "familyName": fam_name,
            "collections": sorted(collections_set) or ["fontivo-essential"],
            "primaryLanguage": primary_lang,
            "supportedScripts": sorted(scripts_set),
            "stylesCount": len(font_entries),
            "designer": designer,
            "manufacturer": manufacturer,
            "isVariable": is_variable,
            "fonts": font_entries,
        })

    return families_out


def _build_font_catalog_entry(fid, clean_rel, fp, sha, pkg_id, pkg_file_name, extracted_meta, lang, fallback_fam):
    """Creates a canonical Metadata/fonts.json entry for a newly added font."""
    stat_res = fp.stat()
    lib_rel = f"Library/{clean_rel}"
    fam_name = extracted_meta.get("familyName") or repair_mojibake(fallback_fam)
    return {
        "status": "valid",
        "id": fid,
        "originalPath": lib_rel,
        "fileName": fp.name,
        "format": extracted_meta.get("format") or fp.suffix[1:].upper(),
        "sizeBytes": stat_res.st_size,
        "sha256": sha,
        "collection": "Essential",
        "familyName": fam_name,
        "subfamilyName": extracted_meta.get("subfamilyName") or "Regular",
        "fullName": extracted_meta.get("fullName") or fam_name,
        "postScriptName": extracted_meta.get("postScriptName") or fam_name.replace(" ", ""),
        "version": extracted_meta.get("version"),
        "designer": extracted_meta.get("designer"),
        "manufacturer": extracted_meta.get("manufacturer"),
        "weight": extracted_meta.get("weight", 400),
        "width": extracted_meta.get("width", 5),
        "style": extracted_meta.get("style", "normal"),
        "isVariable": extracted_meta.get("isVariable", False),
        "variableAxes": extracted_meta.get("variableAxes", []),
        "isColorFont": extracted_meta.get("isColorFont", False),
        "glyphCount": extracted_meta.get("glyphCount", 0),
        "codepointCount": extracted_meta.get("codepointCount", 0),
        "supportedScripts": extracted_meta.get("supportedScripts", ["Latn"]),
        "primaryLanguage": lang,
        "isMultilingual": extracted_meta.get("isMultilingual", False),
        "license": extracted_meta.get("license") or {
            "status": "unknown",
            "name": "Custom / Unspecified License",
            "url": None,
            "copyright": None,
            "redistributable": None,
            "commercialUse": None,
            "evidence": "Metadata extracted during incremental build",
        },
        "libraryRelativePath": lib_rel,
        "collections": ["fontivo-essential"],
        "isCrossCollectionDuplicate": False,
        "sourcePaths": {},
        "packageId": pkg_id,
        "packageFileName": pkg_file_name,
        "isBaseCollection": True,
        "isAdditional": False,
        "mtime": stat_res.st_mtime,
    }


def run_build(root_dir=None, sync_docs=True):
    """
    Executes the incremental build engine against `root_dir` (defaults to repo ROOT).
    Returns the number of packages rebuilt (0 on no-op).
    """
    base_root = Path(root_dir) if root_dir is not None else ROOT
    lib_dir = base_root / "Library"
    build_dir = base_root / "Build"
    man_dir = base_root / "Manifests"
    meta_dir = base_root / "Metadata"

    print("=== Fontivo Unified Incremental Build Engine (v1.1.0) ===")
    state_mgr = BuildStateManager(base_root)
    state = state_mgr.data

    # Load existing catalogs
    with open(meta_dir / "packages.json", "r", encoding="utf-8") as f:
        packages_catalog = json.load(f)
    with open(meta_dir / "collections.json", "r", encoding="utf-8") as f:
        collections_catalog = json.load(f)

    fonts_json_path = meta_dir / "fonts.json"
    families_json_path = meta_dir / "families.json"
    existing_fonts_list = []
    if fonts_json_path.exists():
        with open(fonts_json_path, "r", encoding="utf-8-sig") as f:
            existing_fonts_list = json.load(f)
    existing_families_list = []
    if families_json_path.exists():
        with open(families_json_path, "r", encoding="utf-8-sig") as f:
            existing_families_list = json.load(f)

    # 1. Index state fonts strictly by normalized relative path inside Library/
    state_fonts_by_rel = {}
    lib_resolved = lib_dir.resolve()
    for fid, f in state["fonts"].items():
        lp = f.get("libraryRelativePath", "").replace("\\", "/").strip()
        if ".." in lp.split("/"):
            print(f"[!] Security warning: Traversal sequence in state font {fid}: {lp}. Skipping.")
            continue
        try:
            clean_rel = normalize_library_rel_path(lp, with_library_prefix=False)
        except ValueError:
            print(f"[!] Security warning: Invalid state path for {fid}: {lp}. Skipping.")
            continue
        if not clean_rel:
            clean_rel = f.get("fileName", "")
        resolved_fp = (lib_resolved / clean_rel).resolve()
        if not resolved_fp.is_relative_to(lib_resolved):
            print(f"[!] Security warning: State font {fid} resolves outside Library root: {resolved_fp}. Skipping.")
            continue
        state_fonts_by_rel[clean_rel] = (fid, f)

    # 2. Scan and validate current files on disk in Library/
    current_files = scan_library(lib_dir=lib_dir, known_state_by_rel=state_fonts_by_rel, validate=True)
    print(f"[*] Scanned Library/ on disk: {len(current_files):,} valid font files.")

    # 3. Compare against state keyed by normalized relative path
    added = []
    removed = []
    modified = []
    unchanged = []

    for clean_rel, fp in current_files.items():
        if clean_rel not in state_fonts_by_rel:
            sha = get_file_sha256(fp)
            added.append((clean_rel, fp, sha))
        else:
            fid, state_rec = state_fonts_by_rel[clean_rel]
            stat_res = fp.stat()
            if stat_res.st_size != state_rec["sizeBytes"]:
                sha = get_file_sha256(fp)
                modified.append((clean_rel, fp, sha, fid, state_rec))
            elif state_rec.get("mtime") is not None and stat_res.st_mtime == state_rec["mtime"]:
                unchanged.append((clean_rel, fid, state_rec))
            else:
                sha = get_file_sha256(fp)
                if sha != state_rec["sha256"]:
                    modified.append((clean_rel, fp, sha, fid, state_rec))
                else:
                    state_rec["mtime"] = stat_res.st_mtime
                    unchanged.append((clean_rel, fid, state_rec))

    # Check removals
    for clean_rel, (fid, state_rec) in state_fonts_by_rel.items():
        if clean_rel not in current_files:
            removed.append((clean_rel, fid, state_rec))

    print(f"[*] Change Detection: Added: {len(added)}, Removed: {len(removed)}, Modified: {len(modified)}, Unchanged: {len(unchanged):,}")

    if not added and not removed and not modified:
        print("[+] Repository is up-to-date. Zero packages rebuilt. Build state preserved.")
        return 0

    # 4. Identify dirty packages and track contents
    dirty_packages = set()
    package_file_map = {pid: [] for pid in packages_catalog.keys()}
    package_sizes = {
        pid: int(packages_catalog[pid].get("uncompressedSizeBytes") or 0)
        for pid in packages_catalog.keys()
    }

    # Index Metadata/fonts.json strictly by normalized relative path
    fonts_catalog_by_rel = {}
    for rec in existing_fonts_list:
        raw_rel = rec.get("libraryRelativePath") or rec.get("fileName") or ""
        try:
            c_rel = normalize_library_rel_path(raw_rel, with_library_prefix=False)
        except ValueError:
            continue
        fonts_catalog_by_rel[c_rel] = rec

    # Pre-fill with current unchanged state
    for clean_rel, fid, s_rec in unchanged:
        pid = s_rec["packageId"]
        full_p = current_files[clean_rel]
        package_file_map[pid].append((clean_rel, full_p, fid, s_rec["sha256"]))

    # Handle removals: mark package dirty, record tombstone, remove from state & fonts.json
    for clean_rel, fid, s_rec in removed:
        pid = s_rec["packageId"]
        dirty_packages.add(pid)
        package_sizes[pid] = max(0, package_sizes.get(pid, 0) - int(s_rec.get("sizeBytes", 0)))
        state_mgr.record_tombstone(fid, s_rec, reason="DELETED_FROM_LIBRARY")
        if fid in state["fonts"]:
            del state["fonts"][fid]
        fonts_catalog_by_rel.pop(clean_rel, None)
        print(f"  [-] Detected REMOVAL: {clean_rel} (Package: {pid}) -> Tombstone recorded.")

    # Handle modifications: mark package dirty, update state & fonts.json
    for clean_rel, fp, new_sha, fid, s_rec in modified:
        pid = s_rec["packageId"]
        dirty_packages.add(pid)
        old_size = int(s_rec.get("sizeBytes", 0))
        new_size = fp.stat().st_size
        package_sizes[pid] = max(0, package_sizes.get(pid, 0) - old_size) + new_size

        package_file_map[pid].append((clean_rel, fp, fid, new_sha))
        s_rec["sha256"] = new_sha
        s_rec["sizeBytes"] = new_size
        s_rec["mtime"] = fp.stat().st_mtime

        parts = clean_rel.split("/")
        lang = parts[0] if len(parts) > 0 else s_rec.get("primaryLanguage", "Latin")
        fallback_fam = parts[2] if len(parts) > 2 else (parts[1] if len(parts) > 1 else fp.stem)
        extracted = extract_font_metadata(fp, fallback_family=fallback_fam, fallback_language=lang)

        if clean_rel in fonts_catalog_by_rel:
            entry = fonts_catalog_by_rel[clean_rel]
            entry["sha256"] = new_sha
            entry["sizeBytes"] = new_size
            entry["mtime"] = s_rec["mtime"]
            entry["format"] = extracted.get("format", entry.get("format"))
            entry["glyphCount"] = extracted.get("glyphCount", entry.get("glyphCount", 0))
            entry["codepointCount"] = extracted.get("codepointCount", entry.get("codepointCount", 0))
            if extracted.get("familyName"):
                entry["familyName"] = extracted["familyName"]
                s_rec["familyName"] = extracted["familyName"]
            if extracted.get("fullName"):
                entry["fullName"] = extracted["fullName"]
            if extracted.get("subfamilyName"):
                entry["subfamilyName"] = extracted["subfamilyName"]
        else:
            pkg_fname = packages_catalog[pid]["fileName"]
            fonts_catalog_by_rel[clean_rel] = _build_font_catalog_entry(
                fid, clean_rel, fp, new_sha, pid, pkg_fname, extracted, lang, fallback_fam
            )

        print(f"  [*] Detected MODIFICATION: {clean_rel} (Package: {pid}) -> SHA: {new_sha[:16]}")

    # Handle additions: assign to balanced, category-appropriate package and avoid hash collisions
    existing_ids = set(state["fonts"].keys())
    for clean_rel, fp, new_sha in added:
        norm_rel = normalize_library_rel_path(clean_rel, with_library_prefix=False)
        if ".." in norm_rel.split("/") or not fp.resolve().is_relative_to(lib_resolved):
            raise ValueError(f"Security error: Invalid path for addition: {clean_rel}")

        target_pid = select_target_package(norm_rel, packages_catalog, package_file_map, package_sizes)
        new_fid = generate_unique_font_id(new_sha, norm_rel, existing_ids)
        existing_ids.add(new_fid)

        dirty_packages.add(target_pid)
        file_size = fp.stat().st_size
        package_sizes[target_pid] = package_sizes.get(target_pid, 0) + file_size
        package_file_map[target_pid].append((norm_rel, fp, new_fid, new_sha))

        parts = norm_rel.split("/")
        lang = parts[0] if len(parts) > 0 else "Latin"
        fallback_fam = parts[2] if len(parts) > 2 else (parts[1] if len(parts) > 1 else fp.stem)
        extracted = extract_font_metadata(fp, fallback_family=fallback_fam, fallback_language=lang)
        fam_display = extracted.get("familyName") or repair_mojibake(fallback_fam)

        state["fonts"][new_fid] = {
            "id": new_fid,
            "sha256": new_sha,
            "fileName": fp.name,
            "format": extracted.get("format") or fp.suffix[1:].upper(),
            "sizeBytes": file_size,
            "collection": "Essential",
            "packageId": target_pid,
            "libraryRelativePath": f"Library/{norm_rel}",
            "familyName": fam_display,
            "primaryLanguage": lang,
            "mtime": fp.stat().st_mtime,
            "isBaseCollection": True,
            "isAdditional": False,
        }

        pkg_fname = packages_catalog[target_pid]["fileName"]
        fonts_catalog_by_rel[norm_rel] = _build_font_catalog_entry(
            new_fid, norm_rel, fp, new_sha, target_pid, pkg_fname, extracted, lang, fallback_fam
        )
        print(f"  [+] Detected ADDITION: {norm_rel} -> Assigned to Package: {target_pid} (ID: {new_fid})")

    print(f"\n[*] Rebuilding {len(dirty_packages)} affected package(s): {sorted(list(dirty_packages))}")

    # 5. Rebuild ONLY dirty packages in Build/
    build_dir.mkdir(parents=True, exist_ok=True)
    for pid in sorted(list(dirty_packages)):
        pkg_meta = packages_catalog[pid]
        out_zip = build_dir / pkg_meta["fileName"]

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
        pkg_meta["files"] = sorted(item[0] for item in package_file_map[pid])

        state["packages"][pid] = {
            "id": pid,
            "fileName": pkg_meta["fileName"],
            "collectionId": "fontivo-essential",
            "version": pkg_meta["version"],
            "sha256": pkg_meta["sha256"],
            "fontCount": pkg_meta["fontCount"],
            "compressedSizeBytes": pkg_meta["compressedSizeBytes"],
            "uncompressedSizeBytes": pkg_meta["uncompressedSizeBytes"],
            "format": "zip",
        }

        print(f"[+] Rebuilt {pid} ({pkg_meta['fileName']}) in {t_build:.2f}s: "
              f"Version: {pkg_meta['version']}, {res['fontCount']} fonts, {res['compressedSizeBytes']/(1024*1024):.2f} MB, SHA: {res['sha256'][:16]}...")

    # 6. Bump library version and save State/build-state.json
    new_lib_version = state_mgr.bump_library_version(patch=True)
    state["libraryVersion"] = new_lib_version
    state["buildTimestamp"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    state["totalUniqueValidFonts"] = len(state["fonts"])
    state["totalPackages"] = len(packages_catalog)
    state_mgr.save_state()

    # 7. Atomically update Metadata/fonts.json and Metadata/families.json
    updated_fonts_list = [
        fonts_catalog_by_rel[rel]
        for rel in sorted(fonts_catalog_by_rel.keys())
    ]
    updated_families_list = _rebuild_families_catalog(updated_fonts_list, existing_families_list)
    actual_family_count = len(updated_families_list)

    atomic_write_json(fonts_json_path, updated_fonts_list, indent=2, ensure_ascii=False)
    atomic_write_json(families_json_path, updated_families_list, indent=2, ensure_ascii=False)

    # 8. Update Metadata/packages.json and Metadata/collections.json atomically
    atomic_write_json(meta_dir / "packages.json", packages_catalog, indent=2, ensure_ascii=False)

    collections_catalog["fontivo-essential"]["type"] = "base"
    collections_catalog["fontivo-essential"]["version"] = new_lib_version
    collections_catalog["fontivo-essential"]["fontCount"] = len(state["fonts"])
    collections_catalog["fontivo-essential"]["familyCount"] = actual_family_count
    collections_catalog["fontivo-essential"]["packageCount"] = len(packages_catalog)
    collections_catalog["fontivo-essential"]["totalCompressedSizeBytes"] = sum(p["compressedSizeBytes"] for p in packages_catalog.values())
    collections_catalog["fontivo-essential"]["totalUncompressedSizeBytes"] = sum(p["uncompressedSizeBytes"] for p in packages_catalog.values())
    collections_catalog["fontivo-essential"]["packageIds"] = sorted(packages_catalog.keys())

    atomic_write_json(meta_dir / "collections.json", collections_catalog, indent=2, ensure_ascii=False)

    # 9. Update Compact Manifest with dynamic family count (no hardcoded values)
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
            "totalFamilies": actual_family_count,
            "totalPackages": len(packages_catalog),
            "totalCompressedSizeBytes": sum(p["compressedSizeBytes"] for p in packages_catalog.values()),
            "totalUncompressedSizeBytes": sum(p["uncompressedSizeBytes"] for p in packages_catalog.values()),
        },
        "collections": collections_catalog,
        "packages": {
            pid: {
                "id": p["id"],
                "fileName": p["fileName"],
                "collectionId": p["collectionId"],
                "version": p["version"],
                "url": f"https://github.com/Ahmedfahmy8308/Fontivo-Fonts/releases/download/v{new_lib_version}/{p['fileName']}",
                "fontCount": p["fontCount"],
                "compressedSizeBytes": p["compressedSizeBytes"],
                "uncompressedSizeBytes": p["uncompressedSizeBytes"],
                "sha256": p["sha256"],
                "format": p.get("format", "zip"),
            }
            for pid, p in packages_catalog.items()
        },
    }
    atomic_write_json(man_dir / "manifest.json", compact_manifest, indent=2, ensure_ascii=False)

    # 10. Synchronize Documentation (README.md & GITHUB_PUBLISH_GUIDE.md) from manifest.json
    if sync_docs and base_root.resolve() == ROOT.resolve():
        sync_documentation()

    print(f"\n[+] Incremental build finished successfully: Library upgraded to v{new_lib_version} ({actual_family_count:,} families). {len(dirty_packages)} package(s) rebuilt.")
    return len(dirty_packages)


def main():
    return run_build(root_dir=ROOT, sync_docs=True)


if __name__ == "__main__":
    sys.exit(0 if main() is not None else 1)
