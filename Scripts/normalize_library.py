#!/usr/bin/env python3
"""
Fontivo Fonts - Library Audit & Normalization Script
Audits all font files in Fontivo-Fonts/Library/ (.ttf, .otf, .ttc, .otc):
  - Removes exact SHA-256 binary duplicates (keeps one canonical copy)
  - Removes 0-byte and AppleDouble (._*) files, quarantines corrupted/unsupported files
  - Extracts real internal metadata from the OpenType `name` table
  - Repairs CP1252/MacRoman/Latin-1 -> Windows-1256 mojibake
  - Transliterates Arabic/Persian/Urdu and non-ASCII characters to clean ASCII English
  - Normalizes family and subfamily names and strips noisy prefixes/characters
  - Reorganizes into Library/<Script>/Families/<CleanFamily>/<CleanFamily>-<CleanSubfamily><ext>
  - Removes all empty/legacy folders left behind
"""

import argparse
import os
import re
import shutil
import sys
import unicodedata
from collections import defaultdict
from pathlib import Path

from fontTools.ttLib import TTCollection, TTFont

SCRIPTS_DIR = Path(__file__).resolve().parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from common.hasher import get_file_sha256
from common.mojibake import repair_mojibake
from common.validator import (
    COLLECTION_EXTENSIONS,
    SUPPORTED_EXTENSIONS,
    _is_ttcf_collection,
    validate_font_file,
)

ROOT = SCRIPTS_DIR.parent
LIB = ROOT / "Library"
QUARANTINE = ROOT / "Quarantine"

WINDOWS_RESERVED_NAMES = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}

# Phonetic Arabic/Persian/Urdu -> ASCII transliteration table
ARABIC_CHAR_MAP = {
    "\u0621": "",      # ء hamza
    "\u0622": "A",     # آ alef madda
    "\u0623": "A",     # أ alef hamza above
    "\u0624": "W",     # ؤ waw hamza
    "\u0625": "I",     # إ alef hamza below
    "\u0626": "Y",     # ئ yeh hamza
    "\u0627": "A",     # ا alef
    "\u0628": "B",     # ب beh
    "\u0629": "h",     # ة teh marbuta
    "\u062A": "T",     # ت teh
    "\u062B": "Th",    # ث theh
    "\u062C": "J",     # ج jeem
    "\u062D": "H",     # ح hah
    "\u062E": "Kh",    # خ khah
    "\u062F": "D",     # د dal
    "\u0630": "Dh",    # ذ thal
    "\u0631": "R",     # ر reh
    "\u0632": "Z",     # ز zain
    "\u0633": "S",     # س seen
    "\u0634": "Sh",    # ش sheen
    "\u0635": "S",     # ص sad
    "\u0636": "D",     # ض dad
    "\u0637": "T",     # ط tah
    "\u0638": "Z",     # ظ zah
    "\u0639": "A",     # ع ain
    "\u063A": "Gh",    # غ ghain
    "\u0640": "",      # ـ tatweel
    "\u0641": "F",     # ف feh
    "\u0642": "Q",     # ق qaf
    "\u0643": "K",     # ك kaf
    "\u0644": "L",     # ل lam
    "\u0645": "M",     # م meem
    "\u0646": "N",     # ن noon
    "\u0647": "H",     # ه heh
    "\u0648": "W",     # و waw
    "\u0649": "A",     # ى alef maksura
    "\u064A": "Y",     # ي yeh
    # Persian / Urdu extensions
    "\u067E": "P",     # پ peh
    "\u0686": "Ch",    # چ tcheh
    "\u0698": "Zh",    # ژ jeh
    "\u06A9": "K",     # ک keheh
    "\u06AF": "G",     # گ gaf
    "\u06CC": "Y",     # ی farsi yeh
    "\u06BA": "N",     # ں noon ghunna
    "\u06BE": "H",     # ہ heh doachashmee
    "\u06C1": "H",     # ہ heh goal
    "\u06D2": "E",     # ے yeh barree
    "\u0679": "T",     # ٹ tteh
    "\u0688": "D",     # ڈ ddal
    "\u0691": "R",     # ڑ rreh
    # Digits
    **{chr(0x0660 + i): str(i) for i in range(10)},
    **{chr(0x06F0 + i): str(i) for i in range(10)},
}

