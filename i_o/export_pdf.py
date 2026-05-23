# i_o/export_pdf.py
from __future__ import annotations

from pathlib import Path
from typing import Optional
import os
import sys

from analyzer.fs import ensure_dir_root


# --- Public exception ---------------------------------------------------------

class PdfExportError(Exception):
    """Raised when exporting a scene to PDF fails."""


# --- Logo / watermark helpers -------------------------------------------------

def _resource_base() -> Path:
    """
    Base directory for export resources.

    - In a frozen executable (PyInstaller), use sys._MEIPASS.
    - In normal source execution, treat this file as living under i_o/
      and step up one level to the project root.
    """
    try:
        return Path(sys._MEIPASS)  # type: ignore[attr-defined]
    except Exception:
        return Path(__file__).resolve().parents[1]


def _resource_path(relative: str) -> Path:
    return _resource_base() / relative

def _ensure_parent_dir(path: Path) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except Exception as e:  # pragma: no cover
        raise PdfExportError(f"Failed to create parent directory for {path}: {e}") from e


def _is_subpath(child: Path, parent: Path) -> bool:
    try:
        child.resolve().relative_to(parent.resolve())
        return True
    except Exception:
        return False


def _resolve_repo_root(repo_root: Optional[Path]) -> Optional[Path]:
    """
    Normalize repo/workspace root:
      - accept file or directory; if file, use its parent
      - resolve() to absolute
      - tolerant of environments without analyzer.ensure_dir_root
    """
    if repo_root is None:
        return None
    try:
        p = ensure_dir_root(Path(repo_root))
    except Exception:
        p = Path(repo_root)
    if p.is_file():
        p = p.parent
    return p.resolve()


def _guard_safe_destination(out_path: Path, repo_root: Optional[Path], allow_repo_writes: Optional[bool]) -> None:
    """
    Enforce scan/store separation:
      - If repo_root is provided and out_path is inside repo_root but *not* under .pviz/,
        require explicit opt-in (kwarg or PVIZ_ALLOW_REPO_WRITES=1).
    """
    if repo_root is None:
        return
    out_path = Path(out_path).resolve()
    repo_root = Path(repo_root).resolve()

    if not _is_subpath(out_path, repo_root):
        return  # not within scan root => OK

    # Allow any path under <repo>/.pviz/
    if any(part == ".pviz" for part in out_path.parts):
        return

    if allow_repo_writes is None:
        allow_repo_writes = os.environ.get("PVIZ_ALLOW_REPO_WRITES", "") == "1"

    if not allow_repo_writes:
        raise PdfExportError(
            f"Refusing to write inside scan root:\n  {out_path}\n"
            f"Write under '<repo>/.pviz/' or pass allow_repo_writes=True "
            f"(or set PVIZ_ALLOW_REPO_WRITES=1) to override."
        )

