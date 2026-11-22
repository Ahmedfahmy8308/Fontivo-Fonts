import os
import zipfile
import hashlib
from pathlib import Path
from .hasher import get_file_sha256

def build_package_archive(output_zip_path, files_to_pack, deterministic_date=(2026, 11, 19, 0, 0, 0)):
    """
    Builds a deterministic ZIP package from a list of tuples: (arcname, full_path).
    Returns:
        dict: {
            "compressedSizeBytes": int,
            "uncompressedSizeBytes": int,
            "sha256": str,
            "fontCount": int
        }
    """
    out_path = Path(output_zip_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    temp_zip_path = out_path.with_suffix(".tmp.zip")

    uncompressed_bytes = 0
    # Zip Slip defense: strictly validate and normalize all arcnames
    normalized_files = []
    for arc, fp in files_to_pack:
        norm_arc = str(arc).replace("\\", "/").strip()
        # Reject drive letters (e.g. C:)
        if len(norm_arc) >= 2 and norm_arc[1] == ":" and norm_arc[0].isalpha():
            raise ValueError(f"Zip Slip Defense: arcname cannot contain drive letter: '{arc}'")
        # Reject absolute paths / leading slashes
        if norm_arc.startswith("/"):
            raise ValueError(f"Zip Slip Defense: arcname cannot start with '/': '{arc}'")
        # Reject path traversal sequences
        parts = norm_arc.split("/")
        if ".." in parts or any(p == ".." for p in parts) or "../" in norm_arc:
            raise ValueError(f"Zip Slip Defense: arcname cannot contain '../' traversal sequences: '{arc}'")
        if not norm_arc:
            raise ValueError(f"Zip Slip Defense: arcname cannot be empty: '{arc}'")
        normalized_files.append((norm_arc, fp))

    sorted_files = sorted(normalized_files, key=lambda x: x[0])

    with zipfile.ZipFile(temp_zip_path, mode="w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
        for arcname, full_path in sorted_files:
            fp = Path(full_path)
            sz = fp.stat().st_size
            uncompressed_bytes += sz
            with open(fp, "rb") as src_f:
                data = src_f.read()
            zinfo = zipfile.ZipInfo(filename=arcname)
            zinfo.date_time = deterministic_date
            zinfo.compress_type = zipfile.ZIP_DEFLATED
            zinfo.create_system = 0
            zinfo.create_version = 20
            zinfo.extract_version = 20
            zinfo.internal_attr = 0
            zinfo.external_attr = 0o644 << 16
            zinfo.extra = b""
            zinfo.comment = b""
            zf.writestr(zinfo, data, compress_type=zipfile.ZIP_DEFLATED, compresslevel=9)

    # Test integrity before replacing
    with zipfile.ZipFile(temp_zip_path, "r") as zf:
        bad = zf.testzip()
        if bad is not None:
            temp_zip_path.unlink()
            raise RuntimeError(f"Corrupted entry in generated archive: {bad}")

    # Atomic replace via os.replace()
    os.replace(temp_zip_path, out_path)

    comp_sz = out_path.stat().st_size
    zip_sha = get_file_sha256(out_path)

    return {
        "compressedSizeBytes": comp_sz,
        "uncompressedSizeBytes": uncompressed_bytes,
        "sha256": zip_sha,
        "fontCount": len(sorted_files)
    }

def test_archive_integrity(zip_path):
    """Verifies that a ZIP file passes CRC, is readable, and contains no Zip Slip entries."""
    p = Path(zip_path)
    if not p.exists():
        return False, "File does not exist"
    try:
        with zipfile.ZipFile(p, "r") as zf:
            bad = zf.testzip()
            if bad:
                return False, f"CRC mismatch in {bad}"
            for entry in zf.namelist():
                norm = entry.replace("\\", "/").strip()
                if norm.startswith("/") or "../" in norm or ".." in norm.split("/"):
                    return False, f"Zip Slip vulnerability detected in entry: {entry}"
            return True, None
    except Exception as e:
        return False, str(e)
