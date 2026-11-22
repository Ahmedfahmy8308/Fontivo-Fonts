#!/usr/bin/env python3
"""
sync_docs.py — Canonical Single Source of Truth Documentation Synchronizer

Reads the canonical Manifests/manifest.json and automatically updates:
1. Fontivo-Fonts/README.md
2. Fontivo-Fonts/GITHUB_PUBLISH_GUIDE.md

Ensures 100% mathematical consistency across:
- Package font counts
- Compressed and uncompressed sizes
- Total font counts and collection summaries
"""

import os
import sys
import json
import re
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent
ROOT = SCRIPTS_DIR.parent
MAN = ROOT / "Manifests"
README_PATH = ROOT / "README.md"
GUIDE_PATH = ROOT / "GITHUB_PUBLISH_GUIDE.md"

PACKAGE_CATEGORIES = {
    "pack-essential-001": ("Arabic Typography (Classic, Modern, Kufi, Naskh, Thuluth)", "Arabic Typography (Classic, Modern, Kufi, Naskh)"),
    "pack-essential-002": ("Latin Workhorses (Sans & Body Text — Part 1)", "Latin Workhorses (Sans & Body Text)"),
    "pack-essential-003": ("Latin Typography (Comprehensive Display & Sans — Part 2)", "Latin Typography (Comprehensive Display & Sans)"),
    "pack-essential-004": ("Latin & International Typography (Editorial & Sans — Part 3)", "Latin Typography (Contemporary & Editorial)"),
    "pack-essential-005": ("Latin Display, Geometric & Headline Typography", "Latin Display & Geometric"),
    "pack-essential-006": ("Latin Classic, Editorial & Modern Serif", "Latin Classic & Modern Serif"),
    "pack-essential-007": ("Latin Contemporary & Modern Typography", "Latin Modern & Editorial"),
    "pack-essential-008": ("Latin Extended, Headline & Display Suites", "Latin Extended & Headline"),
    "pack-essential-009": ("Latin Decorative, Script, Cursive & Brush", "Latin Decorative, Script & Brush"),
    "pack-essential-010": ("Urdu Nastaliq & Latin Typography", "Urdu Nastaliq & Latin Typography"),
    "pack-essential-011": ("Urdu Calligraphic & Persian Typographic Suites", "Urdu & Persian Typographic Suites"),
    "pack-essential-012": ("Persian, Indic & Hebrew Typography", "Persian, Indic & Hebrew Typography"),
    "pack-essential-013": ("Multilingual Suites & Global Scripts", "Multilingual Suites & Global Typography"),
    "pack-essential-014": ("Artistic, Display & Specialized Typography", "Artistic & Specialized Display Typography"),
    "pack-essential-015": ("Typography Icons, Symbols, Dingbats & Ornaments", "Symbols, Icons, Dingbats & Ornaments"),
}

def sync_documentation():
    manifest_file = MAN / "manifest.json"
    if not manifest_file.exists():
        print(f"[!] Error: {manifest_file} not found.")
        return False

    with open(manifest_file, "r", encoding="utf-8") as f:
        manifest = json.load(f)

    packages = manifest.get("packages", {})
    summary = manifest.get("summary", {})
    total_fonts = summary.get("totalUniqueFonts", sum(p.get("fontCount", 0) for p in packages.values()))
    total_comp_bytes = summary.get("totalCompressedSizeBytes", sum(p.get("compressedSizeBytes", 0) for p in packages.values()))
    total_comp_mb = total_comp_bytes / (1024 * 1024)

    sorted_pids = sorted(packages.keys())

    # --- 1. Generate README Table ---
    readme_rows = [
        "| Package Archive | Fonts | Category Focus |",
        "| :--- | :---: | :--- |",
    ]
    for pid in sorted_pids:
        p = packages[pid]
        fn = p["fileName"]
        fc = p["fontCount"]
        cat = PACKAGE_CATEGORIES.get(pid, ("General Typography", "General Typography"))[1]
        readme_rows.append(f"| `{fn}` | {fc:,} | {cat} |")

    readme_rows.append(f"| **Total** | **{total_fonts:,}** | **Curated Production Typography Collection ({total_comp_mb:.1f} MB)** |")
    readme_table_str = "\n".join(readme_rows)

    # --- 2. Update README.md ---
    if README_PATH.exists():
        with open(README_PATH, "r", encoding="utf-8") as f:
            readme_content = f.read()

        readme_pattern = re.compile(
            r"(\| Package Archive \| Fonts \| Category Focus \|.*?\n(?:\|.*?\n)+)",
            re.MULTILINE
        )
        if readme_pattern.search(readme_content):
            readme_updated = readme_pattern.sub(readme_table_str + "\n", readme_content)
            with open(README_PATH, "w", encoding="utf-8", newline="\n") as f:
                f.write(readme_updated)
            print(f"[+] Updated {README_PATH.name} with {len(packages)} packages ({total_fonts:,} total fonts).")
        else:
            print(f"[!] Warning: Could not locate table in {README_PATH.name}")

    # --- 3. Generate GITHUB_PUBLISH_GUIDE Table ---
    guide_rows = [
        "| Archive Package | Font Count | Compressed Size | Typographic Focus |",
        "| :--- | :---: | :---: | :--- |",
    ]
    for pid in sorted_pids:
        p = packages[pid]
        fn = p["fileName"]
        fc = p["fontCount"]
        cs_mb = p["compressedSizeBytes"] / (1024 * 1024)
        cat = PACKAGE_CATEGORIES.get(pid, ("General Typography", "General Typography"))[0]
        guide_rows.append(f"| `{fn}` | {fc:,} | {cs_mb:.1f} MB | {cat} |")

    guide_rows.append(f"| **Total** | **{total_fonts:,} Fonts** | **~{total_comp_mb:.1f} MB** | **Unified Curated Typography Collection** |")
    guide_table_str = "\n".join(guide_rows)

    # --- 4. Update GITHUB_PUBLISH_GUIDE.md ---
    if GUIDE_PATH.exists():
        with open(GUIDE_PATH, "r", encoding="utf-8") as f:
            guide_content = f.read()

        guide_pattern = re.compile(
            r"(\| Archive Package \| Font Count \| Compressed Size \| Typographic Focus \|.*?\n(?:\|.*?\n)+)",
            re.MULTILINE
        )
        if guide_pattern.search(guide_content):
            guide_updated = guide_pattern.sub(guide_table_str + "\n", guide_content)
            with open(GUIDE_PATH, "w", encoding="utf-8", newline="\n") as f:
                f.write(guide_updated)
            print(f"[+] Updated {GUIDE_PATH.name} with {len(packages)} packages ({total_fonts:,} total fonts).")
        else:
            print(f"[!] Warning: Could not locate table in {GUIDE_PATH.name}")

    return True

if __name__ == "__main__":
    success = sync_documentation()
    sys.exit(0 if success else 1)
