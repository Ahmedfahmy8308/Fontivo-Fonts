import os
from pathlib import Path
from fontTools.ttLib import TTFont, TTCollection, TTLibError
from .mojibake import repair_mojibake

SUPPORTED_EXTENSIONS = {".ttf", ".otf", ".ttc", ".otc"}
COLLECTION_EXTENSIONS = {".ttc", ".otc"}


def _is_ttcf_collection(p: Path) -> bool:
    """Checks the first 4 bytes of a font file for the TrueType/OpenType Collection header ('ttcf')."""
    try:
        with open(p, "rb") as f:
            return f.read(4) == b"ttcf"
    except OSError:
        return False


def _validate_single_face(font: TTFont, face_label: str = "font"):
    """Validates required SFNT tables on a single TTFont face."""
    tables = set(font.keys())
    if "head" not in tables:
        return False, f"Missing 'head' table in {face_label}"
    if "name" not in tables:
        return False, f"Missing 'name' table in {face_label}"
    return True, None


def validate_font_file(file_path):
    """
    Validates a font file (.ttf, .otf, .ttc, .otc, including TTC collections with .ttf extension)
    using binary parsing and fontTools.

    Returns:
        dict: {
            "valid": bool,
            "status": "VALID" | "CORRUPTED" | "APPLEDOUBLE" | "ZEROBYTE" | "UNSUPPORTED" | "NOT_FOUND",
            "format": "TTF" | "OTF" | "TTC" | "OTC" | "FON" | "UNKNOWN",
            "error": str or None,
            "metadata": dict or None
        }
    """
    p = Path(file_path)
    if not p.exists() or not p.is_file():
        return {"valid": False, "status": "NOT_FOUND", "format": "UNKNOWN", "error": "File does not exist", "metadata": None}

    # 1. Zero-byte check
    sz = p.stat().st_size
    if sz == 0:
        return {"valid": False, "status": "ZEROBYTE", "format": "UNKNOWN", "error": "0-byte file", "metadata": None}

    # 2. AppleDouble check
    if p.name.startswith("._"):
        return {"valid": False, "status": "APPLEDOUBLE", "format": "UNKNOWN", "error": "AppleDouble OS X metadata fork", "metadata": None}

    # 3. Extension check
    ext = p.suffix.lower()
    if ext == ".fon":
        return {"valid": False, "status": "UNSUPPORTED", "format": "FON", "error": "Legacy Windows bitmap font format", "metadata": None}

    if ext not in SUPPORTED_EXTENSIONS:
        fmt_label = ext[1:].upper() if ext else "UNKNOWN"
        return {"valid": False, "status": "UNSUPPORTED", "format": fmt_label, "error": f"Unsupported font extension {ext}", "metadata": None}

    # 4. Binary parsing with fontTools (supports both single SFNT fonts and TTC/OTC collections)
    is_collection = (ext in COLLECTION_EXTENSIONS) or _is_ttcf_collection(p)
    try:
        if is_collection:
            with open(p, "rb") as stream:
                collection = TTCollection(stream, lazy=True)
                try:
                    if not collection.fonts:
                        return {
                            "valid": False,
                            "status": "CORRUPTED",
                            "format": "OTC" if ext == ".otc" else "TTC",
                            "error": "Font collection contains 0 faces",
                            "metadata": None,
                        }

                    has_cff = False
                    is_variable = False
                    has_color = False
                    for idx, face in enumerate(collection.fonts):
                        ok, err = _validate_single_face(face, face_label=f"collection face #{idx}")
                        if not ok:
                            return {
                                "valid": False,
                                "status": "CORRUPTED",
                                "format": "OTC" if ext == ".otc" else "TTC",
                                "error": err,
                                "metadata": None,
                            }
                        tables = set(face.keys())
                        if "CFF " in tables or "CFF2" in tables:
                            has_cff = True
                        if "fvar" in tables:
                            is_variable = True
                        if "COLR" in tables or "CBDT" in tables or "sbix" in tables:
                            has_color = True

                    if ext == ".otc":
                        fmt = "OTC"
                    elif ext == ".ttc":
                        fmt = "TTC"
                    else:
                        # Collection stored with .ttf/.otf extension
                        fmt = "OTC" if has_cff else "TTC"

                    first_face = collection.fonts[0]
                    first_tables = set(first_face.keys())
                    glyph_count = first_face["maxp"].numGlyphs if "maxp" in first_tables else None

                    meta = {
                        "format": fmt,
                        "sizeBytes": sz,
                        "glyphCount": glyph_count,
                        "faceCount": len(collection.fonts),
                        "isVariable": is_variable,
                        "hasColor": has_color,
                    }
                    return {"valid": True, "status": "VALID", "format": fmt, "error": None, "metadata": meta}
                finally:
                    collection.close()

        with open(p, "rb") as stream:
            font = TTFont(stream, lazy=True)
            try:
                tables = set(font.keys())
                is_cff = "CFF " in tables or "CFF2" in tables
                fmt = "OTF" if is_cff else "TTF"

                ok, err = _validate_single_face(font, face_label="font")
                if not ok:
                    return {"valid": False, "status": "CORRUPTED", "format": fmt, "error": err, "metadata": None}

                meta = {
                    "format": fmt,
                    "sizeBytes": sz,
                    "glyphCount": font["maxp"].numGlyphs if "maxp" in tables else None,
                    "isVariable": "fvar" in tables,
                    "hasColor": "COLR" in tables or "CBDT" in tables or "sbix" in tables,
                }
                return {"valid": True, "status": "VALID", "format": fmt, "error": None, "metadata": meta}
            finally:
                font.close()

    except TTLibError as e:
        return {"valid": False, "status": "CORRUPTED", "format": ext[1:].upper(), "error": f"FontTools parse error: {str(e)}", "metadata": None}
    except Exception as e:
        return {"valid": False, "status": "CORRUPTED", "format": ext[1:].upper(), "error": f"Unexpected parse error: {str(e)}", "metadata": None}


