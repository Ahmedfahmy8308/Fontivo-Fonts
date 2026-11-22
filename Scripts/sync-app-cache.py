#!/usr/bin/env python3
"""
sync-app-cache.py
Canonical generator for Fontivo-App/core/data/fonts-cache.json reading from
Fontivo-Fonts/Metadata/fonts.json.

Single source of truth for category mapping (also delegated to by
Fontivo-App/scripts/generate-fonts-cache.py) and Mojibake display-name repair.
Conforms strictly to FontRecord in Fontivo-App/core/src/server/state.rs.
"""

import os
import sys
import json
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from common.mojibake import repair_mojibake
from common.state import normalize_library_rel_path
from common.validator import COLLECTION_EXTENSIONS, _is_ttcf_collection
from fontTools.ttLib import TTFont, TTCollection

# Canonical category constants shared across Fontivo-Fonts and Fontivo-App
CATEGORY_ARABIC = "01 - Arabic Fonts"
CATEGORY_ENGLISH = "02 - English Fonts"
CATEGORY_SYMBOLS = "03 - Symbols and Dingbats"

ARABIC_PATH_MARKERS = (
    "01 - Arabic Fonts",
    "01 - خطوط عربية",
    "Arabic Fonts",
)
ARABIC_LANGUAGES = {"Arabic", "Urdu", "Persian"}

# Unicode codepoint sets for typographic feature detection
TASHKEEL_CODEPOINTS = set(range(0x064B, 0x0653)) | {0x0670} | set(range(0x0653, 0x0660))
EXTENDED_ARABIC_CODEPOINTS = (
    {0x067E, 0x0686, 0x0698, 0x06A9, 0x06AF, 0x06CC, 0x0679, 0x0688, 0x0691, 0x06BA, 0x06BE, 0x06C1, 0x06D2}
    | set(range(0x0750, 0x0780))
    | set(range(0x08A0, 0x0900))
)
ARABIC_INDIC_DIGIT_CODEPOINTS = set(range(0x0660, 0x066A)) | set(range(0x06F0, 0x06FA))
WESTERN_DIGIT_CODEPOINTS = set(range(0x0030, 0x003A))


def detect_typographic_features(font_entry: dict, is_arabic: bool = False, font_path=None) -> dict:
    """
    Computes diacritics (tashkeel), extended Arabic, Arabic-Indic numerals, and Western numerals
    support from explicit metadata flags, binary cmap inspection (when `font_path` is provided and exists),
    or intelligent script/codepoint metadata fallback.
    """
    explicit_keys = ("hasTashkeel", "hasExtendedArabic", "hasArabicNumerals", "hasWesternNumerals")
    if all(isinstance(font_entry.get(k), bool) for k in explicit_keys):
        return {k: bool(font_entry[k]) for k in explicit_keys}

    # 1. Inspect actual font binary cmap when path is available on disk
    if font_path is not None:
        p = Path(font_path)
        if p.exists() and p.is_file():
            handle = None
            try:
                ext = p.suffix.lower()
                is_coll = (ext in COLLECTION_EXTENSIONS) or _is_ttcf_collection(p)
                if is_coll:
                    handle = TTCollection(str(p), lazy=True)
                    face = handle.fonts[0] if handle.fonts else None
                else:
                    handle = TTFont(str(p), lazy=True)
                    face = handle
                if face is not None:
                    cmap = face.getBestCmap() or {}
                    if cmap:
                        cps = set(cmap.keys())
                        return {
                            "hasTashkeel": bool(cps & TASHKEEL_CODEPOINTS),
                            "hasExtendedArabic": bool(cps & EXTENDED_ARABIC_CODEPOINTS),
                            "hasArabicNumerals": bool(cps & ARABIC_INDIC_DIGIT_CODEPOINTS),
                            "hasWesternNumerals": bool(cps & WESTERN_DIGIT_CODEPOINTS),
                        }
            except Exception:
                pass
            finally:
                if handle is not None:
                    try:
                        handle.close()
                    except Exception:
                        pass

    # 2. Clean metadata-driven computation from scripts, language, codepointCount, and glyphCount
    scripts = set(font_entry.get("supportedScripts") or [])
    primary_lang = font_entry.get("primaryLanguage") or ""
    cp_count = int(font_entry.get("codepointCount") or 0)
    glyph_count = int(font_entry.get("glyphCount") or 0)
    has_arab_script = is_arabic or ("Arab" in scripts) or (primary_lang in ARABIC_LANGUAGES)
    has_latn_script = ("Latn" in scripts) or (primary_lang == "Latin") or bool(font_entry.get("isMultilingual"))

    # Extended Arabic is present in Urdu/Persian fonts or large Arabic glyph repertoires (> 300 glyphs / > 220 codepoints)
    has_tashkeel = font_entry.get("hasTashkeel")
    if not isinstance(has_tashkeel, bool):
        has_tashkeel = bool(has_arab_script and (cp_count == 0 or cp_count >= 120 or glyph_count >= 180))

    has_ext_arabic = font_entry.get("hasExtendedArabic")
    if not isinstance(has_ext_arabic, bool):
        has_ext_arabic = bool(
            primary_lang in {"Urdu", "Persian"}
            or (has_arab_script and (cp_count >= 220 or glyph_count >= 350))
        )

    has_arab_numerals = font_entry.get("hasArabicNumerals")
    if not isinstance(has_arab_numerals, bool):
        has_arab_numerals = bool(has_arab_script and (cp_count == 0 or cp_count >= 65 or glyph_count >= 90))

    has_west_numerals = font_entry.get("hasWesternNumerals")
    if not isinstance(has_west_numerals, bool):
        if primary_lang == "Unclassified" and not scripts:
            has_west_numerals = False
        else:
            has_west_numerals = bool(has_latn_script or cp_count == 0 or cp_count >= 50 or glyph_count >= 60)

    return {
        "hasTashkeel": has_tashkeel,
        "hasExtendedArabic": has_ext_arabic,
        "hasArabicNumerals": has_arab_numerals,
        "hasWesternNumerals": has_west_numerals,
    }


