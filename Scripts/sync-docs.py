#!/usr/bin/env python3
"""
sync-docs.py — CLI wrapper delegating to canonical sync_docs.py.

Maintains backward compatibility for CLI invocations (`python Scripts/sync-docs.py`)
while keeping a single canonical implementation in `Scripts/sync_docs.py`.
"""

import sys
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from sync_docs import (  # noqa: E402
    GUIDE_PATH,
    MAN,
    PACKAGE_CATEGORIES,
    README_PATH,
    ROOT,
    SCRIPTS_DIR as _CANONICAL_SCRIPTS_DIR,
    sync_documentation,
)

__all__ = [
    "SCRIPTS_DIR",
    "ROOT",
    "MAN",
    "README_PATH",
    "GUIDE_PATH",
    "PACKAGE_CATEGORIES",
    "sync_documentation",
]

if __name__ == "__main__":
    success = sync_documentation()
    sys.exit(0 if success else 1)