# Latin ligatures/specials not decomposed by NFKD
LATIN_EXTRA_MAP = {
    "æ": "ae", "Æ": "AE",
    "œ": "oe", "Œ": "OE",
    "ß": "ss",
    "ø": "o", "Ø": "O",
    "ł": "l", "Ł": "L",
    "đ": "d", "Đ": "D",
}

# Arabic subfamily keywords -> canonical English
ARABIC_SUBFAMILY_MAP = {
    "عادي": "Regular",
    "قياسي": "Regular",
    "عريض مائل": "BoldItalic",
    "مائل عريض": "BoldItalic",
    "عريض جدا": "ExtraBold",
    "عريض": "Bold",
    "ثقيل": "Black",
    "سميك": "Bold",
    "غامق": "Bold",
    "مائل": "Italic",
    "خفيف": "Light",
    "رفيع": "Light",
    "متوسط": "Medium",
    "نحيف": "Thin",
}

SUBFAMILY_TOKEN_MAP = {
    "regular": "Regular",
    "reg": "Regular",
    "normal": "Regular",
    "roman": "Regular",
    "book": "Regular",
    "plain": "Regular",
    "standard": "Regular",
    "bold": "Bold",
    "bd": "Bold",
    "italic": "Italic",
    "ital": "Italic",
    "it": "Italic",
    "oblique": "Italic",
    "slanted": "Italic",
    "light": "Light",
    "lt": "Light",
    "extralight": "ExtraLight",
    "extra-light": "ExtraLight",
    "ultralight": "ExtraLight",
    "ultra-light": "ExtraLight",
    "thin": "Thin",
    "hairline": "Thin",
    "medium": "Medium",
    "med": "Medium",
    "semibold": "SemiBold",
    "semi-bold": "SemiBold",
    "demibold": "SemiBold",
    "demi-bold": "SemiBold",
    "demi": "SemiBold",
    "extrabold": "ExtraBold",
    "extra-bold": "ExtraBold",
    "ultrabold": "ExtraBold",
    "ultra-bold": "ExtraBold",
    "heavy": "Heavy",
    "black": "Black",
    "blk": "Black",
    "extrablack": "Black",
    "ultrablack": "Black",
    "condensed": "Condensed",
    "cond": "Condensed",
    "narrow": "Condensed",
    "expanded": "Expanded",
    "extended": "Expanded",
}

NOISY_PREFIX_RE = re.compile(r"^(?:\([A-Za-z0-9]{1,4}\)\s*)+")
INVALID_PATH_CHARS_RE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
MULTI_SPACE_RE = re.compile(r"\s+")
MULTI_HYPHEN_RE = re.compile(r"-{2,}")


def _is_garbage_name(text: str) -> bool:
    """Returns True if text is empty or mostly mojibake placeholders like '?? ?????' or '-- ----'."""
    if not text or not text.strip():
        return True
    s = text.strip()
    if "??" in s:
        return True
    alnum = sum(1 for c in s if c.isalnum())
    if alnum == 0:
        return True
    # Mostly question marks or hyphens
    q_or_dash = sum(1 for c in s if c in "?-_ ")
    if q_or_dash / len(s) > 0.75 and alnum < 3:
        return True
    return False


def _transliterate_arabic_word(word: str) -> str:
    """Transliterates a single Arabic/Persian/Urdu word to ASCII."""
    # Strip harakat/diacritics and tatweel
    cleaned = "".join(
        c for c in word
        if c != "\u0640" and not (0x064B <= ord(c) <= 0x065F) and ord(c) != 0x0670
    )
    if not cleaned:
        return ""
    out = []
    i = 0
    if cleaned.startswith("ال") and len(cleaned) > 2:
        out.append("Al-")
        i = 2
    while i < len(cleaned):
        ch = cleaned[i]
        if ch in ARABIC_CHAR_MAP:
            mapped = ARABIC_CHAR_MAP[ch]
            # Lowercase internal letters after first char of word
            if out and mapped and mapped[0].isalpha():
                mapped = mapped.lower()
            out.append(mapped)
        else:
            out.append(ch)
        i += 1
    return "".join(out)


