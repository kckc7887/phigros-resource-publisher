from __future__ import annotations

import sys
from pathlib import Path


def resource_root() -> Path:
    """Read-only assets root (bundled/phiTool). Frozen: _MEIPASS; else demo dir."""
    if getattr(sys, "frozen", False):
        return Path(getattr(sys, "_MEIPASS")).resolve()
    return Path(__file__).resolve().parent.parent


def writable_root() -> Path:
    """Writable root for work/ and config.json. Frozen: beside exe; else demo dir."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent
