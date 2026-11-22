#!/usr/bin/env python3
"""
Fontivo-Fonts — Comprehensive Pipeline Unit & Integration Test Suite

Verifies all critical packaging, validation, incremental build, state lookup,
Mojibake repair, cache-sync alignment, and read-only release-check guarantees.
Runs cleanly both locally (with full Library/) and in CI (without Library/ or Build/).
"""

import importlib.util
import json
import os
import shutil
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

from fontTools.fontBuilder import FontBuilder
from fontTools.pens.ttGlyphPen import TTGlyphPen
from fontTools.ttLib import TTCollection

SCRIPTS_DIR = Path(__file__).resolve().parent
FONTIVO_FONTS_ROOT = SCRIPTS_DIR.parent
WORKSPACE_ROOT = FONTIVO_FONTS_ROOT.parent

if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import build as build_engine
from common.hasher import get_file_sha256
from common.mojibake import is_mojibake, repair_mojibake
from common.packager import build_package_archive, test_archive_integrity
from common.state import BuildStateManager, atomic_write_json
from common.validator import validate_font_file


def _load_script_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _make_minimal_ttf(out_path: Path, family: str = "TestFamily", style: str = "Regular"):
    """Generates a valid minimal TrueType font binary using fontTools.fontBuilder."""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    fb = FontBuilder(1000, isTTF=True)
    fb.setupGlyphOrder([".notdef", "A"])
    fb.setupCharacterMap({65: "A"})

    pen = TTGlyphPen(None)
    pen.moveTo((100, 0))
    pen.lineTo((100, 700))
    pen.lineTo((500, 700))
    pen.lineTo((500, 0))
    pen.closePath()
    glyph_a = pen.glyph()

    empty_pen = TTGlyphPen(None)
    glyph_notdef = empty_pen.glyph()

    fb.setupGlyf({".notdef": glyph_notdef, "A": glyph_a})
    fb.setupHorizontalMetrics({".notdef": (500, 0), "A": (600, 100)})
    fb.setupHorizontalHeader(ascent=800, descent=-200)
    fb.setupNameTable({
        "familyName": family,
        "styleName": style,
        "uniqueFontIdentifier": f"{family}-{style}",
        "fullName": f"{family} {style}",
        "version": "Version 1.000",
        "psName": f"{family}-{style}".replace(" ", ""),
    })
    fb.setupOS2()
    fb.setupPost()
    fb.save(str(out_path))
    return out_path


def _make_minimal_ttc(out_path: Path, families=("CollFamilyOne", "CollFamilyTwo")):
    """Generates a valid multi-face TrueType Collection (.ttc/.otc/.ttf-ttcf)."""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    ttc = TTCollection()
    with tempfile.TemporaryDirectory() as tmp:
        for idx, fam in enumerate(families):
            p = Path(tmp) / f"face_{idx}.ttf"
            _make_minimal_ttf(p, family=fam, style="Regular")
            from fontTools.ttLib import TTFont
            ttc.fonts.append(TTFont(str(p)))
        ttc.save(str(out_path))
        for f in ttc.fonts:
            f.close()
    return out_path


class TestTTCAndFontValidation(unittest.TestCase):
    def test_valid_ttf_and_ttc_and_otc_and_ttcf_with_ttf_ext(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_p = Path(tmp)
            ttf_p = _make_minimal_ttf(tmp_p / "Valid.ttf", "ValidTTF")
            ttc_p = _make_minimal_ttc(tmp_p / "Multi.ttc", ("FaceA", "FaceB"))
            otc_p = _make_minimal_ttc(tmp_p / "Multi.otc", ("FaceC", "FaceD"))
            ttcf_ttf_p = _make_minimal_ttc(tmp_p / "DisguisedCollection.ttf", ("FaceE", "FaceF"))

            res_ttf = validate_font_file(ttf_p)
            self.assertTrue(res_ttf["valid"], res_ttf)
            self.assertEqual(res_ttf["status"], "VALID")
            self.assertEqual(res_ttf["format"], "TTF")

            res_ttc = validate_font_file(ttc_p)
            self.assertTrue(res_ttc["valid"], res_ttc)
            self.assertEqual(res_ttc["status"], "VALID")
            self.assertEqual(res_ttc["format"], "TTC")
            self.assertEqual(res_ttc["metadata"]["faceCount"], 2)

            res_otc = validate_font_file(otc_p)
            self.assertTrue(res_otc["valid"], res_otc)
            self.assertEqual(res_otc["status"], "VALID")
            self.assertEqual(res_otc["format"], "OTC")

            res_disguised = validate_font_file(ttcf_ttf_p)
            self.assertTrue(res_disguised["valid"], res_disguised)
            self.assertEqual(res_disguised["status"], "VALID")
            self.assertEqual(res_disguised["format"], "TTC")
            self.assertEqual(res_disguised["metadata"]["faceCount"], 2)

    def test_invalid_files_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_p = Path(tmp)
            zero_p = tmp_p / "zero.ttf"
            zero_p.write_bytes(b"")

            apple_p = tmp_p / "._apple.ttf"
            apple_p.write_bytes(b"junk")

            corrupt_ttc = tmp_p / "corrupt.ttc"
            corrupt_ttc.write_bytes(b"ttcf\x00\x01\x00\x00BROKEN")

            txt_p = tmp_p / "readme.txt"
            txt_p.write_text("not a font", encoding="utf-8")

            self.assertEqual(validate_font_file(zero_p)["status"], "ZEROBYTE")
            self.assertEqual(validate_font_file(apple_p)["status"], "APPLEDOUBLE")
            self.assertEqual(validate_font_file(corrupt_ttc)["status"], "CORRUPTED")
            self.assertFalse(validate_font_file(corrupt_ttc)["valid"])
            self.assertEqual(validate_font_file(txt_p)["status"], "UNSUPPORTED")

    def test_real_library_ttc_collections_validate(self):
        """If the full Library/ is present locally, verify all real TTC & ttcf-TTF files pass."""
        lib = FONTIVO_FONTS_ROOT / "Library"
        if not lib.exists():
            self.skipTest("Local Library/ not present in CI")

        ttc_files = list(lib.rglob("*.ttc")) + list(lib.rglob("*.otc"))
        # Also include the two .ttf files with ttcf collection headers
        for rel in (
            "Arabic/Families/Diwan Thuluth/خط ديواني الثلث.ttf",
            "Arabic/Families/Waseem/خط وسيم عادي .ttf",
        ):
            cand = lib / rel
            if cand.exists():
                ttc_files.append(cand)

        self.assertGreaterEqual(len(ttc_files), 11, "Expected real TTC collections in Library/")
        for p in ttc_files:
            res = validate_font_file(p)
            self.assertTrue(res["valid"], f"Real collection failed validation: {p} -> {res}")


class TestScanLibraryValidation(unittest.TestCase):
    def test_scan_library_invokes_validate_font_file_and_skips_junk(self):
        with tempfile.TemporaryDirectory() as tmp:
            lib = Path(tmp) / "Library"
            good_ttf = _make_minimal_ttf(lib / "Latin" / "Families" / "Good" / "Good.ttf", "Good")
            good_ttc = _make_minimal_ttc(lib / "Latin" / "Families" / "GoodColl" / "GoodColl.ttc")

            bad_corrupt = lib / "Latin" / "Families" / "Bad" / "Corrupt.ttf"
            bad_corrupt.parent.mkdir(parents=True, exist_ok=True)
            bad_corrupt.write_bytes(b"NOT_A_VALID_FONT_BINARY_DATA")

            bad_zero = lib / "Latin" / "Families" / "Bad" / "Zero.ttf"
            bad_zero.write_bytes(b"")

            bad_apple = lib / "Latin" / "Families" / "Bad" / "._MacFork.ttf"
            bad_apple.write_bytes(b"appledouble")

            bad_junk = lib / "Latin" / "Families" / "Bad" / "Thumbs.db"
            bad_junk.write_bytes(b"windows_thumbs")

            with mock.patch.object(build_engine, "validate_font_file", wraps=validate_font_file) as spy:
                found = build_engine.scan_library(lib_dir=lib)
                self.assertGreaterEqual(spy.call_count, 3, "scan_library must call validate_font_file")

            self.assertIn("Latin/Families/Good/Good.ttf", found)
            self.assertIn("Latin/Families/GoodColl/GoodColl.ttc", found)
            self.assertNotIn("Latin/Families/Bad/Corrupt.ttf", found)
            self.assertNotIn("Latin/Families/Bad/Zero.ttf", found)
            self.assertNotIn("Latin/Families/Bad/._MacFork.ttf", found)
            self.assertNotIn("Latin/Families/Bad/Thumbs.db", found)
            self.assertEqual(len(found), 2)


class TestBalancedPackAssignment(unittest.TestCase):
    def test_assigns_by_category_and_load_not_dumping_into_015(self):
        packages_catalog = {
            "pack-essential-001": {
                "id": "pack-essential-001",
                "fileName": "fontivo-essential-001.zip",
                "uncompressedSizeBytes": 80_000_000,
                "files": ["Arabic/Families/Amiri/Amiri-Regular.ttf"],
            },
            "pack-essential-003": {
                "id": "pack-essential-003",
                "fileName": "fontivo-essential-003.zip",
                "uncompressedSizeBytes": 120_000_000,
                "files": ["Latin/Families/Roboto/Roboto-Regular.ttf"],
            },
            "pack-essential-006": {
                "id": "pack-essential-006",
                "fileName": "fontivo-essential-006.zip",
                "uncompressedSizeBytes": 45_000_000,
                "files": ["Latin/Families/Inter/Inter-Regular.ttf"],
            },
            "pack-essential-015": {
                "id": "pack-essential-015",
                "fileName": "fontivo-essential-015.zip",
                "uncompressedSizeBytes": 9_000_000,
                "files": ["Unclassified/Families/Symbols/Dingbats.ttf"],
            },
        }
        pkg_map = {pid: [] for pid in packages_catalog}

        # New Latin font -> least-loaded Latin pack (006), NOT 015
        latin_pid = build_engine.select_target_package(
            "Latin/Families/BrandNewSans/BrandNewSans-Regular.ttf", packages_catalog, pkg_map
        )
        self.assertEqual(latin_pid, "pack-essential-006")
        self.assertNotEqual(latin_pid, "pack-essential-015")

        # New Arabic font -> Arabic pack (001), NOT 015
        arabic_pid = build_engine.select_target_package(
            "Arabic/Families/BrandNewKufi/Kufi.ttf", packages_catalog, pkg_map
        )
        self.assertEqual(arabic_pid, "pack-essential-001")

        # New style for existing Roboto family -> stays with Roboto in pack 003
        roboto_bold_pid = build_engine.select_target_package(
            "Latin/Families/Roboto/Roboto-Bold.ttf", packages_catalog, pkg_map
        )
        self.assertEqual(roboto_bold_pid, "pack-essential-003")

        # Symbols font -> goes to pack-essential-015
        sym_pid = build_engine.select_target_package(
            "Unclassified/Families/MyDingbats/Icons.ttf", packages_catalog, pkg_map
        )
        self.assertEqual(sym_pid, "pack-essential-015")


class TestIncrementalBuildAndCollisions(unittest.TestCase):
    def _create_temp_repo(self, root: Path):
        lib = root / "Library"
        build_dir = root / "Build"
        meta = root / "Metadata"
        man = root / "Manifests"
        state_dir = root / "State"
        for d in (lib, build_dir, meta, man, state_dir):
            d.mkdir(parents=True, exist_ok=True)

        # Seed two initial fonts across two packs + a Symbols pack-essential-015
        ar_font = _make_minimal_ttf(lib / "Arabic" / "Families" / "AmiriTest" / "Amiri.ttf", "AmiriTest")
        la_font = _make_minimal_ttf(lib / "Latin" / "Families" / "RobotoTest" / "Roboto.ttf", "RobotoTest")
        sym_font = _make_minimal_ttf(lib / "Unclassified" / "Families" / "SymTest" / "Sym.ttf", "SymTest")

        p1 = build_package_archive(build_dir / "fontivo-essential-001.zip", [("Arabic/Families/AmiriTest/Amiri.ttf", ar_font)])
        p2 = build_package_archive(build_dir / "fontivo-essential-006.zip", [("Latin/Families/RobotoTest/Roboto.ttf", la_font)])
        p15 = build_package_archive(build_dir / "fontivo-essential-015.zip", [("Unclassified/Families/SymTest/Sym.ttf", sym_font)])

        ar_sha = get_file_sha256(ar_font)
        la_sha = get_file_sha256(la_font)
        sym_sha = get_file_sha256(sym_font)

        packages = {
            "pack-essential-001": {
                "id": "pack-essential-001", "fileName": "fontivo-essential-001.zip",
                "collectionId": "fontivo-essential", "version": "1.0.0",
                "files": ["Arabic/Families/AmiriTest/Amiri.ttf"], **p1, "format": "zip",
            },
            "pack-essential-006": {
                "id": "pack-essential-006", "fileName": "fontivo-essential-006.zip",
                "collectionId": "fontivo-essential", "version": "1.0.0",
                "files": ["Latin/Families/RobotoTest/Roboto.ttf"], **p2, "format": "zip",
            },
            "pack-essential-015": {
                "id": "pack-essential-015", "fileName": "fontivo-essential-015.zip",
                "collectionId": "fontivo-essential", "version": "1.0.0",
                "files": ["Unclassified/Families/SymTest/Sym.ttf"], **p15, "format": "zip",
            },
        }
        collections = {
            "fontivo-essential": {
                "id": "fontivo-essential", "name": "Fontivo Unified Typography",
                "type": "base", "version": "1.0.0", "fontCount": 3, "familyCount": 3,
                "packageCount": 3,
                "totalCompressedSizeBytes": p1["compressedSizeBytes"] + p2["compressedSizeBytes"] + p15["compressedSizeBytes"],
                "totalUncompressedSizeBytes": p1["uncompressedSizeBytes"] + p2["uncompressedSizeBytes"] + p15["uncompressedSizeBytes"],
                "packageIds": ["pack-essential-001", "pack-essential-006", "pack-essential-015"],
            }
        }
        state = {
            "schemaVersion": "1.0.0", "libraryVersion": "1.0.0", "metadataRevision": 1,
            "buildTimestamp": "2026-11-20T00:00:00Z", "totalUniqueValidFonts": 3, "totalPackages": 3,
            "packages": {pid: {k: v for k, v in pkg.items() if k != "files"} for pid, pkg in packages.items()},
            "collections": collections,
            "fonts": {
                f"font-{ar_sha[:16]}": {
                    "id": f"font-{ar_sha[:16]}", "sha256": ar_sha, "fileName": "Amiri.ttf", "format": "TTF",
                    "sizeBytes": ar_font.stat().st_size, "collection": "Essential", "packageId": "pack-essential-001",
                    "libraryRelativePath": "Library/Arabic/Families/AmiriTest/Amiri.ttf",
                    "familyName": "AmiriTest", "primaryLanguage": "Arabic", "mtime": ar_font.stat().st_mtime,
                },
                f"font-{la_sha[:16]}": {
                    "id": f"font-{la_sha[:16]}", "sha256": la_sha, "fileName": "Roboto.ttf", "format": "TTF",
                    "sizeBytes": la_font.stat().st_size, "collection": "Essential", "packageId": "pack-essential-006",
                    "libraryRelativePath": "Library/Latin/Families/RobotoTest/Roboto.ttf",
                    "familyName": "RobotoTest", "primaryLanguage": "Latin", "mtime": la_font.stat().st_mtime,
                },
                f"font-{sym_sha[:16]}": {
                    "id": f"font-{sym_sha[:16]}", "sha256": sym_sha, "fileName": "Sym.ttf", "format": "TTF",
                    "sizeBytes": sym_font.stat().st_size, "collection": "Essential", "packageId": "pack-essential-015",
                    "libraryRelativePath": "Library/Unclassified/Families/SymTest/Sym.ttf",
                    "familyName": "SymTest", "primaryLanguage": "Unclassified", "mtime": sym_font.stat().st_mtime,
                },
            },
            "tombstones": [],
        }
        fonts_meta = [
            {
                "status": "valid", "id": f"font-{ar_sha[:16]}", "fileName": "Amiri.ttf", "format": "TTF",
                "sizeBytes": ar_font.stat().st_size, "sha256": ar_sha, "familyName": "AmiriTest",
                "subfamilyName": "Regular", "fullName": "AmiriTest Regular", "postScriptName": "AmiriTest-Regular",
                "primaryLanguage": "Arabic", "supportedScripts": ["Arab"],
                "libraryRelativePath": "Library/Arabic/Families/AmiriTest/Amiri.ttf",
                "collections": ["fontivo-essential"], "packageId": "pack-essential-001",
            },
            {
                "status": "valid", "id": f"font-{la_sha[:16]}", "fileName": "Roboto.ttf", "format": "TTF",
                "sizeBytes": la_font.stat().st_size, "sha256": la_sha, "familyName": "RobotoTest",
                "subfamilyName": "Regular", "fullName": "RobotoTest Regular", "postScriptName": "RobotoTest-Regular",
                "primaryLanguage": "Latin", "supportedScripts": ["Latn"],
                "libraryRelativePath": "Library/Latin/Families/RobotoTest/Roboto.ttf",
                "collections": ["fontivo-essential"], "packageId": "pack-essential-006",
            },
            {
                "status": "valid", "id": f"font-{sym_sha[:16]}", "fileName": "Sym.ttf", "format": "TTF",
                "sizeBytes": sym_font.stat().st_size, "sha256": sym_sha, "familyName": "SymTest",
                "subfamilyName": "Regular", "fullName": "SymTest Regular", "postScriptName": "SymTest-Regular",
                "primaryLanguage": "Unclassified", "supportedScripts": ["Latn"],
                "libraryRelativePath": "Library/Unclassified/Families/SymTest/Sym.ttf",
                "collections": ["fontivo-essential"], "packageId": "pack-essential-015",
            },
        ]
        families_meta = build_engine._rebuild_families_catalog(fonts_meta, [])

        atomic_write_json(meta / "packages.json", packages)
        atomic_write_json(meta / "collections.json", collections)
        atomic_write_json(meta / "fonts.json", fonts_meta)
        atomic_write_json(meta / "families.json", families_meta)
        atomic_write_json(state_dir / "build-state.json", state)
        return root

    def test_incremental_updates_fonts_and_families_json_and_dynamic_family_count(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self._create_temp_repo(Path(tmp))

            # Add a brand new Latin font -> must update fonts.json, families.json, manifest.json (dynamic count=4), assign to 006 (not 015)
            new_font = _make_minimal_ttf(root / "Library" / "Latin" / "Families" / "NewFresh" / "NewFresh.ttf", "NewFresh")
            rebuilt = build_engine.run_build(root_dir=root, sync_docs=False)
            self.assertEqual(rebuilt, 1)

            fonts_json = json.loads((root / "Metadata" / "fonts.json").read_text("utf-8"))
            families_json = json.loads((root / "Metadata" / "families.json").read_text("utf-8"))
            manifest = json.loads((root / "Manifests" / "manifest.json").read_text("utf-8"))

            self.assertEqual(len(fonts_json), 4)
            self.assertEqual(len(families_json), 4)
            self.assertEqual(manifest["summary"]["totalFamilies"], 4)
            self.assertEqual(manifest["collections"]["fontivo-essential"]["familyCount"], 4)

            new_entry = [f for f in fonts_json if f["fileName"] == "NewFresh.ttf"][0]
            self.assertEqual(new_entry["packageId"], "pack-essential-006")
            self.assertNotEqual(new_entry["packageId"], "pack-essential-015")

            # Remove the font -> fonts.json & families.json decrement back to 3 dynamically
            new_font.unlink()
            rebuilt_del = build_engine.run_build(root_dir=root, sync_docs=False)
            self.assertEqual(rebuilt_del, 1)

            fonts_after = json.loads((root / "Metadata" / "fonts.json").read_text("utf-8"))
            families_after = json.loads((root / "Metadata" / "families.json").read_text("utf-8"))
            manifest_after = json.loads((root / "Manifests" / "manifest.json").read_text("utf-8"))
            self.assertEqual(len(fonts_after), 3)
            self.assertEqual(len(families_after), 3)
            self.assertEqual(manifest_after["summary"]["totalFamilies"], 3)

    def test_duplicate_hash_in_different_folders_no_collision(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self._create_temp_repo(Path(tmp))
            f_a = _make_minimal_ttf(root / "Library" / "Latin" / "Families" / "DupA" / "Shared.ttf", "DupShared")
            f_b = root / "Library" / "Latin" / "Families" / "DupB" / "Shared.ttf"
            f_b.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(f_a, f_b)

            self.assertEqual(get_file_sha256(f_a), get_file_sha256(f_b))

            rebuilt = build_engine.run_build(root_dir=root, sync_docs=False)
            self.assertEqual(rebuilt, 1)

            state = json.loads((root / "State" / "build-state.json").read_text("utf-8"))
            fonts_json = json.loads((root / "Metadata" / "fonts.json").read_text("utf-8"))

            # Both identical-hash files must be tracked independently (3 initial + 2 identical = 5)
            self.assertEqual(len(state["fonts"]), 5)
            self.assertEqual(len(fonts_json), 5)
            rel_paths = {f["libraryRelativePath"] for f in fonts_json}
            self.assertIn("Library/Latin/Families/DupA/Shared.ttf", rel_paths)
            self.assertIn("Library/Latin/Families/DupB/Shared.ttf", rel_paths)

            # Deleting DupA must leave DupB intact!
            f_a.unlink()
            build_engine.run_build(root_dir=root, sync_docs=False)
            state_after = json.loads((root / "State" / "build-state.json").read_text("utf-8"))
            fonts_after = json.loads((root / "Metadata" / "fonts.json").read_text("utf-8"))
            self.assertEqual(len(state_after["fonts"]), 4)
            self.assertEqual(len(fonts_after), 4)
            remaining_paths = {f["libraryRelativePath"] for f in fonts_after}
            self.assertNotIn("Library/Latin/Families/DupA/Shared.ttf", remaining_paths)
            self.assertIn("Library/Latin/Families/DupB/Shared.ttf", remaining_paths)


class TestFindFontExactMatching(unittest.TestCase):
    def test_exact_path_matching_and_no_ambiguous_suffix(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state_dir = root / "State"
            state_dir.mkdir(parents=True, exist_ok=True)
            atomic_write_json(state_dir / "build-state.json", {
                "schemaVersion": "1.0.0",
                "libraryVersion": "1.0.0",
                "fonts": {
                    "font-1": {"id": "font-1", "fileName": "Regular.ttf", "libraryRelativePath": "Library/Latin/Families/Alpha/Regular.ttf"},
                    "font-2": {"id": "font-2", "fileName": "Regular.ttf", "libraryRelativePath": "Library/Latin/Families/Beta/Regular.ttf"},
                    "font-3": {"id": "font-3", "fileName": "MyFont.ttf", "libraryRelativePath": "Library/Latin/Families/Gamma/MyFont.ttf"},
                    "font-4": {"id": "font-4", "fileName": "NotMyFont.ttf", "libraryRelativePath": "Library/Latin/Families/Delta/NotMyFont.ttf"},
                },
                "packages": {},
                "collections": {},
                "tombstones": [],
            })

            mgr = BuildStateManager(root)

            # 1. Exact ID
            self.assertEqual(mgr.find_font("font-2")["id"], "font-2")

            # 2. Exact normalized relative path (with/without Library/ and Windows slashes)
            self.assertEqual(mgr.find_font("Latin/Families/Beta/Regular.ttf")["id"], "font-2")
            self.assertEqual(mgr.find_font("Library/Latin/Families/Beta/Regular.ttf")["id"], "font-2")
            self.assertEqual(mgr.find_font(r"Latin\Families\Beta\Regular.ttf")["id"], "font-2")

            # 3. Non-existent folder with colliding basename must NOT return Alpha
            self.assertIsNone(mgr.find_font("Latin/Families/NonExistent/Regular.ttf"))

            # 4. Ambiguous bare basename must return None (refuse to guess)
            self.assertIsNone(mgr.find_font("Regular.ttf"))

            # 5. Unambiguous bare basename matches uniquely
            self.assertEqual(mgr.find_font("MyFont.ttf")["id"], "font-3")

            # 6. Partial suffix must NEVER match via endswith
            self.assertIsNone(mgr.find_font("Font.ttf"))
            self.assertIsNone(mgr.find_font("yFont.ttf"))


class TestDeterministicPackager(unittest.TestCase):
    def test_deterministic_zip_create_system_and_order(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_p = Path(tmp)
            f1 = tmp_p / "b.ttf"
            f2 = tmp_p / "a.ttf"
            f1.write_bytes(b"font-data-B" * 100)
            f2.write_bytes(b"font-data-A" * 100)

            z1 = tmp_p / "out1.zip"
            z2 = tmp_p / "out2.zip"

            r1 = build_package_archive(z1, [("Latin/Families/B/b.ttf", f1), ("Arabic/Families/A/a.ttf", f2)])
            r2 = build_package_archive(z2, [("Arabic/Families/A/a.ttf", f2), ("Latin/Families/B/b.ttf", f1)])

            self.assertEqual(r1["sha256"], r2["sha256"])
            self.assertEqual(z1.read_bytes(), z2.read_bytes())

            ok, err = test_archive_integrity(z1)
            self.assertTrue(ok, err)

            with zipfile.ZipFile(z1, "r") as zf:
                names = zf.namelist()
                self.assertEqual(names, sorted(names))
                for zi in zf.infolist():
                    self.assertEqual(zi.create_system, 0, "create_system must be 0 for cross-OS determinism")
                    self.assertEqual(zi.date_time, (2026, 11, 19, 0, 0, 0))
                    self.assertEqual(zi.external_attr, 0o644 << 16)
                    self.assertEqual(zi.extra, b"")


class TestPublishReleaseReadOnlyCheck(unittest.TestCase):
    def test_check_is_strictly_readonly_and_fails_on_drift(self):
        pub_mod = _load_script_module("publish_release", SCRIPTS_DIR / "publish-release.py")

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "Manifests").mkdir()
            (root / "Metadata").mkdir()
            (root / "Build").mkdir()

            sample_f = root / "sample.ttf"
            sample_f.write_bytes(b"sample")
            pkg_res = build_package_archive(root / "Build" / "fontivo-essential-001.zip", [("Latin/Families/S/sample.ttf", sample_f)])

            # Deliberately drifted manifest URL (wrong version v0.9.0 vs v1.0.0)
            drifted_manifest = {
                "schemaVersion": "1.0.0",
                "libraryVersion": "1.0.0",
                "metadataRevision": 1,
                "packageBaseUrl": "https://github.com/Ahmedfahmy8308/Fontivo-Fonts/releases/download/v0.9.0",
                "metadataBaseUrl": "https://raw.githubusercontent.com/Ahmedfahmy8308/Fontivo-Fonts/v0.9.0/Metadata",
                "summary": {
                    "totalUniqueFonts": 1,
                    "totalFamilies": 2,
                    "totalPackages": 1,
                    "totalCompressedSizeBytes": pkg_res["compressedSizeBytes"],
                    "totalUncompressedSizeBytes": pkg_res["uncompressedSizeBytes"],
                },
                "collections": {},
                "packages": {
                    "pack-essential-001": {
                        "id": "pack-essential-001",
                        "fileName": "fontivo-essential-001.zip",
                        "collectionId": "fontivo-essential",
                        "version": "1.0.0",
                        "url": "https://github.com/Ahmedfahmy8308/Fontivo-Fonts/releases/download/v0.9.0/fontivo-essential-001.zip",
                        "fontCount": 1,
                        "compressedSizeBytes": pkg_res["compressedSizeBytes"],
                        "uncompressedSizeBytes": pkg_res["uncompressedSizeBytes"],
                        "sha256": pkg_res["sha256"],
                        "format": "zip",
                    }
                },
            }
            man_path = root / "Manifests" / "manifest.json"
            atomic_write_json(man_path, drifted_manifest)
            atomic_write_json(root / "Metadata" / "families.json", [{"familyName": "F1"}, {"familyName": "F2"}])

            raw_before = man_path.read_bytes()
            rc_drift = pub_mod.main(["--check"], root_dir=root)
            raw_after = man_path.read_bytes()

            self.assertNotEqual(rc_drift, 0, "--check must exit non-zero on manifest URL drift")
            self.assertEqual(raw_before, raw_after, "--check must NEVER mutate manifest.json!")

            # Now fix URLs and verify --check passes cleanly and stays read-only
            drifted_manifest["packageBaseUrl"] = "https://github.com/Ahmedfahmy8308/Fontivo-Fonts/releases/download/v1.0.0"
            drifted_manifest["metadataBaseUrl"] = "https://raw.githubusercontent.com/Ahmedfahmy8308/Fontivo-Fonts/v1.0.0/Metadata"
            drifted_manifest["packages"]["pack-essential-001"]["url"] = (
                "https://github.com/Ahmedfahmy8308/Fontivo-Fonts/releases/download/v1.0.0/fontivo-essential-001.zip"
            )
            atomic_write_json(man_path, drifted_manifest)

            raw_valid_before = man_path.read_bytes()
            rc_ok = pub_mod.main(["--check"], root_dir=root)
            self.assertEqual(rc_ok, 0)
            self.assertEqual(raw_valid_before, man_path.read_bytes())
            self.assertFalse((root / "Build" / "RELEASE_NOTES.tmp.md").exists())

            # Dynamic family count in release notes (must be 2, never hardcoded 4,477)
            notes = pub_mod.generate_release_notes(
                "1.0.0",
                list(drifted_manifest["packages"].values()),
                manifest=drifted_manifest,
                meta_dir=root / "Metadata",
            )
            self.assertIn("across 2 typographic families", notes)
            self.assertNotIn("4,477", notes)


class TestMojibakeAndCacheSyncAlignment(unittest.TestCase):
    def test_mojibake_repair_cp1252_and_macroman_without_false_positives(self):
        # CP1252 -> Windows-1256
        self.assertEqual(repair_mojibake("ÇáÜÜÜÝÜÜÜÊÍ"), "الـــفـــتح")
        self.assertEqual(repair_mojibake("Shojae'ya font ÇáÔÌÇÚíÉ"), "Shojae'ya font الشجاعية")
        self.assertTrue(is_mojibake("ÏíæÇäí1"))
        self.assertEqual(repair_mojibake("ÏíæÇäí1"), "ديواني1")

        # MacRoman -> Windows-1256
        self.assertEqual(repair_mojibake("«·„Õ«—»2 „ÿÊ—"), "المحارب2 مطور")
        self.assertEqual(repair_mojibake("Â‘«„ ⁄«œÌ"), "هشام عادي")

        # Zero false positives on genuine Western / ASCII / already-Arabic strings
        for clean in ("Café", "Müller", "Résumé", "CoconÆ Next Arabic", "Roboto Regular", "خط عربي"):
            self.assertEqual(repair_mojibake(clean), clean, f"False positive on {clean!r}")
            self.assertFalse(is_mojibake(clean))

    def test_cache_sync_scripts_share_canonical_category_mapping(self):
        sync_mod = _load_script_module("sync_app_cache", SCRIPTS_DIR / "sync-app-cache.py")
        gen_script = WORKSPACE_ROOT / "Fontivo-App" / "scripts" / "generate-fonts-cache.py"

        samples = [
            {"originalPath": "Raw/01 - خطوط عربية/test.ttf", "primaryLanguage": "Latin", "supportedScripts": ["Latn"]},
            {"originalPath": "Raw/01 - Arabic Fonts/test.ttf", "primaryLanguage": "Latin", "supportedScripts": ["Latn"]},
            {"originalPath": "Raw/Arabic Fonts/test.ttf", "primaryLanguage": "Latin", "supportedScripts": ["Latn"]},
            {"originalPath": "Raw/03 - Symbols and Dingbats/sym.ttf", "primaryLanguage": "Unclassified", "supportedScripts": []},
            {"originalPath": "Raw/02 - English/Roboto.ttf", "primaryLanguage": "Latin", "supportedScripts": ["Latn"]},
        ]

        expected = [
            ("01 - Arabic Fonts", True),
            ("01 - Arabic Fonts", True),
            ("01 - Arabic Fonts", True),
            ("03 - Symbols and Dingbats", False),
            ("02 - English Fonts", False),
        ]

        for entry, exp in zip(samples, expected):
            self.assertEqual(sync_mod.classify_font_category(entry), exp)

        if gen_script.exists():
            gen_mod = _load_script_module("generate_fonts_cache", gen_script)
            for entry, exp in zip(samples, expected):
                self.assertEqual(gen_mod.classify_font_category(entry), exp)

        # Verify Mojibake displayName is repaired while physical path stays exact
        rec = sync_mod.build_cache_record({
            "fileName": "F#FATH1.TTF",
            "libraryRelativePath": "Library/Latin/Families/ÇáÜÜÜÝÜÜÜÊÍ/F#FATH1.TTF",
            "familyName": "ÇáÜÜÜÝÜÜÜÊÍ",
            "fullName": "ÇáÜÜÜÝÜÜÜÊÍ Regular",
            "primaryLanguage": "Arabic",
            "supportedScripts": ["Arab"],
            "format": "TTF",
            "sizeBytes": 12345,
        }, font_id=1)
        self.assertEqual(rec["path"], "Library/Latin/Families/ÇáÜÜÜÝÜÜÜÊÍ/F#FATH1.TTF")
        self.assertEqual(rec["family"], "الـــفـــتح")
        self.assertEqual(rec["displayName"], "الـــفـــتح Regular")

    def test_detect_typographic_features_from_cmap_and_metadata(self):
        sync_mod = _load_script_module("sync_app_cache", SCRIPTS_DIR / "sync-app-cache.py")

        with tempfile.TemporaryDirectory() as tmp:
            tmp_p = Path(tmp)
            # 1. Create a real TTF with Arabic tashkeel (U+064E), extended Arabic (U+067E), Arabic-Indic digit (U+0661), and no Western digits
            ar_ttf = tmp_p / "ArabicFeatureTest.ttf"
            fb = FontBuilder(1000, isTTF=True)
            fb.setupGlyphOrder([".notdef", "fatha", "peh", "one_ar"])
            fb.setupCharacterMap({0x064E: "fatha", 0x067E: "peh", 0x0661: "one_ar"})
            empty_pen = TTGlyphPen(None)
            g = empty_pen.glyph()
            fb.setupGlyf({".notdef": g, "fatha": g, "peh": g, "one_ar": g})
            fb.setupHorizontalMetrics({".notdef": (500, 0), "fatha": (300, 0), "peh": (500, 0), "one_ar": (500, 0)})
            fb.setupHorizontalHeader(ascent=800, descent=-200)
            fb.setupNameTable({"familyName": "ArFeat", "styleName": "Regular"})
            fb.setupOS2()
            fb.setupPost()
            fb.save(str(ar_ttf))

            feats_bin = sync_mod.detect_typographic_features(
                {"primaryLanguage": "Arabic", "supportedScripts": ["Arab"]},
                is_arabic=True,
                font_path=ar_ttf,
            )
            self.assertEqual(feats_bin, {
                "hasTashkeel": True,
                "hasExtendedArabic": True,
                "hasArabicNumerals": True,
                "hasWesternNumerals": False,
            })

            # 2. Metadata fallback differentiation (e.g. small Arabic font vs Persian/extended font)
            small_ar = sync_mod.detect_typographic_features(
                {"primaryLanguage": "Arabic", "supportedScripts": ["Arab"], "codepointCount": 80, "glyphCount": 110},
                is_arabic=True,
            )
            self.assertFalse(small_ar["hasTashkeel"])
            self.assertFalse(small_ar["hasExtendedArabic"])
            self.assertTrue(small_ar["hasArabicNumerals"])

            persian_ar = sync_mod.detect_typographic_features(
                {"primaryLanguage": "Persian", "supportedScripts": ["Arab", "Latn"], "codepointCount": 400, "glyphCount": 600},
                is_arabic=True,
            )
            self.assertTrue(persian_ar["hasTashkeel"])
            self.assertTrue(persian_ar["hasExtendedArabic"])
            self.assertTrue(persian_ar["hasArabicNumerals"])
            self.assertTrue(persian_ar["hasWesternNumerals"])


class TestUpdateMetadataValidationAndSyncDocsWrapper(unittest.TestCase):
    def test_update_handle_metadata_validates_keys_and_types(self):
        import update as update_mod

        # Allowed string, int, bool, list keys coerce and validate cleanly
        self.assertEqual(update_mod.validate_and_coerce_metadata_value("designer", "  Ahmed  "), "Ahmed")
        self.assertEqual(update_mod.validate_and_coerce_metadata_value("weight", "700"), 700)
        self.assertEqual(update_mod.validate_and_coerce_metadata_value("width", 5), 5)
        self.assertIs(update_mod.validate_and_coerce_metadata_value("isVariable", "true"), True)
        self.assertIs(update_mod.validate_and_coerce_metadata_value("isColorFont", "false"), False)
        self.assertEqual(
            update_mod.validate_and_coerce_metadata_value("supportedScripts", "Arab, Latn"),
            ["Arab", "Latn"],
        )

        # Disallowed or immutable keys must raise ValueError
        for bad_key in ("arbitraryKey", "__proto__", "sha256", "id", "packageId", "libraryRelativePath"):
            with self.assertRaises(ValueError):
                update_mod.validate_and_coerce_metadata_value(bad_key, "val")

        # Invalid value types / out-of-range values must raise ValueError
        with self.assertRaises(ValueError):
            update_mod.validate_and_coerce_metadata_value("weight", "not_an_int")
        with self.assertRaises(ValueError):
            update_mod.validate_and_coerce_metadata_value("weight", "9999")
        with self.assertRaises(ValueError):
            update_mod.validate_and_coerce_metadata_value("isVariable", "maybe")
        with self.assertRaises(ValueError):
            update_mod.validate_and_coerce_metadata_value("familyName", "../EvilTraversal")

    def test_sync_docs_wrapper_delegates_to_canonical_sync_docs(self):
        import sync_docs as canonical_sync
        wrapper_sync = _load_script_module("sync_docs_cli_wrapper", SCRIPTS_DIR / "sync-docs.py")
        self.assertIs(wrapper_sync.sync_documentation, canonical_sync.sync_documentation)
        self.assertEqual(wrapper_sync.PACKAGE_CATEGORIES, canonical_sync.PACKAGE_CATEGORIES)


if __name__ == "__main__":
    unittest.main(verbosity=2)