def to_clean_ascii(text: str) -> str:
    """Repairs mojibake, transliterates Arabic/non-ASCII, and strips invalid path characters."""
    if not text:
        return ""

    text = repair_mojibake(text)

    # Replace Latin extra ligatures first
    for k, v in LATIN_EXTRA_MAP.items():
        text = text.replace(k, v)

    # Transliterate Arabic/Persian/Urdu tokens word-by-word
    tokens = re.split(r"(\s+)", text)
    trans_tokens = []
    for tok in tokens:
        if any(0x0600 <= ord(c) <= 0x06FF or 0x0750 <= ord(c) <= 0x077F or 0xFB50 <= ord(c) <= 0xFDFF or 0xFE70 <= ord(c) <= 0xFEFF for c in tok):
            # Split around punctuation inside token
            subparts = re.split(r"([^\w\u0600-\u06FF]+)", tok)
            trans_tokens.append("".join(_transliterate_arabic_word(sp) if any(0x0600 <= ord(c) <= 0x06FF for c in sp) else sp for sp in subparts))
        else:
            trans_tokens.append(tok)
    text = "".join(trans_tokens)

    # Decompose accented Latin and drop any remaining non-ASCII
    text = unicodedata.normalize("NFKD", text)
    text = text.encode("ascii", "ignore").decode("ascii")

    # Strip invalid path chars and noisy prefixes
    text = INVALID_PATH_CHARS_RE.sub(" ", text)
    text = NOISY_PREFIX_RE.sub("", text.strip())
    text = MULTI_HYPHEN_RE.sub("-", text)
    text = MULTI_SPACE_RE.sub(" ", text)
    # Strip leading/trailing punctuation/spaces/dots/hyphens
    text = text.strip(" \t\r\n.-_@#")
    return text


def clean_family_name(raw_family: str, fallback_stem: str) -> str:
    """Produces a clean, non-empty, Windows-safe ASCII family name."""
    cleaned = to_clean_ascii(raw_family)
    if _is_garbage_name(cleaned):
        cleaned = to_clean_ascii(fallback_stem)
    if _is_garbage_name(cleaned):
        cleaned = "UnnamedFont"

    # Limit length to avoid Windows MAX_PATH issues
    if len(cleaned) > 80:
        cleaned = cleaned[:80].rstrip(" .-_")
    if cleaned.upper() in WINDOWS_RESERVED_NAMES:
        cleaned = f"{cleaned}_Font"
    return cleaned or "UnnamedFont"


def normalize_subfamily(raw_sub: str, os2_weight: int = 400, is_italic: bool = False) -> str:
    """Normalizes subfamily string into a clean PascalCase ASCII token like Regular, Bold, BoldItalic."""
    repaired = repair_mojibake(raw_sub).strip()
    if repaired in ARABIC_SUBFAMILY_MAP:
        return ARABIC_SUBFAMILY_MAP[repaired]

    cleaned = to_clean_ascii(repaired)
    if _is_garbage_name(cleaned):
        if is_italic and os2_weight >= 700:
            return "BoldItalic"
        if os2_weight >= 700:
            return "Bold"
        if is_italic:
            return "Italic"
        return "Regular"

    # Tokenize on spaces/hyphens/underscores
    raw_tokens = [t for t in re.split(r"[\s_\-]+", cleaned) if t]
    joined_lower = "".join(t.lower() for t in raw_tokens)
    if joined_lower in SUBFAMILY_TOKEN_MAP:
        return SUBFAMILY_TOKEN_MAP[joined_lower]

    mapped_tokens = []
    for t in raw_tokens:
        tl = t.lower()
        if tl in SUBFAMILY_TOKEN_MAP:
            val = SUBFAMILY_TOKEN_MAP[tl]
            if val not in mapped_tokens:
                mapped_tokens.append(val)
        else:
            mapped_tokens.append(t[0].upper() + t[1:] if len(t) > 1 else t.upper())

    result = "".join(mapped_tokens)
    # Remove redundant "Regular" if combined with another style? Usually "BoldRegular" -> "Bold"
    if len(mapped_tokens) > 1 and "Regular" in mapped_tokens:
        result = "".join(m for m in mapped_tokens if m != "Regular")

    result = re.sub(r"[^A-Za-z0-9]", "", result)
    if not result:
        return "Regular"
    if len(result) > 40:
        result = result[:40]
    return result


