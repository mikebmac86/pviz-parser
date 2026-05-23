# core/store_root.py
from __future__ import annotations
from pathlib import Path
from typing import Any
import os
import sys

# ---------------------------------------------------------------------------
# Diagnostics / logging hook
# ---------------------------------------------------------------------------

from diagnostics.logging import log_event as _log_event  # type: ignore[attr-defined]

def _log(msg: str, **fields: Any) -> None:
    """
    Lightweight debug helper wired into the central logging controller.

    Namespace: CORE:store_root
    """
    try:
        _log_event("CORE:store_root", msg=msg, **fields)
    except Exception:
        pass

# ---------------------------------------------------------------------------
# Per-user data base + store root
# ---------------------------------------------------------------------------

def _user_data_base() -> Path:
    """
    Return a per-user base directory suitable for storing application data,
    in a way that works both in dev and in packaged builds.

    Windows:   %LOCALAPPDATA% or %APPDATA% or HOME fallback
    macOS:     ~/Library/Application Support
    Linux/Unix: $XDG_DATA_HOME or ~/.local/share
    """
    if sys.platform.startswith("win"):
        base = os.getenv("LOCALAPPDATA") or os.getenv("APPDATA")
        if base:
            return Path(base)
        return Path.home()
    elif sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support"
    else:
        xdg = os.getenv("XDG_DATA_HOME")
        if xdg:
            return Path(xdg)
        return Path.home() / ".local" / "share"


def default_store_root() -> Path:
    """
    Per-user writable store root for Program Visualizer artifacts.

    New behavior:
      - Uses a per-user app-data base directory.
      - Creates (if needed) a subdirectory named after the app, with a
        '.pviz_store' child for artifacts.

    Example on Windows:
      %LOCALAPPDATA%/pviz/.pviz_store
    """
    base = _user_data_base()

    # Keep the app dir name stable and UI-independent.
    app_dir_name = "pviz"
    store_root = (base / app_dir_name / ".pviz_store").resolve()
    store_root.mkdir(parents=True, exist_ok=True)

    _log(
        "default_store_root_resolved",
        platform=sys.platform,
        base=str(base),
        store_root=str(store_root),
    )

    return store_root