def get_workspace_root() -> Path:
    current = Path(__file__).resolve().parent
    for p in [current, current.parent, current.parent.parent]:
        if (p / "Fontivo-Fonts").exists() and (p / "Fontivo-App").exists():
            return p
    cwd = Path.cwd().resolve()
    if (cwd / "Fontivo-Fonts").exists() and (cwd / "Fontivo-App").exists():
        return cwd
    return current.parent.parent


def classify_font_category(font_entry: dict) -> tuple:
    """
    Canonical category classifier for Fontivo fonts.
    Maps legacy Arabic/English/Symbols path markers and language/script metadata
    to one consistent canonical category:
      - "01 - Arabic Fonts"
      - "02 - English Fonts"
      - "03 - Symbols and Dingbats"

    Returns:
        (category: str, is_arabic: bool)
    """
    orig_path = font_entry.get("originalPath") or ""
    primary_lang = font_entry.get("primaryLanguage") or ""
    scripts = font_entry.get("supportedScripts") or []

    if (
        "03 - Symbols and Dingbats" in orig_path
        or (primary_lang == "Unclassified" and "Symbols" in orig_path)
    ):
        category = CATEGORY_SYMBOLS
    elif (
        any(marker in orig_path for marker in ARABIC_PATH_MARKERS)
        or primary_lang in ARABIC_LANGUAGES
        or "Arab" in scripts
    ):
        category = CATEGORY_ARABIC
    else:
        category = CATEGORY_ENGLISH

    is_arabic = (
        category == CATEGORY_ARABIC
        or "Arab" in scripts
        or primary_lang in ARABIC_LANGUAGES
    )
    return category, is_arabic


def build_cache_record(font_entry: dict, font_id: int, font_path=None) -> dict:
    """
    Builds a single FontRecord dict from a Metadata/fonts.json entry.
    Safely repairs Mojibake in display names (family, displayName, subCategory, style)
    while keeping `path` and `name` exact to the physical file on disk.
    Computes diacritics & numeral support from cmap (if `font_path` is provided) or metadata.
    """
    raw_rel = (font_entry.get("libraryRelativePath") or "").replace("\\", "/").strip()
    if ".." in raw_rel.split("/") or "../" in raw_rel:
        raise ValueError(f"Security error: Traversal sequence in libraryRelativePath: {raw_rel}")

    file_name = font_entry.get("fileName") or os.path.basename(raw_rel or f"font-{font_id}.ttf")
    library_rel = normalize_library_rel_path(raw_rel, with_library_prefix=True)

    category, is_arabic = classify_font_category(font_entry)
    orig_path = font_entry.get("originalPath") or ""

    raw_family = font_entry.get("familyName") or file_name.rsplit(".", 1)[0]
    family_name = repair_mojibake(raw_family)

    if category == CATEGORY_ARABIC:
        if "Diverse Arabic" in orig_path or "خطوط عربية متنوعة" in orig_path:
            sub_category = "Diverse Arabic"
        elif "Arabic Families" in orig_path or "عائلات عربية" in orig_path:
            sub_category = "Arabic Families"
        else:
            sub_category = family_name
    elif category == CATEGORY_SYMBOLS:
        sub_category = family_name if family_name else "Symbols and Dingbats"
    else:
        sub_category = family_name

    font_format = (font_entry.get("format") or "TTF").upper()
    size_bytes = int(font_entry.get("sizeBytes", 0))

    raw_display = font_entry.get("fullName") or raw_family or file_name
    display_name = repair_mojibake(raw_display)

    subfam = font_entry.get("subfamilyName")
    repaired_subfam = repair_mojibake(subfam) if subfam else ""
    if repaired_subfam.strip() and repaired_subfam.strip() != family_name.strip():
        style = repaired_subfam.strip()
    else:
        style = "Regular"

    post_script_name = font_entry.get("postScriptName")
    if not post_script_name:
        post_script_name = (display_name or file_name.split(".")[0]).replace(" ", "")

    glyph_count = int(font_entry.get("glyphCount") or 0)
    features = detect_typographic_features(font_entry, is_arabic=is_arabic, font_path=font_path)

    return {
        "id": font_id,
        "name": file_name,
        "path": library_rel,
        "category": category,
        "subCategory": sub_category,
        "family": family_name,
        "format": font_format,
        "size": size_bytes,
        "isArabic": is_arabic,
        "isActive": False,
        "isFavorite": False,
        "displayName": display_name,
        "style": style,
        "postScriptName": post_script_name,
        "glyphCount": glyph_count,
        "hasTashkeel": features["hasTashkeel"],
        "hasExtendedArabic": features["hasExtendedArabic"],
        "hasArabicNumerals": features["hasArabicNumerals"],
        "hasWesternNumerals": features["hasWesternNumerals"],
    }