def _pick_best_name(name_table, name_ids, reject_value: str = None) -> str:
    """
    Extracts the best clean string from OpenType name_table for the given nameIDs.
    Prefers Windows English (platformID=3, langID=0x0409) then Mac English (platformID=1, langID=0),
    skipping garbage/placeholder strings like '?? ?????' when a better record exists.
    """
    candidates = []
    for nid in name_ids:
        for rec in name_table.names:
            if rec.nameID != nid:
                continue
            try:
                val = rec.toUnicode().strip()
            except Exception:
                continue
            if not val:
                continue
            repaired = repair_mojibake(val)
            if reject_value and repaired.lower() == reject_value.lower():
                continue
            is_garbage = _is_garbage_name(repaired)
            is_ascii = all(ord(c) < 128 for c in repaired)

            # Priority score (lower is better)
            if rec.platformID == 3 and rec.langID == 0x0409:
                plat_rank = 0
            elif rec.platformID == 1 and rec.langID == 0:
                plat_rank = 1
            elif rec.langID == 0x0409:
                plat_rank = 2
            else:
                plat_rank = 3

            nid_rank = name_ids.index(nid)
            # Penalize garbage heavily, slightly prefer ASCII English over non-ASCII when both are valid
            score = (
                1 if is_garbage else 0,
                plat_rank,
                0 if is_ascii else 1,
                nid_rank,
            )
            candidates.append((score, repaired))

    if not candidates:
        return ""
    candidates.sort(key=lambda x: x[0])
    return candidates[0][1]


def extract_clean_names(font_path: Path):
    """Opens a font or collection and returns (CleanFamily, CleanSubfamily)."""
    ext = font_path.suffix.lower()
    is_collection = (ext in COLLECTION_EXTENSIONS) or _is_ttcf_collection(font_path)
    handle = None
    try:
        if is_collection:
            handle = TTCollection(str(font_path), lazy=True)
            font = handle.fonts[0]
        else:
            handle = TTFont(str(font_path), lazy=True)
            font = handle

        name_table = font["name"] if "name" in font else None
        raw_family = _pick_best_name(name_table, (16, 1)) if name_table else ""
        clean_fam = clean_family_name(raw_family, font_path.stem)

        raw_sub = _pick_best_name(name_table, (17, 2), reject_value=raw_family) if name_table else ""
        if not raw_sub and name_table:
            raw_sub = _pick_best_name(name_table, (2,))

        os2_weight = 400
        is_italic = False
        if "OS/2" in font:
            try:
                os2 = font["OS/2"]
                os2_weight = int(getattr(os2, "usWeightClass", 400) or 400)
                fs_sel = int(getattr(os2, "fsSelection", 0) or 0)
                is_italic = bool(fs_sel & 0x01)
            except Exception:
                pass

        clean_sub = normalize_subfamily(raw_sub, os2_weight=os2_weight, is_italic=is_italic)
        return clean_fam, clean_sub
    finally:
        if handle is not None:
            try:
                handle.close()
            except Exception:
                pass


