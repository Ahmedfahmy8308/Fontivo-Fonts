#!/usr/bin/env python3
"""
sync-app-cache.py
Generates the canonical Fontivo-App/app/core/data/fonts-cache.json
from the 11,207 validated fonts in Fontivo-Fonts.

Conforms strictly to FontRecord in Fontivo-App/app/core/src/server/state.rs.
"""

import os
import sys
import json
from pathlib import Path

def get_workspace_root() -> Path:
    current = Path(__file__).resolve().parent
    for p in [current, current.parent, current.parent.parent]:
        if (p / "Fontivo-Fonts").exists() and (p / "Fontivo-App").exists():
            return p
    # Fallback to current working directory
    cwd = Path.cwd().resolve()
    if (cwd / "Fontivo-Fonts").exists() and (cwd / "Fontivo-App").exists():
        return cwd
    return current.parent.parent

def generate_cache():
    root = get_workspace_root()
    fonts_json_path = root / "Fontivo-Fonts" / "Metadata" / "fonts.json"
    families_json_path = root / "Fontivo-Fonts" / "Metadata" / "families.json"
    output_cache_path = root / "Fontivo-App" / "app" / "core" / "data" / "fonts-cache.json"

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

    for idx, f in enumerate(raw_fonts):
        font_id = idx + 1
        raw_rel = (f.get("libraryRelativePath") or "").replace("\\", "/").strip()
        if ".." in raw_rel.split("/") or "../" in raw_rel:
            raise ValueError(f"Security error: Traversal sequence in libraryRelativePath: {raw_rel}")

        file_name = f.get("fileName") or os.path.basename(raw_rel or f"font-{font_id}.ttf")

        # Normalize relative path to 'Library/<Category>/Families/...'
        # Strip legacy Essential/ folder if present
        if raw_rel.startswith("Library/Essential/"):
            library_rel = "Library/" + raw_rel[len("Library/Essential/"):]
        elif raw_rel.startswith("Essential/"):
            library_rel = "Library/" + raw_rel[len("Essential/"):]
        elif raw_rel.startswith("Library/"):
            library_rel = raw_rel
        else:
            library_rel = "Library/" + raw_rel.lstrip("/")

        library_rel = library_rel.replace("\\", "/")

        # Check file existence on disk
        disk_path = root / "Fontivo-Fonts" / library_rel
        if not disk_path.exists():
            missing_files.append(str(disk_path))

        orig_path = f.get("originalPath", "")
        primary_lang = f.get("primaryLanguage", "")
        scripts = f.get("supportedScripts", [])

        # Categorization logic:
        # 1. Symbols and Dingbats
        # 2. Arabic Fonts (Arabic / Urdu / Persian / Arab script)
        # 3. English & Latin Fonts
        if "03 - Symbols and Dingbats" in orig_path or (primary_lang == "Unclassified" and "Symbols" in orig_path):
            category = "03 - Symbols and Dingbats"
        elif (
            "01 - Arabic Fonts" in orig_path
            or "Arabic Fonts" in orig_path
            or primary_lang in ("Arabic", "Urdu", "Persian")
            or "Arab" in scripts
        ):
            category = "01 - Arabic Fonts"
        else:
            category = "02 - English Fonts"

        category_counts[category] = category_counts.get(category, 0) + 1

        is_arabic = (
            category == "01 - Arabic Fonts"
            or "Arab" in scripts
            or primary_lang in ("Arabic", "Urdu", "Persian")
        )

        # SubCategory logic:
        # Preserve Diverse Arabic / Arabic Families when present, else use family name
        family_name = f.get("familyName") or file_name.rsplit(".", 1)[0]
        if category == "01 - Arabic Fonts":
            if "Diverse Arabic" in orig_path:
                sub_category = "Diverse Arabic"
            elif "Arabic Families" in orig_path:
                sub_category = "Arabic Families"
            else:
                sub_category = family_name
        elif category == "03 - Symbols and Dingbats":
            sub_category = family_name if family_name else "Symbols and Dingbats"
        else:
            sub_category = family_name

        font_format = (f.get("format") or "TTF").upper()
        format_counts[font_format] = format_counts.get(font_format, 0) + 1

        size_bytes = int(f.get("sizeBytes", 0))

        display_name = f.get("fullName") or family_name or file_name

        # Typographic style
        subfam = f.get("subfamilyName")
        if subfam and subfam.strip() and subfam.strip() != family_name.strip():
            style = subfam.strip()
        else:
            style = "Regular"

        post_script_name = f.get("postScriptName")
        if not post_script_name:
            post_script_name = (display_name or file_name.split(".")[0]).replace(" ", "")

        glyph_count = int(f.get("glyphCount") or 0)

        record = {
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
            "hasTashkeel": is_arabic,
            "hasExtendedArabic": is_arabic,
            "hasArabicNumerals": is_arabic,
            "hasWesternNumerals": True
        }
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

    print(f"\nMetrics Summary:")
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
    else:
        print(f"ALL {len(records):,} font file paths verified successfully on disk!")
        return True

if __name__ == "__main__":
    success = generate_cache()
    sys.exit(0 if success else 1)
