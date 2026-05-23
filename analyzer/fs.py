# analyzer/fs.py
from __future__ import annotations
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Iterator, List, Optional, Tuple
from .config import AnalyzerCfg

# ----------------------------
# Ignore rules & file type support
# ----------------------------

SUPPORTED_EXTS: Tuple[str, ...] = (".py",)

EXCLUDE_DIRS = {
    ".git", ".hg", ".svn", "__pycache__", ".venv", "venv", "env",
    ".mypy_cache", ".pytest_cache", "node_modules", "dist", "build",
}

def _glob_contains(s: str, pat: str) -> bool:
    # private: tiny helper; OK to replace with pathspec later
    from fnmatch import fnmatch
    return fnmatch(s, pat)

def _to_posix(p: Path) -> str:
    return p.as_posix()

def _rel_posix(p: Path, root: Path) -> str:
    """
    Return POSIX path of `p` relative to `root` if possible, else absolute POSIX.
    Never raises.
    """
    try:
        return _to_posix(p.resolve(strict=False).relative_to(root.resolve(strict=False)))
    except Exception:
        return _to_posix(p.resolve(strict=False))

def is_excluded(path: Path, cfg: AnalyzerCfg, *, root: Optional[Path] = None) -> bool:
    """
    Cheap glob filter against cfg.exclude (POSIX-ish).
    If `root` is provided, match globs against repo-relative POSIX paths
    for stability; otherwise match against absolute POSIX.
    """
    rel = _rel_posix(path, root) if root else _to_posix(path)
    for pat in getattr(cfg, "exclude", []) or []:
        if _glob_contains(rel, pat):
            return True
    return False

# ----------------------------
# Path normalization & root handling
# ----------------------------

def ensure_dir_root(p: Path) -> Path:
    """
    Treat incoming path as a project's scan root:
    - If it's a directory, return it.
    - If it's a file (or does not exist yet), return its parent directory.
    """
    p = Path(p).expanduser()
    return p if p.is_dir() else p.parent

def normalize(p: Path) -> Path:
    """Best-effort resolve without exploding on missing paths."""
    try:
        return Path(p).expanduser().resolve(strict=False)
    except Exception:
        return Path(p).expanduser()

def is_supported(path: Path) -> bool:
    try:
        q = Path(path)
        return q.is_file() and q.suffix.lower() in SUPPORTED_EXTS
    except Exception:
        return False

def infer_project_root(path: Path) -> Path:
    """
    Normalize an arbitrary input (file or folder) to a project scan root directory.
    """
    p = ensure_dir_root(Path(path))
    if p.name == "__init__.py":  # extremely defensive; usually impossible after ensure_dir_root
        p = p.parent
    return p

def normalize_files(files: Iterable[Path]) -> List[Path]:
    """
    Resolve, de-dup, and retain only supported source files.
    """
    out: List[Path] = []
    seen: set[Path] = set()
    for f in files:
        n = normalize(Path(f))
        if n not in seen and is_supported(n):
            seen.add(n)
            out.append(n)
    return out

# ----------------------------
# Project walking
# ----------------------------

def iter_source_files(root: Path, cfg: Optional[AnalyzerCfg] = None) -> Iterator[Path]:
    """
    Yield all candidate .py files under the scan root, honoring EXCLUDE_DIRS
    and (optionally) cfg. Skips top-level __init__.py at the root directory.

    NOTES:
      - Globs (include/exclude) are matched against repo-relative POSIX paths
        for cross-platform stability.
      - Symlink handling: if cfg.follow_symlinks is False (default), a file is
        skipped when any parent between the file's parent and the root is a symlink.
    """
    root = ensure_dir_root(Path(root)).resolve()
    for p in root.rglob("*.py"):
        # Skip well-known excluded directories quickly
        if any(part in EXCLUDE_DIRS for part in p.parts):
            continue

        # Honor follow_symlinks flag (default False)
        if cfg is not None and not getattr(cfg, "follow_symlinks", False):
            # If any parent in the chain is a symlink, skip
            skip = False
            cur = p.parent
            while cur != root and root in cur.parents:
                if cur.is_symlink():
                    skip = True
                    break
                cur = cur.parent
            if skip:
                continue

        # Skip by exclude globs if cfg provided (match on repo-relative path)
        if cfg is not None and is_excluded(p, cfg, root=root):
            continue

        # Skip top-level __init__.py at the root anchor (just noise)
        if p.parent == root and p.name == "__init__.py":
            continue

        yield p