def _safe_move(src: Path, dst: Path):
    """Moves src to dst, handling Windows case-only renames safely."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    if src == dst:
        return
    if os.name == "nt" and str(src).lower() == str(dst).lower():
        tmp = src.with_name(f".__tmp_rename_{src.name}")
        src.rename(tmp)
        tmp.rename(dst)
    else:
        shutil.move(str(src), str(dst))


def normalize_library(dry_run: bool = False):
    if not LIB.exists():
        print(f"[-] Library directory not found: {LIB}")
        return 1

    print(f"=== Fontivo Library Normalizer ({'DRY RUN' if dry_run else 'LIVE'}) ===")

    all_files = []
    for root, _, files in os.walk(LIB):
        for fname in sorted(files):
            all_files.append(Path(root) / fname)

    print(f"[*] Discovered {len(all_files):,} total files in Library/.")

    removed_appledouble = 0
    removed_zerobyte = 0
    quarantined_invalid = 0
    removed_duplicates = 0
    seen_hashes = {}
    valid_entries = []  # list of (src_path, script, clean_fam, clean_sub, ext)

    for p in all_files:
        # 1. AppleDouble
        if p.name.startswith("._"):
            removed_appledouble += 1
            if not dry_run:
                p.unlink(missing_ok=True)
            continue

        # 2. Zero-byte
        try:
            if p.stat().st_size == 0:
                removed_zerobyte += 1
                if not dry_run:
                    p.unlink(missing_ok=True)
                continue
        except OSError:
            continue

        # 3. Validate font binary
        val = validate_font_file(p)
        if not val["valid"]:
            quarantined_invalid += 1
            if not dry_run:
                q_dir = QUARANTINE / val["status"]
                q_dir.mkdir(parents=True, exist_ok=True)
                _safe_move(p, q_dir / p.name)
            continue

        # 4. Exact SHA-256 deduplication
        sha = get_file_sha256(p)
        if sha in seen_hashes:
            removed_duplicates += 1
            if not dry_run:
                p.unlink(missing_ok=True)
            continue
        seen_hashes[sha] = p

        # 5. Extract metadata & normalize names
        rel = p.relative_to(LIB)
        script = rel.parts[0] if len(rel.parts) > 1 else "Latin"
        clean_fam, clean_sub = extract_clean_names(p)
        ext = p.suffix.lower()
        valid_entries.append((p, script, clean_fam, clean_sub, ext))

    # Assign unique destination paths
    used_targets = set()
    plan = []  # (src_path, dst_path, script, clean_fam)
    families = set()
    sample_renames = []

    for src, script, clean_fam, clean_sub, ext in valid_entries:
        families.add((script, clean_fam))
        fam_dir = LIB / script / "Families" / clean_fam
        base_name = f"{clean_fam}-{clean_sub}"
        candidate = fam_dir / f"{base_name}{ext}"
        idx = 2
        while str(candidate).lower() in used_targets:
            candidate = fam_dir / f"{base_name}-{idx}{ext}"
            idx += 1
        used_targets.add(str(candidate).lower())
        plan.append((src, candidate, script, clean_fam))
        if src != candidate and len(sample_renames) < 15:
            sample_renames.append((str(src.relative_to(LIB)), str(candidate.relative_to(LIB))))

    moved_count = 0
    if not dry_run:
        # Two-phase move via staging dir to prevent any src->dst cycle or overwrite of unmoved files
        staging_dir = LIB / ".__normalize_staging__"
        staging_dir.mkdir(parents=True, exist_ok=True)
        staged = []
        for idx, (src, dst, _, _) in enumerate(plan):
            if src != dst:
                tmp = staging_dir / f"{idx}_{src.name}"
                _safe_move(src, tmp)
                staged.append((tmp, dst))
        for tmp, dst in staged:
            _safe_move(tmp, dst)
            moved_count += 1
        try:
            staging_dir.rmdir()
        except OSError:
            pass

        # Remove all empty directories left behind (bottom-up)
        removed_dirs = 0
        for root, dirs, files in os.walk(LIB, topdown=False):
            r_path = Path(root)
            if r_path == LIB:
                continue
            try:
                if not any(r_path.iterdir()):
                    r_path.rmdir()
                    removed_dirs += 1
            except OSError:
                pass
        print(f"[+] Moved/renamed {moved_count:,} files; removed {removed_dirs:,} empty directories.")
    else:
        moved_count = sum(1 for s, d, _, _ in plan if s != d)
        print(f"[DRY RUN] Would move/rename {moved_count:,} files.")

    print(f"[+] Fonts kept: {len(plan):,}")
    print(f"[+] Families count: {len(families):,}")
    print(f"[+] Duplicates removed: {removed_duplicates:,}")
    print(f"[+] Removed AppleDouble: {removed_appledouble:,}, Zero-byte: {removed_zerobyte:,}, Quarantined invalid: {quarantined_invalid:,}")
    print("[*] Sample renames:")
    for old_r, new_r in sample_renames[:10]:
        print(f"    {old_r}  ->  {new_r}")

    return 0


def main():
    parser = argparse.ArgumentParser(description="Normalize Fontivo-Fonts/Library/ using internal font metadata.")
    parser.add_argument("--dry-run", action="store_true", help="Preview changes without modifying disk.")
    args = parser.parse_args()
    return normalize_library(dry_run=args.dry_run)


if __name__ == "__main__":
    sys.exit(main())