def _get_name_record(font: TTFont, *name_ids) -> str:
    """Safely extracts the best Unicode string for the given OpenType nameIDs."""
    if "name" not in font:
        return ""
    name_table = font["name"]
    for nid in name_ids:
        # Prefer Windows Unicode (platformID 3, platEncID 1/10) English, then any record
        best = None
        for rec in name_table.names:
            if rec.nameID != nid:
                continue
            try:
                val = rec.toUnicode().strip()
            except Exception:
                continue
            if not val:
                continue
            if rec.platformID == 3 and rec.langID == 0x409:
                return val
            if best is None:
                best = val
        if best:
            return best
    return ""


def _detect_scripts_from_cmap(font: TTFont):
    """Detects primary Unicode scripts and diacritic/numeral support from a font's best cmap table."""
    scripts = set()
    codepoints = set()
    has_tashkeel = False
    has_extended_arabic = False
    has_arabic_numerals = False
    has_western_numerals = False
    try:
        cmap = font.getBestCmap() or {}
        codepoints = set(cmap.keys())
        for cp in codepoints:
            if 0x0600 <= cp <= 0x06FF or 0x0750 <= cp <= 0x077F or 0xFB50 <= cp <= 0xFDFF or 0xFE70 <= cp <= 0xFEFF:
                scripts.add("Arab")
            elif 0x0041 <= cp <= 0x007A or 0x00C0 <= cp <= 0x024F:
                scripts.add("Latn")
            elif 0x0590 <= cp <= 0x05FF:
                scripts.add("Hebr")
            elif 0x0900 <= cp <= 0x0D7F:
                scripts.add("Deva")
            elif 0x4E00 <= cp <= 0x9FFF:
                scripts.add("Hani")

            if (0x064B <= cp <= 0x065F) or cp == 0x0670:
                has_tashkeel = True
            if cp in (0x067E, 0x0686, 0x0698, 0x06A9, 0x06AF, 0x06CC, 0x0679, 0x0688, 0x0691, 0x06BA, 0x06BE, 0x06C1, 0x06D2) or (0x0750 <= cp <= 0x077F) or (0x08A0 <= cp <= 0x08FF):
                has_extended_arabic = True
            if (0x0660 <= cp <= 0x0669) or (0x06F0 <= cp <= 0x06F9):
                has_arabic_numerals = True
            if 0x0030 <= cp <= 0x0039:
                has_western_numerals = True
    except Exception:
        pass
    features = {
        "hasTashkeel": has_tashkeel,
        "hasExtendedArabic": has_extended_arabic,
        "hasArabicNumerals": has_arabic_numerals,
        "hasWesternNumerals": has_western_numerals,
    }
    return sorted(scripts), len(codepoints), features


