"""
Mojibake detection and safe display-name repair helpers for Fontivo Fonts.

Legacy Arabic/Persian/Urdu font binaries frequently store Windows-1256 (CP1256)
bytes in platform-1/platform-3 name tables that were mis-decoded as CP1252,
Latin-1, or MacRoman. This module detects and repairs those display strings
without ever renaming or altering physical paths on disk.
"""

import re
from typing import Optional

# Core Arabic alphabet letters (ء..غ and ف..ي), excluding rare/extended blocks
# that commonly collide with single accented Western letters.
def _is_core_arabic_letter(ch: str) -> bool:
    code = ord(ch)
    return (0x0621 <= code <= 0x063A) or (0x0641 <= code <= 0x064A)


def _is_arabic_char(ch: str) -> bool:
    code = ord(ch)
    return (
        (0x0621 <= code <= 0x063A)
        or (0x0640 <= code <= 0x0652)  # tatweel + fathatan..sukun
        or (0x0660 <= code <= 0x0669)  # Arabic-Indic digits
        or (0x067E <= code <= 0x06CC)  # Persian/Urdu extensions (پ چ ژ گ ک ی)
    )


def _has_existing_arabic(text: str) -> bool:
    return any(0x0600 <= ord(c) <= 0x06FF for c in text)


# Genuine Western European accented word pattern (e.g., "Café", "Sérif", "Müller", "Résumé")
_WESTERN_WORD_RE = re.compile(
    r"[A-Za-z][éèêëàâäîïôöùûüçñáíóúýœæÉÈÊËÀÂÄÎÏÔÖÙÛÜÇÑÁÍÓÚÝŒÆ][A-Za-z]"
)


def _score_candidate(original: str, decoded: str, non_ascii_orig: int) -> Optional[float]:
    """
    Scores a decoded candidate string. Returns None if the candidate does not
    confidently represent repaired Arabic/Persian/Urdu Mojibake.
    """
    if decoded == original:
        return None
    # Reject control characters or replacement characters introduced by bad decode
    if any(ord(c) < 0x20 and c not in ("\t", "\n", "\r") for c in decoded):
        return None
    if "\ufffd" in decoded:
        return None

    core_arabic = sum(1 for c in decoded if _is_core_arabic_letter(c))
    arabic_total = sum(1 for c in decoded if _is_arabic_char(c))

    # Require at least 2 core Arabic letters so single accented Western chars
    # (like Café, Müller, CoconÆ) are never misclassified.
    if core_arabic < 2:
        return None

    # Require a high ratio of original non-ASCII characters to convert into
    # valid Arabic characters.
    ratio = arabic_total / max(non_ascii_orig, 1)
    if ratio < 0.70:
        return None

    # Prefer candidates with more core Arabic letters and fewer leftover weird symbols
    leftover_non_arabic = sum(
        1 for c in decoded if ord(c) >= 128 and not _is_arabic_char(c)
    )
    if leftover_non_arabic > max(1, int(non_ascii_orig * 0.25)):
        return None

    return (core_arabic * 2.0) + ratio - (leftover_non_arabic * 1.5)


def repair_mojibake(text: Optional[str]) -> str:
    """
    Detects CP1252/Latin-1/MacRoman -> Windows-1256 Mojibake in display names
    and returns the repaired Unicode string. Returns the original string
    unchanged if it is not Mojibake or if decoding is ambiguous.
    """
    if not text or not isinstance(text, str):
        return text or ""

    # Pure ASCII or already contains Arabic script -> leave untouched
    non_ascii_orig = sum(1 for c in text if ord(c) >= 128)
    if non_ascii_orig < 2 or _has_existing_arabic(text):
        return text

    # Avoid false positives on genuine Western European accented words when
    # almost all characters are standard ASCII/Latin letters
    if _WESTERN_WORD_RE.search(text) and non_ascii_orig <= 3:
        return text

    best_decoded = text
    best_score = -1.0

    # Try CP1252 -> Windows-1256 first, then MacRoman -> Windows-1256, then Latin-1 -> Windows-1256
    for src_enc in ("cp1252", "mac_roman", "latin1"):
        try:
            raw_bytes = text.encode(src_enc)
            decoded = raw_bytes.decode("cp1256")
        except (UnicodeEncodeError, UnicodeDecodeError):
            continue

        score = _score_candidate(text, decoded, non_ascii_orig)
        if score is not None and score > best_score:
            best_score = score
            best_decoded = decoded

    return best_decoded


def is_mojibake(text: Optional[str]) -> bool:
    """Returns True if `text` is detected as repairable CP1252/MacRoman->CP1256 Mojibake."""
    if not text or not isinstance(text, str):
        return False
    return repair_mojibake(text) != text
