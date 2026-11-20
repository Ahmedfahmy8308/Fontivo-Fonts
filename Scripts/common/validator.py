import os
from pathlib import Path
from fontTools.ttLib import TTFont, TTLibError

SUPPORTED_EXTENSIONS = {".ttf", ".otf", ".ttc"}

def validate_font_file(file_path):
    """
    Validates a font file using binary parsing and fontTools.
    Returns:
        dict: {
            "valid": bool,
            "status": "VALID" | "CORRUPTED" | "APPLEDOUBLE" | "ZEROBYTE" | "UNSUPPORTED",
            "format": "TTF" | "OTF" | "TTC" | "UNKNOWN",
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
        return {"valid": False, "status": "UNSUPPORTED", "format": ext[1:].upper(), "error": f"Unsupported font extension {ext}", "metadata": None}

    # 4. Binary parsing with fontTools
    try:
        font = TTFont(str(p), lazy=True)
        # Check required tables
        tables = set(font.keys())
        is_cff = "CFF " in tables or "CFF2" in tables
        fmt = "OTF" if is_cff else ("TTC" if ext == ".ttc" else "TTF")

        # Check basic table integrity
        if "head" not in tables and ext != ".ttc":
            return {"valid": False, "status": "CORRUPTED", "format": fmt, "error": "Missing 'head' table", "metadata": None}
        if "name" not in tables and ext != ".ttc":
            return {"valid": False, "status": "CORRUPTED", "format": fmt, "error": "Missing 'name' table", "metadata": None}

        # Extract basic metadata
        meta = {
            "format": fmt,
            "sizeBytes": sz,
            "glyphCount": font.get("maxp", None).numGlyphs if "maxp" in tables else None,
            "isVariable": "fvar" in tables,
            "hasColor": "COLR" in tables or "CBDT" in tables or "sbix" in tables
        }
        font.close()
        return {"valid": True, "status": "VALID", "format": fmt, "error": None, "metadata": meta}

    except TTLibError as e:
        return {"valid": False, "status": "CORRUPTED", "format": ext[1:].upper(), "error": f"FontTools parse error: {str(e)}", "metadata": None}
    except Exception as e:
        return {"valid": False, "status": "CORRUPTED", "format": ext[1:].upper(), "error": f"Unexpected parse error: {str(e)}", "metadata": None}