def extract_font_metadata(file_path, fallback_family=None, fallback_language="Latin"):
    """
    Extracts rich typographic metadata for Metadata/fonts.json from a single font
    or TTC/OTC collection, safely repairing Mojibake in display names without
    altering filenames or paths on disk.
    """
    p = Path(file_path)
    ext = p.suffix.lower()
    is_collection = (ext in COLLECTION_EXTENSIONS) or _is_ttcf_collection(p)

    handle = None
    try:
        if is_collection:
            handle = TTCollection(str(p), lazy=True)
            font = handle.fonts[0]
        else:
            handle = TTFont(str(p), lazy=True)
            font = handle

        tables = set(font.keys())
        is_cff = "CFF " in tables or "CFF2" in tables
        if is_collection:
            fmt = "OTC" if (ext == ".otc" or (ext not in COLLECTION_EXTENSIONS and is_cff)) else "TTC"
        else:
            fmt = "OTF" if is_cff else "TTF"

        raw_family = _get_name_record(font, 16, 1) or fallback_family or p.stem
        raw_subfamily = _get_name_record(font, 17, 2) or "Regular"
        raw_full = _get_name_record(font, 4) or f"{raw_family} {raw_subfamily}".strip()
        raw_ps = _get_name_record(font, 6) or raw_full.replace(" ", "")
        version = _get_name_record(font, 5) or None
        designer = repair_mojibake(_get_name_record(font, 9)) or None
        manufacturer = repair_mojibake(_get_name_record(font, 8)) or None
        copyright_str = _get_name_record(font, 0) or None
        license_desc = _get_name_record(font, 13) or None
        license_url = _get_name_record(font, 14) or None

        family_name = repair_mojibake(raw_family)
        subfamily_name = repair_mojibake(raw_subfamily)
        full_name = repair_mojibake(raw_full)

        weight = 400
        width = 5
        style = "normal"
        if "OS/2" in tables:
            try:
                os2 = font["OS/2"]
                weight = int(getattr(os2, "usWeightClass", 400) or 400)
                width = int(getattr(os2, "usWidthClass", 5) or 5)
                fs_sel = int(getattr(os2, "fsSelection", 0) or 0)
                if fs_sel & 0x01:
                    style = "italic"
                elif fs_sel & 0x200:
                    style = "oblique"
            except Exception:
                pass
        elif "italic" in subfamily_name.lower():
            style = "italic"

        is_variable = "fvar" in tables
        variable_axes = []
        if is_variable:
            try:
                variable_axes = [
                    {
                        "tag": ax.axisTag,
                        "min": ax.minValue,
                        "default": ax.defaultValue,
                        "max": ax.maxValue,
                    }
                    for ax in font["fvar"].axes
                ]
            except Exception:
                variable_axes = []

        is_color = any(t in tables for t in ("COLR", "CBDT", "sbix"))
        glyph_count = int(font["maxp"].numGlyphs) if "maxp" in tables else 0
        supported_scripts, codepoint_count, cmap_features = _detect_scripts_from_cmap(font)

        return {
            "format": fmt,
            "familyName": family_name,
            "subfamilyName": subfamily_name,
            "fullName": full_name,
            "postScriptName": raw_ps,
            "version": version,
            "designer": designer,
            "manufacturer": manufacturer,
            "weight": weight,
            "width": width,
            "style": style,
            "isVariable": is_variable,
            "variableAxes": variable_axes,
            "isColorFont": is_color,
            "glyphCount": glyph_count,
            "codepointCount": codepoint_count,
            "supportedScripts": supported_scripts or (["Arab"] if fallback_language in ("Arabic", "Urdu", "Persian") else ["Latn"]),
            "isMultilingual": len(supported_scripts) > 1,
            "hasTashkeel": cmap_features["hasTashkeel"],
            "hasExtendedArabic": cmap_features["hasExtendedArabic"],
            "hasArabicNumerals": cmap_features["hasArabicNumerals"],
            "hasWesternNumerals": cmap_features["hasWesternNumerals"],
            "license": {
                "status": "unknown",
                "name": license_desc or "Custom / Unspecified License",
                "url": license_url,
                "copyright": copyright_str,
                "redistributable": None,
                "commercialUse": None,
                "evidence": "Extracted from font name table",
            },
        }
    except Exception:
        fam = repair_mojibake(fallback_family or p.stem)
        return {
            "format": ext[1:].upper() if ext else "TTF",
            "familyName": fam,
            "subfamilyName": "Regular",
            "fullName": fam,
            "postScriptName": fam.replace(" ", ""),
            "version": None,
            "designer": None,
            "manufacturer": None,
            "weight": 400,
            "width": 5,
            "style": "normal",
            "isVariable": False,
            "variableAxes": [],
            "isColorFont": False,
            "glyphCount": 0,
            "codepointCount": 0,
            "supportedScripts": ["Arab"] if fallback_language in ("Arabic", "Urdu", "Persian") else ["Latn"],
            "isMultilingual": False,
            "license": {
                "status": "unknown",
                "name": "Custom / Unspecified License",
                "url": None,
                "copyright": None,
                "redistributable": None,
                "commercialUse": None,
                "evidence": "Fallback metadata",
            },
        }
    finally:
        if handle is not None:
            try:
                handle.close()
            except Exception:
                pass
