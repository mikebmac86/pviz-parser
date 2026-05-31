# core/workspace_core.py
from __future__ import annotations

from pathlib import Path
from typing import Iterable, List, Optional, Sequence

from analyzer import ensure_dir_root, merge_includes
from adapters.canonical import to_posix


def normalize_root(p: Path | str) -> Path:
    """
    Normalize a user-provided path into a canonical workspace root:

      • coerce to Path
      • ensure directory form (file -> parent)
      • ensure_dir_root() for robustness
      • resolve() to an absolute, normalized path

    This is the core, UI-free equivalent of the previous _norm_root helper.
    """
    # First pass through ensure_dir_root for robust handling of strings, etc.
    root = ensure_dir_root(p)

    # If ensure_dir_root still points at a file, treat its parent as the root.
    if root.is_file():
        root = root.parent

    # Final normalization to an absolute path
    return root.resolve()


def to_posix_str(p: Path | str) -> str:
    """
    Convert a path-like into a POSIX-style string (forward slashes only).

    This is the core, UI-free equivalent of the previous _posix_str helper
    and ensures that workspace roots are stored in a stable, cross-platform
    string form.
    """
    return to_posix(p)


def infer_root_from_files(paths: Iterable[str]) -> Optional[Path]:
    """
    Compute a reasonable default root for a set of absolute file paths.

    Heuristic:
      • Take the parent directory of each file
      • Use Path.commonpath() to find a shared ancestor
      • Return that directory as a Path, or None if not resolvable

    The caller is responsible for converting the result to POSIX string form
    and/or further normalizing with normalize_root() if desired.
    """
    # Normalize to absolute paths first, but do not fail hard if resolution breaks.
    path_list: List[str] = [str(Path(x).resolve()) for x in (paths or [])]
    if not path_list:
        return None

    try:
        parent_dirs = [str(Path(x).parent) for x in path_list]
        base = Path(Path.commonpath(parent_dirs))
        return base.resolve()
    except Exception:
        # If anything goes wrong (e.g., different drives on Windows),
        # fall back to None and let the caller decide.
        return None


def normalize_file_paths(paths: Iterable[str]) -> List[str]:
    """
    Normalize a collection of file paths into a list of absolute strings.

    • Coerces each entry to a Path
    • Calls .resolve() to get an absolute path
    • Returns a list[str] suitable for workspace 'files' / 'includes'
    """
    abs_paths: List[str] = [str(Path(p).resolve()) for p in (paths or [])]
    return abs_paths


def merge_workspace_paths(
    current_paths: Iterable[str],
    new_paths: Iterable[str],
) -> List[str]:
    """
    Merge existing workspace paths with new paths using analyzer.merge_includes.

    This function is intentionally UI-agnostic and does not assume anything
    about how the workspace object is stored; it simply performs the path
    merging logic.

    Args:
        current_paths:
            Existing workspace paths (e.g., ws.includes or ws.files).
        new_paths:
            New file paths to add (typically absolute strings).

    Returns:
        A new merged list of paths, with de-duplication handled by merge_includes.
    """
    current_list = list(current_paths or [])
    new_list = list(new_paths or [])
    return merge_includes(current_list, new_list)


def derive_root_if_missing(
    existing_root: Optional[str],
    files: Sequence[str],
) -> Optional[str]:
    """
    If a workspace root is already present, return it as-is.

    Otherwise:
      • Infer a root directory from the given file list
      • Normalize it with normalize_root()
      • Convert to a POSIX-style string

    This mirrors the previous behavior of:
        if not ws.root:
            maybe_root = _common_root_for_files(merged)
            if maybe_root:
                ws.root = _posix_str(_norm_root(maybe_root))

    but in a UI-agnostic, reusable form.

    Args:
        existing_root:
            A POSIX-style string root if the workspace already has one,
            or None/"" if not set.
        files:
            The workspace's file list (typically absolute paths).

    Returns:
        A POSIX-style root string if one can be derived, otherwise the
        original existing_root value.
    """
    # If the root is already set and non-empty, keep it.
    if existing_root:
        return existing_root

    # No root -> attempt to infer from the file set.
    inferred = infer_root_from_files(files)
    if inferred is None:
        return existing_root

    # Normalize and convert to POSIX string for stable storage.
    norm = normalize_root(inferred)
    return to_posix_str(norm)