def generate_cache(root_dir=None, output_path=None, check_disk=True, inspect_binaries=False):
    root = Path(root_dir) if root_dir is not None else get_workspace_root()
    fonts_json_path = root / "Fontivo-Fonts" / "Metadata" / "fonts.json"
    output_cache_path = Path(output_path) if output_path is not None else (root / "Fontivo-App" / "core" / "data" / "fonts-cache.json")

    print(f"Workspace root: {root}")
    print(f"Reading metadata from: {fonts_json_path}")

    if not fonts_json_path.exists():
        raise FileNotFoundError(f"Cannot find {fonts_json_path}")

    with open(fonts_json_path, "r", encoding="utf-8-sig") as f:
        raw_fonts = json.load(f)

    print(f"Loaded {len(raw_fonts)} font entries from fonts.json")

    records = []
    category_counts = {}
    format_counts = {}
    missing_files = []

    for idx, f_entry in enumerate(raw_fonts):
        font_id = idx + 1
        raw_rel = (f_entry.get("libraryRelativePath") or "").replace("\\", "/").strip()
        library_rel = normalize_library_rel_path(raw_rel, with_library_prefix=True)
        disk_path = root / "Fontivo-Fonts" / library_rel
        bin_path = disk_path if (inspect_binaries and disk_path.exists()) else None

        record = build_cache_record(f_entry, font_id, font_path=bin_path)
        if check_disk:
            if not disk_path.exists():
                missing_files.append(str(disk_path))

        category_counts[record["category"]] = category_counts.get(record["category"], 0) + 1
        format_counts[record["format"]] = format_counts.get(record["format"], 0) + 1
        records.append(record)

    output_cache_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_cache = output_cache_path.with_suffix(".tmp.json")
    with open(tmp_cache, "w", encoding="utf-8") as out_f:
        json.dump(records, out_f, indent=2, ensure_ascii=False)
    os.replace(tmp_cache, output_cache_path)

    print(f"\nSuccessfully generated {len(records)} records in {output_cache_path}")
    print(f"File size: {output_cache_path.stat().st_size / 1024 / 1024:.2f} MB")
    print("\nBreakdown by Category:")
    for cat, cnt in sorted(category_counts.items()):
        print(f"  {cat}: {cnt}")
    print("\nBreakdown by Format:")
    for fmt, cnt in sorted(format_counts.items()):
        print(f"  {fmt}: {cnt}")

    arabic_total = sum(1 for r in records if r["isArabic"])
    english_total = sum(1 for r in records if not r["isArabic"] and "Symbols" not in r["category"])
    symbols_total = sum(1 for r in records if "Symbols" in r["category"])
    families_total = len(set(r["family"] for r in records))

    print("\nMetrics Summary:")
    print(f"  Total Fonts:    {len(records)}")
    print(f"  Arabic Fonts:   {arabic_total}")
    print(f"  English Fonts:  {english_total}")
    print(f"  Symbols Fonts:  {symbols_total}")
    print(f"  Total Families: {families_total}")
    print(f"  Missing files on disk: {len(missing_files)}")

    if missing_files:
        print(f"WARNING: {len(missing_files)} files do not exist on disk!")
        for m in missing_files[:5]:
            print(f"  Missing: {m}")
        return False

    print(f"ALL {len(records):,} font file paths verified successfully on disk!")
    return True


if __name__ == "__main__":
    success = generate_cache()
    sys.exit(0 if success else 1)
