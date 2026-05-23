from __future__ import annotations

from pathlib import Path
import itertools

def ensure_extension(path: Path, ext: str) -> Path:
    ext = ext if ext.startswith(".") else f".{ext}"
    return path if path.suffix.lower() == ext.lower() else path.with_suffix(ext)


def unique_path(base: Path) -> Path:
    """
    If base exists, append ` (n)` before the suffix until unique.
    e.g., report.png -> report (1).png, report (2).png, ...
    """
    if not base.exists():
        return base
    stem, suffix = base.stem, base.suffix
    parent = base.parent
    for i in itertools.count(1):
        cand = parent / f"{stem} ({i}){suffix}"
        if not cand.exists():
            return cand
