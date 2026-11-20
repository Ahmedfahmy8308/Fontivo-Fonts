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
    parts = s.split("/")
    if ".." in parts or any(p == ".." for p in parts) or "../" in s:
        raise ValueError(f"Path traversal sequence detected in path: '{path_str}'")
    return "/".join(parts)

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
            if f["sha256"] == sha256:
                return f
        return None

    def find_font(self, identifier):
        """Finds font record by ID, exact fileName, relative path, or path suffix."""
        if identifier in self.data["fonts"]:
            return self.data["fonts"][identifier]
        clean_id = str(identifier).replace("\\", "/")
        clean_id_name = Path(clean_id).name
        for fid, f in self.data["fonts"].items():
            if fid == identifier:
                return f
            if f.get("fileName") == identifier or f.get("fileName") == clean_id_name:
                return f
            rel = f.get("libraryRelativePath", "").replace("\\", "/")
            if rel == clean_id or rel.endswith(clean_id) or clean_id.endswith(rel):
                return f
            if rel.startswith("Library/"):
                sub = rel[len("Library/"):]
                if sub == clean_id or sub.endswith(clean_id):
                    return f
        return None

    def get_package(self, package_id):
        return self.data["packages"].get(package_id)

    def record_tombstone(self, font_id, metadata, reason="REMOVED"):
        raw_rel = metadata.get("libraryRelativePath") or metadata.get("fileName")
        rel = normalize_path_string(raw_rel)
        if rel.startswith("Library/Essential/"):
            rel = "Library/" + rel[len("Library/Essential/"):]
        elif not rel.startswith("Library/"):
            rel = f"Library/{rel.lstrip('/')}"

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