def iter_project_files(root: Path, cfg: AnalyzerCfg) -> Iterator[Path]:
    """
    Policy-aware wrapper around iter_source_files. Apply include/exclude globs.
    Matching is performed on repo-relative POSIX paths for stability.
    """
    from fnmatch import fnmatch
    includes = getattr(cfg, "include", ["**/*.py"]) or ["**/*.py"]
    root = ensure_dir_root(Path(root)).resolve()
    for p in iter_source_files(root, cfg):
        rel = _rel_posix(p, root)
        if any(fnmatch(rel, pat) for pat in includes):
            yield p

# ----------------------------
# Src-layout sniffing & package chain
# ----------------------------

def detect_src_roots(repo_root: Path, cfg: AnalyzerCfg) -> Tuple[Path, ...]:
    """
    Probe cfg.src_layout_dirs under repo_root and return the ones that exist.
    """
    roots: List[Path] = []
    rr = Path(repo_root).resolve()
    for d in (getattr(cfg, "src_layout_dirs", None) or []):
        p = (rr / d).resolve()
        if p.exists() and p.is_dir():
            roots.append(p)
    return tuple(roots)

def contiguous_pkg_chain(anchor: Path, leaf_dir: Path) -> bool:
    """
    Return True if every directory from anchor .. leaf_dir has an __init__.py.
    """
    anchor = Path(anchor).resolve()
    cur = Path(leaf_dir).resolve()
    if anchor not in cur.parents and cur != anchor:
        return False
    while cur != anchor:
        if not (cur / "__init__.py").exists():
            return False
        cur = cur.parent
    return True

# ----------------------------
# Workspace helpers (manual mode)
# ----------------------------

@dataclass
class Workspace:
    root: Optional[str] = None
    files: List[str] = field(default_factory=list)

def merge_includes(existing: Iterable[str | Path], new_paths: Iterable[str | Path]) -> List[str]:
    """
    Merge two collections of paths, normalize to POSIX strings, preserve order, and de-dup.
    """
    out: List[str] = []
    seen: set[str] = set()

    def add_one(p: str | Path) -> None:
        s = Path(p).expanduser().as_posix()
        if s not in seen:
            seen.add(s)
            out.append(s)

    for p in existing:
        add_one(p)
    for p in new_paths:
        add_one(p)
    return out

def normalize_workspace(ws: Workspace) -> Workspace:
    """
    Normalize file path strings in-place to POSIX and de-dup while preserving order.
    """
    if ws is None:
        return ws
    ws.files = merge_includes([], ws.files)
    return ws

def add_files(ws: Workspace, paths: Iterable[str | Path]) -> Workspace:
    """
    Merge concrete file paths into the workspace.files (no recursion, no patterns).
    """
    ws.files = merge_includes(ws.files, paths)
    return ws

def add_folder(ws: Workspace, folder: str | Path, *, recursive: bool = False) -> Workspace:
    """
    Add *.py files from a user-selected folder (shallow by default).
    """
    base = Path(folder).expanduser()
    it = base.rglob("*.py") if recursive else base.glob("*.py")
    ws.files = merge_includes(ws.files, (p for p in it if p.is_file()))
    return ws

def clear_workspace(ws: Workspace) -> Workspace:
    """Remove all files (does not change root)."""
    ws.files = []
    return ws

def common_root(paths: Iterable[str | Path]) -> Path | None:
    """
    Compute the deepest common directory of the provided paths. Returns None if empty.
    """
    resolved = [Path(p).expanduser().resolve() for p in paths]
    if not resolved:
        return None
    # Use parent for files
    parts_lists = [(p if p.is_dir() else p.parent).parts for p in resolved]
    common: Tuple[str, ...] = ()
    for idx in range(min(len(pl) for pl in parts_lists)):
        token = parts_lists[0][idx]
        if all(len(pl) > idx and pl[idx] == token for pl in parts_lists):
            common = common + (token,)
        else:
            break
    return Path(*common) if common else None
