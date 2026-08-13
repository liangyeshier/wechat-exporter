"""Runtime resource paths for source checkouts and frozen macOS apps."""
from __future__ import annotations

import os
import sys


def resource_path(*parts: str) -> str:
    """Return a bundled resource path under PyInstaller or the source root."""
    base = getattr(sys, "_MEIPASS", None)
    if not base:
        base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, *parts)


def is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))
