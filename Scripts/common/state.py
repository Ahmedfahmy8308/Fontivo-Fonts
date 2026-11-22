import os
import json
import time
from pathlib import Path


def normalize_path_string(path_str: str) -> str:
    """
    Normalizes a path to strictly POSIX forward slashes ('/'),
    stripping any host-specific drive letters (e.g. C:) and leading slashes.
    Rejects traversal sequences ('..').
    """
    if not path_str:
        return ""
    s = str(path_str).replace("\\", "/").strip()
    # Strip Windows drive letters (e.g. C:, D:)
    if len(s) >= 2 and s[1] == ":" and s[0].isalpha():
        s = s[2:]
    while s.startswith("/"):
        s = s[1:]
    parts = [p for p in s.split("/") if p and p != "."]
    if ".." in parts or "../" in s:
        raise ValueError(f"Path traversal sequence detected in path: '{path_str}'")
    return "/".join(parts)


def normalize_library_rel_path(path_str: str, with_library_prefix: bool = False) -> str:
    """
    Normalizes a font path into either:
      - '<Category>/Families/<Family>/<File>' (with_library_prefix=False)
      - 'Library/<Category>/Families/<Family>/<File>' (with_library_prefix=True)
    Strips legacy 'Library/Essential/' and 'Essential/' prefixes cleanly.
    """
    norm = normalize_path_string(path_str)
    if norm.startswith("Library/Essential/"):
        clean = norm[len("Library/Essential/"):]
    elif norm.startswith("Essential/"):
        clean = norm[len("Essential/"):]
    elif norm.startswith("Library/"):
        clean = norm[len("Library/"):]
    else:
        clean = norm
    clean = clean.lstrip("/")
    if with_library_prefix:
        return f"Library/{clean}" if clean else "Library"
    return clean


def atomic_write_json(file_path, data, indent=2, ensure_ascii=False):
    """Atomically writes JSON to target path using a temp file and os.replace()."""
    target = Path(file_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(f"{target.suffix}.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=indent, ensure_ascii=ensure_ascii)
    os.replace(tmp, target)


def atomic_write_text(file_path, text, encoding="utf-8"):
    """Atomically writes text to target path using a temp file and os.replace()."""
    target = Path(file_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(f"{target.suffix}.tmp")
    with open(tmp, "w", encoding=encoding) as f:
        f.write(text)
    os.replace(tmp, target)


class BuildStateManager:
    def __init__(self, root_dir):
        self.root = Path(root_dir)
        self.lib_dir = self.root / "Library"
        self.build_dir = self.root / "Build"
        self.state_file = self.root / "State" / "build-state.json"
        self.man_dir = self.root / "Manifests"
        self.meta_dir = self.root / "Metadata"
        self.data = self._load_state()

    def _load_state(self):
        if not self.state_file.exists():
            return {
                "schemaVersion": "1.0.0",
                "libraryVersion": "1.0.0",
                "metadataRevision": 1,
                "buildTimestamp": "",
                "totalUniqueValidFonts": 0,
                "totalPackages": 0,
                "packages": {},
                "collections": {},
                "fonts": {},
                "tombstones": []
            }
        with open(self.state_file, "r", encoding="utf-8") as f:
            return json.load(f)

    def save_state(self):
        """Atomically saves build state via os.replace()."""
        atomic_write_json(self.state_file, self.data, indent=2, ensure_ascii=False)

    def get_font_by_id(self, font_id):
        return self.data["fonts"].get(font_id)

    def get_font_by_sha(self, sha256):
        for f in self.data["fonts"].values():
            if f.get("sha256") == sha256:
                return f
        return None

    def get_fonts_by_sha(self, sha256):
        """Returns all font records matching the given SHA-256 (supports identical files in different folders)."""
        return [f for f in self.data["fonts"].values() if f.get("sha256") == sha256]

    def get_font_by_rel_path(self, rel_path):
        """Finds a font record by exact normalized relative path (with or without 'Library/' prefix)."""
        if not rel_path:
            return None
        try:
            target_clean = normalize_library_rel_path(rel_path, with_library_prefix=False)
        except ValueError:
            return None
        for f in self.data["fonts"].values():
            try:
                f_clean = normalize_library_rel_path(f.get("libraryRelativePath", ""), with_library_prefix=False)
            except ValueError:
                continue
            if f_clean == target_clean:
                return f
        return None

    def find_font(self, identifier):
        """
        Unambiguously finds a font record using strict priority tiers:
          1. Exact font ID match (state dict key or record['id'])
          2. Exact normalized relative path match (with or without 'Library/' prefix)
          3. Unambiguous exact bare fileName match ONLY when identifier contains no path
             separator ('/' or '\\') AND matches exactly one font in state.

        Never performs ambiguous substring/suffix ('endswith') matching.
        """
        if not identifier:
            return None

        ident_str = str(identifier).strip()
        if not ident_str:
            return None

        # Tier 1: Exact font ID match
        if ident_str in self.data["fonts"]:
            return self.data["fonts"][ident_str]
        for f in self.data["fonts"].values():
            if f.get("id") == ident_str:
                return f

        # Tier 2: Exact normalized relative path match
        try:
            norm_ident = normalize_path_string(ident_str)
            clean_ident = normalize_library_rel_path(ident_str, with_library_prefix=False)
        except ValueError:
            return None

        for f in self.data["fonts"].values():
            raw_rel = f.get("libraryRelativePath", "")
            if not raw_rel:
                continue
            try:
                f_norm = normalize_path_string(raw_rel)
                f_clean = normalize_library_rel_path(raw_rel, with_library_prefix=False)
            except ValueError:
                continue
            if f_norm == norm_ident or f_clean == clean_ident:
                return f

        # Tier 3: Unambiguous bare fileName fallback (only if no directory separators were provided)
        if "/" not in norm_ident:
            matches = [
                f for f in self.data["fonts"].values()
                if f.get("fileName") == norm_ident
            ]
            if len(matches) == 1:
                return matches[0]
            # If len(matches) > 1, it is ambiguous — refuse to guess!
            return None

        return None

    def get_package(self, package_id):
        return self.data["packages"].get(package_id)

    def record_tombstone(self, font_id, metadata, reason="REMOVED"):
        raw_rel = metadata.get("libraryRelativePath") or metadata.get("fileName")
        rel = normalize_library_rel_path(raw_rel, with_library_prefix=True)

        raw_fname = metadata.get("fileName") or Path(rel).name
        file_name = normalize_path_string(raw_fname)
        if "/" in file_name:
            file_name = file_name.split("/")[-1]

        tombstone = {
            "fontId": font_id,
            "sha256": metadata.get("sha256"),
            "fileName": file_name,
            "relativePath": rel,
            "removedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "reason": reason,
            "lastPackageId": metadata.get("packageId")
        }
        self.data["tombstones"].append(tombstone)

    def bump_package_version(self, package_id):
        pkg = self.data["packages"].get(package_id)
        if not pkg:
            return
        v_parts = pkg.get("version", "1.0.0").split(".")
        v_parts[-1] = str(int(v_parts[-1]) + 1)
        pkg["version"] = ".".join(v_parts)

    def bump_library_version(self, patch=True):
        cur = self.data.get("libraryVersion", "1.0.0")
        parts = cur.split(".")
        if patch:
            parts[-1] = str(int(parts[-1]) + 1)
        else:
            parts[1] = str(int(parts[1]) + 1)
            parts[-1] = "0"
        self.data["libraryVersion"] = ".".join(parts)
        return self.data["libraryVersion"]

    def bump_metadata_revision(self):
        self.data["metadataRevision"] = self.data.get("metadataRevision", 1) + 1
        return self.data["metadataRevision"]
